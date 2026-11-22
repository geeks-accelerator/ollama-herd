"""A backend stream that ends with nothing at all is retried on another node.

Run 6: a 24 GB Mac swapping with three models resident answered request after
request with an empty 200 stream in 0.0 s.  Nothing had reached the client, but
herd recorded each as incomplete on that node instead of trying the other Mac,
which was idle.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama, create_test_app

MODEL = "qwen2.5:7b"
DONE = "\n".join(
    json.dumps(x)
    for x in (
        {"model": MODEL, "message": {"role": "assistant", "content": "OK"}, "done": False},
        {"model": MODEL, "done": True, "done_reason": "stop", "prompt_eval_count": 5,
         "eval_count": 1},
    )
) + "\n"  # fmt: skip


@pytest.fixture
def client(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as c:
        for node, ip in (("s1", "10.0.0.1"), ("s2", "10.0.0.2")):
            c.post(
                "/heartbeat",
                json=make_heartbeat(
                    node_id=node, lan_ip=ip, loaded_models=[(MODEL, 5.0)], available_models=[MODEL]
                ).model_dump(),
            )
        yield c


def _chat(client: TestClient):
    return client.post(
        "/api/chat",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )


def test_an_empty_stream_is_retried_on_the_other_node(client):
    seen = []

    def empty(request):
        seen.append("empty")
        return httpx.Response(200, content=b"")

    def answers(request):
        seen.append("ok")
        return httpx.Response(200, content=DONE.encode())

    _install_mock_ollama(client, "s1", empty)
    _install_mock_ollama(client, "s2", answers)
    # Whichever node the scorer tries first, an empty stream must not end the request.
    resp = _chat(client)
    assert resp.status_code == 200
    lines = [json.loads(x) for x in resp.text.splitlines() if x.strip()]
    assert lines[-1]["done"] is True
    assert "ok" in seen


def test_every_node_empty_fails_the_request_with_the_reason(client, caplog):
    for node in ("s1", "s2"):
        _install_mock_ollama(client, node, lambda r: httpx.Response(200, content=b""))
    resp = _chat(client)
    assert not any(json.loads(x).get("done") for x in resp.text.splitlines() if x.strip())
    tried = [
        r.getMessage()
        for r in caplog.records
        if "ended the stream with no output" in r.getMessage()
    ]
    assert {"s1", "s2"} <= {n for m in tried for n in ("s1", "s2") if f"Node {n} failed" in m}


RUNNER_DIED = "llama runner process has terminated: exit status 2"
TOO_LONG = "input length (40000 tokens) exceeds the model's maximum context length (32768)"


def _error_only(message, seen=None, tag=""):
    def handler(request):
        if seen is not None:
            seen.append(tag)
        return httpx.Response(200, content=(json.dumps({"error": message}) + "\n").encode())

    return handler


def test_a_stream_with_only_the_backends_error_is_retried(client):
    """A runner that dies in prefill after Ollama sent 200: the stream holds one
    error line.  It produced nothing, so the request goes to the other node
    instead of starting a 200 that ends in an error."""
    seen = []
    _install_mock_ollama(client, "s1", _error_only(RUNNER_DIED, seen, "s1"))

    def answers(request):
        seen.append("ok")
        return httpx.Response(200, content=DONE.encode())

    _install_mock_ollama(client, "s2", answers)
    resp = _chat(client)
    assert resp.status_code == 200
    assert json.loads(resp.text.splitlines()[-1])["done"] is True
    assert "ok" in seen


def test_a_request_error_is_answered_with_its_status_and_not_retried(client):
    """Context too long is about the request: every peer would refuse it, and
    each refusal would mark that peer failing.  One node is tried, and the
    client gets a 400 with the backend's reason instead of a 200."""
    seen = []
    _install_mock_ollama(client, "s1", _error_only(TOO_LONG, seen, "s1"))
    _install_mock_ollama(client, "s2", _error_only(TOO_LONG, seen, "s2"))
    resp = _chat(client)
    assert resp.status_code == 400
    assert "context length" in resp.text
    assert len(seen) == 1


def test_an_error_after_output_still_reaches_the_client_in_the_stream(client):
    """Once something was sent the response has started: the error line is
    passed through as before."""
    partial = (
        json.dumps(
            {"model": MODEL, "message": {"role": "assistant", "content": "Hel"}, "done": False}
        )
        + "\n"
        + json.dumps({"error": RUNNER_DIED})
        + "\n"
    )
    for node in ("s1", "s2"):
        _install_mock_ollama(client, node, lambda r: httpx.Response(200, content=partial.encode()))
    resp = _chat(client)
    assert resp.status_code == 200
    assert RUNNER_DIED in resp.text
