"""
Planetary K-index (geomagnetic activity) from NOAA SWPC.

Kp is a single global number every 3 hours (0 = quiet, 9 = extreme storm).
It is context for satellite and HF-radio operations, not an India-specific
event, so records use scope "global".
"""

import json

from .. import net
from .common import iso_from_text, make_event


def fetch(context) -> bytes:
    """Download the last week of 3-hourly Kp values."""
    return net.fetch_bytes("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json")


def normalize(data: bytes, context) -> list[dict]:
    """One global-scope event per 3-hour Kp value."""
    rows = json.loads(data)
    # Older versions of this product were a table with a header row;
    # the current one is a list of objects. Support both.
    if rows and isinstance(rows[0], list):
        header = rows[0]
        rows = [dict(zip(header, row)) for row in rows[1:]]
    records = []
    for row in rows:
        kp = float(row["Kp"])
        records.append(make_event(
            record_id=row["time_tag"],
            event_type="geomagnetic-kp",
            category="SPACE",
            title=f"Planetary Kp {kp:.2f}" + (" (storm level)" if kp >= 5 else ""),
            geometry=None,
            start_time=iso_from_text(row["time_tag"]),  # SWPC time tags are UTC
            status="observed",
            evidence_type="OBSERVED",
            attributes={"kp": kp, "station_count": row.get("station_count")},
            raw=row,
            scope="global",
        ))
    return records
