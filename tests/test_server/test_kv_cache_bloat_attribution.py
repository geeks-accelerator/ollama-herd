"""KV cache bloat must name the lever that would actually recover the memory.

KV is ``context x parallel``, so bloat has two independent causes.  The check
used to assert only one of them -- "OLLAMA_NUM_PARALLEL being too high" -- and
prescribe a node-wide drop to 2 regardless.

On 2026-10-09 on the live fleet that advice was strictly worse than the
available action.  gemma3:27b was bloated because it sat resident at 131072
against its *configured* 32768 (an un-applied FLEET_NUM_CTX_OVERRIDES entry).
The right fix was a single-model reload: ~8.5 GB -> ~2.1 GB, nothing else
affected.  Lowering node parallelism 4 -> 2 would have reached only ~4.3 GB,
raised TTFT for every other model, and left the misconfiguration in place.

The check had zero test coverage at the time, which is how it carried wrong
arithmetic (a promised ``kv / 8`` saving beside a 4 -> 2 recommendation that can
only halve it) for as long as it did.
"""

import json
from types import SimpleNamespace

import pytest

from fleet_manager.server.health_engine import HealthEngine


def _node(*loaded, num_parallel=4, node_id="bb", disk_sizes=None):
    """A node whose loaded models are (name, parameter_size, size_gb, ctx).

    ``disk_sizes`` is what the node reports from /api/tags; leaving it out is
    the older-agent case that falls back to the parameter-count heuristic.
    """
    return SimpleNamespace(
        node_id=node_id,
        status=SimpleNamespace(value="online"),
        memory=SimpleNamespace(available_gb=300.0),
        ollama=SimpleNamespace(
            num_parallel=num_parallel,
            models_available_meta={},
            models_available_sizes=disk_sizes or {},
            models_loaded=[
                SimpleNamespace(
                    name=n, parameter_size=ps, size_gb=gb, context_length=ctx
                )
                for n, ps, gb, ctx in loaded
            ],
        ),
    )


@pytest.fixture
def overrides(monkeypatch):
    def _set(mapping):
        monkeypatch.setenv("FLEET_NUM_CTX_OVERRIDES", json.dumps(mapping))

    return _set


# gemma3:27b as measured: 27B params -> ~13.5 GB weights at the check's Q4
# heuristic, 22.2 GB resident, so ~8.7 GB of KV at ctx=131072.
GEMMA = ("gemma3:27b", "27B", 22.2, 131072)


class TestAttribution:
    def test_inert_override_recommends_a_reload_not_parallelism(self, overrides):
        overrides({"gemma3:27b": 32768})
        recs = HealthEngine()._check_kv_cache_bloat([_node(GEMMA)])
        assert len(recs) == 1
        r = recs[0]
        m = r.data["bloated_models"][0]
        assert m["cause"] == "inert_override"
        assert (m["context_length"], m["configured_context"]) == (131072, 32768)
        # The per-model action, and explicitly NOT the node-wide env change that
        # recovers less while slowing every co-resident model.
        assert "ollama stop" in r.fix
        assert "OLLAMA_NUM_PARALLEL" not in r.fix
        # and it must hand the condition to the check that owns it
        assert "num_ctx_override_inert" in r.fix

    def test_ceiling_follows_the_configured_context(self, overrides):
        overrides({"gemma3:27b": 32768})
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA)])[0]
        kv = 22.2 - 13.5
        # A reload to 32768 keeps a quarter of the KV, so three quarters returns.
        assert r.data["recoverable_gb_max"] == pytest.approx(kv * 0.75, abs=0.1)

    def test_parallelism_is_named_only_when_it_is_the_lever(self, overrides):
        overrides({"gemma3:27b": 131072})  # resident == configured
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA, num_parallel=4)])[0]
        m = r.data["bloated_models"][0]
        assert m["cause"] == "parallelism"
        assert "OLLAMA_NUM_PARALLEL 2" in r.fix
        # 4 -> 2 halves KV.  The old text promised kv/8 beside this same advice.
        kv = 22.2 - 13.5
        assert r.data["recoverable_gb_max"] == pytest.approx(kv * 0.5, abs=0.1)

    def test_lowering_parallelism_states_its_cost(self, overrides):
        overrides({"gemma3:27b": 131072})
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA, num_parallel=4)])[0]
        # Memory bought with latency, not found -- the 2026-10-05 measurement.
        assert "TTFT" in r.fix
        assert "node-wide" in r.fix

    def test_no_lever_left_is_not_actionable(self, overrides):
        """Configured context, floor parallelism: the allocation IS the config."""
        overrides({"gemma3:27b": 131072})
        node = _node(GEMMA, num_parallel=2)
        r = HealthEngine()._check_kv_cache_bloat([node])[0]
        m = r.data["bloated_models"][0]
        assert m["cause"] == "as_configured"
        assert r.data["recoverable_gb_max"] == 0.0
        assert r.severity.value == "info"
        assert "OLLAMA_NUM_PARALLEL" not in r.fix
        assert "context_waste" in r.fix

    def test_unmanaged_model_at_high_parallelism_blames_parallelism(self, overrides):
        """No override at all -- parallelism is the only lever herd controls."""
        overrides({})
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA, num_parallel=4)])[0]
        m = r.data["bloated_models"][0]
        assert m["cause"] == "parallelism"
        assert m["configured_context"] is None

    def test_mixed_causes_offer_both_levers_reload_first(self, overrides):
        overrides({"gemma3:27b": 32768})
        # A second genuinely-bloated model with no override of its own.
        # Note gpt-oss:120b is deliberately NOT used here: at 66 GB against
        # ~58 GB of weights it is below the 1.5x threshold, which is why the
        # live card on 2026-10-09 listed only gemma3.
        node = _node(GEMMA, ("qwen3-coder:30b", "30B", 30.0, 131072), num_parallel=4)
        r = HealthEngine()._check_kv_cache_bloat([node])[0]
        causes = {m["name"]: m["cause"] for m in r.data["bloated_models"]}
        assert causes["gemma3:27b"] == "inert_override"
        assert causes["qwen3-coder:30b"] == "parallelism"
        # The cheap per-model action is offered before the node-wide one.
        assert r.fix.index("ollama stop") < r.fix.index("OLLAMA_NUM_PARALLEL")


class TestNoFalsePositives:
    def test_silent_when_vram_matches_weights(self, overrides):
        overrides({})
        node = _node(("gemma3:27b", "27B", 13.6, 8192))
        assert HealthEngine()._check_kv_cache_bloat([node]) == []

    def test_silent_for_offline_node(self, overrides):
        overrides({"gemma3:27b": 32768})
        node = _node(GEMMA)
        node.status = SimpleNamespace(value="offline")
        assert HealthEngine()._check_kv_cache_bloat([node]) == []

    def test_unparseable_parameter_size_is_skipped(self, overrides):
        overrides({})
        node = _node(("weird:model", "", 40.0, 131072))
        assert HealthEngine()._check_kv_cache_bloat([node]) == []


class TestDescription:
    def test_does_not_assert_a_single_cause(self, overrides):
        overrides({"gemma3:27b": 32768})
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA)])[0]
        # The old description said the overhead "is KV cache from
        # OLLAMA_NUM_PARALLEL being too high" whatever the real cause was.
        assert "OLLAMA_NUM_PARALLEL being too high" not in r.description
        assert "context x OLLAMA_NUM_PARALLEL" in r.description

    def test_configured_context_shown_only_on_mismatch(self, overrides):
        overrides({"gemma3:27b": 131072})
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA, num_parallel=4)])[0]
        assert "vs configured" not in r.description


class TestWeightSource:
    """The reported on-disk size, not a guess from the parameter count.

    This is what the whole 2026-10-09 card turned out to be: gemma3:27b's real
    weights are 17.4 GB and the 0.5 bytes/param heuristic guessed 13.7 GB, so
    3.7 GB of weights were counted as KV cache and a 1.39x model crossed the
    1.5x threshold.
    """

    REAL = ("gemma3:27b", "27.4B", 24.2, 32768)

    def test_reported_disk_size_clears_the_phantom_bloat(self, overrides):
        overrides({})
        node = _node(self.REAL, disk_sizes={"gemma3:27b": 17.4})
        assert HealthEngine()._check_kv_cache_bloat([node]) == []

    def test_parameter_heuristic_alone_would_have_fired(self, overrides):
        """The exact false positive, reproduced: no reported size, same model."""
        overrides({})
        node = _node(self.REAL)  # older agent: no models_available_sizes
        recs = HealthEngine()._check_kv_cache_bloat([node])
        assert len(recs) == 1
        assert recs[0].data["bloated_models"][0]["expected_gb"] == 13.7

    def test_reported_size_wins_over_the_heuristic(self, overrides):
        overrides({})
        node = _node(self.REAL, disk_sizes={"gemma3:27b": 17.4})
        assert HealthEngine()._weight_size_gb(
            node.ollama.models_loaded[0], {"gemma3:27b": 17.4}
        ) == pytest.approx(17.4)

    def test_missing_entry_falls_back_not_crashes(self, overrides):
        node = _node(self.REAL, disk_sizes={"other:model": 9.0})
        assert HealthEngine()._weight_size_gb(
            node.ollama.models_loaded[0], {"other:model": 9.0}
        ) == pytest.approx(13.7)


class TestCeilingIsNotAForecast:
    """A 4x context cut on gemma3:27b returned nothing, against a 6.4 GB call.

    Gemma 3 reports sliding_window=1024 across 62 blocks, so most layers cap
    their KV at the local window and never scale with context. context x
    parallel bounds KV; it does not predict it.
    """

    def test_wording_does_not_promise_the_amount(self, overrides):
        overrides({"gemma3:27b": 32768})
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA)])[0]
        assert "At most" in r.description
        assert "possibly none" in r.description

    def test_field_name_says_it_is_a_bound(self, overrides):
        overrides({"gemma3:27b": 32768})
        r = HealthEngine()._check_kv_cache_bloat([_node(GEMMA)])[0]
        assert "recoverable_gb" not in r.data
        assert "recoverable_gb_max" in r.data

    def test_source_keeps_the_counter_evidence(self):
        """Delete the measurement and someone restores the point estimate."""
        import inspect

        doc = inspect.getdoc(HealthEngine._check_kv_cache_bloat) or ""
        assert "sliding_window" in doc
        assert "ceiling, not a forecast" in doc.lower()
