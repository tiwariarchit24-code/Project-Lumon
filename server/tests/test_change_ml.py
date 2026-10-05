"""
Learned change detection (BTC-B): pair checks, georeferenced outputs,
regions, evidence, review, refusal paths and separation from the rule-based
engine.

All imagery here is SYNTHETIC TEST FIXTURES: small generated GeoTIFFs with
controlled changes. Most tests use a FAKE network ("change = colour
difference") so they test Lumon's plumbing, not BTC-B. One test runs the real
BTC-B weights when they are present on this machine (skipped otherwise).
"""

import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform

from lumon import db, net, settings
from lumon.change_ml import learned
from lumon.query import engine, parser

SIZE = 200
ORIGIN = (294000.0, 2104000.0)  # UTM 43N (TEST FIXTURE)
SQUARE = (80, 120, 80, 120)     # rows/cols of the controlled change: 40 x 40 px


class FakeChangeNet:
    """TEST FIXTURE: change logit = 40 x (mean absolute colour difference − 0.05), at 256 x 256."""
    key = "fake-change:1"

    def __init__(self):
        self.calls = 0

    def __call__(self, before, after):
        import torch
        self.calls += 1
        difference = (before - after).abs().mean(dim=1, keepdim=True) * torch.tensor(0.229)  # back to ~0..1 units
        return 40 * (difference - 0.05)


def texture(seed=0):
    """Deterministic textured background (edges for the misregistration check)."""
    rng = np.random.default_rng(seed)
    base = rng.integers(600, 1400, size=(SIZE // 10, SIZE // 10))
    return np.kron(base, np.ones((10, 10), dtype=int))[:SIZE, :SIZE].astype("uint16")


def write_scene(folder, name, change=False, small_change=False, cloud=False, shift=0, origin=ORIGIN):
    data = np.zeros((6, SIZE, SIZE), dtype="uint16")
    base = np.roll(texture(), shift, axis=0)
    for band in range(5):
        data[band] = base
    data[5] = 4
    if change:  # controlled change: a bright square (e.g. new roof)
        r0, r1, c0, c1 = SQUARE
        data[:3, r0:r1, c0:c1] = 2800
    if small_change:  # 2 x 2 px: below the reportable region size
        data[:3, 20:22, 20:22] = 2800
    if cloud:
        data[5, 150:190, 20:60] = 9
    path = folder / f"{name}.tif"
    with rasterio.open(path, "w", driver="GTiff", width=SIZE, height=SIZE, count=6, dtype="uint16", crs="EPSG:32643",
                       transform=from_origin(*origin, 10, 10), nodata=0) as target:
        target.write(data)
        target.descriptions = ("B02", "B03", "B04", "B08", "B11", "SCL")
    return path


def add_scene(scene_id, path, acquired, quality="usable"):
    connection = db.connect()
    connection.execute(
        """INSERT INTO scenes (id, aoi_id, sensor, acquired_at, crs, file_path, file_sha256, cloud_fraction,
               quality_status, radiometric_offset, created_at) VALUES (?, 'test-aoi', 'sentinel-2a', ?, 'EPSG:32643', ?, ?, 0, ?, 0, ?)""",
        (scene_id, acquired, str(path), net.sha256_of_bytes(path.read_bytes()), quality, db.now_iso()))
    connection.commit()
    connection.close()


@pytest.fixture
def pair(isolated, monkeypatch):
    """Model 'present' (fake network) and a before/after pair with one controlled change."""
    monkeypatch.setattr(learned, "model_problems", lambda: [])
    monkeypatch.setattr(learned, "model_key", lambda: FakeChangeNet.key)
    monkeypatch.setattr(learned, "_model_error", None)
    add_scene("BEFORE", write_scene(isolated, "before"), "2023-01-10T05:00:00Z")
    add_scene("AFTER", write_scene(isolated, "after", change=True, small_change=True, cloud=True), "2024-01-12T05:00:00Z")
    return FakeChangeNet()


def square_lonlat():
    r0, r1, c0, c1 = SQUARE
    xs = [ORIGIN[0] + c0 * 10, ORIGIN[0] + c1 * 10]
    ys = [ORIGIN[1] - r1 * 10, ORIGIN[1] - r0 * 10]
    lons, lats = warp_transform("EPSG:32643", "EPSG:4326", xs, ys)
    return min(lons), min(lats), max(lons), max(lats)


def test_tile_offsets_cover_edges():
    offsets = learned.tile_offsets(456)
    assert offsets[0] == 0 and offsets[-1] == 456 - learned.TILE_PX
    assert max(b - a for a, b in zip(offsets, offsets[1:])) <= learned.STRIDE_PX
    assert learned.tile_offsets(50) == [0]


def test_estimate_shift_measures_known_offsets():
    image = texture().astype(float)
    assert np.hypot(*learned.estimate_shift(image, image)) < 0.05
    dy, dx = learned.estimate_shift(image, np.roll(image, 2, axis=0))
    assert abs(abs(dy) - 2) < 0.3 and abs(dx) < 0.3


def test_run_finds_the_controlled_change_with_georeferenced_outputs(pair):
    run = learned.run_pair("BEFORE", "AFTER", model=pair, actor="test")
    assert run["label"] == "MODEL-GENERATED CANDIDATE CHANGE"
    assert run["before_scene_id"] == "BEFORE" and run["after_scene_id"] == "AFTER"
    assert run["before_date"].startswith("2023-01-10") and run["after_date"].startswith("2024-01-12")
    # The 40 x 40 change and the 2 x 2 change. Resizing tiles to 256 px and back
    # smooths scores, so the 2 x 2 change becomes a slightly larger region; it is
    # reported with the resolution-limit flag (see test_minimum_region_size).
    assert run["regions_reported"] == 2
    # Outputs on the scenes' own grid, tagged with the sources.
    with rasterio.open(learned._path(run["mask_path"])) as mask, rasterio.open(learned._path(run["probability_path"])) as score:
        with rasterio.open(learned.settings.DATA_DIR / "after.tif") as source:
            assert mask.crs == source.crs and mask.transform == source.transform and mask.shape == source.shape
        values = mask.read(1)
        assert values[150:190, 20:60].min() == 255             # cloud in one scene: not scored
        assert values[85:115, 85:115].min() == 1                # inside the controlled change
        assert values[10:15, 150:190].max() == 0                # unchanged area
        assert score.tags()["before_scene_id"] == "BEFORE" and score.tags()["label"] == learned.LABEL
    assert run["probability_sha256"] == net.sha256_of_bytes(learned._path(run["probability_path"]).read_bytes())
    assert run["provenance"]["kind"] == "ml-change-run" and run["parameters"]["threshold"] == 0.5
    # The region sits on the square and carries evidence.
    regions = [learned.get_region(f["properties"]["id"]) for f in learned.regions_geojson(run["id"])["features"]]
    region = max(regions, key=lambda r: r["pixels"])
    small = min(regions, key=lambda r: r["pixels"])
    assert small["pixels"] < learned.SMALL_REGION_PX and any("resolution limit" in f for f in small["evidence"]["flags"])
    west, south, east, north = square_lonlat()
    assert west <= region["lon"] <= east and south <= region["lat"] <= north
    assert 1400 <= region["pixels"] <= 1700 and region["mean_score"] > 0.5
    assert region["evidence"]["spectral_after"]["brightness"] > region["evidence"]["spectral_before"]["brightness"]
    assert "not ground truth" in region["evidence"]["baseline_note"]


def test_minimum_region_size(pair, monkeypatch):
    monkeypatch.setattr(learned, "MIN_REGION_PX", 20)
    run = learned.run_pair("BEFORE", "AFTER", model=pair)
    assert run["regions_reported"] == 1 and run["regions_too_small"] == 1  # counted, not reported


def test_rerun_uses_the_stored_run(pair):
    learned.run_pair("BEFORE", "AFTER", model=pair)
    calls = pair.calls
    learned.run_pair("BEFORE", "AFTER", model=pair)
    assert pair.calls == calls
    learned.run_pair("BEFORE", "AFTER", model=pair, force=True)
    assert pair.calls > calls


@pytest.mark.parametrize("case", ["misregistered", "other-grid", "wrong-order", "quarantined", "cloudy"])
def test_pairs_that_cannot_be_compared_are_refused(pair, isolated, case):
    if case == "misregistered":
        add_scene("X", write_scene(isolated, "x", shift=3), "2025-01-01T05:00:00Z")
        before, after, expected = "BEFORE", "X", "misregistration"
    elif case == "other-grid":
        add_scene("X", write_scene(isolated, "x", origin=(294500.0, 2104000.0)), "2025-01-01T05:00:00Z")
        before, after, expected = "BEFORE", "X", "same pixel grid"
    elif case == "wrong-order":
        before, after, expected = "AFTER", "BEFORE", "earlier"
    elif case == "quarantined":
        add_scene("X", write_scene(isolated, "x"), "2025-01-01T05:00:00Z", quality="quarantined")
        before, after, expected = "BEFORE", "X", "quarantined"
    else:
        path = write_scene(isolated, "x")
        with rasterio.open(path, "r+") as target:
            scl = target.read(6)
            scl[:, :] = 9
            target.write(scl, 6)
        add_scene("X", path, "2025-01-01T05:00:00Z")
        before, after, expected = "BEFORE", "X", "clear in both"
    with pytest.raises(learned.PairRejected) as error:
        learned.run_pair(before, after, model=pair)
    assert any(expected in p for p in error.value.problems)


def test_season_gap_is_a_warning_not_a_refusal(pair, isolated):
    add_scene("JULY", write_scene(isolated, "july"), "2024-07-10T05:00:00Z")
    check = learned.check_pair("BEFORE", "JULY")
    assert check["ok"] and check["month_gap"] == 6 and any("seasonal cycle" in w for w in check["warnings"])


def test_baseline_overlap_is_reported_not_merged(pair):
    west, south, east, north = square_lonlat()
    connection = db.connect()
    connection.execute(
        """INSERT INTO change_candidates (id, aoi_id, change_class, direction, tile_ids, geometry, status, last_clean_before,
               earliest_supported_after, created_at) VALUES ('chg-test', 'test-aoi', 'construction', 'appearance', '[]', ?, 'accepted',
               '2023-02-01', '2023-12-01', ?)""",
        (json.dumps({"type": "MultiPolygon", "coordinates": [[[[west, south], [east, south], [east, north], [west, north], [west, south]]]]}),
         db.now_iso()))
    connection.commit()
    before_rules = connection.execute("SELECT COUNT(*) FROM change_candidates").fetchone()[0]
    connection.close()
    run = learned.run_pair("BEFORE", "AFTER", model=pair)
    regions = [learned.get_region(f["properties"]["id"]) for f in learned.regions_geojson(run["id"])["features"]]
    overlap = max(regions, key=lambda r: r["pixels"])["evidence"]["baseline_overlap"]
    assert overlap and overlap[0]["id"] == "chg-test" and overlap[0]["change_window_overlaps_pair"]
    connection = db.connect()
    assert connection.execute("SELECT COUNT(*) FROM change_candidates").fetchone()[0] == before_rules  # rule-based table untouched
    connection.close()


def test_review_is_audited(pair):
    run = learned.run_pair("BEFORE", "AFTER", model=pair)
    region_id = learned.regions_geojson(run["id"])["features"][0]["properties"]["id"]
    reviewed = learned.review_region(region_id, "rejected", "Analyst A", "bright roof, but seasonal?")
    assert reviewed["review_status"] == "rejected" and reviewed["reviewed_by"] == "Analyst A"
    assert learned.get_run(run["id"])["review_counts"] == {"rejected": 1, "unreviewed": 1}
    with pytest.raises(ValueError):
        learned.review_region(region_id, "confirmed", "Analyst A")  # no 'verified' state exists
    with pytest.raises(ValueError):
        learned.review_region(region_id, "plausible", "  ")
    connection = db.connect()
    assert connection.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'ml-change-review'").fetchone()[0] == 1
    connection.close()


def test_missing_model_is_not_staged_and_nothing_is_substituted(isolated, monkeypatch):
    monkeypatch.setattr(learned, "model_problems", lambda: ["Weights not staged: test"])
    add_scene("BEFORE", write_scene(isolated, "before"), "2023-01-10T05:00:00Z")
    add_scene("AFTER", write_scene(isolated, "after", change=True), "2024-01-12T05:00:00Z")
    assert learned.status()["state"] == "NOT STAGED"
    with pytest.raises(learned.ModelNotStaged):
        learned.run_pair("BEFORE", "AFTER")
    with pytest.raises(learned.ModelNotStaged):
        learned.get_model()
    assert learned.list_runs() == [] and learned.regions_geojson()["features"] == []


def test_api(pair, monkeypatch):
    from fastapi.testclient import TestClient
    from lumon import main
    monkeypatch.setenv("LUMON_EMBEDDED_WORKER", "0")
    run = learned.run_pair("BEFORE", "AFTER", model=pair)
    with TestClient(main.app) as client:
        assert client.get("/api/ml-change/status").json()["state"] == "READY"
        assert client.get("/api/ml-change/pairs/check", params={"before": "AFTER", "after": "BEFORE"}).json()["ok"] is False
        queued = client.post("/api/ml-change/runs", json={"before_scene_id": "BEFORE", "after_scene_id": "AFTER"})
        assert queued.status_code == 200 and queued.json()["job_id"]
        assert client.get("/api/ml-change/status").json()["state"] == "INDEXING"
        refused = client.post("/api/ml-change/runs", json={"before_scene_id": "AFTER", "after_scene_id": "BEFORE"})
        assert refused.status_code == 422 and refused.json()["detail"]["state"] == "PAIR REFUSED"
        assert client.get(f"/api/ml-change/runs/{run['id']}").json()["id"] == run["id"]
        assert client.get(f"/api/ml-change/runs/{run['id']}/score.png").headers["content-type"] == "image/png"
        tif = client.get(f"/api/ml-change/runs/{run['id']}/candidates.tif")
        assert tif.status_code == 200 and tif.content[:2] in (b"II", b"MM")
        features = client.get("/api/ml-change/regions", params={"run_id": run["id"]}).json()["features"]
        assert features[0]["properties"]["label"] == "MODEL-GENERATED CANDIDATE CHANGE"
        region_id = features[0]["properties"]["id"]
        assert client.post(f"/api/ml-change/review/{region_id}", json={"decision": "plausible", "analyst": "A"}).json()["review_status"] == "plausible"
        assert client.post(f"/api/ml-change/review/{region_id}", json={"decision": "maybe", "analyst": "A"}).status_code == 400
        monkeypatch.setattr(learned, "model_problems", lambda: ["Weights not staged: test"])
        blocked = client.post("/api/ml-change/runs", json={"before_scene_id": "BEFORE", "after_scene_id": "AFTER"})
        assert blocked.status_code == 409 and blocked.json()["detail"]["state"] == "NOT STAGED"


def test_command_bar_change_results_say_they_are_rule_based(monkeypatch):
    monkeypatch.setattr(learned, "model_problems", lambda: [])
    report = engine.run(parser.parse("Show significant changes since 2020"))["capability"]
    assert any(w.startswith("Results come from the RULE-BASED change engine") for w in report["warnings"])


real_weights = settings.ROOT_DIR / "data/models/btc/BTC-B_oscd96.safetensors"


@pytest.mark.skipif(not real_weights.exists(), reason="BTC-B weights not staged on this machine")
def test_real_btc_identical_images_give_no_change():
    """The real network, loaded strictly: the same image twice must not produce candidates."""
    learned._model = None
    model = learned.get_model()
    rgb = np.dstack([(texture() // 8).astype("uint8")] * 3)[:96, :96]
    scores = learned.predict_tiles(model, rgb[None], rgb[None])
    assert scores.shape == (1, 96, 96) and float((scores > learned.THRESHOLD).mean()) == 0.0


def test_map_layer_shows_one_run_at_a_time(pair, isolated):
    first = learned.run_pair("BEFORE", "AFTER", model=pair)
    add_scene("LATER", write_scene(isolated, "later", change=True), "2025-01-12T05:00:00Z")
    second = learned.run_pair("BEFORE", "LATER", model=pair)
    latest = learned.regions_geojson()
    assert latest["run_id"] == second["id"] and {f["properties"]["run_id"] for f in latest["features"]} == {second["id"]}
    assert {f["properties"]["run_id"] for f in learned.regions_geojson(first["id"])["features"]} == {first["id"]}
    assert {f["properties"]["run_id"] for f in learned.regions_geojson(all_runs=True)["features"]} == {first["id"], second["id"]}
