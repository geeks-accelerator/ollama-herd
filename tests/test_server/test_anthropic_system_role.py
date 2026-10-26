"""Claude Code sends ``role: "system"`` messages inside ``messages[]``.

Claude Code 2.1.220 (seen 2026-10-26) puts a system message after the first
user turn (the agent-types listing), alongside the top-level ``system`` blocks.
Herd's request model accepted only user/assistant, so every such request was
a 400 before anything was routed: ``messages.1.role Input should be 'user' or
'assistant'``. These tests pin the fix: the message is accepted, kept in place
as a system message (the same as Ollama's own Anthropic layer), normalized like
the top-level system prompt, and reaches the backend.

The request shape below is synthetic; only its structure mirrors the capture.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.server.anthropic_models import AnthropicMessagesRequest
from fleet_manager.server.anthropic_translator import anthropic_to_ollama_messages
from fleet_manager.server.mlx_proxy import _ollama_messages_to_openai
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama
from tests.test_server.test_routes import create_test_app

MODEL = "qwen3-coder:30b"
AGENT_LIST = "Available agent types for the Agent tool: - general-purpose: multi-step tasks."

# The structure Claude Code 2.1.220 sends: top-level system blocks, a user turn,
# a mid-conversation system message, and the newer top-level keys.
CLAUDE_CODE_BODY = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 64,
    "system": [
        {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.220; cch=0a1b2c3d;"},
        {"type": "text", "text": "You are Claude Code."},
        {"type": "text", "text": "Use the tools."},
    ],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: herd ok"}]},
        {"role": "system", "content": [{"type": "text", "text": AGENT_LIST}]},
    ],
    "metadata": {"user_id": "test"},
    "thinking": {"type": "disabled"},
    "context_management": {"edits": []},
    "output_config": {"effort": "low"},
}


class TestRequestModel:
    def test_system_role_message_is_accepted(self):
        body = AnthropicMessagesRequest.model_validate(CLAUDE_CODE_BODY)
        assert [m.role for m in body.messages] == ["user", "system"]

    def test_string_content_system_message_is_accepted(self):
        body = AnthropicMessagesRequest.model_validate(
            {
                **CLAUDE_CODE_BODY,
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "system", "content": AGENT_LIST},
                ],
            }
        )
        assert body.messages[1].role == "system"

    def test_other_roles_are_still_rejected(self):
        with pytest.raises(ValueError):
            AnthropicMessagesRequest.model_validate(
                {**CLAUDE_CODE_BODY, "messages": [{"role": "tool", "content": "x"}]}
            )


class TestTranslation:
    def test_system_message_stays_in_place(self):
        out = anthropic_to_ollama_messages(
            [
                {"role": "user", "content": "first"},
                {"role": "system", "content": [{"type": "text", "text": AGENT_LIST}]},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "second"},
            ],
            system="top-level prompt",
        )
        assert [m["role"] for m in out] == ["system", "user", "system", "assistant", "user"]
        assert out[2] == {"role": "system", "content": AGENT_LIST}

    def test_system_message_text_is_normalized_like_the_system_prompt(self):
        out = anthropic_to_ollama_messages(
            [
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "header cch=deadbeef; rest"},
            ]
        )
        assert out[1]["content"] == "header cch=NORMALIZED; rest"

    def test_user_text_is_not_normalized(self):
        """Only system text is ours to normalize; a user may paste anything."""
        out = anthropic_to_ollama_messages([{"role": "user", "content": "cch=deadbeef;"}])
        assert out[0]["content"] == "cch=deadbeef;"

    def test_mlx_conversion_keeps_the_system_message(self):
        msgs = anthropic_to_ollama_messages(
            [{"role": "user", "content": "hi"}, {"role": "system", "content": AGENT_LIST}]
        )
        assert _ollama_messages_to_openai(msgs)[1] == {"role": "system", "content": AGENT_LIST}


class TestRoute:
    @pytest.fixture
    def fleet(self, tmp_path):
        app = create_test_app(tmp_path=tmp_path)
        with TestClient(app) as client:
            hb = make_heartbeat(
                node_id="studio", loaded_models=[(MODEL, 18.6)], available_models=[MODEL]
            )
            client.post("/heartbeat", json=hb.model_dump())
            yield client

    def _backend(self, client: TestClient, seen: list[dict]) -> None:
        done = {
            "model": MODEL,
            "message": {"role": "assistant", "content": "herd ok"},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 5,
            "eval_count": 2,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/chat":
                seen.append(json.loads(request.content))
                return httpx.Response(
                    200,
                    content=(json.dumps(done) + "\n").encode(),
                    headers={"content-type": "application/x-ndjson"},
                )
            return httpx.Response(200, json={})

        _install_mock_ollama(client, "studio", handler)

    @pytest.mark.parametrize("stream", [False, True])
    def test_claude_code_request_is_served(self, fleet, stream):
        seen: list[dict] = []
        self._backend(fleet, seen)
        resp = fleet.post("/v1/messages", json={**CLAUDE_CODE_BODY, "stream": stream})
        assert resp.status_code == 200, resp.text
        assert "herd ok" in resp.text
        roles = [m["role"] for m in seen[-1]["messages"]]
        assert roles == ["system", "user", "system"]
        assert seen[-1]["messages"][2]["content"] == AGENT_LIST
        assert "cch=NORMALIZED;" in seen[-1]["messages"][0]["content"]

    def test_count_tokens_accepts_it(self, fleet):
        resp = fleet.post("/v1/messages/count_tokens", json=CLAUDE_CODE_BODY)
        assert resp.status_code == 200, resp.text
        assert resp.json()["input_tokens"] > 0
