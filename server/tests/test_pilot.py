"""
NIT Raipur pilot foundation: configuration, honest study-area state, no
fabricated geometry, capability/model registries, NIT-scoped search and the
AI evidence contract. All geometry used here is a SYNTHETIC TEST FIXTURE.
"""

import json
from datetime import datetime, timezone

import pytest

from lumon import ai_outputs, db, pilot, settings
from lumon.geo import places
from lumon.query import engine, parser

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def _stage_fixture_place(name="Raipur"):
    """TEST FIXTURE gazetteer with one synthetic point (not a real location)."""
    settings.BOUNDARY_DIR.mkdir(parents=True, exist_ok=True)
    collection = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [75.0, 15.0]},
                  "properties": {"name": name, "state": "Fixture", "population": 1, "rank": 1}}]}
    (settings.BOUNDARY_DIR / "india-places.geojson").write_text(json.dumps(collection))
    places.reload()


@pytest.fixture(autouse=True)
def fresh_gazetteer():
    places.reload()
    yield
    places.reload()


def test_pilot_config_exists_and_is_complete():
    config = pilot.load_pilot()
    assert config["id"] == "nit-raipur" and config["canonical_name"] == "National Institute of Technology Raipur"
    assert {s["id"] for s in config["study_areas"]} == {"nit-raipur", "nit-raipur-campus"}
    assert config["boundary"]["provisional"]["method"] == "gazetteer-buffer"
    for key in ("description", "status", "data_registry"):
        assert config[key]


def test_no_fabricated_geometry_in_configuration():
    """The pilot config must not contain typed-in coordinates or geometry."""
    text = (settings.CONFIG_DIR / "pilots" / "nit-raipur.json").read_text()
    assert '"coordinates"' not in text and '"geometry"' not in text
    assert '"lat"' not in text and '"lon"' not in text


def test_study_area_is_provisional_and_derived_from_staged_place():
    _stage_fixture_place()
    area = pilot.study_area()
    assert area["status"] == "PROVISIONAL" and area["campus_boundary"] is False
    lons = [p[0] for p in area["geometry"]["coordinates"][0]]
    assert min(lons) < 75.0 < max(lons)  # centred on the staged (fixture) place
    assert "NOT the campus boundary" in area["meaning"]


def test_study_area_unavailable_without_gazetteer():
    assert pilot.study_area()["status"] == "UNAVAILABLE"
    assert pilot.aoi_feature_collection()["features"] == []


def test_verified_boundary_replaces_provisional(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT_DIR", tmp_path)
    folder = tmp_path / "data" / "pilots" / "nit-raipur"
    folder.mkdir(parents=True)
    polygon = {"type": "Polygon", "coordinates": [[[75, 15], [75.01, 15], [75.01, 15.01], [75, 15], [75, 15]]]}  # TEST FIXTURE
    (folder / "campus-boundary.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": polygon, "properties": {}}]}))
    assert pilot.study_area()["status"] == "STAGED · UNVERIFIED"
    (folder / "campus-boundary.meta.json").write_text(json.dumps({"verified": True, "source": "fixture"}))
    area = pilot.study_area()
    assert area["status"] == "VERIFIED" and area["campus_boundary"] is True


def test_data_registry_states_are_honest():
    entries = pilot.load_pilot()["data_registry"]
    assert len(entries) == 13
    for entry in entries:
        assert entry["state"] in pilot.DATA_STATES
        for field in ("name", "category", "expected_format", "source", "provenance", "temporal_coverage", "spatial_coverage", "notes"):
            assert field in entry
        if entry["state"] in ("AVAILABLE", "PARTIAL"):
            assert entry["provenance"], f"{entry['name']} claims data without provenance"
    assert not any(e["state"] == "AVAILABLE" for e in entries)  # nothing institutional is staged yet


def _without_staged_models(monkeypatch):
    """Independent of this machine: as if no model weights were present."""
    from lumon.change_ml import learned
    from lumon.imagery import semantic
    monkeypatch.setattr(semantic, "model_problems", lambda: ["Weights not staged (test)"])
    monkeypatch.setattr(learned, "model_problems", lambda: ["Weights not staged (test)"])


def test_capabilities_and_models_are_not_staged(monkeypatch):
    _without_staged_models(monkeypatch)
    registry = pilot.load_capabilities()
    ids = {c["id"] for c in registry["capabilities"]}
    assert ids == {"semantic-search", "image-similarity", "discovery", "eo-embedding", "segmentation", "change-detection",
                   "land-cover", "construction", "vegetation", "temporal-analysis", "analyst-summary"}
    for item in registry["capabilities"]:
        assert item["status"] in registry["statuses"]
        assert item["status"] == "NOT STAGED"
        for field in ("name", "purpose", "requires_data", "model", "component", "output", "limitations"):
            assert field in item
    for model in pilot.load_models():
        assert model["deployment_status"] == "NOT STAGED"
    assert {m["id"] for m in pilot.load_models()} >= {"prithvi-eo-2", "sam-2", "changeformer"}
    assert pilot.validate_registries() == []


def test_staged_remoteclip_stages_only_its_three_capabilities(monkeypatch):
    from lumon.imagery import semantic
    _without_staged_models(monkeypatch)
    monkeypatch.setattr(semantic, "model_problems", lambda: [])
    registry = pilot.load_capabilities()
    for item in registry["capabilities"]:
        # Staged weights are not validation: never READY.
        assert item["status"] == ("STAGED" if item["id"] in ("semantic-search", "image-similarity", "discovery") else "NOT STAGED")
    semantic_search = pilot.capability("semantic-search")
    assert semantic_search["model"] == "remoteclip" and semantic_search["retrieval_status"] in ("NOT STAGED", "INDEXING", "PARTIAL", "READY")
    models = {m["id"]: m["deployment_status"] for m in pilot.load_models()}
    assert models["remoteclip"] == "STAGED" and models["prithvi-eo-2"] == "NOT STAGED"
    assert pilot.validate_registries() == []


def test_staged_btc_stages_only_learned_change_detection(monkeypatch):
    from lumon.change_ml import learned
    _without_staged_models(monkeypatch)
    monkeypatch.setattr(learned, "model_problems", lambda: [])
    registry = pilot.load_capabilities()
    for item in registry["capabilities"]:
        assert item["status"] == ("STAGED" if item["id"] == "change-detection" else "NOT STAGED")
    change = pilot.capability("change-detection")
    assert change["model"] == "btc-b-oscd96" and change["retrieval_status"] in ("READY", "INDEXING", "UNAVAILABLE")
    assert "Never used in place of the model" in change["fallback"]
    models = {m["id"]: m["deployment_status"] for m in pilot.load_models()}
    assert models["btc-b-oscd96"] == "STAGED" and models["changeformer"] == "NOT STAGED"
    assert pilot.validate_registries() == []


def test_validation_rejects_ready_capability_without_model(monkeypatch):
    original = pilot.load_capabilities()
    broken = json.loads(json.dumps(original))
    next(c for c in broken["capabilities"] if c["model"] == "prithvi-eo-2")["status"] = "READY"
    monkeypatch.setattr(pilot, "load_capabilities", lambda: broken)
    assert any("not staged" in p for p in pilot.validate_registries())


@pytest.mark.parametrize("text, area, operation, start, target", [
    ("Show significant changes at NIT Raipur since 2020", "nit-raipur", "change-detection", "2020-01-01T00:00:00Z", None),
    ("Find new construction near the hostels", "nit-raipur-campus", "construction", None, None),
    ("Find imagery similar to this location", "india", "image-similarity", None, None),
    ("Show vegetation change on campus", "nit-raipur-campus", "vegetation", None, "vegetation"),
    ("Find buildings added after 2020", "india", "construction", "2021-01-01T00:00:00Z", "building"),
])
def test_nit_scoped_intents(text, area, operation, start, target, monkeypatch):
    _without_staged_models(monkeypatch)
    plan = parser.parse(text, NOW)
    assert plan["study_area"]["id"] == area
    assert plan["operation"]["id"] == operation and plan["operation"]["status"] == "NOT STAGED"
    assert (plan["time"] or {}).get("start") == start
    assert (plan["target_class"] or {}).get("class") == target
    assert plan["unrecognised"] == []
    if area != "india":
        assert plan["place"] is None  # "Raipur" in the pilot name is not the city gazetteer point


def test_not_staged_capabilities_give_honest_responses():
    connection = db.connect()
    for text in ("Show significant changes at NIT Raipur since 2020", "Show vegetation change on campus", "Find new construction near the hostels"):
        plan = parser.parse(text, NOW)
        report = engine.capability(plan, connection)
        assert report["state"] == "NOT STAGED" and report["supported"] is False and report["issues"]
        result = engine.run(plan)
        assert result["count"] == 0 and result["results"] == []
    # Genuinely unsupported requests are still refused as before.
    vessels = engine.capability(parser.parse("Find vessels near Mumbai", NOW), connection)
    assert vessels["state"] == "UNSUPPORTED"


def _fixture_output():
    """TEST FIXTURE AI output (synthetic; no model produced it)."""
    return {
        "capability_id": "construction", "study_area_id": "nit-raipur", "source": "Sentinel-2 L2A (fixture)",
        "acquisition_date": "2024-01-15", "processing_date": "2026-10-04T00:00:00Z",
        "geometry": {"type": "Point", "coordinates": [75.0, 15.0]}, "model_id": "changeformer", "model_version": "fixture-0",
        "confidence": 0.5, "confidence_kind": "uncalibrated-score", "evidence_refs": ["scene:FIXTURE_A", "scene:FIXTURE_B"],
        "temporal_evidence": [{"date": "2023-01-10", "observation_ref": "scene:FIXTURE_A", "state": "soil"}],
        "review_state": "unreviewed",
    }


def test_ai_output_contract_and_provenance_round_trip():
    connection = db.connect()
    bad = _fixture_output()
    del bad["model_version"]
    bad["confidence_kind"] = "probability"
    assert ai_outputs.validate(bad)
    with pytest.raises(ValueError):
        ai_outputs.store(connection, bad, "fixture://pair", None)
    output_id = ai_outputs.store(connection, _fixture_output(), "fixture://pair", "abc123", {"threshold": 0.5})
    record = ai_outputs.get(connection, output_id)
    assert record["model_version"] == "fixture-0" and record["confidence_kind"] == "uncalibrated-score"
    assert record["evidence_refs"] == ["scene:FIXTURE_A", "scene:FIXTURE_B"]
    assert record["provenance"]["kind"] == "ai-output" and record["provenance"]["input_sha256"] == "abc123"
    assert record["provenance"]["parameters"] == {"threshold": 0.5}
    assert record["review_state"] == "unreviewed"


def test_pilot_scope_does_not_carry_demo_aoi_warnings():
    connection = db.connect()
    report = engine.capability(parser.parse("Show significant changes at NIT Raipur since 2020", NOW), connection)
    assert not any("staged AOIs only" in w for w in report["warnings"])
