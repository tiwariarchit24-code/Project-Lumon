"""
Provenance records: where did a piece of data come from and what did we do
to it?

Every staged boundary, source snapshot, satellite scene and change-analysis
run gets one provenance row. Events, entities, scenes and change candidates
point to that row through their `provenance_id` column, so the analyst can
always trace a marker on the map back to the original download.
"""

import hashlib

from . import db


def create(
    connection,
    kind: str,
    source_id: str | None,
    input_ref: str | None,
    input_sha256: str | None,
    processing: str,
    processing_version: str,
    parameters: dict | None = None,
    source_version: str | None = None,
    retrieved_at: str | None = None,
) -> str:
    """
    Insert a provenance record and return its id.

    The id is derived from the record contents, so recording the exact same
    input + processing twice gives the same id (no duplicate rows).
    """
    key = "|".join([kind, source_id or "", input_ref or "", input_sha256 or "", processing, processing_version, db.to_json(parameters) or ""])
    provenance_id = "prov-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    connection.execute(
        """INSERT OR IGNORE INTO provenance
           (id, kind, source_id, source_version, retrieved_at, input_ref, input_sha256,
            processing, processing_version, parameters, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            provenance_id, kind, source_id, source_version, retrieved_at, input_ref, input_sha256,
            processing, processing_version, db.to_json(parameters), db.now_iso(),
        ),
    )
    return provenance_id


def get(connection, provenance_id: str) -> dict | None:
    """Return one provenance record as a dict, or None if it does not exist."""
    row = connection.execute("SELECT * FROM provenance WHERE id = ?", (provenance_id,)).fetchone()
    return db.row_to_dict(row, ["parameters"]) if row else None
