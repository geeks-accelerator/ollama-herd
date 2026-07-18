"""Tests for StreamingProxy format conversion and context protection."""

from __future__ import annotations

import json
import logging

import pytest

from fleet_manager.server.streaming import StreamingProxy
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import InferenceRequest, RequestFormat


@pytest.fixture
def proxy():
    settings = ServerSettings()
    registry = NodeRegistry(settings)
    return StreamingProxy(registry, settings=settings)


def _make_proxy_with_loaded_model(
    model_name: str = "gpt-oss:120b",
    context_length: int = 32768,
    context_protection: str = "strip",
):
    """Create a StreamingProxy with a node that has a model loaded at a specific context."""
    from fleet_manager.models.node import (
        LoadedModel, NodeState, NodeStatus, HardwareProfile,
        CpuMetrics, MemoryMetrics, DiskMetrics, OllamaMetrics,
    )
    import time

    settings = ServerSettings(context_protection=context_protection)
    registry = NodeRegistry(settings)
    node = NodeState(
        node_id="test-node",
        status=NodeStatus.ONLINE,
        hardware=HardwareProfile(node_id="test-node", memory_total_gb=512.0, cores_physical=32),
        last_heartbeat=time.time(),
        cpu=CpuMetrics(cores_physical=32, utilization_pct=5.0),
        memory=MemoryMetrics(total_gb=512.0, used_gb=100.0, available_gb=412.0),
        disk=DiskMetrics(total_gb=1000.0, used_gb=200.0, available_gb=800.0),
        ollama=OllamaMetrics(
            models_loaded=[LoadedModel(name=model_name, size_gb=89.0, context_length=context_length)],
            models_available=[model_name],
        ),
    )
    registry._nodes["test-node"] = node
    proxy = StreamingProxy(registry, settings=settings)
    return proxy


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
        req = InferenceRequest(
            model="phi4:14b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={"model": "phi4:14b", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
        )
        body = proxy._build_ollama_body(req, "some-node")
        assert body["stream"] is True  # Always stream internally
        assert body["model"] == "phi4:14b"

    def test_openai_to_ollama(self, proxy):
        req = InferenceRequest(
            model="phi4:14b",
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0.5,
            max_tokens=100,
            original_format=RequestFormat.OPENAI,
            raw_body={"model": "phi4:14b", "messages": [{"role": "user", "content": "Hi"}]},
        )
        body = proxy._build_ollama_body(req, "some-node")
        assert body["model"] == "phi4:14b"
        assert body["messages"] == [{"role": "user", "content": "Hi"}]
        assert body["stream"] is True
        assert body["options"]["temperature"] == 0.5
        assert body["options"]["num_predict"] == 100

    def test_default_temperature_not_included(self, proxy):
        req = InferenceRequest(
            model="phi4:14b",
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0.7,  # default
            original_format=RequestFormat.OPENAI,
            raw_body={"model": "phi4:14b"},
        )
        body = proxy._build_ollama_body(req, "some-node")
        assert "options" not in body


class TestContextProtection:
    """Tests for context-size protection that prevents Ollama model reloads."""

    def test_strips_small_num_ctx(self):
        """num_ctx smaller than loaded context should be stripped to prevent reload."""
        proxy = _make_proxy_with_loaded_model(context_length=32768, context_protection="strip")
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "gpt-oss:120b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 4096},
            },
        )
        body = proxy._build_ollama_body(req, "test-node")
        # num_ctx should be stripped — the model already has 32768 context
        assert "options" not in body or "num_ctx" not in body.get("options", {})

    def test_strips_equal_num_ctx(self):
        """num_ctx equal to loaded context should also be stripped (no resize needed)."""
        proxy = _make_proxy_with_loaded_model(context_length=32768, context_protection="strip")
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "gpt-oss:120b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 32768, "temperature": 0.5},
            },
        )
        body = proxy._build_ollama_body(req, "test-node")
        assert "num_ctx" not in body.get("options", {})
        # Other options should be preserved
        assert body["options"]["temperature"] == 0.5

    def test_inert_override_is_not_injected(self, caplog):
        """A num_ctx override below the loaded context can't apply — Ollama would
        have to unload and reload. Don't inject a value the strip branch is
        guaranteed to remove; that produced 393 injected/393 stripped log pairs
        in 9 hours and made a dead knob look active.
        See docs/plans/codex-code-mode-escalation.md (soak findings)."""
        proxy = _make_proxy_with_loaded_model(context_length=262144, context_protection="strip")
        proxy._settings.dynamic_num_ctx = True
        proxy._settings.num_ctx_overrides = {"gpt-oss:120b": 32768}
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={"model": "gpt-oss:120b", "messages": [{"role": "user", "content": "Hi"}]},
        )
        with caplog.at_level(logging.WARNING):
            body = proxy._build_ollama_body(req, "test-node")
        assert "num_ctx" not in body.get("options", {})
        assert "cannot apply" in caplog.text
        assert "injected" not in caplog.text

        # ...and the explanation is logged once per model, not once per request.
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            proxy._build_ollama_body(req, "test-node")
        assert "cannot apply" not in caplog.text

    def test_override_still_applies_when_model_not_loaded(self):
        """The override's real job — setting the context a cold load comes up with."""
        proxy = _make_proxy_with_loaded_model(context_length=262144, context_protection="strip")
        proxy._settings.dynamic_num_ctx = True
        proxy._settings.num_ctx_overrides = {"qwen3-coder:30b": 32768}
        req = InferenceRequest(
            model="qwen3-coder:30b",   # not resident on the node
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={"model": "qwen3-coder:30b", "messages": [{"role": "user", "content": "Hi"}]},
        )
        body = proxy._build_ollama_body(req, "test-node")
        assert body["options"]["num_ctx"] == 32768

    def test_keeps_larger_num_ctx(self, caplog):
        """num_ctx larger than loaded context should be preserved (client needs more)."""
        proxy = _make_proxy_with_loaded_model(context_length=32768, context_protection="strip")
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "gpt-oss:120b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 65536},
            },
        )
        with caplog.at_level(logging.WARNING):
            body = proxy._build_ollama_body(req, "test-node")
        # num_ctx should be preserved — client wants more than available
        assert body["options"]["num_ctx"] == 65536
        assert "client wants num_ctx=65536" in caplog.text

    def test_passthrough_mode(self):
        """Passthrough mode should not modify num_ctx at all."""
        proxy = _make_proxy_with_loaded_model(context_length=32768, context_protection="passthrough")
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "gpt-oss:120b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 4096},
            },
        )
        body = proxy._build_ollama_body(req, "test-node")
        assert body["options"]["num_ctx"] == 4096

    def test_warn_mode(self, caplog):
        """Warn mode should preserve num_ctx but log a warning."""
        proxy = _make_proxy_with_loaded_model(context_length=32768, context_protection="warn")
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "gpt-oss:120b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 4096},
            },
        )
        with caplog.at_level(logging.WARNING):
            body = proxy._build_ollama_body(req, "test-node")
        # num_ctx preserved in warn mode
        assert body["options"]["num_ctx"] == 4096
        assert "would trigger reload" in caplog.text

    def test_no_num_ctx_unchanged(self):
        """Requests without num_ctx should pass through unchanged."""
        proxy = _make_proxy_with_loaded_model(context_length=32768, context_protection="strip")
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "gpt-oss:120b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"temperature": 0.5},
            },
        )
        body = proxy._build_ollama_body(req, "test-node")
        assert body["options"]["temperature"] == 0.5
        assert "num_ctx" not in body["options"]

    def test_unknown_model_passthrough(self):
        """If model isn't in loaded list, num_ctx should pass through."""
        proxy = _make_proxy_with_loaded_model(
            model_name="different-model:latest", context_length=32768, context_protection="strip"
        )
        req = InferenceRequest(
            model="gpt-oss:120b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "gpt-oss:120b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 4096},
            },
        )
        body = proxy._build_ollama_body(req, "test-node")
        # Can't protect — model not found in loaded list
        assert body["options"]["num_ctx"] == 4096

    def test_context_upgrade_switches_model(self, caplog):
        """When num_ctx > loaded context and a bigger model with enough context exists, switch."""
        from fleet_manager.models.node import (
            LoadedModel, NodeState, NodeStatus, HardwareProfile,
            CpuMetrics, MemoryMetrics, DiskMetrics, OllamaMetrics,
        )
        import time

        settings = ServerSettings(context_protection="strip")
        registry = NodeRegistry(settings)
        # Node has two models: small one (32k ctx, 10GB) and big one (128k ctx, 89GB)
        node = NodeState(
            node_id="test-node",
            status=NodeStatus.ONLINE,
            hardware=HardwareProfile(node_id="test-node", memory_total_gb=512.0, cores_physical=32),
            last_heartbeat=time.time(),
            cpu=CpuMetrics(cores_physical=32, utilization_pct=5.0),
            memory=MemoryMetrics(total_gb=512.0, used_gb=100.0, available_gb=412.0),
            disk=DiskMetrics(total_gb=1000.0, used_gb=200.0, available_gb=800.0),
            ollama=OllamaMetrics(
                models_loaded=[
                    LoadedModel(name="small-model:7b", size_gb=10.0, context_length=32768),
                    LoadedModel(name="big-model:70b", size_gb=89.0, context_length=131072),
                ],
                models_available=["small-model:7b", "big-model:70b"],
            ),
        )
        registry._nodes["test-node"] = node
        proxy = StreamingProxy(registry, settings=settings)

        req = InferenceRequest(
            model="small-model:7b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "small-model:7b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 65536},
            },
        )
        with caplog.at_level(logging.INFO):
            body = proxy._build_ollama_body(req, "test-node")

        # Should switch to big-model:70b which has 131072 context
        assert body["model"] == "big-model:70b"
        # num_ctx should be stripped since the upgrade model has enough context
        assert "options" not in body or "num_ctx" not in body.get("options", {})
        assert "switched small-model:7b → big-model:70b" in caplog.text

    def test_context_upgrade_no_suitable_model(self, caplog):
        """When num_ctx > loaded context but no bigger model has enough context, warn."""
        from fleet_manager.models.node import (
            LoadedModel, NodeState, NodeStatus, HardwareProfile,
            CpuMetrics, MemoryMetrics, DiskMetrics, OllamaMetrics,
        )
        import time

        settings = ServerSettings(context_protection="strip")
        registry = NodeRegistry(settings)
        # Node has two models but neither has enough context for 256k
        node = NodeState(
            node_id="test-node",
            status=NodeStatus.ONLINE,
            hardware=HardwareProfile(node_id="test-node", memory_total_gb=512.0, cores_physical=32),
            last_heartbeat=time.time(),
            cpu=CpuMetrics(cores_physical=32, utilization_pct=5.0),
            memory=MemoryMetrics(total_gb=512.0, used_gb=100.0, available_gb=412.0),
            disk=DiskMetrics(total_gb=1000.0, used_gb=200.0, available_gb=800.0),
            ollama=OllamaMetrics(
                models_loaded=[
                    LoadedModel(name="small-model:7b", size_gb=10.0, context_length=32768),
                    LoadedModel(name="big-model:70b", size_gb=89.0, context_length=131072),
                ],
                models_available=["small-model:7b", "big-model:70b"],
            ),
        )
        registry._nodes["test-node"] = node
        proxy = StreamingProxy(registry, settings=settings)

        req = InferenceRequest(
            model="small-model:7b",
            messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={
                "model": "small-model:7b",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"num_ctx": 262144},
            },
        )
        with caplog.at_level(logging.WARNING):
            body = proxy._build_ollama_body(req, "test-node")

        # No model has 262k context — keep num_ctx and warn
        assert body["options"]["num_ctx"] == 262144
        assert "client wants num_ctx=262144" in caplog.text


# ---------------------------------------------------------------------------
# OpenAI function calling — regression guard
#
# Found 2026-07-18: the OpenAI route silently dropped tool calls in BOTH
# directions. Requests lost `tools` (only OLLAMA/ANTHROPIC/RESPONSES took the
# raw_body passthrough; OPENAI fell to a fallback branch that rebuilt the body
# without them), and responses lost `tool_calls` (_ollama_to_openai_sse only
# ever emitted delta.content). Net effect: every OpenAI-SDK client doing
# function calling got prose instead of the call it had actually made.
# ---------------------------------------------------------------------------


def test_openai_request_carries_tools_to_ollama():
    """The fallback body-builder must not drop `tools`/`tool_choice`."""
    import json as _json

    from fleet_manager.models.request import InferenceRequest, RequestFormat

    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    proxy = _make_proxy_with_loaded_model()
    req = InferenceRequest(
        model="qwen3-coder:30b",
        messages=[{"role": "user", "content": "hi"}],
        original_format=RequestFormat.OPENAI,
        raw_body={"model": "qwen3-coder:30b", "messages": [], "tools": tools,
                  "tool_choice": "auto"},
    )
    body = proxy._build_ollama_body(req, "some-node")
    assert body.get("tools") == tools, "tools must survive to the Ollama body"
    assert body.get("tool_choice") == "auto"
    _ = _json


def test_ollama_tool_call_becomes_openai_streaming_delta():
    """Ollama emits tool_calls with an OBJECT `arguments`; OpenAI clients expect
    a JSON string, inside delta.tool_calls."""
    import json as _json

    proxy = _make_proxy_with_loaded_model()
    line = _json.dumps({"message": {"content": "", "tool_calls": [
        {"function": {"name": "bash", "arguments": {"cmd": "ls"}}}]}})
    out = proxy._ollama_to_openai_sse(line, "qwen3-coder:30b")
    chunk = _json.loads(out.removeprefix("data: ").strip())
    tc = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "bash"
    assert tc["function"]["arguments"] == '{"cmd": "ls"}'  # string, not object
    assert tc["id"] and tc["type"] == "function"


def test_openai_done_chunk_reports_tool_calls_finish_reason():
    import json as _json

    proxy = _make_proxy_with_loaded_model()
    line = _json.dumps({"done": True, "message": {"tool_calls": [
        {"function": {"name": "bash", "arguments": {}}}]}})
    chunk = _json.loads(
        proxy._ollama_to_openai_sse(line, "m").removeprefix("data: ").strip()
    )
    assert chunk["choices"][0]["finish_reason"] == "tool_calls"


def test_plain_text_streaming_is_unchanged():
    """Guard the non-tool path stayed identical."""
    import json as _json

    proxy = _make_proxy_with_loaded_model()
    chunk = _json.loads(
        proxy._ollama_to_openai_sse(_json.dumps({"message": {"content": "hi"}}), "m")
        .removeprefix("data: ").strip()
    )
    assert chunk["choices"][0]["delta"] == {"content": "hi"}
    assert chunk["choices"][0]["finish_reason"] is None


def test_finish_reason_survives_the_route_popping_request_meta():
    """Regression: finish_reason was read out of `_request_meta`, which routes pop
    to build response headers. Trace recording runs in the queue worker while the
    route runs in its own task, so the read raced the pop and lost ~8% of the
    time — always on the non-streaming path, where the route consumes everything
    at once. done_reason now lives in a dict the trace path alone owns.
    """
    proxy = _make_proxy_with_loaded_model()
    rid = "req-1"
    proxy._request_tokens[rid] = (10, 20)
    proxy._request_meta[rid] = {"done_reason": "length"}
    proxy._request_done_reason[rid] = "length"

    # The route builds its headers first — this is the race that used to win.
    assert proxy.pop_request_meta(rid) == {"done_reason": "length"}

    # The trace path still sees the value.
    assert proxy._request_done_reason.pop(rid, "") == "length"
