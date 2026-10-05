"""
Gazetteer: find places in India by name.

Built entirely from staged datasets (no online geocoder):
  - cities  from india-places    (Natural Earth populated places)
  - states  from india-states    (geoBoundaries ADM1)
  - districts from india-districts (geoBoundaries ADM2)

Each entry has a name, a kind, a point and (for areas) a bounding box, so
the map can fly to it and the query engine can use it as a search area.
"""

import re
import unicodedata
from functools import lru_cache

from . import boundary, geometry


def _normalise(text: str) -> str:
    """
    Make names comparable: remove accents (the boundary data writes
    'Chhattīsgarh', analysts type 'Chhattisgarh'), lower-case, and turn
    punctuation into spaces so 'Navi-Mumbai' matches 'navi mumbai'.
    """
    without_accents = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", without_accents.lower())).strip()


@lru_cache(maxsize=1)
def load_gazetteer() -> tuple:
    """
    Build the list of searchable places once. Returns a tuple of dicts:
    {name, kind, lon, lat, bbox, state, key}. Empty if nothing is staged.
    """
    entries = []
    places = boundary.load_boundary("india-places")
    if places:
        for feature in places["features"]:
            lon, lat = feature["geometry"]["coordinates"][:2]
            name = feature["properties"]["name"]
            entries.append({"name": name, "kind": "city", "lon": lon, "lat": lat, "bbox": None,
                            "state": feature["properties"].get("state"),
                            "population": feature["properties"].get("population"), "key": _normalise(name)})
    for dataset_id, kind in (("india-states", "state"), ("india-districts", "district")):
        collection = boundary.load_boundary(dataset_id)
        if not collection:
            continue
        for feature in collection["features"]:
            name = feature["properties"]["name"]
            lon, lat = geometry.largest_polygon_label_point(feature["geometry"])
            entries.append({"name": name, "kind": kind, "lon": lon, "lat": lat,
                            "bbox": geometry.bbox(feature["geometry"]), "state": None,
                            "population": None, "key": _normalise(name)})
    return tuple(entries)


def reload() -> None:
    """Forget the cached gazetteer (after re-staging boundaries)."""
    load_gazetteer.cache_clear()


# Cities first, then states, then districts when names tie
# (e.g. "Mumbai" is both a city and a district).
KIND_ORDER = {"city": 0, "state": 1, "district": 2}


def search(text: str, limit: int = 10) -> list[dict]:
    """
    Return places whose name matches `text`: exact matches first, then
    names starting with the text, then names containing it.
    """
    query = _normalise(text)
    if not query:
        return []
    scored = []
    for entry in load_gazetteer():
        if entry["key"] == query:
            rank = 0
        elif entry["key"].startswith(query):
            rank = 1
        elif query in entry["key"]:
            rank = 2
        else:
            continue
        scored.append((rank, KIND_ORDER[entry["kind"]], -(entry["population"] or 0), entry["name"], entry))
    scored.sort(key=lambda item: item[:4])
    return [{k: v for k, v in item[4].items() if k != "key"} for item in scored[:limit]]


def normalise(text: str) -> str:
    """Public version of the name normaliser, used by the query parser."""
    return _normalise(text)


def find_in_text(text: str) -> tuple[dict | None, str | None]:
    """
    Find the longest place name mentioned in a sentence, e.g. "quakes near
    Delhi" -> Delhi. Matches whole words only. Returns (place, matched text)
    or (None, None).
    """
    sentence = f" {_normalise(text)} "
    best = None
    for entry in load_gazetteer():
        if len(entry["key"]) < 3:
            continue
        if f" {entry['key']} " in sentence:
            candidate = (len(entry["key"]), -KIND_ORDER[entry["kind"]], entry["population"] or 0, entry)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
    if best is None:
        return None, None
    entry = best[3]
    # Return the accent-free form of the name, which is how it appears in
    # the (normalised) sentence.
    return {k: v for k, v in entry.items() if k != "key"}, entry["key"]
