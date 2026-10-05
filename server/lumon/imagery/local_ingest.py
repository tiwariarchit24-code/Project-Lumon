"""
LOCAL OFFLINE INGESTION of GeoTIFF / Cloud Optimized GeoTIFF files.

This is the second way imagery enters Lumon, next to the Earth Search
download (lumon/imagery/archive.py). It needs no network, reads only local
files, and registers each file in the SAME scenes table the rest of Lumon
uses (semantic index, image similarity, discovery, both change engines,
review, export). Nothing is reprojected, resampled or copied.

    local file -> validate -> read metadata -> (band mapping) -> register scene
               -> existing pipeline (semantic-index, discovery-update, analyse,
                  ml-change-run) — each run incrementally, as before

WHAT IS READ FROM THE FILE (never guessed):
  format (GeoTIFF; COG when GDAL reports LAYOUT=COG), CRS, affine transform,
  width/height, resolution, bounds, band count, band names, data types,
  nodata, and these tags if present:
    acquisition time   one of ACQUISITION_TAGS (NOT the TIFF "DateTime" tag,
                       which is the file's write time)
    sensor/platform    one of SENSOR_TAGS
    processing level   one of LEVEL_TAGS
    radiometric offset only from the Sentinel-2 pair processing_baseline +
                       boa_offset_applied (the convention of Lumon's own files)
Anything absent stays UNKNOWN unless the operator supplies it. A value given
by both the file and the operator must agree, or ingestion stops.

THE PIPELINE'S BAND CONTRACT (why a band mapping exists):
Lumon's imagery pipelines read six bands in this order, by name:
    B02 (blue), B03 (green), B04 (red), B08 (NIR), B11 (SWIR), SCL (quality)
as uint16 Sentinel-2 L2A digital numbers at 10 m. A local file is mapped to
these canonical names either by its own band descriptions (if they are
exactly these names) or by an explicit operator mapping such as
"B02=1,B03=2,B04=3,B08=4,B11=5,SCL=6". Bands are never identified by
position alone. If the file is already in canonical order it is used as is;
otherwise a small GDAL VRT (a few KB of XML pointing at the original file)
presents the bands in canonical order — no pixel data is duplicated.

COMPATIBILITY, reported per capability (never forced):
  SUPPORTED, NOT COMPATIBLE (reason) or NOT ENOUGH METADATA (reason).
  A file that fails a pipeline requirement is still registered, but marked
  quarantined with that reason, so no pipeline uses it by mistake. A file
  without a known acquisition date is recorded (status pending-metadata) but
  not added to the scenes table until a date is supplied.

INCREMENTAL: files are identified by SHA-256. The same file twice is
ALREADY INGESTED (nothing changes). A new file adds one scene; the existing
indexers then process only that scene.
"""

import hashlib
import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import rasterio
from rasterio.warp import transform_bounds

from .. import audit, db, provenance
from ..sources.common import iso_from_text
from . import aoi as aoi_module
from . import archive, sentinel2

INGEST_VERSION = "local-ingest-v1"
CANONICAL_BANDS = sentinel2.REFLECTANCE_BANDS + ["SCL"]  # B02, B03, B04, B08, B11, SCL
PIPELINE_PIXEL_M = 10.0
ACQUISITION_TAGS = ["datetime", "ACQUISITION_DATETIME", "ACQUISITION_DATE", "SENSING_TIME", "PRODUCT_START_TIME",
                    "DATATAKE_1_DATATAKE_SENSING_START"]
SENSOR_TAGS = ["platform", "PLATFORM", "SPACECRAFT_NAME", "SATELLITE", "SENSOR"]
LEVEL_TAGS = ["processing_level", "PROCESSING_LEVEL", "PRODUCT_TYPE"]
REMOTE_PREFIXES = ("http://", "https://", "s3://", "gs://", "/vsicurl", "/vsis3", "/vsigs", "/vsiaz")
CAPABILITIES = ["registration", "semantic-index", "image-similarity", "discovery", "rule-based-change", "learned-change"]


class LocalIngestError(ValueError):
    """The file cannot be ingested; the message says why."""


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def sha256_of_file(path: Path) -> str:
    """SHA-256 read in 4 MB chunks (organiser files can be large)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_file(path: str) -> dict:
    """
    Validate a local raster and return its metadata. Raises LocalIngestError
    with a readable reason for anything Lumon cannot register.
    """
    text = str(path)
    if text.lower().startswith(REMOTE_PREFIXES):
        raise LocalIngestError(f"{text}: local files only (this path is remote); use the Earth Search ingest for remote imagery")
    file = Path(text).expanduser().resolve()
    if not file.is_file():
        raise LocalIngestError(f"{file}: file not found")
    try:
        source = rasterio.open(file)
    except rasterio.errors.RasterioIOError as error:
        raise LocalIngestError(f"{file.name}: not a readable raster ({error})") from error
    with source:
        if source.driver != "GTiff":
            raise LocalIngestError(f"{file.name}: unsupported format {source.driver}; only GeoTIFF / COG are accepted")
        if source.width == 0 or source.height == 0 or source.count == 0:
            raise LocalIngestError(f"{file.name}: empty raster ({source.width} x {source.height} px, {source.count} bands)")
        if source.crs is None:
            raise LocalIngestError(f"{file.name}: no coordinate reference system; Lumon does not guess one")
        transform = source.transform
        if transform.is_identity:
            raise LocalIngestError(f"{file.name}: no georeferencing (identity transform)")
        if transform.b != 0 or transform.d != 0:
            raise LocalIngestError(f"{file.name}: rotated/sheared geotransform; Lumon needs a north-up grid and does not resample")
        layout = source.tags(ns="IMAGE_STRUCTURE").get("LAYOUT", "")
        tags = source.tags()
        metadata = {
            "path": str(file), "filename": file.name, "size_bytes": file.stat().st_size,
            "format": "COG" if layout.upper() == "COG" else ("GeoTIFF (tiled)" if source.profile.get("tiled") else "GeoTIFF (striped)"),
            "overviews": len(source.overviews(1)),
            "crs": source.crs.to_string(), "crs_is_projected": bool(source.crs.is_projected),
            "crs_units": source.crs.linear_units if source.crs.is_projected else None,
            "transform": list(transform)[:6], "width": source.width, "height": source.height,
            "resolution": [abs(transform.a), abs(transform.e)],
            "bounds": list(source.bounds), "bounds_wgs84": [round(v, 7) for v in transform_bounds(source.crs, "EPSG:4326", *source.bounds)],
            "band_count": source.count, "band_descriptions": [d for d in source.descriptions],
            "dtypes": list(source.dtypes), "nodata": source.nodata,
            "tags": {k: v for k, v in tags.items() if len(str(v)) < 500},
        }
    return metadata


def _first_tag(tags: dict, names: list[str]) -> tuple[str | None, str | None]:
    for name in names:
        if tags.get(name) not in (None, "", "None"):
            return name, str(tags[name])
    return None, None


def parse_band_mapping(text: str | None, metadata: dict) -> tuple[dict | None, str]:
    """
    The canonical-name -> source-band mapping, and where it came from:
    explicit operator text ("B02=1,..."), else the file's own band names when
    they include canonical names, else none. Raises on an inconsistent mapping.
    """
    count = metadata["band_count"]
    if text:
        mapping = {}
        for part in text.split(","):
            if "=" not in part:
                raise LocalIngestError(f"band mapping '{part.strip()}': expected NAME=INDEX, e.g. B04=3")
            name, index = (p.strip() for p in part.split("=", 1))
            name = name.upper()
            if name not in CANONICAL_BANDS:
                raise LocalIngestError(f"band mapping: unknown canonical band '{name}' (allowed: {', '.join(CANONICAL_BANDS)})")
            if name in mapping:
                raise LocalIngestError(f"band mapping: {name} given twice")
            if not index.isdigit() or not 1 <= int(index) <= count:
                raise LocalIngestError(f"band mapping: {name}={index} is not a band of this file (1-{count})")
            mapping[name] = int(index)
        if len(set(mapping.values())) != len(mapping):
            raise LocalIngestError("band mapping: the same source band is assigned to two canonical bands")
        return mapping, "operator"
    named = {d: i for i, d in enumerate(metadata["band_descriptions"], start=1) if d in CANONICAL_BANDS}
    if named:
        return named, "file band descriptions"
    return None, "none"


# ---------------------------------------------------------------------------
# Compatibility with the existing pipeline
# ---------------------------------------------------------------------------

def compatibility(metadata: dict, mapping: dict | None, acquired_at: str | None, offset: float | None) -> dict:
    """SUPPORTED / NOT COMPATIBLE (reason) / NOT ENOUGH METADATA (reason) per capability."""
    pipeline = []
    if not mapping or set(mapping) != set(CANONICAL_BANDS):
        missing = [b for b in CANONICAL_BANDS if not mapping or b not in mapping]
        pipeline.append(f"needs canonical bands {', '.join(CANONICAL_BANDS)}; missing {', '.join(missing)}"
                        + ("" if mapping else f" (file has {metadata['band_count']} band(s) without canonical names: give --bands)"))
    else:
        dtypes = {metadata["dtypes"][i - 1] for i in mapping.values()}
        if dtypes != {"uint16"}:
            pipeline.append(f"needs uint16 Sentinel-2 L2A digital numbers; mapped bands are {', '.join(sorted(dtypes))}")
    if not (metadata["crs_is_projected"] and str(metadata["crs_units"]).lower() in ("metre", "meter", "m")):
        pipeline.append(f"needs a projected CRS in metres; file CRS is {metadata['crs']}")
    elif any(abs(r - PIPELINE_PIXEL_M) > 1e-6 for r in metadata["resolution"]):
        pipeline.append(f"needs {PIPELINE_PIXEL_M:g} m pixels; file has {metadata['resolution'][0]:g} x {metadata['resolution'][1]:g} m (Lumon does not resample)")
    not_compatible = "; ".join(pipeline)
    metadata_gaps = []
    if acquired_at is None:
        metadata_gaps.append("acquisition date UNKNOWN (give --acquired)")
    if offset is None and not not_compatible:
        metadata_gaps.append("radiometric offset UNKNOWN (give --reflectance-offset, e.g. 0 or -0.1)")
    result = {"registration": "SUPPORTED" if acquired_at else "NOT ENOUGH METADATA: acquisition date UNKNOWN (give --acquired)"}
    for capability in CAPABILITIES[1:]:
        if not_compatible:
            result[capability] = f"NOT COMPATIBLE: {not_compatible}"
        elif metadata_gaps:
            result[capability] = f"NOT ENOUGH METADATA: {'; '.join(metadata_gaps)}"
        else:
            result[capability] = "SUPPORTED"
    if result["learned-change"] == "SUPPORTED":
        result["learned-change"] = "SUPPORTED (pairs need a second scene on the same pixel grid)"
    if result["rule-based-change"] == "SUPPORTED":
        result["rule-based-change"] = "SUPPORTED (needs >= 2 Jan-May composite years before and after a change)"
    return result


# ---------------------------------------------------------------------------
# Views, quality, AOIs
# ---------------------------------------------------------------------------

def write_band_vrt(metadata: dict, mapping: dict, target: Path) -> Path:
    """
    A GDAL VRT presenting the mapped source bands in canonical order with
    canonical names. Same CRS, geotransform, size and nodata as the source;
    no pixel values are copied (the VRT references the original file).
    """
    with rasterio.open(metadata["path"]) as source:
        wkt = source.crs.to_wkt()
        blocks = [source.block_shapes[i - 1] for i in range(1, source.count + 1)]
    geotransform = ", ".join(repr(v) for v in (metadata["transform"][2], metadata["transform"][0], metadata["transform"][1],
                                               metadata["transform"][5], metadata["transform"][3], metadata["transform"][4]))
    width, height = metadata["width"], metadata["height"]
    gdal_type = {"uint16": "UInt16", "uint8": "Byte", "int16": "Int16", "uint32": "UInt32", "int32": "Int32",
                 "float32": "Float32", "float64": "Float64"}
    lines = [f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
             f"  <SRS>{escape(wkt)}</SRS>", f"  <GeoTransform>{geotransform}</GeoTransform>"]
    for number, name in enumerate(CANONICAL_BANDS, start=1):
        index = mapping[name]
        dtype = gdal_type[metadata["dtypes"][index - 1]]
        block_y, block_x = blocks[index - 1]
        lines += [f'  <VRTRasterBand dataType="{dtype}" band="{number}">', f"    <Description>{name}</Description>"]
        if metadata["nodata"] is not None:
            lines.append(f"    <NoDataValue>{metadata['nodata']}</NoDataValue>")
        lines += ["    <SimpleSource>",
                  f'      <SourceFilename relativeToVRT="0">{escape(metadata["path"])}</SourceFilename>',
                  f"      <SourceBand>{index}</SourceBand>",
                  f'      <SourceProperties RasterXSize="{width}" RasterYSize="{height}" DataType="{dtype}" BlockXSize="{block_x}" BlockYSize="{block_y}" />',
                  f'      <SrcRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />',
                  f'      <DstRect xOff="0" yOff="0" xSize="{width}" ySize="{height}" />',
                  "    </SimpleSource>", "  </VRTRasterBand>"]
    lines.append("</VRTDataset>")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")
    return target


def measure_scl(metadata: dict, scl_band: int) -> tuple[float, float]:
    """(valid_fraction, cloud_fraction) from the SCL band, read block by block (memory-safe)."""
    total = invalid = cloudy = 0
    with rasterio.open(metadata["path"]) as source:
        for _, window in source.block_windows(scl_band):
            scl = source.read(scl_band, window=window)
            total += scl.size
            invalid += int(np.isin(scl, list(sentinel2.SCL_INVALID)).sum())
            cloudy += int(np.isin(scl, [3, 8, 9, 10]).sum())
    return (total - invalid) / total, cloudy / total


def dark_pixel_result(metadata: dict, mapping: dict) -> str:
    """The existing radiometric dark-pixel test on the blue band (read at most ~2048 px wide)."""
    with rasterio.open(metadata["path"]) as source:
        scale = max(1, math.ceil(max(source.width, source.height) / 2048))
        shape = (max(1, source.height // scale), max(1, source.width // scale))
        blue = source.read(mapping["B02"], out_shape=shape)
        scl = source.read(mapping["SCL"], out_shape=shape)
    return sentinel2.dark_pixel_check(blue, ~np.isin(scl, list(sentinel2.SCL_INVALID)))


def _grid(metadata: dict) -> dict:
    return {"crs": metadata["crs"], "bounds": metadata["bounds"], "res": metadata["resolution"],
            "width": metadata["width"], "height": metadata["height"], "transform": metadata["transform"]}


def check_aoi(connection, aoi_id: str, metadata: dict) -> tuple[dict | None, str]:
    """
    The AOI the scene joins. An existing AOI must have exactly the same
    pixel grid (Lumon does not resample); an unknown id becomes a new local
    AOI defined by this raster. Returns (aoi or None if new, note).
    """
    aoi = aoi_module.get_aoi(aoi_id)
    if aoi is None:
        return None, "new local AOI from this raster's grid"
    crs, bounds = archive.grid_of(aoi)
    same_crs = rasterio.crs.CRS.from_string(crs) == rasterio.crs.CRS.from_string(metadata["crs"])
    same_bounds = all(abs(a - b) < 1e-6 for a, b in zip(bounds, metadata["bounds"]))
    # Earth Search AOIs are cut on a 10 m grid; local AOIs keep their raster's resolution.
    grid_res = aoi["grid"]["res"] if aoi.get("grid") else [PIPELINE_PIXEL_M, PIPELINE_PIXEL_M]
    same_res = all(abs(a - b) < 1e-6 for a, b in zip(grid_res, metadata["resolution"]))
    if not (same_crs and same_bounds and same_res):
        raise LocalIngestError(
            f"{metadata['filename']}: pixel grid differs from AOI '{aoi_id}' (AOI {crs} {[round(b, 2) for b in bounds]}, "
            f"file {metadata['crs']} {[round(b, 2) for b in metadata['bounds']]}); Lumon does not resample — "
            "use a new --aoi id for this grid")
    return aoi, "existing AOI, identical pixel grid"


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def ingest_file(path: str, aoi_id: str, acquired: str | None = None, sensor: str | None = None, bands: str | None = None,
                reflectance_offset: float | None = None, level: str | None = None, scene_id: str | None = None,
                aoi_name: str | None = None, actor: str = "cli") -> dict:
    """
    Register one local GeoTIFF/COG. Returns a report with "outcome":
    INGESTED, ALREADY INGESTED, or PENDING METADATA (recorded, date unknown).
    Raises LocalIngestError for files that cannot be registered.
    """
    if not aoi_id or not aoi_id.replace("-", "").replace("_", "").isalnum():
        raise LocalIngestError("--aoi is required (letters, digits, - and _ only)")
    metadata = inspect_file(path)
    checksum = sha256_of_file(Path(metadata["path"]))
    connection = db.connect()
    try:
        existing = connection.execute("SELECT * FROM local_sources WHERE original_sha256 = ?", (checksum,)).fetchone()
        if existing and existing["status"] == "registered":
            # The same file again: nothing changes. Exception: a scene that was
            # registered but kept out of every pipeline (quarantined as NOT
            # COMPATIBLE / NOT ENOUGH METADATA) may be re-registered when the
            # operator now supplies metadata — it was never used, so replacing it is safe.
            scene = connection.execute("SELECT quality_status, quarantine_reason FROM scenes WHERE id = ?", (existing["scene_id"],)).fetchone()
            not_used = scene is not None and scene["quality_status"] == "quarantined" and (scene["quarantine_reason"] or "").startswith("local scene not usable")
            operator_metadata = any(v not in (None, "") for v in (acquired, sensor, bands, reflectance_offset, level))
            if not (not_used and operator_metadata):
                return {"outcome": "ALREADY INGESTED", "scene_id": existing["scene_id"], "aoi_id": existing["aoi_id"],
                        "sha256": checksum, "path": metadata["path"], "compatibility": json.loads(existing["compatibility"])}
            connection.execute("DELETE FROM scenes WHERE id = ?", (existing["scene_id"],))
            scene_id = existing["scene_id"]
            aoi_id = existing["aoi_id"]

        tags = metadata["tags"]
        # Acquisition time: file tag and/or operator, never invented.
        tag_name, tag_value = _first_tag(tags, ACQUISITION_TAGS)
        from_file = iso_from_text(tag_value, assume_utc=False) if tag_value else None
        if tag_value and from_file is None:
            raise LocalIngestError(f"{metadata['filename']}: acquisition tag {tag_name}='{tag_value}' has no time zone; give --acquired explicitly")
        from_operator = None
        if acquired:
            text = acquired.strip()
            from_operator = iso_from_text(f"{text}T00:00:00Z" if len(text) == 10 else text, assume_utc=False)
            if from_operator is None:
                raise LocalIngestError(f"--acquired '{acquired}': use YYYY-MM-DD or an ISO time with a time zone")
        if from_file and from_operator and from_file[:10] != from_operator[:10]:
            raise LocalIngestError(f"{metadata['filename']}: file says acquired {from_file}, --acquired says {from_operator}")
        acquired_at = from_file or from_operator
        acquired_source = f"file tag {tag_name}" if from_file else ("operator (date only, stored at 00:00Z)" if from_operator and len(acquired.strip()) == 10
                                                                  else "operator" if from_operator else "unknown")
        # Sensor and processing level: operator or file tag, else unknown / none.
        sensor_tag, sensor_value = _first_tag(tags, SENSOR_TAGS)
        sensor_final, sensor_source = (sensor, "operator") if sensor else ((sensor_value, f"file tag {sensor_tag}") if sensor_value else ("unknown", "unknown"))
        level_tag, level_value = _first_tag(tags, LEVEL_TAGS)
        if level:
            level_final, level_source = level, "operator"
        elif level_value:
            level_final, level_source = level_value, f"file tag {level_tag}"
        elif tags.get("processing_baseline"):
            level_final, level_source = f"processing baseline {tags['processing_baseline']}", "file tag processing_baseline"
        else:
            level_final, level_source = None, "unknown"
        # Band mapping.
        mapping, mapping_source = parse_band_mapping(bands, metadata)
        # Radiometric offset: operator, or the Sentinel-2 tag pair; else unknown.
        offset, offset_source = None, "unknown"
        if reflectance_offset is not None:
            offset, offset_source = float(reflectance_offset), "operator"
        elif tags.get("processing_baseline") and tags.get("boa_offset_applied") is not None:
            applied = {"True": True, "False": False}.get(tags["boa_offset_applied"])
            offset = sentinel2.reflectance_offset(tags["processing_baseline"], applied)
            offset_source = "file tags processing_baseline + boa_offset_applied" if offset is not None else "unknown"

        compat = compatibility(metadata, mapping, acquired_at, offset)
        scene_id = scene_id or f"LOCAL_{checksum[:12]}"
        clash = connection.execute("SELECT id FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        if clash:
            raise LocalIngestError(f"scene id {scene_id} already exists for a different file")
        now = db.now_iso()
        aoi, aoi_note = check_aoi(connection, aoi_id, metadata)

        if acquired_at is None:  # NOT ENOUGH METADATA: recorded, not registered as a scene
            connection.execute(
                """INSERT OR REPLACE INTO local_sources (original_sha256, scene_id, aoi_id, original_path, status, metadata, acquired_at,
                       acquired_source, band_mapping, view_path, compatibility, provenance_id, ingest_version, ingested_at)
                   VALUES (?, ?, ?, ?, 'pending-metadata', ?, NULL, 'unknown', ?, NULL, ?, NULL, ?, ?)""",
                (checksum, scene_id, aoi_id, metadata["path"], json.dumps(metadata), json.dumps(mapping), json.dumps(compat), INGEST_VERSION, now))
            connection.commit()
            return {"outcome": "PENDING METADATA", "scene_id": scene_id, "aoi_id": aoi_id, "sha256": checksum, "path": metadata["path"],
                    "metadata": metadata, "compatibility": compat,
                    "reason": "acquisition date UNKNOWN: not in the file; run again with --acquired YYYY-MM-DD"}

        # The raster the pipeline will read: the original if already canonical, else a band-mapping VRT.
        pipeline_ready = compat["semantic-index"] == "SUPPORTED"
        canonical_as_is = (mapping == {name: i for i, name in enumerate(CANONICAL_BANDS, start=1)}
                           and metadata["band_descriptions"][:len(CANONICAL_BANDS)] == CANONICAL_BANDS and metadata["band_count"] == len(CANONICAL_BANDS))
        view_path = Path(metadata["path"])
        if pipeline_ready and not canonical_as_is:
            view_path = write_band_vrt(metadata, mapping, aoi_module.aoi_folder(aoi_id) / "local" / f"{scene_id}.vrt")
        view_sha = checksum if view_path == Path(metadata["path"]) else sha256_of_file(view_path)

        # Quality: measured from the scene's own SCL band, or quarantined with the reason.
        cloud_fraction, dark_result = None, None
        if pipeline_ready:
            valid_fraction, cloud_fraction = measure_scl(metadata, mapping["SCL"])
            status, reason = archive.quality_status(valid_fraction), None
            dark_result = dark_pixel_result(metadata, mapping)
            inconsistent = sentinel2.check_consistency(offset, dark_result)
            if inconsistent:
                status, reason = "quarantined", inconsistent
        else:
            status = "quarantined"
            reason = "local scene not usable by Lumon's imagery pipelines: " + compat["semantic-index"]

        if aoi is None:  # a new local AOI, defined by this raster
            connection.execute(
                "INSERT INTO local_aois (id, name, bbox, grid, created_from, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (aoi_id, aoi_name or f"Local AOI {aoi_id}", json.dumps(metadata["bounds_wgs84"]), json.dumps(_grid(metadata)), checksum, now))

        provenance_id = provenance.create(
            connection, kind="scene", source_id="local-file", input_ref=f"file://{metadata['path']}", input_sha256=checksum,
            processing="registered a local GeoTIFF/COG; pixel values, CRS and grid unchanged"
                       + ("; bands presented in canonical order by a VRT that references the original" if view_path != Path(metadata["path"]) else ""),
            processing_version=INGEST_VERSION,
            parameters={"filename": metadata["filename"], "format": metadata["format"], "crs": metadata["crs"],
                        "width": metadata["width"], "height": metadata["height"], "resolution": metadata["resolution"],
                        "bounds": metadata["bounds"], "bounds_wgs84": metadata["bounds_wgs84"], "transform": metadata["transform"],
                        "nodata": metadata["nodata"], "band_descriptions": metadata["band_descriptions"],
                        "band_mapping": mapping, "band_mapping_source": mapping_source, "view_path": str(view_path), "view_sha256": view_sha,
                        "acquired_at": acquired_at, "acquired_source": acquired_source, "sensor": sensor_final, "sensor_source": sensor_source,
                        "processing_level": level_final, "processing_level_source": level_source,
                        "radiometric_offset": offset, "radiometric_offset_source": offset_source, "dark_pixel_check": dark_result,
                        "aoi": aoi_note, "compatibility": compat},
            retrieved_at=now)
        connection.execute(
            """INSERT INTO scenes (id, aoi_id, sensor, acquired_at, crs, processing_level, file_path, file_sha256, cloud_fraction,
                   radiometric_offset, quality_status, quarantine_reason, processing_version, source_url, provenance_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (scene_id, aoi_id, sensor_final, acquired_at, metadata["crs"], level_final, str(view_path), view_sha,
             None if cloud_fraction is None else round(cloud_fraction, 4), offset, status, reason, INGEST_VERSION,
             f"file://{metadata['path']}", provenance_id, now))
        connection.execute(
            """INSERT OR REPLACE INTO local_sources (original_sha256, scene_id, aoi_id, original_path, status, metadata, acquired_at,
                   acquired_source, band_mapping, view_path, compatibility, provenance_id, ingest_version, ingested_at)
               VALUES (?, ?, ?, ?, 'registered', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (checksum, scene_id, aoi_id, metadata["path"], json.dumps(metadata), acquired_at, acquired_source, json.dumps(mapping),
             str(view_path), json.dumps(compat), provenance_id, INGEST_VERSION, now))
        connection.commit()
        audit.record(connection, actor, "local-ingest", scene_id, {"sha256": checksum, "aoi_id": aoi_id, "status": status,
                                                                   "view": "vrt" if view_path != Path(metadata["path"]) else "original"})
        return {"outcome": "INGESTED", "scene_id": scene_id, "aoi_id": aoi_id, "aoi": aoi_note, "sha256": checksum,
                "path": metadata["path"], "view_path": str(view_path), "quality_status": status, "quarantine_reason": reason,
                "cloud_fraction": cloud_fraction, "acquired_at": acquired_at, "acquired_source": acquired_source,
                "sensor": sensor_final, "band_mapping": mapping, "band_mapping_source": mapping_source,
                "radiometric_offset": offset, "radiometric_offset_source": offset_source, "metadata": metadata,
                "compatibility": compat, "provenance_id": provenance_id}
    finally:
        connection.close()


def ingest_path(path: str, aoi_id: str, **options) -> list[dict]:
    """
    A file, or every .tif/.tiff in a folder (sorted). For a folder, metadata
    options apply to every file, so --acquired and --scene-id are refused
    (they would label several acquisitions with one value).
    """
    folder = Path(path).expanduser()
    if folder.is_dir():
        if options.get("acquired") or options.get("scene_id"):
            raise LocalIngestError("--acquired / --scene-id apply to one file; ingest files one by one, or put the date in the file's tags")
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".tif", ".tiff"))
        if not files:
            raise LocalIngestError(f"{folder}: no .tif/.tiff files")
        reports = []
        for file in files:
            try:
                reports.append(ingest_file(str(file), aoi_id, **options))
            except LocalIngestError as error:
                reports.append({"outcome": "LOCAL INGEST FAILED", "path": str(file), "reason": str(error)})
        return reports
    return [ingest_file(path, aoi_id, **options)]


def describe(scene_id: str) -> dict | None:
    """What Lumon recorded about a locally ingested scene."""
    connection = db.connect()
    row = connection.execute("SELECT * FROM local_sources WHERE scene_id = ?", (scene_id,)).fetchone()
    connection.close()
    if row is None:
        return None
    record = dict(row)
    for key in ("metadata", "band_mapping", "compatibility"):
        record[key] = json.loads(record[key]) if record[key] else None
    return record
