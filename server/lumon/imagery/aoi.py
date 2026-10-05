"""
Areas of interest (AOIs) for satellite analysis.

Two kinds, used the same way by every pipeline:
  - configured AOIs (config/aois.json): imagery is downloaded from Earth
    Search for these small boxes only, never for all of India
  - local AOIs (table local_aois): created by local GeoTIFF/COG ingestion
    (lumon/imagery/local_ingest.py). Their box and pixel grid come from the
    local raster itself; they carry "source": "local" and a "grid".
"""

import json

from .. import db, settings


def load_aois() -> list[dict]:
    """Return every AOI definition: configured ones first, then local ones."""
    aois = json.loads((settings.CONFIG_DIR / "aois.json").read_text())["aois"]
    connection = db.connect()
    rows = connection.execute("SELECT * FROM local_aois ORDER BY created_at, id").fetchall()
    connection.close()
    for row in rows:
        aois.append({
            "id": row["id"], "name": row["name"], "label": "LOCAL AOI", "source": "local",
            "purpose": "Defined by locally supplied GeoTIFF/COG imagery (footprint and grid from the raster itself).",
            "bbox": json.loads(row["bbox"]), "grid": json.loads(row["grid"]),
            "imagery": {"provider": "Local files (operator-supplied GeoTIFF/COG)", "collection": "local", "max_scene_cloud_percent": None},
        })
    return aois


def get_aoi(aoi_id: str) -> dict | None:
    """Return one AOI by id, or None."""
    for aoi in load_aois():
        if aoi["id"] == aoi_id:
            return aoi
    return None


def aoi_folder(aoi_id: str):
    """Folder holding the staged imagery for one AOI."""
    folder = settings.IMAGERY_DIR / aoi_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def aoi_polygon(aoi: dict) -> dict:
    """The AOI bbox as a GeoJSON Polygon (for drawing its footprint)."""
    min_lon, min_lat, max_lon, max_lat = aoi["bbox"]
    return {"type": "Polygon", "coordinates": [[
        [min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat],
    ]]}
