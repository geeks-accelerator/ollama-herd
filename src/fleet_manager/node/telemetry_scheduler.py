"""Telemetry scheduler — fires once per day at ~00:05 UTC + jitter.

Responsible for:
- Building yesterday's daily rollup
- POSTing to platform's /api/telemetry/local-summary
- Persisting last-sent state to avoid duplicates on restart

Graceful failure: network errors log and wait for the next day.  409
responses are treated as success (platform already has the data).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fleet_manager import __version__
from fleet_manager.node import daily_rollup, platform_client, platform_connection

logger = logging.getLogger(__name__)

# Run at 00:05 UTC plus up to 10 minutes of jitter, so not every node
# in the world slams the platform at exactly the same second.
_RUN_HOUR_UTC = 0
_RUN_MINUTE_UTC = 5
_MAX_JITTER_S = 600  # up to 10 minutes

# Persisted state — tracks the last successfully sent day so we don't
# re-send after a restart.  Platform returns 409 for duplicates as a
# safety net, but this avoids unnecessary traffic.
_STATE_FILE = Path.home() / ".fleet-manager" / "telemetry_state.json"


def _load_last_sent_day() -> str | None:
    """Return the last successfully sent day (ISO date) or None."""
    if not _STATE_FILE.exists():
        return None
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        return data.get("last_sent_day")
    except (json.JSONDecodeError, OSError):
        return None


def _save_last_sent_day(day: str) -> None:
    """Persist the last successfully sent day."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump({"last_sent_day": day}, f)
    tmp.replace(_STATE_FILE)


def _seconds_until_next_run(now: datetime | None = None) -> float:
    """Compute seconds to sleep until the next scheduled run.

    Next run is today's _RUN_HOUR_UTC:_RUN_MINUTE_UTC if we haven't
    passed it yet, else tomorrow's.  Adds random jitter within the
    configured window.
    """
    if now is None:
        now = datetime.now(UTC)
    today_run = now.replace(
        hour=_RUN_HOUR_UTC,
        minute=_RUN_MINUTE_UTC,
        second=0,
        microsecond=0,
    )
    next_run = today_run if now < today_run else today_run + timedelta(days=1)
    delay = (next_run - now).total_seconds()
    jitter = random.uniform(0, _MAX_JITTER_S)
    return delay + jitter


async def _emit_once(include_tags: bool) -> bool:
    """Build yesterday's rollup and POST it.

    Returns True if sent successfully (or duplicate treated as success).
    Returns False on error (caller retries tomorrow).
    """
    state = platform_connection.load_state()
    if state is None:
        logger.debug("telemetry: no platform connection, skipping")
        return False

    # Build the payload for yesterday (UTC)
    from fleet_manager.node.daily_rollup import _yesterday_utc_bounds

    day, _, _ = _yesterday_utc_bounds()

    # Skip if we already sent this day
    last_sent = _load_last_sent_day()
    if last_sent == day:
        logger.debug(f"telemetry: already sent summary for {day}, skipping")
        return True

    try:
        payload = await daily_rollup.build_daily_rollup(
            node_uuid=state.node_id,
            agent_version=__version__,
            include_tags=include_tags,
            day=day,
        )
    except Exception as exc:
        logger.warning(f"telemetry: failed to build rollup for {day}: {exc}")
        return False

    # Empty days are not worth sending
    if not payload.get("entries"):
        logger.debug(f"telemetry: no entries for {day}, skipping send")
        _save_last_sent_day(day)  # mark as handled to avoid retries
        return True

    entry_count = len(payload["entries"])
    total_reqs = sum(e.get("local_requests", 0) for e in payload["entries"])

    try:
        await platform_client.post_local_summary(payload)
        _save_last_sent_day(day)
        logger.info(
            f"telemetry: sent daily summary for {day} — "
            f"{entry_count} entries, {total_reqs} total requests, accepted"
        )
        return True
    except platform_client.InvalidTokenError as exc:
        # Token revoked — can't recover automatically
        logger.warning(
            f"telemetry: operator token rejected — reconnect via "
            f"dashboard to resume telemetry. Error: {exc}"
        )
        return False
    except platform_connection.PlatformUnreachableError as exc:
        logger.warning(
            f"telemetry: platform unreachable for {day} — will retry tomorrow. "
            f"Error: {exc}"
        )
        return False
    except Exception as exc:
        logger.warning(
            f"telemetry: unexpected error sending {day} summary: {exc}"
        )
        return False


async def run_scheduler(
    include_tags: bool = False,
    startup_catchup: bool = True,
) -> None:
    """Background task — fires once per day.

    Args:
        include_tags: Whether to include per-tag request counts in each
            entry.  Opt-in separately because tag values can be mildly
            identifying.
        startup_catchup: If True, attempts to emit any missed day
            immediately on startup.  Useful after a restart that
            straddled the scheduled time.
    """
    logger.info(
        f"telemetry: scheduler started (include_tags={include_tags}) — "
        f"will emit daily at ~{_RUN_HOUR_UTC:02d}:{_RUN_MINUTE_UTC:02d} UTC + jitter"
    )

    if startup_catchup:
        # Catch up on any day we missed (e.g. agent was down across midnight)
        await _emit_once(include_tags=include_tags)

    while True:
        delay = _seconds_until_next_run()
        logger.debug(
            f"telemetry: next emit in {delay:.0f}s "
            f"(~{delay/3600:.1f} hours)"
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("telemetry: scheduler cancelled")
            raise

        await _emit_once(include_tags=include_tags)
