"""
GDACS disaster alerts (floods, cyclones, earthquakes, droughts, wildfires,
volcanoes).

The EVENTS4APP list is global, so we download it whole and lumon.ingest
keeps only alerts whose location is inside the India operating area.
GDACS alert levels (Green / Orange / Red) are model-based estimates of
humanitarian impact, so these records are labelled INFERRED.
"""

import json

from .. import net
from .common import iso_from_text, make_event

# GDACS event type code -> (Lumon event type, Lumon layer group)
TYPE_MAP = {
    "EQ": ("earthquake", "DISASTERS"),
    "TC": ("cyclone", "WEATHER"),
    "FL": ("flood", "DISASTERS"),
    "DR": ("drought", "ENVIRONMENT"),
    "WF": ("wildfire", "ENVIRONMENT"),
    "VO": ("volcano", "DISASTERS"),
}


def fetch(context) -> bytes:
    """Download the current global alert list."""
    return net.fetch_bytes("https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP", timeout=90)


def normalize(data: bytes, context) -> list[dict]:
    """One event per GDACS alert, located at the alert's reference point."""
    records = []
    for feature in json.loads(data)["features"]:
        properties = feature["properties"]
        event_type, group = TYPE_MAP.get(properties.get("eventtype"), ("disaster-alert", "DISASTERS"))
        url_info = properties.get("url") or {}
        records.append(make_event(
            record_id=f"{properties.get('eventtype')}-{properties.get('eventid')}-{properties.get('episodeid')}",
            event_type=event_type,
            category=group,
            title=properties.get("name") or properties.get("description") or "GDACS alert",
            description=properties.get("htmldescription"),
            geometry=feature.get("geometry"),
            # GDACS dates have no zone; the GDACS API documents them as UTC.
            start_time=iso_from_text(properties.get("fromdate")),
            end_time=iso_from_text(properties.get("todate")),
            status="current" if properties.get("iscurrent") == "true" else "past",
            evidence_type="INFERRED",
            attributes={
                "alert_level": properties.get("alertlevel"),
                "alert_score": properties.get("alertscore"),
                "country": properties.get("country"),
                "upstream_source": properties.get("source"),
                "source_url": url_info.get("report"),
            },
            raw=feature,
        ))
    return records
