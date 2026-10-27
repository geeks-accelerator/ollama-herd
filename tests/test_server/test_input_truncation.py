"""Silent input truncation: detected from what the backend actually evaluated.

Ollama truncates an over-window prompt and still answers 200 -- measured
2026-10-27 on 0.40.2 at num_ctx=2048 with a ~6K-token prompt: one long message
was evaluated at 1,027 tokens, the same text split across turns at 34 (the long
message dropped whole), and the model lost a fact planted at the start both
times.  Routing already flags the *estimate* exceeding the window; these pin the
second half -- the backend's own count confirming it -- and the card it feeds.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fleet_manager.models.request import InferenceRequest, QueueEntry
from fleet_manager.server.health_engine import HealthEngine, Severity
from fleet_manager.server.routes.routing import check_context_overflow
from fleet_manager.server.streaming import (
    TRUNCATION_RATIO,
    StreamingProxy,
    confirmed_truncation,
    get_context_protection_events,
)


def _req(estimated=6000, window=2048, model="gemma3:4b"):
    return InferenceRequest(model=model, estimated_tokens=estimated, overflow_context_length=window)


class TestConfirmedTruncation:
    @pytest.mark.parametrize("evaluated", [1027, 34])
    def test_the_two_measured_shapes_are_caught(self, evaluated):
        assert confirmed_truncation(_req(), evaluated)

    def test_untruncated_floor_is_not_caught(self):
        # 0.70 was the lowest evaluated/estimate ratio across 54,454 untruncated
        # Ollama-format requests; the threshold must sit below it.
        assert TRUNCATION_RATIO < 0.70
        assert not confirmed_truncation(_req(estimated=3000, window=2048), 2100)

    def test_needs_the_overflow_half(self):
        """A low ratio alone is not evidence: the estimator runs ~8x low on tiny
        prompts and high on pre-translation Responses bodies."""
        req = InferenceRequest(model="m", estimated_tokens=9500)
        assert not confirmed_truncation(req, 4200)

    def test_needs_a_backend_count(self):
        assert not confirmed_truncation(_req(), None)


def test_overflow_routing_marks_the_request_with_the_window():
    req = InferenceRequest(model="gemma3:4b", messages=[{"role": "user", "content": "x" * 24000}])
    node = SimpleNamespace(
        ollama=SimpleNamespace(
            models_loaded=[SimpleNamespace(name="gemma3:4b", context_length=2048)]
        )
    )
    registry = MagicMock()
    registry.get_node.return_value = node
    winner = SimpleNamespace(node_id="bb", scores_breakdown={"context_fit": -10})
    headers = check_context_overflow(winner, req, registry)
    assert "X-Fleet-Context-Overflow" in headers
    assert req.overflow_context_length == 2048


def _trace(proxy, req, evaluated):
    proxy._request_tokens[req.request_id] = (evaluated, 5)
    entry = QueueEntry(request=req, assigned_node="bb")
    proxy._record_trace(entry, "bb", time.time() - 1, None, "completed")


def test_trace_step_records_a_truncation_event_and_logs(caplog):
    proxy = StreamingProxy(MagicMock())
    with caplog.at_level("WARNING", logger="fleet_manager.server.streaming"):
        _trace(proxy, _req(), 1027)
    evs = [e for e in get_context_protection_events(1) if e["action"] == "truncated"]
    assert len(evs) == 1
    assert (evs[0]["client_num_ctx"], evs[0]["loaded_ctx"]) == (6000, 2048)
    assert "Input truncated" in caplog.text


def test_trace_step_is_silent_when_the_prompt_fit():
    proxy = StreamingProxy(MagicMock())
    _trace(proxy, _req(estimated=2200, window=2048), 1900)
    assert not [e for e in get_context_protection_events(1) if e["action"] == "truncated"]


class TestCard:
    def _cards(self):
        return [
            r for r in HealthEngine()._check_context_protection() if r.check_id == "input_truncated"
        ]

    def test_silent_with_no_truncation(self):
        assert self._cards() == []

    def test_one_off_is_info(self):
        _trace(StreamingProxy(MagicMock()), _req(), 1027)
        (card,) = self._cards()
        assert card.severity == Severity.INFO
        assert "gemma3:4b" in card.description and "2048" in card.description

    def test_repeated_on_one_model_is_a_warning(self):
        proxy = StreamingProxy(MagicMock())
        for _ in range(HealthEngine.TRUNCATION_REPEAT_THRESHOLD):
            _trace(proxy, _req(), 1027)
        (card,) = self._cards()
        assert card.severity == Severity.WARNING
        assert "FLEET_NUM_CTX_OVERRIDES" in card.fix
