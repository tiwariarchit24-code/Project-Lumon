"""
Two small lists that change how some event types are SHOWN, kept in one
place so the map, the timeline and the lists all agree.

TELEMETRY_TYPES
    Continuous measurements or positions (aircraft, satellites, model
    weather, the Kp index). They are snapshots of a moment, not happenings
    an analyst reviews, so the timeline keeps them in a separate band and
    their markers are flagged STALE once older than the source's
    stale_after_hours.

SCHEDULED_TYPES
    Events whose time can be in the FUTURE because it is a plan, not an
    observation (launches). Future ones are shown only in the UPCOMING
    window and are labelled SCHEDULED, never as something that happened.
"""

TELEMETRY_TYPES = {"aircraft-position", "satellite-position", "weather-current", "geomagnetic-kp"}
SCHEDULED_TYPES = {"launch"}
