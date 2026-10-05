"""
OSINT ingestion: refresh one source from start to finish.

THE INGEST PATH (in order):

  1. fetch      - CONNECTED mode: download through lumon.net
                  AIR-GAPPED mode: reuse the newest saved snapshot instead
  2. snapshot   - save the raw bytes untouched, with a SHA-256 checksum
  3. normalize  - the source adapter turns raw data into simple records
  4. validate   - drop records with impossible coordinates/times (counted)
  5. scope      - keep only records inside the India operating area
  6. store      - insert/update events, entities or reference layers
  7. record     - provenance row, source_runs row, source health, audit entry

A failure in one source never stops the others and never deletes data
that was stored earlier: health becomes "failed" and the last good data
stays available.
"""

import json
from datetime import datetime, timezone

from . import audit, db, net, provenance, settings
from .geo import boundary, geometry
from .sources import registry

PROCESSING_VERSION = "ingest-v1"

# How many raw snapshots to keep per source. Live feeds keep a short
# history; large static downloads keep only the newest copy to save disk.
SNAPSHOTS_KEPT = {"events": 10, "entities": 1, "reference": 1}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def _snapshot_folder(source_id: str):
    folder = settings.SNAPSHOT_DIR / source_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_snapshot(source: dict, data: bytes) -> tuple[str, str]:
    """
    Save raw bytes as data/snapshots/<source>/<UTC time>.<ext>.
    Returns (path relative to the project root, sha256). Old snapshots
    beyond the retention limit are deleted.
    """
    folder = _snapshot_folder(source["id"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}.{source.get('snapshot_format', 'bin')}"
    path.write_bytes(data)
    keep = SNAPSHOTS_KEPT.get(source["kind"], 5)
    for old in sorted(folder.iterdir())[:-keep]:
        old.unlink()
    return str(path.relative_to(settings.ROOT_DIR)), net.sha256_of_bytes(data)


def latest_snapshot(source: dict):
    """Return (path, bytes) of the newest saved snapshot, or (None, None)."""
    folder = _snapshot_folder(source["id"])
    files = sorted(folder.iterdir())
    if not files:
        return None, None
    return files[-1], files[-1].read_bytes()


# ---------------------------------------------------------------------------
# Validation and geographic scoping
# ---------------------------------------------------------------------------

def _valid_position(position) -> bool:
    return (
        isinstance(position, (list, tuple)) and len(position) >= 2
        and isinstance(position[0], (int, float)) and isinstance(position[1], (int, float))
        and -180 <= position[0] <= 180 and -90 <= position[1] <= 90
    )


def validate(record: dict) -> str | None:
    """
    Check one normalized record. Returns None when it is fine, or a short
    reason when it must be quarantined (not stored, but counted).
    """
    shape = record.get("geometry")
    if record["kind"] in ("entity", "reference") or record.get("scope", "spatial") == "spatial":
        if not shape:
            return "missing geometry"
        try:
            positions = list(geometry.iter_positions(shape))
        except (KeyError, TypeError):
            return "malformed geometry"
        if not positions or not all(_valid_position(p) for p in positions):
            return "coordinates out of range"
    if record["kind"] == "event":
        if not record.get("title"):
            return "missing title"
        if record.get("start_time") is None:
            return "missing or unparseable time"
    return None


def _swapped(shape: dict) -> dict:
    """Return a copy of a geometry with longitude and latitude exchanged."""
    text = json.dumps(shape)
    data = json.loads(text)

    def swap(value):
        if isinstance(value, list) and value and isinstance(value[0], (int, float)):
            return [value[1], value[0]] + value[2:]
        if isinstance(value, list):
            return [swap(item) for item in value]
        return value

    data["coordinates"] = swap(data["coordinates"])
    return data


def scope(record: dict) -> str:
    """
    Decide where a record stands relative to the India operating area:

      "inside"     - located inside the boundary: keep
      "outside"    - located outside: drop (counted)
      "national"   - about India as a whole (no precise location): keep
      "global"     - worldwide context (e.g. space weather): keep
      "unscoped"   - boundary not staged, so we cannot tell: keep, flagged
      "suspect-swap" - outside, but would be inside if lat/lon were
                     exchanged AND the record's own text names India:
                     quarantine. We do NOT silently "fix" it, because that
                     would be guessing. The text check matters: without it,
                     a genuine port in Norway (lon ~10-30, lat ~60-78) would
                     look like a swapped point in the Indian Ocean.
    """
    if record["kind"] == "event" and record.get("scope") in ("national", "global"):
        return record["scope"]
    inside = boundary.contains_geometry(record["geometry"])
    if inside is None:
        return "unscoped"
    if inside:
        return "inside"
    text = f"{record.get('title') or ''} {record.get('name') or ''}"
    try:
        if "india" in text.lower() and boundary.contains_geometry(_swapped(record["geometry"])):
            return "suspect-swap"
    except (KeyError, TypeError, IndexError):
        pass
    return "outside"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _store_event(connection, source: dict, record: dict, scope_value: str, provenance_id: str) -> None:
    """Insert or update one event. created_at is kept when the event already exists."""
    now = db.now_iso()
    shape = record.get("geometry")
    lon = lat = None
    box = [None, None, None, None]
    if shape:
        lon, lat = geometry.representative_point(shape)
        box = geometry.bbox(shape)
    attributes = dict(record.get("attributes") or {})
    attributes["scope"] = scope_value
    connection.execute(
        """INSERT INTO events (id, source_id, type, category, title, description, geometry, lon, lat,
               min_lon, min_lat, max_lon, max_lat, start_time, end_time, status, evidence_type,
               attributes, raw, provenance_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               type=excluded.type, category=excluded.category, title=excluded.title,
               description=excluded.description, geometry=excluded.geometry, lon=excluded.lon, lat=excluded.lat,
               min_lon=excluded.min_lon, min_lat=excluded.min_lat, max_lon=excluded.max_lon, max_lat=excluded.max_lat,
               start_time=excluded.start_time, end_time=excluded.end_time, status=excluded.status,
               evidence_type=excluded.evidence_type, attributes=excluded.attributes, raw=excluded.raw,
               provenance_id=excluded.provenance_id, updated_at=excluded.updated_at""",
        (
            f"{source['id']}:{record['record_id']}", source["id"], record["type"], record["category"],
            record["title"], record.get("description"), db.to_json(shape), lon, lat, *box,
            record.get("start_time"), record.get("end_time"), record.get("status"), record["evidence_type"],
            db.to_json(attributes), db.to_json(record.get("raw")), provenance_id, now, now,
        ),
    )


def _store_entity(connection, source: dict, record: dict, provenance_id: str) -> None:
    """Insert or replace one entity."""
    lon, lat = geometry.representative_point(record["geometry"])
    connection.execute(
        """INSERT OR REPLACE INTO entities (id, source_id, type, category, name, geometry, lon, lat,
               attributes, provenance_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"{source['id']}:{record['record_id']}", source["id"], record["type"], record["category"],
            record["name"], db.to_json(record["geometry"]), lon, lat,
            db.to_json(record.get("attributes")), provenance_id, db.now_iso(),
        ),
    )


def _write_reference_layer(source: dict, records: list[dict], provenance_id: str) -> None:
    """Reference layers (rivers, railways...) are written as one GeoJSON file each."""
    settings.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    collection = {
        "type": "FeatureCollection",
        "provenance_id": provenance_id,
        "features": [{"type": "Feature", "properties": r["properties"], "geometry": r["geometry"]} for r in records],
    }
    (settings.REFERENCE_DIR / f"{source['id']}.geojson").write_text(json.dumps(collection, separators=(",", ":")))


def _update_source_state(connection, source_id: str, **fields) -> None:
    """Create or update the live state row for a source."""
    connection.execute("INSERT OR IGNORE INTO sources (id) VALUES (?)", (source_id,))
    fields["updated_at"] = db.now_iso()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(f"UPDATE sources SET {assignments} WHERE id = ?", (*fields.values(), source_id))


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def _context(source: dict) -> dict:
    """Information adapters need: area bbox, time, API key, staged places."""
    places = boundary.load_boundary("india-places")
    return {
        "bbox": boundary.operating_bbox() or [60.0, -10.0, 100.0, 40.0],
        "now": datetime.now(timezone.utc),
        "source": source,
        "key": settings.get_setting(source.get("key_env", "")) if source.get("requires_key") else None,
        "places": places["features"] if places else [],
    }


def refresh_source(source_id: str, actor: str = "system") -> dict:
    """
    Run the full ingest path for one source and return a summary dict
    (the same facts written to source_runs).
    """
    connection = db.connect()
    source = registry.get_definition(source_id)
    if source is None:
        return {"source_id": source_id, "status": "failed", "error": "unknown source"}

    started = db.now_iso()
    mode = settings.operating_mode()
    summary = {"source_id": source_id, "mode": mode, "started_at": started, "status": "failed",
               "records_in": 0, "records_kept": 0, "records_outside": 0, "records_invalid": 0,
               "invalid_reasons": {}, "error": None}

    adapter = registry.load_adapter(source)
    context = _context(source)

    if adapter is None:
        summary.update(status="skipped", error="adapter not implemented")
        _update_source_state(connection, source_id, health="not-implemented")
    elif source.get("requires_key") and not context["key"]:
        summary.update(status="skipped", error=f"{source['key_env']} is not set")
        _update_source_state(connection, source_id, health="key-required")
    else:
        try:
            # 1-2. fetch + snapshot (or reuse the last snapshot offline)
            if mode == "connected":
                data = adapter.fetch(context)
                snapshot_path, checksum = save_snapshot(source, data)
                run_status, health = "success", "ok"
            else:
                path, data = latest_snapshot(source)
                if data is None:
                    raise net.OfflineError("air-gapped and no staged snapshot for this source")
                snapshot_path = str(path.relative_to(settings.ROOT_DIR))
                checksum = net.sha256_of_bytes(data)
                run_status, health = "offline-snapshot", "offline-snapshot"

            provenance_id = provenance.create(
                connection, kind="source-snapshot", source_id=source_id,
                input_ref=snapshot_path, input_sha256=checksum,
                processing="normalize, validate, scope to India operating area",
                processing_version=PROCESSING_VERSION,
                parameters={"adapter": source["adapter"], "endpoint": source["endpoint"].split("{")[0]},
                retrieved_at=started,
            )

            # 3. normalize
            records = adapter.normalize(data, context)
            summary["records_in"] = len(records)

            # 4-5. validate and scope
            kept = []
            for record in records:
                reason = validate(record)
                if reason is None:
                    where = scope(record)
                    if where == "outside":
                        summary["records_outside"] += 1
                        continue
                    if where == "suspect-swap":
                        reason = "suspected latitude/longitude swap in source data"
                if reason is not None:
                    summary["records_invalid"] += 1
                    summary["invalid_reasons"][reason] = summary["invalid_reasons"].get(reason, 0) + 1
                    continue
                kept.append((record, where))
            summary["records_kept"] = len(kept)

            # 6. store
            if source["kind"] == "reference":
                _write_reference_layer(source, [r for r, _ in kept], provenance_id)
            else:
                if source.get("mode") == "replace":
                    table = "events" if source["kind"] == "events" else "entities"
                    connection.execute(f"DELETE FROM {table} WHERE source_id = ?", (source_id,))
                for record, where in kept:
                    if record["kind"] == "event":
                        _store_event(connection, source, record, where, provenance_id)
                    else:
                        _store_entity(connection, source, record, provenance_id)

            # 7. record state
            times = [r.get("start_time") for r, _ in kept if r.get("start_time")]
            table = {"events": "events", "entities": "entities"}.get(source["kind"])
            count = len(kept) if table is None else connection.execute(f"SELECT COUNT(*) FROM {table} WHERE source_id = ?", (source_id,)).fetchone()[0]
            state = {"health": health, "last_error": None, "last_snapshot": snapshot_path,
                     "last_snapshot_sha256": checksum, "last_data_time": max(times) if times else None, "record_count": count}
            # "Last success" means the last successful DOWNLOAD. Re-reading an
            # old snapshot offline must not make the data look fresher.
            if run_status == "success":
                state["last_success"] = started
                state["consecutive_failures"] = 0
                state["next_allowed_at"] = None
            meta = context.get("response_meta") or {}
            if "rate_limit_remaining" in meta:
                state["rate_limit_remaining"] = meta["rate_limit_remaining"]
            _update_source_state(connection, source_id, **state)
            summary.update(status=run_status, snapshot_path=snapshot_path, snapshot_sha256=checksum, provenance_id=provenance_id)
        except Exception as error:  # any failure: record it, keep old data
            # Stored positions are NOT deleted: the map keeps the last-known
            # data, which then ages into STALE honestly.
            summary["error"] = f"{type(error).__name__}: {error}"
            row = connection.execute("SELECT consecutive_failures FROM sources WHERE id = ?", (source_id,)).fetchone()
            failures = ((row[0] if row else 0) or 0) + 1
            failure_state = {"health": "failed", "last_failure": started, "last_error": summary["error"],
                             "consecutive_failures": failures}
            retry_after = getattr(error, "retry_after_s", None)
            if retry_after is not None:
                failure_state["health"] = "rate-limited"
                failure_state["next_allowed_at"] = db.iso_after_seconds(retry_after)
                summary["retry_after_s"] = retry_after
            meta = context.get("response_meta") or {}
            if meta.get("rate_limit_remaining") is not None:
                failure_state["rate_limit_remaining"] = meta["rate_limit_remaining"]
            _update_source_state(connection, source_id, **failure_state)

    summary["finished_at"] = db.now_iso()
    connection.execute(
        """INSERT INTO source_runs (source_id, started_at, finished_at, mode, status, records_in, records_kept,
               records_outside, records_invalid, snapshot_path, snapshot_sha256, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, started, summary["finished_at"], mode, summary["status"], summary["records_in"],
         summary["records_kept"], summary["records_outside"], summary["records_invalid"],
         summary.get("snapshot_path"), summary.get("snapshot_sha256"), summary["error"]),
    )
    connection.commit()
    audit.record(connection, actor, "source-refresh", source_id, {
        "status": summary["status"], "kept": summary["records_kept"], "error": summary["error"],
    })
    connection.close()
    return summary
