"""
The evidence record every future AI/ML output must carry.

Nothing writes here yet: no model is deployed. This module fixes the
CONTRACT so that, when a real model runs on NIT Raipur imagery, each output
arrives with everything an analyst needs to judge it, using the same
conventions as the rest of Lumon:

  - geometry as GeoJSON, a provenance record (lumon.provenance), and the
    analyst review state used by change candidates and decisions
  - confidence is stored together with its kind, so an uncalibrated score
    can never be shown as a probability

Required fields of an AI output:
  capability_id      which capability produced it (config/ai/capabilities.json)
  study_area_id      e.g. "nit-raipur"
  source             imagery/data source, e.g. "Sentinel-2 L2A"
  acquisition_date   date of the observation(s) used (ISO date)
  processing_date    when the model ran (ISO time)
  geometry           GeoJSON geometry of the finding
  model_id           model registry id (config/ai/models.json)
  model_version      exact version / weights checksum
  confidence         number, or None
  confidence_kind    "uncalibrated-score" | "calibrated-probability" | "none"
  evidence_refs      list of references (scene ids, chip URLs, records)
  temporal_evidence  list of {date, observation_ref, state}
  provenance_id      id of the provenance record describing the run
  review_state       unreviewed | confirmed | rejected | relabelled
"""

from . import db, provenance

REQUIRED = ["capability_id", "study_area_id", "source", "acquisition_date", "processing_date", "geometry",
            "model_id", "model_version", "confidence_kind", "evidence_refs", "temporal_evidence", "review_state"]
CONFIDENCE_KINDS = {"uncalibrated-score", "calibrated-probability", "none"}
REVIEW_STATES = {"unreviewed", "confirmed", "rejected", "relabelled"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capability_id TEXT NOT NULL,
    study_area_id TEXT NOT NULL,
    source TEXT NOT NULL,
    acquisition_date TEXT NOT NULL,
    processing_date TEXT NOT NULL,
    geometry TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    confidence REAL,
    confidence_kind TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    temporal_evidence TEXT NOT NULL,
    provenance_id TEXT NOT NULL,
    review_state TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL
);
"""


def validate(output: dict) -> list[str]:
    """Return the problems with an AI output record (empty list = valid)."""
    problems = [f"missing {field}" for field in REQUIRED if output.get(field) in (None, "")]
    if output.get("confidence_kind") not in CONFIDENCE_KINDS:
        problems.append("confidence_kind must be one of " + ", ".join(sorted(CONFIDENCE_KINDS)))
    if output.get("confidence_kind") == "none" and output.get("confidence") is not None:
        problems.append("a confidence value needs a confidence_kind")
    if output.get("review_state") not in REVIEW_STATES:
        problems.append("review_state must be one of " + ", ".join(sorted(REVIEW_STATES)))
    if not isinstance(output.get("evidence_refs"), list) or not output.get("evidence_refs"):
        problems.append("evidence_refs must list at least one reference")
    return problems


def store(connection, output: dict, input_ref: str, input_sha256: str | None, parameters: dict | None = None) -> int:
    """
    Store one validated AI output with its provenance record (the model,
    version and parameters of the run). Raises ValueError if invalid.
    Returns the new row id.
    """
    problems = validate(output)
    if problems:
        raise ValueError("; ".join(problems))
    connection.executescript(SCHEMA)
    provenance_id = provenance.create(
        connection, kind="ai-output", source_id=output["capability_id"], input_ref=input_ref, input_sha256=input_sha256,
        processing=f"{output['model_id']} inference", processing_version=output["model_version"],
        parameters=parameters, retrieved_at=output["processing_date"],
    )
    cursor = connection.execute(
        """INSERT INTO ai_outputs (capability_id, study_area_id, source, acquisition_date, processing_date, geometry,
               model_id, model_version, confidence, confidence_kind, evidence_refs, temporal_evidence, provenance_id,
               review_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (output["capability_id"], output["study_area_id"], output["source"], output["acquisition_date"],
         output["processing_date"], db.to_json(output["geometry"]), output["model_id"], output["model_version"],
         output.get("confidence"), output["confidence_kind"], db.to_json(output["evidence_refs"]),
         db.to_json(output["temporal_evidence"]), provenance_id, output["review_state"], db.now_iso()),
    )
    connection.commit()
    return cursor.lastrowid


def get(connection, output_id: int) -> dict | None:
    """One stored AI output with its provenance record."""
    connection.executescript(SCHEMA)
    row = connection.execute("SELECT * FROM ai_outputs WHERE id = ?", (output_id,)).fetchone()
    if row is None:
        return None
    record = db.row_to_dict(row, ["geometry", "evidence_refs", "temporal_evidence"])
    record["provenance"] = provenance.get(connection, record["provenance_id"])
    return record
