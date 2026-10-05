"""
Satellite archive ingestion for one AOI (Sentinel-2 L2A from Earth Search).

THE IMAGERY INGEST PATH:
  1. search    - ask the STAC catalogue for low-cloud scenes over the AOI
  2. select    - keep at most one scene per month (best candidate first);
                 skip scenes already in the archive (no duplicates)
  3. validate  - required metadata present and understood? If not, the
                 scene is QUARANTINED with a reason, never guessed
  4. download  - read ONLY the AOI window of 6 bands from the remote
                 Cloud-Optimized GeoTIFFs (a few hundred KB per scene)
  5. store     - write one local GeoTIFF per scene, exactly the downloaded
                 numbers (no corrections applied to the stored file)
  6. register  - scenes table row with checksum, CRS, baseline, quality,
                 plus a provenance record

Incremental by design: running it again only fetches months that are not
yet in the archive, and back-filled older scenes are simply added.
"""

import json
from datetime import datetime, timezone

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

from .. import audit, db, net, provenance
from ..geo import geometry
from . import aoi as aoi_module
from . import sentinel2

PROCESSING_VERSION = "s2-archive-v1"
EXPECTED_EPSG = 32643  # UTM zone 43N; the demo AOI lies in this zone


# ---------------------------------------------------------------------------
# 1. Search
# ---------------------------------------------------------------------------

def search_catalogue(aoi: dict, until: str | None = None) -> list[dict]:
    """
    Return STAC items (full JSON) for the AOI, year by year (each request
    returns at most 200 items, so splitting by year avoids paging).
    """
    settings_imagery = aoi["imagery"]
    start_year = int(settings_imagery["start"][:4])
    end = until or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = []
    for year in range(start_year, int(end[:4]) + 1):
        body = {
            "collections": [settings_imagery["collection"]],
            "bbox": aoi["bbox"],
            "datetime": f"{year}-01-01T00:00:00Z/{min(f'{year}-12-31T23:59:59Z', end)}",
            "query": {"eo:cloud_cover": {"lt": settings_imagery["max_scene_cloud_percent"]}},
            "limit": 200,
        }
        result = json.loads(net.fetch_bytes(settings_imagery["stac_search"], timeout=120, json_body=body))
        items.extend(result.get("features", []))
    return items


# ---------------------------------------------------------------------------
# 2-3. Select and validate
# ---------------------------------------------------------------------------

def validate_item(item: dict) -> str | None:
    """
    Return None if the scene's metadata is complete and understood, or the
    quarantine reason. We never invent missing metadata.
    """
    properties = item.get("properties", {})
    if not properties.get("datetime"):
        return "missing acquisition time"
    epsg = properties.get("proj:epsg") or (properties.get("proj:code") or "").replace("EPSG:", "")
    if not epsg:
        return "missing CRS"
    if int(epsg) != EXPECTED_EPSG:
        return f"unexpected CRS EPSG:{epsg} (AOI grid is EPSG:{EXPECTED_EPSG})"
    baseline = properties.get("s2:processing_baseline")
    if sentinel2.baseline_number(baseline) is None or sentinel2.baseline_number(baseline) < 2.0:
        return f"unrecognised processing baseline '{baseline}'"
    if sentinel2.reflectance_offset(baseline, properties.get("earthsearch:boa_offset_applied")) is None:
        return f"cannot decide radiometric offset (baseline {baseline}, boa_offset_applied={properties.get('earthsearch:boa_offset_applied')})"
    for asset_key in sentinel2.BANDS:
        if asset_key not in item.get("assets", {}):
            return f"missing asset '{asset_key}'"
    return None


def covers_aoi(item: dict, aoi: dict) -> bool:
    """
    True if the scene's data footprint contains all four AOI corners.
    The demo AOI sits where two Sentinel-2 tiles overlap; one of them only
    covers a sliver of it, so we prefer the tile that covers it fully.
    """
    shape = item.get("geometry")
    if not shape:
        return False
    min_lon, min_lat, max_lon, max_lat = aoi["bbox"]
    corners = [(min_lon, min_lat), (max_lon, min_lat), (max_lon, max_lat), (min_lon, max_lat)]
    return all(geometry.point_in_geometry(lon, lat, shape) for lon, lat in corners)


def _preference(item: dict, aoi: dict) -> tuple:
    """
    Sort key for choosing between candidates in the same month:
    full AOI coverage first, then lowest scene cloud, then the newest
    reprocessing ('_1_' beats '_0_').
    """
    properties = item["properties"]
    parts = item["id"].split("_")
    version = int(parts[-2]) if parts[-2].isdigit() else 0
    return (0 if covers_aoi(item, aoi) else 1, properties.get("eo:cloud_cover", 100), -version)


def group_by_month(items: list[dict], aoi: dict) -> dict:
    """{'2024-03': [best candidate, next best, ...], ...}"""
    months = {}
    for item in items:
        months.setdefault(item["properties"]["datetime"][:7], []).append(item)
    for month in months:
        months[month].sort(key=lambda item: _preference(item, aoi))
    return months


# ---------------------------------------------------------------------------
# 4-5. Download the AOI window and store it
# ---------------------------------------------------------------------------

def aoi_utm_bounds(aoi: dict) -> tuple:
    """
    The AOI bbox converted to UTM metres and snapped OUTWARD to a 60 m grid.
    60 m is a multiple of every Sentinel-2 pixel size (10, 20, 60 m), so the
    same bounds give perfectly aligned pixels for all bands and all dates.
    """
    left, bottom, right, top = transform_bounds("EPSG:4326", f"EPSG:{EXPECTED_EPSG}", *aoi["bbox"])
    snap = 60
    return (
        np.floor(left / snap) * snap, np.floor(bottom / snap) * snap,
        np.ceil(right / snap) * snap, np.ceil(top / snap) * snap,
    )


def grid_of(aoi: dict) -> tuple[str, tuple]:
    """
    CRS and (left, bottom, right, top) of an AOI's pixel grid. Earth Search
    AOIs use the snapped UTM box above; local AOIs (local_ingest) use the
    grid of their own raster, which is never reprojected or resampled.
    """
    if aoi.get("grid"):
        return aoi["grid"]["crs"], tuple(aoi["grid"]["bounds"])
    return f"EPSG:{EXPECTED_EPSG}", aoi_utm_bounds(aoi)


def download_window(item: dict, bounds: tuple) -> tuple[np.ndarray, dict]:
    """
    Read the AOI window of each band and return (array[6, H, W] uint16, profile).
    20 m bands (B11, SCL) are resampled to the 10 m grid with nearest
    neighbour, which keeps SCL class values intact.
    """
    left, bottom, right, top = bounds
    height = int(round((top - bottom) / 10))
    width = int(round((right - left) / 10))
    layers = []
    with rasterio.Env(**net.gdal_network_options()):
        for asset_key in sentinel2.BANDS:
            href = item["assets"][asset_key]["href"]
            with rasterio.open(href) as source:
                window = from_bounds(left, bottom, right, top, transform=source.transform)
                data = source.read(1, window=window, out_shape=(height, width),
                                   resampling=Resampling.nearest, boundless=True, fill_value=0)
                layers.append(data.astype("uint16"))
    transform = from_origin(left, top, 10, 10)
    profile = {"driver": "GTiff", "height": height, "width": width, "count": len(layers), "dtype": "uint16",
               "crs": f"EPSG:{EXPECTED_EPSG}", "transform": transform, "compress": "deflate",
               "tiled": True, "blockxsize": 256, "blockysize": 256, "nodata": 0}
    return np.stack(layers), profile


def write_scene_file(path, stack: np.ndarray, profile: dict, item: dict) -> None:
    """Write the downloaded numbers unchanged, with band names and source metadata as tags."""
    with rasterio.open(path, "w", **profile) as destination:
        destination.write(stack)
        for index, name in enumerate(sentinel2.BANDS.values(), start=1):
            destination.set_band_description(index, name)
        destination.update_tags(
            scene_id=item["id"],
            datetime=item["properties"]["datetime"],
            processing_baseline=item["properties"].get("s2:processing_baseline", ""),
            boa_offset_applied=str(item["properties"].get("earthsearch:boa_offset_applied")),
            note="Values are the original L2A digital numbers for this window; no corrections applied.",
        )


def measure_quality(stack: np.ndarray) -> tuple[float, float]:
    """
    From the SCL band: share of AOI pixels that are usable, and share that
    are cloud/cirrus/shadow. Returns (valid_fraction, cloud_fraction).
    """
    scl = stack[-1]
    total = scl.size
    invalid = np.isin(scl, list(sentinel2.SCL_INVALID))
    cloudy = np.isin(scl, [3, 8, 9, 10])
    return float((~invalid).sum() / total), float(cloudy.sum() / total)


def quality_status(valid_fraction: float) -> str:
    """usable (>= 90 % clear), degraded (60-90 %), unusable (< 60 %)."""
    if valid_fraction >= 0.9:
        return "usable"
    if valid_fraction >= 0.6:
        return "degraded"
    return "unusable"


# ---------------------------------------------------------------------------
# 6. The whole ingest for one AOI
# ---------------------------------------------------------------------------

def _register_quarantine(connection, aoi_id: str, item: dict, reason: str) -> None:
    """Record a scene we refused to use, so the analyst can see why."""
    properties = item.get("properties", {})
    connection.execute(
        """INSERT OR IGNORE INTO scenes (id, aoi_id, sensor, acquired_at, crs, processing_level, quality_status,
               quarantine_reason, processing_version, source_url, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'quarantined', ?, ?, ?, ?)""",
        (item["id"], aoi_id, properties.get("platform", "sentinel-2"), properties.get("datetime", ""),
         str(properties.get("proj:epsg")), f"L2A baseline {properties.get('s2:processing_baseline')}",
         reason, PROCESSING_VERSION, _item_url(item), db.now_iso()),
    )


def _item_url(item: dict) -> str | None:
    for link in item.get("links", []):
        if link.get("rel") == "self":
            return link.get("href")
    return None


def ingest_aoi(aoi_id: str, max_new_scenes: int | None = None, log=print) -> dict:
    """
    Bring the AOI archive up to date. Returns counts of what happened.

    - max_new_scenes: optional cap for a quick partial run
    - log:            function used to print progress
    """
    net.require_connected("Imagery ingest")
    aoi = aoi_module.get_aoi(aoi_id)
    if aoi is None:
        raise ValueError(f"unknown AOI {aoi_id}")
    if aoi.get("source") == "local":
        raise ValueError(f"AOI {aoi_id} is a local AOI: add its imagery with ingest-local, not Earth Search")
    connection = db.connect()
    folder = aoi_module.aoi_folder(aoi_id)
    bounds = aoi_utm_bounds(aoi)

    items = search_catalogue(aoi)
    months = group_by_month(items, aoi)
    log(f"  catalogue: {len(items)} candidate scenes in {len(months)} months")

    # Months that already have a usable/degraded scene are skipped (incremental).
    have = {row["acquired_at"][:7] for row in connection.execute(
        "SELECT acquired_at FROM scenes WHERE aoi_id = ? AND quality_status IN ('usable', 'degraded')", (aoi_id,))}
    known_ids = {row["id"] for row in connection.execute("SELECT id FROM scenes WHERE aoi_id = ?", (aoi_id,))}

    counts = {"candidates": len(items), "added": 0, "quarantined": 0, "unusable": 0, "skipped_existing_months": 0}
    for month in sorted(months):
        if month in have:
            counts["skipped_existing_months"] += 1
            continue
        if max_new_scenes is not None and counts["added"] >= max_new_scenes:
            break
        for item in months[month][:3]:  # try up to 3 candidates per month
            if item["id"] in known_ids:
                continue
            reason = validate_item(item)
            if reason:
                _register_quarantine(connection, aoi_id, item, reason)
                counts["quarantined"] += 1
                continue
            try:
                stack, profile = download_window(item, bounds)
            except Exception as error:  # network/read failure: try the next candidate
                log(f"  ! {item['id']}: download failed ({error})")
                continue
            valid_fraction, cloud_fraction = measure_quality(stack)
            status = quality_status(valid_fraction)
            # Radiometric check: does the data agree with the metadata?
            properties = item["properties"]
            offset = sentinel2.reflectance_offset(properties.get("s2:processing_baseline"), properties.get("earthsearch:boa_offset_applied"))
            valid_mask = ~np.isin(stack[-1], list(sentinel2.SCL_INVALID))
            dark_result = sentinel2.dark_pixel_check(stack[0], valid_mask)
            inconsistent = sentinel2.check_consistency(offset, dark_result)
            if inconsistent:
                _register_quarantine(connection, aoi_id, item, inconsistent)
                counts["quarantined"] += 1
                continue
            path = folder / f"{item['properties']['datetime'][:10]}_{item['id']}.tif"
            write_scene_file(path, stack, profile, item)
            checksum = net.sha256_of_bytes(path.read_bytes())
            provenance_id = provenance.create(
                connection, kind="scene", source_id="earth-search-sentinel-2-l2a",
                input_ref=_item_url(item), input_sha256=checksum,
                processing="read AOI window of B02,B03,B04,B08,B11,SCL; 20 m bands nearest-resampled to 10 m; stored unchanged",
                processing_version=PROCESSING_VERSION,
                parameters={"utm_bounds": [float(b) for b in bounds], "epsg": EXPECTED_EPSG,
                            "reflectance_offset": offset, "dark_pixel_check": dark_result,
                            "catalogue_boa_offset_applied": properties.get("earthsearch:boa_offset_applied")},
                source_version=properties.get("s2:product_uri"),
                retrieved_at=db.now_iso(),
            )
            connection.execute(
                """INSERT OR REPLACE INTO scenes (id, aoi_id, sensor, acquired_at, crs, processing_level, file_path,
                       file_sha256, cloud_fraction, radiometric_offset, quality_status, quarantine_reason, processing_version,
                       source_url, provenance_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                (item["id"], aoi_id, properties.get("platform"), properties["datetime"], f"EPSG:{EXPECTED_EPSG}",
                 f"L2A baseline {properties.get('s2:processing_baseline')}", str(path), checksum, round(cloud_fraction, 4),
                 offset, status, PROCESSING_VERSION, _item_url(item), provenance_id, db.now_iso()),
            )
            connection.commit()
            log(f"  + {item['id']}  clear={valid_fraction:.0%}  {status}")
            if status == "unusable":
                # Keep the record (so we know it was tried) but not the file.
                path.unlink()
                connection.execute("UPDATE scenes SET file_path = NULL WHERE id = ?", (item["id"],))
                connection.commit()
                counts["unusable"] += 1
                continue  # try the next candidate for this month
            counts["added"] += 1
            break
        connection.commit()

    audit.record(connection, "system", "imagery-ingest", aoi_id, counts)
    connection.close()
    return counts


def recheck_radiometry(aoi_id: str, log=print) -> dict:
    """
    Re-decide the radiometric offset of every staged scene of an AOI from
    the metadata stored in the file's tags, cross-checked with the
    dark-pixel test. Scenes where the two disagree are quarantined. Cached
    tile observations are cleared so they are recomputed with the result.
    """
    connection = db.connect()
    counts = {"checked": 0, "quarantined": 0}
    for row in connection.execute("SELECT id, file_path FROM scenes WHERE aoi_id = ? AND file_path IS NOT NULL", (aoi_id,)).fetchall():
        with rasterio.open(row["file_path"]) as source:
            tags = source.tags()
            blue, scl = source.read(1), source.read(6)
        flag = {"True": True, "False": False}.get(tags.get("boa_offset_applied"))
        offset = sentinel2.reflectance_offset(tags.get("processing_baseline"), flag)
        dark_result = sentinel2.dark_pixel_check(blue, ~np.isin(scl, list(sentinel2.SCL_INVALID)))
        reason = "cannot decide radiometric offset" if offset is None else sentinel2.check_consistency(offset, dark_result)
        counts["checked"] += 1
        if reason:
            connection.execute("UPDATE scenes SET quality_status = 'quarantined', quarantine_reason = ?, radiometric_offset = NULL WHERE id = ?",
                               (reason, row["id"]))
            counts["quarantined"] += 1
            log(f"  quarantined {row['id']}: {reason}")
        else:
            connection.execute("UPDATE scenes SET radiometric_offset = ? WHERE id = ?", (offset, row["id"]))
        connection.execute("DELETE FROM tile_observations WHERE scene_id = ?", (row["id"],))
    connection.commit()
    audit.record(connection, "system", "recheck-radiometry", aoi_id, counts)
    connection.close()
    return counts
