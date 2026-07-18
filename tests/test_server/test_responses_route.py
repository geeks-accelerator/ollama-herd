"""Route-level tests for /v1/responses (Codex endpoint).

Covers the guards that run *before* the inference pipeline, so no live Ollama
is needed: bad input, unsupported stateful chaining, nothing-to-route-to, and
the MLX-not-served case.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.routes import responses_compat
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.server.streaming import StreamingProxy


def _app(model_map: dict | None = None) -> FastAPI:
    settings = ServerSettings()
    if model_map is not None:
        settings.anthropic_model_map = model_map

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = NodeRegistry(settings)
        queue_mgr = QueueManager()
        proxy = StreamingProxy(registry)
        app.state.settings = settings
        app.state.registry = registry
        app.state.scorer = ScoringEngine(settings, registry)
        app.state.queue_mgr = queue_mgr
        app.state.streaming_proxy = proxy
        app.state.mlx_proxy = None
        yield
        await queue_mgr.shutdown()
        await proxy.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(responses_compat.router)
    return app


@pytest.fixture
def client():
    with TestClient(_app()) as c:
        yield c


def test_invalid_json_is_400(client):
    r = client.post("/v1/responses", content=b"{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_non_object_body_is_400(client):
    r = client.post("/v1/responses", json=["nope"])
    assert r.status_code == 400


def test_previous_response_id_is_rejected_clearly(client):
    """We never persist responses, so stateful chaining cannot be honoured.
    Fail loudly rather than silently answering with partial context."""
    r = client.post("/v1/responses", json={
        "model": "gpt-5-codex", "input": "hi", "previous_response_id": "resp_abc",
    })
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "previous_response_id" in msg
    assert "stateless" in msg.lower()


def test_empty_fleet_returns_404_with_pull_hint(client):
    """No nodes → nothing to auto-route to. The error must tell the user what
    to do (pull a model), matching the endpoint's zero-config promise."""
    r = client.post("/v1/responses", json={"model": "gpt-5-codex", "input": "hi"})
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["type"] == "not_found_error"
    assert "ollama pull" in body["message"]


def test_explicit_mlx_mapping_gets_a_precise_503():
    """Auto-routing filters mlx: out, so this only fires on an explicit map —
    and must say so rather than failing confusingly downstream."""
    app = _app(model_map={"gpt-5-codex": "mlx:some/Model-4bit"})
    with TestClient(app) as c:
        r = c.post("/v1/responses", json={"model": "gpt-5-codex", "input": "hi"})
    assert r.status_code == 503
    assert "MLX" in r.json()["error"]["message"]


def test_input_that_yields_no_messages_is_400(client):
    """An input array of only non-message items (e.g. reasoning) has nothing to
    send — better a clean 400 than an empty prompt to the model."""
    app = _app(model_map={"gpt-5-codex": "qwen3-coder:30b"})
    with TestClient(app) as c:
        r = c.post("/v1/responses", json={
            "model": "gpt-5-codex", "input": [{"type": "reasoning", "summary": []}],
        })
    assert r.status_code == 400
    assert "no messages" in r.json()["error"]["message"].lower()
