"""
OSINT routes: sources, events, entities and the timeline.

All data is served from the local database. The browser never talks to a
third-party API; only a source refresh (backend, connected mode) does.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from .. import db, provenance, settings, worker
from ..event_kinds import SCHEDULED_TYPES, TELEMETRY_TYPES
from ..geo import geometry
from ..sources import registry

router = APIRouter(prefix="/api")


def _layer_by_type() -> dict:
    """event/entity type -> map layer id, from config/layers.json."""
    config = json.loads((settings.CONFIG_DIR / "layers.json").read_text())
    mapping = {}
    for layer in config["layers"]:
        for item_type in layer.get("types", []):
            mapping.setdefault(item_type, layer["id"])
    return mapping


def _parse_bbox(bbox: str | None) -> list[float] | None:
    if not bbox:
        return None
    values = [float(v) for v in bbox.split(",")]
    if len(values) != 4:
        raise HTTPException(400, "bbox must be min_lon,min_lat,max_lon,max_lat")
    return values


@router.get("/sources")
def sources():
    """Source cards: static facts + live health."""
    connection = db.connect()
    result = registry.describe_all(connection)
    for source in result:
        runs = connection.execute(
            "SELECT status, started_at, records_in, records_kept, records_outside, records_invalid, error FROM source_runs WHERE source_id = ? ORDER BY id DESC LIMIT 1",
            (source["id"],)).fetchone()
        source["last_run"] = dict(runs) if runs else None
    connection.close()
    return result


@router.get("/live/status")
def live_status():
    """Automatic live polling (OpenSky): NOT CONFIGURED / DISABLED / POLLING / BACKING OFF, with reasons."""
    from .. import live_poll
    return {"aircraft": live_poll.status(),
            "vessels": {"source_id": "aisstream-vessels", "state": "NOT CONFIGURED",
                        "problems": ["No AIS feed is connected: the aisstream.io adapter is not implemented (needs AISSTREAM_API_KEY and a WebSocket client)"]}}


@router.post("/sources/{source_id}/refresh")
def refresh(source_id: str):
    """Queue a refresh. In air-gapped mode the job re-reads the last snapshot."""
    if registry.get_definition(source_id) is None:
        raise HTTPException(404, "unknown source")
    return {"job_id": worker.enqueue("source-refresh", {"source_id": source_id, "actor": "analyst"}), "mode": settings.operating_mode()}


def _event_filter(bbox: str | None, start: str | None, end: str | None, types: str | None) -> tuple[str, list]:
    """
    The WHERE clause shared by the map (/api/events) and the timeline, so
    both always select exactly the same events:
      - located events only (lon IS NOT NULL)
      - inside the viewport bbox, if given
      - overlapping the time window: ended (or started) after `start` and
        started before `end`
      - of the requested types
    """
    sql = "lon IS NOT NULL"
    params = []
    box = _parse_bbox(bbox)
    if box:
        sql += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        params += [box[0], box[2], box[1], box[3]]
    if start:
        sql += " AND COALESCE(end_time, start_time) >= ?"
        params.append(start)
    if end:
        sql += " AND start_time <= ?"
        params.append(end)
    if types:
        type_list = types.split(",")
        sql += f" AND type IN ({','.join('?' * len(type_list))})"
        params += type_list
    return sql, params


def _stale_limits(connection) -> dict:
    """source id -> stale_after_hours, from the registry."""
    return {s["id"]: s.get("stale_after_hours") for s in registry.load_definitions()}


def _hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600


def _feature(row, layer_of: dict, stale_after: dict, scheduled: bool = False) -> dict:
    """
    One map point. Extra honesty flags:
      stale      telemetry (aircraft, satellites, model weather) older than
                 its source's stale_after_hours - the position is out of date
      age_hours  how old that telemetry is
      scheduled  a planned future event (e.g. a launch), not something that
                 has happened
      time is the OBSERVATION time reported by the source; ingested_at is
      when Lumon stored the record. They are never merged.
    """
    attributes = json.loads(row["attributes"] or "{}")
    age = _hours_since(row["start_time"]) if row["type"] in TELEMETRY_TYPES else None
    limit = stale_after.get(row["source_id"])
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
        "properties": {
            "id": row["id"], "type": row["type"], "category": row["category"], "title": row["title"],
            "time": row["start_time"], "layer": layer_of.get(row["type"], "other"),
            "evidence_type": row["evidence_type"], "source_id": row["source_id"],
            "magnitude": attributes.get("magnitude"), "track": attributes.get("track_deg"),
            "alert": attributes.get("alert_level"),
            "stale": bool(age is not None and limit is not None and age > limit),
            "age_hours": round(age, 2) if age is not None else None,
            "scheduled": scheduled,
            "time_precision": attributes.get("time_precision"),
            "ingested_at": row["updated_at"] if "updated_at" in row.keys() else None,
        },
    }


@router.get("/events")
def events(bbox: str | None = None, start: str | None = None, end: str | None = None,
           types: str | None = None, limit: int = 5000, upcoming_days: int = 0):
    """
    Events with a location, as GeoJSON points for the map. Only what is in
    the requested viewport (bbox), time window and types is returned.

    - upcoming_days: also return SCHEDULED events (launches) planned between
      now and now + upcoming_days. They come back with scheduled=true and
      never mix into the historical window, which still ends at `end`.
    - The response also carries `total` (matches before the limit), so a
      list can say "200 of 241" instead of hiding the cap.
    """
    where, params = _event_filter(bbox, start, end, types)
    connection = db.connect()
    total = connection.execute(f"SELECT COUNT(*) FROM events WHERE {where}", params).fetchone()[0]
    layer_of = _layer_by_type()
    stale_after = _stale_limits(connection)
    columns = "id, type, category, title, start_time, end_time, lon, lat, evidence_type, source_id, attributes, updated_at"
    features = [_feature(row, layer_of, stale_after) for row in connection.execute(
        f"SELECT {columns} FROM events WHERE {where} ORDER BY start_time DESC LIMIT ?", params + [limit])]

    upcoming_total = 0
    if upcoming_days > 0:
        now = datetime.now(timezone.utc)
        horizon = (now + timedelta(days=upcoming_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future_where, future_params = _event_filter(bbox, None, None, types)
        scheduled = list(SCHEDULED_TYPES)
        future_where += f" AND type IN ({','.join('?' * len(scheduled))}) AND start_time > ? AND start_time <= ?"
        future_params += scheduled + [now.strftime("%Y-%m-%dT%H:%M:%SZ"), horizon]
        rows = connection.execute(f"SELECT {columns} FROM events WHERE {future_where} ORDER BY start_time", future_params).fetchall()
        upcoming_total = len(rows)
        features += [_feature(row, layer_of, stale_after, scheduled=True) for row in rows]
    connection.close()
    return {"type": "FeatureCollection", "features": features, "total": total, "returned": len(features),
            "upcoming_total": upcoming_total}


@router.get("/events/nonspatial")
def nonspatial_events(start: str | None = None, end: str | None = None, types: str | None = None, limit: int = 500):
    """National/global events without a location (internet outages, space weather)."""
    sql = "SELECT id, type, category, title, start_time, end_time, evidence_type, source_id, attributes FROM events WHERE lon IS NULL"
    params = []
    if start:
        sql += " AND COALESCE(end_time, start_time) >= ?"
        params.append(start)
    if end:
        sql += " AND start_time <= ?"
        params.append(end)
    if types:
        type_list = types.split(",")
        sql += f" AND type IN ({','.join('?' * len(type_list))})"
        params += type_list
    connection = db.connect()
    rows = [db.row_to_dict(r, ["attributes"]) for r in connection.execute(sql + " ORDER BY start_time DESC LIMIT ?", params + [limit])]
    connection.close()
    return rows


def _source_card(source_id: str, connection) -> dict | None:
    for source in registry.describe_all(connection):
        if source["id"] == source_id:
            return {k: source[k] for k in ("id", "name", "provider", "license", "attribution", "health", "last_success",
                                           "data_age_hours", "coverage", "connection", "docs_url",
                                           "freshness_status", "freshness_label", "freshness_age_hours")}
    return None


@router.get("/events/{event_id:path}")
def event_detail(event_id: str):
    """One event with its raw record, source card, provenance and nearby related events."""
    connection = db.connect()
    row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(404, "event not found")
    event = db.row_to_dict(row, ["attributes", "raw"])
    event["geometry"] = db.from_json(event["geometry"])
    event["source"] = _source_card(event["source_id"], connection)
    event["provenance"] = provenance.get(connection, event["provenance_id"]) if event["provenance_id"] else None
    related = []
    if event["lon"] is not None:
        pad = 1.0  # ~100 km pre-filter
        for other in connection.execute(
                "SELECT id, title, type, start_time, lon, lat FROM events WHERE id != ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
                (event_id, event["lon"] - pad, event["lon"] + pad, event["lat"] - pad, event["lat"] + pad)):
            distance = geometry.haversine_m(event["lon"], event["lat"], other["lon"], other["lat"])
            if distance <= 100_000 and other["type"] != "weather-current":
                related.append({**dict(other), "distance_m": round(distance)})
        related.sort(key=lambda r: r["distance_m"])
    event["related_events"] = related[:15]
    # Nearest staged satellite AOI, for the OSINT -> imagery fusion workflow.
    aoi_row = None
    if event["lon"] is not None:
        from ..imagery import aoi as aoi_module
        for aoi in aoi_module.load_aois():
            min_lon, min_lat, max_lon, max_lat = aoi["bbox"]
            centre = ((min_lon + max_lon) / 2, (min_lat + max_lat) / 2)
            d = geometry.haversine_m(event["lon"], event["lat"], *centre)
            if aoi_row is None or d < aoi_row["distance_m"]:
                aoi_row = {"id": aoi["id"], "name": aoi["name"], "distance_m": round(d)}
    event["nearest_aoi"] = aoi_row
    connection.close()
    return event


@router.get("/entities")
def entities(types: str | None = None, bbox: str | None = None, limit: int = 5000):
    """Entities (airports, ports, power plants) as GeoJSON points."""
    sql = "SELECT id, type, category, name, lon, lat, source_id, attributes FROM entities WHERE 1 = 1"
    params = []
    if types:
        type_list = types.split(",")
        sql += f" AND type IN ({','.join('?' * len(type_list))})"
        params += type_list
    box = _parse_bbox(bbox)
    if box:
        sql += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        params += [box[0], box[2], box[1], box[3]]
    layer_of = _layer_by_type()
    connection = db.connect()
    features = []
    for row in connection.execute(sql + " LIMIT ?", params + [limit]):
        attributes = json.loads(row["attributes"] or "{}")
        features.append({
            "type": "Feature", "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": {"id": row["id"], "type": row["type"], "category": row["category"], "title": row["name"],
                           "layer": layer_of.get(row["type"], "other"), "source_id": row["source_id"],
                           "size": attributes.get("airport_type") or attributes.get("capacity_mw")},
        })
    connection.close()
    return {"type": "FeatureCollection", "features": features}


@router.get("/entities/{entity_id:path}")
def entity_detail(entity_id: str):
    """One entity with source card, provenance and recent events within 50 km."""
    connection = db.connect()
    row = connection.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(404, "entity not found")
    entity = db.row_to_dict(row, ["attributes"])
    entity["geometry"] = db.from_json(entity["geometry"])
    entity["source"] = _source_card(entity["source_id"], connection)
    entity["provenance"] = provenance.get(connection, entity["provenance_id"]) if entity["provenance_id"] else None
    nearby = []
    for other in connection.execute(
            "SELECT id, title, type, start_time, lon, lat FROM events WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND type NOT IN ('weather-current')",
            (entity["lon"] - 0.6, entity["lon"] + 0.6, entity["lat"] - 0.6, entity["lat"] + 0.6)):
        distance = geometry.haversine_m(entity["lon"], entity["lat"], other["lon"], other["lat"])
        if distance <= 50_000:
            nearby.append({**dict(other), "distance_m": round(distance)})
    nearby.sort(key=lambda r: r["distance_m"])
    entity["related_events"] = nearby[:15]
    # Same-type entities nearby ("similar entities" by type and proximity).
    similar = []
    for other in connection.execute("SELECT id, name, lon, lat FROM entities WHERE type = ? AND id != ?", (entity["type"], entity_id)):
        similar.append({"id": other["id"], "name": other["name"], "distance_m": round(geometry.haversine_m(entity["lon"], entity["lat"], other["lon"], other["lat"]))})
    similar.sort(key=lambda r: r["distance_m"])
    entity["similar_entities"] = similar[:5]
    connection.close()
    return entity


@router.get("/timeline")
def timeline(start: str, end: str, types: str | None = None, bbox: str | None = None, upcoming_days: int = 0):
    """
    Data for the bottom timeline, split into separate bands so hidden
    telemetry cannot distort the main picture:

      density    per day: the events the MAP shows for the same window,
                 types and viewport (same filter as /api/events). Events that
                 started before the window but are still ongoing are counted
                 on the window's first day, exactly as the map shows them.
      telemetry  per day: aircraft / satellite / model-weather / Kp records
                 and national-scope signals (internet outages) for the whole
                 operating area, whether or not their layers are switched on
      scenes     dates of staged satellite observations
      changes    earliest-supported dates of accepted change events
      upcoming   scheduled events (launches) between now and now + upcoming_days
    """
    connection = db.connect()
    main_types = ",".join(t for t in (types or "").split(",") if t and t not in TELEMETRY_TYPES) or None
    density = {}
    if main_types:
        where, params = _event_filter(bbox, start, end, main_types)
        for row in connection.execute(
                f"SELECT MAX(substr(start_time, 1, 10), ?) AS day, category, COUNT(*) AS n FROM events WHERE {where} GROUP BY day, category",
                [start[:10]] + params):
            density.setdefault(row["day"], {})[row["category"]] = row["n"]

    telemetry = {}
    telemetry_types = list(TELEMETRY_TYPES)
    for row in connection.execute(
            f"""SELECT MAX(substr(start_time, 1, 10), ?) AS day, COUNT(*) AS n FROM events
                WHERE (type IN ({','.join('?' * len(telemetry_types))}) OR lon IS NULL)
                AND COALESCE(end_time, start_time) >= ? AND start_time <= ? GROUP BY day""",
            [start[:10]] + telemetry_types + [start, end]):
        telemetry[row["day"]] = row["n"]

    scenes = [r[0][:10] for r in connection.execute(
        "SELECT acquired_at FROM scenes WHERE quality_status IN ('usable','degraded') AND acquired_at >= ? AND acquired_at <= ? ORDER BY acquired_at", (start, end))]
    changes = [dict(r) for r in connection.execute(
        "SELECT id, change_class, earliest_supported_after AS day FROM change_candidates WHERE status = 'accepted' AND earliest_supported_after >= ? AND earliest_supported_after <= ?",
        (start[:10], end[:10]))]

    upcoming = []
    if upcoming_days > 0:
        now = datetime.now(timezone.utc)
        scheduled = [t for t in SCHEDULED_TYPES if not types or t in types.split(",")]
        if scheduled:
            where, params = _event_filter(bbox, None, None, ",".join(scheduled))
            upcoming = [dict(r) for r in connection.execute(
                f"SELECT id, title, start_time FROM events WHERE {where} AND start_time > ? AND start_time <= ? ORDER BY start_time",
                params + [now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now + timedelta(days=upcoming_days)).strftime("%Y-%m-%dT%H:%M:%SZ")])]
    connection.close()
    return {"start": start, "end": end, "density": density, "telemetry": telemetry, "scenes": scenes,
            "changes": changes, "upcoming": upcoming}
