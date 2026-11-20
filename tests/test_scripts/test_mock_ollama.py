"""scripts/mock_ollama.py: the Ollama stand-in the scale and failure runs use.

The fault tests feed the mock's answers to the router's own classifiers, so a
mock that paraphrased Ollama's error text (and so tested a different router)
fails here rather than in a misleading measurement.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.common.errors import categorize_error, ollama_stream_error
from fleet_manager.server.streaming import StreamingProxy
from tests.test_scripts import load_script

mock = load_script("mock_ollama")

CHAT = {"model": "llama3.2:3b", "messages": [{"role": "user", "content": "hello"}]}


def client(**config) -> TestClient:
    config.setdefault("models", ["llama3.2:3b:2.0", "nomic-embed-text:0.3", "llava:7b:4.7"])
    config.setdefault("delay_ms", 0.0)
    return TestClient(mock.build_app(mock.MockConfig(**config)))


def lines(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def as_httpx_error(resp) -> httpx.HTTPStatusError:
    """The mock's answer as the httpx error the router's proxy would raise."""
    request = httpx.Request("POST", "http://10.0.0.5:11434/api/chat")
    response = httpx.Response(resp.status_code, content=resp.content, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError(f"expected an error status, got {resp.status_code}")


class TestModelSpecs:
    def test_name_and_size(self):
        assert mock.MockModel.parse("llama3.2:3b:2.0") == mock.MockModel("llama3.2:3b", 2.0)

    def test_tag_without_size_keeps_the_default_size(self):
        assert mock.MockModel.parse("mock:latest") == mock.MockModel(
            "mock:latest", mock.DEFAULT_SIZE_GB
        )

    def test_untagged_name_is_latest(self):
        assert mock.MockModel.parse("nomic-embed-text:0.3").name == "nomic-embed-text:latest"

    def test_default_is_the_one_model_the_overhead_runs_used(self):
        tags = TestClient(mock.build_app()).get("/api/tags").json()["models"]
        assert [(m["name"], m["size"]) for m in tags] == [("mock:latest", 1_000_000)]


class TestMetadata:
    def test_tags_list_every_model_with_size_digest_and_quantization(self):
        tags = {m["name"]: m for m in client().get("/api/tags").json()["models"]}
        assert set(tags) == {"llama3.2:3b", "nomic-embed-text:latest", "llava:7b"}
        llama = tags["llama3.2:3b"]
        assert llama["size"] == 2_000_000_000
        assert len(llama["digest"]) == 64
        assert llama["details"]["quantization_level"] == "Q4_K_M"
        assert llama["details"]["parameter_size"] == "3B"

    def test_capabilities_follow_model_type(self):
        tags = {m["name"]: m for m in client().get("/api/tags").json()["models"]}
        assert tags["nomic-embed-text:latest"]["capabilities"] == ["embedding"]
        assert "vision" in tags["llava:7b"]["capabilities"]
        assert "completion" in tags["llama3.2:3b"]["capabilities"]

    def test_show(self):
        c = client(context_length=8192)
        shown = c.post("/api/show", json={"model": "llava:7b"}).json()
        assert "vision" in shown["capabilities"]
        assert shown["model_info"]["llava.context_length"] == 8192
        missing = c.post("/api/show", json={"model": "nope:1b"})
        assert missing.status_code == 404
        assert missing.json() == {"error": "model 'nope:1b' not found"}

    def test_the_node_reads_ps_like_a_real_ollama(self):
        """What herd-node's OllamaClient takes from /api/ps."""
        ps = client(context_length=8192).get("/api/ps").json()["models"]
        assert {m["model"] for m in ps} == {"llama3.2:3b", "nomic-embed-text:latest", "llava:7b"}
        assert all(m["context_length"] == 8192 and m["size_vram"] == m["size"] for m in ps)


class TestChat:
    def test_streams_the_configured_chunks_then_done(self):
        out = lines(client(chunks=3).post("/api/chat", json=CHAT))
        assert [o["done"] for o in out] == [False, False, False, True]
        assert "".join(o["message"]["content"] for o in out) == "ok ok ok"
        assert out[-1]["eval_count"] == 3
        assert out[-1]["done_reason"] == "stop"

    def test_non_streaming_agrees_with_the_stream(self):
        body = client(chunks=3).post("/api/chat", json={**CHAT, "stream": False}).json()
        assert body["done"] is True
        assert body["message"]["content"] == "ok ok ok"
        assert body["eval_count"] == 3

    def test_generate_streams_response_text(self):
        out = lines(client().post("/api/generate", json={"model": "llama3.2:3b", "prompt": "x"}))
        assert out[0]["response"] == "ok" and out[-1]["done"] is True

    def test_prompt_tokens_estimated_from_content(self):
        chat = {**CHAT, "messages": [{"role": "user", "content": "x" * 400}]}
        assert lines(client().post("/api/chat", json=chat))[-1]["prompt_eval_count"] == 100

    def test_unknown_model_is_ollamas_404(self):
        resp = client().post("/api/chat", json={**CHAT, "model": "ghost:7b"})
        assert resp.status_code == 404
        assert StreamingProxy._is_model_not_found_error(as_httpx_error(resp))


class TestLoadedModels:
    def test_hot_model_cap_evicts_the_least_recently_used(self):
        c = client(max_loaded=2, load_ms=1.0)
        ps = lambda: [m["name"] for m in c.get("/api/ps").json()["models"]]  # noqa: E731
        assert ps() == ["nomic-embed-text:latest", "llama3.2:3b"]  # first two, newest first
        out = lines(c.post("/api/chat", json={**CHAT, "model": "llava:7b"}))
        assert out[-1]["load_duration"] > 0
        assert ps() == ["llava:7b", "nomic-embed-text:latest"]
        warm = lines(c.post("/api/chat", json={**CHAT, "model": "llava:7b"}))
        assert warm[-1]["load_duration"] == 0

    def test_load_only_generate_and_keep_alive_zero(self):
        c = client(max_loaded=1)
        loaded = c.post("/api/generate", json={"model": "llava:7b", "prompt": ""}).json()
        assert loaded["done_reason"] == "load"
        assert [m["name"] for m in c.get("/api/ps").json()["models"]] == ["llava:7b"]
        c.post("/api/generate", json={"model": "llava:7b", "keep_alive": 0})
        assert c.get("/api/ps").json()["models"] == []

    def test_requests_release_the_model_when_they_end(self):
        c = client(max_loaded=1, chunks=3)
        backend = c.app.state.backend
        lines(c.post("/api/chat", json=CHAT))
        c.post("/api/chat", json={**CHAT, "stream": False})
        c.post("/api/embed", json={"model": "nomic-embed-text", "input": "x"})
        c.post("/_mock/fault", json={"die_after_chunks": 1})
        c.post("/api/chat", json=CHAT)  # the stream that dies
        c.post("/api/chat", json={**CHAT, "stream": False})
        assert not any(backend.busy.values())


class TestBusyModels:
    """Ollama waits for a runner's requests before unloading it; so must the mock."""

    def backend(self, max_loaded=1):
        models = [mock.MockModel("a:latest", 1.0), mock.MockModel("b:latest", 1.0)]
        return mock.Backend(models, max_loaded=max_loaded, load_ms=0.0)

    @pytest.mark.asyncio
    async def test_a_load_waits_for_the_busy_model_to_go_idle(self):
        backend = self.backend()
        await backend.acquire("a:latest")
        loading = asyncio.create_task(backend.acquire("b:latest"))
        await asyncio.sleep(0.05)
        assert not loading.done()
        assert list(backend.loaded) == ["a:latest"]  # still listed by /api/ps
        backend.release("a:latest")
        await asyncio.wait_for(loading, 1)
        assert list(backend.loaded) == ["b:latest"]
        assert backend.busy["b:latest"] == 1

    @pytest.mark.asyncio
    async def test_an_idle_model_is_evicted_at_once(self):
        backend = self.backend()
        await backend.acquire("a:latest")
        backend.release("a:latest")
        await asyncio.wait_for(backend.acquire("b:latest"), 1)
        assert list(backend.loaded) == ["b:latest"]

    @pytest.mark.asyncio
    async def test_keep_alive_zero_on_a_busy_model_unloads_it_when_idle(self):
        backend = self.backend(max_loaded=0)
        await backend.acquire("a:latest")
        await backend.acquire("a:latest")
        backend.unload("a:latest")
        backend.release("a:latest")
        assert "a:latest" in backend.loaded
        backend.release("a:latest")
        assert "a:latest" not in backend.loaded


class TestEmbed:
    def test_fixed_dimension_deterministic_vectors(self):
        c = client(embed_dim=16)
        body = {"model": "nomic-embed-text", "input": ["a", "b", "a"]}
        out = c.post("/api/embed", json=body).json()
        assert [len(v) for v in out["embeddings"]] == [16, 16, 16]
        assert out["embeddings"][0] == out["embeddings"][2] != out["embeddings"][1]

    def test_single_string_input(self):
        out = client().post("/api/embed", json={"model": "nomic-embed-text", "input": "a"})
        assert len(out.json()["embeddings"]) == 1


class TestFaults:
    def set_fault(self, c: TestClient, **fault) -> dict:
        resp = c.post("/_mock/fault", json=fault)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_queue_full_503_is_the_text_the_router_will_not_retry(self):
        c = client()
        self.set_fault(c, error_rate=1.0, status=503)
        error = as_httpx_error(c.post("/api/chat", json=CHAT))
        assert StreamingProxy._is_queue_full_error(error)
        assert not StreamingProxy._is_retryable_error(error)

    def test_500_is_retryable(self):
        c = client()
        self.set_fault(c, error_rate=1.0, status=500)
        error = as_httpx_error(c.post("/api/chat", json=CHAT))
        assert StreamingProxy._is_retryable_error(error)

    def test_404_fault_is_model_not_found(self):
        c = client()
        self.set_fault(c, error_rate=1.0, status=404)
        error = as_httpx_error(c.post("/api/chat", json=CHAT))
        assert StreamingProxy._is_model_not_found_error(error)

    def test_message_overrides_the_text(self):
        c = client()
        self.set_fault(c, error_rate=1.0, status=503, message="upstream restarting")
        error = as_httpx_error(c.post("/api/chat", json=CHAT))
        assert not StreamingProxy._is_queue_full_error(error)
        assert StreamingProxy._is_retryable_error(error)  # a generic 503 is retried

    def test_error_rate_is_a_share(self):
        c = client(seed=7)
        self.set_fault(c, error_rate=0.5)
        statuses = [c.post("/api/chat", json=CHAT).status_code for _ in range(100)]
        assert 25 < statuses.count(500) < 75
        assert statuses.count(500) + statuses.count(200) == 100

    def test_models_filter_targets_one_model(self):
        c = client()
        self.set_fault(c, error_rate=1.0, models=["llama3.2:3b"])
        assert c.post("/api/chat", json=CHAT).status_code == 500
        assert c.post("/api/chat", json={**CHAT, "model": "llava:7b"}).status_code == 200

    def test_die_after_chunks_ends_the_stream_without_done(self):
        c = client(chunks=5)
        self.set_fault(c, die_after_chunks=2)
        out = lines(c.post("/api/chat", json=CHAT))
        assert len(out) == 2 and not any(o["done"] for o in out)

    def test_die_before_the_first_chunk(self):
        c = client(chunks=5)
        self.set_fault(c, die_after_chunks=0)
        assert c.post("/api/chat", json=CHAT).text == ""
        assert c.post("/api/chat", json={**CHAT, "stream": False}).text == ""

    def test_context_limit_streams_ollamas_error_line(self):
        c = client()
        self.set_fault(c, context_limit=10)
        chat = {**CHAT, "messages": [{"role": "user", "content": "x" * 400}]}
        (line,) = lines(c.post("/api/chat", json=chat))
        message = ollama_stream_error(line)
        assert message.startswith("input length (100 tokens) exceeds")
        assert categorize_error(message) == "context_too_long"
        assert c.post("/api/chat", json={**chat, "stream": False}).status_code == 400
        assert c.post("/api/chat", json=CHAT).status_code == 200  # short prompt still fine

    def test_context_limit_applies_to_embed(self):
        c = client()
        self.set_fault(c, context_limit=10)
        body = {"model": "nomic-embed-text", "input": "x" * 400}
        assert c.post("/api/embed", json=body).status_code == 400

    def test_latency_delays_the_answer(self):
        c = client()
        self.set_fault(c, latency_ms=50)
        assert lines(c.post("/api/chat", json=CHAT))[-1]["total_duration"] >= 50_000_000

    def test_get_reflects_post_and_empty_object_clears(self):
        c = client()
        self.set_fault(c, error_rate=1.0, status=503)
        assert c.get("/_mock/fault").json()["status"] == 503
        self.set_fault(c)
        assert c.get("/_mock/fault").json() == mock.asdict(mock.Fault())
        assert c.post("/api/chat", json=CHAT).status_code == 200

    @pytest.mark.parametrize(
        "fault", [{"error_rat": 1}, {"error_rate": 2}, {"status": 200}, [1, 2]]
    )
    def test_invalid_fault_is_refused(self, fault):
        c = client()
        resp = c.post("/_mock/fault", json=fault)
        assert resp.status_code == 422
        assert c.get("/_mock/fault").json() == mock.asdict(mock.Fault())

    def test_startup_fault(self):
        c = client(fault={"error_rate": 1.0, "status": 503})
        assert c.post("/api/chat", json=CHAT).status_code == 503

    def test_fault_file_is_shared_between_workers(self, tmp_path):
        path = tmp_path / "fault.json"
        mock.write_fault_file(str(path), mock.Fault())
        one = client(fault_file=str(path))
        two = client(fault_file=str(path))
        self.set_fault(one, error_rate=1.0, status=503)
        assert two.post("/api/chat", json=CHAT).status_code == 503
        assert two.get("/_mock/fault").json()["status"] == 503

    def test_a_fault_rewritten_within_one_mtime_tick_is_seen(self, tmp_path):
        """Two writes in one filesystem timestamp tick (coarse on some filesystems)."""
        path = tmp_path / "fault.json"
        mock.write_fault_file(str(path), mock.Fault())
        store = mock.FaultStore(mock.Fault(), str(path))
        assert store.get().status == 500
        before = os.stat(path).st_mtime_ns
        mock.write_fault_file(str(path), mock.Fault(status=503))
        os.utime(path, ns=(before, before))
        assert store.get().status == 503

    def test_metadata_endpoints_ignore_faults(self):
        """A faulty backend still answers the node's polls, so it stays registered."""
        c = client()
        self.set_fault(c, error_rate=1.0, status=500)
        assert c.get("/api/tags").status_code == 200
        assert c.get("/api/ps").status_code == 200
