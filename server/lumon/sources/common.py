"""
Helpers shared by the source adapters.

Every adapter module in this folder provides two functions:

    fetch(context) -> bytes
        Download the raw data (through lumon.net, so the air-gap rule is
        enforced). The bytes are saved untouched as a snapshot.

    normalize(data, context) -> list[dict]
        Turn the raw bytes into simple normalized records (see the
        make_event / make_entity / make_reference helpers below).

`context` is a dict prepared by lumon.ingest with:
    bbox   - [min_lon, min_lat, max_lon, max_lat] of the India operating area
    now    - current UTC datetime
    source - the registry entry for this source
    key    - API key for sources that need one (never logged)
    places - staged populated places (for sources that query by city)
"""

from datetime import datetime, timezone


def iso_from_epoch_seconds(seconds) -> str | None:
    """Convert a Unix timestamp in seconds to an ISO 8601 UTC string."""
    if seconds is None:
        return None
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_from_epoch_ms(milliseconds) -> str | None:
    """Convert a Unix timestamp in milliseconds to an ISO 8601 UTC string."""
    if milliseconds is None:
        return None
    return iso_from_epoch_seconds(float(milliseconds) / 1000)


def iso_from_text(text, assume_utc: bool = True) -> str | None:
    """
    Normalise a date/time string to ISO 8601 UTC ('...Z').

    Sources write times in slightly different ways ('2026-10-05T01:00:00',
    '2026-10-04T08:15', '2026-09-23T20:00:00Z'). Times without a zone are
    treated as UTC only when the source documents them as UTC
    (assume_utc=True); otherwise None is returned rather than guessing.
    """
    if not text:
        return None
    value = str(text).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not assume_utc:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def point(lon, lat) -> dict:
    """A GeoJSON Point geometry."""
    return {"type": "Point", "coordinates": [float(lon), float(lat)]}


def make_event(record_id, event_type, category, title, geometry, start_time,
               end_time=None, status=None, description=None, evidence_type="OBSERVED",
               attributes=None, raw=None, scope="spatial") -> dict:
    """
    Build one normalized EVENT record.

    - record_id:  the id used by the source (we prefix it with the source id later)
    - geometry:   GeoJSON geometry, or None for non-spatial records
    - scope:      "spatial" (has a location and must be inside India),
                  "national" (about India as a whole, no precise location), or
                  "global" (worldwide context such as space weather)
    - attributes: source-specific values worth showing (magnitude, alert level...)
    - raw:        the original source record, kept for provenance
    """
    return {
        "kind": "event",
        "record_id": str(record_id),
        "type": event_type,
        "category": category,
        "title": title,
        "description": description,
        "geometry": geometry,
        "start_time": start_time,
        "end_time": end_time,
        "status": status,
        "evidence_type": evidence_type,
        "attributes": attributes or {},
        "raw": raw,
        "scope": scope,
    }


def make_entity(record_id, entity_type, category, name, geometry, attributes=None, raw=None) -> dict:
    """Build one normalized ENTITY record (a long-lived thing such as an airport)."""
    return {
        "kind": "entity",
        "record_id": str(record_id),
        "type": entity_type,
        "category": category,
        "name": name,
        "geometry": geometry,
        "attributes": attributes or {},
        "raw": raw,
    }


def make_reference(geometry, properties) -> dict:
    """Build one REFERENCE feature (a line/polygon drawn as a map layer, e.g. a river)."""
    return {"kind": "reference", "geometry": geometry, "properties": properties}
