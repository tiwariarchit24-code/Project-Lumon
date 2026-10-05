"""Audit chain, provenance ids, review decisions and GeoPackage export."""

import sqlite3

import pytest

from lumon import audit, db, export, provenance, review


def test_audit_chain_detects_tampering():
    connection = db.connect()
    audit.record(connection, "analyst", "confirm", "x", {"reason": "synthetic"})
    audit.record(connection, "analyst", "reject", "y")
    assert audit.verify_chain(connection) == {"ok": True, "entries": 2}
    connection.execute("UPDATE audit_log SET action = 'reject' WHERE id = 1")
    connection.commit()
    result = audit.verify_chain(connection)
    assert result["ok"] is False and result["broken_at"] == 1


def test_provenance_id_is_deterministic():
    connection = db.connect()
    a = provenance.create(connection, "scene", "s", "url", "abc", "proc", "v1", {"p": 1})
    b = provenance.create(connection, "scene", "s", "url", "abc", "proc", "v1", {"p": 1})
    c = provenance.create(connection, "scene", "s", "url", "abd", "proc", "v1", {"p": 1})
    assert a == b and a != c


def _insert_candidate(connection, candidate_id="chg-test"):
    """TEST FIXTURE change candidate."""
    geometry = {"type": "MultiPolygon", "coordinates": [[[[75, 15], [75.001, 15], [75.001, 15.001], [75, 15.001], [75, 15]]]]}
    connection.execute(
        """INSERT INTO change_candidates (id, aoi_id, change_class, direction, from_class, to_class, tile_ids, geometry, lon, lat,
               area_m2, area_min_m2, area_max_m2, last_clean_before, earliest_supported_after, supporting_scene_ids, score,
               score_kind, status, gate_results, review_state, created_at)
           VALUES (?, 'test', 'construction', 'appearance', 'vegetation', 'built', '["test:0:0"]', ?, 75, 15, 10000, 5000, 10000,
                   '2024-01-01', '2024-02-01', '[]', 0.5, 'uncalibrated-score', 'accepted', '[]', 'unreviewed', ?)""",
        (candidate_id, db.to_json(geometry), db.now_iso()))
    connection.commit()


def test_decision_is_logged_and_audited():
    connection = db.connect()
    _insert_candidate(connection)
    result = review.decide("chg-test", "rejected", "analyst-1", "synthetic reason")
    assert result["decision"] == "rejected"
    row = connection.execute("SELECT review_state FROM change_candidates WHERE id = 'chg-test'").fetchone()
    assert row["review_state"] == "rejected"
    assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert audit.verify_chain(connection)["ok"]


def test_invalid_decisions_are_refused():
    connection = db.connect()
    _insert_candidate(connection)
    with pytest.raises(ValueError):
        review.decide("chg-test", "approved", "analyst-1")
    with pytest.raises(ValueError):
        review.decide("chg-test", "relabelled", "analyst-1", new_label="quarry")
    with pytest.raises(ValueError):
        review.decide("chg-test", "confirmed", "  ")


def test_geopackage_structure(tmp_path):
    connection = db.connect()
    _insert_candidate(connection)
    result = export.export_geopackage(tmp_path / "out.gpkg")
    assert result["layers"]["change_events"] == 1
    gpkg = sqlite3.connect(tmp_path / "out.gpkg")
    assert gpkg.execute("PRAGMA application_id").fetchone()[0] == 1196444487
    tables = {r[0] for r in gpkg.execute("SELECT table_name FROM gpkg_contents")}
    assert {"change_events", "osint_events", "scenes", "decisions", "provenance"} <= tables
    blob = gpkg.execute("SELECT geom FROM change_events").fetchone()[0]
    assert blob[:2] == b"GP"


def test_audit_duplicate_is_reported_but_edits_still_fail():
    """A byte-identical double-write is listed; a real edit still breaks the chain."""
    connection = db.connect()
    audit.record(connection, "analyst", "open-evidence", "x")
    row = connection.execute("SELECT * FROM audit_log WHERE id = 1").fetchone()
    connection.execute("INSERT INTO audit_log (at, actor, action, target, details, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (row["at"], row["actor"], row["action"], row["target"], row["details"], row["prev_hash"], row["hash"]))
    connection.commit()
    audit.record(connection, "analyst", "confirmed", "x")
    result = audit.verify_chain(connection)
    assert result["ok"] and result["duplicate_entries"] == [2]
    connection.execute("UPDATE audit_log SET actor = 'someone-else' WHERE id = 3")
    connection.commit()
    assert audit.verify_chain(connection)["ok"] is False


def test_audit_concurrent_writers_do_not_fork():
    """Many threads writing at once must still produce one unbroken chain."""
    import threading
    db.connect().close()

    def write(n):
        connection = db.connect()
        audit.record(connection, "system", "test", str(n))
        connection.close()

    threads = [threading.Thread(target=write, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result = audit.verify_chain(db.connect())
    assert result == {"ok": True, "entries": 20}
