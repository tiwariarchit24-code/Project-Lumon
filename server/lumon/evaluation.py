"""
Reproducible evaluation: measure what can be measured, and say clearly
what cannot be measured yet.

Run:  python -m lumon.cli evaluate
Results are written to data/evaluation/<UTC time>.json and latest.json.

METRICS
  query_latency_ms        measured: fixed list of queries, each run 5 times
  change_analysis_s       measured: wall time of one full change-engine run
  storage_bytes           measured: size of data folders and database
  hardware                measured: CPU, memory, OS of this machine
  precision_at_k          ONLY from analyst decisions (confirmed / reviewed
                          among the top-K by score); otherwise "not measurable"
  false_alarms_per_100km2 ONLY from analyst rejections of accepted events
  earliest_date_error     ONLY if config/evaluation/reference_dates.json
                          provides independently known change dates
  decision_time_s         ONLY from audit log pairs "open-evidence" -> decision
No number is ever estimated or filled in.
"""

import json
import os
import platform
import statistics
import time
from datetime import date, datetime, timezone

from . import db, settings
from .imagery import aoi as aoi_module

FIXED_QUERIES = [
    "Show recent earthquakes near Delhi",
    "Show airports in Maharashtra",
    "Show power plants near Chennai",
    "Show newly constructed areas near rivers",
    "Show water expansion since January",
    "Show aircraft near Mumbai",
]


def _folder_size(path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    return total


def hardware() -> dict:
    """Describe the machine (measured, not assumed)."""
    memory = None
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        pass
    return {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
            "cpu_count": os.cpu_count(), "memory_bytes": memory, "python": platform.python_version()}


def measure_query_latency(repeats: int = 5) -> dict:
    """Parse + run each fixed query several times; report median and max in ms."""
    from .query import engine, parser
    results = {}
    for text in FIXED_QUERIES:
        timings = []
        count = None
        for _ in range(repeats):
            start = time.perf_counter()
            output = engine.run(parser.parse(text))
            timings.append((time.perf_counter() - start) * 1000)
            count = output["count"]
        results[text] = {"median_ms": round(statistics.median(timings), 2), "max_ms": round(max(timings), 2), "results": count}
    return results


def measure_change_runtime(aoi_id: str) -> dict:
    """Time one change-engine run (observations already cached are reused)."""
    from .change import engine
    start = time.perf_counter()
    summary = engine.run(aoi_id, log=lambda _: None)
    return {"seconds": round(time.perf_counter() - start, 2), "summary": summary}


def label_metrics(connection, k_values=(5, 10, 20)) -> dict:
    """Precision@K and false alarms per 100 km2 from analyst decisions only."""
    reviewed = [dict(r) for r in connection.execute(
        "SELECT id, aoi_id, score, review_state FROM change_candidates WHERE status = 'accepted' AND review_state IN ('confirmed','rejected','relabelled') ORDER BY score DESC")]
    if not reviewed:
        return {"precision_at_k": "not measurable: no analyst decisions recorded yet",
                "false_alarms_per_100km2": "not measurable: no analyst decisions recorded yet",
                "reviewed": 0}
    precision = {}
    for k in k_values:
        top = reviewed[:k]
        if len(top) < k:
            precision[f"@{k}"] = f"not measurable: only {len(top)} reviewed"
            continue
        correct = sum(1 for r in top if r["review_state"] in ("confirmed", "relabelled"))
        precision[f"@{k}"] = round(correct / k, 3)
    false_alarms = {}
    for aoi in aoi_module.load_aois():
        min_lon, min_lat, max_lon, max_lat = aoi["bbox"]
        from .geo.geometry import haversine_m
        width_km = haversine_m(min_lon, min_lat, max_lon, min_lat) / 1000
        height_km = haversine_m(min_lon, min_lat, min_lon, max_lat) / 1000
        area = width_km * height_km
        rejected = sum(1 for r in reviewed if r["aoi_id"] == aoi["id"] and r["review_state"] == "rejected")
        reviewed_here = sum(1 for r in reviewed if r["aoi_id"] == aoi["id"])
        false_alarms[aoi["id"]] = {"rejected": rejected, "reviewed": reviewed_here, "aoi_km2": round(area, 2),
                                   "per_100km2": round(rejected / area * 100, 2) if area else None,
                                   "note": "counts only reviewed candidates; unreviewed ones are not assumed correct or wrong"}
    return {"precision_at_k": precision, "false_alarms_per_100km2": false_alarms, "reviewed": len(reviewed)}


def earliest_date_error(connection) -> dict | str:
    """Compare earliest_supported_after with independently known dates, if provided."""
    path = settings.CONFIG_DIR / "evaluation" / "reference_dates.json"
    if not path.exists():
        return "not measurable: no reference dates provided (config/evaluation/reference_dates.json)"
    references = json.loads(path.read_text())
    errors = []
    for item in references.get("changes", []):
        row = connection.execute("SELECT earliest_supported_after FROM change_candidates WHERE id = ?", (item["candidate_id"],)).fetchone()
        if row and row["earliest_supported_after"]:
            errors.append(abs((date.fromisoformat(row["earliest_supported_after"]) - date.fromisoformat(item["reference_date"])).days))
    if not errors:
        return "not measurable: none of the reference candidates exist"
    return {"n": len(errors), "median_days": statistics.median(errors), "max_days": max(errors)}


def decision_times(connection) -> dict | str:
    """Seconds from first 'open-evidence' to the decision, per candidate."""
    opened, durations = {}, []
    for row in connection.execute("SELECT at, action, target FROM audit_log ORDER BY id"):
        if row["action"] == "open-evidence" and row["target"] not in opened:
            opened[row["target"]] = row["at"]
        elif row["action"] in ("confirmed", "rejected", "relabelled") and row["target"] in opened:
            start = datetime.fromisoformat(opened.pop(row["target"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
            durations.append((end - start).total_seconds())
    if not durations:
        return "not measurable: no decisions made after opening evidence"
    return {"n": len(durations), "median_s": statistics.median(durations), "max_s": max(durations)}


def run_all() -> dict:
    """Run every measurement and save the report."""
    connection = db.connect()
    report = {
        "generated_at": db.now_iso(),
        "mode": settings.operating_mode(),
        "hardware": hardware(),
        "storage_bytes": {
            "database": settings.DATABASE_PATH.stat().st_size if settings.DATABASE_PATH.exists() else 0,
            "boundaries": _folder_size(settings.BOUNDARY_DIR), "reference": _folder_size(settings.REFERENCE_DIR),
            "snapshots": _folder_size(settings.SNAPSHOT_DIR), "imagery": _folder_size(settings.IMAGERY_DIR),
            "raw": _folder_size(settings.RAW_DIR),
        },
        "counts": {
            "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "entities": connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "scenes_usable": connection.execute("SELECT COUNT(*) FROM scenes WHERE quality_status IN ('usable','degraded')").fetchone()[0],
            "scenes_quarantined": connection.execute("SELECT COUNT(*) FROM scenes WHERE quality_status = 'quarantined'").fetchone()[0],
            "tile_observations": connection.execute("SELECT COUNT(*) FROM tile_observations").fetchone()[0],
            "change_candidates_accepted": connection.execute("SELECT COUNT(*) FROM change_candidates WHERE status = 'accepted'").fetchone()[0],
            "change_candidates_suppressed": connection.execute("SELECT COUNT(*) FROM change_candidates WHERE status = 'suppressed'").fetchone()[0],
        },
    }
    report["change_analysis"] = {aoi["id"]: measure_change_runtime(aoi["id"]) for aoi in aoi_module.load_aois()}
    report["query_latency_ms"] = measure_query_latency()
    report["labels"] = label_metrics(connection)
    report["earliest_date_error"] = earliest_date_error(connection)
    report["decision_time"] = decision_times(connection)
    connection.close()

    folder = settings.DATA_DIR / "evaluation"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    text = json.dumps(report, indent=2, default=str)
    (folder / f"{stamp}.json").write_text(text)
    (folder / "latest.json").write_text(text)
    return report
