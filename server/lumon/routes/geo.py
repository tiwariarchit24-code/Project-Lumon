"""
Geography routes: operating area, boundary files, gazetteer search and the
map layer registry.
"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import db, settings
from ..geo import boundary, places
from ..imagery import aoi as aoi_module
from ..query.engine import ENTITY_TYPE_SOURCES, EVENT_TYPE_SOURCES
from ..sources import registry

router = APIRouter(prefix="/api")


@router.get("/operating-area")
def operating_area():
    """Operating-area definition, whether it is staged, and its bbox."""
    config = boundary.operating_area_config()
    return {**config, "status": boundary.status(), "bbox": boundary.operating_bbox()}


@router.get("/boundaries")
def boundaries():
    """What boundary datasets are staged, with their provenance facts."""
    path = settings.BOUNDARY_DIR / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}


@router.get("/boundaries/{dataset_id}.geojson")
def boundary_file(dataset_id: str):
    """The staged GeoJSON of one boundary dataset (served from local disk)."""
    path = boundary.boundary_path(dataset_id)
    if "/" in dataset_id or not path.exists():
        raise HTTPException(404, "BOUNDARY NOT STAGED")
    return FileResponse(path, media_type="application/geo+json")


@router.get("/reference/{source_id}.geojson")
def reference_file(source_id: str):
    """A reference layer (rivers, railways...) built by a source refresh."""
    path = settings.REFERENCE_DIR / f"{source_id}.geojson"
    if "/" in source_id or not path.exists():
        raise HTTPException(404, "LAYER NOT STAGED")
    return FileResponse(path, media_type="application/geo+json")


@router.get("/places/search")
def search_places(q: str, limit: int = 8):
    """Gazetteer search over staged cities, states and districts."""
    return places.search(q, limit=limit)


def _layer_state(layer: dict, connection, sources: dict, boundary_manifest: dict) -> dict:
    """
    Work out whether one layer can be shown right now, where its data came
    from, how fresh it is and whether it works offline.
    """
    kind = layer["kind"]
    state = {"available": False, "source": None, "updated_at": None, "coverage": None, "offline_available": False,
             "count": None, "status_text": "NOT IMPLEMENTED", "freshness": None}
    if kind == "boundary":
        entry = boundary_manifest.get(layer["dataset"])
        staged = boundary.is_staged(layer["dataset"])
        state.update(available=staged, offline_available=staged, source=entry and entry["provider"],
                     updated_at=entry and entry["retrieved_at"], coverage="India",
                     count=entry and entry["feature_count"], status_text="STAGED" if staged else "BOUNDARY NOT STAGED")
    elif kind == "reference":
        source = sources.get(layer["source_id"], {})
        staged = (settings.REFERENCE_DIR / f"{layer['source_id']}.geojson").exists()
        state.update(available=staged, offline_available=staged, source=source.get("provider"),
                     updated_at=source.get("last_success"), coverage=source.get("coverage"),
                     count=source.get("record_count"), freshness=source.get("freshness_status"),
                     status_text=source.get("freshness_label", "STAGED") if staged else "DATA NOT STAGED")
    elif kind in ("events", "entities"):
        table = "events" if kind == "events" else "entities"
        types = layer["types"]
        row = connection.execute(
            f"SELECT COUNT(*) AS n, GROUP_CONCAT(DISTINCT source_id) AS sources, MAX({'updated_at'}) AS updated FROM {table} WHERE type IN ({','.join('?' * len(types))})",
            types).fetchone()
        source_ids = (row["sources"] or "").split(",") if row["sources"] else []
        # Which sources COULD provide these types (whether or not they have data)?
        providers = {s for t in types for s in (EVENT_TYPE_SOURCES.get(t, []) + ENTITY_TYPE_SOURCES.get(t, []))}
        if row["n"]:
            # Data present: report the freshest status among the sources that
            # actually supplied records, with that source's age label.
            supplying = [sources[s] for s in source_ids if s in sources]
            best = min(supplying, key=lambda s: registry.STATUS_ORDER.index(s["freshness_status"])) if supplying else None
            freshness = best["freshness_status"] if best else None
            status_text = best["freshness_label"] if best else "STAGED"
        else:
            # No records: say WHY - the sources ran and found nothing in India,
            # need a key, failed, were never run, or are not implemented.
            statuses = [sources[s]["freshness_status"] for s in providers if s in sources]
            if any(x in ("LIVE", "SNAPSHOT", "STALE") for x in statuses):
                freshness, status_text = "EMPTY", "NO RECORDS IN SNAPSHOT"
                if "KEY REQUIRED" in statuses:
                    status_text += " · A SOURCE NEEDS A KEY"
            elif statuses:
                freshness = min(statuses, key=registry.STATUS_ORDER.index)
                status_text = freshness
            else:
                freshness, status_text = "NOT IMPLEMENTED", "NOT IMPLEMENTED"
        state.update(available=row["n"] > 0, offline_available=row["n"] > 0,
                     source=", ".join(sources[s]["provider"] for s in source_ids if s in sources) or None,
                     updated_at=row["updated"], count=row["n"], freshness=freshness,
                     coverage="; ".join(sources[s]["coverage"] for s in source_ids if s in sources) or None,
                     status_text=status_text)
    elif kind in ("imagery", "analysis"):
        if layer["id"] == "discovery-cluster":
            from ..imagery import discovery
            version = discovery.get_version()
            n = version["n_clusters"] if version else 0
            state.update(available=n > 0, offline_available=n > 0, source="RemoteCLIP embeddings, spherical k-means (embedding-based grouping)",
                         updated_at=version["created_at"] if version else None, count=n,
                         coverage=", ".join(a["name"] for a in aoi_module.load_aois()) + " (AOI only)",
                         status_text=f"{n} CLUSTERS · CHOOSE ONE" if n else "NO CLUSTERS BUILT")
            return state
        if layer["id"] == "ml-change-candidates":
            from ..change_ml import learned
            connection.executescript(learned.SCHEMA)
            n = connection.execute("SELECT COUNT(*) FROM ml_change_regions WHERE review_status != 'rejected'").fetchone()[0]
            latest = connection.execute("SELECT MAX(created_at) FROM ml_change_runs").fetchone()[0]
            state.update(available=n > 0, offline_available=n > 0, source="BTC-B (OSCD checkpoint) on Sentinel-2 L2A pairs — model output",
                         updated_at=latest, count=n, coverage=", ".join(a["name"] for a in aoi_module.load_aois()) + " (AOI only)",
                         status_text="MODEL CANDIDATES · NOT VERIFIED" if n else "NO MODEL RUN YET")
            return state
        if layer["id"] in ("aoi-footprints", "sentinel-2"):
            n = connection.execute("SELECT COUNT(*) FROM scenes WHERE quality_status IN ('usable','degraded')").fetchone()[0]
            latest = connection.execute("SELECT MAX(acquired_at) FROM scenes WHERE quality_status IN ('usable','degraded')").fetchone()[0]
        else:
            status = "accepted" if layer["id"] == "change-events" else "suppressed"
            n = connection.execute("SELECT COUNT(*) FROM change_candidates WHERE status = ?", (status,)).fetchone()[0]
            latest = connection.execute("SELECT MAX(created_at) FROM change_candidates").fetchone()[0]
        state.update(available=n > 0, offline_available=n > 0, source="Sentinel-2 L2A (Earth Search) + Lumon change engine",
                     updated_at=latest, count=n, coverage=", ".join(a["name"] for a in aoi_module.load_aois()) + " (AOI only)",
                     status_text="STAGED" if n else "NO IMAGERY STAGED")
    elif kind == "pilot":
        # Pilot study area: always shown with its honest geometry status
        # (PROVISIONAL / STAGED · UNVERIFIED / VERIFIED / UNAVAILABLE).
        from .. import pilot
        area = pilot.study_area()
        available = area["geometry"] is not None
        state.update(available=available, offline_available=available, source=area["source"], coverage="NIT Raipur pilot",
                     count=1 if available else 0, freshness=None,
                     status_text=f"{area['status']} · {'campus boundary' if area['campus_boundary'] else 'search area, not campus boundary'}")
    elif kind == "planned":
        state["status_text"] = "AVAILABLE LATER"
    return state


@router.get("/layers")
def layers():
    """
    The layer registry (config/layers.json) with live availability:
    id, name, group, description, enabled, available, source, updated_at,
    coverage, offline_available (+ count and a status text for the UI).
    """
    config = json.loads((settings.CONFIG_DIR / "layers.json").read_text())
    connection = db.connect()
    sources = {s["id"]: s for s in registry.describe_all(connection)}
    manifest_path = settings.BOUNDARY_DIR / "manifest.json"
    boundary_manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    result = []
    for layer in config["layers"]:
        state = _layer_state(layer, connection, sources, boundary_manifest)
        result.append({**layer, "enabled": bool(layer.get("enabled")) and state["available"], **state})
    connection.close()
    return {"groups": config["groups"], "layers": result}
