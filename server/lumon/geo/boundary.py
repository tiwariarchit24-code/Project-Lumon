"""
The India operating area: "is this point inside the area we care about?"

The operating area is defined in config/geo/operating-area.json as a list of
staged boundary datasets (by default: India land + India EEZ). Nothing here
hard-codes coordinates; if the boundary files are not staged yet, the
functions say so instead of guessing.
"""

import json
from functools import lru_cache

from .. import settings
from . import geometry


def operating_area_config() -> dict:
    """Read config/geo/operating-area.json."""
    return json.loads((settings.CONFIG_DIR / "geo" / "operating-area.json").read_text())


def boundary_path(dataset_id: str):
    """Where the processed GeoJSON for a dataset lives (it may not exist yet)."""
    return settings.BOUNDARY_DIR / f"{dataset_id}.geojson"


def is_staged(dataset_id: str) -> bool:
    """True if this boundary dataset has been staged (downloaded and processed)."""
    return boundary_path(dataset_id).exists()


def load_boundary(dataset_id: str) -> dict | None:
    """Return the staged FeatureCollection for a dataset, or None if not staged."""
    path = boundary_path(dataset_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def _operating_area_parts() -> tuple:
    """
    Load the polygons that make up the operating area, once.

    Returns a tuple of (bbox, geometry) pairs. The bbox is checked first
    because it is much cheaper than the full point-in-polygon test.
    Returns an empty tuple if none of the components are staged.
    """
    parts = []
    for dataset_id in operating_area_config()["components"]:
        collection = load_boundary(dataset_id)
        if not collection:
            continue
        for feature in collection["features"]:
            shape = feature["geometry"]
            parts.append((geometry.bbox(shape), shape))
    return tuple(parts)


def reload() -> None:
    """Forget the cached boundary (call after re-staging boundaries)."""
    _operating_area_parts.cache_clear()


def status() -> dict:
    """
    Describe whether the operating area is usable.
    `missing` lists component datasets that still need staging.
    """
    components = operating_area_config()["components"]
    missing = [dataset_id for dataset_id in components if not is_staged(dataset_id)]
    return {"staged": len(missing) == 0, "components": components, "missing": missing}


def operating_bbox() -> list[float] | None:
    """Bounding box around the whole operating area, or None when not staged."""
    parts = _operating_area_parts()
    if not parts:
        return None
    boxes = [box for box, _ in parts]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def contains_point(lon: float, lat: float) -> bool | None:
    """
    True/False: is the point inside the India operating area?
    None: we cannot tell, because the boundary is not staged.

    Callers must handle None explicitly (for example by keeping the record
    but marking it "unscoped") rather than treating it as True or False.
    """
    parts = _operating_area_parts()
    if not parts:
        return None
    for box, shape in parts:
        if geometry.bbox_contains(box, lon, lat) and geometry.point_in_geometry(lon, lat, shape):
            return True
    return False


def contains_geometry(shape: dict) -> bool | None:
    """
    Is any part of a geometry inside the operating area? We test the
    representative point and, for lines/polygons, every vertex - cheap and
    good enough for scoping event footprints.
    """
    if shape["type"] == "Point":
        return contains_point(*shape["coordinates"][:2])
    result = contains_point(*geometry.representative_point(shape))
    if result:
        return True
    for position in geometry.iter_positions(shape):
        inside = contains_point(position[0], position[1])
        if inside is None:
            return None
        if inside:
            return True
    return False
