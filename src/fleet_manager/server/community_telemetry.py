"""Herd-level anonymous telemetry: build and send one payload per fleet per day.

Replaces the v1 node-level sender.  In v1 every node POSTed its own payload, so
a 3-Mac fleet reported as three separate installs and there was no way to tell
one fleet of three from three solo users.  Worse, the fleet-shape fields could
only ever be wrong: whichever node filled them in would report the whole
fleet's totals, and the server would multiply that by the number of nodes.

The router is the only component that knows a fleet is one fleet, so it sends:

    install_id  -> the HERD (random UUID on the router)
    devices[]   -> one entry per node, each with a stable device_id

``device_id`` is derived from the node's own ``node_id`` rather than being a
separate identity -- but hashed, never raw.  ``node_id`` falls back to
``socket.gethostname()`` when unset, which on macOS is routinely
"johns-macbook", and ollamaherd.com/telemetry promises in writing that a
hostname is never sent.  Salting with the herd's random ``install_id`` also
means the same Mac in two different herds produces two different device_ids,
so the value cannot act as a cross-herd fingerprint or be reversed to a name.

Fleet totals (node count, memory) are deliberately NOT sent as scalars.  The
server derives them from ``devices[]``, so the total is always consistent with
its parts and there is only one source of truth.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_RUN_HOUR_UTC = 0
_RUN_MINUTE_UTC = 5
_MAX_JITTER_S = 600

_STATE_FILE = Path.home() / ".fleet-manager" / "community_telemetry_state.json"
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

MAX_DEVICES = 100

# Mirrors the receiving service's per-device schema, which validates with
# extra="forbid": an unknown key 422s the WHOLE payload, not just the device.
# Learned the hard way -- an "mlx_servers" count invented here (the plan said
# mlx_version) rejected every send until it was removed.  Keep this in step
# with telemetry/app/schemas.py.
DEVICE_FIELDS = frozenset(
    {
        "device_id",
        "chip",
        "memory_gb",
        "cores",
        "agent_version",
        "ollama_version",
        "mlx_version",
        "requests",
    }
)


def device_id_for(herd_install_id: str, node_id: str) -> str:
    """Stable, non-reversible per-herd device identifier derived from node_id.

    Deterministic so a device keeps its identity across days (aggregation needs
    that), salted so it is not a global fingerprint, and hashed so a hostname
    fallback can never reach the wire.
    """
    digest = hashlib.sha256(f"{herd_install_id}:{node_id}".encode())
    return digest.hexdigest()[:16]


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
        logger.debug("could not persist community telemetry state: %s", exc)


def _seconds_until_next_run(now: datetime | None = None) -> float:
    if now is None:
        now = datetime.now(UTC)
    today_run = now.replace(
        hour=_RUN_HOUR_UTC, minute=_RUN_MINUTE_UTC, second=0, microsecond=0
    )
    next_run = today_run if now < today_run else today_run + timedelta(days=1)
    return (next_run - now).total_seconds() + random.uniform(0, _MAX_JITTER_S)


def build_devices(registry, herd_install_id: str, requests_by_node: dict) -> list[dict]:
    """One entry per known node, from registry state the router already holds.

    Includes offline nodes: a machine that was serving yesterday is part of the
    fleet that produced yesterday's numbers, and dropping it would make the
    device list disagree with the request counts.
    """
    devices = []
    for node in list(getattr(registry, "_nodes", {}).values())[:MAX_DEVICES]:
        hw = getattr(node, "hardware", None)
        ollama = getattr(node, "ollama", None)
        devices.append(
            {
                "device_id": device_id_for(herd_install_id, node.node_id),
                "chip": (getattr(hw, "chip", "") or "")[:80],
                "memory_gb": int(getattr(hw, "memory_total_gb", 0) or 0),
                "cores": int(getattr(hw, "cores_physical", 0) or 0),
                "agent_version": (getattr(node, "agent_version", "") or "")[:32],
                "ollama_version": (getattr(ollama, "version", "") or "")[:32],
                "requests": int(requests_by_node.get(node.node_id, 0)),
            }
        )
    return devices


async def _requests_by_node(data_dir: str, start_ts: float, end_ts: float) -> dict:
    """Per-node request counts for the day, so the leaderboard can aggregate."""
    from fleet_manager.server.trace_store import TraceStore

    counts: dict[str, int] = {}
    store = TraceStore(data_dir=data_dir)
    await store.initialize()
    try:
        cursor = await store._db.execute(
            """
            SELECT node_id, COUNT(*) FROM request_traces
            WHERE timestamp >= ? AND timestamp < ? AND node_id != ''
            GROUP BY node_id
            """,
            (start_ts, end_ts),
        )
        for node_id, count in await cursor.fetchall():
            counts[node_id] = int(count)
    finally:
        await store.close()
    return counts


async def build_herd_rollup(
    registry,
    install_id: str,
    agent_version: str,
    data_dir: str = "~/.fleet-manager",
    day: str | None = None,
    nickname: str = "",
) -> dict:
    """Build the herd's payload for one closed day.

    Contract: cumulative totals for a CLOSED day.  The receiver REPLACEs on
    conflict rather than SUMs, so a retried POST is idempotent -- but sending
    deltas would silently under-count on replay with nothing to alert on.
    """
    from fleet_manager.node.anonymous_rollup import build_anonymous_rollup

    if day is None:
        day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Per-model aggregates reuse the v1 builder: same stores, same privacy
    # whitelist, same error categorisation.  Only the identity and the device
    # list are new, so there is no second copy of the sensitive logic.
    payload = await build_anonymous_rollup(
        install_id=install_id,
        agent_version=agent_version,
        data_dir=data_dir,
        day=day,
        nickname=nickname,
    )

    start = datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()
    by_node = await _requests_by_node(data_dir, start, start + 86400)
    payload["devices"] = build_devices(registry, install_id, by_node)
    return payload


async def send_once(
    settings, registry, agent_version: str, state_file: Path | None = None
) -> bool:
    """Build and POST yesterday's herd rollup.  Never raises."""
    if not getattr(settings, "telemetry", True):
        return False

    from fleet_manager.common.install_id import get_install_id

    try:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        if _load_last_sent_day(state_file) == yesterday:
            return False

        payload = await build_herd_rollup(
            registry=registry,
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
            logger.debug("community telemetry sent for %s", yesterday)
            return True

        # 4xx means this build is wrong and tomorrow's will be too; record the
        # day so we do not re-send a payload the server keeps rejecting.
        if 400 <= response.status_code < 500:
            logger.debug(
                "telemetry endpoint rejected payload (%s); not retrying %s",
                response.status_code,
                yesterday,
            )
            _save_last_sent_day(yesterday, state_file)
        return False
    except Exception as exc:  # noqa: BLE001 - telemetry must never escalate
        logger.debug("community telemetry send failed: %s", exc)
        return False


async def run_scheduler(settings, registry, agent_version: str) -> None:
    """Daily loop on the router.  Exits immediately when telemetry is off."""
    if not getattr(settings, "telemetry", True):
        logger.debug("community telemetry disabled; scheduler not started")
        return

    while True:
        try:
            await asyncio.sleep(_seconds_until_next_run())
            await send_once(settings, registry, agent_version)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("community telemetry iteration failed: %s", exc)
            await asyncio.sleep(3600)
