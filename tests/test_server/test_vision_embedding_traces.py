"""Vision embedding requests leave a trace for every outcome.

``embedding_compat`` recorded nothing at all: vision traffic, latency and
failures never reached ``request_traces``, so the dashboard and every health
check were blind to them.  These pin the trace each outcome leaves — success,
backend error, transport error, and the three answers given before any node is
chosen — through both entry points (``/api/embed-image`` and ``/api/embed``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.models.node import VisionEmbeddingMetrics, VisionEmbeddingModel
from tests.conftest import make_heartbeat
from tests.test_server.test_routes import create_test_app

IMAGE = "aGVsbG8="  # the router never decodes it; only the node does


@pytest.fixture
def app_client(tmp_path):
    from fleet_manager.server.routes import embedding_compat

    app = create_test_app(tmp_path=tmp_path)
    # The shared test app mounts no vision router; /api/embed reaches the
    # handler through dispatch, /api/embed-image needs the route itself.
    app.include_router(embedding_compat.router)
    with TestClient(app) as c:
        c.app.state.trace_store.record_trace = AsyncMock()
        yield c


def _with_vision_server(client: TestClient, node_id: str = "mini") -> None:
    hb = make_heartbeat(node_id=node_id)
    hb.vision_embedding_port = 11438
    hb.vision_embedding = VisionEmbeddingMetrics(
        models_available=[
            VisionEmbeddingModel(name="dinov2-vit-s14", runtime="onnx", dimensions=384)
        ]
    )
    client.post("/heartbeat", json=hb.model_dump())


@pytest.fixture
def vision_backend(monkeypatch):
    """Route ``embed_image_on_node``'s outbound httpx client to a test handler."""
    from fleet_manager.server import streaming

    state = {"handler": None}
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda r: state["handler"](r))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(streaming.httpx, "AsyncClient", patched)
    return state


def _trace(client: TestClient) -> dict:
    return client.app.state.trace_store.record_trace.call_args.kwargs


def _embed(client: TestClient, **body):
    return client.post("/api/embed-image", json={"images": [IMAGE], **body})


class TestVisionEmbeddingTraces:
    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/embed-image", {"model": "dinov2", "images": [IMAGE]}),
            ("/api/embed", {"model": "dinov2", "input": [IMAGE]}),
        ],
    )
    def test_success_is_traced_on_the_node_under_the_resolved_model(
        self, app_client, vision_backend, path, body
    ):
        _with_vision_server(app_client)
        vision_backend["handler"] = lambda r: httpx.Response(
            200, json={"model": "dinov2-vit-s14", "embeddings": [[0.1]], "dimensions": 384}
        )
        resp = app_client.post(path, json=body)

        assert resp.status_code == 200
        t = _trace(app_client)
        assert t["status"] == "completed"
        assert t["node_id"] == "mini"
        assert t["model"] == "dinov2-vit-s14"  # alias resolved, not "dinov2"
        assert t["original_format"] == "embed"
        assert t["tags"] == ["embed", "vision-embed"]
        assert t["latency_ms"] >= 0
        # Images have no tokens.  A count here would put vision models in front
        # of num_ctx_unmanaged / context_waste, which have nothing to manage.
        assert t["prompt_tokens"] is None

    def test_auto_selected_model_is_traced_by_name(self, app_client, vision_backend):
        _with_vision_server(app_client)
        vision_backend["handler"] = lambda r: httpx.Response(
            200, json={"embeddings": [[0.1]], "dimensions": 384}
        )
        assert _embed(app_client).status_code == 200
        assert _trace(app_client)["model"] == "dinov2-vit-s14"

    def test_backend_error_passes_through_and_is_a_failure_on_the_node(
        self, app_client, vision_backend
    ):
        _with_vision_server(app_client)
        vision_backend["handler"] = lambda r: httpx.Response(
            400, json={"error": "Failed to decode image 0: bad"}
        )
        resp = _embed(app_client, model="dinov2-vit-s14")

        assert resp.status_code == 400
        assert resp.json() == {"error": "Failed to decode image 0: bad", "node": "mini"}
        t = _trace(app_client)
        assert (t["status"], t["node_id"]) == ("failed", "mini")
        assert t["error_message"].startswith("HTTP 400")

    def test_transport_timeout_is_502_and_records_a_cause(self, app_client, vision_backend):
        """httpx timeouts stringify to "" — the trace must still say what happened."""
        _with_vision_server(app_client)

        def timeout(request):
            raise httpx.ReadTimeout("", request=request)

        vision_backend["handler"] = timeout
        resp = _embed(app_client, model="dinov2-vit-s14")

        assert resp.status_code == 502
        t = _trace(app_client)
        assert (t["status"], t["node_id"]) == ("failed", "mini")
        assert t["error_message"]
        assert t["error_message"] in resp.json()["error"]

    def test_no_vision_node_is_a_rejected_404(self, app_client):
        app_client.post("/heartbeat", json=make_heartbeat(node_id="mini").model_dump())
        resp = _embed(app_client, model="dinov2")

        assert resp.status_code == 404
        t = _trace(app_client)
        assert (t["status"], t["node_id"]) == ("rejected", "")
        assert t["error_message"] == resp.json()["error"]
        assert t["model"] == "dinov2-vit-s14"
        assert t["tags"] == ["embed", "vision-embed"]

    def test_disabled_is_a_rejected_503(self, app_client):
        _with_vision_server(app_client)
        app_client.app.state.settings.vision_embedding = False
        resp = _embed(app_client)

        assert resp.status_code == 503
        t = _trace(app_client)
        assert (t["status"], t["node_id"]) == ("rejected", "")
        assert t["error_message"] == resp.json()["error"]

    def test_missing_images_is_a_rejected_400(self, app_client):
        _with_vision_server(app_client)
        resp = app_client.post("/api/embed-image", json={"model": "dinov2"})

        assert resp.status_code == 400
        assert _trace(app_client)["status"] == "rejected"

    def test_every_rejection_is_logged(self, app_client, caplog):
        import logging

        app_client.post("/heartbeat", json=make_heartbeat(node_id="mini").model_dump())
        with caplog.at_level(logging.WARNING):
            _embed(app_client, model="dinov2")
        assert any("rejected (404)" in r.getMessage() for r in caplog.records)


class TestVisionTracesStayOutOfContextChecks:
    """The traces must not make the num_ctx checks treat vision models as Ollama's."""

    async def test_prompt_token_stats_never_see_a_vision_trace(self, tmp_path):
        from fleet_manager.server.trace_store import TraceStore

        store = TraceStore(str(tmp_path / "t.db"))
        await store.initialize()
        try:
            # What the vision path writes on success, beside a text embed.
            await store.record_trace(
                request_id="v", model="dinov2-vit-s14", original_model="dinov2-vit-s14",
                node_id="mini", status="completed", latency_ms=40.0,
                original_format="embed", tags=["embed", "vision-embed"],
            )
            await store.record_trace(
                request_id="t", model="nomic-embed-text", original_model="nomic-embed-text",
                node_id="mini", status="completed", latency_ms=40.0, prompt_tokens=7,
                original_format="embed", tags=["embed", "text-embed"],
            )
            models = {s["model"] for s in await store.get_prompt_token_stats(days=1)}
            assert "dinov2-vit-s14" not in models
            assert "nomic-embed-text" in models  # the query itself still works
        finally:
            await store.close()
