"""Platform heartbeat sender — signs + POSTs metrics every 60s.

Sends signed heartbeats to platform.ollamaherd.com/api/heartbeats so
the platform's Nodes-detail dashboard can show:
  - Current CPU%, memory, VRAM
  - Per-model queue depths + loaded models (hot in VRAM)
  - Uptime over last 24h
  - Failure count since last heartbeat

Cadence: 60s.  The platform docs suggest ~5s in the P2P-era, but for
Stage 1 (dashboard-only), 60s gives good freshness at modest bandwidth.

Signing: Ed25519 over the canonical JSON of the `raw_payload` field.
The outer payload duplicates signed fields + adds the `signature` and
`raw_payload` envelope.  Simpler and more correct than maintaining two
separate field sets.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import UTC, datetime

from fleet_manager import __version__
from fleet_manager.node import platform_client, platform_connection

logger = logging.getLogger(__name__)

# How often to send heartbeats (60s).  Short enough for live dashboards,
# long enough not to be a bandwidth problem.
_HEARTBEAT_INTERVAL_S = 60.0

# Track accumulated counters between heartbeats
_requests_completed_since_last: int = 0
_requests_failed_since_last: int = 0
_tokens_served_since_last: int = 0
_compute_seconds_since_last: float = 0.0


def _load_private_key():
    """Load the Ed25519 private key from ~/.fleet-manager/node_key.ed25519."""
    from cryptography.hazmat.primitives import serialization

    with open(platform_connection.KEYPAIR_FILE, "rb") as f:
        priv_bytes = f.read()
    return serialization.load_pem_private_key(priv_bytes, password=None)


def _sign_payload(payload: dict) -> str:
    """Sign the canonical JSON of `payload` with the node's Ed25519 key.

    Returns a base64 signature string.  Canonical JSON = sorted keys,
    no whitespace — so the platform can reproduce the exact bytes.
    """
    private_key = _load_private_key()
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = private_key.sign(canonical)
    return base64.b64encode(signature).decode("ascii")


def record_request(
    completed: bool,
    tokens: int = 0,
    compute_seconds: float = 0.0,
) -> None:
    """Record a completed or failed request for the next heartbeat.

    Called by the streaming proxy / routing code when a request finishes.
    Accumulates counters that ship on the next heartbeat.

    NOTE: Currently unused — the node-side request tracking lives in
    the trace_store, which the heartbeat reads directly.  Kept as the
    future extension point for in-flight request tracking without
    SQLite reads on the hot path.
    """
    global _requests_completed_since_last, _requests_failed_since_last
    global _tokens_served_since_last, _compute_seconds_since_last
    if completed:
        _requests_completed_since_last += 1
    else:
        _requests_failed_since_last += 1
    _tokens_served_since_last += tokens
    _compute_seconds_since_last += compute_seconds


def _reset_counters() -> None:
    global _requests_completed_since_last, _requests_failed_since_last
    global _tokens_served_since_last, _compute_seconds_since_last
    _requests_completed_since_last = 0
    _requests_failed_since_last = 0
    _tokens_served_since_last = 0
    _compute_seconds_since_last = 0.0


async def _gather_metrics() -> dict:
    """Collect current fleet metrics for the heartbeat payload.

    Reads from the local SQLite stores + psutil + the router registry
    (via HTTP to localhost).  Returns the `raw_payload` dict — what
    will be signed and what the platform authenticates.
    """
    import psutil

    state = platform_connection.load_state()
    if state is None:
        raise RuntimeError("Heartbeat called without platform connection")

    # System metrics
    mem = psutil.virtual_memory()
    cpu_pct = psutil.cpu_percent(interval=None)

    # Recent request stats from local trace store
    queue_depth = 0
    queue_depths_by_model: dict[str, int] = {}
    loaded_models: list[str] = []
    vram_used_gb = 0.0
    vram_total_gb = round(mem.total / (1024**3), 1)

    # Prefer reading live state from the router's /fleet/status (this
    # node's own router, localhost).  Falls back to 0s if unreachable.
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://localhost:11435/fleet/status")
            if resp.status_code == 200:
                fleet = resp.json()
                # Our own node's data
                for node in fleet.get("nodes", []):
                    ollama = node.get("ollama") or {}
                    for m in ollama.get("models_loaded", []):
                        name = m.get("name", "")
                        if name:
                            loaded_models.append(name)
                            vram_used_gb += m.get("size_gb", 0) or 0
                for _key, q in fleet.get("queues", {}).items():
                    depth = q.get("pending", 0) + q.get("in_flight", 0)
                    queue_depth += depth
                    model = q.get("model", "")
                    if model:
                        queue_depths_by_model[model] = depth
    except Exception as exc:
        logger.debug(f"heartbeat: could not read /fleet/status: {exc}")

    # Uptime: last 24h ratio of completed/total requests (rough SLO)
    try:
        from fleet_manager.server.trace_store import TraceStore

        ts = TraceStore()
        await ts.initialize()
        try:
            cursor = await ts._db.execute(
                """
                SELECT
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS ok,
                    COUNT(*) AS total
                FROM request_traces
                WHERE timestamp >= strftime('%s', 'now', '-24 hours')
                """
            )
            row = await cursor.fetchone()
            if row and row[1] and row[1] > 0:
                uptime_pct_24h = round((row[0] / row[1]) * 100, 2)
            else:
                uptime_pct_24h = 100.0
        finally:
            await ts.close()
    except Exception:
        uptime_pct_24h = 100.0

    # Build signed payload
    raw_payload: dict = {
        "node_id": state.node_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "cpu_pct": round(cpu_pct, 1),
        "memory_used_gb": round(mem.used / (1024**3), 1),
        "memory_total_gb": round(mem.total / (1024**3), 1),
        "vram_used_gb": round(vram_used_gb, 1),
        "vram_total_gb": vram_total_gb,
        "queue_depth": queue_depth,
        "queue_depths_by_model": queue_depths_by_model,
        "loaded_models": loaded_models,
        "requests_completed": _requests_completed_since_last,
        "requests_failed": _requests_failed_since_last,
        "tokens_served": _tokens_served_since_last,
        "compute_seconds": round(_compute_seconds_since_last, 2),
        "uptime_pct_24h": uptime_pct_24h,
        "agent_version": __version__,
    }
    return raw_payload


async def _send_one_heartbeat() -> bool:
    """Build + sign + POST one heartbeat.  Returns True on success.

    Signature contract (platform agreed 2026-04-20):
      1. Build body with all fields EXCEPT signature.
      2. Compute Ed25519 over canonical JSON of body.
      3. Add signature field to body.
      4. POST the body as-is.

    The platform reads the raw request bytes, pops `signature`, and
    verifies against what's left.  No separate raw_payload envelope,
    no re-serialization drift risk.  Adding fields can't invalidate
    old signatures because there's no canonicalization step on the
    platform side that might differ from ours.
    """
    try:
        body = await _gather_metrics()
    except Exception as exc:
        logger.debug(f"heartbeat: failed to gather metrics: {exc}")
        return False

    # Sign the body BEFORE adding the signature field
    signature = _sign_payload(body)
    body["signature"] = signature

    try:
        await platform_client.post("/api/heartbeats", json=body)
    except platform_client.InvalidTokenError as exc:
        logger.warning(f"heartbeat: token rejected — {exc}")
        return False
    except platform_connection.PlatformUnreachableError as exc:
        logger.debug(f"heartbeat: platform unreachable — {exc}")
        return False
    except Exception as exc:
        logger.debug(f"heartbeat: unexpected error — {exc}")
        return False

    _reset_counters()
    return True


async def run_scheduler() -> None:
    """Background task — POST a heartbeat every 60 seconds.

    Only runs when platform is connected.  If connection is lost mid-run,
    subsequent heartbeats fail gracefully and retry on next tick.
    """
    logger.info(
        f"platform heartbeat: scheduler started "
        f"(every {_HEARTBEAT_INTERVAL_S:.0f}s)"
    )
    last_success = time.time()
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            logger.info("platform heartbeat: scheduler cancelled")
            raise

        ok = await _send_one_heartbeat()
        if ok:
            last_success = time.time()
        else:
            staleness_min = (time.time() - last_success) / 60
            # Quietly tolerate short outages; warn louder after 10 min
            if staleness_min > 10:
                logger.warning(
                    f"platform heartbeat: {staleness_min:.0f} min since last "
                    f"successful send"
                )
