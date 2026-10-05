"""Deterministic query parsing: chips, assumed values and unrecognised words."""

from datetime import datetime, timezone

from lumon.query import parser

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def test_construction_near_rivers_since_january():
    plan = parser.parse("Show newly constructed areas within 200 m of rivers since January", NOW)
    assert plan["intent"] == "find-changes"
    assert plan["change"]["class"] == "construction"
    assert plan["reference"]["kind"] == "river"
    assert plan["distance"] == {"value_m": 200.0, "matched": "200 m", "assumed": False}
    assert plan["time"]["start"] == "2026-01-01T00:00:00Z"
    assert plan["unrecognised"] == []


def test_since_future_month_means_last_year():
    plan = parser.parse("water expansion since November", NOW)
    assert plan["time"]["start"] == "2025-11-01T00:00:00Z"


def test_missing_distance_is_assumed_and_flagged():
    plan = parser.parse("construction near rivers", NOW)
    assert plan["distance"]["assumed"] is True
    assert "Edit" in plan["distance"]["note"]


def test_recent_is_assumed_window():
    plan = parser.parse("recent fires", NOW)
    assert plan["time"]["assumed"] is True
    assert plan["target"]["types"] == ["fire-detection", "wildfire"]


def test_magnitude_filter_keeps_decimals():
    plan = parser.parse("earthquakes M4.5+ last 2 weeks", NOW)
    assert plan["filters"]["min_magnitude"]["value"] == 4.5
    assert plan["time"]["start"] == "2026-09-20T00:00:00Z"


def test_unknown_words_are_reported_not_guessed():
    plan = parser.parse("find vessels near the western coast", NOW)
    assert "western" in plan["unrecognised"]
    assert plan["target"]["types"] == ["vessel-position"]


def test_flood_context_attached_to_change_query():
    plan = parser.parse("newly built areas where flood activity was reported", NOW)
    assert plan["intent"] == "find-changes"
    assert plan["related_events"]["types"] == ["flood"]
