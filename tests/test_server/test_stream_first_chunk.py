"""A routed response is answered from what actually served it.

Found by the fake-fleet smoke test (2026-11-20): every route built its
``X-Fleet-*`` headers from the first-choice node before the stream was read,
while the proxy retries on other nodes *while producing the first chunk*.  So

* ``X-Fleet-Node`` / ``X-Fleet-Retries`` / ``X-Fleet-Score`` named the node that
  failed, with 0 retries, when another node served the request after 1-2
  retries;
* a streaming request that failed on every node (or hit Ollama's queue-full
  503) was answered **200** with an error line, so the status never said so;
* Ollama's queue-full reached OpenAI's non-streaming clients as a 502, although
  ``_is_queue_full_error`` documents a 429 + Retry-After.

``dispatched_stream`` now waits for the first chunk; routes build headers from
the queue entry after it, and answer a failure before it with a real status.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.models.request import InferenceRequest, QueueEntry
from fleet_manager.server.routes.routing import dispatched_stream
from tests.conftest import make_heartbeat
from tests.test_server.test_backend_model_not_found import (
    COMPLETE,
    MODEL,
    REQUESTS,
    SURFACES,
    _handler,
    _post,
    _spy_traces,
)
from tests.test_server.test_client_compat import _install_mock_ollama
from tests.test_server.test_routes import create_test_app
from tests.test_server.test_upstream_incomplete import MESSAGES as MLX_MESSAGES
from tests.test_server.test_upstream_incomplete import (
    MLX_MODEL,
    _FakeMlxProxy,
    fleet,  # noqa: F401 -- fixture mlx_fleet depends on
    mlx_fleet,  # noqa: F401 -- fixture
)

# Ollama's own queue-full text (server/sched.go), as scripts/mock_ollama.py sends it.
QUEUE_FULL = "server busy, please try again.  maximum pending requests exceeded"


@pytest.fixture
def two_nodes(tmp_path):
    """``first`` is favoured (model hot, more memory) and fails; ``second`` serves."""
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        first = make_heartbeat(
            node_id="first",
            memory_total=512.0,
            loaded_models=[(MODEL, 9.0)],
            available_models=[MODEL],
            lan_ip="10.0.0.1",
        )
        second = make_heartbeat(node_id="second", available_models=[MODEL], lan_ip="10.0.0.2")
        client.post("/heartbeat", json=first.model_dump())
        client.post("/heartbeat", json=second.model_dump())
        _install_mock_ollama(client, "first", _handler(None, status=500, error="runner died"))
        _install_mock_ollama(client, "second", _handler(COMPLETE))
        yield client


@pytest.fixture
def one_node(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        hb = make_heartbeat(node_id="only", loaded_models=[(MODEL, 9.0)], available_models=[MODEL])
        client.post("/heartbeat", json=hb.model_dump())
        yield client


def _error_message(surface: str, body: dict) -> str:
    """The message out of each surface's own error envelope."""
    if surface == "ollama":
        assert set(body) == {"error"}
        return body["error"]
    if surface == "anthropic":
        assert body["type"] == "error"
    return body["error"]["message"]


class TestHeadersNameTheNodeThatServed:
    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_after_a_retry(self, two_nodes, surface, stream):
        spy = _spy_traces(two_nodes)
        resp = _post(two_nodes, surface, stream=stream)
        assert resp.status_code == 200, resp.text
        assert "Served" in resp.text
        served = [c.args[0] for c in spy.call_args_list if c.args[4] == "completed"]
        assert len(served) == 1
        entry = served[0]
        assert resp.headers["X-Fleet-Node"] == "second" == entry.assigned_node
        assert resp.headers["X-Fleet-Retries"] == "1" == str(entry.retry_count)
        # The score of the node that served, which the retry put on the entry.
        assert resp.headers["X-Fleet-Score"] == str(int(entry.routing_score))


class TestFailureBeforeTheFirstChunkIsAStatus:
    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_every_attempt_failed_is_a_502(self, one_node, surface, stream):
        _install_mock_ollama(one_node, "only", _handler(None, status=500, error="runner died"))
        resp = _post(one_node, surface, stream=stream)
        assert resp.status_code == 502, resp.text
        assert _error_message(surface, resp.json()) == "runner died"
        assert "http://" not in resp.text
        assert resp.headers["X-Fleet-Node"] == "only"
        assert resp.headers["X-Fleet-Retries"] == "0"

    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_queue_full_is_a_429_with_retry_after(self, one_node, surface, stream):
        _install_mock_ollama(one_node, "only", _handler(None, status=503, error=QUEUE_FULL))
        resp = _post(one_node, surface, stream=stream)
        assert resp.status_code == 429, resp.text
        assert int(resp.headers["Retry-After"]) > 0
        assert _error_message(surface, resp.json()) == QUEUE_FULL

    @pytest.mark.parametrize("surface", SURFACES)
    def test_a_backend_4xx_keeps_its_status_when_streaming(self, one_node, surface):
        _install_mock_ollama(one_node, "only", _handler(None, status=400, error="bad tools"))
        resp = _post(one_node, surface, stream=True)
        assert resp.status_code == 400, resp.text
        assert "bad tools" in _error_message(surface, resp.json())


class TestFailureAfterTheFirstChunkStaysInStream:
    """Headers are sent by then: the stream ends with the protocol's error."""

    @pytest.mark.parametrize("surface", SURFACES)
    def test_mid_stream_drop(self, one_node, surface):
        first_line = json.dumps(COMPLETE[0]) + "\n"

        def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                yield first_line.encode()
                raise httpx.ReadError("connection reset")

            return httpx.Response(200, content=body())

        _install_mock_ollama(one_node, "only", handler)
        path, payload = REQUESTS[surface]
        resp = one_node.post(path, json={**payload, "stream": True})
        assert resp.status_code == 200
        assert "Served" in resp.text
        assert "error" in resp.text


class TestClientLeavesBeforeTheFirstChunk:
    @pytest.mark.asyncio
    async def test_the_wait_is_cancelled_and_nothing_returned(self):
        """A client that gives up during a cold load must not keep the request
        running for nobody: the wait is cancelled (the proxy traces that as a
        disconnect) and the route answers 499."""
        cancelled = asyncio.Event()

        async def slow_stream():
            try:
                await asyncio.Event().wait()  # the model is still loading
                yield "never"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        future = asyncio.get_running_loop().create_future()
        future.set_result(slow_stream())
        request = SimpleNamespace(is_disconnected=MagicMock(side_effect=_disconnect_after(2)))
        entry = QueueEntry(request=InferenceRequest(model="m"), assigned_node="n")

        assert await dispatched_stream(request, future, entry, None, poll_s=0.01) is None
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_an_empty_stream_is_returned_empty(self):
        async def empty():
            return
            yield

        future = asyncio.get_running_loop().create_future()
        future.set_result(empty())
        request = SimpleNamespace(is_disconnected=MagicMock(side_effect=_disconnect_after(99)))
        entry = QueueEntry(request=InferenceRequest(model="m"), assigned_node="n")
        stream = await dispatched_stream(request, future, entry, None, poll_s=0.01)
        assert [c async for c in stream] == []


def _disconnect_after(n: int):
    calls = 0

    async def is_disconnected():
        nonlocal calls
        calls += 1
        return calls >= n

    return is_disconnected


class TestMlxStreamFailingBeforeTheFirstChunk:
    """The MLX passthroughs have no retries, so their headers were right; their
    status was not: a stream that failed before any output was a 200."""

    @pytest.mark.parametrize("surface", ["openai", "anthropic"])
    def test_is_a_502_and_frees_the_slot(self, mlx_fleet, surface, monkeypatch):  # noqa: F811
        from tests.test_server.test_backend_model_not_found import _status_error

        traces = []
        monkeypatch.setattr(
            "fleet_manager.server.mlx_proxy.record_trace_mlx",
            lambda _s, _r, _t0, _ttft, status, error_message=None: traces.append(status),
        )

        class Failing(_FakeMlxProxy):
            released = 0

            def _release_slot(self, _key):
                Failing.released += 1

            async def stream_openai(self, _req, already_admitted=False):
                raise _status_error(500, {"error": "mlx died"})
                yield

        mlx_fleet.app.state.mlx_proxy = Failing([])
        if surface == "openai":
            resp = mlx_fleet.post(
                "/v1/chat/completions",
                json={"model": MLX_MODEL, "messages": MLX_MESSAGES, "stream": True},
            )
        else:
            resp = mlx_fleet.post(
                "/v1/messages",
                json={
                    "model": "claude-test",
                    "max_tokens": 64,
                    "messages": MLX_MESSAGES,
                    "stream": True,
                },
            )
        assert resp.status_code == 502, resp.text
        assert _error_message(surface, resp.json()) == "mlx died"
        assert Failing.released == 1
        assert traces == ["failed"]
