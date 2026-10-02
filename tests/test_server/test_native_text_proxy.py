"""The native text server proxy (fastembed on :11439): every outcome, by behavior.

``embed_text`` had one end-to-end test (the success path, via ``/api/embed``)
and a test that grepped its source for two strings.  It is reached only through
``/api/embed`` → ``dispatch_embed``: its own ``/api/embed-text`` decorator sits
on a router that is never mounted, so these drive the real entry point.  These pin each outcome
— success, timeout, transport error, backend error, no server — and the trace
each one leaves, so the proxy core can be shared with ``/v1/rerank`` without
anything moving.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_heartbeat
from tests.test_server.test_routes import create_test_app


@pytest.fixture
def app_client(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as c:
        c.app.state.trace_store.record_trace = AsyncMock()
        yield c


def _with_text_server(client: TestClient, node_id: str = "mini") -> None:
    hb = make_heartbeat(node_id=node_id)
    hb.text_embedding_port = 11439
    client.post("/heartbeat", json=hb.model_dump())


@pytest.fixture
def native_backend(monkeypatch):
    """Route the proxy's outbound httpx client to a handler the test sets."""
    from fleet_manager.server.routes import text_embedding_compat

    state = {"handler": None}
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda r: state["handler"](r))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(text_embedding_compat.httpx, "AsyncClient", patched)
    return state


def _trace(client: TestClient) -> dict:
    return client.app.state.trace_store.record_trace.call_args.kwargs


class TestEmbedTextProxy:
    def test_success_returns_result_with_node_and_traces_request_size(
        self, app_client, native_backend
    ):
        _with_text_server(app_client)
        native_backend["handler"] = lambda r: httpx.Response(
            200,
            json={
                "model": "nomic-embed-text",
                "embeddings": [[1.0]],
                "prompt_eval_count": 7,
            },
        )
        resp = app_client.post("/api/embed", json={"model": "nomic-embed-text", "input": "x"})

        assert resp.status_code == 200
        assert resp.json()["node"] == "mini"
        assert resp.headers["X-Fleet-Node"] == "mini"
        t = _trace(app_client)
        assert t["status"] == "completed"
        assert t["prompt_tokens"] == 7
        assert t["tags"] == ["embed", "text-embed"]
        assert t["original_format"] == "embed"

    def test_read_timeout_is_504_with_download_hint(self, app_client, native_backend):
        _with_text_server(app_client)

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        native_backend["handler"] = timeout
        resp = app_client.post("/api/embed", json={"model": "nomic-embed-text", "input": "x"})
        assert resp.status_code == 504
        assert resp.json()["node"] == "mini"
        assert "downloading" in resp.json()["error"]
        assert _trace(app_client)["status"] == "failed"

    def test_transport_error_is_502(self, app_client, native_backend):
        _with_text_server(app_client)

        def refused(request):
            raise httpx.ConnectError("refused", request=request)

        native_backend["handler"] = refused
        resp = app_client.post("/api/embed", json={"model": "nomic-embed-text", "input": "x"})
        assert resp.status_code == 502
        assert resp.json()["error"].startswith("Text embedding failed")
        assert _trace(app_client)["status"] == "failed"

    def test_backend_error_passes_through_with_node(self, app_client, native_backend):
        _with_text_server(app_client)
        native_backend["handler"] = lambda r: httpx.Response(500, json={"error": "boom"})
        resp = app_client.post("/api/embed", json={"model": "nomic-embed-text", "input": "x"})
        assert resp.status_code == 500
        assert resp.json() == {"error": "boom", "node": "mini"}
        t = _trace(app_client)
        assert t["status"] == "failed"
        assert t["error_message"].startswith("HTTP 500")

    def test_no_text_server_is_503_with_install_hint(self, app_client):
        app_client.post("/heartbeat", json=make_heartbeat(node_id="mini").model_dump())
        resp = app_client.post("/api/embed", json={"model": "nomic-embed-text", "input": "x"})
        assert resp.status_code == 503
        assert "uv sync --extra embedding" in resp.json()["error"]


# ---------------------------------------------------------------------------
# /v1/rerank — the same proxy core, reranker kind
# ---------------------------------------------------------------------------

RERANK_RESULT = {
    "model": "ms-marco-minilm-l-6-v2",
    "results": [{"index": 1, "relevance_score": 0.97}, {"index": 0, "relevance_score": 0.11}],
    "usage": {"total_tokens": 12},
}


def _with_rerank_server(client: TestClient, node_id: str = "mini") -> None:
    from fleet_manager.models.node import TextEmbeddingMetrics, TextEmbeddingModel

    hb = make_heartbeat(node_id=node_id)
    hb.text_embedding_port = 11439
    hb.text_embedding = TextEmbeddingMetrics(
        models_available=[
            TextEmbeddingModel(name="nomic-embed-text", dimensions=768, cached=True),
            TextEmbeddingModel(
                name="ms-marco-minilm-l-6-v2",
                dimensions=0,
                cached=False,
                kind="rerank",
            ),
        ]
    )
    client.post("/heartbeat", json=hb.model_dump())


class TestRerankRoute:
    def test_forwards_to_node_rerank_and_traces(self, app_client, native_backend):
        _with_rerank_server(app_client)
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = __import__("json").loads(request.content)
            return httpx.Response(200, json=RERANK_RESULT)

        native_backend["handler"] = handler
        resp = app_client.post("/v1/rerank", json={"query": "q", "documents": ["a", "b"]})

        assert resp.status_code == 200
        assert resp.json() == RERANK_RESULT  # Jina/Cohere shape untouched: no "node" key
        assert resp.headers["X-Fleet-Node"] == "mini"
        assert seen["path"] == "/rerank"
        assert seen["body"]["model"] == "ms-marco-minilm-l-6-v2"  # default filled in
        t = _trace(app_client)
        assert t["status"] == "completed"
        assert t["tags"] == ["rerank"]
        assert t["original_format"] == "rerank"
        assert t["prompt_tokens"] == 12

    def test_node_without_a_reranker_is_not_a_candidate(self, app_client):
        """An older agent has a text server but no /rerank route — skip it."""
        _with_text_server(app_client)  # embedder only, no kind="rerank"
        resp = app_client.post("/v1/rerank", json={"query": "q", "documents": ["a"]})
        assert resp.status_code == 503
        assert "native rerank server" in resp.json()["error"]

    def test_node_validation_errors_pass_through(self, app_client, native_backend):
        _with_rerank_server(app_client)
        native_backend["handler"] = lambda r: httpx.Response(
            400,
            json={"error": "'documents' must be a non-empty list"},
        )
        resp = app_client.post("/v1/rerank", json={"query": "q", "documents": []})
        assert resp.status_code == 400
        assert resp.json()["error"] == "'documents' must be a non-empty list"
        assert _trace(app_client)["status"] == "failed"


# ---------------------------------------------------------------------------
# Node /rerank — validation, ordering, scores
# ---------------------------------------------------------------------------


@pytest.fixture
def node_rerank(monkeypatch):
    """The node's text server with a fake cross-encoder returning set logits."""
    from fastapi import FastAPI

    from fleet_manager.node import text_embedding_server as tes

    state = {"logits": [], "loaded": []}

    class FakeCrossEncoder:
        def rerank(self, query, documents, batch_size=64):
            return list(state["logits"])[: len(documents)]

    async def fake_get_reranker(model):
        state["loaded"].append(model)
        return FakeCrossEncoder()

    monkeypatch.setattr(tes, "_get_reranker", fake_get_reranker)
    app = FastAPI()
    app.include_router(tes.router)
    with TestClient(app) as c:
        yield c, state


class TestNodeRerank:
    def test_sorted_by_score_with_original_indices_and_sigmoid(self, node_rerank):
        client, state = node_rerank
        state["logits"] = [-2.0, 3.0, 0.0]
        body = client.post("/rerank", json={"query": "q", "documents": ["a", "b", "c"]}).json()
        assert [r["index"] for r in body["results"]] == [1, 2, 0]
        scores = [r["relevance_score"] for r in body["results"]]
        assert all(0.0 < s < 1.0 for s in scores)  # 0..1 like Cohere/Jina
        assert scores[1] == pytest.approx(0.5)  # sigmoid(0)
        assert "document" not in body["results"][0]
        assert body["usage"]["total_tokens"] == 6  # (1 + 1) words x 3 pairs

    def test_top_n_and_return_documents(self, node_rerank):
        client, state = node_rerank
        state["logits"] = [0.1, 0.9, 0.5]
        body = client.post(
            "/rerank",
            json={
                "query": "q",
                "documents": ["a", {"text": "b"}, "c"],
                "top_n": 2,
                "return_documents": True,
            },
        ).json()
        assert [r["index"] for r in body["results"]] == [1, 2]
        assert body["results"][0]["document"] == {"text": "b"}

    def test_default_model_when_omitted(self, node_rerank):
        client, state = node_rerank
        state["logits"] = [1.0]
        body = client.post("/rerank", json={"query": "q", "documents": ["a"]}).json()
        assert body["model"] == "ms-marco-minilm-l-6-v2"
        assert state["loaded"] == ["ms-marco-minilm-l-6-v2"]

    @pytest.mark.parametrize(
        "payload,status",
        [
            ({"documents": ["a"]}, 400),  # no query
            ({"query": "q", "documents": []}, 400),  # empty docs
            ({"query": "q", "documents": [1]}, 400),  # non-string doc
            ({"query": "q", "documents": ["a"], "top_n": 0}, 400),  # bad top_n
            ({"query": "q", "documents": ["a"] * 1001}, 400),  # too many
            ({"query": "q", "documents": ["x" * 32_001]}, 400),  # too long
            ({"query": "q", "documents": ["a"], "model": "nope"}, 404),  # unknown model
        ],
    )
    def test_rejects_bad_requests_before_loading_a_model(self, node_rerank, payload, status):
        client, state = node_rerank
        assert client.post("/rerank", json=payload).status_code == status
        assert state["loaded"] == []


# ---------------------------------------------------------------------------
# Registry, collector, heartbeat compatibility
# ---------------------------------------------------------------------------


class TestRerankRegistry:
    def test_rerankers_resolve_but_are_never_text_embedders(self):
        """The /api/embed dispatcher keys on is_text_embedding_model, so a
        reranker must never pass it — or an embed request could reach one."""
        from fleet_manager.node.text_embedding_models import (
            get_fastembed_name,
            is_model_cached,
            is_rerank_model,
            is_text_embedding_model,
        )

        assert is_rerank_model("bge-reranker-base")
        assert is_rerank_model("BGE-Reranker-Base")  # lookups lowercase
        assert not is_text_embedding_model("bge-reranker-base")
        assert get_fastembed_name("bge-reranker-base") == "BAAI/bge-reranker-base"
        assert is_model_cached("not-a-model") is False
        with pytest.raises(KeyError):
            get_fastembed_name("not-a-model")

    def test_collector_advertises_default_and_cached_rerankers_only(self, monkeypatch):
        """Not all six — six never-used dashboard cards would be noise."""
        import sys
        from unittest.mock import MagicMock

        from fleet_manager.node import collector, text_embedding_models

        monkeypatch.setitem(sys.modules, "fastembed", MagicMock())
        monkeypatch.setattr(
            text_embedding_models,
            "is_model_cached",
            lambda name: name == "bge-reranker-base",
        )
        metrics = collector._detect_text_embedding_models()
        rerank = {m.name: m for m in metrics.models_available if m.kind == "rerank"}
        assert set(rerank) == {"ms-marco-minilm-l-6-v2", "bge-reranker-base"}
        assert rerank["bge-reranker-base"].cached is True
        assert rerank["bge-reranker-base"].dimensions == 0

    def test_heartbeat_from_an_older_agent_still_validates(self):
        from fleet_manager.models.node import TextEmbeddingModel

        m = TextEmbeddingModel.model_validate(
            {"name": "nomic-embed-text", "dimensions": 768, "cached": True},
        )
        assert m.kind == "embed"


class TestIsModelCached:
    """fastembed's cache is keyed by HF source repo, which is not always the
    model name — nomic's -Q file lives in the base repo.  Keying on the name made
    nomic read uncached forever."""

    def _cache(self, tmp_path, monkeypatch, *repos):
        from fleet_manager.node import text_embedding_models

        for repo in repos:
            d = tmp_path / f"models--{repo.replace('/', '--')}" / "blobs"
            d.mkdir(parents=True)
            (d / "weights").write_bytes(b"x")
        monkeypatch.setattr(text_embedding_models, "TEXT_EMBEDDING_CACHE_DIR", tmp_path)

    def test_nomic_is_found_under_its_source_repo(self, tmp_path, monkeypatch):
        from fleet_manager.node.text_embedding_models import is_model_cached

        self._cache(tmp_path, monkeypatch, "nomic-ai/nomic-embed-text-v1.5")
        assert is_model_cached("nomic-embed-text")
        assert is_model_cached("nomic-embed-text:latest")

    def test_reranker_is_found_under_its_name(self, tmp_path, monkeypatch):
        from fleet_manager.node.text_embedding_models import is_model_cached

        self._cache(tmp_path, monkeypatch, "BAAI/bge-reranker-base")
        assert is_model_cached("bge-reranker-base")
        assert not is_model_cached("ms-marco-minilm-l-6-v2")

    def test_empty_directory_is_not_cached(self, tmp_path, monkeypatch):
        from fleet_manager.node import text_embedding_models

        (tmp_path / "models--BAAI--bge-reranker-base").mkdir()
        monkeypatch.setattr(text_embedding_models, "TEXT_EMBEDDING_CACHE_DIR", tmp_path)
        assert not text_embedding_models.is_model_cached("bge-reranker-base")
