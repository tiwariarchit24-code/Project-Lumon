"""
SIH 26227 evaluation harness (lumon/ps_evaluation.py).

Everything here is a SYNTHETIC TEST FIXTURE: scenes, 512-number "embeddings",
a fake text encoder, change candidates and masks are generated so that the
expected metric values are known exactly. They test the harness (manifest
integrity, metric arithmetic, reporting, claim safety), not any model.
"""

import json
import socket

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from lumon import db, ps_evaluation
from lumon.change_ml import learned
from lumon.imagery import discovery, semantic
from lumon.ps_evaluation import EvaluationIntegrityError, NOT_AVAILABLE

KEY = "fake-embedding:1"
DIM = 512
GROUP_AXIS = {"A": 0, "B": 1}
SCENES = [("SCENE_2023", "2023-01-10T05:00:00Z"), ("SCENE_2024", "2024-01-10T05:00:00Z")]


def vector(group, jitter):
    rng = np.random.default_rng(jitter)
    v = np.zeros(DIM, dtype="float32")
    v[GROUP_AXIS[group]] = 1.0
    v += 0.01 * rng.standard_normal(DIM).astype("float32")
    return v / np.linalg.norm(v)


class FakeTextModel:
    """TEST FIXTURE: 'group a' -> direction of group A, else group B."""
    key = KEY
    version = {"model": "fake"}

    def encode_text(self, text):
        return vector("A" if "group a" in text else "B", 999)


def chip(scene, col):
    return f"{scene}:112:0:{col}"


@pytest.fixture
def archive(isolated, monkeypatch):
    """Two scenes x 6 places: places 0-2 look like group A, 3-5 like group B."""
    monkeypatch.setattr(semantic, "model_problems", lambda: [])
    monkeypatch.setattr(semantic, "model_key", lambda: KEY)
    monkeypatch.setattr(ps_evaluation, "_model_identity", lambda config: {"id": config["id"], "name": config["name"],
                        "license": config.get("license"), "weights_path": "test", "checksum_verified": "not checked (test)"})
    connection = db.connect()
    for scene_id, when in SCENES:
        connection.execute("""INSERT INTO scenes (id, aoi_id, sensor, acquired_at, file_sha256, quality_status, provenance_id, created_at)
                              VALUES (?, 'test-aoi', 'sentinel-2a', ?, ?, 'usable', 'prov-test', ?)""", (scene_id, when, f"sha-{scene_id}", db.now_iso()))
        for place in range(6):
            col = place * 112
            fp = {"type": "Polygon", "coordinates": [[[73 + place * .01, 19], [73.01 + place * .01, 19], [73.01 + place * .01, 19.01], [73 + place * .01, 19.01], [73 + place * .01, 19]]]}
            connection.execute(
                """INSERT INTO semantic_chips (id, model_key, preprocess_version, scene_id, aoi_id, acquired_at, chip_px, row_off, col_off,
                       footprint, lon, lat, valid_fraction, embedding, created_at) VALUES (?, ?, ?, ?, 'test-aoi', ?, 112, 0, ?, ?, ?, 19.005, 1.0, ?, ?)""",
                (chip(scene_id, col), KEY, semantic.PREPROCESS_VERSION, scene_id, when, col, json.dumps(fp), 73.005 + place * .01,
                 vector("A" if place < 3 else "B", place * 10 + len(scene_id)).tobytes(), db.now_iso()))
    connection.commit()
    connection.close()
    return isolated


def make_manifest(folder, **changes):
    pending = {"status": "pending", "source": None, "exhaustive": False, "judgements": {}}
    manifest = {
        "manifest_id": "test-eval", "manifest_version": "1.0.0", "created_at": "2026-10-05", "created_by": "test",
        "creation_method": "synthetic fixture", "ground_truth_source": "synthetic (test)",
        "dataset": {"aoi_id": "test-aoi", "sensor": "test", "source": "synthetic", "date_range": ["2020-01-01", "2026-12-31"], "area_km2": 1.0,
                    "scenes": [{"id": s, "acquired_at": w, "file_sha256": f"sha-{s}"} for s, w in SCENES]},
        "splits": {"development": {"status": "USED", "note": "test"}, "evaluation": {"status": "INTERNAL", "note": "test"},
                   "held-out": {"status": "PENDING", "note": "none in tests"}},
        "semantic": {"k_values": [2, 4], "queries": [{"id": "q-a", "text": "group a", "split": "evaluation", "k_values": [2, 4],
                                                      "pool_depth": 6, "relevance": dict(pending)}]},
        "similarity": {"references": [{"id": "s-a", "chip_id": chip("SCENE_2023", 0), "scope": "all", "split": "evaluation",
                                       "k_values": [2], "pool_depth": 6, "relevance": dict(pending)}]},
        "rule_change": {"labels": []},
        "btc_b": {"pairs": []},
        "discovery": {},
        "content_sha256": None,
    }
    for key, value in changes.items():
        manifest[key] = value
    manifest["content_sha256"] = ps_evaluation.canonical_sha(manifest)
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, manifest


def write_manifest(path, manifest, seal=True):
    if seal:
        manifest["content_sha256"] = ps_evaluation.canonical_sha(manifest)
    path.write_text(json.dumps(manifest))
    return path


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_ranking_metrics():
    ranked = ["a", "x", "b", "y", "c"]
    relevant = {"a", "b", "c", "d"}
    assert ps_evaluation.precision_at_k(ranked, relevant, 2) == 0.5
    assert ps_evaluation.precision_at_k(ranked, relevant, 5) == 0.6
    assert ps_evaluation.recall_at_k(ranked, relevant, 5) == 0.75
    assert ps_evaluation.recall_at_k(ranked, set(), 5) is None
    assert ps_evaluation.average_precision(ranked, relevant, 4) == pytest.approx((1 + 2 / 3 + 3 / 5) / 4)
    assert ps_evaluation.average_precision(ranked, relevant) == pytest.approx((1 + 2 / 3 + 3 / 5) / 3)  # judged depth
    assert ps_evaluation.average_precision(["x"], {"a"}) is None


def test_change_metrics():
    assert ps_evaluation.binary_metrics(3, 1, 2) == {"tp": 3, "fp": 1, "fn": 2, "precision": 0.75, "recall": 0.6,
                                                     "f1": pytest.approx(2 * 0.75 * 0.6 / 1.35)}
    assert ps_evaluation.binary_metrics(0, 0, 0)["precision"] is None
    predicted = np.array([[1, 1, 0, 0]], dtype=bool)
    truth = np.array([[1, 0, 1, 0]], dtype=bool)
    valid = np.array([[True, True, True, False]])
    m = ps_evaluation.mask_metrics(predicted, truth, valid)
    assert (m["tp"], m["fp"], m["fn"], m["iou"], m["valid_pixels"]) == (1, 1, 1, pytest.approx(1 / 3), 3)


# ---------------------------------------------------------------------------
# Manifest integrity
# ---------------------------------------------------------------------------

def test_valid_manifest_loads(archive):
    path, manifest = make_manifest(archive)
    connection = db.connect()
    assert ps_evaluation.validate_manifest(json.loads(path.read_text()), connection, KEY, semantic.PREPROCESS_VERSION) == []
    connection.close()


def _problems(manifest):
    connection = db.connect()
    problems = ps_evaluation.validate_manifest(manifest, connection, KEY, semantic.PREPROCESS_VERSION)
    connection.close()
    return " | ".join(problems)


@pytest.mark.parametrize("breaker, expected", [
    (lambda m: m.pop("ground_truth_source"), "missing manifest field"),
    (lambda m: m["dataset"]["scenes"].append({"id": "NOPE", "file_sha256": "x"}), "not in the archive"),
    (lambda m: m["dataset"]["scenes"][0].update(file_sha256="different"), "checksum differs"),
    (lambda m: m["semantic"]["queries"][0]["relevance"].update(status="verified", judgements={"NOT_A_CHIP": 1}, source="x"), "not indexed"),
    (lambda m: m["semantic"]["queries"].append(dict(m["semantic"]["queries"][0])), "duplicate item id"),
    (lambda m: m["semantic"]["queries"][0].update(split="training"), "split must be"),
    (lambda m: m["semantic"]["queries"][0]["relevance"].update(status="maybe"), "label status must be"),
    (lambda m: m["semantic"]["queries"][0]["relevance"].update(status="verified", judgements={chip("SCENE_2023", 0): 1}), "without a source"),
    (lambda m: m["splits"]["held-out"].update(status="DONE"), "held-out.status"),
])
def test_invalid_manifests_are_refused(archive, breaker, expected):
    _, manifest = make_manifest(archive)
    breaker(manifest)
    manifest["content_sha256"] = ps_evaluation.canonical_sha(manifest)
    assert expected in _problems(manifest)


def test_missing_provenance_is_refused(archive):
    connection = db.connect()
    connection.execute("UPDATE scenes SET provenance_id = NULL WHERE id = 'SCENE_2023'")
    connection.commit()
    connection.close()
    _, manifest = make_manifest(archive)
    assert "no provenance record" in _problems(manifest)


def test_leakage_and_duplicates_are_refused(archive):
    _, manifest = make_manifest(archive)
    shared = chip("SCENE_2024", 112)
    manifest["semantic"]["queries"][0]["relevance"] = {"status": "verified", "source": "t", "exhaustive": False, "judgements": {shared: 1}}
    manifest["similarity"]["references"][0].update(split="development",
                                                   relevance={"status": "verified", "source": "t", "exhaustive": False, "judgements": {shared: 0}})
    pair = {"before_scene_id": "SCENE_2023", "after_scene_id": "SCENE_2024"}
    manifest["btc_b"]["pairs"] = [{"id": "b1", "split": "evaluation", **pair, "label": {"status": "pending"}},
                                  {"id": "b2", "split": "held-out", **pair, "label": {"status": "pending"}}]
    problems = _problems(manifest)
    assert f"leakage: chip {shared}" in problems
    assert "leakage: scene pair SCENE_2023 -> SCENE_2024" in problems and "duplicate btc-b entry" in problems


def test_edits_need_a_new_seal_and_a_new_version(archive):
    path, manifest = make_manifest(archive)
    manifest["semantic"]["queries"][0]["text"] = "group a buildings"
    write_manifest(path, manifest, seal=False)
    with pytest.raises(EvaluationIntegrityError, match="not re-sealed"):
        ps_evaluation.run(str(path), model=FakeTextModel())
    write_manifest(path, manifest)  # sealed, same version
    ps_evaluation.run(str(path), model=FakeTextModel())  # first report for this version
    manifest["semantic"]["queries"][0]["text"] = "group a fields"
    write_manifest(path, manifest)  # sealed again but same version: refused
    with pytest.raises(EvaluationIntegrityError, match="bump manifest_version"):
        ps_evaluation.run(str(path), model=FakeTextModel())
    manifest["manifest_version"] = "1.0.1"
    write_manifest(path, manifest)
    assert ps_evaluation.run(str(path), model=FakeTextModel())["manifest"]["manifest_version"] == "1.0.1"


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def test_pending_labels_give_no_numbers_but_a_judgement_pool(archive):
    path, _ = make_manifest(archive)
    report = ps_evaluation.run(str(path), model=FakeTextModel())
    query = report["semantic_retrieval"]["queries"][0]
    assert report["semantic_retrieval"]["status"] == ps_evaluation.LABELS_INSUFFICIENT
    assert query["metrics"]["status"] == NOT_AVAILABLE and "precision@2" not in query["metrics"]
    assert len(query["judgement_pool"]) == 6 and query["top_results"][0]["chip_id"].endswith((":0", ":112", ":224"))
    assert report["rule_based_change"]["metrics"]["status"] == NOT_AVAILABLE
    assert any(item.startswith("semantic 'group a'") for item in report["not_available"])
    assert any(item.startswith("held-out evaluation: PENDING") for item in report["not_available"])


def test_verified_relevance_labels_give_ranking_metrics(archive):
    path, manifest = make_manifest(archive)
    relevant = [chip(s, c) for s, _ in SCENES for c in (0, 112, 224)]  # every group-A chip
    judgements = {c: 1 for c in relevant} | {chip("SCENE_2023", 336): 0}
    manifest["semantic"]["queries"][0]["relevance"] = {"status": "verified", "source": "synthetic fixture", "exhaustive": True,
                                                      "judgements": judgements}
    write_manifest(path, manifest)
    metrics = ps_evaluation.run(str(path), model=FakeTextModel())["semantic_retrieval"]["queries"][0]["metrics"]
    assert metrics["status"] == "MEASURED" and metrics["precision@2"] == 1.0 and metrics["precision@4"] == 1.0
    assert metrics["recall@4"] == round(4 / 6, 4) and metrics["average_precision"] == 1.0 and metrics["relevant"] == 6
    manifest["semantic"]["queries"][0]["relevance"]["exhaustive"] = False
    manifest["manifest_version"] = "1.0.1"
    write_manifest(path, manifest)
    metrics = ps_evaluation.run(str(path), model=FakeTextModel())["semantic_retrieval"]["queries"][0]["metrics"]
    assert str(metrics["recall@4"]).startswith(NOT_AVAILABLE) and metrics["average_precision_kind"].startswith("judged-depth")


def test_rule_change_metrics_and_separate_suppression(archive):
    connection = db.connect()
    gates = json.dumps([{"gate": "5 size/shape", "passed": False}, {"gate": "class note", "passed": True}])
    for cid, status, tiles in (("c-hit", "accepted", ["t:0:0"]), ("c-false", "accepted", ["t:5:5"]), ("c-supp", "suppressed", ["t:9:9"])):
        connection.execute("""INSERT INTO change_candidates (id, aoi_id, change_class, direction, tile_ids, geometry, status, last_clean_before,
                              earliest_supported_after, gate_results, created_at) VALUES (?, 'test-aoi', 'construction', 'appearance', ?, '{}', ?,
                              '2023-02-01', '2023-12-01', ?, ?)""", (cid, json.dumps(tiles), status, gates, db.now_iso()))
    connection.commit()
    connection.close()
    path, manifest = make_manifest(archive)
    base = {"split": "evaluation", "aoi_id": "test-aoi", "before_scene_id": "SCENE_2023", "after_scene_id": "SCENE_2024",
            "source": "synthetic fixture", "status": "verified"}
    manifest["rule_change"]["labels"] = [
        {"id": "l-change-found", "tile_ids": ["t:0:0"], "change": True, "change_class": "construction", "known_change_date": "2023-11-01", **base},
        {"id": "l-change-missed", "tile_ids": ["t:3:3"], "change": True, **base},
        {"id": "l-no-change-flagged", "tile_ids": ["t:5:5"], "change": False, **base},
        {"id": "l-no-change-quiet", "tile_ids": ["t:7:7"], "change": False, **base},
    ]
    write_manifest(path, manifest)
    section = ps_evaluation.run(str(path), model=FakeTextModel())["rule_based_change"]
    m = section["metrics"]
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (1, 1, 1, 1) and m["precision"] == 0.5 and m["recall"] == 0.5
    assert m["class_agreement"] == "1/1" and m["earliest_date_error_days"]["median"] == 30.0
    suppression = section["suppression_statistics"]
    assert suppression["kind"] == "SUPPRESSION STATISTICS — NOT PRECISION" and suppression["failed_gate_counts"] == {"5 size/shape": 1}
    assert "precision" not in suppression


def test_btc_b_archive_metrics_from_verified_masks_only(archive, monkeypatch):
    monkeypatch.setattr(learned, "model_key", lambda: "fake-btc:1")
    profile = {"driver": "GTiff", "width": 4, "height": 1, "count": 1, "dtype": "uint8", "crs": "EPSG:32643",
               "transform": from_origin(300000, 2110000, 10, 10), "nodata": 255}
    with rasterio.open(archive / "pred.tif", "w", **profile) as f:
        f.write(np.array([[[1, 1, 0, 255]]], dtype="uint8"))
    with rasterio.open(archive / "truth.tif", "w", **profile) as f:
        f.write(np.array([[[1, 0, 1, 0]]], dtype="uint8"))
    connection = db.connect()
    connection.executescript(learned.SCHEMA)
    connection.execute("""INSERT INTO ml_change_runs (id, model_key, preprocess_version, aoi_id, before_scene_id, after_scene_id, before_date, after_date,
                          checks, parameters, mask_path, candidate_pixels, clear_pixels, regions_reported, regions_too_small, seconds, created_at)
                          VALUES ('run1', 'fake-btc:1', 'v', 'test-aoi', 'SCENE_2023', 'SCENE_2024', '2023', '2024', ?, '{}', ?, 2, 3, 1, 0, 1.5, ?)""",
                       (json.dumps({"shift_px": 0.1, "joint_clear": 0.75, "month_gap": 0, "warnings": []}), str(archive / "pred.tif"), db.now_iso()))
    connection.commit()
    connection.close()
    import hashlib
    path, manifest = make_manifest(archive)
    manifest["btc_b"]["pairs"] = [{"id": "b1", "split": "evaluation", "before_scene_id": "SCENE_2023", "after_scene_id": "SCENE_2024",
                                   "label": {"status": "verified", "source": "synthetic fixture", "mask_path": str(archive / "truth.tif"),
                                             "mask_sha256": hashlib.sha256((archive / "truth.tif").read_bytes()).hexdigest()}}]
    write_manifest(path, manifest)
    section = ps_evaluation.run(str(path), model=FakeTextModel())["btc_b"]
    pair = section["archive_pairs"][0]
    assert pair["kind"] == "MODEL-GENERATED CANDIDATES — NOT CONFIRMED CHANGE"
    assert (pair["metrics"]["tp"], pair["metrics"]["fp"], pair["metrics"]["fn"], pair["metrics"]["valid_pixels"]) == (1, 1, 1, 3)
    assert section["oscd_benchmark"]["kind"] == "OSCD BENCHMARK EVALUATION — NOT LUMON ARCHIVE"
    manifest["btc_b"]["pairs"][0]["label"]["mask_sha256"] = "tampered"
    manifest["manifest_version"] = "1.0.1"
    write_manifest(path, manifest)
    with pytest.raises(EvaluationIntegrityError, match="checksum differs"):
        ps_evaluation.run(str(path), model=FakeTextModel())


def test_discovery_section_is_structural_with_incremental_check(archive):
    discovery.build(log=lambda _: None)
    path, _ = make_manifest(archive, discovery={"incremental_check": {"holdout_from": "2024-01-01"}})
    section = ps_evaluation.run(str(path), model=FakeTextModel())["discovery"]
    assert section["kind"].startswith("STRUCTURAL CLUSTER QUALITY — NOT SEMANTIC")
    assert section["semantic_correctness"]["status"] == NOT_AVAILABLE
    inc = section["incremental"]
    assert inc["new_chips"] == 6 and inc["assigned"] + inc["unassigned"] == 6 and "temporary copy" in inc["note"]
    assert discovery.get_version()["n_embeddings"] == 12  # the real index was not modified


def test_report_files_provenance_and_claim_safety(archive):
    path, _ = make_manifest(archive)
    report = ps_evaluation.run(str(path), model=FakeTextModel())
    json_file, md_file = report["evaluation"]["report_files"]
    stored = json.loads(open(json_file).read())
    markdown = open(md_file).read()
    assert stored["manifest"]["content_sha256"] == report["manifest"]["content_sha256"]
    assert ps_evaluation.to_markdown(stored) == markdown  # Markdown is generated from the JSON
    assert report["manifest"]["content_sha256"] in markdown and "Claims this report does NOT support" in markdown
    connection = db.connect()
    record = dict(connection.execute("SELECT * FROM provenance WHERE id = ?", (report["evaluation"]["provenance_id"],)).fetchone())
    assert connection.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'ps-evaluation'").fetchone()[0] == 1
    connection.close()
    assert record["kind"] == "ps-evaluation" and record["input_sha256"] == report["manifest"]["content_sha256"]
    text = json.dumps(stored).lower()
    assert '"accuracy"' not in text and "confidence\":" not in text
    assert "not a probability" in report["semantic_retrieval"]["score_kind"]
    assert report["rule_based_change"]["suppression_statistics"]["kind"].endswith("NOT PRECISION")
    assert len(report["do_not_claim"]) >= 7 and report["held_out"]["status"] == "PENDING"


def test_index_must_not_change_during_evaluation(archive, monkeypatch):
    path, _ = make_manifest(archive)
    original = ps_evaluation.evaluate_similarity

    def tampering(manifest):
        connection = db.connect()
        connection.execute("DELETE FROM semantic_chips WHERE id = ?", (chip("SCENE_2024", 560),))
        connection.commit()
        connection.close()
        return original(manifest)
    monkeypatch.setattr(ps_evaluation, "evaluate_similarity", tampering)
    with pytest.raises(EvaluationIntegrityError, match="changed during evaluation"):
        ps_evaluation.run(str(path), model=FakeTextModel())


def test_offline_with_sockets_blocked(archive, monkeypatch):
    monkeypatch.setenv("LUMON_MODE", "airgapped")

    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted during evaluation")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    path, _ = make_manifest(archive)
    report = ps_evaluation.run(str(path), model=FakeTextModel())
    assert report["evaluation"]["mode"] == "airgapped"


def test_cli(archive, capsys):
    from lumon import cli
    path, manifest = make_manifest(archive)
    manifest["created_by"] = "edited"
    write_manifest(path, manifest, seal=False)
    cli.cmd_evaluate(["--manifest", str(path), "--seal"])
    assert "sealed" in capsys.readouterr().out
    assert json.loads(path.read_text())["content_sha256"] == ps_evaluation.canonical_sha(json.loads(path.read_text()))
