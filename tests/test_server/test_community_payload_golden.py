"""The anonymous community-telemetry payload, pinned byte for byte.

Written before the platform integration was removed
(``docs/plans/remove-platform-integration.md``), whose helper moves --
``categorize_error``, ``percentile``, the "yesterday" bounds -- all sit under
this payload.  The other telemetry tests assert the payload's *shape*; this one
asserts its *content*, so a refactor that changes a category, a percentile or a
device row fails here instead of reaching ollamaherd.com.

Built through ``build_herd_rollup`` -- the full payload the router sends,
devices included -- with an explicit ``day`` so no clock is involved.  If this
test fails after an intentional change to the payload, that change is a wire
contract change: update ollamaherd.com/telemetry first, then the expected
string.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from fleet_manager.server.community_telemetry import build_herd_rollup
from fleet_manager.server.latency_store import LatencyStore
from fleet_manager.server.trace_store import TraceStore

INSTALL_ID = "22222222-2222-2222-2222-222222222222"
DAY = "2026-10-20"

# One error message per category _categorize_error knows, plus the falsy and
# unmatched cases -- the histogram is the part of the payload most exposed to a
# helper moving modules.
FAILED_MESSAGES = [
    "404 model not found",
    "400 num_ctx exceeds the context window",
    "400 malformed body",
    "502 bad gateway from backend",
    "CUDA out of memory",
    "prompt is too long for the context",
    "permission denied writing cache",
    "ReadTimeout",
    "ConnectError",
    None,
    "something nobody anticipated",
]

# Latencies chosen so p50/p95 interpolate between samples and int() truncates
# a fractional result -- the arithmetic a shared percentile must preserve.
LATENCIES = {
    "gpt-oss:120b": [812.4, 1033.9, 977.1, 1500.25, 640.6, 2210.75, 905.3],
    "gemma3:27b": [2175.5, 2619.25, 1980.8],
    "nomic-embed-text": [41.3],
}


def _ts(hour: int, minute: int = 0) -> float:
    return datetime.fromisoformat(DAY).replace(tzinfo=UTC, hour=hour, minute=minute).timestamp()


async def _seed(data_dir: str) -> None:
    latency = LatencyStore(data_dir=data_dir)
    await latency.initialize()
    try:
        minute = 0
        for model, values in LATENCIES.items():
            for i, ms in enumerate(values):
                await latency._db.execute(
                    "INSERT INTO latency_observations (node_id, model_name, latency_ms, "
                    "tokens_generated, timestamp, prompt_tokens, completion_tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("bb", model, ms, 50 + i, _ts(3, minute), 900 + 7 * i, 60 + 3 * i),
                )
                minute += 1
        # Outside the day on both sides: must not be counted.
        for ts in (_ts(0) - 1, _ts(0) + 86400):
            await latency._db.execute(
                "INSERT INTO latency_observations (node_id, model_name, latency_ms, "
                "tokens_generated, timestamp, prompt_tokens, completion_tokens) "
                "VALUES ('bb', 'gpt-oss:120b', 99999, 1, ?, 1, 1)",
                (ts,),
            )
        await latency._db.commit()
    finally:
        await latency.close()

    traces = TraceStore(data_dir=data_dir)
    await traces.initialize()
    try:
        rows = [("gpt-oss:120b", "bb", "completed", None)] * 9
        rows += [("gemma3:27b", "mini", "completed", None)] * 2
        rows.append(("gemma3:27b", "mini", "retried", None))
        for msg in FAILED_MESSAGES:
            rows.append(("gpt-oss:120b", "bb", "failed", msg))
        rows.append(("nomic-embed-text", "bb", "completed", None))
        for i, (model, node, status, msg) in enumerate(rows):
            await traces._db.execute(
                "INSERT INTO request_traces (request_id, model, original_model, node_id, "
                "status, error_message, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"req-{i}", model, model, node, status, msg, _ts(5, i)),
            )
        await traces._db.execute(
            "INSERT INTO request_traces (request_id, model, original_model, node_id, "
            "status, error_message, timestamp) VALUES "
            "('outside', 'gpt-oss:120b', 'gpt-oss:120b', 'bb', 'failed', '500', ?)",
            (_ts(0) + 86400,),
        )
        await traces._db.commit()
    finally:
        await traces.close()


def _registry() -> SimpleNamespace:
    def node(node_id, chip, mem, cores, ollama_version, mlx_version=""):
        return SimpleNamespace(
            node_id=node_id,
            hardware=SimpleNamespace(chip=chip, memory_total_gb=mem, cores_physical=cores),
            ollama=SimpleNamespace(version=ollama_version),
            agent_version="0.11.4",
            mlx_version=mlx_version,
        )

    return SimpleNamespace(
        _nodes={
            "bb": node("bb", "Apple M3 Ultra", 512, 32, "0.40.2", "0.32.4"),
            "mini": node("mini", "Apple M4 Pro", 48.7, 14, "0.40.2"),
        }
    )


EXPECTED = (
    '{"agent_version": "0.11.4", "day": "2026-10-20", "devices": [{"agent_version": "0.11'
    '.4", "chip": "Apple M3 Ultra", "cores": 32, "device_id": "30b7012add886bcc", "memory'
    '_gb": 512, "mlx_version": "0.32.4", "ollama_version": "0.40.2", "requests": 21}, {"a'
    'gent_version": "0.11.4", "chip": "Apple M4 Pro", "cores": 14, "device_id": "a7e2d329'
    '30d91807", "memory_gb": 48, "mlx_version": "", "ollama_version": "0.40.2", "requests'
    '": 3}], "entries": [{"completion_tokens": 189, "error_count": 0, "model": "gemma3:27'
    'b", "p50_latency_ms": 2175, "p95_latency_ms": 2574, "prompt_tokens": 2721, "requests'
    '": 3, "success_count": 2}, {"completion_tokens": 483, "error_count": 11, "model": "g'
    'pt-oss:120b", "p50_latency_ms": 977, "p95_latency_ms": 1997, "prompt_tokens": 6447, '
    '"requests": 20, "success_count": 9}, {"completion_tokens": 60, "error_count": 0, "mo'
    'del": "nomic-embed-text", "p50_latency_ms": 41, "p95_latency_ms": 41, "prompt_tokens'
    '": 900, "requests": 1, "success_count": 1}], "errors": {"bad_request": 1, "connectio'
    'n_error": 1, "context_too_long": 2, "model_not_found": 1, "other": 1, "permission_er'
    'ror": 1, "server_error": 1, "timeout": 1, "unknown": 1, "vram_exceeded": 1}, "instal'
    'l_id": "22222222-2222-2222-2222-222222222222", "nickname": "NeonHerd"}'
)


@pytest.mark.asyncio
async def test_community_payload_is_byte_identical(tmp_path):
    await _seed(str(tmp_path))
    payload = await build_herd_rollup(
        _registry(),
        install_id=INSTALL_ID,
        agent_version="0.11.4",
        data_dir=str(tmp_path),
        day=DAY,
        nickname="NeonHerd",
    )
    assert json.dumps(payload, sort_keys=True) == EXPECTED
