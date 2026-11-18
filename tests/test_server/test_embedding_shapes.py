"""Every embed path returns the shape its endpoint promises, or fails loudly.

``/api/embeddings`` (Ollama's legacy endpoint: ``prompt`` in, ``embedding``
out) shared ``/api/embed``'s handler, so a legacy body reached Ollama's
``/api/embed`` with no ``input``.  Ollama answered ``{"embeddings": []}``, the
router returned it with a 200, and the trace said ``completed``: a legacy
client indexed nothing while every signal herd has reported success.

Two fixes, pinned here: the legacy endpoint translates both ways, and no
embed path records ``completed`` unless the backend returned one vector per
input.  See docs/plans/silent-failures-2026-11.md, issue 1.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.server.routes import text_embedding_compat
from fleet_manager.server.routes.routing import (
    EMBEDDING_COUNT_MISMATCH,
    embedding_count_mismatch,
)
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama, _register_embed_node
from tests.test_server.test_routes import create_test_app

MODEL = "mxbai-embed-large:latest"


@pytest.fixture
def app_client(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as c:
        c.app.state.trace_store.record_trace = AsyncMock()
        yield c


def _traces(client: TestClient) -> list[dict]:
    return [c.kwargs for c in client.app.state.trace_store.record_trace.call_args_list]


def _ollama(client: TestClient, response: httpx.Response, seen: dict | None = None):
    _register_embed_node(client, model=MODEL)

    def handler(request: httpx.Request):
        if seen is not None:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
        return response

    _install_mock_ollama(client, "studio", handler)


@pytest.fixture
def native(app_client, monkeypatch):
    """A node running the native text server, answering with ``state["response"]``."""
    hb = make_heartbeat(node_id="studio")
    hb.text_embedding_port = 11439
    app_client.post("/heartbeat", json=hb.model_dump())
    state: dict = {}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request):
        state["body"] = json.loads(request.content)
        return state["response"]

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(text_embedding_compat.httpx, "AsyncClient", patched)
    return state


class TestLegacyEmbeddings:
    def test_ollama_backend_gets_input_and_client_gets_embedding(self, app_client):
        seen: dict = {}
        _ollama(
            app_client,
            httpx.Response(200, json={"model": MODEL, "embeddings": [[0.1, 0.2, 0.3]]}),
            seen,
        )
        resp = app_client.post("/api/embeddings", json={"model": MODEL, "prompt": "hello"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"embedding": [0.1, 0.2, 0.3]}
        assert seen["path"] == "/api/embed"
        assert seen["body"]["input"] == "hello"
        assert "prompt" not in seen["body"]
        assert resp.headers["X-Fleet-Node"] == "studio"
        assert [t["status"] for t in _traces(app_client)] == ["completed"]

    def test_native_backend_returns_embedding(self, app_client, native):
        native["response"] = httpx.Response(
            200, json={"model": "nomic-embed-text", "embeddings": [[1.0, 2.0]]}
        )
        resp = app_client.post(
            "/api/embeddings", json={"model": "nomic-embed-text", "prompt": "hello"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"embedding": [1.0, 2.0]}
        assert native["body"]["input"] == "hello"
        assert "prompt" not in native["body"]
        assert resp.headers["X-Fleet-Backend"] == "native"

    def test_vector_matches_api_embed(self, app_client):
        vec = [0.5, -0.25]
        _ollama(app_client, httpx.Response(200, json={"embeddings": [vec]}))
        legacy = app_client.post("/api/embeddings", json={"model": MODEL, "prompt": "x"})
        current = app_client.post("/api/embed", json={"model": MODEL, "input": "x"})
        assert legacy.json()["embedding"] == current.json()["embeddings"][0] == vec

    @pytest.mark.parametrize(
        "body",
        [{"model": MODEL}, {"model": MODEL, "prompt": ""}, {"model": MODEL, "prompt": ["a"]}],
    )
    def test_prompt_must_be_a_non_empty_string(self, app_client, body):
        resp = app_client.post("/api/embeddings", json=body)
        assert resp.status_code == 400
        assert "prompt" in resp.json()["error"]

    def test_backend_error_passes_through(self, app_client):
        _ollama(app_client, httpx.Response(400, json={"error": "input too long"}))
        resp = app_client.post("/api/embeddings", json={"model": MODEL, "prompt": "x"})
        assert resp.status_code == 400
        assert "input too long" in resp.json()["error"]

    def test_unknown_model_is_404(self, app_client):
        resp = app_client.post("/api/embeddings", json={"model": "nope:1b", "prompt": "x"})
        assert resp.status_code == 404


class TestApiEmbedUnchanged:
    def test_returns_embeddings_list(self, app_client):
        _ollama(app_client, httpx.Response(200, json={"embeddings": [[0.1], [0.2]]}))
        resp = app_client.post("/api/embed", json={"model": MODEL, "input": ["a", "b"]})
        assert resp.status_code == 200
        assert resp.json()["embeddings"] == [[0.1], [0.2]]

    def test_empty_input_list_still_completes(self, app_client):
        _ollama(app_client, httpx.Response(200, json={"embeddings": []}))
        resp = app_client.post("/api/embed", json={"model": MODEL, "input": []})
        assert resp.status_code == 200
        assert [t["status"] for t in _traces(app_client)] == ["completed"]


class TestWrongVectorCountIsAFailure:
    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/embed", {"model": MODEL, "input": "x"}),
            ("/api/embeddings", {"model": MODEL, "prompt": "x"}),
            ("/v1/embeddings", {"model": MODEL, "input": "x"}),
        ],
    )
    def test_ollama_empty_embeddings_is_502_and_failed(self, app_client, path, body):
        """The exact answer Ollama gave the legacy body: a 200 with no vectors."""
        _ollama(app_client, httpx.Response(200, json={"model": MODEL, "embeddings": []}))
        resp = app_client.post(path, json=body)
        assert resp.status_code == 502, resp.text
        traces = _traces(app_client)
        assert [t["status"] for t in traces] == ["failed"]
        assert traces[0]["error_message"] == EMBEDDING_COUNT_MISMATCH

    def test_ollama_short_count_is_502(self, app_client):
        _ollama(app_client, httpx.Response(200, json={"embeddings": [[0.1]]}))
        resp = app_client.post("/api/embed", json={"model": MODEL, "input": ["a", "b"]})
        assert resp.status_code == 502
        assert "1 embedding(s) for 2 input(s)" in resp.json()["error"]

    def test_native_dropped_vector_is_502_and_failed(self, app_client, native):
        """fastembed drops empty strings, shifting every later vector."""
        native["response"] = httpx.Response(200, json={"embeddings": [[1.0]]})
        resp = app_client.post(
            "/api/embed", json={"model": "nomic-embed-text", "input": ["a", "b"]}
        )
        assert resp.status_code == 502
        traces = _traces(app_client)
        assert [t["status"] for t in traces] == ["failed"]
        assert traces[0]["error_message"] == EMBEDDING_COUNT_MISMATCH


class TestEmbeddingCountMismatch:
    @pytest.mark.parametrize(
        "body,result,expected",
        [
            ({"input": "x"}, {"embeddings": [[1]]}, None),
            ({"input": ["a", "b"]}, {"embeddings": [[1], [2]]}, None),
            ({"input": []}, {"embeddings": []}, None),
            ({"input": "x"}, {"embeddings": []}, (1, 0)),
            ({"input": ["a", "b"]}, {"embeddings": [[1]]}, (2, 1)),
            ({"input": "x"}, {}, (1, 0)),
            ({"input": "x"}, "not a dict", (1, 0)),
            ({"images": ["..."]}, {"embeddings": []}, None),
        ],
    )
    def test_table(self, body, result, expected):
        assert embedding_count_mismatch(body, result) == expected

    def test_trace_text_is_digit_free(self):
        """categorize_error files bare status-like numbers, so counts stay out."""
        assert not any(ch.isdigit() for ch in EMBEDDING_COUNT_MISMATCH)
