"""Where a request's time goes, and how much of it is herd's.

The 2026-11-20 fleet benchmark measured the router adding 24 ms per request
across a network (6.5 ms on loopback) and nothing recorded which part was its
own.  Each trace now carries ``timings`` and a non-streaming response carries
``X-Fleet-Overhead-Ms``: the request's time in herd minus Ollama's own
``total_duration``.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.models.request import InferenceRequest, QueueEntry
from fleet_manager.server.streaming import request_timings
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama
from tests.test_server.test_routes import create_test_app

MODEL = "qwen3-coder:30b"


def _entry(created, enqueued, started) -> QueueEntry:
    req = InferenceRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}])
    req.created_at = created
    entry = QueueEntry(request=req, enqueued_at=enqueued)
    entry.started_at = started
    return entry


class TestRequestTimings:
    def test_splits_the_request(self):
        # arrived at 0, enqueued at 5 ms, a slot at 105 ms, done at 1,125 ms;
        # Ollama says it spent 1,000 ms.
        t = request_timings(_entry(100.0, 100.005, 100.105), 100.105, 101.125, 1000.0)
        assert t["route_ms"] == pytest.approx(5.0)
        assert t["queue_ms"] == pytest.approx(100.0)
        assert t["upstream_ms"] == pytest.approx(1020.0)
        assert t["backend_ms"] == 1000.0
        # 1,125 total - 100 queue (load, not overhead) - 1,000 Ollama
        assert t["overhead_ms"] == pytest.approx(25.0)

    def test_without_ollamas_figure_there_is_no_overhead(self):
        t = request_timings(_entry(100.0, 100.0, 100.0), 100.0, 101.0, None)
        assert "overhead_ms" not in t and "backend_ms" not in t


@pytest.fixture
def fleet(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        hb = make_heartbeat(
            node_id="studio", loaded_models=[(MODEL, 18.0)], available_models=[MODEL]
        )
        client.post("/heartbeat", json=hb.model_dump())
        lines = [
            {"model": MODEL, "message": {"role": "assistant", "content": "ready"}, "done": False},
            {
                "model": MODEL,
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 5,
                "eval_count": 1,
                "total_duration": 37_000_000,  # 37 ms, as Ollama reports it
            },
        ]
        body = "".join(json.dumps(line) + "\n" for line in lines).encode()
        _install_mock_ollama(
            client,
            "studio",
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "application/x-ndjson"}
            ),
        )
        yield client


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/chat", {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/chat/completions", {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}),
    ],
)
def test_non_streaming_response_reports_herds_overhead(fleet, path, body):
    resp = fleet.post(path, json={**body, "stream": False})
    assert resp.status_code == 200, resp.text
    overhead = float(resp.headers["X-Fleet-Overhead-Ms"])
    assert overhead >= 0.0


@pytest.mark.asyncio
async def test_timings_round_trip_through_the_trace_store(tmp_path):
    from fleet_manager.server.trace_store import TraceStore

    store = TraceStore(data_dir=str(tmp_path))
    await store.initialize()
    timings = {
        "route_ms": 1.2,
        "queue_ms": 0.0,
        "upstream_ms": 40.1,
        "backend_ms": 37.0,
        "overhead_ms": 4.3,
    }
    await store.record_trace(
        request_id="r1",
        model=MODEL,
        original_model=MODEL,
        node_id="n1",
        latency_ms=40.1,
        timings=timings,
    )
    [trace] = await store.get_trace_by_request_id("r1")
    assert trace["timings"] == timings
    await store.close()


class TestParallelismUnknownCheck:
    """Requests queueing on a node that reports no OLLAMA_NUM_PARALLEL."""

    def _node(self, num_parallel: int):
        from tests.conftest import make_node

        node = make_node(node_id="n1")
        node.ollama.num_parallel = num_parallel
        return node

    def test_fires_when_requests_wait_and_parallelism_is_unknown(self):
        from fleet_manager.server.health_engine import HealthEngine

        recs = HealthEngine()._check_parallelism_unknown(
            {"n1": {"requests": 40, "waited": 12}}, [self._node(0)]
        )
        assert [r.check_id for r in recs] == ["parallelism_unknown"]
        assert "systemctl edit ollama" in recs[0].fix

    def test_quiet_when_herd_knows_the_value(self):
        from fleet_manager.server.health_engine import HealthEngine

        recs = HealthEngine()._check_parallelism_unknown(
            {"n1": {"requests": 40, "waited": 12}}, [self._node(4)]
        )
        assert recs == []

    def test_quiet_without_queueing(self):
        from fleet_manager.server.health_engine import HealthEngine

        recs = HealthEngine()._check_parallelism_unknown(
            {"n1": {"requests": 40, "waited": 1}}, [self._node(0)]
        )
        assert recs == []


@pytest.mark.asyncio
async def test_queue_wait_stats_read_the_timings(tmp_path):
    from fleet_manager.server.trace_store import TraceStore

    store = TraceStore(data_dir=str(tmp_path))
    await store.initialize()
    for i, queue_ms in enumerate([0.5, 2500.0, 1200.0]):
        await store.record_trace(
            request_id=f"r{i}",
            model=MODEL,
            original_model=MODEL,
            node_id="n1",
            latency_ms=10.0,
            timings={"queue_ms": queue_ms},
        )
    assert await store.get_queue_wait_stats() == {"n1": {"requests": 3, "waited": 2}}
    await store.close()
