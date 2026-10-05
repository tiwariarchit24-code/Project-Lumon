"""
System routes: health, operating mode, storage, jobs, audit, manifest.
"""

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import audit, db, settings, worker
from ..geo import boundary
from ..sources import registry

router = APIRouter(prefix="/api")

# Models the full design expects, with their real status on this machine.
# Only the image/text embedding model (RemoteCLIP, semantic image search) can
# be staged in this build; its status is read from the files present.
def _learned_change_model() -> dict:
    from ..change_ml import learned
    state = learned.status()
    return {"name": f"{state['model_name']} — model-generated candidate changes",
            "status": "NOT STAGED" if learned.model_problems() else "STAGED", "retrieval_status": state["state"],
            "fallback": "none: the rule-based change engine is a separate, labelled baseline and is never run in its place"}


def models() -> list[dict]:
    from ..imagery import semantic
    retrieval = semantic.status()
    staged = not semantic.model_problems()
    return [
        {"name": f"{retrieval['model_name']} — semantic image search", "status": "STAGED" if staged else "NOT STAGED",
         "retrieval_status": retrieval["state"],
         "fallback": "none: text-to-image search is refused when the model is unavailable "
                     "(image-to-image 'similar tiles' still uses non-semantic spectral features)"},
        _learned_change_model(),
        {"name": "Local language model (query planning)", "status": "NOT STAGED",
         "fallback": "deterministic parser (always on)"},
        {"name": "Learned land-cover classifier", "status": "NOT STAGED",
         "fallback": "documented spectral-index thresholds (uncalibrated)"},
    ]


def _folder_size(path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@router.get("/health")
def health():
    """Quick liveness check used by the UI status bar."""
    connection = db.connect()
    connection.execute("SELECT 1")
    connection.close()
    return {"status": "ok", "mode": settings.operating_mode(), "time": db.now_iso()}


@router.get("/system")
def system():
    """Everything the SYSTEM STATUS panel shows. All numbers are counted, not estimated."""
    connection = db.connect()
    count = lambda sql: connection.execute(sql).fetchone()[0]
    sources = registry.describe_all(connection)
    health_counts, status_counts = {}, {}
    for source in sources:
        health_counts[source["health"]] = health_counts.get(source["health"], 0) + 1
        status_counts[source["freshness_status"]] = status_counts.get(source["freshness_status"], 0) + 1
    # Age of the most recent successful download of any source (hours).
    ages = [s["freshness_age_hours"] for s in sources if s["freshness_age_hours"] is not None]
    last_scene = connection.execute("SELECT MAX(acquired_at) FROM scenes WHERE quality_status IN ('usable','degraded')").fetchone()[0]
    last_ingest = connection.execute("SELECT MAX(last_success) FROM sources").fetchone()[0]
    result = {
        "mode": settings.operating_mode(),
        "boundary": boundary.status(),
        "counts": {
            "events": count("SELECT COUNT(*) FROM events"),
            "entities": count("SELECT COUNT(*) FROM entities"),
            "scenes_usable": count("SELECT COUNT(*) FROM scenes WHERE quality_status IN ('usable','degraded')"),
            "scenes_quarantined": count("SELECT COUNT(*) FROM scenes WHERE quality_status = 'quarantined'"),
            "changes_accepted": count("SELECT COUNT(*) FROM change_candidates WHERE status = 'accepted'"),
            "changes_suppressed": count("SELECT COUNT(*) FROM change_candidates WHERE status = 'suppressed'"),
            "decisions": count("SELECT COUNT(*) FROM decisions"),
            "audit_entries": count("SELECT COUNT(*) FROM audit_log"),
            "jobs_queued": count("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"),
        },
        "sources": {"total": len(sources), "by_health": health_counts, "by_status": status_counts,
                    "newest_download_age_hours": min(ages) if ages else None},
        "last_source_success": last_ingest,
        "latest_scene": last_scene,
        "storage_bytes": {
            "database": settings.DATABASE_PATH.stat().st_size if settings.DATABASE_PATH.exists() else 0,
            "boundaries": _folder_size(settings.BOUNDARY_DIR),
            "reference": _folder_size(settings.REFERENCE_DIR),
            "snapshots": _folder_size(settings.SNAPSHOT_DIR),
            "imagery": _folder_size(settings.IMAGERY_DIR),
            "raw": _folder_size(settings.RAW_DIR),
        },
        "models": models(),
        "audit": audit.verify_chain(connection),
    }
    connection.close()
    return result


class JobRequest(BaseModel):
    kind: str
    params: dict = {}


@router.post("/jobs")
def create_job(request: JobRequest):
    """Queue a background job (source refresh, imagery ingest, change analysis, evaluation)."""
    allowed = {"source-refresh", "refresh-all", "imagery-ingest", "change-analysis", "evaluation"}
    if request.kind not in allowed:
        raise HTTPException(400, f"kind must be one of {sorted(allowed)}")
    return {"job_id": worker.enqueue(request.kind, request.params)}


@router.get("/jobs")
def list_jobs(limit: int = 20):
    """Most recent jobs, newest first."""
    connection = db.connect()
    rows = [db.row_to_dict(r, ["params", "result"]) for r in connection.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]
    connection.close()
    return rows


@router.get("/audit")
def audit_log(limit: int = 100):
    """Most recent audit entries, newest first."""
    connection = db.connect()
    rows = [db.row_to_dict(r, ["details"]) for r in connection.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]
    connection.close()
    return rows


@router.get("/audit/verify")
def audit_verify():
    """Re-check the hash chain of the audit log."""
    connection = db.connect()
    result = audit.verify_chain(connection)
    connection.close()
    return result


@router.get("/manifest")
def manifest():
    """
    The source manifest: every external dataset, API, imagery collection,
    font and model, with licence and retrieval facts. (Reproducibility.)
    """
    boundary_manifest_path = settings.BOUNDARY_DIR / "manifest.json"
    connection = db.connect()
    scenes = connection.execute(
        "SELECT COUNT(*) AS n, MIN(acquired_at) AS first, MAX(acquired_at) AS last FROM scenes WHERE quality_status IN ('usable','degraded')").fetchone()
    result = {
        "boundaries": json.loads(boundary_manifest_path.read_text()) if boundary_manifest_path.exists() else {},
        "sources": [{k: s[k] for k in ("id", "name", "provider", "endpoint", "license", "attribution", "last_success",
                                         "last_snapshot_sha256", "offline_cache_supported", "health")} for s in registry.describe_all(connection)],
        "imagery": {"collection": "sentinel-2-l2a", "provider": "Earth Search by Element 84 / AWS Open Data",
                    "license": "Copernicus Sentinel data terms (free, full and open)", "scenes": scenes["n"],
                    "first": scenes["first"], "last": scenes["last"]},
        "fonts": [{"name": "Noto Sans Regular (PBF glyphs)", "license": "SIL OFL 1.1", "source": "protomaps basemaps-assets", "retrieved": "2026-10-04"}],
        "models": models(),
    }
    connection.close()
    return result
