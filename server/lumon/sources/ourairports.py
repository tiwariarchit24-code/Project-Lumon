"""
Airports in India from OurAirports (public domain, community maintained).

The published CSV is global (~12 MB); we keep large, medium and small
airports whose iso_country is IN.
"""

import csv
import io

from .. import net
from .common import make_entity, point

KEPT_TYPES = {"large_airport", "medium_airport", "small_airport"}


def fetch(context) -> bytes:
    """Download the global airports CSV."""
    return net.fetch_bytes("https://davidmegginson.github.io/ourairports-data/airports.csv", timeout=120)


def normalize(data: bytes, context) -> list[dict]:
    """One airport entity per Indian airport of a kept type."""
    records = []
    for row in csv.DictReader(io.StringIO(data.decode("utf-8"))):
        if row["iso_country"] != "IN" or row["type"] not in KEPT_TYPES:
            continue
        records.append(make_entity(
            record_id=row["ident"],
            entity_type="airport",
            category="AVIATION",
            name=row["name"],
            geometry=point(row["longitude_deg"], row["latitude_deg"]),
            attributes={
                "airport_type": row["type"].replace("_", " "),
                "iata": row["iata_code"] or None,
                "icao": row["gps_code"] or None,
                "municipality": row["municipality"] or None,
                "region": row["iso_region"],
                "scheduled_service": row["scheduled_service"] == "yes",
                "elevation_ft": int(row["elevation_ft"]) if row["elevation_ft"] else None,
            },
            raw=row,
        ))
    return records
