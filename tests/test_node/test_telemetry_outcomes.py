"""Every trace status reaches telemetry, or is excluded on purpose.

The rollup counted ``completed`` as success and ``failed`` as error; every other
status added to ``requests`` and nothing else.  Over 30 days that was 2,678
requests with no recorded outcome, more than every error category together.
See docs/plans/silent-failures-2026-11.md, issue 4.
"""

from __future__ import annotations

import logging

import pytest

from fleet_manager.node.anonymous_rollup import (
    ALLOWED_ENTRY_KEYS,
    EXCLUDED_STATUSES,
    OUTCOME_FIELDS,
    build_anonymous_rollup,
)
from fleet_manager.server.trace_store import TRACE_STATUSES, TraceStore

DAY = "2026-11-17"


def test_every_trace_status_is_counted_or_excluded():
    assert set(OUTCOME_FIELDS) | EXCLUDED_STATUSES == TRACE_STATUSES
    assert not set(OUTCOME_FIELDS) & EXCLUDED_STATUSES


def test_every_outcome_field_is_on_the_wire():
    assert set(OUTCOME_FIELDS.values()) <= ALLOWED_ENTRY_KEYS


@pytest.mark.asyncio
async def test_outcomes_counted_and_retries_excluded(tmp_path):
    from datetime import UTC, datetime

    ts = datetime.fromisoformat(DAY).replace(tzinfo=UTC, hour=6).timestamp()
    store = TraceStore(data_dir=str(tmp_path))
    await store.initialize()
    statuses = [
        "completed",
        "completed",
        "retried",
        "failed",
        "rejected",
        "incomplete",
        "no_output",
        "client_disconnected",
    ]
    for i, status in enumerate(statuses):
        await store._db.execute(
            "INSERT INTO request_traces (request_id, model, original_model, node_id, status, "
            "error_message, timestamp) VALUES (?, 'm:1b', 'm:1b', 'n', ?, ?, ?)",
            (f"r{i}", status, "HTTP 400: bad" if status == "failed" else None, ts + i),
        )
    await store._db.commit()
    await store.close()

    payload = await build_anonymous_rollup(
        "11111111-1111-1111-1111-111111111111", "0.12.6", data_dir=str(tmp_path), day=DAY
    )
    (entry,) = payload["entries"]
    assert entry["requests"] == 7  # eight rows, one of them a retried attempt
    assert (entry["success_count"], entry["error_count"]) == (2, 1)
    assert (
        entry["rejected_count"],
        entry["incomplete_count"],
        entry["no_output_count"],
        entry["disconnected_count"],
    ) == (1, 1, 1, 1)
    # The service rejects outcomes that exceed requests.
    outcomes = sum(entry[f] for f in OUTCOME_FIELDS.values())
    assert outcomes == entry["requests"]
    assert payload["errors"] == {"bad_request": 1}


@pytest.mark.asyncio
async def test_unknown_status_is_logged_and_still_recorded(tmp_path, caplog):
    store = TraceStore(data_dir=str(tmp_path))
    await store.initialize()
    with caplog.at_level(logging.ERROR, logger="fleet_manager.server.trace_store"):
        await store.record_trace(
            request_id="typo-1", model="m", original_model="m", node_id="n", status="complted"
        )
    cursor = await store._db.execute("SELECT status FROM request_traces")
    assert [r[0] for r in await cursor.fetchall()] == ["complted"]
    await store.close()
    assert "unknown status 'complted'" in caplog.text
