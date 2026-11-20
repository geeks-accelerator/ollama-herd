"""The benchmark harness measures the model it names, on every node, and keeps
what the run was.

From the 2026-11-20 fleet benchmark:
- with defaults the router answered requests for one model with another
  (vram_fallback), and the harness counted them as the requested model;
- the warm-up went through the router, so only one node got warmed, and the
  first request on each other node took ~39 s;
- ``--models`` dropped any model not already loaded, so a mixed workload on
  one machine (two models that don't fit together) could not run;
- a saved run lost its workload and mode;
- the dashboard runner raised NameError on a run without chat models.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fleet_manager.server import benchmark_engine as engine
from fleet_manager.server.benchmark_engine import (
    ModelTarget,
    NodeInfo,
    RequestResult,
    build_report,
    discover_fleet,
    send_request,
    warmup,
)

ROUTER = "http://router.test"


def _status(nodes):
    return {"nodes": nodes}


def _node(node_id, loaded=(), on_disk=(), url=None):
    return {
        "node_id": node_id,
        "status": "online",
        "ollama_url": url or f"http://{node_id}.test:11434",
        "memory": {"total_gb": 64, "used_gb": 8, "available_gb": 56},
        "cpu": {"cores_physical": 16},
        "model_memory_available_gb": 56,
        "ollama": {
            "models_loaded": [{"name": n, "size_gb": 5.0, "context_length": 4096} for n in loaded],
            "models_available": list(loaded) + list(on_disk),
            "models_available_sizes": {n: 9.0 for n in on_disk},
        },
    }


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_labels_embedding_models(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status",
            json=_status([_node("a", loaded=["qwen3-coder:30b", "nomic-embed-text:latest"])]),
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, nodes = await discover_fleet(c)
        types = {t.name: t.model_type for t in targets}
        assert types == {"qwen3-coder:30b": "llm", "nomic-embed-text:latest": "embed"}
        assert nodes[0].ollama_url == "http://a.test:11434"
        assert nodes[0].model_contexts == {"qwen3-coder:30b": 4096, "nomic-embed-text:latest": 4096}

    @pytest.mark.asyncio
    async def test_includes_a_requested_model_that_is_only_on_disk(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status",
            json=_status([_node("a", loaded=["qwen3-coder:30b"], on_disk=["qwen3.5:9b"])]),
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, _ = await discover_fleet(c, include_on_disk=["qwen3.5:9b"])
        by_name = {t.name: t for t in targets}
        assert by_name["qwen3.5:9b"].nodes == ["a"]
        assert by_name["qwen3.5:9b"].size_gb == 9.0

    @pytest.mark.asyncio
    async def test_a_model_loaded_somewhere_is_not_also_listed_where_it_is_on_disk(
        self, httpx_mock
    ):
        """Listing it there made the warm-up load it there and evict what the
        node held, before the router decided anything."""
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status",
            json=_status(
                [
                    _node("a", loaded=["qwen3-coder:30b"], on_disk=["qwen3.5:9b"]),
                    _node("b", loaded=["qwen3.5:9b"], on_disk=["qwen3-coder:30b"]),
                ]
            ),
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, _ = await discover_fleet(c, include_on_disk=["qwen3-coder:30b", "qwen3.5:9b"])
        assert {t.name: t.nodes for t in targets} == {
            "qwen3-coder:30b": ["a"],
            "qwen3.5:9b": ["b"],
        }


def _ndjson(lines):
    return "".join(json.dumps(x) + "\n" for x in lines)


class TestExactModel:
    @pytest.mark.asyncio
    async def test_asks_for_no_substitution_and_fails_another_models_answer(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/api/chat",
            text=_ndjson([{"done": True, "eval_count": 3}]),
            headers={"x-fleet-node": "a", "x-fleet-served-model": "phi4:14b-q8_0"},
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            result = await send_request(c, "qwen2.5:14b-instruct-q8_0")
        request = httpx_mock.get_requests()[0]
        assert request.headers["x-fleet-no-fallback"] == "true"
        assert not result.success
        assert result.served_model == "phi4:14b-q8_0"
        assert "served by phi4:14b-q8_0" in result.error

    @pytest.mark.asyncio
    async def test_the_latest_tag_is_the_same_model(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/api/chat",
            text=_ndjson([{"done": True, "eval_count": 3}]),
            headers={"x-fleet-served-model": "nomic-thing:latest"},
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            assert (await send_request(c, "nomic-thing")).success

    @pytest.mark.asyncio
    async def test_think_false_is_sent_when_asked(self, httpx_mock):
        httpx_mock.add_response(url=f"{ROUTER}/api/chat", text=_ndjson([{"done": True}]))
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            await send_request(c, "qwen3.5:9b", think=False)
        assert json.loads(httpx_mock.get_requests()[0].content)["think"] is False


class TestWarmup:
    @pytest.mark.asyncio
    async def test_warms_every_node_directly_with_the_resident_context(self, httpx_mock):
        for host in ("a", "b"):
            httpx_mock.add_response(url=f"http://{host}.test:11434/api/chat", json={"done": True})
        httpx_mock.add_response(url="http://a.test:11434/api/embed", json={"embeddings": [[1.0]]})
        nodes = [
            NodeInfo(
                "a", 16, 64, 8, 56, ollama_url="http://a.test:11434", model_contexts={"m": 8192}
            ),
            NodeInfo("b", 16, 64, 8, 56, ollama_url="http://b.test:11434"),
        ]
        targets = [
            ModelTarget("m", 5.0, 2, nodes=["a", "b"]),
            ModelTarget("nomic-embed-text:latest", 0.3, 2, nodes=["a"], model_type="embed"),
        ]
        kept = await warmup(targets, nodes)
        assert [t.name for t in kept] == ["m", "nomic-embed-text:latest"]
        sent = {str(r.url): json.loads(r.content) for r in httpx_mock.get_requests()}
        a = sent["http://a.test:11434/api/chat"]
        assert a["keep_alive"] == -1 and a["options"]["num_ctx"] == 8192
        assert "num_ctx" not in sent["http://b.test:11434/api/chat"]["options"]

    @pytest.mark.asyncio
    async def test_a_model_no_node_could_warm_is_dropped(self, httpx_mock):
        httpx_mock.add_response(url="http://a.test:11434/api/chat", status_code=500)
        nodes = [NodeInfo("a", 16, 64, 8, 56, ollama_url="http://a.test:11434")]
        assert await warmup([ModelTarget("m", 5.0, 1, nodes=["a"])], nodes) == []


class TestReport:
    def _results(self):
        ok = dict(latency_ms=10, ttft_ms=5, prompt_tokens=10, completion_tokens=2, success=True)
        return [
            RequestResult(model="m", node_id="a", **ok),
            RequestResult(model="m", node_id="b", **ok),
            RequestResult(model="m", node_id="b", **ok),
            RequestResult(
                model="m",
                node_id="a",
                latency_ms=1,
                ttft_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                success=False,
                error="served by other, not m",
                served_model="other",
            ),
        ]

    def test_placement_mismatches_and_run_are_kept(self):
        report = build_report(
            self._results(),
            10.0,
            [ModelTarget("m", 5.0, 2)],
            [],
            [],
            run={"workload": {"prompt_tokens": 1250, "max_tokens": 200}},
        )
        assert report["per_model_results"][0]["nodes"] == {"a": 1, "b": 2}
        assert report["served_model_mismatches"] == 1
        assert report["manifest"]["workload"]["prompt_tokens"] == 1250

    @pytest.mark.asyncio
    async def test_a_saved_run_keeps_what_it_was(self, tmp_path):
        from fleet_manager.server.trace_store import TraceStore

        store = TraceStore(data_dir=str(tmp_path))
        await store.initialize()
        report = build_report(self._results(), 10.0, [], [], [], run={"mode": "smart"})
        await store.save_benchmark_run(engine.dashboard_record(report))
        [saved] = await store.get_benchmark_runs(limit=1)
        assert saved["fleet_snapshot"]["manifest"]["mode"] == "smart"
        assert "requests" not in saved
        await store.close()


class TestRunLoad:
    @pytest.mark.asyncio
    async def test_stops_at_the_duration_and_drains(self, monkeypatch):
        calls = []

        async def fake_send(client, model, **kw):
            calls.append(model)
            await asyncio.sleep(0.01)
            return RequestResult(model, "a", 10, 5, 1, 1, True)

        async def no_poll(client, stop_event, snapshots):
            await stop_event.wait()

        monkeypatch.setattr(engine, "send_request", fake_send)
        monkeypatch.setattr(engine, "poll_fleet_status", no_poll)
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            load = await engine.run_load(
                c, [ModelTarget("m", 1.0, 2)], 0.2, asyncio.Event(), results
            )
        assert results and all(r.success for r in results)
        assert 0.15 < load.elapsed_s < 5


@pytest.mark.asyncio
async def test_dashboard_run_without_chat_models_does_not_crash(monkeypatch):
    """It passed `targets` to build_report, defined only for chat models."""
    from fleet_manager.server import benchmark_runner

    async def fake_discover(client, include_on_disk=()):
        return [ModelTarget("nomic-embed-text:latest", 0.3, 1, nodes=["a"], model_type="embed")], []

    async def fake_warmup(targets, nodes_info, log_fn=None):
        return targets

    async def fake_run_load(client, targets, duration, stop_event, results, **kw):
        results.append(
            RequestResult("nomic-embed-text:latest", "a", 5, 5, 0, 1, True, model_type="embed")
        )
        return engine.LoadResult([], duration, "start", "end", seed=3)

    monkeypatch.setattr(benchmark_runner, "discover_fleet", fake_discover)
    monkeypatch.setattr(benchmark_runner, "warmup", fake_warmup)
    monkeypatch.setattr(benchmark_runner, "run_load", fake_run_load)
    runner = benchmark_runner.BenchmarkRunner(base_url=ROUTER)
    await runner._run("default", 0.1, ["embed"], None, None, None, None)
    assert runner._status == "complete", runner._error
    assert runner._report["manifest"]["mode"] == "default"
    assert runner._report["manifest"]["seed"] == 3


class TestSessions:
    """Multi-turn sessions over one shared system prompt, what coding agents
    send; the workload where a router's affinity can matter."""

    @pytest.mark.asyncio
    async def test_each_turn_resends_the_history_and_is_tagged(self, monkeypatch):
        sent = []
        stop = asyncio.Event()

        async def fake_send(client, model, **kw):
            sent.append(kw)
            if len(sent) == 3:
                stop.set()
            return RequestResult(
                model, kw["node_label"] or "a", 10, 5, 1, 1, True, reply=f"r{len(sent)}"
            )

        monkeypatch.setattr(engine, "send_request", fake_send)
        results: list = []
        await engine.session_worker(
            lambda: (None, ""), "m", results, stop, system_prompt="SYS", turns=3, user_tokens=20
        )
        assert [len(k["messages"]) for k in sent] == [2, 4, 6]
        assert sent[2]["messages"][0] == {"role": "system", "content": "SYS"}
        assert sent[2]["messages"][2] == {"role": "assistant", "content": "r1"}
        assert len({k["tags"][0] for k in sent}) == 1 and sent[0]["tags"][0].startswith("session:")
        assert [r.turn for r in results] == [0, 1, 2]

    def test_shared_system_prompt_is_the_same_every_time(self):
        assert engine.session_system_prompt(500) == engine.session_system_prompt(500)

    def test_report_has_ttft_by_turn(self):
        rs = [
            RequestResult("m", "a", 10, 900, 1, 1, True, turn=0),
            RequestResult("m", "a", 10, 300, 1, 1, True, turn=1),
        ]
        report = build_report(rs, 10.0, [ModelTarget("m", 1.0, 1)], [], [])
        assert report["per_model_results"][0]["ttft_p50_by_turn"] == {"0": 900.0, "1": 300.0}
