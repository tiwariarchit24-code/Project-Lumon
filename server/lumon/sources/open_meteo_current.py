"""
Current weather at India's largest cities from Open-Meteo.

Open-Meteo returns numerical weather model analysis, NOT readings from a
physical weather station, so the records are labelled INFERRED.
The list of cities comes from the staged Natural Earth places dataset,
so no coordinates are hard-coded here.
"""

import json
import urllib.parse

from .. import net
from .common import iso_from_text, make_event, point

MAX_CITIES = 40
CURRENT_FIELDS = [
    "temperature_2m", "relative_humidity_2m", "precipitation", "cloud_cover",
    "pressure_msl", "wind_speed_10m", "wind_direction_10m", "weather_code",
]


def _largest_cities(places: list[dict]) -> list[dict]:
    """Pick the most populous places so the request stays small."""
    ranked = sorted(places, key=lambda place: place["properties"].get("population") or 0, reverse=True)
    return ranked[:MAX_CITIES]


def fetch(context) -> bytes:
    """
    One request for all cities (Open-Meteo accepts comma-separated lists).
    The snapshot also stores which cities were requested, because the
    response itself only contains coordinates.
    """
    cities = _largest_cities(context["places"])
    if not cities:
        raise RuntimeError("No staged places: run stage-boundaries first.")
    latitudes = ",".join(f"{city['geometry']['coordinates'][1]:.4f}" for city in cities)
    longitudes = ",".join(f"{city['geometry']['coordinates'][0]:.4f}" for city in cities)
    query = urllib.parse.urlencode({"latitude": latitudes, "longitude": longitudes, "current": ",".join(CURRENT_FIELDS), "timezone": "GMT"})
    response = json.loads(net.fetch_bytes("https://api.open-meteo.com/v1/forecast?" + query))
    if isinstance(response, dict):  # a single location comes back as an object, not a list
        response = [response]
    wrapped = {"cities": [{"name": c["properties"]["name"], "state": c["properties"].get("state"), "coordinates": c["geometry"]["coordinates"]} for c in cities], "response": response}
    return json.dumps(wrapped).encode("utf-8")


def normalize(data: bytes, context) -> list[dict]:
    """One weather-observation event per city, at the city's coordinates."""
    wrapped = json.loads(data)
    records = []
    for city, result in zip(wrapped["cities"], wrapped["response"]):
        current = result.get("current") or {}
        units = result.get("current_units") or {}
        lon, lat = city["coordinates"]
        records.append(make_event(
            record_id=f"{city['name']}-{current.get('time')}",
            event_type="weather-current",
            category="WEATHER",
            title=f"{city['name']}: {current.get('temperature_2m')}{units.get('temperature_2m', '')}, wind {current.get('wind_speed_10m')} {units.get('wind_speed_10m', '')}",
            geometry=point(lon, lat),
            start_time=iso_from_text(current.get("time")),  # requested in GMT
            status="model-analysis",
            evidence_type="INFERRED",
            attributes={**{field: current.get(field) for field in CURRENT_FIELDS}, "units": units, "city": city["name"], "state": city.get("state")},
            raw=result,
        ))
    return records
