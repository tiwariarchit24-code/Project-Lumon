"""
The Lumon API application (FastAPI).

Start it from the server/ folder:
    ../.venv/bin/uvicorn lumon.main:app --port 8000

What it does:
  - serves every /api/... route (see the routes/ folder)
  - runs the job worker as a background thread (set
    LUMON_EMBEDDED_WORKER=0 to run the worker as a separate process)
  - polls OpenSky automatically only when lumon/live_poll.py says it is
    configured (otherwise aircraft change only on manual refresh)
  - if the frontend has been built (npm run build -> dist/), also serves
    the user interface, so ONE local process is enough for offline use
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, live_poll, settings, worker
from .routes import change_ml, discovery, geo, imagery, osint, pilot, query, review, system


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect().close()  # create the database and tables if needed
    stop_event = None
    if settings.get_setting("LUMON_EMBEDDED_WORKER", "1") != "0":
        stop_event = worker.start_background_thread()
    # Automatic OpenSky polling: starts only when fully configured
    # (credentials + written-agreement confirmation + connected mode).
    poll_stop = live_poll.start_background_thread()
    yield
    if stop_event:
        stop_event.set()
    if poll_stop:
        poll_stop.set()


app = FastAPI(title="Project Lumon API", version="0.1.0", lifespan=lifespan)

for module in (system, geo, osint, imagery, query, review, pilot, change_ml, discovery):
    app.include_router(module.router)

# Serve the built frontend (if present) for single-process offline use.
_dist = settings.ROOT_DIR / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
