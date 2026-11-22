"""The benchmark keeps what a published result needs.

Before: per-request data was reduced to aggregates and dropped, so the
published JSONs were assembled from dashboard reports by hand; versions and
settings were hand-typed strings; model loads and evictions went unrecorded;
the load was fixed concurrency over unseeded ``random``; the open-loop
schedule was copied into two scripts; and the engine could not send vision
work.  These pin the per-request records, the manifest, model events, the
workload shapes (open loop, seeded and weighted sequences, alternation) and the
vision request shapes.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import random
import struct
import time
from pathlib import Path

import httpx
import pytest

import fleet_manager
from fleet_manager.common.stats import percentile
from fleet_manager.server import benchmark_engine as engine
from fleet_manager.server.benchmark_engine import (
    TEST_IMAGE_B64,
    ModelSequence,
    ModelTarget,
    NodeInfo,
    RequestResult,
    build_manifest,
    build_report,
    dashboard_record,
    discover_fleet,
    model_events,
    open_loop,
    parse_models,
    run_load,
    send_one,
    send_request,
    warmup,
)

ROUTER = "http://router.test"
REPO = Path(__file__).resolve().parents[2]

REQUEST_KEYS = {
    "started_ms",
    "model",
    "model_type",
    "node_id",
    "served_model",
    "request_id",
    "success",
    "error",
    "ttft_ms",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "turn",
}


def _load_script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ok(model="m", node="a", started=0.0, latency=10.0, ttft=5.0, **kw) -> RequestResult:
    return RequestResult(model, node, latency, ttft, 10, 2, True, started_ms=started, **kw)


def _ndjson_stream(content="hi", prompt=10, completion=2) -> str:
    lines = [
        {"message": {"content": content}, "done": False},
        {"done": True, "prompt_eval_count": prompt, "eval_count": completion},
    ]
    return "\n".join(json.dumps(x) for x in lines)


def _status(nodes) -> dict:
    return {"nodes": nodes}


def _node(node_id, loaded=(), vision_embed=(), **ollama_extra) -> dict:
    return {
        "node_id": node_id,
        "status": "online",
        "ollama_url": f"http://{node_id}.test:11434",
        "hardware": {"chip": "NVIDIA Quadro RTX 6000"},
        "memory": {"total_gb": 64, "used_gb": 8, "available_gb": 56},
        "cpu": {"cores_physical": 16},
        "model_memory_available_gb": 56,
        "ollama": {
            "models_loaded": [
                {
                    "name": n,
                    "size_gb": 5.0,
                    "context_length": 4096,
                    "digest": f"sha-{n}",
                    "quantization": "Q4_K_M",
                    "parameter_size": "7B",
                }
                for n in loaded
            ],
            "models_available": list(loaded),
            **ollama_extra,
        },
        **(
            {"vision_embedding": {"models_available": [{"name": v} for v in vision_embed]}}
            if vision_embed
            else {}
        ),
    }


# -- P1: per-request records, p90, --output ---------------------------------


class TestRecords:
    def test_every_request_is_kept_in_start_order(self):
        results = [
            _ok(started=30.0, request_id="r3"),
            _ok(started=10.0, request_id="r1", served_model="m"),
            RequestResult("m", "b", 4, 0, 0, 0, False, error="HTTP 500", started_ms=20.0),
        ]
        report = build_report(results, 1.0, [ModelTarget("m", 1.0, 1)], [], [])
        assert [r["started_ms"] for r in report["requests"]] == [10.0, 20.0, 30.0]
        assert set(report["requests"][0]) == REQUEST_KEYS
        assert report["requests"][0]["request_id"] == "r1"
        assert (
            report["requests"][1] | {"success": False, "error": "HTTP 500"}
            == (report["requests"][1])
        )

    def test_p90_is_reported_with_the_canonical_percentile(self):
        results = [_ok(latency=float(v), ttft=float(v) / 2) for v in range(1, 11)]
        report = build_report(results, 1.0, [], [], [])
        lat = [float(v) for v in range(1, 11)]
        for p in (50, 90, 95, 99):
            assert report[f"latency_p{p}_ms"] == round(percentile(lat, p), 1)
        assert report["latency_p90_ms"] == 9.1
        assert report["ttft_p90_ms"] == round(percentile([v / 2 for v in lat], 90), 1)

    def test_no_samples_report_none_not_zero(self):
        report = build_report([], 1.0, [], [], [])
        assert report["latency_p90_ms"] is None and report["ttft_p50_ms"] is None
        assert report["requests"] == [] and report["model_events"] == []

    def test_first_request_and_after_per_model(self):
        """The alternation result: a cold first request, then each swap."""
        results = [
            _ok("a", started=0.0, ttft=36000.0),
            _ok("a", started=200.0, ttft=60.0),
            _ok("a", started=100.0, ttft=70.0),
        ]
        [m] = build_report(results, 1.0, [], [], [])["per_model_results"]
        assert m["first_ttft_ms"] == 36000.0
        assert m["ttft_p50_ms_after_first"] == 65.0
        assert m["ttft_max_ms_after_first"] == 70.0

    def test_a_failure_from_the_right_model_is_not_a_mismatch(self):
        results = [
            RequestResult("m", "a", 1, 0, 0, 0, False, error="ReadTimeout", served_model="m"),
            RequestResult("m", "a", 1, 0, 0, 0, False, error="x", served_model="m:latest"),
            RequestResult("m", "a", 1, 0, 0, 0, False, error="served by n", served_model="n"),
        ]
        assert build_report(results, 1.0, [], [], [])["served_model_mismatches"] == 1

    def test_dashboard_copy_omits_requests_and_keeps_the_manifest(self):
        report = build_report([_ok()], 1.0, [], [], [], run={"seed": 4})
        record = dashboard_record(report)
        assert "requests" not in record and "requests" in report
        assert record["fleet_snapshot"]["manifest"]["seed"] == 4
        assert record["fleet_snapshot"]["model_events"] == []
        assert record["latency_p50_ms"] == report["latency_p50_ms"]
        # No column of their own, so kept in the snapshot rather than dropped.
        assert record["fleet_snapshot"]["cancelled_at_drain"] == 0
        assert record["fleet_snapshot"]["per_type_results"][0]["model_type"] == "llm"

    @pytest.mark.asyncio
    async def test_request_id_and_start_time_come_from_the_response_and_run_clock(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/api/chat",
            text=_ndjson_stream(),
            headers={"X-Request-Id": "req-123", "X-Fleet-Node": "a"},
        )
        httpx_mock.add_response(url=f"{ROUTER}/api/chat", text=_ndjson_stream())
        origin = time.monotonic() - 1.5
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            first = await send_one(c, ModelTarget("m", 1, 1), rng=random.Random(1), origin=origin)
            second = await send_one(c, ModelTarget("m", 1, 1), rng=random.Random(1), origin=origin)
        assert first.request_id == "req-123" and first.node_id == "a"
        assert second.request_id == ""  # a backend that isn't herd sends none
        assert 1500 <= first.started_ms <= second.started_ms < 5000


# -- P2: manifest --------------------------------------------------------------


class TestManifest:
    @pytest.mark.asyncio
    async def test_versions_and_model_identity_from_fleet_status(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status",
            json=_status([_node("a", loaded=["m:7b"], version="0.40.16", num_parallel=4)]),
        )
        httpx_mock.add_response(url=f"{ROUTER}/api/tags", json={"models": []})
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            _, nodes = await discover_fleet(c)
        manifest = build_manifest(
            nodes, {"seed": 7, "repetition": 2, "workload": {"rate": 5}}, "t0", "t1"
        )
        assert manifest["herd_version"] == fleet_manager.__version__
        assert {"git_commit", "git_dirty"} <= set(manifest)
        assert (manifest["started_at"], manifest["ended_at"]) == ("t0", "t1")
        assert (manifest["seed"], manifest["repetition"]) == (7, 2)
        assert manifest["workload"] == {"rate": 5}
        [node] = manifest["nodes"]
        assert node["ollama_version"] == "0.40.16" and node["num_parallel"] == 4
        assert node["chip"] == "NVIDIA Quadro RTX 6000"
        assert node["agent_version"] is None  # /fleet/status doesn't carry it
        assert node["models"]["m:7b"] == {
            "digest": "sha-m:7b",
            "quantization": "Q4_K_M",
            "parameter_size": "7B",
            "size_gb": 5.0,
            "context_length": 4096,
        }

    def test_unreported_values_are_none(self):
        ni = NodeInfo("a", 1, 1, 0, 1)
        [node] = build_manifest([ni])["nodes"]
        assert node["ollama_version"] is None and node["num_parallel"] is None

    def test_commit_only_from_the_checkout_that_is_running(self, monkeypatch, tmp_path):
        """An installed package must not report whatever repository surrounds it."""
        commit, _ = engine._git_revision()
        if (REPO / ".git").exists():
            assert commit and all(c in "0123456789abcdef" for c in commit)
        fake = tmp_path / "fleet_manager" / "__init__.py"
        fake.parent.mkdir()
        fake.write_text("")
        monkeypatch.setattr(fleet_manager, "__file__", str(fake))
        assert engine._git_revision() == (None, None)


# -- P4: model load/evict events ----------------------------------------------


def test_model_events_diff_each_nodes_loaded_models():
    snaps = [
        {"t_ms": 0.0, "nodes": [_node("a", loaded=["x"]), _node("b", loaded=["y"])]},
        {"t_ms": 3000.0, "nodes": [_node("a", loaded=["z"]), _node("b", loaded=["y"])]},
        {"t_ms": 6000.0, "nodes": [_node("b", loaded=[])]},  # a missing: no events
        {"t_ms": 9000.0, "nodes": [_node("a", loaded=["z"]), _node("c", loaded=["x"])]},
    ]
    assert model_events(snaps) == [
        {"t_ms": 3000.0, "node_id": "a", "model": "z", "event": "loaded"},
        {"t_ms": 3000.0, "node_id": "a", "model": "x", "event": "unloaded"},
        {"t_ms": 6000.0, "node_id": "b", "model": "y", "event": "unloaded"},
    ]


@pytest.mark.asyncio
async def test_polls_are_stamped_on_the_run_clock(httpx_mock):
    httpx_mock.add_response(url=f"{ROUTER}/fleet/status", json=_status([]))
    stop = asyncio.Event()
    snaps: list = []
    async with httpx.AsyncClient(base_url=ROUTER) as c:
        task = asyncio.create_task(
            engine.poll_fleet_status(c, stop, snaps, origin=time.monotonic() - 2)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await task
    assert len(snaps) == 1 and 2000 <= snaps[0]["t_ms"] < 3000


# -- P5: open loop, sequences, seeds, alternation ----------------------------


class TestOpenLoop:
    @pytest.mark.asyncio
    async def test_starts_follow_the_rate_not_the_completions(self):
        starts: list[float] = []
        t0 = time.perf_counter()

        async def fire(i, lag):
            starts.append(time.perf_counter() - t0)
            await asyncio.sleep(0.2)  # slower than the arrival interval

        lags = await open_loop(fire, 50, 10)
        assert len(starts) == len(lags) == 10
        for i, s in enumerate(starts):
            assert abs(s - i / 50) < 0.03, (i, s)
        assert time.perf_counter() - t0 < 0.6  # not 10 x 0.2 s: nothing waited
        assert all(lag >= 0 for lag in lags)

    @pytest.mark.asyncio
    async def test_stop_event_ends_an_unbounded_schedule(self):
        stop = asyncio.Event()
        fired = []

        async def fire(i, lag):
            fired.append(i)
            if i == 4:
                stop.set()

        await open_loop(fire, 200, None, stop_event=stop)
        assert 5 <= len(fired) <= 6

    @pytest.mark.asyncio
    async def test_run_load_rate_mode_records_lag_and_start_times(self, monkeypatch):
        async def fake_send(client, model, **kw):
            await asyncio.sleep(0.05)
            return RequestResult(model, "a", 50, 10, 1, 1, True)

        monkeypatch.setattr(engine, "send_request", fake_send)
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            load = await run_load(
                c,
                [ModelTarget("a", 1, 1), ModelTarget("b", 1, 1)],
                10.0,
                asyncio.Event(),
                results,
                rate=40,
                max_requests=8,
                seed=1,
            )
        starts = sorted(r.started_ms for r in results)
        assert len(results) == 8 and load.elapsed_s < 2
        assert all(r.schedule_lag_ms is not None for r in results)
        assert abs(starts[-1] - 7 * 25) < 30  # 25 ms apart at 40/s
        assert sorted(r.model for r in results) == ["a"] * 4 + ["b"] * 4
        report = build_report(results, load.elapsed_s, [], [], load.snapshots)
        assert set(report["schedule_lag_ms"]) == {"p50", "p99", "max"}
        assert all("schedule_lag_ms" in r for r in report["requests"])

    def test_router_overhead_uses_the_engine_schedule_and_percentile(self):
        module = _load_script(REPO / "scripts" / "router_overhead.py")
        assert module.open_loop is open_loop and module.percentile is percentile
        s = module.summary([1.0, 2.0, 3.0, 4.0])
        assert s["median_ms"] == 2.5 and s["p95_ms"] == round(percentile([1, 2, 3, 4], 95), 1)
        loadgen = _load_script(REPO / "docs" / "experiments" / "router-ceiling-loadgen.py")
        assert loadgen.open_loop is open_loop and loadgen.percentile is percentile


class TestSequence:
    def _targets(self):
        return [ModelTarget("a", 1, 1), ModelTarget("b", 1, 1)]

    def test_alternate_repeats_by_weight(self):
        seq = ModelSequence(self._targets(), "alternate", {"a": 2})
        assert [seq.next().name for _ in range(6)] == ["a", "a", "b", "a", "a", "b"]

    def test_random_is_reproducible_with_a_seed(self):
        def draw(seed):
            seq = ModelSequence(self._targets(), "random", rng=random.Random(seed))
            return [seq.next().name for _ in range(50)]

        assert draw(5) == draw(5)
        assert draw(5) != draw(6)

    def test_random_follows_the_weights(self):
        seq = ModelSequence(self._targets(), "random", {"a": 3, "b": 1}, random.Random(0))
        share = sum(seq.next().name == "a" for _ in range(4000)) / 4000
        assert 0.72 < share < 0.78

    @pytest.mark.parametrize(
        ("mode", "weights"),
        [("alternate", {"a": 1.5}), ("random", {"a": 0}), ("random", {"zz": 1}), ("x", {})],
    )
    def test_bad_sequences_are_refused(self, mode, weights):
        with pytest.raises(ValueError):
            ModelSequence(self._targets(), mode, weights)

    @pytest.mark.parametrize(
        ("values", "names", "weights"),
        [
            (["a:7b", "b"], ["a:7b", "b"], {}),
            (["a:7b=3,b=1"], ["a:7b", "b"], {"a:7b": 3.0, "b": 1.0}),
            (["a=3", "b"], ["a", "b"], {"a": 3.0}),
        ],
    )
    def test_models_syntax(self, values, names, weights):
        assert parse_models(values) == (names, weights)

    def test_a_bad_weight_is_an_error(self):
        with pytest.raises(ValueError):
            parse_models(["a=lots"])


def _chat_callback(seen: list):
    """Answer every /api/chat, recording (model, prompt) per request."""

    def callback(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body["model"], body["messages"][-1]["content"]))
        return httpx.Response(200, text=_ndjson_stream())

    return callback


class TestSeededRuns:
    async def _run(self, httpx_mock, seed, **kw):
        seen: list = []
        httpx_mock.add_callback(_chat_callback(seen), url=f"{ROUTER}/api/chat", is_reusable=True)
        results: list = []
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            await run_load(
                c,
                [ModelTarget("a", 1, 1), ModelTarget("b", 1, 1)],
                30.0,
                asyncio.Event(),
                results,
                seed=seed,
                **kw,
            )
        return seen, results

    @pytest.mark.asyncio
    async def test_same_seed_same_models_and_prompts(self, httpx_mock):
        kw = dict(sequence="random", concurrency=1, max_requests=12, prompt_tokens=200)
        first, _ = await self._run(httpx_mock, 11, **kw)
        httpx_mock.reset()
        second, _ = await self._run(httpx_mock, 11, **kw)
        httpx_mock.reset()
        other, _ = await self._run(httpx_mock, 12, **kw)
        assert first == second and len(first) == 12
        assert first != other

    @pytest.mark.asyncio
    async def test_alternation_is_strict_and_sequential(self, httpx_mock):
        """What model-alternation-benchmark.py did: A, B, A, B, one at a time."""
        seen, results = await self._run(
            httpx_mock, 1, sequence="alternate", concurrency=1, max_requests=6
        )
        assert [m for m, _ in seen] == ["a", "b"] * 3
        ordered = sorted(results, key=lambda r: r.started_ms)
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            assert nxt.started_ms >= prev.started_ms + prev.latency_ms - 1
        report = build_report(results, 1.0, [], [], [])
        assert {m["model"]: m["requests"] for m in report["per_model_results"]} == {"a": 3, "b": 3}
        assert all("first_ttft_ms" in m for m in report["per_model_results"])

    @pytest.mark.asyncio
    async def test_sessions_are_seeded_too(self, monkeypatch):
        async def run(seed):
            sent = []
            stop = asyncio.Event()

            async def fake_send(client, model, **kw):
                sent.append((kw["tags"][0], kw["messages"][-1]["content"]))
                if len(sent) == 4:
                    stop.set()
                return RequestResult(model, "a", 1, 1, 1, 1, True, reply="r")

            monkeypatch.setattr(engine, "send_request", fake_send)
            await engine.session_worker(
                lambda: (None, ""),
                "m",
                [],
                stop,
                system_prompt="S",
                turns=2,
                user_tokens=20,
                rng=random.Random(seed),
            )
            return sent

        assert await run(3) == await run(3)


# -- P6: vision workloads --------------------------------------------------------


def test_the_test_image_is_a_real_png():
    png = base64.b64decode(TEST_IMAGE_B64)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR"
    assert struct.unpack(">II", png[16:24]) == (64, 64)
    assert png.endswith(b"IEND\xaeB`\x82")


class TestVision:
    @pytest.mark.asyncio
    async def test_discovery_types_vision_and_finds_sidecar_embeddings(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status",
            json=_status(
                [
                    _node(
                        "a",
                        loaded=["llava:7b", "nomic-embed-text"],
                        vision_embed=["dinov2-vit-s14"],
                    )
                ]
            ),
        )
        httpx_mock.add_response(url=f"{ROUTER}/api/tags", json={"models": []})
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, _ = await discover_fleet(c, include_on_disk=["dinov2-vit-s14"])
        types = {t.name: (t.model_type, t.nodes) for t in targets}
        assert types == {
            "llava:7b": ("vision", ["a"]),
            "nomic-embed-text": ("embed", ["a"]),
            "dinov2-vit-s14": ("embed-image", ["a"]),
        }

    @pytest.mark.asyncio
    async def test_native_text_embeddings_are_found_in_their_sidecar(self, httpx_mock):
        """Run 6: herd serves nomic-embed-text from fastembed, not Ollama, and
        the harness said "not loaded or on disk anywhere" and ran without it."""
        a = _node("a", loaded=["qwen2.5:7b"])
        a["text_embedding"] = {
            "models_available": [
                {"name": "nomic-embed-text", "kind": "embed"},
                {"name": "ms-marco-minilm-l-6-v2", "kind": "rerank"},
            ]
        }
        b = _node("b", loaded=["qwen2.5:7b"])
        b["text_embedding"] = {"models_available": [{"name": "nomic-embed-text", "kind": "embed"}]}
        httpx_mock.add_response(url=f"{ROUTER}/fleet/status", json=_status([a, b]))
        httpx_mock.add_response(url=f"{ROUTER}/api/tags", json={"models": []})
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, _ = await discover_fleet(
                c, include_on_disk=["nomic-embed-text", "ms-marco-minilm-l-6-v2"]
            )
        types = {t.name: (t.model_type, t.nodes) for t in targets}
        assert types == {
            "qwen2.5:7b": ("llm", ["a", "b"]),
            "nomic-embed-text": ("embed", ["a", "b"]),
        }
        assert next(t for t in targets if t.name == "nomic-embed-text").sidecar

    @pytest.mark.asyncio
    async def test_sidecar_embeddings_only_when_asked_for(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status",
            json=_status([_node("a", loaded=["m:7b"], vision_embed=["dinov2-vit-s14"])]),
        )
        httpx_mock.add_response(url=f"{ROUTER}/api/tags", json={"models": []})
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            targets, _ = await discover_fleet(c)
        assert [t.name for t in targets] == ["m:7b"]

    @pytest.mark.asyncio
    async def test_vision_chat_carries_the_image(self, httpx_mock):
        httpx_mock.add_response(url=f"{ROUTER}/api/chat", text=_ndjson_stream())
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            r = await send_one(
                c,
                ModelTarget("llava:7b", 1, 1, model_type="vision", workload="vision"),
                rng=random.Random(1),
                origin=time.monotonic(),
            )
        [msg] = json.loads(httpx_mock.get_requests()[0].content)["messages"]
        assert msg["images"] == [TEST_IMAGE_B64]
        assert msg["content"] in engine.VISION_PROMPTS
        assert r.success and r.model_type == "vision"

    @pytest.mark.asyncio
    async def test_vision_chat_in_openai_format(self, httpx_mock):
        httpx_mock.add_response(url=f"{ROUTER}/v1/chat/completions", text="data: [DONE]\n")
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            await send_request(c, "llava:7b", image=True, api="openai")
        [msg] = json.loads(httpx_mock.get_requests()[0].content)["messages"]
        assert msg["content"][1] == {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{TEST_IMAGE_B64}"},
        }

    @pytest.mark.asyncio
    async def test_embed_image_request_shape(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{ROUTER}/api/embed-image",
            json={"embeddings": [[0.1]], "dimensions": 1},
            headers={"X-Fleet-Node": "a", "X-Fleet-Served-Model": "dinov2-vit-s14"},
        )
        async with httpx.AsyncClient(base_url=ROUTER) as c:
            r = await send_one(
                c,
                ModelTarget("dinov2-vit-s14", 0, 1, model_type="embed-image"),
                rng=random.Random(1),
                origin=time.monotonic(),
            )
        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body == {"model": "dinov2-vit-s14", "images": [TEST_IMAGE_B64]}
        assert r.success and r.model_type == "embed-image" and r.node_id == "a"
        report = build_report([r], 1.0, [], [], [])
        assert report["per_model_results"][0]["model_type"] == "embed-image"

    @pytest.mark.asyncio
    async def test_warmup_warms_vision_and_keeps_sidecar_models(self, httpx_mock):
        httpx_mock.add_response(url="http://a.test:11434/api/chat", json={"done": True})
        nodes = [NodeInfo("a", 1, 1, 0, 1, ollama_url="http://a.test:11434")]
        targets = [
            ModelTarget("llava:7b", 1, 1, nodes=["a"], model_type="vision"),
            ModelTarget("dinov2-vit-s14", 0, 1, nodes=["a"], model_type="embed-image"),
        ]
        kept = await warmup(targets, nodes)
        assert [t.name for t in kept] == ["llava:7b", "dinov2-vit-s14"]
        assert len(httpx_mock.get_requests()) == 1  # only the vision model, at Ollama

    @pytest.mark.asyncio
    async def test_warmup_leaves_native_text_embeddings_alone(self, httpx_mock):
        """Run 6's soak: warming sent nomic-embed-text to Ollama, which herd
        does not serve it from; it FAILED, was dropped, and the weighted run
        stopped with "weights for models not in the run"."""
        httpx_mock.add_response(url="http://a.test:11434/api/chat", json={"done": True})
        nodes = [NodeInfo("a", 1, 1, 0, 1, ollama_url="http://a.test:11434")]
        targets = [
            ModelTarget("qwen2.5:7b", 5, 1, nodes=["a"]),
            ModelTarget("nomic-embed-text", 0, 1, nodes=["a"], model_type="embed", sidecar=True),
        ]
        kept = await warmup(targets, nodes)
        assert [t.name for t in kept] == ["qwen2.5:7b", "nomic-embed-text"]
        assert len(httpx_mock.get_requests()) == 1  # only the chat model, at Ollama


# -- The CLI ---------------------------------------------------------------------


class TestCli:
    @pytest.fixture
    def cli(self):
        return _load_script(REPO / "scripts" / "benchmark.py")

    @pytest.mark.asyncio
    async def test_a_missing_requested_model_fails_the_run(self, cli, httpx_mock, tmp_path):
        """Fail, never shrink: run 6's embeddings arm warned about
        nomic-embed-text, ran without it, and its report looked complete."""
        httpx_mock.add_response(
            url=f"{ROUTER}/fleet/status", json=_status([_node("a", loaded=["m:7b"])])
        )
        httpx_mock.add_response(url=f"{ROUTER}/api/tags", json={"models": []})
        out = tmp_path / "run.json"
        args = cli.parse_args(
            ["--url", ROUTER, "--models", "m:7b", "gone:1b", "--requests", "1",
             "--no-save", "--output", str(out)]
        )  # fmt: skip
        with pytest.raises(SystemExit) as e:
            await cli.run_benchmark(args)
        assert e.value.code == 2
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_output_file_and_the_dashboard_post(self, cli, httpx_mock, tmp_path, monkeypatch):
        """Traffic to one URL, the fleet observed (and the run saved) at another."""
        polls = []

        def fleet(request):
            # Discovery and the run's first poll see one model; the last, two.
            polls.append(1)
            loaded = ["m:7b"] if len(polls) <= 2 else ["m:7b", "n:3b"]
            return httpx.Response(200, json=_status([_node("a", loaded=loaded)]))

        def chat(request):
            return httpx.Response(
                200, text=_ndjson_stream(), headers={"X-Request-Id": "rid", "X-Fleet-Node": "a"}
            )

        posted = []

        def save(request):
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"run_id": "x"})

        httpx_mock.add_callback(fleet, url=f"{ROUTER}/fleet/status", is_reusable=True)
        httpx_mock.add_response(url=f"{ROUTER}/api/tags", json={"models": []})
        httpx_mock.add_callback(chat, url="http://proxy.test/api/chat", is_reusable=True)
        httpx_mock.add_callback(save, url=f"{ROUTER}/dashboard/api/benchmarks")
        out = tmp_path / "run.json"
        args = cli.parse_args(
            [
                "--url",
                "http://proxy.test",
                "--fleet-url",
                ROUTER,
                "--models",
                "m:7b",
                "--requests",
                "3",
                "--concurrency",
                "1",
                "--skip-warmup",
                "--seed",
                "5",
                "--repetition",
                "2",
                "--output",
                str(out),
            ]
        )

        # Keep the 3 s poll interval out of the test: one poll before, one after.
        async def quick_poll(client, stop_event, snapshots, origin=None):
            snapshots.append({**(await client.get("/fleet/status")).json(), "t_ms": 1.0})
            await stop_event.wait()
            snapshots.append({**(await client.get("/fleet/status")).json(), "t_ms": 2.0})

        monkeypatch.setattr(engine, "poll_fleet_status", quick_poll)
        await cli.run_benchmark(args)

        report = json.loads(out.read_text())
        assert len(report["requests"]) == 3
        assert {r["request_id"] for r in report["requests"]} == {"rid"}
        assert report["manifest"]["seed"] == 5 and report["manifest"]["repetition"] == 2
        assert report["manifest"]["run_seed"] is None
        assert report["manifest"]["workload"]["url"] == "http://proxy.test"
        assert report["manifest"]["workload"]["fleet_url"] == ROUTER
        assert report["model_events"] == [
            {"t_ms": 2.0, "node_id": "a", "model": "n:3b", "event": "loaded"}
        ]
        assert report["latency_p90_ms"] is not None
        [record] = posted
        assert "requests" not in record and record["fleet_snapshot"]["manifest"]["seed"] == 5

    @pytest.mark.asyncio
    async def test_no_fleet_alternation_over_the_openai_api(self, cli, httpx_mock, tmp_path):
        """An endpoint with no router (llama-swap): the models as named, no warmup,
        no polling, nothing saved; strictly alternating."""
        seen = []

        def chat(request):
            body = json.loads(request.content)
            seen.append(body["model"])
            assert body["max_tokens"] == 128 and body["temperature"] == 0
            assert body["messages"][0]["content"] == "List three facts."
            events = [
                {"choices": [{"delta": {"content": "Moon"}}]},
                {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 40}},
            ]
            text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text)

        httpx_mock.add_callback(chat, url="http://swap.test/v1/chat/completions", is_reusable=True)
        out = tmp_path / "arm2.json"
        args = cli.parse_args(
            [
                "--url",
                "http://swap.test",
                "--fleet-url",
                "none",
                "--api",
                "openai",
                "--models",
                "a",
                "b",
                "--sequence",
                "alternate",
                "--concurrency",
                "1",
                "--requests",
                "4",
                "--max-tokens",
                "128",
                "--temperature",
                "0",
                "--label",
                "arm2",
            ]
            + ["--prompt", "List three facts.", "--output", str(out)]
        )
        await cli.run_benchmark(args)
        assert seen == ["a", "b", "a", "b"]
        report = json.loads(out.read_text())
        assert [r["completion_tokens"] for r in report["requests"]] == [40] * 4
        assert report["manifest"]["label"] == "arm2" and report["manifest"]["nodes"] == []
        assert report["total_failures"] == 0
        assert {m["model"]: m["requests"] for m in report["per_model_results"]} == {"a": 2, "b": 2}

    @pytest.mark.parametrize(
        "argv",
        [
            "--fleet-url none",
            "--session-turns 3 --sequence random",
            "--no-think --api openai",
            "--models a=1.5 --sequence alternate",
            "--rate 0",
            "--seed 1 --run-seed 2",
        ],
    )
    def test_contradictory_options_are_refused(self, cli, argv):
        with pytest.raises(SystemExit):
            cli.parse_args(argv.split())

    def test_seed_keeps_its_meaning(self, cli):
        assert cli.effective_seed(cli.parse_args(["--seed", "5", "--concurrency", "8"])) == 5
        assert cli.effective_seed(cli.parse_args([])) is None

    def test_run_seed_gives_each_cell_its_own_seed(self, cli):
        """Findings item 35: a concurrency-8 cell replayed the concurrency-4
        cell's first four worker streams exactly, and found them cached."""

        def seed(*argv):
            return cli.effective_seed(cli.parse_args(["--run-seed", "7", "--models", "m", *argv]))

        c4 = seed("--concurrency", "4", "--repetition", "1")
        c8 = seed("--concurrency", "8", "--repetition", "1")
        assert c4 != c8
        assert c4 != seed("--concurrency", "4", "--repetition", "2")
        assert c4 == seed("--concurrency", "4", "--repetition", "1")  # repeatable
        workload = ",".join(engine.DEFAULT_TYPES) + "|m"
        assert c4 == engine.cell_seed(7, workload, 4, None, 1)

    @pytest.mark.asyncio
    async def test_the_manifest_records_the_effective_seed(self, cli, httpx_mock, tmp_path):
        """Rerunning one cell with ``--seed <manifest seed>`` repeats it."""
        events = [
            {"choices": [{"delta": {"content": "x"}}]},
            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ]
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        httpx_mock.add_response(
            url="http://swap.test/v1/chat/completions", text=text, is_reusable=True
        )
        out = tmp_path / "cell.json"
        argv = ["--url", "http://swap.test", "--fleet-url", "none", "--api", "openai",
                "--models", "a", "--concurrency", "4", "--requests", "1", "--run-seed", "7",
                "--repetition", "3", "--output", str(out)]  # fmt: skip
        args = cli.parse_args(argv)
        await cli.run_benchmark(args)
        manifest = json.loads(out.read_text())["manifest"]
        assert manifest["run_seed"] == 7
        assert manifest["seed"] == cli.effective_seed(args) != 7


@pytest.mark.asyncio
async def test_dashboard_runner_stores_the_report_without_requests(monkeypatch):
    from fleet_manager.server import benchmark_runner

    async def fake_discover(client, include_on_disk=()):
        assert "dinov2-vit-s14" in include_on_disk  # embed-image asked for by name
        return [ModelTarget("dinov2-vit-s14", 0, 1, nodes=["a"], model_type="embed-image")], []

    async def fake_warmup(targets, nodes_info, log_fn=None):
        return targets

    async def fake_run_load(client, targets, duration, stop_event, results, **kw):
        assert kw["fleet_client"] is client
        results.append(_ok("dinov2-vit-s14", model_type="embed-image"))
        return engine.LoadResult([], duration, "start", "end", seed=9, window_s=duration)

    class Store:
        saved: list = []

        async def save_benchmark_run(self, data):
            self.saved.append(data)

    monkeypatch.setattr(benchmark_runner, "discover_fleet", fake_discover)
    monkeypatch.setattr(benchmark_runner, "warmup", fake_warmup)
    monkeypatch.setattr(benchmark_runner, "run_load", fake_run_load)
    runner = benchmark_runner.BenchmarkRunner(base_url=ROUTER)
    store = Store()
    await runner._run("default", 0.1, ["embed-image"], None, store, None, None)
    assert runner._status == "complete", runner._error
    [saved] = store.saved
    assert "requests" not in saved and len(runner._report["requests"]) == 1
    assert saved["fleet_snapshot"]["manifest"]["started_at"] == "start"
    assert saved["fleet_snapshot"]["manifest"]["model_types"] == ["embed-image"]


def test_requested_models_match_with_or_without_latest():
    """Run 4: '--models nomic-embed-text moondream' matched nothing, because
    Ollama reports them as 'nomic-embed-text:latest', and the run went ahead
    with the chat model alone."""
    from fleet_manager.server.benchmark_engine import ModelTarget, match_requested

    targets = [
        ModelTarget(name="qwen3-coder:30b", size_gb=18.6, concurrency=4),
        ModelTarget(name="nomic-embed-text:latest", size_gb=0.3, concurrency=4),
        ModelTarget(name="moondream:latest", size_gb=1.7, concurrency=4),
    ]
    found, weights, missing = match_requested(
        targets, ["moondream", "qwen3-coder:30b", "nomic-embed-text", "absent"], {"moondream": 2}
    )
    assert [t.name for t in found] == [
        "moondream:latest",
        "qwen3-coder:30b",
        "nomic-embed-text:latest",
    ]
    assert weights == {"moondream:latest": 2}
    assert missing == ["absent"]
