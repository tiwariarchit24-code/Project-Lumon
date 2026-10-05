"""
Active fire detections over India from NASA FIRMS (VIIRS S-NPP, near real time).

Requires a free FIRMS MAP_KEY (set NASA_FIRMS_MAP_KEY in .env.local).
Without a key the source reports "key required" and stays empty.

Note: the `confidence` column (low / nominal / high) is FIRMS' own
detection-quality flag. We store it as `source_confidence` and never turn
it into a Lumon confidence number.
"""

import csv
import io

from .. import net
from .common import iso_from_text, make_event, point

CONFIDENCE_NAMES = {"l": "low", "n": "nominal", "h": "high"}


def fetch(context) -> bytes:
    """Download the last 2 days of VIIRS detections for India (country code IND)."""
    if not context.get("key"):
        raise RuntimeError("NASA_FIRMS_MAP_KEY is not set")
    url = f"https://firms.modaps.eosdis.nasa.gov/api/country/csv/{context['key']}/VIIRS_SNPP_NRT/IND/2"
    return net.fetch_bytes(url, timeout=90)


def normalize(data: bytes, context) -> list[dict]:
    """One fire-detection event per CSV row."""
    records = []
    for row in csv.DictReader(io.StringIO(data.decode("utf-8"))):
        hhmm = row["acq_time"].zfill(4)
        start = iso_from_text(f"{row['acq_date']}T{hhmm[:2]}:{hhmm[2:]}:00")  # FIRMS times are UTC
        records.append(make_event(
            record_id=f"{row['latitude']}-{row['longitude']}-{row['acq_date']}-{hhmm}-{row.get('satellite')}",
            event_type="fire-detection",
            category="ENVIRONMENT",
            title=f"Thermal anomaly ({row.get('instrument', 'VIIRS')}, {row.get('daynight') == 'D' and 'day' or 'night'})",
            geometry=point(row["longitude"], row["latitude"]),
            start_time=start,
            status="detected",
            evidence_type="OBSERVED",
            attributes={
                "frp_mw": float(row["frp"]) if row.get("frp") else None,
                "source_confidence": CONFIDENCE_NAMES.get(row.get("confidence"), row.get("confidence")),
                "satellite": row.get("satellite"),
                "bright_ti4_k": row.get("bright_ti4"),
            },
            raw=row,
        ))
    return records
