"""
SIH 26227 §2.3 evaluation harness: one manifest in, one reproducible report out.

    npm run lumon -- evaluate --manifest config/evaluation/ps26227/<manifest>.json
    npm run lumon -- evaluate --manifest <manifest> --seal     # after editing labels

WHAT IT DOES
  1. loads a versioned, sealed MANIFEST (dataset, splits, queries, labels,
     change pairs, ground-truth source) and checks its integrity — it FAILS
     rather than produce misleading numbers (see validate_manifest)
  2. runs each existing capability READ-ONLY on the manifest's data:
       semantic retrieval (RemoteCLIP)   image-to-image similarity
       rule-based change (gates kept separate from precision)
       BTC-B (OSCD benchmark kept separate from the archive)
       discovery (structural only)
  3. computes a metric ONLY from labels whose status is "verified"
     (human-established). Anything else is reported NOT AVAILABLE with the
     reason; pending queries get a judgement pool for analysts to label.
  4. writes JSON (machine-readable) and Markdown (generated from the JSON),
     a provenance record and an audit entry.

WHAT IT NEVER DOES
  invent labels, turn visual inspection into numbers, call a similarity
  score a probability, call suppression a precision, apply OSCD numbers to
  the archive, treat clusters as classes or BTC-B candidates as confirmed
  change, download anything, or modify the indexes (checked before/after).
"""

import hashlib
import importlib.metadata
import json
import platform
import shutil
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path

import numpy as np

from . import audit, db, provenance, settings

HARNESS_VERSION = "ps26227-eval-v1"
SPLITS = ("development", "evaluation", "held-out")
LABEL_STATES = ("verified", "pending")
NOT_AVAILABLE = "NOT AVAILABLE"
LABELS_INSUFFICIENT = "REAL-DATA EVALUATION EXECUTED — LABELS INSUFFICIENT FOR METRIC"
REQUIRED_KEYS = ["manifest_id", "manifest_version", "created_at", "created_by", "creation_method", "ground_truth_source",
                 "dataset", "splits", "semantic", "similarity", "rule_change", "btc_b", "discovery", "content_sha256"]
DO_NOT_CLAIM = [
    "A RemoteCLIP or image-similarity score is a cosine similarity, not a probability or a confidence.",
    "Suppression statistics of the rule-based gates are not precision.",
    "OSCD benchmark metrics describe BTC-B on the OSCD test split, not on Lumon's archive.",
    "Visual inspection is not accuracy; only metrics computed from verified labels are measurements.",
    "Cluster membership is an embedding grouping, not a semantic class.",
    "BTC-B candidate pixels/regions are model-generated candidates, not confirmed changes.",
    "Unlabelled archive results are not ground truth.",
    "The evaluation split below is INTERNAL and NOT held-out unless the held-out section says MEASURED.",
]


class EvaluationIntegrityError(ValueError):
    """The manifest or data cannot support an honest evaluation (problems listed)."""

    def __init__(self, problems: list[str]):
        super().__init__("EVALUATION REFUSED:\n  - " + "\n  - ".join(problems))
        self.problems = problems


# ---------------------------------------------------------------------------
# Metrics (pure functions)
# ---------------------------------------------------------------------------

def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Share of the top k results that are labelled relevant (unjudged count as not relevant)."""
    top = ranked[:k]
    return sum(1 for r in top if r in relevant) / k if k else 0.0


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float | None:
    """Share of ALL relevant items found in the top k (needs an exhaustive relevant set)."""
    return sum(1 for r in ranked[:k] if r in relevant) / len(relevant) if relevant else None


def average_precision(ranked: list[str], relevant: set[str], total_relevant: int | None = None) -> float | None:
    """
    Mean of precision at each rank where a relevant item appears. Divided
    by total_relevant when the relevant set is exhaustive, otherwise by the
    relevant items retrieved (reported as "judged-depth AP").
    """
    hits, precisions = 0, []
    for rank, item in enumerate(ranked, start=1):
        if item in relevant:
            hits += 1
            precisions.append(hits / rank)
    denominator = total_relevant if total_relevant else hits
    return sum(precisions) / denominator if denominator else None


def binary_metrics(tp: int, fp: int, fn: int) -> dict:
    """Precision, recall and F1 from counts (None where undefined)."""
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else (0.0 if precision == 0 or recall == 0 else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def mask_metrics(predicted: np.ndarray, truth: np.ndarray, valid: np.ndarray) -> dict:
    """Pixel precision/recall/F1/IoU over pixels valid in both masks."""
    p, t = predicted.astype(bool) & valid, truth.astype(bool) & valid
    tp, fp, fn = int((p & t).sum()), int((p & ~t).sum()), int((~p & t).sum())
    result = binary_metrics(tp, fp, fn)
    result["iou"] = tp / (tp + fp + fn) if tp + fp + fn else None
    result["valid_pixels"] = int(valid.sum())
    return result


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def canonical_sha(manifest: dict) -> str:
    """SHA-256 of the manifest content without its own content_sha256 field."""
    body = {k: v for k, v in manifest.items() if k != "content_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(path: str) -> str:
    """Write the content checksum into a manifest after an intentional edit (bump manifest_version too)."""
    file = Path(path)
    manifest = json.loads(file.read_text())
    manifest["content_sha256"] = canonical_sha(manifest)
    file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest["content_sha256"]


def report_folder(manifest: dict) -> Path:
    return settings.DATA_DIR / "evaluation" / "ps26227" / manifest["manifest_id"] / f"v{manifest['manifest_version']}"


def _label_items(manifest: dict) -> list[tuple[str, dict]]:
    """Every labelled item with its kind: queries, similarity references, change labels, BTC-B pairs."""
    items = [("semantic", q) for q in manifest["semantic"].get("queries", [])]
    items += [("similarity", r) for r in manifest["similarity"].get("references", [])]
    items += [("rule-change", label) for label in manifest["rule_change"].get("labels", [])]
    items += [("btc-b", pair) for pair in manifest["btc_b"].get("pairs", [])]
    return items


def validate_manifest(manifest: dict, connection, semantic_key: str, preprocess_version: str) -> list[str]:
    """
    Problems that make an honest evaluation impossible (empty list = valid):
    missing fields, edited-but-unsealed content, a changed manifest under an
    already-reported version, unknown scenes/chips, scenes whose file
    checksum or provenance is missing/different, items without a split or
    label status, duplicate ids or scene pairs, the same chip or scene pair
    in two splits (leakage), verified labels without a source.
    """
    problems = [f"missing manifest field '{k}'" for k in REQUIRED_KEYS if k not in manifest]
    if problems:
        return problems
    sha = canonical_sha(manifest)
    if manifest["content_sha256"] != sha:
        problems.append("manifest content changed but was not re-sealed (content_sha256 mismatch): bump manifest_version and run --seal")
    folder = report_folder(manifest)
    if folder.exists():
        for previous in folder.glob("*.json"):
            try:
                old = json.loads(previous.read_text())
            except json.JSONDecodeError:
                continue
            if old.get("manifest", {}).get("content_sha256") not in (None, sha):
                problems.append(f"manifest {manifest['manifest_id']} v{manifest['manifest_version']} was already reported with "
                                f"different content ({previous.name}): bump manifest_version")
                break
    # Dataset: every scene resolves, with the recorded checksum and provenance.
    dataset = manifest["dataset"]
    for scene in dataset.get("scenes", []):
        row = connection.execute("SELECT aoi_id, file_sha256, provenance_id, acquired_at FROM scenes WHERE id = ?", (scene["id"],)).fetchone()
        if row is None:
            problems.append(f"scene {scene['id']} is not in the archive")
        elif row["file_sha256"] != scene.get("file_sha256"):
            problems.append(f"scene {scene['id']}: file checksum differs from the manifest")
        elif not row["provenance_id"]:
            problems.append(f"scene {scene['id']}: no provenance record")
        elif row["aoi_id"] != dataset.get("aoi_id"):
            problems.append(f"scene {scene['id']} belongs to AOI {row['aoi_id']}, not {dataset.get('aoi_id')}")
    scene_ids = {s["id"] for s in dataset.get("scenes", [])}
    # Label items.
    seen_ids, split_of_chip, split_of_pair, pairs_by_kind = set(), {}, {}, Counter()
    known_chips = {r[0] for r in connection.execute(
        "SELECT id FROM semantic_chips WHERE model_key = ? AND preprocess_version = ?", (semantic_key, preprocess_version))}
    for kind, item in _label_items(manifest):
        name = f"{kind} item {item.get('id', '?')}"
        if not item.get("id"):
            problems.append(f"{kind} item without an id")
        elif item["id"] in seen_ids:
            problems.append(f"duplicate item id {item['id']}")
        seen_ids.add(item.get("id"))
        if item.get("split") not in SPLITS:
            problems.append(f"{name}: split must be one of {', '.join(SPLITS)}")
        label = item.get("relevance") if kind in ("semantic", "similarity") else item.get("label") if kind == "btc-b" else item
        status = (label or {}).get("status")
        if status not in LABEL_STATES:
            problems.append(f"{name}: label status must be one of {', '.join(LABEL_STATES)}")
        if status == "verified" and not (label or {}).get("source"):
            problems.append(f"{name}: verified label without a source (who established it, how)")
        chips = []
        if kind in ("semantic", "similarity"):
            chips = list((label or {}).get("judgements", {}))
            if kind == "similarity":
                chips.append(item.get("chip_id"))
        for chip in chips:
            if chip not in known_chips:
                problems.append(f"{name}: chip {chip} is not indexed with the current embedding version")
            other = split_of_chip.setdefault(chip, item.get("split"))
            if other != item.get("split"):
                problems.append(f"leakage: chip {chip} is labelled in both '{other}' and '{item.get('split')}'")
        if kind in ("rule-change", "btc-b"):
            pair = (item.get("before_scene_id"), item.get("after_scene_id"))
            for scene in pair:
                if scene not in scene_ids:
                    problems.append(f"{name}: scene {scene} is not in the manifest dataset")
            key = (kind, pair, tuple(sorted(item.get("tile_ids", [])))) if kind == "rule-change" else (kind, pair)
            pairs_by_kind[key] += 1
            other = split_of_pair.setdefault(pair, item.get("split"))
            if other != item.get("split"):
                problems.append(f"leakage: scene pair {pair[0]} -> {pair[1]} is in both '{other}' and '{item.get('split')}'")
        if kind == "rule-change" and item.get("status") == "verified":
            for field in ("tile_ids", "change", "before_scene_id", "after_scene_id"):
                if item.get(field) in (None, [], ""):
                    problems.append(f"{name}: verified change label without {field}")
    for key, count in pairs_by_kind.items():
        if count > 1:
            problems.append(f"duplicate {key[0]} entry for scene pair {key[1][0]} -> {key[1][1]}")
    held_out = manifest["splits"].get("held-out", {})
    if held_out.get("status") not in ("PENDING", "AVAILABLE"):
        problems.append("splits.held-out.status must be PENDING or AVAILABLE")
    return problems


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _not_available(reason: str) -> dict:
    return {"status": NOT_AVAILABLE, "reason": reason}


def _judged(label: dict) -> tuple[bool, set[str], dict]:
    verified = label.get("status") == "verified" and bool(label.get("judgements"))
    judgements = label.get("judgements", {}) if verified else {}
    return verified, {c for c, v in judgements.items() if v}, judgements


def _ranking_metrics(ranked: list[str], label: dict, k_values: list[int]) -> dict:
    """Metrics for one ranked list from its verified relevance label, else NOT AVAILABLE."""
    verified, relevant, judgements = _judged(label)
    if not verified:
        return {"status": NOT_AVAILABLE, "reason": "no verified relevance judgements for this item (label status: "
                f"{label.get('status')})"}
    exhaustive = bool(label.get("exhaustive"))
    metrics = {"status": "MEASURED", "label_source": label.get("source"), "judged": len(judgements), "relevant": len(relevant),
               "exhaustive_relevant_set": exhaustive,
               "unjudged_in_top_k": {f"@{k}": sum(1 for r in ranked[:k] if r not in judgements) for k in k_values}}
    for k in k_values:
        metrics[f"precision@{k}"] = round(precision_at_k(ranked, relevant, k), 4)
        metrics[f"recall@{k}"] = round(recall_at_k(ranked, relevant, k), 4) if exhaustive and relevant else \
            f"{NOT_AVAILABLE}: relevant set not declared exhaustive"
    ap = average_precision(ranked, relevant, len(relevant) if exhaustive else None)
    metrics["average_precision"] = None if ap is None else round(ap, 4)
    metrics["average_precision_kind"] = "AP over the exhaustive relevant set" if exhaustive else "judged-depth AP (relevant set not exhaustive)"
    return metrics


def evaluate_semantic(manifest: dict, model=None) -> dict:
    from .imagery import semantic
    section = manifest["semantic"]
    dataset = manifest["dataset"]
    results, measured = [], 0
    for query in section.get("queries", []):
        k_values = query.get("k_values", section.get("k_values", [5, 10]))
        depth = max(query.get("pool_depth", 20), max(k_values))
        found = semantic.search(query["text"], limit=depth, start=dataset["date_range"][0], end=dataset["date_range"][1],
                                aoi_id=dataset["aoi_id"], model=model)
        ranked = [r["id"] for r in found["results"]]
        metrics = _ranking_metrics(ranked, query.get("relevance", {}), k_values)
        measured += metrics["status"] == "MEASURED"
        results.append({
            "id": query["id"], "text": query["text"], "split": query["split"], "capability_note": query.get("capability_note"),
            "top_results": [{"rank": r["rank"], "chip_id": r["id"], "score": r["score"], "acquired_at": r["acquired_at"]} for r in found["results"][:10]],
            "score_kind": found["score_kind"], "chips_searched": found["chips_searched"],
            "latency_ms": {"text_encode": found["timing_ms"]["text_encode"], "rank": found["timing_ms"]["rank"]},
            "metrics": metrics,
            "judgement_pool": None if metrics["status"] == "MEASURED" else ranked[:depth],
        })
    latencies = [r["latency_ms"]["text_encode"] + r["latency_ms"]["rank"] for r in results]
    return {"capability": "RemoteCLIP text-to-image retrieval", "queries": results, "queries_measured": measured,
            "queries_total": len(results), "status": "MEASURED" if measured else LABELS_INSUFFICIENT,
            "latency_ms_median": round(float(np.median(latencies)), 1) if latencies else None,
            "score_kind": "cosine similarity (not a probability)"}


def evaluate_similarity(manifest: dict) -> dict:
    from .imagery import semantic
    dataset = manifest["dataset"]
    results, measured = [], 0
    for reference in manifest["similarity"].get("references", []):
        k_values = reference.get("k_values", [5, 10])
        found = semantic.similar([reference["chip_id"]], scope=reference.get("scope", "other-places"),
                                 limit=max(reference.get("pool_depth", 20), max(k_values)),
                                 start=dataset["date_range"][0], end=dataset["date_range"][1])
        ranked = [r["id"] for r in found["results"]]
        metrics = _ranking_metrics(ranked, reference.get("relevance", {}), k_values)
        measured += metrics["status"] == "MEASURED"
        results.append({"id": reference["id"], "chip_id": reference["chip_id"], "scope": reference.get("scope", "other-places"),
                        "split": reference["split"],
                        "top_results": [{"rank": r["rank"], "chip_id": r["id"], "score": r["score"]} for r in found["results"][:10]],
                        "chips_compared": found["chips_searched"], "latency_ms": found["timing_ms"]["total"], "metrics": metrics,
                        "judgement_pool": None if metrics["status"] == "MEASURED" else ranked})
    return {"capability": "RemoteCLIP image-to-image similarity", "references": results, "references_measured": measured,
            "status": "MEASURED" if measured else LABELS_INSUFFICIENT, "score_kind": "cosine similarity of image embeddings (not a probability)"}


def evaluate_rule_change(manifest: dict, connection) -> dict:
    """Site-level metrics from verified change/no-change labels; gate statistics reported separately."""
    aoi_id = manifest["dataset"]["aoi_id"]
    candidates = [dict(r) for r in connection.execute("SELECT * FROM change_candidates WHERE aoi_id = ?", (aoi_id,))]
    gates = Counter()
    for c in candidates:
        if c["status"] == "suppressed":
            for gate in json.loads(c["gate_results"] or "[]"):
                if not gate.get("passed") and gate.get("gate") != "class note":
                    gates[gate["gate"]] += 1
    suppression = {"kind": "SUPPRESSION STATISTICS — NOT PRECISION", "candidates": len(candidates),
                   "accepted": sum(c["status"] == "accepted" for c in candidates),
                   "suppressed": sum(c["status"] == "suppressed" for c in candidates),
                   "failed_gate_counts": dict(sorted(gates.items())),
                   "note": "How many candidates the gates removed and why. Says nothing about how many were real changes."}
    labels = [l for l in manifest["rule_change"].get("labels", []) if l.get("status") == "verified"]
    accepted = [c for c in candidates if c["status"] == "accepted"]
    if not labels:
        return {"capability": "rule-based change engine", "status": LABELS_INSUFFICIENT,
                "metrics": _not_available(f"no verified change/no-change labels ({len(manifest['rule_change'].get('labels', []))} items, none verified)"),
                "suppression_statistics": suppression}
    tp = fp = fn = tn = 0
    class_agree, class_total, date_errors, per_label = 0, 0, [], []
    scenes = {r["id"]: r["acquired_at"][:10] for r in connection.execute("SELECT id, acquired_at FROM scenes")}
    for label in labels:
        before, after = scenes[label["before_scene_id"]], scenes[label["after_scene_id"]]
        hits = [c for c in accepted if set(json.loads(c["tile_ids"])) & set(label["tile_ids"])
                and (c["last_clean_before"] or "") <= after and (c["earliest_supported_after"] or "9999") >= before]
        if label["change"]:
            tp, fn = tp + bool(hits), fn + (not hits)
            if hits and label.get("change_class"):
                class_total += 1
                class_agree += any(c["change_class"] == label["change_class"] for c in hits)
            if hits and label.get("known_change_date"):
                date_errors.append(min(abs((date.fromisoformat(c["earliest_supported_after"]) - date.fromisoformat(label["known_change_date"])).days)
                                       for c in hits if c["earliest_supported_after"]))
        else:
            fp, tn = fp + bool(hits), tn + (not hits)
        per_label.append({"id": label["id"], "split": label["split"], "change": label["change"], "detected": bool(hits),
                          "matched_candidates": [c["id"] for c in hits]})
    metrics = {"status": "MEASURED", "unit": "labelled site (tile set) per scene pair", **binary_metrics(tp, fp, fn), "tn": tn,
               "class_agreement": f"{class_agree}/{class_total}" if class_total else NOT_AVAILABLE,
               "earliest_date_error_days": {"n": len(date_errors), "median": float(np.median(date_errors)), "max": max(date_errors)}
               if date_errors else NOT_AVAILABLE, "labels_used": len(labels), "per_label": per_label}
    return {"capability": "rule-based change engine", "status": "MEASURED", "metrics": metrics, "suppression_statistics": suppression}


def evaluate_btc_b(manifest: dict, connection) -> dict:
    """OSCD benchmark (separate) + stored archive runs; archive metrics only from verified masks."""
    import rasterio
    from .change_ml import learned
    config = learned.model_config()
    oscd = {"kind": "OSCD BENCHMARK EVALUATION — NOT LUMON ARCHIVE", "dataset": "OSCD test split (blaz-r/OSCD_RGB_Cropped_96), 385 tile pairs",
            "source": "reproduced in this project on 2026-10-05 (docs/LEARNED_CHANGE_EVAL.md)", "reported": config.get("verified_here"),
            "values": {"f1": 0.540, "precision": 0.620, "recall": 0.479, "iou": 0.370},
            "note": "Describes BTC-B on OSCD (Sentinel-2 L1C, urban labels). Must not be applied to Lumon's archive."}
    connection.executescript(learned.SCHEMA)
    pairs, measured = [], 0
    for pair in manifest["btc_b"].get("pairs", []):
        run = connection.execute("SELECT * FROM ml_change_runs WHERE before_scene_id = ? AND after_scene_id = ? AND model_key = ? ORDER BY created_at DESC LIMIT 1",
                                 (pair["before_scene_id"], pair["after_scene_id"], learned.model_key())).fetchone()
        entry = {"id": pair["id"], "split": pair["split"], "before_scene_id": pair["before_scene_id"], "after_scene_id": pair["after_scene_id"]}
        if run is None:
            entry.update(status=NOT_AVAILABLE, reason="no stored BTC-B run for this pair with the current model (the evaluator does not run inference; use ml-change-run)")
            pairs.append(entry)
            continue
        checks = json.loads(run["checks"])
        entry.update({"run_id": run["id"], "kind": "MODEL-GENERATED CANDIDATES — NOT CONFIRMED CHANGE",
                      "candidate_pixels": run["candidate_pixels"], "clear_pixels": run["clear_pixels"],
                      "regions_reported": run["regions_reported"], "regions_too_small": run["regions_too_small"], "runtime_s": run["seconds"],
                      "gates": {"misregistration_px": checks.get("shift_px"), "joint_clear": checks.get("joint_clear"),
                                "month_gap": checks.get("month_gap"), "warnings": checks.get("warnings")}})
        label = pair.get("label") or {}
        if label.get("status") == "verified" and label.get("mask_path"):
            truth_path = Path(label["mask_path"])
            if not truth_path.is_absolute():
                truth_path = settings.ROOT_DIR / truth_path
            digest = hashlib.sha256(truth_path.read_bytes()).hexdigest()
            if digest != label.get("mask_sha256"):
                raise EvaluationIntegrityError([f"btc-b item {pair['id']}: label mask checksum differs from the manifest"])
            with rasterio.open(learned._path(run["mask_path"])) as predicted, rasterio.open(truth_path) as truth:
                if (predicted.crs, predicted.transform, predicted.shape) != (truth.crs, truth.transform, truth.shape):
                    raise EvaluationIntegrityError([f"btc-b item {pair['id']}: label mask is not on the run's pixel grid"])
                p, t = predicted.read(1), truth.read(1)
            entry["metrics"] = {"status": "MEASURED", "label_source": label.get("source"),
                                **mask_metrics(p == 1, t == 1, (p != 255) & (t != 255))}
            measured += 1
        else:
            entry["metrics"] = _not_available("no verified change mask for this pair")
        pairs.append(entry)
    return {"capability": "BTC-B learned change (separate from the rule-based engine)", "oscd_benchmark": oscd,
            "archive_pairs": pairs, "status": "MEASURED" if measured else LABELS_INSUFFICIENT}


def _incremental_check(holdout_from: str, current_version: str) -> dict:
    """Re-cluster without chips from `holdout_from`, add them incrementally, compare with the current full clustering (scratch DB copy)."""
    from .imagery import discovery
    connection = db.connect()
    full = dict(connection.execute("SELECT chip_id, cluster_id FROM discovery_members WHERE version_id = ?", (current_version,)).fetchall())
    connection.close()
    original = settings.DATABASE_PATH
    scratch = Path(tempfile.mkdtemp(prefix="lumon-eval-"))
    try:
        shutil.copy(original, scratch / "lumon.db")
        settings.DATABASE_PATH = scratch / "lumon.db"
        copy = db.connect()
        held = [dict(r) for r in copy.execute("SELECT * FROM semantic_chips WHERE acquired_at >= ?", (holdout_from,))]
        copy.execute("DELETE FROM semantic_chips WHERE acquired_at >= ?", (holdout_from,))
        copy.commit()
        copy.close()
        started = time.perf_counter()
        base = discovery.build(actor="ps-evaluation (scratch copy)", log=lambda _: None)
        build_s = time.perf_counter() - started
        copy = db.connect()
        if held:
            columns = list(held[0])
            copy.executemany(f"INSERT INTO semantic_chips ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                             [tuple(r[c] for c in columns) for r in held])
            copy.commit()
        copy.close()
        started = time.perf_counter()
        update = discovery.update(actor="ps-evaluation (scratch copy)", log=lambda _: None)
        update_ms = (time.perf_counter() - started) * 1000
        copy = db.connect()
        incremental = dict(copy.execute("SELECT chip_id, cluster_id FROM discovery_members WHERE version_id = ?", (base["id"],)).fetchall())
        copy.close()
    finally:
        settings.DATABASE_PATH = original
        shutil.rmtree(scratch, ignore_errors=True)
    agreement = {}
    for family in sorted({r["chip_px"] for r in held}, reverse=True):
        ids = [r["id"] for r in held if r["chip_px"] == family and incremental.get(r["id"]) and r["id"] in full]
        pairs = list(combinations(ids, 2))
        agree = sum((incremental[a] == incremental[b]) == (full[a] == full[b]) for a, b in pairs)
        agreement[f"{family}px"] = round(agree / len(pairs), 3) if pairs else None
    return {"holdout_from": holdout_from, "base_embeddings": base["n_embeddings"], "base_clusters": base["n_clusters"],
            "new_chips": update["new_chips"], "assigned": update["assigned"], "unassigned": update["unassigned"],
            "rand_index_vs_full_reclustering": agreement, "build_s": round(build_s, 2), "update_ms": round(update_ms, 1),
            "note": "run on a temporary copy of the database; the real index is not modified"}


def evaluate_discovery(manifest: dict) -> dict:
    from .imagery import discovery
    version = discovery.get_version()
    if version is None:
        return {"status": NOT_AVAILABLE, "reason": "no discovery clustering version"}
    report = discovery.evaluate()
    section = {"kind": "STRUCTURAL CLUSTER QUALITY — NOT SEMANTIC / OPERATIONAL CORRECTNESS", "version_id": version["id"],
               "method": version["method"], "embeddings": version["n_embeddings"], "clusters": version["n_clusters"],
               "families": report["families"], "semantic_correctness": _not_available("no labels of what clusters contain; "
               "clusters are embedding groupings, never classes"), "status": "MEASURED (structural only)"}
    holdout = manifest["discovery"].get("incremental_check", {}).get("holdout_from")
    section["incremental"] = _incremental_check(holdout, version["id"]) if holdout else _not_available("no incremental check requested")
    return section


# ---------------------------------------------------------------------------
# Context: hardware, software, models, storage
# ---------------------------------------------------------------------------

def _software() -> dict:
    versions = {"python": platform.python_version(), "lumon_harness": HARNESS_VERSION}
    for package in ("numpy", "rasterio", "torch", "open_clip_torch", "transformers", "fastapi"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions


def _model_identity(config: dict) -> dict:
    from .imagery.local_ingest import sha256_of_file
    weights = config["weights"]
    path = settings.ROOT_DIR / weights["path"]
    present = path.exists()
    actual = sha256_of_file(path) if present else None
    return {"id": config["id"], "name": config["name"], "license": config.get("license"), "source": config.get("source"),
            "weights_path": weights["path"], "expected_sha256": weights["sha256"], "file_present": present,
            "checksum_verified": actual == weights["sha256"] if present else False}


def _folder_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def _index_fingerprint(connection) -> str:
    """Hash of the embedding cache and the current discovery version (must not change during evaluation)."""
    digest = hashlib.sha256()
    for row in connection.execute("SELECT id, model_key, embedding FROM semantic_chips ORDER BY id, model_key"):
        digest.update(row["id"].encode() + row["model_key"].encode() + row["embedding"])
    current = connection.execute("SELECT id, n_embeddings FROM discovery_versions WHERE status = 'current'").fetchone() \
        if connection.execute("SELECT name FROM sqlite_master WHERE name = 'discovery_versions'").fetchone() else None
    digest.update(repr(tuple(current) if current else None).encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(manifest_path: str, model=None, write: bool = True) -> dict:
    """Validate the manifest, evaluate every section, write JSON + Markdown. Raises EvaluationIntegrityError."""
    from .change_ml import learned
    from .evaluation import hardware
    from .imagery import semantic

    started = time.perf_counter()
    path = Path(manifest_path)
    manifest = json.loads(path.read_text())
    connection = db.connect()
    try:
        problems = validate_manifest(manifest, connection, semantic.model_key(), semantic.PREPROCESS_VERSION)
        if problems:
            raise EvaluationIntegrityError(problems)
        if model is None and semantic.model_problems():
            raise EvaluationIntegrityError(["RemoteCLIP is not staged: " + "; ".join(semantic.model_problems())])
        fingerprint_before = _index_fingerprint(connection)
        dataset = manifest["dataset"]
        chips = connection.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT scene_id) FROM semantic_chips WHERE model_key = ? AND preprocess_version = ? AND scene_id IN ({','.join('?' * len(dataset['scenes']))})",
            (semantic.model_key(), semantic.PREPROCESS_VERSION, *[s["id"] for s in dataset["scenes"]])).fetchone()
        sections = {
            "semantic_retrieval": evaluate_semantic(manifest, model),
            "image_similarity": evaluate_similarity(manifest),
            "rule_based_change": evaluate_rule_change(manifest, connection),
            "btc_b": evaluate_btc_b(manifest, connection),
            "discovery": evaluate_discovery(manifest),
        }
        if _index_fingerprint(connection) != fingerprint_before:
            raise EvaluationIntegrityError(["the embedding index or discovery version changed during evaluation"])
        labels = Counter()
        for kind, item in _label_items(manifest):
            label = item.get("relevance") if kind in ("semantic", "similarity") else item.get("label") if kind == "btc-b" else item
            labels[f"{kind}:{(label or {}).get('status')}"] += 1
        held_out = manifest["splits"]["held-out"]
        report = {
            "evaluation": {"harness_version": HARNESS_VERSION, "generated_at": db.now_iso(), "mode": settings.operating_mode(),
                           "runtime_s": None, "reproduce": f"LUMON_MODE=airgapped npm run lumon -- evaluate --manifest {manifest_path}"},
            "manifest": {k: manifest[k] for k in ("manifest_id", "manifest_version", "created_at", "created_by", "creation_method",
                                                  "ground_truth_source", "content_sha256")} | {"path": str(manifest_path)},
            "hardware": hardware(), "software": _software(),
            "dataset": {"aoi_id": dataset["aoi_id"], "sensor": dataset.get("sensor"), "source": dataset.get("source"),
                        "date_range": dataset["date_range"], "scenes": len(dataset["scenes"]), "indexed_chips": chips[0],
                        "indexed_scenes": chips[1], "area_km2": dataset.get("area_km2"), "splits": manifest["splits"]},
            "models": {"remoteclip": _model_identity(semantic.model_config()) | {"embedding_index_version": semantic.model_key(),
                                                                                 "preprocess_version": semantic.PREPROCESS_VERSION},
                       "btc_b": _model_identity(learned.model_config())},
            "storage_bytes": {"database": settings.DATABASE_PATH.stat().st_size, "imagery": _folder_bytes(settings.IMAGERY_DIR),
                              "models": _folder_bytes(settings.ROOT_DIR / "data" / "models"),
                              "embeddings": connection.execute("SELECT COALESCE(SUM(LENGTH(embedding)), 0) FROM semantic_chips").fetchone()[0]},
            "label_counts": dict(sorted(labels.items())),
            "held_out": {"status": held_out.get("status"), "note": held_out.get("note")},
            **sections,
            "do_not_claim": DO_NOT_CLAIM,
            "known_limitations": manifest.get("known_limitations", []),
        }
        report["not_available"] = _collect_not_available(report)
        report["evaluation"]["runtime_s"] = round(time.perf_counter() - started, 2)
        if write:
            folder = report_folder(manifest)
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            provenance_id = provenance.create(connection, kind="ps-evaluation", source_id=manifest["manifest_id"], input_ref=str(manifest_path),
                                              input_sha256=manifest["content_sha256"], processing="SIH 26227 evaluation harness",
                                              processing_version=HARNESS_VERSION, parameters={"report_stamp": stamp, "label_counts": report["label_counts"]})
            report["evaluation"]["provenance_id"] = provenance_id
            text = json.dumps(report, indent=2, default=str)
            (folder / f"{stamp}.json").write_text(text)
            (folder / f"{stamp}.md").write_text(to_markdown(report))
            (folder / "latest.json").write_text(text)
            (folder / "latest.md").write_text(to_markdown(report))
            report["evaluation"]["report_files"] = [str(folder / f"{stamp}.json"), str(folder / f"{stamp}.md")]
            audit.record(connection, "ps-evaluation", "ps-evaluation", manifest["manifest_id"],
                         {"version": manifest["manifest_version"], "report": stamp, "not_available": len(report["not_available"])})
        return report
    finally:
        connection.close()


def _collect_not_available(report: dict) -> list[str]:
    """Every metric that could not be computed, with its reason."""
    out = []
    for item in report["semantic_retrieval"]["queries"]:
        if item["metrics"]["status"] != "MEASURED":
            out.append(f"semantic '{item['text']}': {item['metrics']['reason']}")
    for item in report["image_similarity"]["references"]:
        if item["metrics"]["status"] != "MEASURED":
            out.append(f"similarity {item['id']}: {item['metrics']['reason']}")
    if report["rule_based_change"]["metrics"]["status"] != "MEASURED":
        out.append(f"rule-based change precision/recall/F1: {report['rule_based_change']['metrics']['reason']}")
    for item in report["btc_b"]["archive_pairs"]:
        metrics = item.get("metrics") or {"status": item.get("status"), "reason": item.get("reason")}
        if metrics["status"] != "MEASURED":
            out.append(f"BTC-B archive pair {item['id']}: {metrics['reason']}")
    out.append("discovery semantic/operational correctness: " + report["discovery"]["semantic_correctness"]["reason"]
               if isinstance(report["discovery"].get("semantic_correctness"), dict) else "discovery: not evaluated")
    if report["held_out"]["status"] != "AVAILABLE":
        out.append(f"held-out evaluation: PENDING — {report['held_out']['note']}")
    return out


def _short(scene_id: str) -> str:
    """The date part of a Sentinel-2 scene id (S2B_43QBB_20240112_1_L2A -> 20240112); other ids unchanged."""
    parts = scene_id.split("_")
    return parts[2] if len(parts) > 2 and parts[2].isdigit() else scene_id


def to_markdown(report: dict) -> str:
    """Human-readable report generated from the JSON (same numbers, never re-computed)."""
    def fmt(value):
        return "—" if value is None else (f"{value:.3f}" if isinstance(value, float) else str(value))

    m, d, ev = report["manifest"], report["dataset"], report["evaluation"]
    lines = [f"# SIH 26227 evaluation report — {m['manifest_id']} v{m['manifest_version']}", "",
             f"Generated {ev['generated_at']} by `{ev['reproduce']}` (harness {ev['harness_version']}, mode {ev['mode']}, {fmt(ev['runtime_s'])} s).", "",
             "## Claims this report does NOT support", ""] + [f"- {c}" for c in report["do_not_claim"]] + [
             "", "## Dataset and splits", "",
             f"- AOI `{d['aoi_id']}` ({fmt(d.get('area_km2'))} km²), {d['sensor']}, source: {d['source']}",
             f"- Dates {d['date_range'][0]} → {d['date_range'][1]}; {d['scenes']} scenes; {d['indexed_chips']} indexed chips from {d['indexed_scenes']} scenes",
             f"- Manifest checksum `{m['content_sha256']}`; created {m['created_at']} by {m['created_by']}",
             f"- Ground-truth source: {m['ground_truth_source']}",
             f"- **Held-out status: {report['held_out']['status']}** — {report['held_out']['note']}", ""]
    for name, split in d["splits"].items():
        lines.append(f"- split `{name}`: {split.get('status', '')} {split.get('note', '')}".rstrip())
    lines += ["", "## Hardware, software, models", "",
              f"- Hardware: {report['hardware']}", f"- Software: {report['software']}"]
    for key, model in report["models"].items():
        lines.append(f"- {key}: {model['name']} · licence {model['license']} · weights {model['weights_path']} · "
                     f"checksum verified: {model['checksum_verified']}")
    lines += [f"- Storage (bytes): {report['storage_bytes']}", f"- Label counts: {report['label_counts']}", "",
              "## Semantic retrieval (RemoteCLIP)", "", f"Status: **{report['semantic_retrieval']['status']}** · "
              f"scores are {report['semantic_retrieval']['score_kind']} · median query latency {fmt(report['semantic_retrieval']['latency_ms_median'])} ms", "",
              "| Query | Split | Metrics | Latency ms | Top-3 (score) |", "|---|---|---|---|---|"]
    for q in report["semantic_retrieval"]["queries"]:
        metrics = q["metrics"]
        shown = ", ".join(f"{k} {fmt(v)}" for k, v in metrics.items() if k.startswith(("precision@", "recall@", "average_precision"))
                          and not k.endswith("kind")) if metrics["status"] == "MEASURED" else f"{NOT_AVAILABLE}: {metrics['reason']}"
        top = "; ".join(f"{t['chip_id']} ({t['score']})" for t in q["top_results"][:3])
        lines.append(f"| {q['text']}{' — ' + q['capability_note'] if q.get('capability_note') else ''} | {q['split']} | {shown} | "
                     f"{fmt(q['latency_ms']['text_encode'] + q['latency_ms']['rank'])} | {top} |")
    lines += ["", "## Image-to-image similarity", "", f"Status: **{report['image_similarity']['status']}**", "",
              "| Reference | Scope | Metrics | Latency ms |", "|---|---|---|---|"]
    for r in report["image_similarity"]["references"]:
        metrics = r["metrics"]
        shown = metrics["status"] if metrics["status"] == "MEASURED" else f"{NOT_AVAILABLE}: {metrics['reason']}"
        lines.append(f"| {r['chip_id']} | {r['scope']} | {shown} | {fmt(r['latency_ms'])} |")
    rc = report["rule_based_change"]
    lines += ["", "## Rule-based change engine", "", f"Status: **{rc['status']}**", ""]
    if rc["metrics"]["status"] == "MEASURED":
        lines.append(f"- precision {fmt(rc['metrics']['precision'])}, recall {fmt(rc['metrics']['recall'])}, F1 {fmt(rc['metrics']['f1'])} "
                     f"({rc['metrics']['labels_used']} verified labels; unit: {rc['metrics']['unit']})")
    else:
        lines.append(f"- Metrics {NOT_AVAILABLE}: {rc['metrics']['reason']}")
    s = rc["suppression_statistics"]
    lines += [f"- {s['kind']}: {s['accepted']} accepted, {s['suppressed']} suppressed of {s['candidates']}; failed gates {s['failed_gate_counts']}. {s['note']}",
              "", "## BTC-B learned change", "", f"Status: **{report['btc_b']['status']}**", ""]
    o = report["btc_b"]["oscd_benchmark"]
    lines += [f"- {o['kind']}: F1 {o['values']['f1']}, precision {o['values']['precision']}, recall {o['values']['recall']}, "
              f"IoU {o['values']['iou']} — {o['dataset']}. {o['note']}", "",
              "| Pair | Candidate px / clear px | Regions | Misreg. px | Runtime s | Archive metrics |", "|---|---|---|---|---|---|"]
    for p in report["btc_b"]["archive_pairs"]:
        if p.get("run_id"):
            metrics = p["metrics"]
            shown = f"F1 {fmt(metrics.get('f1'))}, IoU {fmt(metrics.get('iou'))}" if metrics["status"] == "MEASURED" else f"{NOT_AVAILABLE}: {metrics['reason']}"
            lines.append(f"| {_short(p['before_scene_id'])} → {_short(p['after_scene_id'])} | {p['candidate_pixels']} / {p['clear_pixels']} "
                         f"(candidates, not confirmed) | {p['regions_reported']} | {fmt(p['gates']['misregistration_px'])} | {fmt(p['runtime_s'])} | {shown} |")
        else:
            lines.append(f"| {p['before_scene_id']} → {p['after_scene_id']} | — | — | — | — | {p['status']}: {p['reason']} |")
    dsc = report["discovery"]
    lines += ["", "## Discovery / clustering", ""]
    if dsc.get("status", "").startswith("MEASURED"):
        lines += [f"{dsc['kind']} · version {dsc['version_id']} · {dsc['embeddings']} embeddings → {dsc['clusters']} clusters", ""]
        for family, f in dsc["families"].items():
            lines.append(f"- {family} px: silhouette {f['silhouette']}, 10-NN same cluster {f['nn10_same_cluster']} "
                         f"(chance {f['chance_same_cluster']}), other places only {f['nn10_other_places_same_cluster']}, "
                         f"places/cluster {f['places_per_cluster_min_median_max']}")
        inc = dsc["incremental"]
        if "rand_index_vs_full_reclustering" in inc:
            lines.append(f"- Incremental: chips from {inc['holdout_from']} held out; {inc['new_chips']} added, {inc['assigned']} assigned, "
                         f"{inc['unassigned']} unassigned in {inc['update_ms']} ms; agreement with full re-clustering {inc['rand_index_vs_full_reclustering']}")
        lines.append(f"- Semantic correctness: {NOT_AVAILABLE} — {dsc['semantic_correctness']['reason']}")
    else:
        lines.append(f"- {dsc.get('status')}: {dsc.get('reason')}")
    lines += ["", "## Metrics NOT AVAILABLE", ""] + [f"- {item}" for item in report["not_available"]]
    if report["known_limitations"]:
        lines += ["", "## Known limitations", ""] + [f"- {item}" for item in report["known_limitations"]]
    lines += ["", f"Reproduce: `{ev['reproduce']}`", ""]
    return "\n".join(lines)
