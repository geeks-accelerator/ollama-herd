"""The benchmark counts what happened, and measures what it says it measures.

From an independent review of the tooling (2026-11-20), each confirmed by
experiment:

- a stream that failed mid-way (HTTP 200, then Ollama's ``{"error": ...}`` line
  or an OpenAI ``data: {"error": ...}`` event, or no ``done`` at all) counted as
  a success;
- requests still in flight when the run ended were cancelled after a fixed 30 /
  35 s and never recorded, so under overload the slowest vanished; and the
  rates' denominator included that drain;
- httpx's default pool (100 connections) queued every request past the 100th
  inside the client, timed as server latency while the open loop's schedule
  lag read ~1 ms;
- a vision-capable chat model (gemma3, llava) got the image workload, so
  ``--prompt-tokens 1250`` sent a picture and one sentence, and the dashboard
  (types ``["llm"]``) dropped it;
- the warm-up loaded an on-disk model on every node that had it, evicting what
  they held before the router chose;
- embeddings counted as tokens and mixed into TTFT percentiles.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time

import httpx
import pytest

from fleet_manager.server import benchmark_engine as engine
from fleet_manager.server.benchmark_engine import (
    CANCELLED_AT_DRAIN,
    ModelTarget,
    NodeInfo,
    RequestResult,
    build_report,
    discover_fleet,
    request_record,
    run_load,
    select_workloads,
    send_request,
    warmup,
)
from tests.test_scripts import load_script

ROUTER = "http://router.test"


def _ndjson(lines) -> str:
    return "".join(json.dumps(x) + "\n" for x in lines)


def _sse(events) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def _content(text="ok") -> dict:
    return {"message": {"role": "assistant", "content": text}, "done": False}


# -- 1. in-band errors and incomplete streams -----------------------------------


class TestStreamFailures:
    @pytest.mark.asyncio
    async def test_an_ollama_error_line_after_some_chunks_is_a_failure(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/api/chat",
            text=_ndjson(
                [_content(), _content(), {"error": "llama runner process has terminated"}]
            ),
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            r = await send_request(c, "m:7b")
        assert not r.success
        assert "llama runner process has terminated" in r.error
        assert r.ttft_ms > 0  # it did start

    @pytest.mark.asyncio
    async def test_an_ollama_stream_without_done_is_a_failure(self, httpx_mock):
        httpx_mock.add_response(url=f"{ROUTER}/api/chat", text=_ndjson([_content(), _content()]))
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            r = await send_request(c, "m:7b")
        assert not r.success and "without done" in r.error

    @pytest.mark.asyncio
    async def test_an_openai_error_event_is_a_failure(self, httpx_mock):
        events = [
            {"choices": [{"delta": {"content": "a"}}]},
            {"error": {"message": "context length exceeded", "type": "server_error"}},
        ]
        httpx_mock.add_response(
            url=f"{ROUTER}/v1/chat/completions", text=_sse(events) + "data: [DONE]\n\n"
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            r = await send_request(c, "m:7b", api="openai")
        assert not r.success and "context length exceeded" in r.error

    @pytest.mark.asyncio
    async def test_an_openai_stream_that_just_stops_is_a_failure(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/v1/chat/completions",
            text=_sse([{"choices": [{"delta": {"content": "a"}}]}]),
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            r = await send_request(c, "m:7b", api="openai")
        assert not r.success and "without" in r.error

    @pytest.mark.asyncio
    async def test_why_the_reply_stopped_is_recorded(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/api/chat",
            text=_ndjson([_content(), {"done": True, "done_reason": "length", "eval_count": 9}]),
        )
        httpx_mock.add_response(
            url=f"{ROUTER}/v1/chat/completions",
            text=_sse(
                [
                    {"choices": [{"delta": {"content": "a"}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                ]
            )
            + "data: [DONE]\n\n",
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            ollama = await send_request(c, "m:7b")
            openai = await send_request(c, "m:7b", api="openai")
        assert ollama.success and ollama.finish_reason == "length"
        assert openai.success and openai.finish_reason == "stop"
        assert request_record(ollama)["finish_reason"] == "length"


class TestErrorText:
    @pytest.mark.asyncio
    async def test_a_timeout_is_named_not_recorded_as_an_empty_string(self, httpx_mock):
        httpx_mock.add_exception(httpx.ReadTimeout(""), url=f"{ROUTER}/api/chat")
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            r = await send_request(c, "m:7b")
        assert not r.success and r.error == "ReadTimeout"


# -- 2. the drain -----------------------------------------------------------------


def _slow_send(seconds: float):
    async def send(client, model, **kw):
        await asyncio.sleep(seconds)
        return RequestResult(model, "a", seconds * 1000, 1, 1, 5, True)

    return send


class TestDrain:
    @pytest.mark.asyncio
    async def test_closed_loop_requests_cut_off_by_the_drain_are_failures(self, monkeypatch):
        monkeypatch.setattr(engine, "send_request", _slow_send(30))
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            load = await run_load(
                c, [ModelTarget("m", 1, 3)], 0.2, asyncio.Event(), results, timeout=0.3
            )
        assert load.elapsed_s < 2  # waited the per-request timeout, not 35 s
        assert len(results) == 3
        assert all(not r.success and r.error == CANCELLED_AT_DRAIN for r in results)
        report = build_report(results, load.window_s, [], [], [])
        assert report["total_failures"] == 3 and report["cancelled_at_drain"] == 3

    @pytest.mark.asyncio
    async def test_open_loop_requests_cut_off_by_the_drain_are_failures(self, monkeypatch):
        monkeypatch.setattr(engine, "send_request", _slow_send(30))
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            load = await run_load(
                c,
                [ModelTarget("m", 1, 1)],
                0.25,
                asyncio.Event(),
                results,
                rate=20,
                timeout=0.3,
                seed=1,
            )
        assert load.elapsed_s < 2
        assert len(results) == 5
        assert all(r.error == CANCELLED_AT_DRAIN for r in results)
        assert all(r.schedule_lag_ms is not None for r in results)

    @pytest.mark.asyncio
    async def test_a_request_finishing_in_the_drain_counts_but_not_in_the_rate(self, monkeypatch):
        monkeypatch.setattr(engine, "send_request", _slow_send(0.4))
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            load = await run_load(
                c, [ModelTarget("m", 1, 1)], 0.5, asyncio.Event(), results, timeout=5
            )
        assert len(results) == 2 and all(r.success for r in results)
        assert load.elapsed_s >= 0.75  # the second finished in the drain ...
        assert 0.45 <= load.window_s <= 0.6  # ... and the rates are over the run
        report = build_report(results, load.window_s, [], [], [])
        # Answered, but past the window: in the counts, in no rate.  Dividing it
        # by the load window is what made throughput rise past saturation.
        assert report["total_requests"] == 2 and report["drained_completed"] == 1
        assert report["requests_per_sec"] == pytest.approx(1 / load.window_s, abs=0.05)
        assert report["rate_basis"] == "window"

    @pytest.mark.asyncio
    async def test_open_loop_rates_are_over_the_send_window(self, monkeypatch):
        monkeypatch.setattr(engine, "send_request", _slow_send(0.4))
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            load = await run_load(
                c,
                [ModelTarget("m", 1, 1)],
                30,
                asyncio.Event(),
                results,
                rate=10,
                max_requests=3,
                timeout=5,
                seed=1,
            )
        assert len(results) == 3 and load.elapsed_s >= 0.55
        assert load.window_s == pytest.approx(0.3, abs=0.05)  # three arrival slots
        # Open loop: every answer to a request sent in the window counts, over
        # the first send to the last answer (about 0.2 s + 0.4 s), not the
        # 0.3 s arrivals window, which would inflate it, nor dropping the tail.
        report = build_report(results, load.window_s, [], [], [])
        assert report["drained_completed"] == 0
        assert report["rate_span_s"] == pytest.approx(0.6, abs=0.1)
        assert report["requests_per_sec"] == pytest.approx(3 / 0.6, rel=0.2)

    @pytest.mark.asyncio
    async def test_the_fleet_is_polled_through_the_drain(self, monkeypatch):
        monkeypatch.setattr(engine, "send_request", _slow_send(0.4))
        stopped_at: list[float] = []

        async def poll(client, stop_event, snapshots, origin=None):
            await stop_event.wait()
            stopped_at.append(time.monotonic())

        monkeypatch.setattr(engine, "poll_fleet_status", poll)
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            t0 = time.monotonic()
            await run_load(
                c, [ModelTarget("m", 1, 1)], 0.1, asyncio.Event(), results, fleet_client=c
            )
        assert stopped_at and stopped_at[0] - t0 >= 0.38  # after the last request, not at 0.1 s


# -- 3. the connection pool ------------------------------------------------------


async def _slow_http_server(delay: float):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(delay)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0, backlog=1024)


async def _concurrent_gets(client: httpx.AsyncClient, n: int) -> float:
    t0 = time.monotonic()
    responses = await asyncio.gather(*(client.get("/") for _ in range(n)))
    assert all(r.status_code == 200 for r in responses)
    return time.monotonic() - t0


class TestConnectionPool:
    @pytest.mark.asyncio
    async def test_more_than_100_requests_in_flight_are_not_queued_in_the_client(self):
        server = await _slow_http_server(0.4)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            # The control: httpx's default pool runs the 101st after the first 100.
            async with httpx.AsyncClient(base_url=url, timeout=10) as default:
                assert await _concurrent_gets(default, 120) >= 0.75
            async with engine.bench_client(url, timeout=10) as client:
                assert await _concurrent_gets(client, 120) < 0.75
        finally:
            server.close()
            await server.wait_closed()

    def test_the_cli_uses_the_engines_client(self):
        cli = load_script("benchmark")
        assert cli.bench_client is engine.bench_client


# -- 4. a vision-capable chat model is a chat model ---------------------------------


def _node(node_id, loaded=(), on_disk=()):
    return {
        "node_id": node_id,
        "status": "online",
        "ollama_url": f"http://{node_id}.test:11434",
        "memory": {"total_gb": 64, "used_gb": 8, "available_gb": 56},
        "cpu": {"cores_physical": 16},
        "model_memory_available_gb": 56,
        "ollama": {
            "models_loaded": [{"name": n, "size_gb": 5.0} for n in loaded],
            "models_available": list(loaded) + list(on_disk),
            "models_available_sizes": {n: 9.0 for n in on_disk},
        },
    }


def _fleet(httpx_mock, nodes, tags=()):
    httpx_mock.add_response(url=f"{ROUTER}/fleet/status", json={"nodes": nodes}, is_reusable=True)
    httpx_mock.add_response(
        url=f"{ROUTER}/api/tags",
        json={"models": [{"name": n, "capabilities": caps} for n, caps in tags]},
        is_reusable=True,
    )


class TestWorkloads:
    def _targets(self):
        return [
            ModelTarget("gemma3:27b", 1, 1, model_type="vision"),
            ModelTarget("qwen3:14b", 1, 1),
            ModelTarget("nomic-embed-text", 1, 1, model_type="embed"),
        ]

    def test_a_vision_capable_model_runs_the_text_workload_by_default(self):
        assert ModelTarget("gemma3:27b", 1, 1, model_type="vision").workload == "llm"
        picked = select_workloads(self._targets(), ["llm"])
        assert [(t.name, t.model_type, t.workload) for t in picked] == [
            ("gemma3:27b", "vision", "llm"),
            ("qwen3:14b", "llm", "llm"),
        ]

    def test_the_image_workload_only_when_vision_is_asked_for(self):
        picked = select_workloads(self._targets(), ["llm", "vision", "embed"])
        assert [(t.name, t.workload) for t in picked] == [
            ("gemma3:27b", "vision"),
            ("qwen3:14b", "llm"),
            ("nomic-embed-text", "embed"),
        ]
        only = select_workloads(self._targets(), ["vision"])
        assert [(t.name, t.workload) for t in only] == [("gemma3:27b", "vision")]

    @pytest.mark.asyncio
    async def test_discovery_reads_ollamas_reported_capabilities(self, httpx_mock):
        """The router's /api/tags carries them; /fleet/status does not."""
        _fleet(
            httpx_mock,
            [_node("a", loaded=["qwen3.8:4b", "m:7b"])],
            tags=[("qwen3.8:4b", ["completion", "vision"]), ("m:7b", ["completion"])],
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, _ = await discover_fleet(c)
        assert {t.name: t.model_type for t in targets} == {"qwen3.8:4b": "vision", "m:7b": "llm"}

    @pytest.mark.asyncio
    async def test_discovery_without_tags_falls_back_to_names(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status", json={"nodes": [_node("a", ["llava:7b"])]}
        )
        httpx_mock.add_response(url=f"{ROUTER}/api/tags", status_code=404)
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            [t], _ = await discover_fleet(c)
        assert (t.model_type, t.workload) == ("vision", "llm")

    @pytest.mark.asyncio
    async def test_cli_sends_the_long_text_prompt_to_a_vision_capable_model(self, httpx_mock):
        bodies = self._run_cli(httpx_mock, [])
        [body] = await bodies
        [msg] = body["messages"]
        assert "images" not in msg and len(msg["content"]) > 3000

    @pytest.mark.asyncio
    async def test_cli_sends_images_when_vision_is_asked_for(self, httpx_mock):
        [body] = await self._run_cli(httpx_mock, ["--types", "vision"])
        [msg] = body["messages"]
        assert msg["images"] == [engine.TEST_IMAGE_B64]

    async def _run_cli(self, httpx_mock, extra):
        cli = load_script("benchmark")
        bodies = []

        def chat(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text=_ndjson([_content(), {"done": True, "eval_count": 1}]))

        _fleet(httpx_mock, [_node("a", ["gemma3:27b"])])
        httpx_mock.add_callback(chat, url=f"{ROUTER}/api/chat", is_reusable=True)
        args = cli.parse_args(
            ["--url", ROUTER, "--models", "gemma3:27b", "--prompt-tokens", "1250"]
            + ["--requests", "1", "--concurrency", "1", "--skip-warmup", "--no-save"]
            + extra
        )
        await cli.run_benchmark(args)
        return bodies

    def test_cli_types_are_validated(self):
        cli = load_script("benchmark")
        assert cli.parse_args([]).types == list(engine.DEFAULT_TYPES)
        assert cli.parse_args(["--types", "llm,vision"]).types == ["llm", "vision"]
        with pytest.raises(SystemExit):
            cli.parse_args(["--types", "llm,video"])

    @pytest.mark.asyncio
    async def test_dashboard_runner_keeps_vision_capable_models_as_chat(self, monkeypatch):
        from fleet_manager.server import benchmark_runner

        async def fake_discover(client, include_on_disk=()):
            return [
                ModelTarget("gemma3:27b", 1, 1, nodes=["a"], model_type="vision"),
                ModelTarget("qwen3:14b", 1, 1, nodes=["a"]),
            ], []

        async def fake_warmup(targets, nodes_info, log_fn=None):
            return targets

        ran: list = []

        async def fake_run_load(client, targets, duration, stop_event, results, **kw):
            ran.append([(t.name, t.workload) for t in targets])
            return engine.LoadResult([], duration, "s", "e", seed=1, window_s=duration)

        monkeypatch.setattr(benchmark_runner, "discover_fleet", fake_discover)
        monkeypatch.setattr(benchmark_runner, "warmup", fake_warmup)
        monkeypatch.setattr(benchmark_runner, "run_load", fake_run_load)
        runner = benchmark_runner.BenchmarkRunner(base_url=ROUTER)
        await runner._run("default", 0.1, ["llm"], None, None, None, None)
        await runner._run("default", 0.1, ["llm", "vision"], None, None, None, None)
        assert ran == [
            [("gemma3:27b", "llm"), ("qwen3:14b", "llm")],
            [("gemma3:27b", "vision"), ("qwen3:14b", "llm")],
        ]

    def test_dashboard_offers_vision_and_vision_embeddings(self):
        from fleet_manager.server.routes.dashboard import _BENCHMARKS_BODY

        for kind in ("vision", "embed-image"):
            assert f'id="type-{kind}"' in _BENCHMARKS_BODY
            assert f"types.push('{kind}')" in _BENCHMARKS_BODY


# -- 5. warm-up of a model that is only on disk -------------------------------------


class TestOnDiskWarmup:
    @pytest.mark.asyncio
    async def test_an_on_disk_model_is_not_loaded_on_every_node(self, httpx_mock):
        _fleet(
            httpx_mock,
            [_node("a", ["x:7b"], on_disk=["y:7b"]), _node("b", ["z:7b"], on_disk=["y:7b"])],
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, nodes = await discover_fleet(c, include_on_disk=["y:7b"])
        [y] = [t for t in targets if t.name == "y:7b"]
        assert y.nodes == ["a", "b"] and y.on_disk_only
        kept = await warmup([y], nodes)
        assert kept == [y]  # kept, and nothing was sent: its first request includes the load
        assert [r.url.path for r in httpx_mock.get_requests()] == ["/fleet/status", "/api/tags"]


# -- low: prompts, per-type aggregates, the runner's event loop ------------------------


def test_make_prompt_encodes_the_padding_once(monkeypatch):
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    calls = []
    real = enc.encode

    def counting(text, *a, **kw):
        calls.append(len(text))
        return real(text, *a, **kw)

    monkeypatch.setattr(enc, "encode", counting)
    engine.make_prompt(4000, random.Random(1))
    assert len(calls) <= 3  # the tail, the padding: not once per 40 words


def test_token_rates_and_ttft_come_from_chat_requests_only():
    chat = RequestResult("m", "a", 1000, 200, 50, 100, True, model_type="llm")
    embeds = [RequestResult("e", "a", 20, 20, 0, 1, True, model_type="embed") for _ in range(50)]
    report = build_report([chat, *embeds], 10.0, [], [], [])
    assert report["total_completion_tokens"] == 100
    assert report["tokens_per_sec"] == 10.0
    assert report["ttft_p50_ms"] == 200.0
    assert report["requests_per_sec"] == 5.1
    by_type = {t["model_type"]: t for t in report["per_type_results"]}
    assert by_type["embed"]["requests"] == 50 and by_type["embed"]["latency_p50_ms"] == 20.0
    assert by_type["llm"]["tok_s"] == 10.0
    [node] = report["per_node_results"]
    assert node["tokens"] == 100


@pytest.mark.asyncio
async def test_dashboard_runner_builds_the_report_off_the_event_loop(monkeypatch):
    """build_report runs git (the manifest's commit): never inside the router's loop."""
    from fleet_manager.server import benchmark_runner

    async def fake_discover(client, include_on_disk=()):
        return [ModelTarget("m", 1, 1, nodes=["a"])], [NodeInfo("a", 1, 1, 0, 1)]

    async def fake_warmup(targets, nodes_info, log_fn=None):
        return targets

    async def fake_run_load(client, targets, duration, stop_event, results, **kw):
        return engine.LoadResult([], duration, "s", "e", seed=1, window_s=duration)

    threads = []
    real_build = benchmark_runner.build_report

    def build(*a, **kw):
        threads.append(threading.current_thread())
        return real_build(*a, **kw)

    monkeypatch.setattr(benchmark_runner, "discover_fleet", fake_discover)
    monkeypatch.setattr(benchmark_runner, "warmup", fake_warmup)
    monkeypatch.setattr(benchmark_runner, "run_load", fake_run_load)
    monkeypatch.setattr(benchmark_runner, "build_report", build)
    runner = benchmark_runner.BenchmarkRunner(base_url=ROUTER)
    await runner._run("default", 0.1, ["llm"], None, None, None, None)
    assert runner._status == "complete", runner._error
    assert threads and threads[0] is not threading.main_thread()
