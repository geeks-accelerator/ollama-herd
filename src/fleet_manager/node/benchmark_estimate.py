"""Throughput benchmark estimation for platform registration.

Produces a `tokens_per_sec` number for the platform's `benchmark` field
during node registration.  Prefers real measurements from the local
trace store; falls back to a hardware-derived estimate when no history
exists (first-connect case).

The platform uses this to classify nodes into throughput tiers
(economy / standard / performance / premium) which affects earning rates.
"""

from __future__ import annotations

import logging
import platform as _platform

import psutil

logger = logging.getLogger(__name__)


def _hardware_estimate() -> float:
    """Rough tokens/sec estimate from hardware specs.

    Used when no trace history exists.  Returns conservative mid-range
    numbers; real measurements will update this on next re-registration.
    """
    total_ram_gb = psutil.virtual_memory().total / (1024**3)
    is_apple_silicon = (
        _platform.system() == "Darwin"
        and _platform.machine() == "arm64"
    )

    # Mac Studio M3 Ultra (512GB): measured ~90 tok/s on gpt-oss:120b
    # Mac Mini M4 (64GB): measured ~40 tok/s on llama3:8b
    # RTX 4090: measured ~120 tok/s on llama3:8b
    # Standard x86 CPU-only: ~5-10 tok/s

    if is_apple_silicon:
        if total_ram_gb >= 256:
            return 90.0  # Mac Studio Ultra tier
        if total_ram_gb >= 64:
            return 50.0  # Mac Studio / Mac Mini Pro
        if total_ram_gb >= 32:
            return 30.0  # Mac Mini / MacBook Pro M-series
        return 15.0  # Smaller M-series

    # Non-Apple Silicon — harder to estimate without GPU info
    if total_ram_gb >= 128:
        return 40.0  # Workstation-class
    if total_ram_gb >= 32:
        return 20.0
    return 8.0


async def estimate_throughput(data_dir: str = "~/.fleet-manager") -> float:
    """Estimate this node's tokens/sec from recent trace data.

    Queries the local latency store (if populated) for recent
    observations and computes an average tok/s.  Falls back to a
    hardware-based estimate if no data is available.

    Returns a float tokens/sec value.
    """
    # Try to read from the local latency store
    try:
        from fleet_manager.server.latency_store import LatencyStore

        store = LatencyStore(data_dir=data_dir)
        await store.initialize()

        # Query recent completion_tokens / latency_ms from observations
        # to compute tokens per second.  We need both fields to be present.
        cursor = await store._db.execute(
            """
            SELECT completion_tokens, latency_ms
            FROM latency_observations
            WHERE completion_tokens > 0
              AND latency_ms > 100
              AND timestamp >= strftime('%s', 'now', '-7 days')
            ORDER BY timestamp DESC
            LIMIT 100
            """
        )
        rows = await cursor.fetchall()
        await store.close()

        if rows:
            # tok/s per row, then average
            rates = [
                (r[0] / (r[1] / 1000.0))
                for r in rows
                if r[0] and r[1] and r[1] > 0
            ]
            if rates:
                avg = sum(rates) / len(rates)
                logger.info(
                    f"Throughput estimate: {avg:.1f} tok/s "
                    f"(from {len(rates)} recent observations)"
                )
                return round(avg, 1)
    except Exception as exc:
        logger.debug(f"Latency store query failed: {exc}")

    # Fall back to hardware estimate
    estimate = _hardware_estimate()
    logger.info(
        f"Throughput estimate: {estimate:.1f} tok/s "
        f"(hardware-derived; no trace history yet)"
    )
    return estimate


async def build_benchmark_payload(data_dir: str = "~/.fleet-manager") -> dict:
    """Build the benchmark dict for POST /api/nodes/register.

    Platform expects at minimum `tokens_per_sec`.  We include additional
    fields that may become useful for tiered routing decisions.
    """
    tps = await estimate_throughput(data_dir=data_dir)
    total_ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
    return {
        "tokens_per_sec": tps,
        "total_ram_gb": total_ram_gb,
        "arch": _platform.machine(),
        "platform": _platform.system().lower(),
    }
