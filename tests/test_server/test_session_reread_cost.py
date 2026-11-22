"""Session affinity weighs waiting against re-reading the conversation (B6).

Run 6: eight conversations on two M4s, each opening with a ~1,500-token report.
A depth-only decay gave two of them away while their Mac was busy, and each
paid 9-11 s to re-read the report on the other Mac to save a few seconds of
queueing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.latency_store import _cold_prefill_rate
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.server.session_affinity import SessionAffinityTracker
from fleet_manager.testing.heartbeats import make_heartbeat

MODEL = "qwen2.5:7b"


async def _scorer(prefill_ms_per_token: float, p75_ms: float):
    settings = ServerSettings()
    registry = NodeRegistry(settings)
    await registry.update_from_heartbeat(
        make_heartbeat(node_id="home", memory_total=24.0, loaded_models=[(MODEL, 5.0)])
    )
    store = SimpleNamespace(
        get_cached_percentile=lambda node, model: p75_ms,
        get_cached_prefill_ms_per_token=lambda node, model: prefill_ms_per_token,
    )
    sessions = SessionAffinityTracker()
    sessions.remember("conv", "home")
    return ScoringEngine(settings, registry, latency_store=store, sessions=sessions)


def _bonus(scorer, tokens, depth=2):
    node = scorer._registry.get_node("home")
    return scorer._score_session_affinity(node, "conv", depth, MODEL, tokens)


async def test_a_long_conversation_stays_through_a_short_wait():
    """2,000 tokens at 6 ms each is 12 s to re-read; two requests ahead at
    1.5 s each is 3 s of waiting: most of the bonus stays."""
    scorer = await _scorer(prefill_ms_per_token=6.0, p75_ms=1500.0)
    assert _bonus(scorer, 2000) == pytest.approx(20.0 * 12 / 15)
    depth_only = 20.0 / (1 + 2 / ScoringEngine.SESSION_AFFINITY_DECAY_SCALE)
    assert _bonus(scorer, 2000) > depth_only, "the depth decay gave it away"


async def test_a_short_conversation_moves_when_its_node_is_busy():
    scorer = await _scorer(prefill_ms_per_token=6.0, p75_ms=1500.0)
    assert _bonus(scorer, 100) < 20.0 * 0.2  # 0.6 s to re-read against 3 s of waiting


async def test_no_queue_means_the_full_bonus():
    scorer = await _scorer(prefill_ms_per_token=6.0, p75_ms=1500.0)
    assert _bonus(scorer, 2000, depth=0) == 20.0


def test_the_cold_rate_ignores_cache_hits_and_short_prompts():
    rows = [(6000, 1000), (6200, 1000), (5800, 1000), (40, 1000), (5, 10)]
    rate = _cold_prefill_rate(rows)
    assert 5.5 < rate <= 6.2, "the fast cache hit and the 10-token prompt don't set it"
    assert _cold_prefill_rate([(6000, 1000)]) is None, "too few to say"


def test_prefill_time_is_in_the_request_timings():
    from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
    from fleet_manager.server.streaming import request_timings

    entry = QueueEntry(
        request=InferenceRequest(
            model=MODEL, messages=[], original_format=RequestFormat.OLLAMA, raw_body={}
        ),
        assigned_node="home",
    )
    timings = request_timings(entry, entry.enqueued_at, entry.enqueued_at + 1, 900.0, 640.0)
    assert timings["prefill_ms"] == 640.0
