"""
USGS earthquakes for the India operating area.

API: FDSN event web service, GeoJSON output.
We ask for the last 30 days, magnitude 2.5 and above, inside the bounding
box of the operating area. The bounding box is larger than India, so
lumon.ingest later drops epicentres outside the real boundary.
"""

import json
from datetime import timedelta

from .. import net
from .common import iso_from_epoch_ms, make_event


def fetch(context) -> bytes:
    """Download recent earthquakes inside the operating-area bounding box."""
    min_lon, min_lat, max_lon, max_lat = context["bbox"]
    start = (context["now"] - timedelta(days=30)).strftime("%Y-%m-%d")
    url = (
        "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
        f"&starttime={start}&minmagnitude=2.5"
        f"&minlatitude={min_lat:.3f}&maxlatitude={max_lat:.3f}"
        f"&minlongitude={min_lon:.3f}&maxlongitude={max_lon:.3f}"
    )
    return net.fetch_bytes(url)


def normalize(data: bytes, context) -> list[dict]:
    """
    One event per earthquake.

    USGS marks each event "reviewed" (checked by a seismologist) or
    "automatic". Only reviewed events are labelled VERIFIED RECORD.
    """
    records = []
    for feature in json.loads(data)["features"]:
        properties = feature["properties"]
        lon, lat, depth_km = feature["geometry"]["coordinates"][:3]
        reviewed = properties.get("status") == "reviewed"
        records.append(make_event(
            record_id=feature["id"],
            event_type="earthquake",
            category="DISASTERS",
            title=f"M{properties['mag']} earthquake - {properties.get('place') or 'location text not given'}",
            geometry={"type": "Point", "coordinates": [lon, lat]},
            start_time=iso_from_epoch_ms(properties["time"]),
            status=properties.get("status"),
            evidence_type="VERIFIED RECORD" if reviewed else "OBSERVED",
            attributes={
                "magnitude": properties.get("mag"),
                "magnitude_type": properties.get("magType"),
                "depth_km": depth_km,
                "tsunami_flag": properties.get("tsunami"),
                "alert": properties.get("alert"),
                "source_url": properties.get("url"),
            },
            raw=feature,
        ))
    return records
