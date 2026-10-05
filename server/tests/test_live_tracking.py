"""
Live tracking: OpenSky refresh, rate limits, freshness labels, the gated
poller and the vessel contract. The provider is replaced by a fake
net.fetch_response, so no test touches the network. All aircraft and
vessel records here are SYNTHETIC TEST FIXTURES.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from lumon import db, ingest, live_poll, net, settings
from lumon.sources import opensky_aircraft, registry, vessel_contract

NOW = int(time.time())
# TEST FIXTURE: one aircraft state vector inside the synthetic square.
STATES = {"time": NOW, "states": [["abc123", "TEST01  ", "Testland", NOW - 5, NOW - 2, 75.0, 15.0,
                                    10000.0, False, 230.0, 90.0, 0.0, None, 10100.0, "1234", False, 0]]}


@pytest.fixture
def connected(monkeypatch, square_area, isolated):
    """Connected mode with no credentials from the real .env.local."""
    monkeypatch.setenv("LUMON_MODE", "connected")
    monkeypatch.setattr(settings, "ROOT_DIR", isolated)  # snapshot paths are stored relative to it
    monkeypatch.setattr(settings, "_file_values", {})
    opensky_aircraft._token.update(value=None, expires_at=0.0)


def fake_provider(monkeypatch, responses):
    """Make net.fetch_response return the given (status, headers, body) answers in order."""
    calls = []

    def fetch_response(url, timeout=20, headers=None, form_body=None):
        calls.append({"url": url, "headers": headers or {}, "form": form_body, "timeout": timeout})
        return responses.pop(0)
    monkeypatch.setattr(net, "fetch_response", fetch_response)
    return calls


def aircraft_count():
    connection = db.connect()
    count = connection.execute("SELECT COUNT(*) FROM events WHERE source_id = 'opensky-aircraft'").fetchone()[0]
    connection.close()
    return count


def source_state():
    connection = db.connect()
    row = dict(connection.execute("SELECT * FROM sources WHERE id = 'opensky-aircraft'").fetchone())
    connection.close()
    return row


def test_successful_refresh_records_credits_and_observation_time(monkeypatch, connected):
    calls = fake_provider(monkeypatch, [(200, {"X-Rate-Limit-Remaining": "396"}, json.dumps(STATES).encode())])
    summary = ingest.refresh_source("opensky-aircraft")
    assert summary["status"] == "success" and summary["records_kept"] == 1, summary["error"]
    assert "Authorization" not in calls[0]["headers"]  # anonymous without credentials
    assert calls[0]["timeout"] <= 20
    state = source_state()
    assert state["rate_limit_remaining"] == 396 and state["consecutive_failures"] == 0
    connection = db.connect()
    event = connection.execute("SELECT start_time, updated_at FROM events WHERE source_id = 'opensky-aircraft'").fetchone()
    connection.close()
    # Observation time comes from the provider, not from when Lumon stored it.
    assert event["start_time"] == datetime.fromtimestamp(NOW - 5, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert event["updated_at"] is not None


def test_rate_limit_keeps_last_known_positions(monkeypatch, connected):
    fake_provider(monkeypatch, [
        (200, {"X-Rate-Limit-Remaining": "4"}, json.dumps(STATES).encode()),
        (429, {"X-Rate-Limit-Retry-After-Seconds": "3600", "X-Rate-Limit-Remaining": "0"}, b""),
    ])
    ingest.refresh_source("opensky-aircraft")
    summary = ingest.refresh_source("opensky-aircraft")
    assert summary["status"] == "failed" and "429" in summary["error"]
    assert summary["retry_after_s"] == 3600
    assert aircraft_count() == 1  # nothing erased
    state = source_state()
    assert state["health"] == "rate-limited" and state["consecutive_failures"] == 1
    assert state["rate_limit_remaining"] == 0 and state["next_allowed_at"] > db.now_iso()


def test_server_error_keeps_positions_and_counts_failures(monkeypatch, connected):
    fake_provider(monkeypatch, [(200, {}, json.dumps(STATES).encode()), (503, {}, b""), (503, {}, b"")])
    ingest.refresh_source("opensky-aircraft")
    ingest.refresh_source("opensky-aircraft")
    summary = ingest.refresh_source("opensky-aircraft")
    assert "HTTP 503" in summary["error"]
    assert aircraft_count() == 1
    assert source_state()["consecutive_failures"] == 2


def test_credentials_use_oauth_token_and_reuse_it(monkeypatch, connected):
    monkeypatch.setenv("OPENSKY_CLIENT_ID", "test-client")  # TEST VALUE
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "test-secret")  # TEST VALUE
    calls = fake_provider(monkeypatch, [
        (200, {}, json.dumps({"access_token": "tok", "expires_in": 1800}).encode()),
        (200, {}, json.dumps(STATES).encode()),
        (200, {}, json.dumps(STATES).encode()),
    ])
    ingest.refresh_source("opensky-aircraft")
    ingest.refresh_source("opensky-aircraft")
    assert calls[0]["url"] == opensky_aircraft.TOKEN_URL and calls[0]["form"]["grant_type"] == "client_credentials"
    assert calls[1]["headers"]["Authorization"] == "Bearer tok"
    assert len(calls) == 3  # one token request, then two data requests


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_freshness_live_delayed_stale_snapshot_unavailable():
    source = registry.get_definition("opensky-aircraft")
    assert registry.freshness(source, {"health": "ok", "last_success": _iso(0.01)}, "connected")["status"] == "LIVE"
    assert registry.freshness(source, {"health": "ok", "last_success": _iso(0.1)}, "connected")["status"] == "DELAYED"
    assert registry.freshness(source, {"health": "ok", "last_success": _iso(4.6)}, "connected")["status"] == "STALE"
    # A re-read snapshot is never LIVE, however recent the old download was.
    assert registry.freshness(source, {"health": "offline-snapshot", "last_success": _iso(0.01)}, "airgapped")["status"] == "SNAPSHOT"
    assert registry.freshness(source, {"health": "rate-limited"}, "connected")["status"] == "UNAVAILABLE"
    label = registry.freshness(source, {"health": "rate-limited", "last_success": _iso(4.6)}, "connected")["label"]
    assert label.startswith("STALE") and "RATE LIMITED" in label


def test_poller_not_configured_without_credentials_and_agreement(monkeypatch):
    monkeypatch.setattr(settings, "_file_values", {})
    monkeypatch.setenv("LUMON_MODE", "connected")
    status = live_poll.status()
    assert status["state"] == "NOT CONFIGURED" and len(status["problems"]) == 2
    assert live_poll.start_background_thread() is None
    # Credentials alone are not enough: the written agreement must be confirmed.
    monkeypatch.setenv("OPENSKY_CLIENT_ID", "test-client")
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "test-secret")
    assert live_poll.status()["state"] == "NOT CONFIGURED"
    monkeypatch.setenv("LUMON_MODE", "airgapped")
    assert live_poll.status()["state"] == "DISABLED"


def test_poll_interval_and_backoff_stay_within_limits():
    # 4 credits per India-wide call; standard 4,000/day with 20 % margin.
    interval = live_poll.poll_interval_seconds(4000)
    assert interval >= 86400 * 4 / 4000 and interval >= live_poll.MIN_INTERVAL_S
    assert live_poll.poll_interval_seconds(400) >= 864  # anonymous budget would be ~15 min apart
    assert live_poll.next_delay_seconds({"status": "success"}, 0, 120) == 120
    assert live_poll.next_delay_seconds({"status": "failed"}, 3, 120) == 480
    assert live_poll.next_delay_seconds({"status": "failed"}, 20, 120) == live_poll.MAX_BACKOFF_S
    assert live_poll.next_delay_seconds({"status": "failed", "retry_after_s": 5000}, 1, 120) == 5000


def test_vessel_contract_refuses_incomplete_reports():
    record = vessel_contract.make_vessel_position(  # TEST FIXTURE
        "419000001", 72.8, 18.9, "2026-10-04T08:00:00Z", "test-ais", course_deg=360, speed_kn=12.5, heading_deg=511)
    assert record["type"] == "vessel-position" and record["start_time"] == "2026-10-04T08:00:00Z"
    assert record["attributes"]["course_deg"] is None and record["attributes"]["heading_deg"] is None
    assert record["attributes"]["speed_kn"] == 12.5
    for bad in [dict(identifier="123"), dict(lon=None), dict(observed_at="2026-10-04T08:00:00"), dict(source_id="")]:
        args = dict(identifier="419000001", lon=72.8, lat=18.9, observed_at="2026-10-04T08:00:00Z", source_id="test-ais")
        args.update(bad)
        with pytest.raises(vessel_contract.InvalidVesselReport):
            vessel_contract.make_vessel_position(**args)
