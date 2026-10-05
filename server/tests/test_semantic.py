"""
Semantic image search: chips, cache, ranking, exclusions, metadata, and the
missing-model path.

Everything here uses SYNTHETIC TEST FIXTURES: tiny generated GeoTIFFs and a
FAKE embedding model whose "embedding" is just the chip's mean colour. They
test Lumon's plumbing (what gets indexed, cached, excluded, returned), NOT
RemoteCLIP and NOT real-world retrieval quality.
"""

import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform as warp_transform

from lumon import db, net, pilot, worker
from lumon.imagery import semantic
from lumon.query import engine, parser

SIZE = 240  # pixels: 2 x 2 = 4 overlapping 224-px chips and 3 x 3 = 9 112-px chips per scene (13)
ORIGIN = (294000.0, 2104000.0)  # UTM 43N, top-left corner (TEST FIXTURE)


class FakeModel:
    """TEST FIXTURE: 'embeds' a chip as its normalised mean R, G, B (plus a constant)."""
    key = "fake-model:1"
    version = {"model": "fake test model"}

    def __init__(self):
        self.images_encoded = 0

    def encode_images(self, chips):
        self.images_encoded += len(chips)
        vectors = np.array([[c[..., 0].mean(), c[..., 1].mean(), c[..., 2].mean(), 1.0] for c in chips], dtype="float32")
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def encode_text(self, text):
        vector = {"red": [1, 0, 0, 0.1], "green": [0, 1, 0, 0.1]}[text]
        vector = np.array(vector, dtype="float32")
        return vector / np.linalg.norm(vector)


def write_scene(folder, name, colour, cloud_rows=0):
    """A 6-band GeoTIFF (B02, B03, B04, B08, B11, SCL). `colour` = (R, G, B) DN; SCL 9 (cloud) on the top rows."""
    path = folder / f"{name}.tif"
    red, green, blue = colour
    data = np.zeros((6, SIZE, SIZE), dtype="uint16")
    data[0], data[1], data[2], data[3], data[4] = blue, green, red, 2000, 1500
    data[5] = 4  # vegetation: a valid SCL class
    data[5, :cloud_rows, :] = 9
    with rasterio.open(path, "w", driver="GTiff", width=SIZE, height=SIZE, count=6, dtype="uint16",
                       crs="EPSG:32643", transform=from_origin(*ORIGIN, 10, 10), nodata=0) as target:
        target.write(data)
        target.descriptions = ("B02", "B03", "B04", "B08", "B11", "SCL")
    return path


def add_scene(scene_id, path, acquired, quality="usable", offset=0.0, checksum=None):
    connection = db.connect()
    connection.execute(
        """INSERT INTO scenes (id, aoi_id, sensor, acquired_at, crs, file_path, file_sha256, cloud_fraction,
               quality_status, radiometric_offset, created_at) VALUES (?, 'test-aoi', 'sentinel-2a', ?, 'EPSG:32643', ?, ?, 0, ?, ?, ?)""",
        (scene_id, acquired, str(path), checksum or net.sha256_of_bytes(path.read_bytes()), quality, offset, db.now_iso()))
    connection.commit()
    connection.close()


@pytest.fixture
def staged(monkeypatch, isolated):
    """Model 'present' (fake) and two clean scenes: one red, one green."""
    monkeypatch.setattr(semantic, "model_problems", lambda: [])
    monkeypatch.setattr(semantic, "model_key", lambda: FakeModel.key)
    monkeypatch.setattr(semantic, "_model_error", None)
    model = FakeModel()
    add_scene("SCENE_RED", write_scene(isolated, "red", (2500, 300, 300)), "2024-01-10T05:00:00Z")
    add_scene("SCENE_GREEN", write_scene(isolated, "green", (300, 2500, 300)), "2025-01-10T05:00:00Z")
    return model


def test_chip_windows_cover_the_scene_edge_to_edge():
    windows = semantic.chip_windows(456, 486)
    for size in semantic.CHIP_SIZES_PX:
        rows = sorted({r for s, r, _ in windows if s == size})
        cols = sorted({c for s, _, c in windows if s == size})
        assert rows[0] == 0 and rows[-1] == 456 - size
        assert cols[0] == 0 and cols[-1] == 486 - size
    assert semantic.chip_windows(100, 100) == []  # smaller than every chip size: nothing is invented


def test_index_embeds_once_then_uses_the_cache(staged):
    first = semantic.index(model=staged, log=lambda _: None)
    assert first["scenes_indexed"] == 2 and first["chips_embedded"] == 26 and first["chips_cached"] == 0
    encoded = staged.images_encoded
    second = semantic.index(model=staged, log=lambda _: None)
    assert second["chips_embedded"] == 0 and second["chips_cached"] == 26
    assert staged.images_encoded == encoded  # nothing re-embedded


def test_ranking_orders_by_similarity(staged):
    semantic.index(model=staged, log=lambda _: None)
    result = semantic.search("red", limit=50, model=staged)
    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)
    assert {r["scene_id"] for r in result["results"][:10]} == {"SCENE_RED"}
    assert result["results"][0]["score_kind"].startswith("cosine similarity")
    assert "not a detection" in result["note"]
    green_first = semantic.search("green", limit=1, model=staged)["results"][0]
    assert green_first["scene_id"] == "SCENE_GREEN"


def test_time_filter(staged):
    semantic.index(model=staged, log=lambda _: None)
    result = semantic.search("red", limit=50, start="2025-01-01T00:00:00Z", model=staged)
    assert {r["scene_id"] for r in result["results"]} == {"SCENE_GREEN"}


def test_results_keep_scene_metadata_and_true_footprint(staged):
    semantic.index(model=staged, log=lambda _: None)
    item = next(r for r in semantic.search("red", limit=50, model=staged)["results"] if r["chip_px"] == 224)
    assert item["scene_id"] == "SCENE_RED" and item["acquired_at"] == "2024-01-10T05:00:00Z" and item["aoi_id"] == "test-aoi"
    # Footprint = the raster's own window bounds, transformed to WGS84.
    row, col = (int(part) for part in item["id"].split(":")[2:4])  # chip id = scene:size:row:col
    x0, y0 = ORIGIN[0] + col * 10, ORIGIN[1] - row * 10
    lons, lats = warp_transform("EPSG:32643", "EPSG:4326", [x0, x0 + 2240], [y0, y0 - 2240])
    ring = item["footprint"]["coordinates"][0]
    assert ring[0] == pytest.approx([lons[0], lats[0]], abs=1e-5)
    assert ring[2] == pytest.approx([lons[1], lats[1]], abs=1e-5)
    assert item["provenance_id"]
    record = semantic.chip_record(item["id"])
    assert record["provenance"]["kind"] == "semantic-index" and record["crs"] == "EPSG:32643"


def test_invalid_imagery_is_excluded_with_reasons(staged, isolated):
    add_scene("SCENE_CLOUDY", write_scene(isolated, "cloudy", (900, 900, 900), cloud_rows=SIZE), "2024-02-01T05:00:00Z")
    add_scene("SCENE_CORRUPT", write_scene(isolated, "corrupt", (900, 900, 900)), "2024-03-01T05:00:00Z", checksum="0" * 64)
    add_scene("SCENE_QUARANTINED", write_scene(isolated, "quarantined", (900, 900, 900)), "2024-04-01T05:00:00Z", quality="quarantined")
    add_scene("SCENE_NO_OFFSET", write_scene(isolated, "nooffset", (900, 900, 900)), "2024-05-01T05:00:00Z", offset=None)
    missing = isolated / "missing.tif"
    write_scene(isolated, "missing", (900, 900, 900))
    add_scene("SCENE_MISSING", missing, "2024-06-01T05:00:00Z")
    missing.unlink()
    # Half-cloudy: only chips with more than 10 % cloud are excluded.
    add_scene("SCENE_HALF", write_scene(isolated, "half", (900, 900, 900), cloud_rows=SIZE // 2), "2024-07-01T05:00:00Z")

    summary = semantic.index(model=staged, log=lambda _: None)
    reasons = {item["scene_id"]: item["reason"] for item in summary["excluded_scenes"]}
    assert "checksum" in reasons["SCENE_CORRUPT"]
    assert "quarantined" in reasons["SCENE_QUARANTINED"]
    assert "offset" in reasons["SCENE_NO_OFFSET"]
    assert "not staged" in reasons["SCENE_MISSING"]
    connection = db.connect()
    rows = {r["scene_id"]: dict(r) for r in connection.execute("SELECT * FROM semantic_scenes")}
    indexed = {r[0] for r in connection.execute("SELECT DISTINCT scene_id FROM semantic_chips")}
    connection.close()
    assert rows["SCENE_CLOUDY"]["status"] == "excluded" and rows["SCENE_CLOUDY"]["chips_indexed"] == 0
    assert rows["SCENE_HALF"]["chips_indexed"] > 0 and rows["SCENE_HALF"]["chips_excluded"] > 0
    assert "cloud" in json.loads(rows["SCENE_HALF"]["exclusion_reasons"]).popitem()[0]
    assert indexed == {"SCENE_RED", "SCENE_GREEN", "SCENE_HALF"}


def test_status_states(staged):
    assert semantic.status()["state"] == "NOT STAGED"  # model present, nothing indexed yet
    job = worker.enqueue("semantic-index", {})
    assert semantic.status()["state"] == "INDEXING"
    connection = db.connect()
    connection.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job,))
    connection.commit()
    connection.close()
    semantic.index(model=staged, log=lambda _: None)
    status = semantic.status()
    assert status["state"] == "READY" and status["scenes_indexed"] == 2 and status["chips_indexed"] == 26
    add_scene("SCENE_NEW", write_scene(semantic.settings.DATA_DIR, "new", (900, 900, 900)), "2025-06-01T05:00:00Z")
    assert semantic.status()["state"] == "PARTIAL"


def test_missing_weights_means_not_staged_and_no_substitute(monkeypatch, isolated):
    config = semantic.model_config()
    config["weights"]["path"] = "data/models/does-not-exist.pt"
    monkeypatch.setattr(semantic, "model_config", lambda: config)
    monkeypatch.setattr(semantic, "_model", None)
    problems = semantic.model_problems()
    assert any("Weights not staged" in p for p in problems)
    assert semantic.status()["state"] == "NOT STAGED"
    with pytest.raises(semantic.ModelNotStaged):
        semantic.search("airport runway")
    with pytest.raises(semantic.ModelNotStaged):
        semantic.index(log=lambda _: None)
    # Through the command search: understood, NOT STAGED, no results, no fallback.
    plan = parser.parse("show satellite images of an airport runway")
    run = engine.run(plan)
    assert run["capability"]["state"] == "NOT STAGED" and run["results"] == []
    assert pilot.capability("semantic-search")["status"] == "NOT STAGED"
    assert next(m for m in pilot.load_models() if m["id"] == "remoteclip")["deployment_status"] == "NOT STAGED"


def test_api_refuses_search_without_model(monkeypatch, isolated):
    from fastapi.testclient import TestClient
    from lumon import main
    monkeypatch.setenv("LUMON_EMBEDDED_WORKER", "0")
    monkeypatch.setattr(semantic, "model_problems", lambda: ["Weights not staged: test"])
    monkeypatch.setattr(semantic, "_model", None)
    with TestClient(main.app) as client:
        assert client.get("/api/semantic/status").json()["state"] == "NOT STAGED"
        response = client.post("/api/semantic/search", json={"text": "airport runway"})
        assert response.status_code == 409 and response.json()["detail"]["state"] == "NOT STAGED"
        assert client.post("/api/semantic/index").status_code == 409


def test_parser_only_takes_explicit_image_requests():
    plan = parser.parse("show satellite images of an airport runway since 2023")
    assert plan["intent"] == "search-imagery" and plan["semantic_text"] == "an airport runway"
    assert plan["time"]["start"] == "2023-01-01T00:00:00Z" and plan["unrecognised"] == []
    assert parser.parse("Find imagery showing mangroves")["semantic_text"] == "mangroves"
    # Unrelated queries are parsed exactly as before.
    assert parser.parse("Show recent earthquakes near Delhi")["intent"] == "find-events"
    assert parser.parse("Find imagery similar to this location")["intent"] == "find-similar"
    pilot_plan = parser.parse("show images of new buildings at NIT Raipur")
    assert pilot_plan["intent"] == "search-imagery" and pilot_plan["study_area"]["id"] == "nit-raipur"
    assert "raipur" not in pilot_plan["semantic_text"]


# ---------------------------------------------------------------------------
# Image-to-image similarity (reuses the cached embeddings; FakeModel above)
# ---------------------------------------------------------------------------

def _chip(result, size, row, col):
    return next(r for r in result if r["id"].endswith(f":{size}:{row}:{col}"))


def test_window_overlap():
    assert semantic.window_overlap(("a", 0, 0, 112), ("a", 0, 0, 112)) == 1.0
    assert semantic.window_overlap(("a", 0, 0, 112), ("a", 0, 56, 112)) == 0.5
    assert semantic.window_overlap(("a", 0, 0, 112), ("a", 112, 112, 112)) == 0.0
    assert semantic.window_overlap(("a", 0, 0, 112), ("b", 0, 0, 112)) == 0.0  # different AOI grids never match
    assert semantic.window_overlap(("a", 0, 0, 224), ("a", 0, 0, 112)) == 1.0  # share of the smaller window


def test_similar_ranks_look_alikes_and_never_repeats_the_example(staged):
    semantic.index(model=staged, log=lambda _: None)
    example = "SCENE_RED:112:0:0"
    found = semantic.similar([example], scope="all", limit=50)
    ids = [r["id"] for r in found["results"]]
    assert example not in ids
    scores = [r["score"] for r in found["results"]]
    assert scores == sorted(scores, reverse=True)
    assert found["results"][0]["scene_id"] == "SCENE_RED" and found["results"][-1]["scene_id"] == "SCENE_GREEN"
    assert {r["chip_px"] for r in found["results"]} == {112}  # only chips of the example's size
    assert found["score_kind"].startswith("cosine similarity of image embeddings")
    assert found["examples"][0]["id"] == example and found["timing_ms"]["model_load"] == 0.0
    # One result per place: each window appears once (its best-matching date).
    windows = [r["id"].split(":", 1)[1] for r in found["results"]]
    assert len(windows) == len(set(windows))


def test_similar_scopes(staged):
    semantic.index(model=staged, log=lambda _: None)
    example = "SCENE_RED:112:0:0"
    elsewhere = semantic.similar([example], scope="other-places", limit=50)["results"]
    assert elsewhere and not any(r["same_place"] for r in elsewhere)
    assert all(semantic.window_overlap(("test-aoi", 0, 0, 112), ("test-aoi", int(r["id"].split(":")[2]), int(r["id"].split(":")[3]), 112))
               <= semantic.SAME_PLACE_OVERLAP for r in elsewhere)
    same = semantic.similar([example], scope="same-place", limit=50)["results"]
    # The same window on the other date only (the example itself is excluded).
    assert [r["id"] for r in same] == ["SCENE_GREEN:112:0:0"] and same[0]["same_place"]
    with pytest.raises(ValueError):
        semantic.similar([example], scope="everywhere")


def test_similar_negatives_push_results_away(staged):
    semantic.index(model=staged, log=lambda _: None)
    # same-place: the only candidate is the green chip at the example's window.
    plain = semantic.similar(["SCENE_RED:112:0:0"], scope="same-place")["results"][0]
    pushed = semantic.similar(["SCENE_RED:112:0:0"], ["SCENE_GREEN:112:128:128"], scope="same-place")["results"][0]
    assert plain["id"] == pushed["id"] == "SCENE_GREEN:112:0:0"
    assert pushed["score"] < plain["score"]  # "not like this" (green) lowers green look-alikes
    assert semantic.similar(["SCENE_RED:112:0:0"], ["SCENE_GREEN:112:128:128"])["negatives"][0]["id"] == "SCENE_GREEN:112:128:128"


def test_similar_keeps_metadata_and_refuses_unknown_chips(staged):
    semantic.index(model=staged, log=lambda _: None)
    item = semantic.similar(["SCENE_RED:224:0:0"], scope="all", limit=5)["results"][0]
    record = semantic.chip_record(item["id"])
    assert item["footprint"] == record["footprint"] and item["acquired_at"] == record["acquired_at"]
    assert item["provenance_id"] == record["provenance_id"]
    with pytest.raises(ValueError):
        semantic.similar(["NOT_A_CHIP:112:0:0"])
    with pytest.raises(ValueError):
        semantic.similar([])


def test_chips_at_picks_the_chip_under_a_point(staged):
    semantic.index(model=staged, log=lambda _: None)
    target = semantic.chip_record("SCENE_RED:112:128:128")
    found = semantic.chips_at(target["lon"], target["lat"])
    assert found[0]["chip_px"] == 112 and found[0]["scene_id"] == "SCENE_GREEN"  # latest scene by default
    assert found[0]["id"].endswith(":112:128:128")
    assert semantic.chips_at(target["lon"], target["lat"], scene_id="SCENE_RED")[0]["id"] == "SCENE_RED:112:128:128"
    assert semantic.chips_at(0.0, 0.0) == []


def test_similar_refuses_without_model_files(monkeypatch, isolated):
    monkeypatch.setattr(semantic, "model_problems", lambda: ["Weights not staged: test"])
    with pytest.raises(semantic.ModelNotStaged):
        semantic.similar(["ANY:112:0:0"])


def test_similar_api(staged, monkeypatch):
    from fastapi.testclient import TestClient
    from lumon import main
    monkeypatch.setenv("LUMON_EMBEDDED_WORKER", "0")
    semantic.index(model=staged, log=lambda _: None)
    target = semantic.chip_record("SCENE_RED:112:0:0")
    with TestClient(main.app) as client:
        body = client.post("/api/semantic/similar", json={"chip_ids": ["SCENE_RED:112:0:0"], "scope": "all"}).json()
        assert body["results"] and body["results"][0]["kind"] == "chip"
        by_point = client.post("/api/semantic/similar", json={"at": {"lon": target["lon"], "lat": target["lat"]}}).json()
        assert by_point["examples"][0]["id"].endswith(":112:0:0")
        assert client.get("/api/semantic/chips/at", params={"lon": target["lon"], "lat": target["lat"]}).json()[0]["chip_px"] == 112
        assert client.get(f"/api/semantic/chips/{target['id']}").json()["id"] == target["id"]  # chip route still works
        assert client.post("/api/semantic/similar", json={"at": {"lon": 0, "lat": 0}}).status_code == 404
        assert client.post("/api/semantic/similar", json={"chip_ids": ["NOPE:1:2:3"]}).status_code == 400
        monkeypatch.setattr(semantic, "model_problems", lambda: ["Weights not staged: test"])
        refused = client.post("/api/semantic/similar", json={"chip_ids": ["SCENE_RED:112:0:0"]})
        assert refused.status_code == 409 and refused.json()["detail"]["state"] == "NOT STAGED"


def test_command_search_similar_to_selected_location(staged):
    semantic.index(model=staged, log=lambda _: None)
    target = semantic.chip_record("SCENE_RED:112:128:128")
    plan = parser.parse("Find imagery similar to this location")
    plan["place"].update(lon=target["lon"], lat=target["lat"])
    run = engine.run(plan)
    assert run["capability"]["state"] == "SUPPORTED" and run["results"]
    assert all(r["kind"] == "chip" and r["evidence_type"] == "MODEL SIMILARITY" for r in run["results"])
    assert run["semantic"]["examples"][0]["id"].endswith(":112:128:128")
    # No selection, or a selection without imagery: refused exactly as before.
    unselected = engine.run(parser.parse("Find imagery similar to this location"))
    assert unselected["capability"]["state"] == "UNSUPPORTED" and not unselected["results"]
    elsewhere = parser.parse("Find imagery similar to this location")
    elsewhere["place"].update(lon=0.0, lat=0.0)
    report = engine.run(elsewhere)["capability"]
    assert any(i.startswith("Similarity search needs example sites") for i in report["issues"])
