"""
The minimal contract every future VESSEL (AIS) adapter must produce.

STATUS: NOT CONFIGURED. Lumon has no AIS feed today, so it has no vessel
positions. Ports (natural-earth-ports) are static reference entities and
are never vessels.

When an AIS provider is connected (for example aisstream.io; see the
registry entry "aisstream-vessels" for its access requirements), its
adapter's normalize() must turn each position report into a normalized
event via make_vessel_position(). The function refuses incomplete reports
instead of filling gaps, so no vessel position can be invented.

Fields of one vessel position:
  identifier    MMSI (9 digits), the vessel's AIS identity
  lon, lat      reported position (WGS84 degrees)
  observed_at   when the vessel reported this position (ISO 8601 UTC), from
                the AIS message - NOT when Lumon received it. Ingestion time
                is recorded separately by the pipeline (events.updated_at).
  course_deg    course over ground, 0-360, or None if not available (AIS 360)
  speed_kn      speed over ground in knots, or None if not available (AIS 102.3)
  heading_deg   true heading 0-359, or None if not available (AIS 511)
  name          vessel name from static AIS data, or None
  source_id     the Lumon registry source that supplied it
  provider_ref  the provider's message reference (for provenance)

Freshness (LIVE / DELAYED / STALE ...) is decided by the source registry
from download times, exactly as for aircraft.
"""

from .common import iso_from_text, make_event, point

# AIS "not available" sentinel values (ITU-R M.1371).
COURSE_NOT_AVAILABLE = 360.0
SPEED_NOT_AVAILABLE = 102.3
HEADING_NOT_AVAILABLE = 511


class InvalidVesselReport(ValueError):
    """A position report that cannot become a vessel position."""


def _optional(value, not_available, low, high):
    """AIS value or None when missing, 'not available' or out of range."""
    if value is None:
        return None
    number = float(value)
    if number == float(not_available) or not (low <= number <= high):
        return None
    return number


def make_vessel_position(identifier, lon, lat, observed_at, source_id, course_deg=None, speed_kn=None,
                         heading_deg=None, name=None, provider_ref=None) -> dict:
    """
    Build one normalized vessel-position event. Raises InvalidVesselReport
    if the identity, position or observation time is missing or invalid.
    """
    mmsi = str(identifier or "").strip()
    if not (mmsi.isdigit() and len(mmsi) == 9):
        raise InvalidVesselReport(f"identifier must be a 9-digit MMSI, got {identifier!r}")
    if lon is None or lat is None or not (-180 <= float(lon) <= 180 and -90 <= float(lat) <= 90):
        raise InvalidVesselReport("position missing or out of range")
    observed = iso_from_text(observed_at, assume_utc=False)
    if observed is None:
        raise InvalidVesselReport("observed_at must be a timestamp with a time zone")
    if not source_id:
        raise InvalidVesselReport("source_id is required")

    attributes = {
        "mmsi": mmsi,
        "name": (name or "").strip() or None,
        "course_deg": _optional(course_deg, COURSE_NOT_AVAILABLE, 0, 359.9),
        "speed_kn": _optional(speed_kn, SPEED_NOT_AVAILABLE, 0, 102.2),
        "heading_deg": _optional(heading_deg, HEADING_NOT_AVAILABLE, 0, 359),
        "source_id": source_id,
        "provider_ref": provider_ref,
    }
    return make_event(
        record_id=mmsi, event_type="vessel-position", category="MARITIME",
        title=f"Vessel {attributes['name'] or mmsi}", geometry=point(lon, lat), start_time=observed,
        evidence_type="OBSERVED", attributes=attributes,
    )
