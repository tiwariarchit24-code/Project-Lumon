"""
Review routes: queue, evidence opening, decisions, more-like-these, export
and evaluation results.
"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import audit, db, export, review, settings

router = APIRouter(prefix="/api")


@router.get("/review/queue")
def queue(include_suppressed: bool = False, aoi_id: str | None = None):
    """Ranked review queue."""
    return review.queue(aoi_id=aoi_id, include_suppressed=include_suppressed)


@router.get("/decisions")
def decisions(limit: int = 200):
    """Every analyst decision, newest first, with the candidate's class."""
    connection = db.connect()
    rows = [dict(r) for r in connection.execute(
        """SELECT d.*, c.change_class, c.aoi_id FROM decisions d
           LEFT JOIN change_candidates c ON c.id = d.target_id ORDER BY d.id DESC LIMIT ?""", (limit,))]
    connection.close()
    return rows


@router.post("/review/{candidate_id}/open")
def open_evidence(candidate_id: str):
    """Log that the analyst opened the evidence (used for decision-time measurement)."""
    connection = db.connect()
    audit.record(connection, "analyst", "open-evidence", candidate_id)
    connection.close()
    return {"ok": True}


class DecisionRequest(BaseModel):
    decision: str
    analyst: str
    reason: str | None = None
    new_label: str | None = None


@router.post("/review/{candidate_id}/decision")
def decide(candidate_id: str, request: DecisionRequest):
    """Confirm, reject or relabel a candidate (logged and audited)."""
    try:
        return review.decide(candidate_id, request.decision, request.analyst, request.reason, request.new_label)
    except ValueError as error:
        raise HTTPException(400, str(error))


class MoreLikeRequest(BaseModel):
    candidate_ids: list[str]
    limit: int = 10


@router.post("/review/more-like-these")
def more_like_these(request: MoreLikeRequest):
    """Similar sites to the given candidates, away from rejected ones."""
    return review.more_like_these(request.candidate_ids, request.limit)


@router.post("/export/geopackage")
def export_geopackage():
    """Write a GeoPackage of events, observations, provenance and decisions."""
    result = export.export_geopackage()
    result["download"] = "/api/exports/" + result["path"].split("/")[-1]
    return result


@router.get("/exports/{name}")
def download_export(name: str):
    """Download a previously written export file."""
    path = settings.EXPORT_DIR / name
    if "/" in name or not path.exists():
        raise HTTPException(404, "export not found")
    return FileResponse(path, media_type="application/geopackage+sqlite3", filename=name)


@router.get("/evaluation/latest")
def evaluation_latest():
    """The most recent evaluation report, or a clear 'not run' message."""
    path = settings.DATA_DIR / "evaluation" / "latest.json"
    if not path.exists():
        return {"status": "not run", "how": "python -m lumon.cli evaluate"}
    return json.loads(path.read_text())
