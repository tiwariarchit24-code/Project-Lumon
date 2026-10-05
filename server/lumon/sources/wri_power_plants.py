"""
Power plants in India from the WRI Global Power Plant Database v1.3.

The database stopped being updated in 2021, so capacities and plant lists
may be out of date; the UI shows the data year next to every plant.
"""

import csv
import io
import zipfile

from .. import net
from .common import make_entity, point


def fetch(context) -> bytes:
    """Download the zipped global CSV (~4 MB)."""
    return net.fetch_bytes(context["source"]["endpoint"], timeout=180)


def _number(text):
    """Convert a CSV cell to float, or None when empty."""
    return float(text) if text not in (None, "") else None


def normalize(data: bytes, context) -> list[dict]:
    """One power-plant entity per Indian plant."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    csv_name = next(name for name in archive.namelist() if name.endswith("global_power_plant_database.csv"))
    text = archive.read(csv_name).decode("utf-8")
    records = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["country"] != "IND":
            continue
        records.append(make_entity(
            record_id=row["gppd_idnr"],
            entity_type="power-plant",
            category="INFRASTRUCTURE",
            name=row["name"],
            geometry=point(row["longitude"], row["latitude"]),
            attributes={
                "primary_fuel": row["primary_fuel"],
                "capacity_mw": _number(row["capacity_mw"]),
                "commissioning_year": _number(row["commissioning_year"]),
                "owner": row["owner"] or None,
                "capacity_data_year": _number(row["year_of_capacity_data"]),
                "geolocation_source": row["geolocation_source"] or None,
                "original_source": row["source"] or None,
            },
            raw={key: row[key] for key in ("gppd_idnr", "name", "capacity_mw", "primary_fuel", "latitude", "longitude", "source", "url")},
        ))
    return records
