"""A node's backend answers 404 for a model the node advertises.

docs/plans/backend-model-not-found.md, steps 0 and 1.  Community telemetry
showed one install fail 1,440 requests in a row this way.  Reproduced:

* the 404 was terminal, so a healthy peer holding the model was never tried;
* the Ollama-native surface answered it with a plain-text 500 (non-streaming)
  or a dropped connection (streaming), so the client never saw "not found";
* every other surface's stream error carried httpx's text, backend URL included.

Also here: native non-streaming used to fabricate ``"done": true`` over a
truncated answer.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.common.errors import (
    UPSTREAM_INCOMPLETE_MESSAGE,
    backend_error_detail,
    categorize_error,
    client_error_message,
    describe_exception,
)
from fleet_manager.server.streaming import StreamingProxy
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama
from tests.test_server.test_routes import create_test_app

MODEL = "qwen2.5-coder:14b"
# Ollama's two "not found" bodies (server/routes.go): no manifest, and a
# manifest whose blobs are gone.
NO_MANIFEST = f"model '{MODEL}' not found"
NO_BLOB = f'model "{MODEL}" not found, try pulling it first'

MESSAGES = [{"role": "user", "content": "hi"}]
COMPLETE = [
    {"model": MODEL, "message": {"role": "assistant", "content": "Served"}, "done": False},
    {
        "model": MODEL,
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 5,
        "eval_count": 1,
    },
]
TRUNCATED = COMPLETE[:1]
REQUESTS = {
    "ollama": ("/api/chat", {"model": MODEL, "messages": MESSAGES}),
    "openai": ("/v1/chat/completions", {"model": MODEL, "messages": MESSAGES}),
    "anthropic": ("/v1/messages", {"model": MODEL, "max_tokens": 64, "messages": MESSAGES}),
    "responses": ("/v1/responses", {"model": MODEL, "input": "hi"}),
}
SURFACES = list(REQUESTS)


def _status_error(status: int, body: str | dict | None):
    """The exception httpx really raises, from a read response, as
    ``stream_from_node`` produces it (it ``aread()``s before raising)."""
    request = httpx.Request("POST", "http://10.0.0.5:11434/api/chat")
    if body is None:
        response = httpx.Response(status, request=request)
    elif isinstance(body, dict):
        response = httpx.Response(status, json=body, request=request)
    else:
        response = httpx.Response(status, text=body, request=request)
    response.read()
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError(f"{status} did not raise")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class TestBackendErrorDetail:
    def test_ollama_shape(self):
        assert backend_error_detail(_status_error(404, {"error": NO_MANIFEST})) == NO_MANIFEST

    def test_openai_shape(self):
        exc = _status_error(400, {"error": {"message": "bad tool", "type": "x"}})
        assert backend_error_detail(exc) == "bad tool"

    def test_plain_text_body(self):
        assert backend_error_detail(_status_error(502, "upstream exploded")) == "upstream exploded"

    def test_json_without_a_message_is_none(self):
        assert backend_error_detail(_status_error(400, {"foo": 1})) is None

    def test_empty_body_is_none(self):
        assert backend_error_detail(_status_error(500, None)) is None

    def test_not_an_http_error_is_none(self):
        assert backend_error_detail(httpx.ConnectError("refused")) is None
        assert backend_error_detail(None) is None


class TestClientErrorMessage:
    def test_prefers_the_backend_message(self):
        assert client_error_message(_status_error(404, {"error": NO_MANIFEST})) == NO_MANIFEST

    def test_never_carries_the_backend_url(self):
        exc = _status_error(500, None)
        assert "10.0.0.5" in describe_exception(exc)  # what clients used to get
        assert client_error_message(exc) == "backend returned HTTP 500"

    def test_transport_errors_keep_their_text(self):
        assert client_error_message(httpx.ConnectError("refused")) == "refused"
        assert client_error_message(httpx.ReadTimeout("")) == "ReadTimeout"

    def test_traces_keep_their_telemetry_category(self):
        """Traces stay on describe_exception: the published histogram reads them."""
        exc = _status_error(404, {"error": NO_MANIFEST})
        assert categorize_error(describe_exception(exc)) == "model_not_found"


# ---------------------------------------------------------------------------
# Which 404s are retried
# ---------------------------------------------------------------------------


class TestModelNotFoundIsRetryable:
    @pytest.mark.parametrize("message", [NO_MANIFEST, NO_BLOB])
    def test_ollamas_not_found_bodies(self, message):
        exc = _status_error(404, {"error": message})
        assert StreamingProxy._is_model_not_found_error(exc)
        assert StreamingProxy._is_retryable_error(exc)

    def test_a_404_for_anything_else_stays_terminal(self):
        exc = _status_error(404, "404 page not found")  # wrong endpoint
        assert not StreamingProxy._is_model_not_found_error(exc)
        assert not StreamingProxy._is_retryable_error(exc)

    def test_an_unreadable_404_stays_terminal(self):
        assert not StreamingProxy._is_retryable_error(_status_error(404, None))

    def test_other_4xx_mentioning_a_model_stays_terminal(self):
        exc = _status_error(400, {"error": f"{MODEL} does not support tools"})
        assert not StreamingProxy._is_retryable_error(exc)

    def test_only_transport_errors_are_transport(self):
        """The pooled client is dropped on a transport error only: an HTTP
        answer proves the connection works."""
        assert StreamingProxy._is_transport_error(httpx.ConnectError("refused"))
        assert not StreamingProxy._is_transport_error(_status_error(404, {"error": NO_MANIFEST}))
        assert not StreamingProxy._is_transport_error(_status_error(500, None))


# ---------------------------------------------------------------------------
# End to end, every surface
# ---------------------------------------------------------------------------


def _handler(lines: list[dict] | None, *, status: int = 200, error: str = ""):
    body = "".join(json.dumps(x) + "\n" for x in (lines or [])).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path not in ("/api/chat", "/api/generate"):
            return httpx.Response(200, json={})
        if status != 200:
            return httpx.Response(status, json={"error": error})
        return httpx.Response(200, content=body, headers={"content-type": "application/x-ndjson"})

    return handler


def _post(client: TestClient, surface: str, *, stream: bool):
    path, body = REQUESTS[surface]
    return client.post(path, json={**body, "stream": stream})


def _spy_traces(client: TestClient) -> MagicMock:
    proxy = client.app.state.streaming_proxy
    proxy._record_trace = MagicMock(wraps=proxy._record_trace)
    return proxy._record_trace


def _traces(spy: MagicMock) -> list[tuple[str, str]]:
    """(node_id, status) per trace: _record_trace(entry, node_id, start, ttft, status)."""
    return [(c.args[1], c.args[4]) for c in spy.call_args_list]


@pytest.fixture
def one_node(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        hb = make_heartbeat(
            node_id="broken", loaded_models=[(MODEL, 9.0)], available_models=[MODEL]
        )
        client.post("/heartbeat", json=hb.model_dump())
        _install_mock_ollama(client, "broken", _handler(None, status=404, error=NO_MANIFEST))
        yield client


@pytest.fixture
def two_nodes(tmp_path):
    """`broken` is favoured (model hot, more memory) and 404s; `good` has it on disk."""
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        broken = make_heartbeat(
            node_id="broken",
            memory_total=512.0,
            loaded_models=[(MODEL, 9.0)],
            available_models=[MODEL],
            lan_ip="10.0.0.1",
        )
        good = make_heartbeat(node_id="good", available_models=[MODEL], lan_ip="10.0.0.2")
        client.post("/heartbeat", json=broken.model_dump())
        client.post("/heartbeat", json=good.model_dump())
        _install_mock_ollama(client, "broken", _handler(None, status=404, error=NO_MANIFEST))
        _install_mock_ollama(client, "good", _handler(COMPLETE))
        yield client


class TestRetriedOnAPeer:
    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize("surface", SURFACES)
    def test_served_by_the_node_that_has_it(self, two_nodes, surface, stream):
        spy = _spy_traces(two_nodes)
        resp = _post(two_nodes, surface, stream=stream)
        assert resp.status_code == 200, resp.text
        assert "Served" in resp.text
        traces = _traces(spy)
        assert ("broken", "retried") in traces
        assert not any(status == "failed" for _, status in traces)


class TestSingleNodeAnswersInProtocol:
    """No peer to retry on: the client must be told what the backend said."""

    @pytest.mark.parametrize("surface", SURFACES)
    def test_non_streaming_is_the_backends_404(self, one_node, surface):
        resp = _post(one_node, surface, stream=False)
        assert resp.status_code == 404, resp.text
        assert NO_MANIFEST in resp.text
        if surface == "ollama":
            assert resp.json() == {"error": NO_MANIFEST}

    @pytest.mark.parametrize("surface", SURFACES)
    def test_streaming_ends_with_the_backends_message_and_no_url(self, one_node, surface):
        resp = _post(one_node, surface, stream=True)
        assert NO_MANIFEST in resp.text
        assert "http://" not in resp.text
        if surface == "ollama":
            last = json.loads(resp.text.strip().splitlines()[-1])
            assert last == {"error": NO_MANIFEST}

    def test_native_generate_too(self, one_node):
        resp = one_node.post(
            "/api/generate", json={"model": MODEL, "prompt": "hi", "stream": False}
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": NO_MANIFEST}

    def test_a_backend_5xx_is_a_502_without_the_url(self, one_node):
        _install_mock_ollama(one_node, "broken", _handler(None, status=500, error=""))
        resp = _post(one_node, "ollama", stream=False)
        assert resp.status_code == 502
        assert "http://" not in resp.text


class TestNativeNonStreamingTruncation:
    def test_a_truncated_answer_is_a_502_not_a_fabricated_done(self, one_node):
        _install_mock_ollama(one_node, "broken", _handler(TRUNCATED))
        resp = _post(one_node, "ollama", stream=False)
        assert resp.status_code == 502
        assert resp.json() == {"error": UPSTREAM_INCOMPLETE_MESSAGE}

    def test_a_complete_answer_is_unchanged(self, one_node):
        _install_mock_ollama(one_node, "broken", _handler(COMPLETE))
        resp = _post(one_node, "ollama", stream=False)
        assert resp.status_code == 200
        assert resp.json()["done"] is True
        assert resp.json()["message"]["content"] == "Served"


# ---------------------------------------------------------------------------
# Step 2: a loopback heartbeat is local only if the node's lan_ip is ours
# ---------------------------------------------------------------------------

ROUTER_IP = "10.0.0.92"
REMOTE_IP = "10.0.0.50"


def _url(monkeypatch, *, request_ip, lan_ip, ollama_host, router_ip=ROUTER_IP):
    from fleet_manager.models.config import ServerSettings
    from fleet_manager.server.registry import NodeRegistry

    monkeypatch.setattr("fleet_manager.server.registry.get_local_ip", lambda **_: router_ip)
    hb = make_heartbeat(node_id="n", lan_ip=lan_ip, ollama_host=ollama_host)
    return NodeRegistry(ServerSettings())._build_ollama_url(hb, request_ip=request_ip)


class TestLoopbackHeartbeats:
    @pytest.mark.parametrize("loopback", ["127.0.0.1", "::1", ""])
    def test_a_remote_node_forwarded_over_loopback_keeps_its_own_ollama(
        self, monkeypatch, loopback
    ):
        """The bug: an SSH tunnel or nginx's default proxy_pass delivers a remote
        node's heartbeat from loopback, and it was sent to *this* machine's
        Ollama (reproduced: 3 of 3 requests 404'd).  The collector has already
        rewritten its ollama_host to its LAN address, which is honoured."""
        url = _url(
            monkeypatch,
            request_ip=loopback,
            lan_ip=REMOTE_IP,
            ollama_host=f"http://{REMOTE_IP}:11434",
        )
        assert url == f"http://{REMOTE_IP}:11434"

    def test_a_remote_node_with_a_loopback_ollama_host_falls_back_to_its_lan_ip(self, monkeypatch):
        url = _url(
            monkeypatch,
            request_ip="127.0.0.1",
            lan_ip=REMOTE_IP,
            ollama_host="http://localhost:11500",
        )
        assert url == f"http://{REMOTE_IP}:11500"

    @pytest.mark.parametrize(
        "ollama_host, expected",
        [
            # what the collector sends for a default local Ollama (rewritten)
            (f"http://{ROUTER_IP}:11434", "http://localhost:11434"),
            ("http://localhost:11500", "http://localhost:11500"),
            # a node on this box deliberately driving another machine's Ollama
            (f"http://{REMOTE_IP}:11434", f"http://{REMOTE_IP}:11434"),
        ],
    )
    def test_a_node_on_this_machine(self, monkeypatch, ollama_host, expected):
        url = _url(monkeypatch, request_ip="127.0.0.1", lan_ip=ROUTER_IP, ollama_host=ollama_host)
        assert url == expected

    @pytest.mark.parametrize("lan_ip", ["", "127.0.0.1"])
    def test_a_node_with_no_network_can_only_be_local(self, monkeypatch, lan_ip):
        url = _url(
            monkeypatch, request_ip="127.0.0.1", lan_ip=lan_ip, ollama_host="http://localhost:11434"
        )
        assert url == "http://localhost:11434"

    def test_a_router_booted_before_the_network_still_reaches_a_local_node(self, monkeypatch):
        """get_local_ip() falls back to 127.0.0.1 with no network (the 0.12.3
        case).  The co-located node is then not recognised as local, and gets its
        own LAN address instead -- which is this machine, so still reachable."""
        url = _url(
            monkeypatch,
            request_ip="127.0.0.1",
            lan_ip=ROUTER_IP,
            ollama_host=f"http://{ROUTER_IP}:11434",
            router_ip="127.0.0.1",
        )
        assert url == f"http://{ROUTER_IP}:11434"

    def test_a_heartbeat_over_the_lan_is_unchanged(self, monkeypatch):
        url = _url(
            monkeypatch,
            request_ip=REMOTE_IP,
            lan_ip=REMOTE_IP,
            ollama_host=f"http://{REMOTE_IP}:11434",
        )
        assert url == f"http://{REMOTE_IP}:11434"
