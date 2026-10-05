"""
Tests for the audit fixes: honest freshness statuses, map/timeline
consistency, stale and scheduled flags, PARTIAL capability, and
verify-offline working on a copy. All records are SYNTHETIC TEST FIXTURES.
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lumon import db, offline_check, settings
from lumon.query import engine
from lumon.sources import registry


def iso(delta_hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_freshness_statuses(monkeypatch):
    source = {"adapter": "x", "stale_after_hours": 1}
    assert registry.freshness(source, {"health": "ok", "last_success": iso(-0.2)}, "connected")["status"] == "LIVE"
    assert registry.freshness(source, {"health": "offline-snapshot", "last_success": iso(-0.2)}, "airgapped")["status"] == "SNAPSHOT"
    # Re-reading a snapshot offline must not make data look fresher.
    stale = registry.freshness(source, {"health": "offline-snapshot", "last_success": iso(-5)}, "connected")
    assert stale["status"] == "STALE" and "old" in stale["label"]
    assert registry.freshness(source, {"health": "failed"}, "connected")["status"] == "UNAVAILABLE"
    assert registry.freshness(source, {}, "connected")["status"] == "NOT STAGED"
    assert registry.freshness({"adapter": None}, {}, "connected")["status"] == "NOT IMPLEMENTED"
    keyed = {"adapter": "x", "requires_key": True, "key_env": "LUMON_TEST_MISSING_KEY"}
    assert registry.freshness(keyed, {}, "connected")["status"] == "KEY REQUIRED"


def _insert_event(connection, event_id, event_type, category, start, lon=75.0, lat=15.0, source="usgs-earthquakes", attributes="{}"):
    """TEST FIXTURE event."""
    connection.execute(
        """INSERT INTO events (id, source_id, type, category, title, lon, lat, start_time, evidence_type, attributes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OBSERVED', ?, ?, ?)""",
        (event_id, source, event_type, category, f"synthetic {event_type}", lon, lat, start, attributes, iso(0), iso(0)))


def test_map_and_timeline_agree_and_flags(monkeypatch):
    monkeypatch.setenv("LUMON_EMBEDDED_WORKER", "0")
    connection = db.connect()
    _insert_event(connection, "e1", "earthquake", "DISASTERS", iso(-24 * 3))
    _insert_event(connection, "e2", "earthquake", "DISASTERS", iso(-24 * 40))  # outside a 30-day window
    _insert_event(connection, "a1", "aircraft-position", "AVIATION", iso(-3), source="opensky-aircraft")
    _insert_event(connection, "l1", "launch", "SPACE", iso(24 * 20), source="launch-library")  # scheduled, future
    connection.execute("INSERT INTO events (id, source_id, type, category, title, start_time, evidence_type, attributes, created_at, updated_at) VALUES ('k1','noaa-swpc-kp','geomagnetic-kp','SPACE','kp',?, 'OBSERVED','{}',?,?)",
                       (iso(-5), iso(0), iso(0)))
    connection.commit()
    from lumon import main
    start, end = iso(-24 * 30), iso(0)
    with TestClient(main.app) as client:
        types = "earthquake,launch,aircraft-position"
        events = client.get("/api/events", params={"start": start, "end": end, "types": types, "upcoming_days": 90}).json()
        timeline = client.get("/api/timeline", params={"start": start, "end": end, "types": types, "upcoming_days": 90}).json()
    props = {f["properties"]["id"]: f["properties"] for f in events["features"]}
    assert props["a1"]["stale"] is True  # 3 h old, OpenSky limit is 0.25 h
    assert props["l1"]["scheduled"] is True and events["upcoming_total"] == 1
    assert "e2" not in props
    # Main band = non-telemetry events the map shows (1 earthquake); the
    # aircraft and the Kp value go to the separate telemetry band.
    assert sum(sum(day.values()) for day in timeline["density"].values()) == 1
    assert sum(timeline["telemetry"].values()) == 2
    assert len(timeline["upcoming"]) == 1


def test_capability_partial_vs_unsupported():
    connection = db.connect()
    fires = engine.capability({"intent": "find-events", "target": {"types": ["fire-detection"]}, "unrecognised": []}, connection)
    assert fires["state"] == "PARTIAL" and fires["supported"] is True and fires["partial"]
    vessels = engine.capability({"intent": "find-events", "target": {"types": ["vessel-position"]}, "unrecognised": []}, connection)
    assert vessels["state"] == "UNSUPPORTED" and vessels["supported"] is False


def test_verify_offline_uses_a_copy():
    connection = db.connect()
    connection.execute("INSERT INTO sources (id, health, last_success) VALUES ('usgs-earthquakes', 'ok', ?)", (iso(-1),))
    connection.commit()
    connection.close()
    real = settings.DATABASE_PATH
    scratch = offline_check._use_scratch_copy()
    assert settings.DATABASE_PATH != real and settings.DATABASE_PATH.parent == scratch
    copy = db.connect()
    copy.execute("UPDATE sources SET health = 'offline-snapshot'")
    copy.commit()
    copy.close()
    import sqlite3
    original = sqlite3.connect(real).execute("SELECT health FROM sources").fetchone()[0]
    assert original == "ok"  # the real database was not changed
