"""A thinking model's reasoning reaches OpenAI-format clients as ``reasoning``.

Run 6: gpt-oss:20b through /v1/chat/completions sent its first chunk at 5.0 s,
all of it answer, because the converter kept only content and tool calls and
dropped Ollama's ``thinking``.  Ollama's own /v1 and herd's MLX path call it
``reasoning``.
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama, create_test_app

MODEL = "gpt-oss:20b"
A = {"role": "assistant"}
LINES = "\n".join(
    json.dumps(x)
    for x in (
        {"model": MODEL, "message": {**A, "content": "", "thinking": "User wants "}, "done": False},
        {"model": MODEL, "message": {**A, "content": "", "thinking": "one word."}, "done": False},
        {"model": MODEL, "message": {**A, "content": "Hello"}, "done": False},
        {"model": MODEL, "done": True, "done_reason": "stop", "prompt_eval_count": 9,
         "eval_count": 6},
    )
) + "\n"  # fmt: skip


def _client(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    c = TestClient(app)
    c.__enter__()
    c.post(
        "/heartbeat",
        json=make_heartbeat(
            node_id="s1", lan_ip="10.0.0.1", loaded_models=[(MODEL, 13.0)], available_models=[MODEL]
        ).model_dump(),
    )
    _install_mock_ollama(c, "s1", lambda r: httpx.Response(200, content=LINES.encode()))
    return c


def test_streaming_sends_reasoning_before_content(tmp_path):
    c = _client(tmp_path)
    try:
        r = c.post(
            "/v1/chat/completions",
            json={"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        deltas = [
            json.loads(e[6:])["choices"][0]["delta"]
            for e in r.text.split("\n\n")
            if e.startswith("data: {")
        ]
        reasoning = [d["reasoning"] for d in deltas if d.get("reasoning")]
        assert reasoning == ["User wants ", "one word."]
        first_reasoning = next(i for i, d in enumerate(deltas) if d.get("reasoning"))
        first_content = next(i for i, d in enumerate(deltas) if d.get("content"))
        assert first_reasoning < first_content
    finally:
        c.__exit__(None, None, None)


def test_non_streaming_message_carries_the_reasoning(tmp_path):
    c = _client(tmp_path)
    try:
        r = c.post(
            "/v1/chat/completions",
            json={"model": MODEL, "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        )
        msg = r.json()["choices"][0]["message"]
        assert msg == {"role": "assistant", "content": "Hello", "reasoning": "User wants one word."}
    finally:
        c.__exit__(None, None, None)
