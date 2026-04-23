"""Bandwidth-aware scoring tests.

Validates that Signal 5 (role affinity), Signal 3 (queue depth), and Signal 4
(wait time) behave correctly when node ``memory_bandwidth_gbps`` is populated,
and that the fallback path (unknown bandwidth) preserves the original
memory-tier behaviour.

See ``docs/plans/device-aware-scoring.md`` for the design rationale and
expected load-distribution math.
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from tests.conftest import make_heartbeat


@pytest.fixture
def settings_bw_on():
    return ServerSettings(
        bandwidth_aware_scoring=True,
        queue_penalty_bandwidth_normalize=True,
    )


@pytest.fixture
def settings_bw_off():
    """Explicitly disable bandwidth-aware scoring — exercises fallback path."""
    return ServerSettings(
        bandwidth_aware_scoring=False,
        queue_penalty_bandwidth_normalize=False,
    )


@pytest.fixture
def registry_bw(settings_bw_on):
    return NodeRegistry(settings_bw_on)


@pytest.fixture
def registry_mem(settings_bw_off):
    return NodeRegistry(settings_bw_off)


@pytest.fixture
def scorer_bw(settings_bw_on, registry_bw):
    return ScoringEngine(settings_bw_on, registry_bw)


@pytest.fixture
def scorer_mem(settings_bw_off, registry_mem):
    return ScoringEngine(settings_bw_off, registry_mem)


# ---------------------------------------------------------------------------
# Signal 5 — bandwidth-aware role affinity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRoleAffinityBandwidth:
    async def test_mac_studio_beats_macbook_for_big_model(
        self, scorer_bw, registry_bw
    ):
        """The real-world case — both ≥128 GB, but Studio's bandwidth wins."""
        studio = make_heartbeat(
            node_id="studio",
            memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Ultra",
            memory_bandwidth_gbps=819.0,
        )
        macbook = make_heartbeat(
            node_id="macbook",
            memory_total=128.0, memory_used=40.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Max",
            memory_bandwidth_gbps=400.0,
        )
        await registry_bw.update_from_heartbeat(studio)
        await registry_bw.update_from_heartbeat(macbook)

        results = scorer_bw.score_request("qwen3-coder:30b-agent", {})
        affinity = {r.node_id: r.scores_breakdown["role_affinity"] for r in results}

        # Studio's +25 should clearly beat MacBook's +15
        assert affinity["studio"] > affinity["macbook"]
        assert affinity["studio"] >= 25.0
        assert 13.0 <= affinity["macbook"] <= 16.0

    async def test_unknown_bandwidth_falls_back_to_memory_tiers(
        self, scorer_bw, registry_bw
    ):
        """When bandwidth is unknown (0), the old memory-tier logic applies."""
        studio = make_heartbeat(
            node_id="studio",
            memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            # chip / bandwidth unset
        )
        await registry_bw.update_from_heartbeat(studio)
        results = scorer_bw.score_request("qwen3-coder:30b-agent", {})
        # Should get +15 (≥128 GB tier), not bandwidth bonus
        assert results[0].scores_breakdown["role_affinity"] == 15.0

    async def test_feature_flag_off_uses_memory_tiers(
        self, scorer_mem, registry_mem
    ):
        """When bandwidth_aware_scoring=False, bandwidth is ignored."""
        studio = make_heartbeat(
            node_id="studio",
            memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Ultra",
            memory_bandwidth_gbps=819.0,
        )
        await registry_mem.update_from_heartbeat(studio)
        results = scorer_mem.score_request("qwen3-coder:30b-agent", {})
        assert results[0].scores_breakdown["role_affinity"] == 15.0

    async def test_small_model_prefers_slower_node(
        self, scorer_bw, registry_bw
    ):
        """Keep the fast machine free — small models favour smaller/slower nodes."""
        studio = make_heartbeat(
            node_id="studio",
            memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen2.5:0.5b", 0.4)],
            chip="Apple M3 Ultra", memory_bandwidth_gbps=819.0,
        )
        small = make_heartbeat(
            node_id="air",
            memory_total=16.0, memory_used=4.0,
            loaded_models=[("qwen2.5:0.5b", 0.4)],
            chip="Apple M1", memory_bandwidth_gbps=68.0,
        )
        await registry_bw.update_from_heartbeat(studio)
        await registry_bw.update_from_heartbeat(small)

        results = scorer_bw.score_request("qwen2.5:0.5b", {})
        aff = {r.node_id: r.scores_breakdown["role_affinity"] for r in results}
        assert aff["air"] > aff["studio"]

    async def test_ultra_gets_clamped_to_max(self, scorer_bw, registry_bw):
        """Bandwidth bonus is capped at 25 — don't run away on datacentre GPUs."""
        a100 = make_heartbeat(
            node_id="a100",
            memory_total=256.0, memory_used=40.0,
            loaded_models=[("llama3.3:70b", 42.0)],
            chip="Intel Xeon + NVIDIA A100",
            memory_bandwidth_gbps=2039.0,
        )
        await registry_bw.update_from_heartbeat(a100)
        results = scorer_bw.score_request("llama3.3:70b", {})
        assert results[0].scores_breakdown["role_affinity"] <= 25.0


# ---------------------------------------------------------------------------
# Signal 3 — capacity-normalized queue penalty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestQueueDepthNormalization:
    async def test_fast_node_with_small_queue_outscores_slow_with_none(
        self, scorer_bw, registry_bw
    ):
        """Studio with queue=2 should still win over MacBook with queue=0
        because Studio is 2× faster — a queue of 2 there is like a queue of
        1 elsewhere, and its role_affinity bonus still dominates."""
        studio = make_heartbeat(
            node_id="studio",
            memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Ultra", memory_bandwidth_gbps=800.0,
        )
        macbook = make_heartbeat(
            node_id="macbook",
            memory_total=128.0, memory_used=40.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Max", memory_bandwidth_gbps=400.0,
        )
        await registry_bw.update_from_heartbeat(studio)
        await registry_bw.update_from_heartbeat(macbook)

        depths = {"studio:qwen3-coder:30b-agent": 2,
                  "macbook:qwen3-coder:30b-agent": 0}
        results = scorer_bw.score_request("qwen3-coder:30b-agent", depths)
        assert results[0].node_id == "studio"

    async def test_load_flips_to_slow_node_when_fast_saturated(
        self, scorer_bw, registry_bw
    ):
        """At some queue depth on Studio, MacBook should start winning."""
        studio = make_heartbeat(
            node_id="studio",
            memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Ultra", memory_bandwidth_gbps=800.0,
        )
        macbook = make_heartbeat(
            node_id="macbook",
            memory_total=128.0, memory_used=40.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Max", memory_bandwidth_gbps=400.0,
        )
        await registry_bw.update_from_heartbeat(studio)
        await registry_bw.update_from_heartbeat(macbook)

        # Heavily loaded Studio, empty MacBook — MacBook should win
        depths = {"studio:qwen3-coder:30b-agent": 20,
                  "macbook:qwen3-coder:30b-agent": 0}
        results = scorer_bw.score_request("qwen3-coder:30b-agent", depths)
        assert results[0].node_id == "macbook"

    async def test_normalization_ignores_nodes_without_bandwidth(
        self, scorer_bw, registry_bw
    ):
        """When bandwidth is unknown, fall back to plain queue-depth scoring."""
        node = make_heartbeat(
            node_id="unknown",
            memory_total=128.0, memory_used=40.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            # No chip / bandwidth
        )
        await registry_bw.update_from_heartbeat(node)
        results = scorer_bw.score_request(
            "qwen3-coder:30b-agent", {"unknown:qwen3-coder:30b-agent": 5}
        )
        # Should match the original -6/queue penalty (5 × 6 = -30 cap)
        assert results[0].scores_breakdown["queue_depth"] == -30.0

    async def test_no_queue_no_penalty(self, scorer_bw, registry_bw):
        node = make_heartbeat(
            node_id="studio", memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Ultra", memory_bandwidth_gbps=800.0,
        )
        await registry_bw.update_from_heartbeat(node)
        results = scorer_bw.score_request("qwen3-coder:30b-agent", {})
        assert results[0].scores_breakdown["queue_depth"] == 0.0


# ---------------------------------------------------------------------------
# Signal 4 — bandwidth cold-start fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWaitTimeBandwidthFallback:
    async def test_cold_start_uses_bandwidth_estimate(
        self, scorer_bw, registry_bw
    ):
        """Without a latency store entry, wait_time falls back to a bandwidth-
        derived throughput estimate (as opposed to the pre-bandwidth heuristic
        which had no speed signal at all)."""

        class _EmptyLatencyStore:
            def get_cached_percentile(self, _node_id, _model):
                return None

        fast = make_heartbeat(
            node_id="fast", memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Ultra", memory_bandwidth_gbps=800.0,
        )
        slow = make_heartbeat(
            node_id="slow", memory_total=128.0, memory_used=40.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M1", memory_bandwidth_gbps=68.0,
        )
        await registry_bw.update_from_heartbeat(fast)
        await registry_bw.update_from_heartbeat(slow)

        settings = ServerSettings(
            bandwidth_aware_scoring=True,
            queue_penalty_bandwidth_normalize=True,
        )
        scorer = ScoringEngine(settings, registry_bw, latency_store=_EmptyLatencyStore())

        depths = {"fast:qwen3-coder:30b-agent": 3, "slow:qwen3-coder:30b-agent": 3}
        results = scorer.score_request("qwen3-coder:30b-agent", depths)
        waits = {r.node_id: r.scores_breakdown["wait_time"] for r in results}
        # Slow node should have a bigger (more negative) wait_time penalty
        assert waits["slow"] < waits["fast"]


# ---------------------------------------------------------------------------
# End-to-end — expected load distribution over many sequential requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestProportionalLoadDistribution:
    async def test_studio_dominates_but_macbook_picks_up_spillover(
        self, scorer_bw, registry_bw
    ):
        """Simulate 20 sequential routing decisions on a 2-node fleet with
        growing queues. Studio should win most of them (higher base score),
        but MacBook should pick up at least some spillover as Studio queues.

        This doesn't prove the exact 67/33 ratio — that requires end-to-end
        simulation with actual request lifecycles — but it proves the
        scorer eventually flips under load rather than feeding all traffic
        to Studio until it collapses.
        """
        studio = make_heartbeat(
            node_id="studio",
            memory_total=512.0, memory_used=100.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Ultra", memory_bandwidth_gbps=800.0,
        )
        macbook = make_heartbeat(
            node_id="macbook",
            memory_total=128.0, memory_used=40.0,
            loaded_models=[("qwen3-coder:30b-agent", 30.0)],
            chip="Apple M3 Max", memory_bandwidth_gbps=400.0,
        )
        await registry_bw.update_from_heartbeat(studio)
        await registry_bw.update_from_heartbeat(macbook)

        # Simulate: each pick increments the winner's queue, routing_budget
        # of 20 total picks
        queue = {
            "studio:qwen3-coder:30b-agent": 0,
            "macbook:qwen3-coder:30b-agent": 0,
        }
        picks = {"studio": 0, "macbook": 0}
        for _ in range(20):
            results = scorer_bw.score_request("qwen3-coder:30b-agent", queue)
            winner = results[0].node_id
            picks[winner] += 1
            queue[f"{winner}:qwen3-coder:30b-agent"] += 1

        # Studio should win *most*, but not all — MacBook should pick up
        # at least some work as Studio's queue grows.
        assert picks["studio"] > picks["macbook"], (
            f"Studio should win majority but picks were {picks}"
        )
        assert picks["macbook"] > 0, (
            f"MacBook should win at least some spillover but picks were {picks}"
        )
