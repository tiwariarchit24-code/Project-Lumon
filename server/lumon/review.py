"""
Analyst review: the ranked queue of change candidates and the analyst's
decisions on them.

Every decision (confirm / reject / relabel) is:
  - stored in the `decisions` table (never edited or deleted)
  - reflected in the candidate's review_state
  - written to the hash-chained audit log
Confirmed and rejected candidates then feed "more like these" as positive
and negative examples.
"""

from . import audit, db, provenance
from .imagery import similarity

DECISIONS = {"confirmed", "rejected", "relabelled"}
LABELS = {"construction", "clearance", "water-extent", "road-development", "other"}


def _candidate_summary(row) -> dict:
    candidate = db.row_to_dict(row, ["gate_results", "tile_ids", "supporting_scene_ids"])
    candidate["geometry"] = db.from_json(candidate["geometry"])
    candidate["failed_gates"] = [g for g in candidate["gate_results"] if not g["passed"]]
    return candidate


def queue(aoi_id: str | None = None, include_suppressed: bool = False, limit: int = 200) -> list[dict]:
    """
    The review queue, highest priority first.

    PRIORITY (documented, simple): unreviewed before reviewed, then higher
    uncalibrated score, then larger area. Suppressed candidates are only
    included when asked for ("SHOW SUPPRESSED").
    """
    connection = db.connect()
    sql = "SELECT * FROM change_candidates WHERE 1 = 1"
    params = []
    if aoi_id:
        sql += " AND aoi_id = ?"
        params.append(aoi_id)
    if not include_suppressed:
        sql += " AND status = 'accepted'"
    sql += " ORDER BY (review_state = 'unreviewed') DESC, score DESC, area_m2 DESC LIMIT ?"
    params.append(limit)
    items = []
    for rank, row in enumerate(connection.execute(sql, params), start=1):
        candidate = _candidate_summary(row)
        candidate["priority"] = rank
        candidate.pop("geometry")
        items.append(candidate)
    connection.close()
    return items


def get_candidate(candidate_id: str) -> dict | None:
    """Full detail of one candidate: gates, scenes, decisions, provenance."""
    connection = db.connect()
    row = connection.execute("SELECT * FROM change_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        connection.close()
        return None
    candidate = _candidate_summary(row)
    scene_ids = candidate["supporting_scene_ids"] or []
    scenes = [dict(r) for r in connection.execute(
        f"SELECT id, acquired_at, sensor, processing_level, quality_status, cloud_fraction, file_sha256, provenance_id FROM scenes WHERE id IN ({','.join('?' * len(scene_ids))}) ORDER BY acquired_at",
        scene_ids)] if scene_ids else []
    candidate["scenes"] = scenes
    # The two evidence images: last clear look before, first clear look after.
    candidate["evidence_before"] = next((s for s in scenes if s["acquired_at"][:10] == candidate["last_clean_before"]), None)
    candidate["evidence_after"] = next((s for s in scenes if s["acquired_at"][:10] == candidate["earliest_supported_after"]), None)
    candidate["decisions"] = [dict(r) for r in connection.execute(
        "SELECT * FROM decisions WHERE target_kind = 'change' AND target_id = ? ORDER BY id", (candidate_id,))]
    candidate["provenance"] = provenance.get(connection, candidate["provenance_id"]) if candidate["provenance_id"] else None
    connection.close()
    return candidate


def decide(candidate_id: str, decision: str, analyst: str, reason: str | None = None, new_label: str | None = None) -> dict:
    """
    Record an analyst decision. Raises ValueError for invalid input.
    - decision: confirmed | rejected | relabelled
    - new_label: required for relabelled (one of LABELS)
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    if decision == "relabelled" and new_label not in LABELS:
        raise ValueError(f"new_label must be one of {sorted(LABELS)}")
    if not analyst or not analyst.strip():
        raise ValueError("analyst name is required")
    connection = db.connect()
    if connection.execute("SELECT 1 FROM change_candidates WHERE id = ?", (candidate_id,)).fetchone() is None:
        connection.close()
        raise ValueError("unknown candidate")
    decided_at = db.now_iso()
    connection.execute(
        "INSERT INTO decisions (target_kind, target_id, decision, new_label, analyst, reason, decided_at) VALUES ('change', ?, ?, ?, ?, ?, ?)",
        (candidate_id, decision, new_label, analyst.strip(), reason, decided_at),
    )
    connection.execute("UPDATE change_candidates SET review_state = ? WHERE id = ?", (decision, candidate_id))
    connection.commit()
    entry = audit.record(connection, analyst.strip(), decision, candidate_id, {"reason": reason, "new_label": new_label})
    connection.close()
    return {"candidate_id": candidate_id, "decision": decision, "new_label": new_label, "decided_at": decided_at, "audit_hash": entry["hash"]}


def more_like_these(candidate_ids: list[str], limit: int = 10) -> dict:
    """
    Similar tiles to the given candidates' tiles (positives), steering away
    from tiles of candidates the analyst rejected in the same AOI (negatives).
    """
    connection = db.connect()
    rows = [dict(r) for r in connection.execute(
        f"SELECT id, aoi_id, tile_ids FROM change_candidates WHERE id IN ({','.join('?' * len(candidate_ids))})", candidate_ids)]
    if not rows:
        connection.close()
        return {"results": [], "method": "no matching candidates"}
    aoi_id = rows[0]["aoi_id"]
    positive = [t for r in rows for t in db.from_json(r["tile_ids"])]
    negative = [t for r in connection.execute(
        "SELECT tile_ids FROM change_candidates WHERE aoi_id = ? AND review_state = 'rejected'", (aoi_id,))
        for t in db.from_json(r["tile_ids"])]
    connection.close()
    result = similarity.find_similar(aoi_id, positive, negative, limit=limit)
    result["aoi_id"] = aoi_id
    return result
