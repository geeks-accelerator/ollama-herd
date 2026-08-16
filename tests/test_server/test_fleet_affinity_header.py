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
