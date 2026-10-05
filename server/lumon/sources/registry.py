"""
The OSINT source registry.

Static facts about each source (provider, licence, endpoint...) live in
config/sources/registry.json so they can be reviewed without reading code.
This module loads that file and joins it with the live state stored in the
database (health, last success, record counts).
"""

import importlib
import json
from datetime import datetime, timezone

from .. import db, settings


def load_definitions() -> list[dict]:
    """Return every source definition from config/sources/registry.json."""
    path = settings.CONFIG_DIR / "sources" / "registry.json"
    return json.loads(path.read_text())["sources"]


def get_definition(source_id: str) -> dict | None:
    """Return one source definition by id, or None if unknown."""
    for source in load_definitions():
        if source["id"] == source_id:
            return source
    return None


def load_adapter(source: dict):
    """
    Import the adapter module named in the registry entry.
    Returns None for planned sources that have no adapter yet.
    """
    if not source.get("adapter"):
        return None
    return importlib.import_module(f"lumon.sources.{source['adapter']}")


def data_age_hours(last_time: str | None) -> float | None:
    """How old (in hours) the newest record is, or None if unknown."""
    if not last_time:
        return None
    parsed = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 3600, 1)


# The status words shown to analysts, from best to worst.
#   LIVE             downloaded recently (within the source's stale_after_hours)
#                    in CONNECTED mode
#   SNAPSHOT         recent enough, but we are air-gapped, so it is the last
#                    staged copy, not a live feed
#   STALE            the last successful download is older than
#                    stale_after_hours (shown with its age)
#   UNAVAILABLE      the last attempt failed and nothing was ever staged
#   NOT STAGED       never downloaded
#   KEY REQUIRED     needs an API key that is not configured
#   NOT IMPLEMENTED  registered but no adapter exists yet
STATUS_ORDER = ["LIVE", "DELAYED", "SNAPSHOT", "STALE", "UNAVAILABLE", "NOT STAGED", "KEY REQUIRED", "NOT IMPLEMENTED"]


def freshness(source: dict, state: dict, mode: str) -> dict:
    """
    Decide a source's status from facts only: the operating mode, whether
    it has an adapter/key, the result of its last run and how long ago its
    last SUCCESSFUL DOWNLOAD was (re-reading a snapshot offline never makes
    data younger).

    Returns {"status", "age_hours", "stale_after_hours", "label"} where
    label is the short text for the UI, e.g. "SNAPSHOT · 3.2 h old".

    DELAYED (only for sources with "delayed_after_hours", i.e. live feeds):
    downloaded in ingest mode and still inside the stale limit, but older
    than a live feed should be - for example one or two polls were missed.
    """
    stale_after = source.get("stale_after_hours")
    delayed_after = source.get("delayed_after_hours")
    age = data_age_hours(state.get("last_success"))  # age of the last download
    health = state.get("health", "never-run")
    if not source.get("adapter"):
        status = "NOT IMPLEMENTED"
    elif source.get("requires_key") and not settings.get_setting(source.get("key_env", "")):
        status = "KEY REQUIRED"
    elif age is None:
        status = "UNAVAILABLE" if health in ("failed", "rate-limited") else "NOT STAGED"
    elif stale_after is not None and age > stale_after:
        status = "STALE"
    elif mode == "connected" and health == "ok" and delayed_after is not None and age > delayed_after:
        status = "DELAYED"
    elif mode == "connected" and health == "ok":
        status = "LIVE"
    else:
        status = "SNAPSHOT"
    label = status
    if age is not None and status in ("SNAPSHOT", "STALE", "LIVE", "DELAYED"):
        label = f"{status} · {format_age(age)} old"
    if health == "failed" and status in ("SNAPSHOT", "STALE"):
        label += " · LAST REFRESH FAILED"
    if health == "rate-limited" and status in ("SNAPSHOT", "STALE"):
        label += " · RATE LIMITED"
    return {"status": status, "age_hours": age, "stale_after_hours": stale_after, "label": label}


def format_age(hours: float) -> str:
    """0.3 -> '18 min', 5.2 -> '5.2 h', 50 -> '2.1 d'."""
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} d"


def describe_all(connection) -> list[dict]:
    """
    Every source definition merged with its live state, as shown on the
    analyst's source cards. Sources never refreshed show health "never-run".
    """
    live = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM sources")}
    mode = settings.operating_mode()
    result = []
    for source in load_definitions():
        state = live.get(source["id"], {})
        key_missing = source.get("requires_key") and not settings.get_setting(source.get("key_env", ""))
        health = state.get("health", "never-run")
        if not source.get("adapter"):
            health = "not-implemented"
        elif key_missing:
            health = "key-required"
        result.append({
            **{k: v for k, v in source.items() if not k.startswith("_")},
            "health": health,
            # Operating mode, not a network test: "ingest enabled" only means
            # downloads are ALLOWED. Actual freshness is in freshness_status.
            "connection": "INGEST ENABLED" if mode == "connected" else "AIR-GAPPED",
            "last_success": state.get("last_success"),
            "last_failure": state.get("last_failure"),
            "last_error": state.get("last_error"),
            "last_data_time": state.get("last_data_time"),
            "data_age_hours": data_age_hours(state.get("last_data_time")),
            "record_count": state.get("record_count", 0),
            "cache_status": "snapshot staged" if state.get("last_snapshot") else "no snapshot",
            "last_snapshot_sha256": state.get("last_snapshot_sha256"),
            # Provider rate limits (live feeds only; None when not reported).
            "rate_limit_remaining": state.get("rate_limit_remaining"),
            "next_allowed_at": state.get("next_allowed_at"),
            "consecutive_failures": state.get("consecutive_failures") or 0,
            **{f"freshness_{k}": v for k, v in freshness(source, state, mode).items()},
        })
    return result
