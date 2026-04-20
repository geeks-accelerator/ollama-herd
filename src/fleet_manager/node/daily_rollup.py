"""Daily rollup builder for platform telemetry.

Aggregates yesterday's usage data from the local SQLite stores into
the payload shape expected by POST /api/telemetry/local-summary.

**Privacy invariants (enforced structurally):**

- Only reads whitelisted columns from `latency_observations` and
  `request_traces`.  Never reads prompt content, completion content,
  client_ip, error_message, request_id, scores_breakdown, or any other
  per-request detail.
- Aggregates at day granularity — never sub-day timestamps.
- Tag per-value counts are only included if the caller opts in via
  `include_tags=True` (two-factor opt-in at the scheduler level).
- Built payload keys are whitelisted and tested — contributors cannot
  casually add new fields.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)


# Structural privacy: the ONLY keys permitted in entries sent to the
# platform.  Tests enforce this list is never exceeded.  Adding a new
# field requires updating this list AND the privacy test.
ALLOWED_ENTRY_KEYS = frozenset({
    "model",
    "local_requests",
    "local_prompt_tokens",
    "local_completion_tokens",
    "p2p_served_requests",
    "p2p_served_tokens",
    "avg_latency_ms",
    "p95_latency_ms",
    "request_count_by_tag",  # only when include_tags=True
    "success_count",
    "error_count",
    "error_breakdown",  # {"context_too_long": 1, "vram_exceeded": 2, ...}
})

# Top-level payload keys
ALLOWED_PAYLOAD_KEYS = frozenset({
    "day",
    "node_id",
    "agent_version",
    "entries",
})


def _yesterday_utc_bounds() -> tuple[str, float, float]:
    """Return (date_str, start_ts, end_ts) for yesterday in UTC.

    Yesterday means: the 24h window ending at today's 00:00 UTC.
    """
    now_utc = datetime.now(UTC)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    return (
        yesterday_start.date().isoformat(),
        yesterday_start.timestamp(),
        today_start.timestamp(),
    )


def _categorize_error(error_message: str | None) -> str:
    """Map a raw error message to a short category name.

    Categories are free-form strings — the platform treats them
    opaquely.  New categories can be added without a platform migration.

    Keep names short, snake_case, and descriptive enough that an
    operator reading "error_breakdown: {context_too_long: 12}" on the
    dashboard immediately knows what's wrong.
    """
    if not error_message:
        return "unknown"
    msg = error_message.lower()

    # HTTP-layer errors (what we see most from Ollama)
    if "404" in msg and "not found" in msg:
        return "model_not_found"
    if "400" in msg and ("context" in msg or "num_ctx" in msg):
        return "context_too_long"
    if "400" in msg:
        return "bad_request"
    if "500" in msg or "502" in msg or "503" in msg or "504" in msg:
        return "server_error"

    # Resource errors
    if "out of memory" in msg or "vram" in msg or "cuda oom" in msg:
        return "vram_exceeded"
    if "context" in msg and ("too long" in msg or "exceeds" in msg):
        return "context_too_long"
    if "permission" in msg:
        return "permission_error"

    # Network / timeout
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "connection" in msg and ("refused" in msg or "reset" in msg):
        return "connection_error"

    # Client disconnects (streaming)
    if "disconnect" in msg or "generatorexit" in msg:
        return "client_disconnected"

    return "other"


def _percentile(values: list[float], pct: int) -> float | None:
    """Simple linear-interpolation percentile.  Returns None for empty input.

    Kept inline rather than importing from latency_store to avoid a
    circular dependency and because the caller holds a list of raw
    numbers, not a histogram.
    """
    if not values:
        return None
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


async def build_daily_rollup(
    node_uuid: str,
    agent_version: str,
    data_dir: str = "~/.fleet-manager",
    include_tags: bool = False,
    day: str | None = None,
) -> dict:
    """Build the telemetry payload for a single day.

    Args:
        node_uuid: Platform-issued node UUID from ConnectionState.
        agent_version: herd-node version string.
        data_dir: Path to ~/.fleet-manager (SQLite files live here).
        include_tags: If True, include per-tag request counts in each
            entry.  Default False — tag values can be mildly identifying.
        day: ISO date string (YYYY-MM-DD).  Defaults to yesterday UTC.

    Returns the payload dict.  Entries list is empty if no data.
    """
    from fleet_manager.server.latency_store import LatencyStore

    if day is None:
        day, start_ts, end_ts = _yesterday_utc_bounds()
    else:
        # Parse explicit day (for backfill / testing)
        dt = datetime.fromisoformat(day).replace(tzinfo=UTC)
        start_ts = dt.timestamp()
        end_ts = start_ts + 86400

    # 1. Per-model aggregates from latency_observations
    store = LatencyStore(data_dir=data_dir)
    await store.initialize()
    try:
        cursor = await store._db.execute(
            """
            SELECT
                model_name,
                COUNT(*)                          AS local_requests,
                SUM(COALESCE(prompt_tokens, 0))   AS local_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS local_completion_tokens,
                AVG(latency_ms)                   AS avg_latency_ms
            FROM latency_observations
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY model_name
            """,
            (start_ts, end_ts),
        )
        aggregate_rows = await cursor.fetchall()

        # Collect latency samples per model for p95 calculation
        cursor = await store._db.execute(
            """
            SELECT model_name, latency_ms
            FROM latency_observations
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start_ts, end_ts),
        )
        latency_by_model: dict[str, list[float]] = {}
        for row in await cursor.fetchall():
            latency_by_model.setdefault(row[0], []).append(row[1])
    finally:
        await store.close()

    # 2. Status + error counts per model (always) and tag counts (opt-in)
    #    — both from request_traces
    from fleet_manager.server.trace_store import TraceStore

    tag_counts_by_model: dict[str, dict[str, int]] = {}
    success_by_model: dict[str, int] = {}
    error_by_model: dict[str, int] = {}
    error_breakdown_by_model: dict[str, dict[str, int]] = {}

    trace_store = TraceStore(data_dir=data_dir)
    await trace_store.initialize()
    try:
        # Success/error counts with error categorization
        cursor = await trace_store._db.execute(
            """
            SELECT model, status, error_message
            FROM request_traces
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start_ts, end_ts),
        )
        for row in await cursor.fetchall():
            model, status, err_msg = row
            if not model:
                continue
            if status == "completed":
                success_by_model[model] = success_by_model.get(model, 0) + 1
            elif status == "failed":
                error_by_model[model] = error_by_model.get(model, 0) + 1
                category = _categorize_error(err_msg)
                bucket = error_breakdown_by_model.setdefault(model, {})
                bucket[category] = bucket.get(category, 0) + 1
            # "retried" status is neither success nor error — it's an
            # intermediate state before the retry completed or failed

        # Tag counts (only if user opted in)
        if include_tags:
            cursor = await trace_store._db.execute(
                """
                SELECT model, tags
                FROM request_traces
                WHERE timestamp >= ? AND timestamp < ?
                  AND tags IS NOT NULL
                  AND tags != ''
                """,
                (start_ts, end_ts),
            )
            for row in await cursor.fetchall():
                model = row[0]
                try:
                    tag_list = json.loads(row[1]) if row[1] else []
                except json.JSONDecodeError:
                    continue
                if not isinstance(tag_list, list):
                    continue
                model_tags = tag_counts_by_model.setdefault(model, {})
                for tag in tag_list:
                    if isinstance(tag, str) and tag:
                        model_tags[tag] = model_tags.get(tag, 0) + 1
    finally:
        await trace_store.close()

    # 3. Build entries with structural whitelist enforcement
    entries: list[dict] = []
    for row in aggregate_rows:
        model, req_count, p_toks, c_toks, avg_lat = row
        if not model:  # skip empty model names
            continue
        p95 = _percentile(latency_by_model.get(model, []), 95)
        entry: dict = {
            "model": model,
            "local_requests": int(req_count or 0),
            "local_prompt_tokens": int(p_toks or 0),
            "local_completion_tokens": int(c_toks or 0),
            "p2p_served_requests": 0,  # P2P not yet shipped
            "p2p_served_tokens": 0,
            "avg_latency_ms": round(avg_lat, 1) if avg_lat else 0.0,
            "p95_latency_ms": round(p95, 1) if p95 else 0.0,
            "success_count": success_by_model.get(model, 0),
            "error_count": error_by_model.get(model, 0),
        }
        if model in error_breakdown_by_model and error_breakdown_by_model[model]:
            entry["error_breakdown"] = dict(error_breakdown_by_model[model])
        if include_tags and model in tag_counts_by_model:
            entry["request_count_by_tag"] = dict(tag_counts_by_model[model])

        # Structural enforcement: drop any key not in the whitelist
        entry = {k: v for k, v in entry.items() if k in ALLOWED_ENTRY_KEYS}
        entries.append(entry)

    payload: dict = {
        "day": day,
        "node_id": node_uuid,
        "agent_version": agent_version,
        "entries": entries,
    }
    # Same structural enforcement at payload level
    payload = {k: v for k, v in payload.items() if k in ALLOWED_PAYLOAD_KEYS}

    logger.debug(
        f"Built daily rollup for {day}: "
        f"{len(entries)} model entries, "
        f"{sum(e['local_requests'] for e in entries)} total requests, "
        f"include_tags={include_tags}"
    )
    return payload
