"""A backend that fails every request before any output stops winning the first try.

Run 6: Metal ran out of memory on a 24 GB M4 and Ollama's llama-server went into
a permanent error state, failing every request in milliseconds.  The retry sent
each request to the other Mac, but the broken one had nothing queued, so it
scored as the idlest node and every new request asked it first.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.backend_failures import BackendFailureMemory
from fleet_manager.server.health_engine import HealthEngine, Severity
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama, create_test_app

MODEL = "gpt-oss:20b"
WEDGED = "s2 ended the stream with no output"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestMemory:
    def test_failing_after_threshold_consecutive_failures(self):
        m = BackendFailureMemory(clock=FakeClock())
        for _ in range(BackendFailureMemory.THRESHOLD - 1):
            m.record_failure("s2", MODEL, WEDGED)
        assert not m.is_failing("s2", MODEL)
        m.record_failure("s2", MODEL, WEDGED)
        assert m.is_failing("s2", MODEL)
        assert m.failing() == [
            {"node_id": "s2", "model": MODEL, "consecutive_failures": 3, "last_error": WEDGED}
        ]

    def test_a_success_clears_it(self):
        m = BackendFailureMemory(clock=FakeClock())
        for _ in range(5):
            m.record_failure("s2", MODEL)
        m.record_success("s2", MODEL)
        assert not m.is_failing("s2", MODEL)
        m.record_failure("s2", MODEL)
        assert not m.is_failing("s2", MODEL)  # counting starts again

    def test_cool_off_lets_one_request_probe_again(self):
        clock = FakeClock()
        m = BackendFailureMemory(clock=clock)
        for _ in range(3):
            m.record_failure("s2", MODEL)
        clock.t += BackendFailureMemory.COOL_OFF_S + 1
        assert not m.is_failing("s2", MODEL)
        m.record_failure("s2", MODEL)  # the probe failed too
        assert m.is_failing("s2", MODEL)

    def test_per_model(self):
        m = BackendFailureMemory(clock=FakeClock())
        for _ in range(3):
            m.record_failure("s2", MODEL)
        assert not m.is_failing("s2", "qwen2.5:1.5b")
        assert not m.is_failing("s1", MODEL)


@pytest.mark.asyncio
class TestScoring:
    async def _two_nodes(self):
        settings = ServerSettings()
        registry = NodeRegistry(settings)
        for nid in ("s1", "s2"):
            await registry.update_from_heartbeat(
                make_heartbeat(
                    node_id=nid, memory_total=24.0, memory_used=15.0, loaded_models=[(MODEL, 13.0)]
                )
            )
        return ScoringEngine(settings, registry)

    async def test_a_failing_idle_node_loses_to_a_busy_working_one(self):
        scorer = await self._two_nodes()
        for _ in range(3):
            scorer.failures.record_failure("s2", MODEL, WEDGED)
        results = scorer.score_request(MODEL, {f"s1:{MODEL}": 4})
        assert [r.node_id for r in results] == ["s1", "s2"]
        assert results[1].scores_breakdown["failing"] == -ScoringEngine.FAILING_BACKEND_PENALTY

    async def test_a_failing_node_is_still_a_candidate_when_alone(self):
        settings = ServerSettings()
        registry = NodeRegistry(settings)
        await registry.update_from_heartbeat(
            make_heartbeat(node_id="s2", memory_total=24.0, loaded_models=[(MODEL, 13.0)])
        )
        scorer = ScoringEngine(settings, registry)
        for _ in range(3):
            scorer.failures.record_failure("s2", MODEL)
        assert [r.node_id for r in scorer.score_request(MODEL, {})] == ["s2"]

    async def test_auto_routing_sees_it_too(self):
        scorer = await self._two_nodes()
        for _ in range(3):
            scorer.failures.record_failure("s2", MODEL)
        ranked = scorer.score_loaded_models(None, {})
        assert ranked[0][0].node_id == "s1"
        assert ranked[-1][0].scores_breakdown["failing"] < 0


DONE = "\n".join(
    json.dumps(x)
    for x in (
        {"model": MODEL, "message": {"role": "assistant", "content": "OK"}, "done": False},
        {"model": MODEL, "done": True, "done_reason": "stop", "prompt_eval_count": 5,
         "eval_count": 1},
    )
) + "\n"  # fmt: skip


def test_through_the_app_new_requests_stop_asking_the_broken_node(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        # s2 first: between two idle nodes the first registered wins the tie, so
        # the broken node is the one asked first, as it was on the real Macs.
        for node, ip in (("s2", "10.0.0.2"), ("s1", "10.0.0.1")):
            client.post(
                "/heartbeat",
                json=make_heartbeat(
                    node_id=node, lan_ip=ip, loaded_models=[(MODEL, 13.0)], available_models=[MODEL]
                ).model_dump(),
            )
        seen = []

        def wedged(request):
            seen.append("s2")
            return httpx.Response(200, content=b"")

        def works(request):
            seen.append("s1")
            return httpx.Response(200, content=DONE.encode())

        _install_mock_ollama(client, "s2", wedged)
        _install_mock_ollama(client, "s1", works)
        body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}

        # Until s2 has failed THRESHOLD times, the tie between two idle nodes
        # may send requests there first; every one still completes on s1.
        for _ in range(12):
            assert client.post("/api/chat", json=body).status_code == 200
            if app.state.scorer.failures.is_failing("s2", MODEL):
                break
        assert app.state.scorer.failures.is_failing("s2", MODEL)
        assert seen.count("s2") == BackendFailureMemory.THRESHOLD

        seen.clear()
        for _ in range(6):
            assert client.post("/api/chat", json=body).status_code == 200
        assert seen == ["s1"] * 6


@pytest.fixture
async def store(tmp_path):
    from fleet_manager.server.trace_store import TraceStore

    s = TraceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


async def _trace(store, node, status, message=None, ttft=None, ts=None, model=MODEL):
    await store.record_trace(
        request_id=uuid.uuid4().hex,
        model=model,
        original_model=model,
        node_id=node,
        status=status,
        error_message=message,
        time_to_first_token_ms=ttft,
    )
    if ts is not None:
        await store._db.execute(
            "UPDATE request_traces SET timestamp=? "
            "WHERE rowid=(SELECT MAX(rowid) FROM request_traces)",
            (ts,),
        )
        await store._db.commit()


@pytest.mark.asyncio
class TestFailingBackendsQuery:
    async def test_three_failures_with_no_success_after(self, store):
        for _ in range(3):
            await _trace(store, "s2", "retried", WEDGED)
        rows = await store.get_failing_backends()
        assert [(r["node_id"], r["model"], r["failures"]) for r in rows] == [("s2", MODEL, 3)]
        assert rows[0]["last_error"] == WEDGED

    async def test_a_success_after_the_failures_clears_it(self, store):
        now = time.time()
        for i in range(3):
            await _trace(store, "s2", "retried", WEDGED, ts=now - 60 + i)
        await _trace(store, "s2", "completed", ttft=300, ts=now - 10)
        assert await store.get_failing_backends() == []

    async def test_client_errors_and_failures_after_output_do_not_count(self, store):
        for _ in range(3):
            await _trace(store, "s2", "failed", "Client error '400 Bad Request'")
            await _trace(store, "s2", "failed", "stream broke", ttft=200)
        assert await store.get_failing_backends() == []

    async def test_terminal_failures_count_on_a_one_node_fleet(self, store):
        for _ in range(3):
            await _trace(store, "solo", "failed", "Server error '500 Internal Server Error'")
        assert [r["node_id"] for r in await store.get_failing_backends()] == ["solo"]

    async def test_window(self, store):
        for _ in range(3):
            await _trace(store, "s2", "retried", WEDGED, ts=time.time() - 900)
        assert await store.get_failing_backends() == []


def test_health_check_names_the_node_and_the_fix():
    recs = HealthEngine()._check_backend_failing(
        [{"node_id": "s2", "model": MODEL, "failures": 29, "last_ts": time.time(),
          "last_error": WEDGED}]
    )  # fmt: skip
    assert len(recs) == 1
    r = recs[0]
    assert r.check_id == "backend_failing" and r.severity == Severity.WARNING
    assert r.node_id == "s2" and MODEL in r.title
    assert "Restart Ollama on s2" in r.fix
    assert HealthEngine()._check_backend_failing([]) == []
