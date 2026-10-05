"""
Internet outage events for India from IODA (Georgia Tech).

IODA detects statistical anomalies in several measurement signals. The
country-level events have no precise location, so they are stored with
scope "national" (about India as a whole) and appear in the timeline and
connectivity panel rather than as map markers.
"""

import json
import time

from .. import net
from .common import iso_from_epoch_seconds, make_event

# IODA data source codes, written out for analysts.
DATASOURCE_NAMES = {
    "bgp": "BGP routing visibility",
    "ping-slash24": "Active probing",
    "merit-nt": "Network telescope",
    "gtr": "Google Transparency Report traffic",
    "gtr-norm": "Google traffic (normalised)",
}


def fetch(context) -> bytes:
    """Download country-level outage events for the last 30 days."""
    until = int(time.time())
    since = until - 30 * 24 * 3600
    url = f"https://api.ioda.inetintel.cc.gatech.edu/v2/outages/events?entityType=country&entityCode=IN&from={since}&until={until}"
    return net.fetch_bytes(url, timeout=60)


def normalize(data: bytes, context) -> list[dict]:
    """One national-scope event per IODA outage event."""
    records = []
    for item in json.loads(data).get("data") or []:
        source_name = DATASOURCE_NAMES.get(item.get("datasource"), item.get("datasource"))
        start = item.get("start")
        duration = item.get("duration") or 0
        records.append(make_event(
            record_id=f"{item.get('datasource')}-{start}",
            event_type="internet-outage-signal",
            category="CONNECTIVITY",
            title=f"Internet outage signal in India ({source_name})",
            geometry=None,
            start_time=iso_from_epoch_seconds(start),
            end_time=iso_from_epoch_seconds(start + duration) if start else None,
            status="detected",
            evidence_type="INFERRED",
            attributes={"datasource": item.get("datasource"), "method": item.get("method"), "ioda_score": item.get("score"), "duration_s": duration},
            raw=item,
            scope="national",
        ))
    return records
