"""
Learned change detection routes (BTC-B, OSCD checkpoint).

Everything here is MODEL-GENERATED CANDIDATE CHANGE, kept apart from the
rule-based change engine (/api/changes). When the model is not staged the
routes say so (409 NOT STAGED / 503 UNAVAILABLE); they never fall back to
the rule-based engine.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .. import worker
from ..change_ml import learned

router = APIRouter(prefix="/api/ml-change")


class RunRequest(BaseModel):
    before_scene_id: str
    after_scene_id: str


class ReviewRequest(BaseModel):
    decision: str          # rejected | plausible | unreviewed
    analyst: str
    note: str | None = None


@router.get("/status")
def status():
    """NOT STAGED / INDEXING / READY / UNAVAILABLE, model version, run and review counts."""
    return learned.status()


@router.get("/pairs/suggested")
def suggested_pairs(aoi_id: str | None = None, limit: int = 30):
    """Same-season, cross-year, clear, well-registered pairs (longest span first)."""
    return learned.suggest_pairs(aoi_id, max(1, min(limit, 500)))


@router.get("/pairs/check")
def check_pair(before: str, after: str):
    """Can these two scenes be compared? Problems refuse the pair; warnings are shown with results."""
    result = learned.check_pair(before, after)
    result.pop("_data", None)
    return result


@router.post("/runs")
def start_run(request: RunRequest):
    """Queue inference for a pair (checked first; refused pairs are not queued)."""
    problems = learned.model_problems()
    if problems:
        raise HTTPException(409, {"state": "NOT STAGED", "reasons": problems})
    check = learned.check_pair(request.before_scene_id, request.after_scene_id)
    if not check["ok"]:
        raise HTTPException(422, {"state": "PAIR REFUSED", "reasons": check["problems"]})
    job = worker.enqueue("ml-change-run", {"before": request.before_scene_id, "after": request.after_scene_id, "actor": "analyst"})
    return {"job_id": job, "warnings": check["warnings"]}


@router.get("/runs")
def runs(aoi_id: str | None = None):
    return learned.list_runs(aoi_id)


@router.get("/runs/{run_id:path}/score.png")
def run_score_png(run_id: str, bbox: str | None = None, upscale: int = 1):
    """Score map (optionally cut to a lon/lat bbox, like /api/scenes/<id>/quicklook.png)."""
    box = [float(v) for v in bbox.split(",")] if bbox else None
    png = learned.probability_png(run_id, box, max(1, min(upscale, 6)))
    if png is None:
        raise HTTPException(404, "run output not found")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@router.get("/runs/{run_id:path}/{name}.tif")
def run_geotiff(run_id: str, name: str):
    """The georeferenced outputs: probability.tif (scores) or candidates.tif (mask)."""
    run = learned.get_run(run_id)
    if run is None or name not in ("probability", "candidates"):
        raise HTTPException(404, "run output not found")
    path = learned._path(run["probability_path"] if name == "probability" else run["mask_path"])
    if path is None or not path.exists():
        raise HTTPException(404, "run output file missing")
    return FileResponse(path, media_type="image/tiff", filename=f"{name}.tif")


@router.get("/runs/{run_id:path}")
def run_detail(run_id: str):
    run = learned.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@router.get("/regions")
def regions(run_id: str | None = None, include_rejected: bool = True, all_runs: bool = False):
    """Candidate regions as GeoJSON for one run (default: latest), every feature labelled MODEL-GENERATED CANDIDATE CHANGE."""
    return learned.regions_geojson(run_id, include_rejected, all_runs)


@router.get("/regions/{region_id:path}")
def region_detail(region_id: str):
    region = learned.get_region(region_id)
    if region is None:
        raise HTTPException(404, "region not found")
    return region


@router.post("/review/{region_id:path}")
def review(region_id: str, request: ReviewRequest):
    """Analyst decision on a candidate region (audited)."""
    try:
        return learned.review_region(region_id, request.decision, request.analyst, request.note)
    except KeyError:
        raise HTTPException(404, "region not found")
    except ValueError as error:
        raise HTTPException(400, str(error))
