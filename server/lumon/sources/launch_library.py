"""
Launches from Indian launch sites, from The Space Devs' Launch Library 2.

We request upcoming and recent launches for two launch locations:
Satish Dhawan Space Centre (Sriharikota, LL2 location id 14) and
Kulasekarapattinam Spaceport (LL2 location id 187). The ids were looked up
from the LL2 /locations endpoint. The free tier allows 15 requests per hour,
so this source should be refreshed rarely.
"""

import json

from .. import net
from .common import iso_from_text, make_event, point

LOCATION_IDS = "14,187"


def fetch(context) -> bytes:
    """Two requests (upcoming + previous), stored together in one snapshot."""
    base = "https://ll.thespacedevs.com/2.3.0/launches"
    upcoming = json.loads(net.fetch_bytes(f"{base}/upcoming/?limit=15&location__ids={LOCATION_IDS}"))
    previous = json.loads(net.fetch_bytes(f"{base}/previous/?limit=15&location__ids={LOCATION_IDS}"))
    return json.dumps({"upcoming": upcoming, "previous": previous}).encode("utf-8")


def normalize(data: bytes, context) -> list[dict]:
    """One launch event per launch, located at its launch pad."""
    payload = json.loads(data)
    records = []
    for group in ("upcoming", "previous"):
        for launch in payload[group].get("results", []):
            pad = launch.get("pad") or {}
            if pad.get("longitude") is None or pad.get("latitude") is None:
                continue
            status = launch.get("status") or {}
            precision = (launch.get("net_precision") or {}).get("name")
            records.append(make_event(
                record_id=launch["id"],
                event_type="launch",
                category="SPACE",
                title=launch["name"],
                geometry=point(pad["longitude"], pad["latitude"]),
                start_time=iso_from_text(launch.get("net")),
                status=status.get("abbrev") or status.get("name"),
                evidence_type="VERIFIED RECORD",
                attributes={
                    "status": status.get("name"),
                    "status_description": status.get("description"),
                    "time_precision": precision,  # e.g. "Month" means the date is only known to the month
                    "pad": pad.get("name"),
                    "provider": (launch.get("launch_service_provider") or {}).get("name"),
                    "mission": (launch.get("mission") or {}).get("name"),
                    "upcoming": group == "upcoming",
                    "source_url": launch.get("url"),
                },
                raw=launch,
            ))
    return records
