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
