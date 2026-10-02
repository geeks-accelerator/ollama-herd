"""Thinking-model budget inflation.

Thinking models spend ``num_predict`` on chain-of-thought before any visible
output, so a small client budget can come back empty.  The proxy inflates it.

The first two classes pin the behavior as it was before Ollama-reported
capabilities existed, so widening detection can be shown not to change anything
for the models that already worked.
"""

from __future__ import annotations

from types import SimpleNamespace

from fleet_manager.server.model_knowledge import is_thinking_model
from fleet_manager.server.streaming import StreamingProxy


def _proxy(nodes: dict | None = None, settings=None) -> StreamingProxy:
    registry = SimpleNamespace(get_node=lambda node_id: (nodes or {}).get(node_id))
    return StreamingProxy(registry, settings=settings)


def _body(num_predict=None, **extra) -> dict:
    body = {"model": "m", "messages": [], **extra}
    if num_predict is not None:
        body["options"] = {"num_predict": num_predict}
    return body


# ---------------------------------------------------------------------------
# Characterization: behavior before capabilities (must not change)
# ---------------------------------------------------------------------------


class TestIsThinkingModelByName:
    def test_known_thinking_families(self):
        for name in ("deepseek-r1:8b", "gpt-oss:120b", "qwq:32b", "muse-glimmer:30b"):
            assert is_thinking_model(name), name

    def test_ordinary_models_are_not_thinking(self):
        for name in ("gemma3:27b", "llama3.3:70b", "nomic-embed-text:latest"):
            assert not is_thinking_model(name), name


class TestInflationCharacterization:
    def test_small_budget_is_raised_to_the_minimum(self):
        body = _body(num_predict=50)
        _proxy()._apply_thinking_overhead(body, "gpt-oss:120b")
        assert body["options"]["num_predict"] == 1024  # max(50 * 4, 1024)

    def test_large_budget_is_multiplied(self):
        body = _body(num_predict=2000)
        _proxy()._apply_thinking_overhead(body, "gpt-oss:120b")
        assert body["options"]["num_predict"] == 8000

    def test_unset_budget_is_left_to_ollama(self):
        body = _body()
        _proxy()._apply_thinking_overhead(body, "gpt-oss:120b")
        assert "options" not in body

    def test_non_thinking_model_is_untouched(self):
        body = _body(num_predict=50)
        _proxy()._apply_thinking_overhead(body, "gemma3:27b")
        assert body["options"]["num_predict"] == 50

    def test_settings_override_the_multiplier_and_floor(self):
        settings = SimpleNamespace(thinking_overhead=2.0, thinking_min_predict=100)
        body = _body(num_predict=500)
        _proxy(settings=settings)._apply_thinking_overhead(body, "gpt-oss:120b")
        assert body["options"]["num_predict"] == 1000


# ---------------------------------------------------------------------------
# Ollama-reported capabilities widen detection
# ---------------------------------------------------------------------------


def _node_reporting(model: str, capabilities: list[str]):
    from fleet_manager.models.node import ModelTagMeta

    return SimpleNamespace(
        ollama=SimpleNamespace(
            models_available_meta={model: ModelTagMeta(capabilities=capabilities)},
        )
    )


class TestCapabilityDetection:
    def test_reported_thinking_model_the_name_list_misses(self):
        """qwen3.8 thinks by default but matches no name pattern — before this,
        a small budget on it could come back empty."""
        assert not is_thinking_model("qwen3.8:27b")
        nodes = {"mini": _node_reporting("qwen3.8:27b", ["completion", "thinking"])}
        body = _body(num_predict=50)
        _proxy(nodes)._apply_thinking_overhead(body, "qwen3.8:27b", "mini")
        assert body["options"]["num_predict"] == 1024

    def test_no_node_reporting_falls_back_to_the_name_list(self):
        body = _body(num_predict=50)
        _proxy({})._apply_thinking_overhead(body, "gpt-oss:120b", "gone-node")
        assert body["options"]["num_predict"] == 1024

    def test_capability_absent_does_not_override_the_name_list(self):
        """Presence-only: Ollama not listing "thinking" is not evidence against."""
        nodes = {"mini": _node_reporting("gpt-oss:120b", ["completion"])}
        body = _body(num_predict=50)
        _proxy(nodes)._apply_thinking_overhead(body, "gpt-oss:120b", "mini")
        assert body["options"]["num_predict"] == 1024


class TestThinkFalse:
    def test_think_false_disables_inflation(self):
        """The client turned thinking off, so the whole budget is visible output."""
        nodes = {"mini": _node_reporting("qwen3.8:27b", ["thinking"])}
        body = _body(num_predict=50, think=False)
        _proxy(nodes)._apply_thinking_overhead(body, "qwen3.8:27b", "mini")
        assert body["options"]["num_predict"] == 50

    def test_think_levels_still_inflate(self):
        """gpt-oss takes "low"/"medium"/"high"; only an explicit False disables."""
        body = _body(num_predict=50, think="high")
        _proxy()._apply_thinking_overhead(body, "gpt-oss:120b")
        assert body["options"]["num_predict"] == 1024
