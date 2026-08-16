"""Build the anonymous community-telemetry payload.

Distinct from ``daily_rollup.py``, which builds the *account* payload for a
platform-connected fleet.  That one is keyed by a platform-issued node UUID and
needs a token; this one is keyed by a random ``install_id`` and has no account
at all.  They are deliberately separate because the receiving schemas differ:
the account tables are FK'd to ``auth.users``, anonymous installs have no such
row, so the community service has its own tables and its own endpoint.

The two privacy-critical helpers -- error categorisation and the day window --
are imported from ``daily_rollup`` rather than re-implemented.  Raw error text
must never leave the machine, and a second copy of that mapping is a second
place for it to rot.

**Contract: this payload is a snapshot of cumulative totals for a CLOSED day.**
The receiver REPLACEs on conflict rather than SUMs, precisely so a retried POST
is idempotent.  If this ever starts sending *deltas*, a replay would silently
under-count and no error would surface anywhere.  Keep sending totals.

The server validates with ``extra="forbid"``, so an unknown key is a 422 for
the whole payload, not a warning.  ``ALLOWED_*`` below therefore mirrors the
published contract at ollamaherd.com/telemetry exactly; if the two ever
disagree, the published page wins and this file is the bug.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fleet_manager.node.daily_rollup import (
    _categorize_error,
    _percentile,
    _yesterday_utc_bounds,
)

logger = logging.getLogger(__name__)

# Structural privacy, same discipline as daily_rollup: these are the ONLY keys
# that may appear.  Tests assert equality, not membership, so adding a field
# without updating the published contract fails the build.
ALLOWED_ENTRY_KEYS = frozenset(
    {
        "model",
        "requests",
        "success_count",
        "error_count",
        "prompt_tokens",
        "completion_tokens",
        "p50_latency_ms",
        "p95_latency_ms",
    }
)

ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "install_id",
        "agent_version",
        "day",
        "entries",
        "errors",
        # Present only when the operator separately opted in to a public name.
        "nickname",
        "fleet_node_count",
        "fleet_memory_gb",
    }
)

# Server-side bounds.  Mirrored here so we fail locally and skip a doomed POST
# rather than eating a 422 that nobody reads.
MAX_ENTRIES = 200
MAX_AGENT_VERSION_LEN = 32
MAX_NICKNAME_LEN = 30


async def build_anonymous_rollup(
    install_id: str,
    agent_version: str,
    data_dir: str = "~/.fleet-manager",
    day: str | None = None,
    nickname: str = "",
    fleet_node_count: int | None = None,
    fleet_memory_gb: int | None = None,
) -> dict:
    """Build one day's anonymous rollup.  Returns a dict ready to POST.

    ``entries`` is empty when the day had no traffic; the caller decides
    whether an empty day is worth sending (it is -- it distinguishes an idle
    install from a vanished one, which is what retention analysis needs).
    """
    from fleet_manager.server.latency_store import LatencyStore
    from fleet_manager.server.trace_store import TraceStore

    if day is None:
        day, start_ts, end_ts = _yesterday_utc_bounds()
    else:
        dt = datetime.fromisoformat(day).replace(tzinfo=UTC)
        start_ts = dt.timestamp()
        end_ts = start_ts + 86400

    # 1. Token counts + latency samples per model.
    totals: dict[str, dict] = {}
    latency_by_model: dict[str, list[float]] = {}

    store = LatencyStore(data_dir=data_dir)
    await store.initialize()
    try:
        cursor = await store._db.execute(
            """
            SELECT model_name,
                   SUM(COALESCE(prompt_tokens, 0)),
                   SUM(COALESCE(completion_tokens, 0))
            FROM latency_observations
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY model_name
            """,
            (start_ts, end_ts),
        )
        for model, prompt_tokens, completion_tokens in await cursor.fetchall():
            if not model:
                continue
            totals.setdefault(model, {})
            totals[model]["prompt_tokens"] = int(prompt_tokens or 0)
            totals[model]["completion_tokens"] = int(completion_tokens or 0)

        cursor = await store._db.execute(
            """
            SELECT model_name, latency_ms
            FROM latency_observations
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start_ts, end_ts),
        )
        for model, latency_ms in await cursor.fetchall():
            if model and latency_ms is not None:
                latency_by_model.setdefault(model, []).append(latency_ms)
    finally:
        await store.close()

    # 2. Request outcomes.  Error *categories* only -- never raw messages.
    errors: dict[str, int] = {}

    trace_store = TraceStore(data_dir=data_dir)
    await trace_store.initialize()
    try:
        cursor = await trace_store._db.execute(
            """
            SELECT model, status, error_message
            FROM request_traces
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start_ts, end_ts),
        )
        for model, status, err_msg in await cursor.fetchall():
            if not model:
                continue
            entry = totals.setdefault(model, {})
            entry["requests"] = entry.get("requests", 0) + 1
            if status == "completed":
                entry["success_count"] = entry.get("success_count", 0) + 1
            elif status == "failed":
                entry["error_count"] = entry.get("error_count", 0) + 1
                category = _categorize_error(err_msg)
                errors[category] = errors.get(category, 0) + 1
            # "retried" is an intermediate state: neither success nor error.
    finally:
        await trace_store.close()

    # 3. Project into the wire shape.  Explicit construction rather than
    #    passing a dict through, so a new column in either store cannot ride
    #    along into a payload unnoticed.
    entries = []
    for model, agg in sorted(totals.items()):
        samples = latency_by_model.get(model, [])
        p50 = _percentile(samples, 50)
        p95 = _percentile(samples, 95)
        entries.append(
            {
                "model": model[:160],
                "requests": agg.get("requests", 0),
                "success_count": agg.get("success_count", 0),
                "error_count": agg.get("error_count", 0),
                "prompt_tokens": agg.get("prompt_tokens", 0),
                "completion_tokens": agg.get("completion_tokens", 0),
                "p50_latency_ms": int(p50) if p50 is not None else None,
                "p95_latency_ms": int(p95) if p95 is not None else None,
            }
        )

    # The server rejects >200 entries outright.  Keep the busiest models rather
    # than an arbitrary alphabetical slice, so a truncated payload still
    # describes what the install actually runs.
    if len(entries) > MAX_ENTRIES:
        entries.sort(key=lambda e: e["requests"], reverse=True)
        entries = entries[:MAX_ENTRIES]
        logger.debug("anonymous rollup truncated to %d models", MAX_ENTRIES)

    payload: dict = {
        "install_id": install_id,
        "agent_version": agent_version[:MAX_AGENT_VERSION_LEN],
        "day": day,
        "entries": entries,
        "errors": errors,
    }

    # Nickname + fleet shape are the *second* opt-in: they are the only fields
    # that can ever appear publicly, so they are omitted entirely rather than
    # sent as null when the operator has not named their herd.
    if nickname:
        payload["nickname"] = nickname[:MAX_NICKNAME_LEN]
        if fleet_node_count is not None:
            payload["fleet_node_count"] = fleet_node_count
        if fleet_memory_gb is not None:
            payload["fleet_memory_gb"] = fleet_memory_gb

    return payload
