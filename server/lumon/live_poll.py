"""
Bounded automatic polling of the OpenSky aircraft feed.

Without this module, aircraft positions only change when someone runs a
manual refresh, so they turn STALE within 15 minutes.

The poller runs ONLY when every condition below holds; otherwise its status
is NOT CONFIGURED (or DISABLED when air-gapped) with the exact reasons:

  1. LUMON_MODE=connected
  2. OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET are set in .env.local
     (an OpenSky API client, created on https://opensky-network.org/my-opensky/account)
  3. OPENSKY_AGREEMENT_CONFIRMED=yes in .env.local. OpenSky's terms say any
     automated or operational use of the REST API, even internal, requires
     a prior written agreement (contact contact@opensky-network.org). Set
     this only once that agreement exists.

Limits it keeps to:
  - Interval from the daily credit budget: an India-wide /states/all query
    costs 4 credits, so with OPENSKY_DAILY_CREDITS (default 4,000 for a
    standard API client) and a 20 % safety margin it polls at most every
    ~108 s, and never faster than MIN_INTERVAL_S.
  - Each request times out after 20 s (in the adapter).
  - After a failure it waits base x 2^(failures-1), capped at 30 minutes.
  - On HTTP 429 it waits at least X-Rate-Limit-Retry-After-Seconds.
  - A failed poll never deletes stored positions; they age into STALE.
"""

import threading

from . import db, settings

SOURCE_ID = "opensky-aircraft"
CREDITS_PER_CALL = 4          # /states/all over more than 400 square degrees
DEFAULT_DAILY_CREDITS = 4000  # standard registered API client
SAFETY_MARGIN = 0.8           # use at most 80 % of the daily credits
MIN_INTERVAL_S = 90
MAX_BACKOFF_S = 30 * 60

# What the running poller last did (in memory; reset when the API restarts).
_state = {"running": False, "last_poll_at": None, "last_result": None, "next_poll_at": None,
          "consecutive_failures": 0, "interval_s": None}


def configuration_problems() -> list[str]:
    """Why automatic polling cannot run (empty list = it can)."""
    problems = []
    if settings.operating_mode() != "connected":
        problems.append("LUMON_MODE is not 'connected' (air-gapped: no network requests)")
    if not (settings.get_setting("OPENSKY_CLIENT_ID") and settings.get_setting("OPENSKY_CLIENT_SECRET")):
        problems.append("OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET not set (create an API client on your OpenSky account page)")
    if settings.get_setting("OPENSKY_AGREEMENT_CONFIRMED").lower() != "yes":
        problems.append("OPENSKY_AGREEMENT_CONFIRMED is not 'yes' (OpenSky requires a written agreement for automated REST use)")
    return problems


def daily_credits() -> int:
    """The daily OpenSky credit budget to plan against."""
    try:
        return max(1, int(settings.get_setting("OPENSKY_DAILY_CREDITS", str(DEFAULT_DAILY_CREDITS))))
    except ValueError:
        return DEFAULT_DAILY_CREDITS


def poll_interval_seconds(credits_per_day: int) -> int:
    """Seconds between polls so a day's polling stays within the safe share of the credits."""
    calls_per_day = credits_per_day * SAFETY_MARGIN / CREDITS_PER_CALL
    return max(MIN_INTERVAL_S, int(86400 / max(calls_per_day, 1)) + 1)


def next_delay_seconds(summary: dict, failures: int, interval_s: int) -> int:
    """
    How long to wait after a poll:
      success          -> the normal interval
      rate limited     -> max(interval, provider's retry-after), with backoff
      other failure    -> interval x 2^(failures-1), capped at 30 minutes
    """
    if summary.get("status") == "success":
        return interval_s
    backoff = min(interval_s * 2 ** max(failures - 1, 0), MAX_BACKOFF_S)
    retry_after = summary.get("retry_after_s")
    return max(backoff, int(retry_after)) if retry_after is not None else backoff


def status() -> dict:
    """Poller status for /api/live/status and the source cards."""
    problems = configuration_problems()
    if _state["running"]:
        label = "BACKING OFF" if _state["consecutive_failures"] else "POLLING"
    elif problems and settings.operating_mode() != "connected":
        label = "DISABLED"
    elif problems:
        label = "NOT CONFIGURED"
    else:
        label = "STOPPED"
    credits = daily_credits()
    return {"source_id": SOURCE_ID, "state": label, "problems": problems,
            "daily_credits": credits, "credits_per_call": CREDITS_PER_CALL,
            "interval_s": _state["interval_s"] or poll_interval_seconds(credits),
            **{k: _state[k] for k in ("last_poll_at", "last_result", "next_poll_at", "consecutive_failures")}}


def run_forever(stop_event: threading.Event) -> None:
    """Poll until stop_event is set, respecting the interval, backoff and retry-after."""
    from .ingest import refresh_source  # imported here to keep module import light

    interval = poll_interval_seconds(daily_credits())
    _state.update(running=True, interval_s=interval)
    while not stop_event.is_set():
        if configuration_problems():  # settings changed while running
            break
        _state["last_poll_at"] = db.now_iso()
        try:
            summary = refresh_source(SOURCE_ID, actor="live-poller")
        except Exception as error:  # never let the thread die silently
            summary = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        failures = 0 if summary.get("status") == "success" else _state["consecutive_failures"] + 1
        delay = next_delay_seconds(summary, failures, interval)
        _state.update(last_result=summary.get("error") or summary.get("status"), consecutive_failures=failures,
                      next_poll_at=db.iso_after_seconds(delay))
        stop_event.wait(delay)
    _state.update(running=False, next_poll_at=None)


def start_background_thread() -> threading.Event | None:
    """Start the poller if fully configured; return its stop event, or None."""
    if configuration_problems():
        return None
    stop_event = threading.Event()
    threading.Thread(target=run_forever, args=(stop_event,), daemon=True, name="lumon-live-poll").start()
    return stop_event
