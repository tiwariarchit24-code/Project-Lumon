"""
A simple database-backed job worker.

Jobs are rows in the `jobs` table (kind, params, status). The API adds
jobs; the worker takes the oldest queued job, runs it and stores the
result or error. That is the whole job system - no Redis, Celery or queue
server. The worker can run:
  - inside the API process as a background thread (default, simplest), or
  - on its own:  python -m lumon.cli worker
"""

import threading
import time
import traceback

from . import db


def enqueue(kind: str, params: dict | None = None) -> int:
    """Add a job and return its id."""
    connection = db.connect()
    cursor = connection.execute(
        "INSERT INTO jobs (kind, params, status, created_at) VALUES (?, ?, 'queued', ?)",
        (kind, db.to_json(params or {}), db.now_iso()),
    )
    connection.commit()
    job_id = cursor.lastrowid
    connection.close()
    return job_id


def _run_job(kind: str, params: dict):
    """Dispatch one job to the code that does the work. Returns a JSON-able result."""
    if kind == "source-refresh":
        from . import ingest
        return ingest.refresh_source(params["source_id"], actor=params.get("actor", "system"))
    if kind == "refresh-all":
        from . import ingest
        from .sources import registry
        return [ingest.refresh_source(s["id"]) for s in registry.load_definitions() if s.get("enabled") and s.get("adapter")]
    if kind == "imagery-ingest":
        from .imagery import archive
        return archive.ingest_aoi(params["aoi_id"], max_new_scenes=params.get("max_new_scenes"), log=lambda _: None)
    if kind == "change-analysis":
        from .change import engine
        return engine.run(params["aoi_id"], log=lambda _: None)
    if kind == "discovery-build":
        from .imagery import discovery
        version = discovery.build(actor=params.get("actor", "system"), log=lambda _: None)
        return {"version_id": version["id"], "clusters": version["n_clusters"], "embeddings": version["n_embeddings"]}
    if kind == "discovery-update":
        from .imagery import discovery
        result = discovery.update(actor=params.get("actor", "system"), log=lambda _: None)
        return {k: v for k, v in result.items() if k != "excluded"} | {"excluded": len(result["excluded"])}
    if kind == "ml-change-run":
        from .change_ml import learned
        run = learned.run_pair(params["before"], params["after"], actor=params.get("actor", "system"))
        return {"run_id": run["id"], "regions": run["regions_reported"], "seconds": run["seconds"]}
    if kind == "semantic-index":
        from .imagery import semantic
        result = semantic.index(params.get("aoi_id"), actor=params.get("actor", "system"), log=lambda _: None)
        return {k: v for k, v in result.items() if k != "excluded_scenes"} | {"excluded_scenes": len(result["excluded_scenes"])}
    if kind == "evaluation":
        from . import evaluation
        return evaluation.run_all()
    raise ValueError(f"unknown job kind '{kind}'")


def process_next() -> bool:
    """
    Run the oldest queued job, if any. Returns True if a job was processed.
    The status update uses `WHERE status = 'queued'` so two workers can
    never take the same job.
    """
    connection = db.connect()
    row = connection.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        connection.close()
        return False
    taken = connection.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                               (db.now_iso(), row["id"])).rowcount
    connection.commit()
    if not taken:
        connection.close()
        return True
    try:
        result = _run_job(row["kind"], db.from_json(row["params"]) or {})
        connection.execute("UPDATE jobs SET status = 'done', finished_at = ?, result = ? WHERE id = ?",
                           (db.now_iso(), db.to_json(result), row["id"]))
    except Exception as error:
        connection.execute("UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                           (db.now_iso(), f"{type(error).__name__}: {error}\n{traceback.format_exc(limit=3)}", row["id"]))
    connection.commit()
    connection.close()
    return True


def run_forever(poll_seconds: float = 2.0, stop_event: threading.Event | None = None) -> None:
    """Keep processing jobs until stopped."""
    while stop_event is None or not stop_event.is_set():
        if not process_next():
            time.sleep(poll_seconds)


def start_background_thread() -> threading.Event:
    """Start the worker as a daemon thread inside the API. Returns a stop event."""
    stop_event = threading.Event()
    threading.Thread(target=run_forever, kwargs={"stop_event": stop_event}, daemon=True, name="lumon-worker").start()
    return stop_event
