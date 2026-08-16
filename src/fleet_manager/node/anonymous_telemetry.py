"""Send the anonymous daily rollup to the community telemetry endpoint.

Design constraints, in priority order:

1. **It must never affect inference.**  Telemetry is a background nicety; any
   failure is swallowed and retried tomorrow.  There is no error path here that
   can propagate into the agent.
2. **Opt-out is checked before anything happens**, including before the
   ``install_id`` is created.  Writing an identifier to disk is itself the act
   the opt-out declines, so a disabled node touches nothing.
3. **One request per day, jittered.**  Not per request, not hourly.  Every
   question this data answers is answered by a daily aggregate, and a jittered
   send stops every node on earth hitting the endpoint at the same second.

The endpoint is unauthenticated by design (there is no account), so the client
is deliberately dull: no retry storm, no backoff ladder, no queueing of missed
days.  A missed day is simply lost.  That is the right trade for a free
community service -- an aggressive client would be indistinguishable from an
attack, and the server's rate limiter would treat it as one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# 00:05 UTC + up to 10 minutes of jitter, matching the account scheduler.
_RUN_HOUR_UTC = 0
_RUN_MINUTE_UTC = 5
_MAX_JITTER_S = 600

_STATE_FILE = Path.home() / ".fleet-manager" / "anonymous_telemetry_state.json"

# Short: the endpoint is a small FastAPI service and we have nothing urgent to
# say.  A hung POST must not keep a task alive for minutes.
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


def _load_last_sent_day(state_file: Path | None = None) -> str | None:
    target = state_file or _STATE_FILE
    try:
        return json.loads(target.read_text(encoding="utf-8")).get("last_sent_day")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _save_last_sent_day(day: str, state_file: Path | None = None) -> None:
    target = state_file or _STATE_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last_sent_day": day}), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        # Costs a duplicate POST tomorrow, which the server upserts
        # idempotently. Not worth failing over.
        logger.debug("could not persist telemetry state: %s", exc)


def _seconds_until_next_run(now: datetime | None = None) -> float:
    if now is None:
        now = datetime.now(UTC)
    today_run = now.replace(
        hour=_RUN_HOUR_UTC, minute=_RUN_MINUTE_UTC, second=0, microsecond=0
    )
    next_run = today_run if now < today_run else today_run + timedelta(days=1)
    return (next_run - now).total_seconds() + random.uniform(0, _MAX_JITTER_S)


async def send_once(settings, agent_version: str, state_file: Path | None = None) -> bool:
    """Build and POST yesterday's rollup.  Returns True if it was sent.

    Never raises.  Returns False for "not sent", whether that is opt-out,
    already-sent, or a network failure -- the caller does not act differently
    on any of them.
    """
    # Checked first, so an opted-out node creates no install_id and no state.
    if not getattr(settings, "telemetry", False):
        return False

    from fleet_manager.common.install_id import get_install_id
    from fleet_manager.node.anonymous_rollup import build_anonymous_rollup

    try:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        if _load_last_sent_day(state_file) == yesterday:
            return False

        payload = await build_anonymous_rollup(
            install_id=get_install_id(),
            agent_version=agent_version,
            data_dir=getattr(settings, "data_dir", "~/.fleet-manager"),
            day=yesterday,
            nickname=getattr(settings, "herd_nickname", "") or "",
        )

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(settings.telemetry_url, json=payload)

        if response.status_code in (200, 201, 202):
            _save_last_sent_day(yesterday, state_file)
            logger.debug("anonymous telemetry sent for %s", yesterday)
            return True

        # 4xx means this build is wrong and retrying will not fix it; mark the
        # day done so we do not hammer the endpoint once a day forever with a
        # payload it will keep rejecting.
        if 400 <= response.status_code < 500:
            logger.debug(
                "telemetry endpoint rejected payload (%s); not retrying %s",
                response.status_code,
                yesterday,
            )
            _save_last_sent_day(yesterday, state_file)
        else:
            logger.debug("telemetry endpoint returned %s", response.status_code)
        return False
    except Exception as exc:  # noqa: BLE001 - telemetry must never escalate
        logger.debug("anonymous telemetry send failed: %s", exc)
        return False


async def run_scheduler(settings, agent_version: str) -> None:
    """Daily loop.  Exits immediately when telemetry is disabled."""
    if not getattr(settings, "telemetry", False):
        logger.debug("anonymous telemetry disabled; scheduler not started")
        return

    while True:
        try:
            await asyncio.sleep(_seconds_until_next_run())
            await send_once(settings, agent_version)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("telemetry scheduler iteration failed: %s", exc)
            # Do not spin: wait out the day even if something above misbehaved.
            await asyncio.sleep(3600)
