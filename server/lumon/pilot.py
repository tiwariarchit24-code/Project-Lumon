"""
The NIT Raipur pilot: study area, pilot data registry, AI/ML capability
registry and candidate model registry.

India remains Lumon's broad OSINT/GEOINT coverage area. NIT Raipur is the
detailed demonstrator. Everything here is CONFIGURATION plus honest status:
no institutional data is staged and no NIT ground truth exists in the
repository. Two models can be staged (RemoteCLIP for semantic search and
image similarity, BTC-B for learned change detection, both on the demo AOI);
their status is read from the files present, never assumed.

Files (all in config/):
  pilots/nit-raipur.json  study area, boundary rules, pilot data registry
  ai/capabilities.json    the AI/ML capabilities and their status
  ai/models.json          candidate models (all NOT STAGED)

STUDY-AREA GEOMETRY (no coordinates are typed in anywhere):
  1. If a verified campus boundary file exists (boundary.verified_file),
     it is used. It counts as VERIFIED only when its metadata file says
     verified: true; otherwise it is STAGED · UNVERIFIED.
  2. Otherwise a PROVISIONAL study area is derived at runtime from a staged
     gazetteer place (Natural Earth "Raipur" city point) with a configured
     buffer. It is labelled PROVISIONAL and is NOT the campus boundary.
"""

import json
import math

from . import settings
from .geo import places

DATA_STATES = ["AVAILABLE", "PARTIAL", "EXPECTED", "NOT AVAILABLE", "NOT STAGED"]
MODEL_STATES = ["DEPLOYED", "STAGED", "NOT STAGED"]


def _read(relative_path: str) -> dict:
    return json.loads((settings.CONFIG_DIR / relative_path).read_text())


def load_pilot() -> dict:
    """The NIT Raipur pilot definition (config/pilots/nit-raipur.json)."""
    return _read("pilots/nit-raipur.json")


# Runtime modules of staged models: each provides model_problems(), status()
# and MODEL_ID. The registry reads a capability's real state from them.
RUNTIME_MODULES = {
    "lumon.imagery.semantic": "lumon.imagery.semantic",      # RemoteCLIP: semantic search + image similarity
    "lumon.change_ml.learned": "lumon.change_ml.learned",    # BTC-B: learned change detection
    "lumon.imagery.discovery": "lumon.imagery.discovery",    # clustering of RemoteCLIP embeddings (discovery)
}


def _runtime(name: str):
    import importlib
    return importlib.import_module(RUNTIME_MODULES[name])


def load_capabilities() -> dict:
    """
    The AI/ML capability registry (config/ai/capabilities.json). A
    capability with a "runtime" block gets its status from what is actually
    on this machine: STAGED when its model files are present (not validated
    for the pilot, so never READY), else NOT STAGED; plus a retrieval_status
    from the runtime (NOT STAGED / INDEXING / READY / PARTIAL / UNAVAILABLE).
    Runtimes: lumon.imagery.semantic (semantic-search, image-similarity) and
    lumon.change_ml.learned (change-detection).
    """
    registry = _read("ai/capabilities.json")
    for item in registry["capabilities"]:
        if item.get("runtime") in RUNTIME_MODULES:
            runtime = _runtime(item["runtime"])
            state = runtime.status()
            item["status"] = "NOT STAGED" if runtime.model_problems() else "STAGED"
            item["retrieval_status"] = state["state"]
            item["retrieval"] = {k: state[k] for k in ("reasons", "scenes_indexed", "scenes_eligible", "chips_indexed",
                                                       "chips_excluded", "model_name", "architecture", "runs", "regions",
                                                       "embeddings", "pending", "method")
                                 if k in state}
    return registry


def load_models() -> list[dict]:
    """
    The candidate model registry (config/ai/models.json). A model with a
    "weights" block is STAGED only when its files and packages are present.
    """
    models = _read("ai/models.json")["models"]
    by_model = {_runtime(name).MODEL_ID: _runtime(name) for name in RUNTIME_MODULES}
    for model in models:
        if model.get("weights") and model["id"] in by_model:
            model["deployment_status"] = "NOT STAGED" if by_model[model["id"]].model_problems() else "STAGED"
    return models


def capability(capability_id: str) -> dict | None:
    """One capability by id, or None."""
    for item in load_capabilities()["capabilities"]:
        if item["id"] == capability_id:
            return item
    return None


# ---------------------------------------------------------------------------
# Validation: catch wrong states before the UI shows them.
# ---------------------------------------------------------------------------

def validate_registries() -> list[str]:
    """
    Return a list of problems (empty when everything is consistent):
    unknown data/capability/model states, or a capability claiming READY or
    STAGED while its model is not staged.
    """
    problems = []
    for entry in load_pilot()["data_registry"]:
        if entry["state"] not in DATA_STATES:
            problems.append(f"data '{entry['name']}': unknown state {entry['state']}")
    capabilities = load_capabilities()
    models = {m["id"]: m for m in load_models()}
    for model in models.values():
        if model["deployment_status"] not in MODEL_STATES:
            problems.append(f"model '{model['id']}': unknown status {model['deployment_status']}")
    for item in capabilities["capabilities"]:
        if item["status"] not in capabilities["statuses"]:
            problems.append(f"capability '{item['id']}': unknown status {item['status']}")
        model = models.get(item.get("model") or "")
        if item["status"] in ("READY", "STAGED") and (model is None or model["deployment_status"] == "NOT STAGED"):
            problems.append(f"capability '{item['id']}' is {item['status']} but its model is not staged")
    return problems


# ---------------------------------------------------------------------------
# Study-area geometry
# ---------------------------------------------------------------------------

def _square_around(lon: float, lat: float, half_size_km: float) -> dict:
    """A lon/lat square of +/- half_size_km around a point (approximate km to degrees)."""
    d_lat = half_size_km / 111.32
    d_lon = half_size_km / (111.32 * math.cos(math.radians(lat)))
    ring = [[lon - d_lon, lat - d_lat], [lon + d_lon, lat - d_lat], [lon + d_lon, lat + d_lat],
            [lon - d_lon, lat + d_lat], [lon - d_lon, lat - d_lat]]
    return {"type": "Polygon", "coordinates": [[[round(x, 6), round(y, 6)] for x, y in ring]]}


def study_area() -> dict:
    """
    Resolve the pilot's study-area geometry and its honest status.

    Returns {"status", "geometry", "source", "meaning", "campus_boundary"}:
      status: VERIFIED | STAGED · UNVERIFIED | PROVISIONAL | UNAVAILABLE
      campus_boundary: whether a campus polygon (not just a search area) exists
    """
    pilot = load_pilot()
    boundary = pilot["boundary"]
    verified_path = settings.ROOT_DIR / boundary["verified_file"]
    if verified_path.exists():
        collection = json.loads(verified_path.read_text())
        meta_path = settings.ROOT_DIR / boundary["verified_metadata_file"]
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        verified = meta.get("verified") is True
        geometry = collection["features"][0]["geometry"] if collection.get("features") else collection
        return {"status": "VERIFIED" if verified else "STAGED · UNVERIFIED", "geometry": geometry,
                "source": meta.get("source", boundary["verified_file"]), "meaning": "Campus boundary file supplied to Lumon.",
                "campus_boundary": True}

    rule = boundary["provisional"]
    matches = [p for p in places.search(rule["gazetteer_place"], limit=10)
               if p["name"] == rule["gazetteer_place"] and p["kind"] == rule["gazetteer_kind"]]
    if not matches:
        return {"status": "UNAVAILABLE", "geometry": None, "source": rule["source"],
                "meaning": "The gazetteer place used for the provisional area is not staged (run stage-boundaries).",
                "campus_boundary": False}
    place = matches[0]
    return {"status": "PROVISIONAL", "geometry": _square_around(place["lon"], place["lat"], rule["half_size_km"]),
            "source": f"{rule['source']}, ±{rule['half_size_km']} km square", "meaning": rule["meaning"],
            "campus_boundary": False}


def summary() -> dict:
    """Everything the NIT Raipur pilot panel shows, with counts by state."""
    pilot = load_pilot()
    capabilities = load_capabilities()["capabilities"]
    area = study_area()
    data_counts = {state: sum(1 for e in pilot["data_registry"] if e["state"] == state) for state in DATA_STATES}
    ai_counts = {state: sum(1 for c in capabilities if c["status"] == state) for state in load_capabilities()["statuses"]}
    return {
        "id": pilot["id"], "canonical_name": pilot["canonical_name"], "short_name": pilot["short_name"],
        "description": pilot["description"], "status": pilot["status"], "study_areas": pilot["study_areas"],
        "study_area": {k: v for k, v in area.items() if k != "geometry"},
        "data_registry": pilot["data_registry"], "data_counts": data_counts,
        "capabilities": capabilities, "ai_counts": ai_counts, "models": load_models(),
        "registry_problems": validate_registries(),
    }


def aoi_feature_collection() -> dict:
    """The study area as GeoJSON for the map, with its status as properties."""
    pilot = load_pilot()
    area = study_area()
    features = []
    if area["geometry"]:
        label = f"{pilot['short_name'].upper()} · {area['status']}"
        features.append({"type": "Feature", "geometry": area["geometry"],
                         "properties": {"id": pilot["id"], "label": label, "status": area["status"], "source": area["source"]}})
    return {"type": "FeatureCollection", "features": features, "status": area["status"]}
