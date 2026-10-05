"""
Build and store per-tile observations for every staged scene of an AOI.

The tile grid is the same for every scene (see archive.grid_of), so
tile (row, col) always covers the same 100 m x 100 m square on the ground.
Observations are computed once per scene and cached in the database, so
adding a new scene only processes that scene (incremental).
"""

import numpy as np
from rasterio.warp import transform as warp_transform

from .. import db
from . import aoi as aoi_module
from . import archive, features


def tile_id(aoi_id: str, row: int, col: int) -> str:
    return f"{aoi_id}:{row}:{col}"


def ensure_tiles(connection, aoi: dict, rows: int, cols: int) -> None:
    """
    Create the tiles table rows for an AOI (once). Each tile gets its
    corner coordinates converted from UTM metres to longitude/latitude.
    """
    existing = connection.execute("SELECT COUNT(*) FROM tiles WHERE aoi_id = ?", (aoi["id"],)).fetchone()[0]
    if existing == rows * cols:
        return
    crs, (left, bottom, right, top) = archive.grid_of(aoi)
    size = features.TILE_PIXELS * 10  # metres (the pipeline requires 10 m pixels)
    for row in range(rows):
        for col in range(cols):
            x0, x1 = left + col * size, left + (col + 1) * size
            y1, y0 = top - row * size, top - (row + 1) * size
            xs = [x0, x1, x1, x0, x0, (x0 + x1) / 2]
            ys = [y0, y0, y1, y1, y0, (y0 + y1) / 2]
            lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)
            ring = [[round(lons[i], 6), round(lats[i], 6)] for i in range(5)]
            connection.execute(
                "INSERT OR REPLACE INTO tiles (id, aoi_id, row, col, lon, lat, geometry) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tile_id(aoi["id"], row, col), aoi["id"], row, col, round(lons[5], 6), round(lats[5], 6),
                 db.to_json({"type": "Polygon", "coordinates": [ring]})),
            )
    connection.commit()


def process_scene(connection, aoi: dict, scene: dict) -> int:
    """
    Compute tile observations for one scene unless they already exist.
    Returns the number of tiles written (0 if cached).
    """
    cached = connection.execute("SELECT COUNT(*) FROM tile_observations WHERE scene_id = ?", (scene["id"],)).fetchone()[0]
    if cached:
        return 0
    bands, valid = features.load_reflectance(scene["file_path"], scene["radiometric_offset"])
    tiles = features.summarise_tiles(bands, valid)
    rows = max(t["row"] for t in tiles) + 1
    cols = max(t["col"] for t in tiles) + 1
    ensure_tiles(connection, aoi, rows, cols)
    for tile in tiles:
        connection.execute(
            """INSERT OR REPLACE INTO tile_observations (scene_id, tile_id, valid_fraction, land_class, class_fractions, features)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (scene["id"], tile_id(aoi["id"], tile["row"], tile["col"]), tile["valid_fraction"], tile["land_class"],
             db.to_json(tile["class_fractions"]), db.to_json(tile["features"])),
        )
    connection.commit()
    return len(tiles)


def usable_scenes(connection, aoi_id: str) -> list[dict]:
    """Scenes with a stored file and usable/degraded quality, oldest first."""
    rows = connection.execute(
        """SELECT * FROM scenes WHERE aoi_id = ? AND file_path IS NOT NULL AND radiometric_offset IS NOT NULL
           AND quality_status IN ('usable', 'degraded') ORDER BY acquired_at""", (aoi_id,))
    return [dict(row) for row in rows]


def process_aoi(aoi_id: str, log=print) -> dict:
    """Make sure every usable scene of the AOI has tile observations."""
    connection = db.connect()
    aoi = aoi_module.get_aoi(aoi_id)
    scenes = usable_scenes(connection, aoi_id)
    new = 0
    for scene in scenes:
        if process_scene(connection, aoi, scene):
            new += 1
    connection.close()
    log(f"  observations: {len(scenes)} scenes, {new} newly processed")
    return {"scenes": len(scenes), "newly_processed": new}


def load_sequences(connection, aoi_id: str) -> tuple[list[dict], dict]:
    """
    Return (scenes, sequences) where sequences[tile_id] is a list, in date
    order, of {scene_id, date, land_class, valid_fraction, fractions}.
    Cloudy observations are included with land_class None so the change
    engine can count how many were masked.
    """
    scenes = usable_scenes(connection, aoi_id)
    order = {scene["id"]: index for index, scene in enumerate(scenes)}
    sequences = {}
    rows = connection.execute(
        """SELECT o.scene_id, o.tile_id, o.valid_fraction, o.land_class, o.class_fractions, o.features
           FROM tile_observations o JOIN scenes s ON s.id = o.scene_id
           WHERE s.aoi_id = ? AND s.quality_status IN ('usable', 'degraded') AND s.file_path IS NOT NULL""", (aoi_id,))
    for row in rows:
        scene_index = order.get(row["scene_id"])
        if scene_index is None:
            continue
        sequences.setdefault(row["tile_id"], []).append({
            "index": scene_index, "scene_id": row["scene_id"], "date": scenes[scene_index]["acquired_at"][:10],
            "land_class": row["land_class"], "valid_fraction": row["valid_fraction"],
            "fractions": db.from_json(row["class_fractions"]) or {},
            "features": db.from_json(row["features"]) or {},
        })
    for sequence in sequences.values():
        sequence.sort(key=lambda item: item["index"])
    return scenes, sequences


def latest_feature_matrix(connection, aoi_id: str) -> tuple[list[str], np.ndarray, list[str]]:
    """
    For similarity search: the most recent clear feature vector per tile,
    plus the change in those features since the earliest clear observation.
    Returns (tile_ids, matrix[n_tiles, n_features], feature_names).
    """
    scenes, _ = load_sequences(connection, aoi_id)
    order = {scene["id"]: index for index, scene in enumerate(scenes)}
    first, last = {}, {}
    for row in connection.execute(
        """SELECT o.scene_id, o.tile_id, o.features FROM tile_observations o JOIN scenes s ON s.id = o.scene_id
           WHERE s.aoi_id = ? AND o.valid_fraction >= ?""", (aoi_id, features.MIN_VALID_FRACTION)):
        index = order.get(row["scene_id"])
        if index is None or row["features"] is None:
            continue
        tile = row["tile_id"]
        if tile not in first or index < first[tile][0]:
            first[tile] = (index, db.from_json(row["features"]))
        if tile not in last or index > last[tile][0]:
            last[tile] = (index, db.from_json(row["features"]))
    names = sorted(next(iter(last.values()))[1].keys()) if last else []
    tile_ids = sorted(last)
    matrix = np.array([[last[t][1][n] for n in names] + [last[t][1][n] - first[t][1][n] for n in names] for t in tile_ids], dtype="float32")
    return tile_ids, matrix, names + [f"delta_{n}" for n in names]
