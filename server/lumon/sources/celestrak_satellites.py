"""
Satellites over the India operating area, computed from CelesTrak orbital
elements.

CelesTrak publishes "general perturbations" (GP) orbital elements for
public satellites. From those elements the SGP4 model computes where each
satellite is at a given moment. We compute positions for the moment the
elements were downloaded (stored in the snapshot), so re-processing an old
snapshot offline gives the same answer instead of extrapolating stale
elements to "now".

These positions are COMPUTED, not observed, so they are labelled INFERRED.
Groups used: Earth resources, weather, and space stations.

HOW A POSITION IS COMPUTED (plain language):
  1. SGP4 gives the satellite's position in an Earth-centred frame that does
     NOT rotate with the Earth (TEME), in kilometres.
  2. We rotate it by the Earth's rotation angle at that moment (Greenwich
     sidereal time) to get an Earth-fixed position.
  3. We convert that to latitude / longitude / altitude on the WGS84
     ellipsoid.
"""

import json
import math
from datetime import datetime, timezone

from sgp4 import omm
from sgp4.api import Satrec, jday

from .. import net
from .common import iso_from_text, make_event, point

GROUPS = ["resource", "weather", "stations"]
WGS84_A = 6378.137  # equatorial radius, km
WGS84_F = 1 / 298.257223563


def fetch(context) -> bytes:
    """Download the three groups and record the download time in the snapshot."""
    groups = {}
    for group in GROUPS:
        groups[group] = json.loads(net.fetch_bytes(f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"))
    retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return json.dumps({"retrieved_at": retrieved, "groups": groups}).encode("utf-8")


def gmst_radians(jd_ut1: float) -> float:
    """Greenwich mean sidereal time (IAU 1982 formula), in radians."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    seconds = 67310.54841 + (876600.0 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return math.radians((seconds % 86400) / 240.0)


def teme_to_geodetic(position_km, jd_ut1: float) -> tuple[float, float, float]:
    """TEME position -> (longitude deg, latitude deg, altitude km) on WGS84."""
    theta = gmst_radians(jd_ut1)
    x, y, z = position_km
    # Rotate about the Earth's axis by the sidereal angle (TEME -> Earth-fixed).
    xe = math.cos(theta) * x + math.sin(theta) * y
    ye = -math.sin(theta) * x + math.cos(theta) * y
    lon = math.degrees(math.atan2(ye, xe))
    # Geodetic latitude by a few fixed-point iterations (converges quickly).
    e2 = WGS84_F * (2 - WGS84_F)
    p = math.hypot(xe, ye)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        n = WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * n * math.sin(lat), p)
    # Height above the ellipsoid. This form stays correct over the poles,
    # where the simpler p / cos(lat) - N would divide by zero.
    altitude = p * math.cos(lat) + z * math.sin(lat) - WGS84_A * math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return lon, math.degrees(lat), altitude


def normalize(data: bytes, context) -> list[dict]:
    """One satellite-position record per satellite, at the snapshot time."""
    payload = json.loads(data)
    moment = datetime.fromisoformat(payload["retrieved_at"].replace("Z", "+00:00"))
    jd, fraction = jday(moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    records = []
    for group, elements in payload["groups"].items():
        for fields in elements:
            satellite = Satrec()
            omm.initialize(satellite, fields)
            error, position, _velocity = satellite.sgp4(jd, fraction)
            if error != 0:
                continue  # SGP4 reports the elements cannot be propagated (e.g. decayed)
            lon, lat, altitude = teme_to_geodetic(position, jd + fraction)
            epoch = iso_from_text(fields.get("EPOCH"))
            records.append(make_event(
                record_id=fields["NORAD_CAT_ID"],
                event_type="satellite-position",
                category="SPACE",
                title=f"{fields['OBJECT_NAME']} (NORAD {fields['NORAD_CAT_ID']})",
                geometry=point(lon, lat),
                start_time=payload["retrieved_at"],
                status="computed",
                evidence_type="INFERRED",
                attributes={
                    "norad_id": fields["NORAD_CAT_ID"], "international_designator": fields.get("OBJECT_ID"),
                    "group": group, "altitude_km": round(altitude, 1), "element_epoch": epoch,
                    "element_age_hours": round((moment - datetime.fromisoformat(epoch.replace("Z", "+00:00"))).total_seconds() / 3600, 1) if epoch else None,
                    "inclination_deg": fields.get("INCLINATION"),
                },
                raw=fields,
            ))
    return records
