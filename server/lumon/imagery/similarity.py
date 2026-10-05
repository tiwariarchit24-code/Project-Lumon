"""
Image-to-image similarity ("find sites like this one") inside an AOI.

WHAT THE "EMBEDDING" IS HERE:
No neural embedding model is staged on this machine, so each 100 m tile is
described by a small, explainable feature vector computed from the imagery
(see features.py): average reflectance in 5 bands, NDVI/MNDWI/NDBI, texture,
plus how each of those changed between the tile's earliest and latest clear
observation. Similar vectors mean similar spectral appearance and similar
change history - NOT a semantic label such as "quarry". The UI says so.

ALGORITHM:
  1. standardise every feature (z-score over all tiles of the AOI) so no
     single feature dominates because of its units
  2. query vector = average of the positive example tiles; if negative
     examples are given, move away from them (Rocchio-style: q - 0.5 * mean(neg))
  3. rank all tiles by cosine similarity to the query vector
  4. spatial de-duplication: skip tiles within 300 m of a better result or of
     an example, so the list is not ten copies of the same place
  5. explain each match with the features that agree most
"""

import numpy as np

from .. import db
from . import observations

DEDUP_METRES = 300
TILE_METRES = 100


def _grid_distance_m(tile_a: str, tile_b: str) -> float:
    """Distance between two tile centres in metres (same AOI grid)."""
    _, row_a, col_a = tile_a.rsplit(":", 2)
    _, row_b, col_b = tile_b.rsplit(":", 2)
    return TILE_METRES * float(np.hypot(int(row_a) - int(row_b), int(col_a) - int(col_b)))


def find_similar(aoi_id: str, positive: list[str], negative: list[str] | None = None, limit: int = 10) -> dict:
    """
    Return the tiles most similar to the positive examples.

    - positive / negative: lists of tile ids (e.g. "demo-01:12:30")
    Result: {"results": [...], "method": ..., "feature_names": [...]}
    """
    negative = negative or []
    connection = db.connect()
    tile_ids, matrix, names = observations.latest_feature_matrix(connection, aoi_id)
    tiles = {row["id"]: dict(row) for row in connection.execute("SELECT id, lon, lat FROM tiles WHERE aoi_id = ?", (aoi_id,))}
    connection.close()
    if not tile_ids:
        return {"results": [], "method": "no observations", "feature_names": []}

    index = {tile_id: i for i, tile_id in enumerate(tile_ids)}
    missing = [t for t in positive + negative if t not in index]
    usable_positive = [t for t in positive if t in index]
    if not usable_positive:
        return {"results": [], "method": "none of the example tiles have clear observations", "missing": missing, "feature_names": names}

    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0)
    std[std == 0] = 1.0
    standard = (matrix - mean) / std

    query = standard[[index[t] for t in usable_positive]].mean(axis=0)
    usable_negative = [t for t in negative if t in index]
    if usable_negative:
        query = query - 0.5 * standard[[index[t] for t in usable_negative]].mean(axis=0)

    norms = np.linalg.norm(standard, axis=1) * (np.linalg.norm(query) or 1.0)
    norms[norms == 0] = 1.0
    similarity = standard @ query / norms

    results, taken = [], list(usable_positive) + usable_negative
    for i in np.argsort(-similarity):
        tile_id = tile_ids[i]
        if any(_grid_distance_m(tile_id, other) < DEDUP_METRES for other in taken):
            continue
        # Explain: features where this tile is closest to the query (in z-units).
        closeness = -np.abs(standard[i] - query)
        top = np.argsort(-closeness)[:3]
        results.append({
            "tile_id": tile_id, "similarity": round(float(similarity[i]), 3),
            "lon": tiles[tile_id]["lon"], "lat": tiles[tile_id]["lat"],
            "why": [f"{names[j]} {'similar' if abs(standard[i][j] - query[j]) < 0.5 else 'close'}" for j in top],
        })
        taken.append(tile_id)
        if len(results) >= limit:
            break
    return {
        "results": results,
        "method": "cosine similarity on standardised spectral + change-history features (non-semantic)",
        "feature_names": names, "positive": usable_positive, "negative": usable_negative, "missing": missing,
        "similarity_kind": "uncalibrated cosine similarity",
    }
