"""Semantics of ``X-Fleet-Affinity``.

This header reports a ROUTING decision, never a cache hit.  We cannot observe
whether a backend reused its prefix cache: Ollama folds llama.cpp's ``cache_n``
back into ``prompt_n`` on purpose (ollama/ollama#16428), so
``prompt_eval_count`` cannot yield a hit ratio.  Inferring one from it already
produced a false "zero prefix-cache reuse" report in this project.

These tests exist so that stays true.  If a future change makes the header
claim ``hit``/``miss``, or emit a value where affinity never applied, it should
fail here rather than mislead a user reading their own terminal.
"""

from __future__ import annotations

from fleet_manager.server.fleet_headers import affinity_from_breakdown, fleet_headers


class TestThreeStates:
    """`did not apply` and `missed` are different facts. Do not collapse them."""

    def test_nonzero_session_signal_is_matched(self):
        assert affinity_from_breakdown({"session_affinity": 20.0}) == "matched"

    def test_zero_session_signal_is_new(self):
        assert affinity_from_breakdown({"session_affinity": 0}) == "new"

    def test_scored_without_the_signal_is_new(self):
        """Scoring ran, affinity simply did not fire."""
        assert affinity_from_breakdown({"thermal": 50.0}) == "new"

    def test_unscored_request_omits_the_header(self):
        """Direct MLX passthrough / embeddings never score — say nothing."""
        assert affinity_from_breakdown(None) is None
        assert affinity_from_breakdown({}) is None

    def test_header_absent_when_affinity_does_not_apply(self):
        headers = fleet_headers(
            node_id="bb", served_model="m", requested_model="m",
            affinity=affinity_from_breakdown(None),
        )
        assert "X-Fleet-Affinity" not in headers


class TestNeverClaimsACacheHit:
    """The mistake this header was designed around."""

    def test_values_are_routing_words_not_cache_words(self):
        for breakdown in ({"session_affinity": 20.0}, {"session_affinity": 0}):
            value = affinity_from_breakdown(breakdown)
            assert value in ("matched", "new")
            assert value not in ("hit", "miss", "HIT", "MISS")

    def test_header_set_carries_no_cache_claim(self):
        headers = fleet_headers(
            node_id="bb", served_model="m", requested_model="m",
            affinity=affinity_from_breakdown({"session_affinity": 20.0}),
        )
        blob = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
        assert "cache" not in blob, (
            "X-Fleet-* must not assert cache state: Ollama discards cache_n "
            "(ollama/ollama#16428) so we cannot measure it"
        )


class TestEmission:
    def test_matched_and_new_are_emitted(self):
        for value in ("matched", "new"):
            headers = fleet_headers(
                node_id="bb", served_model="m", requested_model="m", affinity=value,
            )
            assert headers["X-Fleet-Affinity"] == value

    def test_does_not_disturb_the_canonical_set(self):
        base = fleet_headers(node_id="bb", served_model="m", requested_model="m")
        with_aff = fleet_headers(
            node_id="bb", served_model="m", requested_model="m", affinity="matched",
        )
        assert set(base).issubset(set(with_aff))
        assert set(with_aff) - set(base) == {"X-Fleet-Affinity"}


class TestAffinityDecaysUnderLoad:
    """A flat bonus is the naive version: a warm but saturated node keeps
    winning while an idle peer sits free.  That is the bug vLLM's
    production-stack shipped `loadaware` routing to fix, and every production
    router (Dynamo, Ray Serve, SGLang) decays the cache bonus by load."""

    def _engine(self):
        from fleet_manager.models.config import ServerSettings
        from fleet_manager.server.scorer import ScoringEngine

        class Sessions:
            def preferred_node(self, key):
                return "bb"

        return ScoringEngine(ServerSettings(), registry=None, sessions=Sessions())

    class _Node:
        node_id = "bb"

    def test_full_bonus_on_an_idle_node(self):
        engine = self._engine()
        assert engine._score_session_affinity(self._Node(), "s", 0) == (
            engine.SESSION_AFFINITY_BONUS
        )

    def test_bonus_shrinks_as_the_queue_grows(self):
        engine = self._engine()
        scores = [engine._score_session_affinity(self._Node(), "s", d) for d in (0, 1, 2, 4, 8)]
        assert scores == sorted(scores, reverse=True), scores
        assert scores[-1] < scores[0] / 4

    def test_decay_never_exceeds_the_ceiling(self):
        """Decay may only shrink, so the deliberate 20-vs-thermal-50 gap holds."""
        engine = self._engine()
        for depth in range(0, 20):
            assert 0 < engine._score_session_affinity(self._Node(), "s", depth) <= (
                engine.SESSION_AFFINITY_BONUS
            )

    def test_no_pin_scores_zero_regardless_of_depth(self):
        from fleet_manager.models.config import ServerSettings
        from fleet_manager.server.scorer import ScoringEngine

        class NoPin:
            def preferred_node(self, key):
                return None

        engine = ScoringEngine(ServerSettings(), registry=None, sessions=NoPin())
        for depth in (0, 5, 50):
            assert engine._score_session_affinity(self._Node(), "s", depth) == 0.0

    def test_negative_depth_is_clamped(self):
        engine = self._engine()
        assert engine._score_session_affinity(self._Node(), "s", -5) == (
            engine.SESSION_AFFINITY_BONUS
        )


class TestCachedTokensIsOmittedNotZeroed:
    """`0` means measured-and-missed. Absent means cannot-measure. Conflating
    them is the bug vLLM shipped (#44383) and SGLang still has -- and this
    fleet is routinely in the second case, because Ollama folds llama.cpp's
    cache_n back into prompt_n on purpose (ollama/ollama#16428)."""

    BASE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}

    def _usage(self, cached):
        from fleet_manager.server.fleet_headers import usage_with_cached_tokens

        return usage_with_cached_tokens(self.BASE, cached)

    def test_unmeasurable_backend_omits_the_field(self):
        """The Ollama case -- the one that must never claim a miss."""
        assert "prompt_tokens_details" not in self._usage(None)

    def test_a_real_zero_is_still_reported(self):
        """MLX measured and genuinely reused nothing. That is a fact, keep it."""
        assert self._usage(0)["prompt_tokens_details"] == {"cached_tokens": 0}

    def test_a_real_hit_is_reported(self):
        assert self._usage(80)["prompt_tokens_details"] == {"cached_tokens": 80}

    def test_the_original_usage_is_not_mutated(self):
        before = dict(self.BASE)
        self._usage(80)
        assert before == self.BASE

    def test_core_usage_fields_survive(self):
        for cached in (None, 0, 80):
            usage = self._usage(cached)
            assert usage["prompt_tokens"] == 100
            assert usage["completion_tokens"] == 10
            assert usage["total_tokens"] == 110
