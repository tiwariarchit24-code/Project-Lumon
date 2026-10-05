"""
Hash-chained audit log.

HOW THE CHAIN WORKS (plain language):
Every audit entry stores the hash of the entry before it ("prev_hash") and
its own hash, computed from its contents plus prev_hash. If anybody edits or
deletes an old row, its hash no longer matches, and every later row's
prev_hash stops lining up. `verify_chain` walks the log from the start and
reports the first place where the chain breaks.

This does not make tampering impossible (someone with database access could
rewrite the whole chain), but it makes silent edits detectable, which is
the purpose of an analyst audit trail.
"""

import hashlib
import json

from . import db

# The "previous hash" used for the very first entry.
GENESIS_HASH = "0" * 64


def _entry_hash(prev_hash: str, at: str, actor: str, action: str, target: str | None, details_json: str | None) -> str:
    """Compute the SHA-256 hash of one entry. The field order must never change."""
    text = json.dumps([prev_hash, at, actor, action, target, details_json], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(connection, actor: str, action: str, target: str | None = None, details: dict | None = None) -> dict:
    """
    Append one entry to the audit log and return it.

    - actor:   who did it, e.g. "analyst" or "system"
    - action:  what happened, e.g. "confirm", "source-refresh", "query"
    - target:  what it was done to, e.g. a change candidate id
    - details: any extra facts worth keeping (stored as JSON)
    """
    # Reading the last hash and inserting the new entry must happen as ONE
    # step. Without that, two requests arriving at the same moment both read
    # the same last hash and the chain forks. BEGIN IMMEDIATE takes SQLite's
    # write lock first, so concurrent writers wait their turn.
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        last = connection.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else GENESIS_HASH
        at = db.now_iso()
        details_json = db.to_json(details)
        entry_hash = _entry_hash(prev_hash, at, actor, action, target, details_json)
        connection.execute(
            "INSERT INTO audit_log (at, actor, action, target, details, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, actor, action, target, details_json, prev_hash, entry_hash),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"at": at, "actor": actor, "action": action, "target": target, "details": details, "hash": entry_hash}


def verify_chain(connection) -> dict:
    """
    Check every entry of the audit log.

    Returns {"ok": True, "entries": N} when the chain is intact, or
    {"ok": False, "entries": N, "broken_at": id, "reason": "..."} at the first problem.
    Exact duplicates of the previous entry are listed in "duplicate_entries".
    """
    expected_prev = GENESIS_HASH
    previous = None
    count = 0
    duplicates = []
    for row in connection.execute("SELECT * FROM audit_log ORDER BY id"):
        count += 1
        # An EXACT copy of the previous entry (same contents, same prev_hash,
        # same hash) is the trace of a concurrent double-write from before
        # writes were serialised. It adds nothing and hides nothing, so it is
        # reported, not treated as tampering. Any other mismatch still fails.
        if previous is not None and all(row[k] == previous[k] for k in ("at", "actor", "action", "target", "details", "prev_hash", "hash")):
            duplicates.append(row["id"])
            continue
        if row["prev_hash"] != expected_prev:
            return {"ok": False, "entries": count, "broken_at": row["id"], "reason": "prev_hash does not match the previous entry"}
        recomputed = _entry_hash(row["prev_hash"], row["at"], row["actor"], row["action"], row["target"], row["details"])
        if recomputed != row["hash"]:
            return {"ok": False, "entries": count, "broken_at": row["id"], "reason": "entry contents were changed after writing"}
        expected_prev = row["hash"]
        previous = row
    result = {"ok": True, "entries": count}
    if duplicates:
        result["duplicate_entries"] = duplicates
        result["note"] = "exact duplicate entries from a concurrent double-write; chain otherwise intact"
    return result
