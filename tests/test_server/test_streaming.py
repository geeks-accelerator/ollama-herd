"""Tests for StreamingProxy format conversion."""

from __future__ import annotations

import json

import pytest

from fleet_manager.server.streaming import StreamingProxy
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.models.config import ServerSettings


@pytest.fixture
def proxy():
    settings = ServerSettings()
    registry = NodeRegistry(settings)
    return StreamingProxy(registry)


class TestOllamaToOpenAIConversion:
    def test_content_chunk(self, proxy):
        ollama_line = json.dumps({
            "model": "phi4:14b",
            "message": {"role": "assistant", "content": "Hello"},
            "done": False,
        })
        result = proxy._ollama_to_openai_sse(ollama_line, "phi4:14b")
        assert result.startswith("data: ")
        data = json.loads(result[6:].strip())
        assert data["object"] == "chat.completion.chunk"
        assert data["model"] == "phi4:14b"
        assert data["choices"][0]["delta"]["content"] == "Hello"
        assert data["choices"][0]["finish_reason"] is None

    def test_done_chunk(self, proxy):
        ollama_line = json.dumps({
            "model": "phi4:14b",
            "message": {"role": "assistant", "content": ""},
            "done": True,
        })
        result = proxy._ollama_to_openai_sse(ollama_line, "phi4:14b")
        data = json.loads(result[6:].strip())
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["choices"][0]["delta"] == {}

    def test_generate_format(self, proxy):
        # /api/generate uses "response" field instead of "message"
        ollama_line = json.dumps({
            "model": "phi4:14b",
            "response": "Hello world",
            "done": False,
        })
        result = proxy._ollama_to_openai_sse(ollama_line, "phi4:14b")
        data = json.loads(result[6:].strip())
        assert data["choices"][0]["delta"]["content"] == "Hello world"

    def test_invalid_json(self, proxy):
        result = proxy._ollama_to_openai_sse("not valid json", "phi4:14b")
        assert result == ""

    def test_empty_content(self, proxy):
        ollama_line = json.dumps({
            "model": "phi4:14b",
            "message": {"role": "assistant", "content": ""},
            "done": False,
        })
        result = proxy._ollama_to_openai_sse(ollama_line, "phi4:14b")
        data = json.loads(result[6:].strip())
        assert data["choices"][0]["delta"]["content"] == ""


class TestBuildOllamaBody:
    def test_passthrough_ollama_format(self, proxy):
        from fleet_manager.models.request import InferenceRequest, RequestFormat
        req = InferenceRequest(
            model="phi4:14b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={"model": "phi4:14b", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
        )
        body = proxy._build_ollama_body(req)
        assert body["stream"] is True  # Always stream internally
        assert body["model"] == "phi4:14b"

    def test_openai_to_ollama(self, proxy):
        from fleet_manager.models.request import InferenceRequest, RequestFormat
        req = InferenceRequest(
            model="phi4:14b",
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0.5,
            max_tokens=100,
            original_format=RequestFormat.OPENAI,
            raw_body={"model": "phi4:14b", "messages": [{"role": "user", "content": "Hi"}]},
        )
        body = proxy._build_ollama_body(req)
        assert body["model"] == "phi4:14b"
        assert body["messages"] == [{"role": "user", "content": "Hi"}]
        assert body["stream"] is True
        assert body["options"]["temperature"] == 0.5
        assert body["options"]["num_predict"] == 100

    def test_default_temperature_not_included(self, proxy):
        from fleet_manager.models.request import InferenceRequest, RequestFormat
        req = InferenceRequest(
            model="phi4:14b",
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0.7,  # default
            original_format=RequestFormat.OPENAI,
            raw_body={"model": "phi4:14b"},
        )
        body = proxy._build_ollama_body(req)
        assert "options" not in body
