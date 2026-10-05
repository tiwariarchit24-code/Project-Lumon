"""
Offline verification (Stage 11): prove that, once data is staged, the whole
system works with NO network.

How it works:
  1. force LUMON_MODE=airgapped
  2. replace Python's socket connect with a guard that raises if anything
     tries to connect to a non-local address (and records the attempt)
  3. exercise the stack against the REAL staged data: API endpoints the UI
     uses, every source refresh (which must fall back to the last snapshot),
     a natural-language query, satellite chips, the change engine, the
     review queue, the GeoPackage export and the audit verification
  4. report each check as PASS / FAIL and list any blocked connection

The checks WRITE (source refreshes update source health, the change engine
rewrites candidates, the export writes a file). So they run against a
temporary COPY of the database and of the writable folders, in
data/offline-check/, which is deleted afterwards. The real database's
source health and the real exports are never changed by this check.

Run:  python -m lumon.cli verify-offline
Note: this checks the Python backend. The browser side is offline by
construction (it only calls /api on this machine; fonts and map style are
local) - see docs/offline-mode.md for how to confirm that in a browser.
"""

import os
import shutil
import socket
import sqlite3
import time

from . import db, settings

BLOCKED = []


def _guarded_connect(original):
    """Wrap socket.connect: allow only localhost / unix sockets."""
    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "localhost", "::1") and not str(host).startswith("/"):
            BLOCKED.append(str(address))
            raise OSError(f"offline check: blocked network connection to {address}")
        return original(self, address)
    return connect


def _use_scratch_copy() -> object:
    """
    Copy the database (with SQLite's backup API, which also captures recent
    WAL writes) and the reference layers into data/offline-check/, then point
    the settings at the copies. Returns the scratch folder.
    """
    scratch = settings.DATA_DIR / "offline-check"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    source = sqlite3.connect(settings.DATABASE_PATH)
    target = sqlite3.connect(scratch / "lumon.db")
    source.backup(target)
    source.close()
    target.close()
    if settings.REFERENCE_DIR.exists():
        shutil.copytree(settings.REFERENCE_DIR, scratch / "reference")
    settings.DATABASE_PATH = scratch / "lumon.db"
    settings.REFERENCE_DIR = scratch / "reference"
    settings.EXPORT_DIR = scratch / "exports"
    return scratch


def run() -> dict:
    """Run every check and return {"passed", "failed", "checks", "blocked"}."""
    os.environ["LUMON_MODE"] = "airgapped"
    os.environ["LUMON_EMBEDDED_WORKER"] = "0"
    real_database = settings.DATABASE_PATH
    scratch = _use_scratch_copy()
    try:
        return _run_checks(real_database)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_checks(real_database) -> dict:
    """The checks themselves (always on the scratch copy, see run())."""
    socket.socket.connect = _guarded_connect(socket.socket.connect)

    from fastapi.testclient import TestClient

    from . import main
    from .sources import registry

    checks = []

    def check(name, function):
        started = time.perf_counter()
        try:
            detail = function()
            checks.append({"check": name, "result": "PASS", "detail": detail, "ms": round((time.perf_counter() - started) * 1000)})
        except Exception as error:  # report, never crash the whole check
            checks.append({"check": name, "result": "FAIL", "detail": f"{type(error).__name__}: {error}", "ms": round((time.perf_counter() - started) * 1000)})

    with TestClient(main.app) as client:
        def get(path):
            response = client.get(path)
            assert response.status_code == 200, f"{path} -> {response.status_code}"
            return response

        check("mode is air-gapped", lambda: get("/api/health").json()["mode"] == "airgapped" or (_ for _ in ()).throw(AssertionError("mode not airgapped")))
        check("boundary staged", lambda: get("/api/operating-area").json()["status"]["staged"] or (_ for _ in ()).throw(AssertionError("boundary missing")))
        check("layer registry", lambda: f"{sum(l['available'] for l in get('/api/layers').json()['layers'])} layers available offline")
        check("boundary file served", lambda: f"{len(get('/api/boundaries/india-states.geojson').json()['features'])} states")
        check("events served", lambda: f"{len(get('/api/events').json()['features'])} events")
        check("entities served", lambda: f"{len(get('/api/entities').json()['features'])} entities")

        def refresh_all():
            results = []
            from . import ingest
            for source in registry.load_definitions():
                if source.get("adapter") and source.get("enabled"):
                    summary = ingest.refresh_source(source["id"], actor="offline-check")
                    results.append(f"{source['id']}={summary['status']}")
                    assert summary["status"] in ("offline-snapshot", "skipped"), f"{source['id']} -> {summary['status']}: {summary['error']}"
            return ", ".join(results)
        check("source refresh falls back to snapshots", refresh_all)

        def query():
            plan = client.post("/api/query/parse", json={"text": "Show newly constructed areas near rivers"}).json()
            result = client.post("/api/query/run", json={"plan": plan}).json()
            return f"{result['count']} results, supported={result['capability']['supported']}"
        check("query parse + run", query)

        def chips():
            queue = get("/api/review/queue").json()
            assert queue, "no change candidates"
            detail = get(f"/api/changes/{queue[0]['id']}").json()
            png = get(f"/api/scenes/{detail['evidence_after']['id']}/quicklook.png")
            assert png.content[:4] == b"\x89PNG"
            return f"evidence chip {len(png.content)} bytes"
        check("satellite evidence chip", chips)

        def change_engine():
            from .change import engine
            from .imagery import aoi as aoi_module
            return str([engine.run(a["id"], log=lambda _: None)["status"] for a in aoi_module.load_aois()])
        check("change engine", change_engine)

        def similarity():
            queue = get("/api/review/queue").json()
            result = client.post("/api/review/more-like-these", json={"candidate_ids": [queue[0]["id"]]}).json()
            return f"{len(result['results'])} similar tiles"
        check("similarity search", similarity)

        def semantic_search():
            # Real RemoteCLIP inference with the network blocked. When the
            # model is not staged, the honest refusal is the expected result.
            status = get("/api/semantic/status").json()
            response = client.post("/api/semantic/search", json={"text": "airport runway", "limit": 3})
            if status["state"] in ("READY", "PARTIAL"):
                body = response.json()
                assert response.status_code == 200 and body["results"], "no ranked chips"
                return f"{status['state']}: {body['chips_searched']} chips ranked, top {body['results'][0]['score']:.3f} (cosine), {body['timing_ms']['total']:.0f} ms"
            assert response.status_code in (409, 503), "search should be refused when the model is not usable"
            return f"{status['state']}: refused honestly ({response.status_code})"
        check("semantic image search (RemoteCLIP)", semantic_search)

        def image_similarity():
            # Image-to-image similarity from the cached embeddings (no model run).
            status = get("/api/semantic/status").json()
            if status["state"] not in ("READY", "PARTIAL"):
                response = client.post("/api/semantic/similar", json={"chip_ids": ["none"]})
                assert response.status_code in (400, 409, 503), "similarity should be refused when the model is not usable"
                return f"{status['state']}: refused honestly ({response.status_code})"
            example = client.post("/api/semantic/search", json={"text": "river", "limit": 1}).json()["results"][0]["id"]
            body = client.post("/api/semantic/similar", json={"chip_ids": [example], "limit": 5}).json()
            assert body["results"], "no similar chips"
            return f"{body['chips_searched']} chips compared, top {body['results'][0]['score']:.3f} (cosine), {body['timing_ms']['total']:.0f} ms"
        check("image-to-image similarity (RemoteCLIP)", image_similarity)

        def learned_change():
            # Real BTC-B inference on one real tile pair, network blocked; no files written.
            status = get("/api/ml-change/status").json()
            if status["state"] != "READY":
                response = client.post("/api/ml-change/runs", json={"before_scene_id": "x", "after_scene_id": "y"})
                assert response.status_code in (409, 422), "a run should be refused when the model is not usable"
                return f"{status['state']}: refused honestly ({response.status_code})"
            import time as _time
            import numpy as np
            from .change_ml import learned
            from .imagery import semantic
            pair = learned.suggest_pairs(limit=1)[0]
            tiles = []
            connection = db.connect()
            scenes = {row["id"]: dict(row) for row in connection.execute(
                "SELECT * FROM scenes WHERE id IN (?, ?)", (pair["before_scene_id"], pair["after_scene_id"]))}
            connection.close()
            for scene_id in (pair["before_scene_id"], pair["after_scene_id"]):
                scene = scenes[scene_id]
                stack = learned._read(scene)[0]
                tiles.append(semantic.to_rgb(stack, scene["radiometric_offset"])[:learned.TILE_PX, :learned.TILE_PX][None])
            started = _time.perf_counter()
            scores = learned.predict_tiles(learned.get_model(), tiles[0], tiles[1])
            regions = get("/api/ml-change/regions").json()["features"]
            return (f"1 tile pair scored in {(_time.perf_counter() - started):.1f} s (incl. model load), "
                    f"{float((scores > learned.THRESHOLD).mean()):.1%} candidate px; {status['runs']} stored runs, {len(regions)} regions in latest")
        check("learned change detection (BTC-B)", learned_change)

        def discovery_clusters():
            # Reference chip -> cluster -> member places, from stored clusters (no model run).
            status = get("/api/discovery/status").json()
            if status["state"] not in ("READY", "PARTIAL"):
                response = client.post("/api/discovery/discover", json={"chip_id": "none"})
                assert response.status_code in (404, 409), "discovery should be refused without clusters"
                return f"{status['state']}: refused honestly ({response.status_code})"
            example = client.post("/api/semantic/search", json={"text": "river", "limit": 1}).json()["results"][0]["id"]
            found = client.post("/api/discovery/discover", json={"chip_id": example}).json()
            geo = get(f"/api/discovery/clusters/{found['cluster']['cluster_id']}/places.geojson").json()
            return (f"{status['version']['n_clusters']} clusters; reference in {found['cluster']['label']} "
                    f"({found['cluster']['n_places']} places, {len(geo['features'])} map footprints)")
        check("discovery clusters (RemoteCLIP embeddings)", discovery_clusters)

        def local_ingest_check():
            # A staged real scene registered as a local GeoTIFF (in place, on the temporary database copy).
            from .imagery import local_ingest
            files = sorted((settings.ROOT_DIR / "data" / "imagery").glob("*/*.tif"))
            if not files:
                return "no local GeoTIFF available to test"
            report = local_ingest.ingest_file(str(files[-1]), "offline-check-local", actor="verify-offline")
            again = local_ingest.ingest_file(str(files[-1]), "offline-check-local", actor="verify-offline")
            supported = sum(1 for v in report["compatibility"].values() if v.startswith("SUPPORTED"))
            return f"{report['outcome']} then {again['outcome']}; {report['metadata']['format']}, {supported}/{len(report['compatibility'])} capabilities supported"
        check("local GeoTIFF/COG ingestion", local_ingest_check)

        check("export GeoPackage", lambda: str(client.post("/api/export/geopackage").json()["layers"]))
        check("audit chain", lambda: get("/api/audit/verify").json()["ok"] or (_ for _ in ()).throw(AssertionError("audit broken")))

    passed = sum(c["result"] == "PASS" for c in checks)
    return {"passed": passed, "failed": len(checks) - passed, "checks": checks, "blocked_connections": BLOCKED,
            "ran_on": "temporary copy of " + str(real_database.relative_to(settings.ROOT_DIR))}
