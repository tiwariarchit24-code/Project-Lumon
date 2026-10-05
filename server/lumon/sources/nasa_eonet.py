"""
NASA EONET (Earth Observatory Natural Event Tracker) events.

EONET is a curated catalogue of natural events (wildfires, storms, floods,
volcanoes...). Each event has one or more dated geometries; we keep the
most recent geometry as the event location and the first date as start.
"""

import json

from .. import net
from .common import iso_from_text, make_event

# EONET category id -> (Lumon event type, Lumon layer group)
CATEGORY_MAP = {
    "wildfires": ("wildfire", "ENVIRONMENT"),
    "floods": ("flood", "DISASTERS"),
    "severeStorms": ("severe-storm", "WEATHER"),
    "volcanoes": ("volcano", "DISASTERS"),
    "landslides": ("landslide", "DISASTERS"),
    "earthquakes": ("earthquake", "DISASTERS"),
    "drought": ("drought", "ENVIRONMENT"),
    "dustHaze": ("dust-haze", "ENVIRONMENT"),
    "seaLakeIce": ("sea-lake-ice", "ENVIRONMENT"),
    "snow": ("snow", "WEATHER"),
    "tempExtremes": ("temperature-extreme", "WEATHER"),
    "waterColor": ("water-colour", "ENVIRONMENT"),
    "manmade": ("manmade", "PUBLIC EVENTS"),
}


def fetch(context) -> bytes:
    """Download events of the last 60 days inside the operating-area bounding box."""
    min_lon, min_lat, max_lon, max_lat = context["bbox"]
    # EONET expects bbox as min_lon, max_lat, max_lon, min_lat (upper-left, lower-right).
    url = f"https://eonet.gsfc.nasa.gov/api/v3/events?status=all&days=60&bbox={min_lon:.3f},{max_lat:.3f},{max_lon:.3f},{min_lat:.3f}"
    return net.fetch_bytes(url, timeout=90)


def normalize(data: bytes, context) -> list[dict]:
    """One event per EONET event, located at its latest reported geometry."""
    records = []
    for event in json.loads(data)["events"]:
        geometries = event.get("geometry") or []
        if not geometries:
            continue
        category_id = event["categories"][0]["id"] if event.get("categories") else "other"
        event_type, group = CATEGORY_MAP.get(category_id, ("natural-event", "DISASTERS"))
        latest = geometries[-1]
        records.append(make_event(
            record_id=event["id"],
            event_type=event_type,
            category=group,
            title=event["title"],
            description=event.get("description"),
            geometry={"type": latest["type"], "coordinates": latest["coordinates"]},
            start_time=iso_from_text(geometries[0].get("date")),
            end_time=iso_from_text(event.get("closed")),
            status="closed" if event.get("closed") else "open",
            evidence_type="OBSERVED",
            attributes={
                "eonet_category": category_id,
                "magnitude": latest.get("magnitudeValue"),
                "magnitude_unit": latest.get("magnitudeUnit"),
                "geometry_count": len(geometries),
                "upstream_sources": [source.get("id") for source in event.get("sources", [])],
                "source_url": event.get("link"),
            },
            raw=event,
        ))
    return records
