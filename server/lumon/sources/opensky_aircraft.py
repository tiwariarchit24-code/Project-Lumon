"""
Aircraft positions from the OpenSky Network REST API (/states/all).

Each "state vector" is one aircraft's self-reported position at a moment
in time (ADS-B / Mode S). Coverage depends on OpenSky's volunteer
receivers and is incomplete over India; absence of an aircraft on the map
does NOT mean no aircraft is there.

ACCESS (checked against OpenSky's documentation and terms, 2026-10-04):
  - Anonymous: 400 API credits/day, 10 s time resolution, latest data only.
  - Registered API client: OAuth2 client-credentials (client_id and
    client_secret from the OpenSky account page), 4,000 credits/day,
    5 s resolution. Username/password basic auth is no longer accepted.
    Tokens expire after 30 minutes.
  - A /states/all query over more than 400 square degrees (the India
    operating area is far larger) costs 4 credits.
  - X-Rate-Limit-Remaining reports the remaining credits; when they run out
    the API answers 429 with X-Rate-Limit-Retry-After-Seconds.
  - Terms: non-profit research/education use only; AUTOMATED / operational
    use of the REST API (even internal) requires a prior written agreement
    with OpenSky. Lumon therefore never polls automatically unless that
    agreement is explicitly confirmed (see lumon/live_poll.py).

Credentials are read server-side from .env.local (OPENSKY_CLIENT_ID,
OPENSKY_CLIENT_SECRET) and never sent to the browser or logged.
"""

import json
import time

from .. import net, settings
from .common import iso_from_epoch_seconds, make_event, point

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"
REQUEST_TIMEOUT_S = 20

# Order of the fields in each OpenSky state vector (from the API docs).
FIELDS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk", "spi", "position_source",
]


class RateLimited(Exception):
    """OpenSky answered 429: no credits left. `retry_after_s` says how long to wait."""

    def __init__(self, retry_after_s: int | None):
        super().__init__(f"OpenSky rate limit reached (HTTP 429); retry after {retry_after_s if retry_after_s is not None else 'unknown'} s")
        self.retry_after_s = retry_after_s


class AccessDenied(Exception):
    """OpenSky refused the credentials (HTTP 401/403)."""


# The current OAuth2 access token and when it expires (in memory only).
_token = {"value": None, "expires_at": 0.0}


def credentials() -> tuple[str, str] | None:
    """(client_id, client_secret) from .env.local / environment, or None."""
    client_id = settings.get_setting("OPENSKY_CLIENT_ID")
    client_secret = settings.get_setting("OPENSKY_CLIENT_SECRET")
    return (client_id, client_secret) if client_id and client_secret else None


def access_token() -> str | None:
    """
    A valid bearer token when credentials are configured, else None
    (anonymous access). Tokens are reused until 60 s before they expire.
    """
    creds = credentials()
    if creds is None:
        return None
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]
    status, _headers, body = net.fetch_response(TOKEN_URL, timeout=REQUEST_TIMEOUT_S, form_body={
        "grant_type": "client_credentials", "client_id": creds[0], "client_secret": creds[1]})
    if status != 200:
        raise AccessDenied(f"OpenSky token request failed (HTTP {status}); check OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET")
    payload = json.loads(body)
    _token["value"] = payload["access_token"]
    _token["expires_at"] = time.time() + float(payload.get("expires_in", 1800))
    return _token["value"]


def _header(headers: dict, name: str):
    """Case-insensitive header lookup."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def fetch(context) -> bytes:
    """
    Download all state vectors inside the operating-area bounding box.

    Records what OpenSky said about the request in context["response_meta"]
    (authenticated or anonymous, remaining credits, HTTP status) so the
    ingest step can store it in the source's health record.
    Raises RateLimited on 429 and AccessDenied on 401/403; any other non-200
    answer raises RuntimeError. The previous positions stay in place.
    """
    min_lon, min_lat, max_lon, max_lat = context["bbox"]
    url = f"{STATES_URL}?lamin={min_lat:.3f}&lomin={min_lon:.3f}&lamax={max_lat:.3f}&lomax={max_lon:.3f}"
    token = access_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, response_headers, body = net.fetch_response(url, timeout=REQUEST_TIMEOUT_S, headers=headers)
    remaining = _header(response_headers, "X-Rate-Limit-Remaining")
    context["response_meta"] = {
        "http_status": status, "authenticated": token is not None,
        "rate_limit_remaining": int(remaining) if remaining not in (None, "") else None,
    }
    if status == 429:
        retry = _header(response_headers, "X-Rate-Limit-Retry-After-Seconds")
        raise RateLimited(int(retry) if retry not in (None, "") else None)
    if status in (401, 403):
        _token["value"] = None  # force a fresh token next time
        raise AccessDenied(f"OpenSky refused the request (HTTP {status})")
    if status != 200:
        raise RuntimeError(f"OpenSky answered HTTP {status}")
    return body


def normalize(data: bytes, context) -> list[dict]:
    """One aircraft-position event per aircraft that reported a position."""
    payload = json.loads(data)
    records = []
    for values in payload.get("states") or []:
        state = dict(zip(FIELDS, values))
        if state["longitude"] is None or state["latitude"] is None:
            continue  # no position reported: nothing to place on the map
        callsign = (state["callsign"] or "").strip()
        records.append(make_event(
            record_id=state["icao24"],
            event_type="aircraft-position",
            category="AVIATION",
            title=f"Aircraft {callsign or state['icao24']}",
            geometry=point(state["longitude"], state["latitude"]),
            start_time=iso_from_epoch_seconds(state["time_position"] or state["last_contact"]),
            status="on-ground" if state["on_ground"] else "airborne",
            evidence_type="OBSERVED",
            attributes={
                "icao24": state["icao24"],
                "callsign": callsign or None,
                "origin_country": state["origin_country"],
                "baro_altitude_m": state["baro_altitude"],
                "velocity_m_s": state["velocity"],
                "track_deg": state["true_track"],
                "vertical_rate_m_s": state["vertical_rate"],
            },
            raw=state,
        ))
    return records
