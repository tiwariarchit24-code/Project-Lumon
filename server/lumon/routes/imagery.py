"""
Satellite routes: AOIs, scenes, quicklook chips, tile history, change
events and similarity search.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .. import db, review, worker
from ..geo import geometry
from ..imagery import aoi as aoi_module
from ..imagery import archive, quicklook, semantic, sentinel2, similarity
from rasterio.warp import transform as warp_transform

router = APIRouter(prefix="/api")


def image_corners(aoi: dict) -> list[list[float]]:
    """
    Where the AOI's full-scene image sits on the map: its four corners
    (top-left, top-right, bottom-right, bottom-left) converted from the UTM
    grid the image was cut on to longitude/latitude. MapLibre stretches
    the image between these corners, so it lines up exactly even though
    the UTM grid is slightly rotated relative to north.
    """
    crs, (left, bottom, right, top) = archive.grid_of(aoi)
    lons, lats = warp_transform(crs, "EPSG:4326", [left, right, right, left], [top, top, bottom, bottom])
    return [[round(lon, 6), round(lat, 6)] for lon, lat in zip(lons, lats)]


@router.get("/aois")
def aois():
    """AOIs with their footprint and what is staged for them."""
    connection = db.connect()
    result = []
    for aoi in aoi_module.load_aois():
        row = connection.execute(
            """SELECT COUNT(*) AS n, MIN(acquired_at) AS first, MAX(acquired_at) AS last FROM scenes
               WHERE aoi_id = ? AND quality_status IN ('usable','degraded')""", (aoi["id"],)).fetchone()
        quarantined = connection.execute("SELECT COUNT(*) FROM scenes WHERE aoi_id = ? AND quality_status = 'quarantined'", (aoi["id"],)).fetchone()[0]
        unusable = connection.execute("SELECT COUNT(*) FROM scenes WHERE aoi_id = ? AND quality_status = 'unusable'", (aoi["id"],)).fetchone()[0]
        accepted = connection.execute("SELECT COUNT(*) FROM change_candidates WHERE aoi_id = ? AND status = 'accepted'", (aoi["id"],)).fetchone()[0]
        suppressed = connection.execute("SELECT COUNT(*) FROM change_candidates WHERE aoi_id = ? AND status = 'suppressed'", (aoi["id"],)).fetchone()[0]
        latest = connection.execute(
            """SELECT id FROM scenes WHERE aoi_id = ? AND quality_status = 'usable' AND file_path IS NOT NULL
               AND radiometric_offset IS NOT NULL ORDER BY acquired_at DESC LIMIT 1""", (aoi["id"],)).fetchone()
        result.append({**aoi, "footprint": aoi_module.aoi_polygon(aoi), "image_corners": image_corners(aoi),
                       "latest_scene": latest["id"] if latest else None, "scenes": row["n"], "first_scene": row["first"],
                       "last_scene": row["last"], "quarantined": quarantined, "unusable": unusable,
                       "changes_accepted": accepted, "changes_suppressed": suppressed})
    connection.close()
    return result


@router.get("/aois/{aoi_id}/scenes")
def scenes(aoi_id: str):
    """Every scene record for an AOI, including quarantined ones and why."""
    connection = db.connect()
    rows = [dict(r) for r in connection.execute("SELECT * FROM scenes WHERE aoi_id = ? ORDER BY acquired_at", (aoi_id,))]
    connection.close()
    for row in rows:
        row.pop("file_path", None)  # local paths are not useful to the browser
    return rows


@router.get("/scenes/{scene_id}/quicklook.png")
def scene_quicklook(scene_id: str, bbox: str | None = None, upscale: int = 1):
    """True-colour PNG chip of a staged scene (optionally cut to a lon/lat bbox)."""
    connection = db.connect()
    row = connection.execute("SELECT file_path, radiometric_offset FROM scenes WHERE id = ?", (scene_id,)).fetchone()
    connection.close()
    if row is None or not row["file_path"] or row["radiometric_offset"] is None:
        raise HTTPException(404, "scene file not staged")
    box = [float(v) for v in bbox.split(",")] if bbox else None
    png = quicklook.render_chip(row["file_path"], row["radiometric_offset"], box, max(1, min(upscale, 6)))
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@router.get("/tiles/at")
def tile_at(lon: float, lat: float):
    """
    The 100 m analysis tile under a map click, with its full observation
    history (one row per staged scene). Returns 404 outside staged AOIs.
    """
    connection = db.connect()
    for aoi in aoi_module.load_aois():
        if not geometry.bbox_contains(aoi["bbox"], lon, lat):
            continue
        for tile in connection.execute("SELECT * FROM tiles WHERE aoi_id = ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
                                       (aoi["id"], lon - 0.002, lon + 0.002, lat - 0.002, lat + 0.002)):
            shape = db.from_json(tile["geometry"])
            if geometry.point_in_geometry(lon, lat, shape):
                history = [dict(r) for r in connection.execute(
                    """SELECT s.id AS scene_id, substr(s.acquired_at, 1, 10) AS date, o.valid_fraction, o.land_class, o.class_fractions
                       FROM tile_observations o JOIN scenes s ON s.id = o.scene_id WHERE o.tile_id = ? ORDER BY s.acquired_at""", (tile["id"],))]
                for item in history:
                    item["class_fractions"] = db.from_json(item["class_fractions"])
                changes = [dict(r) for r in connection.execute(
                    "SELECT id, change_class, status, earliest_supported_after FROM change_candidates WHERE tile_ids LIKE ?", (f'%"{tile["id"]}"%',))]
                connection.close()
                return {"tile_id": tile["id"], "aoi_id": aoi["id"], "aoi_name": aoi["name"], "geometry": shape,
                        "lon": tile["lon"], "lat": tile["lat"], "history": history, "changes": changes,
                        "classifier": "spectral-index thresholds (uncalibrated)", "scl_invalid_classes": [sentinel2.SCL_NAMES[c] for c in sorted(sentinel2.SCL_INVALID)]}
    connection.close()
    raise HTTPException(404, "NO IMAGERY STAGED at this location")


@router.get("/changes")
def changes(status: str = "accepted", aoi_id: str | None = None):
    """Change candidates as GeoJSON polygons (status: accepted | suppressed | all)."""
    sql = "SELECT * FROM change_candidates WHERE 1 = 1"
    params = []
    if status != "all":
        sql += " AND status = ?"
        params.append(status)
    if aoi_id:
        sql += " AND aoi_id = ?"
        params.append(aoi_id)
    connection = db.connect()
    features = []
    for row in connection.execute(sql, params):
        features.append({"type": "Feature", "geometry": db.from_json(row["geometry"]), "properties": {
            "id": row["id"], "change_class": row["change_class"], "status": row["status"], "review_state": row["review_state"],
            "score": row["score"], "earliest": row["earliest_supported_after"], "last_before": row["last_clean_before"],
            "area_m2": row["area_m2"], "lon": row["lon"], "lat": row["lat"]}})
    connection.close()
    return {"type": "FeatureCollection", "features": features}


@router.get("/changes/{candidate_id}")
def change_detail(candidate_id: str):
    """Full evidence for one change candidate."""
    candidate = review.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(404, "change not found")
    return candidate


class SimilarRequest(BaseModel):
    aoi_id: str
    positive: list[str]
    negative: list[str] = []
    limit: int = 10


@router.post("/similar")
def similar(request: SimilarRequest):
    """Image-to-image similarity from example tiles (non-semantic features)."""
    return similarity.find_similar(request.aoi_id, request.positive, request.negative, request.limit)


@router.post("/aois/{aoi_id}/ingest")
def ingest(aoi_id: str):
    """Queue an incremental imagery ingest (CONNECTED mode only)."""
    return {"job_id": worker.enqueue("imagery-ingest", {"aoi_id": aoi_id})}


@router.post("/aois/{aoi_id}/analyse")
def analyse(aoi_id: str):
    """Queue a change-analysis run."""
    return {"job_id": worker.enqueue("change-analysis", {"aoi_id": aoi_id})}


# ---------------------------------------------------------------------------
# Semantic image search (RemoteCLIP). Scores are model similarity, never a
# probability or a detection; see lumon/imagery/semantic.py.
# ---------------------------------------------------------------------------

class SemanticSearchRequest(BaseModel):
    text: str
    limit: int = 20
    start: str | None = None
    end: str | None = None
    aoi_id: str | None = None


@router.get("/semantic/status")
def semantic_status():
    """Retrieval status: NOT STAGED / INDEXING / READY / PARTIAL / UNAVAILABLE, with counts and model version."""
    return semantic.status()


@router.post("/semantic/index")
def semantic_index(aoi_id: str | None = None):
    """Queue an (incremental, cached) embedding run. Refused when the model is not staged."""
    problems = semantic.model_problems()
    if problems:
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": problems})
    return {"job_id": worker.enqueue("semantic-index", {"aoi_id": aoi_id, "actor": "analyst"})}


@router.post("/semantic/search")
def semantic_search(request: SemanticSearchRequest):
    """Rank indexed chips by similarity to the text. Never falls back to keyword or rule matching."""
    try:
        return semantic.search(request.text, max(1, min(request.limit, 200)), request.start, request.end, request.aoi_id)
    except semantic.ModelNotStaged as error:
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": [str(error)]})
    except semantic.ModelUnavailable as error:
        raise HTTPException(503, {"state": "UNAVAILABLE", "reasons": [str(error)]})
    except ValueError as error:
        raise HTTPException(400, str(error))


class SemanticAt(BaseModel):
    lon: float
    lat: float
    scene_id: str | None = None


class SemanticSimilarRequest(BaseModel):
    chip_ids: list[str] = []
    negative_ids: list[str] = []
    at: SemanticAt | None = None  # or: use the chip under a map point
    scope: str = "other-places"
    limit: int = 20
    start: str | None = None
    end: str | None = None


@router.post("/semantic/similar")
def semantic_similar(request: SemanticSimilarRequest):
    """
    Image-to-image similarity with RemoteCLIP embeddings (cached; no model
    inference at query time). Examples are chip ids, or the chip under a
    map point. Refused when the model is not staged; never substituted.
    """
    chip_ids = list(request.chip_ids)
    try:
        if request.at is not None:
            semantic._require_model_files()
            found = semantic.chips_at(request.at.lon, request.at.lat, request.at.scene_id)
            if not found:
                raise HTTPException(404, "no indexed image chip at this location")
            chip_ids = [found[0]["id"]] + chip_ids
        return semantic.similar(chip_ids, request.negative_ids, request.scope, max(1, min(request.limit, 100)),
                                request.start, request.end)
    except semantic.ModelNotStaged as error:
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": [str(error)]})
    except semantic.ModelUnavailable as error:
        raise HTTPException(503, {"state": "UNAVAILABLE", "reasons": [str(error)]})
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.get("/semantic/chips/at")
def semantic_chips_at(lon: float, lat: float, scene_id: str | None = None):
    """Indexed chips containing a point (latest indexed scene unless scene_id is given)."""
    return semantic.chips_at(lon, lat, scene_id)


@router.get("/semantic/chips/{chip_id:path}")
def semantic_chip(chip_id: str):
    """One indexed chip: scene metadata, footprint and provenance."""
    record = semantic.chip_record(chip_id)
    if record is None:
        raise HTTPException(404, "chip not indexed")
    return record
