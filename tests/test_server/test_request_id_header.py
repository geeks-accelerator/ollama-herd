"""``X-Request-Id``: the id a response reports is the id its trace row is under.

A client (the benchmark, or anyone reporting "this request was slow") reads the
header and asks ``/dashboard/api/traces?request_id=`` for the router's side of
the same request: node, retries, fallback, queue and route time.  That only
works if the two ids are one value, so these tests compare the header with the
row the route actually wrote, on every surface that writes one, streaming and
not, success and rejection.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from fleet_manager.models.node import VisionEmbeddingMetrics, VisionEmbeddingModel
from fleet_manager.server.cors import EXPOSED_HEADERS
from fleet_manager.server.fleet_headers import (
    REQUEST_ID_HEADER,
    bind_request_id,
    install_request_id_header,
)
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama
from tests.test_server.test_routes import create_test_app

MODEL = "qwen3-coder:30b"
EMBED_MODEL = "mxbai-embed-large"
MESSAGES = [{"role": "user", "content": "hi"}]
LINES = [
    {
        "model": MODEL,
        "message": {"role": "assistant", "content": "ready"},
        "response": "ready",
        "done": False,
    },
    {
        "model": MODEL,
        "message": {"role": "assistant", "content": ""},
        "response": "",
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 5,
        "eval_count": 1,
    },
]


# ---------------------------------------------------------------------------
# The middleware on its own
# ---------------------------------------------------------------------------


def _bare_app() -> Starlette:
    async def bound(request):
        bind_request_id(request, "rid-1")
        return JSONResponse({"ok": True})

    async def bound_stream(request):
        rid = bind_request_id(request)

        async def body():
            yield b"a"
            yield b"b"

        return StreamingResponse(body(), headers={"X-Echo": rid})

    async def unbound(request):
        return JSONResponse({"ok": True})

    async def crash(request):
        bind_request_id(request, "rid-500")
        raise RuntimeError("unhandled")

    app = Starlette(
        routes=[
            Route("/bound", bound),
            Route("/stream", bound_stream),
            Route("/unbound", unbound),
            Route("/crash", crash),
        ]
    )
    install_request_id_header(app)
    return app


class TestMiddleware:
    def test_bound_id_is_returned(self):
        resp = TestClient(_bare_app()).get("/bound")
        assert resp.headers[REQUEST_ID_HEADER] == "rid-1"

    def test_streaming_response_carries_it(self):
        resp = TestClient(_bare_app()).get("/stream")
        assert resp.text == "ab"
        # bind_request_id with no id makes one, and returns what it bound.
        assert resp.headers[REQUEST_ID_HEADER] == resp.headers["X-Echo"]
        assert len(resp.headers[REQUEST_ID_HEADER]) == 36

    def test_no_header_when_nothing_was_bound(self):
        resp = TestClient(_bare_app()).get("/unbound")
        assert REQUEST_ID_HEADER not in resp.headers

    def test_browser_clients_may_read_it(self):
        assert REQUEST_ID_HEADER in EXPOSED_HEADERS

    def test_an_unhandled_500_carries_it(self):
        """Starlette's ServerErrorMiddleware answers an unhandled exception
        itself, outside every ``add_middleware`` layer, so a 500 -- the
        response most worth looking up -- had no ``X-Request-Id``."""
        resp = TestClient(_bare_app(), raise_server_exceptions=False).get("/crash")
        assert resp.status_code == 500
        assert resp.headers[REQUEST_ID_HEADER] == "rid-500"

    def test_the_router_app_is_wired_the_same_way(self):
        from fleet_manager.models.config import ServerSettings
        from fleet_manager.server.app import create_app

        app = create_app(ServerSettings())

        @app.get("/crash")
        async def crash(request: Request):
            bind_request_id(request, "rid-app")
            raise RuntimeError("unhandled")

        resp = TestClient(app, raise_server_exceptions=False).get("/crash")
        assert resp.status_code == 500
        assert resp.headers[REQUEST_ID_HEADER] == "rid-app"


# ---------------------------------------------------------------------------
# Through the real routes
# ---------------------------------------------------------------------------


def _ollama(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/api/chat", "/api/generate"):
        body = "".join(json.dumps(line) + "\n" for line in LINES).encode()
        return httpx.Response(200, content=body, headers={"content-type": "application/x-ndjson"})
    if request.url.path == "/api/embed":
        return httpx.Response(200, json={"model": EMBED_MODEL, "embeddings": [[0.1]]})
    return httpx.Response(200, json={})


@pytest.fixture
def fleet(tmp_path):
    from fleet_manager.server.routes import embedding_compat, image_compat, transcription_compat

    app = create_test_app(tmp_path=tmp_path)
    for module in (embedding_compat, image_compat, transcription_compat):
        app.include_router(module.router)
    with TestClient(app) as client:
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[(MODEL, 18.0), (EMBED_MODEL, 1.0)],
            available_models=[MODEL, EMBED_MODEL],
        )
        client.post("/heartbeat", json=hb.model_dump())
        _install_mock_ollama(client, "studio", _ollama)
        # The shared test app gives the proxy no trace store; app.py does.
        client.app.state.streaming_proxy._trace_store = client.app.state.trace_store
        yield client


def _trace_rows(client: TestClient, request_id: str) -> list[dict]:
    """The rows under ``request_id``, via the endpoint a client would use.

    Traces are written after the response (a streamed one finishes first), so
    give the write a moment.
    """
    deadline = time.monotonic() + 3.0
    while True:
        resp = client.get("/dashboard/api/traces", params={"request_id": request_id})
        rows = resp.json()["traces"]
        if rows or time.monotonic() > deadline:
            return rows
        time.sleep(0.02)


CHAT = {
    "ollama-chat": ("/api/chat", {"model": MODEL, "messages": MESSAGES}),
    "ollama-generate": ("/api/generate", {"model": MODEL, "prompt": "hi"}),
    "openai": ("/v1/chat/completions", {"model": MODEL, "messages": MESSAGES}),
    "anthropic": ("/v1/messages", {"model": MODEL, "max_tokens": 64, "messages": MESSAGES}),
    "responses": ("/v1/responses", {"model": MODEL, "input": "hi"}),
}


class TestEverySurface:
    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize("surface", list(CHAT))
    def test_header_names_the_trace_row(self, fleet, surface, stream):
        path, body = CHAT[surface]
        resp = fleet.post(path, json={**body, "stream": stream})
        assert resp.status_code == 200, resp.text
        rid = resp.headers[REQUEST_ID_HEADER]
        rows = _trace_rows(fleet, rid)
        assert [r["status"] for r in rows] == ["completed"]
        assert rows[0]["node_id"] == "studio"

    def test_each_request_gets_its_own_id(self, fleet):
        path, body = CHAT["openai"]
        ids = {
            fleet.post(path, json={**body, "stream": False}).headers[REQUEST_ID_HEADER]
            for _ in range(3)
        }
        assert len(ids) == 3

    def test_a_rejection_reports_the_rejected_row(self, fleet):
        # An mlx: model cannot be reached through the Ollama API: rejected, traced.
        resp = fleet.post("/api/chat", json={"model": "mlx:absent", "messages": MESSAGES})
        assert resp.status_code >= 400, resp.text
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert row["status"] == "rejected"
        assert row["model"] == "mlx:absent"

    def test_ollama_embed(self, fleet):
        resp = fleet.post("/api/embed", json={"model": EMBED_MODEL, "input": "x"})
        assert resp.status_code == 200, resp.text
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert row["status"] == "completed"
        assert row["model"].startswith(EMBED_MODEL)

    def test_routes_with_no_trace_return_no_id(self, fleet):
        assert REQUEST_ID_HEADER not in fleet.get("/fleet/status").headers
        assert REQUEST_ID_HEADER not in fleet.get("/api/tags").headers


class TestOllamaEmbedRetry:
    """The Ollama embed path retries a ReadTimeout on the same node.  Every
    retry leaves one ``retried`` row on the node that failed -- the rule the
    trace store counts retries by -- so a timed-out attempt is visible and the
    request's retries are counted once, against that node."""

    def test_a_timed_out_attempt_is_a_retried_row(self, fleet, monkeypatch):
        from fleet_manager.server.routes import ollama_compat

        monkeypatch.setattr(ollama_compat, "EMBED_RETRY_BACKOFFS_S", (0.0, 0.0))
        calls = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/embed":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise httpx.ReadTimeout("queue full", request=request)
            return _ollama(request)

        _install_mock_ollama(fleet, "studio", flaky)
        resp = fleet.post("/api/embed", json={"model": EMBED_MODEL, "input": "x"})
        assert resp.status_code == 200, resp.text
        rid = resp.headers[REQUEST_ID_HEADER]
        deadline = time.monotonic() + 3.0
        while len(rows := _trace_rows(fleet, rid)) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        by_status = {r["status"]: r for r in rows}
        assert sorted(by_status) == ["completed", "retried"]
        assert by_status["retried"]["retry_count"] == 0
        assert by_status["completed"]["retry_count"] == 1
        assert {r["node_id"] for r in rows} == {"studio"}
        assert resp.headers["X-Fleet-Retries"] == "1"


class TestDirectProxyPaths:
    """Native text embedding and vision embedding bypass the queue and write
    their own trace; the header must carry that trace's id."""

    @pytest.fixture
    def outbound(self, monkeypatch):
        from fleet_manager.server import streaming
        from fleet_manager.server.routes import text_embedding_compat

        state = {"handler": None}
        real_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(lambda r: state["handler"](r))
            return real_client(*args, **kwargs)

        monkeypatch.setattr(streaming.httpx, "AsyncClient", patched)
        monkeypatch.setattr(text_embedding_compat.httpx, "AsyncClient", patched)
        return state

    def test_native_text_embedding(self, fleet, outbound):
        hb = make_heartbeat(node_id="mini")
        hb.text_embedding_port = 11439
        fleet.post("/heartbeat", json=hb.model_dump())
        outbound["handler"] = lambda r: httpx.Response(
            200, json={"model": "nomic-embed-text", "embeddings": [[1.0]], "prompt_eval_count": 2}
        )
        resp = fleet.post("/api/embed", json={"model": "nomic-embed-text", "input": "x"})
        assert resp.status_code == 200, resp.text
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert (row["status"], row["node_id"]) == ("completed", "mini")

    def test_native_rejection(self, fleet):
        # Reranking has no Ollama fallback: with no native server it is rejected.
        resp = fleet.post(
            "/v1/rerank", json={"model": "bge-reranker-base", "query": "q", "documents": ["d"]}
        )
        assert resp.status_code == 503, resp.text
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert row["status"] == "rejected"

    def test_vision_embedding(self, fleet, outbound):
        hb = make_heartbeat(node_id="mini")
        hb.vision_embedding_port = 11438
        hb.vision_embedding = VisionEmbeddingMetrics(
            models_available=[
                VisionEmbeddingModel(name="dinov2-vit-s14", runtime="onnx", dimensions=384)
            ]
        )
        fleet.post("/heartbeat", json=hb.model_dump())
        outbound["handler"] = lambda r: httpx.Response(
            200, json={"model": "dinov2-vit-s14", "embeddings": [[0.1]], "dimensions": 384}
        )
        resp = fleet.post("/api/embed-image", json={"model": "dinov2", "images": ["aGk="]})
        assert resp.status_code == 200, resp.text
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert (row["status"], row["node_id"]) == ("completed", "mini")


class TestQueuedMediaPaths:
    """Image generation and transcription are queued; the header carries the
    queue entry's id, which is the id their trace row is under (more outcomes
    in test_media_traces.py)."""

    def test_image_generation(self, fleet):
        fleet.post("/dashboard/api/settings", json={"image_generation": True})
        hb = make_heartbeat(node_id="studio", loaded_models=[(MODEL, 18.0)]).model_dump()
        hb["image"] = {
            "models_available": [{"name": "z-image-turbo", "binary": "mflux-generate"}],
            "generating": False,
        }
        hb["image_port"] = 11436
        fleet.post("/heartbeat", json=hb)
        fleet.app.state.streaming_proxy.generate_image_on_node = AsyncMock(return_value=b"PNG")
        resp = fleet.post("/api/generate-image", json={"model": "z-image-turbo", "prompt": "a"})
        assert resp.status_code == 200, resp.text
        assert len(resp.headers[REQUEST_ID_HEADER]) == 36
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert row["status"] == "completed"

    def test_transcription(self, fleet):
        fleet.post("/dashboard/api/settings", json={"transcription": True})
        hb = make_heartbeat(node_id="studio", loaded_models=[(MODEL, 18.0)]).model_dump()
        hb["transcription"] = {
            "models_available": [{"name": "qwen3-asr", "binary": "mlx-qwen3-asr"}],
            "transcribing": False,
        }
        hb["transcription_port"] = 11437
        fleet.post("/heartbeat", json=hb)
        fleet.app.state.streaming_proxy.transcribe_on_node = AsyncMock(
            return_value={"text": "hello"}
        )
        resp = fleet.post("/api/transcribe", files={"audio": ("a.wav", b"RIFF", "audio/wav")})
        assert resp.status_code == 200, resp.text
        assert len(resp.headers[REQUEST_ID_HEADER]) == 36
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# /dashboard/api/traces filters
# ---------------------------------------------------------------------------


class TestTracesFilters:
    def test_tag_and_since_reach_the_store(self, fleet):
        path, body = CHAT["openai"]
        tagged = fleet.post(
            path, json={**body, "stream": False}, headers={"X-Herd-Tags": "bench-run"}
        ).headers[REQUEST_ID_HEADER]
        plain = fleet.post(path, json={**body, "stream": False}).headers[REQUEST_ID_HEADER]
        _trace_rows(fleet, tagged), _trace_rows(fleet, plain)

        rows = fleet.get("/dashboard/api/traces", params={"tag": "bench-run"}).json()["traces"]
        assert [r["request_id"] for r in rows] == [tagged]
        assert "bench-run" in rows[0]["tags"]

        future = fleet.get("/dashboard/api/traces", params={"since": time.time() + 60})
        assert future.json()["traces"] == []
        past = fleet.get("/dashboard/api/traces", params={"since": 0, "limit": 1}).json()
        assert [r["request_id"] for r in past["traces"]] == [plain]

    def test_rows_include_finish_reason_estimate_and_timings(self, fleet):
        path, body = CHAT["ollama-chat"]
        rid = fleet.post(path, json={**body, "stream": False}).headers[REQUEST_ID_HEADER]
        [row] = _trace_rows(fleet, rid)
        assert row["finish_reason"] == "stop"
        assert "estimated_tokens" in row
        assert set(row["timings"]) >= {"route_ms", "queue_ms"}
