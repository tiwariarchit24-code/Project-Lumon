"""
Local database for the Lumon backend.

WHY SQLITE FOR NOW?
The target architecture is PostgreSQL + PostGIS + pgvector. Installing that
stack is heavy (and Docker is not available on the development machine), so
this stage uses SQLite, which ships with Python and needs no server.

The tables below are deliberately shaped like the future PostGIS tables:
 - geometries are stored as GeoJSON text (PostGIS can load this directly
   with ST_GeomFromGeoJSON)
 - each spatial row also stores a bounding box (min/max lon/lat) so we can
   do fast "what is in view" filtering without spatial extensions
 - JSON columns hold source-specific details that do not deserve their own
   column

Moving to PostGIS later means changing this file and the SQL in a few
query functions, not the API or the frontend.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import settings

# One table per concept. Read this top to bottom to understand the data model.
SCHEMA = """
-- Live state of every OSINT source (the static definition lives in
-- config/sources/registry.json).
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    health TEXT NOT NULL DEFAULT 'never-run',  -- never-run | ok | failed | offline-snapshot | key-required
    last_success TEXT,
    last_failure TEXT,
    last_error TEXT,
    last_snapshot TEXT,          -- path of the newest raw snapshot file
    last_snapshot_sha256 TEXT,
    last_data_time TEXT,         -- newest event/record time seen in the data
    record_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

-- One row per refresh attempt, kept for history and debugging.
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,          -- connected | airgapped
    status TEXT NOT NULL,        -- success | failed | offline-snapshot | skipped
    records_in INTEGER DEFAULT 0,
    records_kept INTEGER DEFAULT 0,
    records_outside INTEGER DEFAULT 0,
    records_invalid INTEGER DEFAULT 0,
    snapshot_path TEXT,
    snapshot_sha256 TEXT,
    error TEXT
);

-- Time-based happenings: earthquakes, natural hazards, outages, launches...
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,         -- "<source_id>:<id in source>"
    source_id TEXT NOT NULL,
    type TEXT NOT NULL,          -- earthquake | wildfire | flood | cyclone | ...
    category TEXT NOT NULL,      -- layer group, e.g. DISASTERS
    title TEXT NOT NULL,
    description TEXT,
    geometry TEXT,               -- GeoJSON geometry (may be NULL for non-spatial events)
    lon REAL, lat REAL,          -- representative point used for markers
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
    start_time TEXT,             -- ISO 8601 UTC
    end_time TEXT,
    status TEXT,                 -- as reported by the source (e.g. "reviewed", "open")
    evidence_type TEXT NOT NULL DEFAULT 'VERIFIED RECORD',
    attributes TEXT,             -- JSON: source-specific values (magnitude, alert level...)
    raw TEXT,                    -- JSON: the original record from the source
    provenance_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events(start_time);
CREATE INDEX IF NOT EXISTS events_bbox ON events(min_lon, min_lat, max_lon, max_lat);

-- Long-lived things: airports, ports, power plants, satellites...
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    type TEXT NOT NULL,          -- airport | port | power-plant | ...
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    geometry TEXT,
    lon REAL, lat REAL,
    attributes TEXT,
    provenance_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entities_point ON entities(lon, lat);

-- Where every important output came from.
CREATE TABLE IF NOT EXISTS provenance (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,          -- source-snapshot | boundary | scene | change-run | ...
    source_id TEXT,
    source_version TEXT,
    retrieved_at TEXT,
    input_ref TEXT,              -- URL or file the data came from
    input_sha256 TEXT,
    processing TEXT,             -- what we did to it
    processing_version TEXT,
    parameters TEXT,             -- JSON
    created_at TEXT NOT NULL
);

-- Append-only, hash-chained log of analyst and system actions (see audit.py).
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

-- Simple job queue processed by the worker (see worker.py).
-- Local GeoTIFF/COG ingestion (lumon.imagery.local_ingest). local_sources
-- records every local file Lumon was given (one row per SHA-256), with the
-- metadata read from it, where each value came from, and the scene it became.
-- local_aois holds AOIs defined by a local raster's own grid.
CREATE TABLE IF NOT EXISTS local_sources (
    original_sha256 TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    aoi_id TEXT NOT NULL,
    original_path TEXT NOT NULL,
    status TEXT NOT NULL,              -- registered | pending-metadata
    metadata TEXT NOT NULL,            -- JSON: format, CRS, transform, size, bounds, bands, nodata, tags
    acquired_at TEXT,                  -- NULL = UNKNOWN
    acquired_source TEXT NOT NULL,     -- file tag name | operator | unknown
    band_mapping TEXT,                 -- JSON {canonical name: source band index}
    view_path TEXT,                    -- the raster the pipeline reads (original or band-mapping VRT)
    compatibility TEXT NOT NULL,       -- JSON {capability: SUPPORTED | NOT COMPATIBLE ... | NOT ENOUGH METADATA ...}
    provenance_id TEXT,
    ingest_version TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_aois (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    bbox TEXT NOT NULL,                -- JSON [w, s, e, n] in WGS84, from the raster bounds
    grid TEXT NOT NULL,                -- JSON {crs, bounds, res, width, height}: the raster's own pixel grid
    created_from TEXT NOT NULL,        -- SHA-256 of the first file
    created_at TEXT NOT NULL
);

-- Semantic image search (RemoteCLIP): one row per image chip with its
-- cached embedding, and one row per scene recording what the indexer did.
-- Rows are tied to a model_key (model id + weights checksum) and a
-- preprocessing version, so a different model never reuses old vectors.
CREATE TABLE IF NOT EXISTS semantic_chips (
    id TEXT NOT NULL,            -- scene_id:chip_px:row_off:col_off
    model_key TEXT NOT NULL,
    preprocess_version TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    aoi_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    chip_px INTEGER NOT NULL,    -- chip size in 10 m pixels
    row_off INTEGER NOT NULL,
    col_off INTEGER NOT NULL,
    footprint TEXT NOT NULL,     -- GeoJSON polygon (WGS84) of the chip
    lon REAL NOT NULL,           -- chip centre
    lat REAL NOT NULL,
    valid_fraction REAL NOT NULL,
    file_sha256 TEXT,
    embedding BLOB NOT NULL,     -- float32, L2-normalised
    provenance_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, model_key, preprocess_version)
);
CREATE TABLE IF NOT EXISTS semantic_scenes (
    scene_id TEXT NOT NULL,
    model_key TEXT NOT NULL,
    preprocess_version TEXT NOT NULL,
    status TEXT NOT NULL,        -- indexed | excluded
    reason TEXT,                 -- why a scene was excluded
    chips_indexed INTEGER NOT NULL DEFAULT 0,
    chips_excluded INTEGER NOT NULL DEFAULT 0,
    exclusion_reasons TEXT,      -- JSON {reason: count} for excluded chips
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (scene_id, model_key, preprocess_version)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- source-refresh | imagery-ingest | change-analysis | ...
    params TEXT,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result TEXT,
    error TEXT
);

-- Satellite scenes registered in the local archive.
CREATE TABLE IF NOT EXISTS scenes (
    id TEXT PRIMARY KEY,         -- the provider's scene id
    aoi_id TEXT NOT NULL,
    sensor TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    crs TEXT,
    processing_level TEXT,
    file_path TEXT,
    file_sha256 TEXT,
    cloud_fraction REAL,         -- measured inside the AOI from the scene's own mask
    radiometric_offset REAL,     -- reflectance offset decided at ingest (see imagery/sentinel2.py)
    quality_status TEXT NOT NULL, -- usable | degraded | unusable | quarantined
    quarantine_reason TEXT,
    processing_version TEXT,
    source_url TEXT,
    provenance_id TEXT,
    created_at TEXT NOT NULL
);

-- Small square analysis cells covering an AOI.
CREATE TABLE IF NOT EXISTS tiles (
    id TEXT PRIMARY KEY,         -- "<aoi>:<row>:<col>"
    aoi_id TEXT NOT NULL,
    row INTEGER NOT NULL,
    col INTEGER NOT NULL,
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    geometry TEXT NOT NULL
);

-- What one scene showed in one tile: land-cover class and features.
CREATE TABLE IF NOT EXISTS tile_observations (
    scene_id TEXT NOT NULL,
    tile_id TEXT NOT NULL,
    valid_fraction REAL NOT NULL,  -- share of pixels not masked as cloud/shadow/etc.
    land_class TEXT,               -- water | vegetation | built | bare | NULL when unusable
    class_fractions TEXT,          -- JSON: share of valid pixels in each class
    features TEXT,                 -- JSON: feature vector used for similarity search
    PRIMARY KEY (scene_id, tile_id)
);

-- Change candidates. Accepted ones are the "change events" shown to analysts;
-- suppressed ones keep their rejection reasons so analysts can inspect them.
CREATE TABLE IF NOT EXISTS change_candidates (
    id TEXT PRIMARY KEY,
    aoi_id TEXT NOT NULL,
    change_class TEXT NOT NULL,   -- construction | clearance | water-extent | road-development | other
    direction TEXT NOT NULL,      -- appearance | disappearance | expansion | contraction
    from_class TEXT,
    to_class TEXT,
    tile_ids TEXT NOT NULL,       -- JSON list
    geometry TEXT NOT NULL,
    lon REAL, lat REAL,
    area_m2 REAL,
    area_min_m2 REAL,
    area_max_m2 REAL,
    last_clean_before TEXT,       -- date of the last clean observation before the change
    earliest_supported_after TEXT,-- date of the first clean observation showing the change
    supporting_scene_ids TEXT,    -- JSON list
    score REAL,
    score_kind TEXT,              -- always 'uncalibrated-score' until labelled data exists
    status TEXT NOT NULL,         -- accepted | suppressed
    gate_results TEXT,            -- JSON: pass/fail + reason per gate
    review_state TEXT NOT NULL DEFAULT 'unreviewed',  -- unreviewed | confirmed | rejected | relabelled
    provenance_id TEXT,
    created_at TEXT NOT NULL
);

-- Analyst decisions (confirm / reject / relabel). Never deleted.
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,    -- change | event | entity
    target_id TEXT NOT NULL,
    decision TEXT NOT NULL,       -- confirmed | rejected | relabelled
    new_label TEXT,
    analyst TEXT NOT NULL,
    reason TEXT,
    decided_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string, e.g. '2026-10-04T08:00:00Z'."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_after_seconds(seconds: float) -> str:
    """UTC time `seconds` from now, in the same ISO format as now_iso()."""
    later = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return later.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    """
    Open the database, creating the file and tables on first use.

    Rows come back as sqlite3.Row, which behaves like a dictionary
    (row["title"]), keeping the calling code readable.
    """
    settings.ensure_data_folders()
    connection = sqlite3.connect(settings.DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    # WAL mode lets the API read while the worker writes.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    _add_missing_columns(connection)
    return connection


# Columns added after the first release. SQLite's CREATE TABLE IF NOT EXISTS
# does not add new columns to an existing table, so we add them here.
LATER_COLUMNS = [
    ("scenes", "radiometric_offset", "REAL"),
    # Live-feed health (OpenSky): remaining provider credits, earliest time
    # the provider allows another request, and failures in a row.
    ("sources", "rate_limit_remaining", "INTEGER"),
    ("sources", "next_allowed_at", "TEXT"),
    ("sources", "consecutive_failures", "INTEGER"),
]


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    for table, column, kind in LATER_COLUMNS:
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            connection.commit()


def to_json(value) -> str | None:
    """Store Python values in JSON text columns (None stays NULL)."""
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def from_json(text):
    """Read a JSON text column back into Python values (NULL becomes None)."""
    if text is None:
        return None
    return json.loads(text)


def row_to_dict(row: sqlite3.Row, json_columns: list[str] = ()) -> dict:
    """Convert a database row to a plain dict, decoding the given JSON columns."""
    result = dict(row)
    for column in json_columns:
        if column in result:
            result[column] = from_json(result[column])
    return result
