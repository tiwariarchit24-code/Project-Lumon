"""Geometry, operating-area scoping and the swap quarantine rule."""

from lumon import ingest
from lumon.geo import boundary, geometry
from lumon.sources.common import make_entity, make_event, point


def test_point_in_polygon_with_hole():
    polygon = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]
    assert geometry.point_in_polygon(1, 1, polygon)
    assert not geometry.point_in_polygon(5, 5, polygon)  # inside the hole
    assert not geometry.point_in_polygon(11, 5, polygon)


def test_haversine_one_degree_latitude():
    # One degree of latitude is about 111.2 km everywhere.
    assert abs(geometry.haversine_m(75, 10, 75, 11) - 111_195) < 200


def test_simplify_keeps_end_points_and_drops_collinear():
    line = [[0, 0], [1, 0.00001], [2, 0], [3, 0]]
    assert geometry.simplify_line(line, 0.001) == [[0, 0], [3, 0]]


def test_boundary_not_staged_returns_none(monkeypatch):
    monkeypatch.setattr(boundary, "_operating_area_parts", lambda: ())
    assert boundary.contains_point(75, 15) is None  # unknown, never guessed


def test_operating_area_filter(square_area):
    assert boundary.contains_point(75, 15) is True
    assert boundary.contains_point(85, 15) is False


def test_scope_inside_outside_and_national(square_area):
    inside = make_event("a", "earthquake", "DISASTERS", "Synthetic", point(75, 15), "2026-01-01T00:00:00Z")
    outside = make_event("b", "earthquake", "DISASTERS", "Synthetic", point(100, 15), "2026-01-01T00:00:00Z")
    national = make_event("c", "internet-outage-signal", "CONNECTIVITY", "Synthetic", None, "2026-01-01T00:00:00Z", scope="national")
    assert ingest.scope(inside) == "inside"
    assert ingest.scope(outside) == "outside"
    assert ingest.scope(national) == "national"


def test_swapped_coordinates_are_quarantined_only_when_text_names_india(square_area):
    # (lat, lon) written in the wrong order: [15, 75] instead of [75, 15].
    named = make_event("d", "flood", "DISASTERS", "Flood in India (synthetic)", point(15, 75), "2026-01-01T00:00:00Z")
    unnamed = make_entity("e", "port", "MARITIME", "Synthetic port", point(15, 75))
    assert ingest.scope(named) == "suspect-swap"
    assert ingest.scope(unnamed) == "outside"


def test_validate_rejects_bad_coordinates_and_missing_time():
    bad = make_event("x", "earthquake", "DISASTERS", "Synthetic", point(75, 95), "2026-01-01T00:00:00Z")
    no_time = make_event("y", "earthquake", "DISASTERS", "Synthetic", point(75, 15), None)
    assert ingest.validate(bad) == "coordinates out of range"
    assert ingest.validate(no_time) == "missing or unparseable time"
