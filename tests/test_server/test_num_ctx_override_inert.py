"""A num_ctx override that silently never applies must be visible state.

FLEET_NUM_CTX_OVERRIDES only takes effect on a cold load, so the router correctly
refuses to shrink a resident model.  The failure is that nothing then triggers that
cold load: on 2026-09-28 gemma3:27b ran at 131072 instead of its configured 32768
for hours (4x the intended KV, 28 GB), and the only signal was one log line emitted
earlier carrying a by-then-wrong value.
"""

import json
from types import SimpleNamespace

import pytest

from fleet_manager.server.health_engine import HealthEngine
from fleet_manager.server.streaming import StreamingProxy


def _node(node_id: str, loaded: list[tuple[str, int]]):
    return SimpleNamespace(
        node_id=node_id,
        ollama=SimpleNamespace(
            models_loaded=[
                SimpleNamespace(name=n, context_length=c, size_gb=1.0) for n, c in loaded
            ]
        ),
    )


@pytest.fixture
def env(monkeypatch):
    """Drive the check the way it really reads config: from env.

    An earlier version of this test invented `_settings` / `_registry` attributes on
    HealthEngine and passed while the production path raised AttributeError — the
    engine is stateless and `analyze` takes registry + trace_store as arguments. The
    green suite proved only that the code matched the test.
    """

    def _set(overrides, dynamic=True):
        monkeypatch.setenv("FLEET_DYNAMIC_NUM_CTX", "true" if dynamic else "false")
        monkeypatch.setenv("FLEET_NUM_CTX_OVERRIDES", json.dumps(overrides))

    return _set


class TestOverrideInertCheck:
    def test_fires_when_resident_context_exceeds_override(self, env):
        env({"gemma3:27b": 32768})
        nodes = [_node("bb", [("gemma3:27b", 131072)])]
        recs = HealthEngine()._check_num_ctx_override_inert(nodes)
        assert len(recs) == 1
        r = recs[0]
        assert r.check_id == "num_ctx_override_inert"
        # Severity follows impact, not ratio: with memory headroom this is a
        # KV-efficiency note, because herd deliberately serves at the resident
        # context rather than forcing a reload. It only warns when capacity is
        # actually threatened.
        assert r.severity.value.upper() == "INFO"
        m = r.data["mismatched_models"][0]
        assert (m["configured"], m["resident"], m["ratio"]) == (32768, 131072, 4.0)
        # the fix must be actionable, not "requires a restart"
        assert "ollama stop gemma3:27b" in r.fix
        # and must not send the reader to `ollama ps`, which is not authoritative
        assert "-np" in r.fix

    def test_silent_when_resident_matches_override(self, env):
        env({"gemma3:27b": 32768})
        nodes = [_node("bb", [("gemma3:27b", 32768)])]
        assert HealthEngine()._check_num_ctx_override_inert(nodes) == []

    def test_silent_for_models_without_an_override(self, env):
        env({"gemma3:27b": 32768})
        nodes = [_node("bb", [("qwen3.8:27b", 147491)])]
        assert HealthEngine()._check_num_ctx_override_inert(nodes) == []

    def test_warns_only_when_capacity_is_threatened(self, env):
        """Oversized + low free memory is the one case that earns a WARNING."""
        env({"gemma3:27b": 32768})
        from types import SimpleNamespace
        tight = SimpleNamespace(
            node_id="bb",
            memory=SimpleNamespace(available_gb=5.0),
            ollama=SimpleNamespace(
                models_loaded=[
                    SimpleNamespace(name="gemma3:27b", context_length=131072, size_gb=1.0)
                ]
            ),
        )
        recs = HealthEngine()._check_num_ctx_override_inert([tight])
        assert len(recs) == 1
        assert recs[0].severity.value.upper() == "WARNING"

    def test_undersized_is_info_not_warning(self, env):
        """Less context than intended is a correctness annoyance, not a memory hazard."""
        env({"gpt-oss:120b": 131072})
        nodes = [_node("bb", [("gpt-oss:120b", 32768)])]
        recs = HealthEngine()._check_num_ctx_override_inert(nodes)
        assert len(recs) == 1
        assert recs[0].severity.value.upper() == "INFO"

    def test_silent_when_dynamic_num_ctx_disabled(self, env):
        env({"gemma3:27b": 32768}, dynamic=False)
        nodes = [_node("bb", [("gemma3:27b", 131072)])]
        assert HealthEngine()._check_num_ctx_override_inert(nodes) == []

    def test_reads_live_state_not_stale_events(self, env):
        """Must answer 'is this true now', so a corrected model clears the card."""
        env({"gemma3:27b": 32768})
        eng = HealthEngine()
        assert eng._check_num_ctx_override_inert([_node("bb", [("gemma3:27b", 131072)])])
        # operator unloaded + reloaded through the router
        assert eng._check_num_ctx_override_inert([_node("bb", [("gemma3:27b", 32768)])]) == []


class TestInertLogDedupe:
    def _proxy(self):
        p = StreamingProxy.__new__(StreamingProxy)
        p._inert_override_logged = set()
        return p

    def test_relogs_when_the_resident_context_changes(self, caplog):
        """Deduping by model alone made the log lie.

        It fired once at 32768 and then stayed silent when the model reloaded at
        131072 — so the newest line in the log reported a context that had not been
        true for hours, and read like a stale cache.
        """
        import logging

        # Both values must be genuine MISMATCHES.  This used to open with
        # (32768, 32768), which is equality -- and equality is the override
        # working, so it is now deliberately silent (see TestEqualIsNotInert).
        # Using it as the "first" line here only ever worked by accident.
        p = self._proxy()
        with caplog.at_level(logging.WARNING):
            p._log_override_inert_once("gemma3:27b", 32768, 65536, "bb")
            p._log_override_inert_once("gemma3:27b", 32768, 65536, "bb")   # dupe
            p._log_override_inert_once("gemma3:27b", 32768, 131072, "bb")  # changed
        msgs = [r.getMessage() for r in caplog.records]
        assert len(msgs) == 2, f"expected 2 distinct lines, got {msgs}"
        assert "resident at 131072" in msgs[-1]
        assert "4.0x" in msgs[-1]       # quantifies the waste
        assert "ollama stop gemma3:27b" in msgs[-1]

    def test_records_an_event_so_health_can_see_it(self):
        from fleet_manager.server.streaming import get_context_protection_events

        p = self._proxy()
        p._log_override_inert_once("gemma3:27b", 32768, 131072, "bb")
        evs = [e for e in get_context_protection_events(hours=1) if e["action"] == "override_inert"]
        assert evs, "inert override must be recorded, not only logged"
        assert evs[-1]["model"] == "gemma3:27b"
        assert evs[-1]["loaded_ctx"] == 131072


class TestRemediationDirection:
    """The remedy depends on whether the CONFIGURED number is right.

    A resident context larger than the config is not evidence that the config is too
    small — on the reference fleet gemma3 was resident at 131072, configured 32768,
    and had never used more than 896 tokens. Raising the override to match residency
    would have silenced the check while preserving the only real cost: 131072 x
    OLLAMA_NUM_PARALLEL is exactly what makes Ollama predict 341.7 GiB and take its
    evict-first path.
    """

    def _nodes(self, resident):
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                node_id="bb",
                memory=SimpleNamespace(available_gb=200.0),
                ollama=SimpleNamespace(
                    models_loaded=[
                        SimpleNamespace(name="m:1", context_length=resident, size_gb=1.0)
                    ]
                ),
            )
        ]

    def test_recommends_unloading_when_usage_is_below_the_config(self, env):
        env({"m:1": 32768})
        stats = [{"model": "m:1", "total_p99": 800, "max_total_24h": 896,
                  "request_count": 500}]
        fix = HealthEngine()._check_num_ctx_override_inert(self._nodes(131072), stats)[0].fix
        assert "ollama stop m:1" in fix
        assert "raise the override" not in fix, (
            "must not suggest increasing when observed usage is far below the config"
        )
        assert fix.startswith("Unload"), f"awkward lead-in: {fix[:40]!r}"

    def test_recommends_raising_when_usage_exceeds_the_config(self, env):
        """The one legitimate 'increase the context' case."""
        env({"m:1": 8192})
        stats = [{"model": "m:1", "total_p99": 20000, "max_total_24h": 24000,
                  "request_count": 500}]
        fix = HealthEngine()._check_num_ctx_override_inert(self._nodes(131072), stats)[0].fix
        assert "raise the override" in fix
        assert "context protection" in fix, "should say what breaks if left too small"

    def test_cross_references_context_waste_when_config_is_generous(self, env):
        """Two checks must not name different targets for the same model."""
        env({"m:1": 32768})
        stats = [{"model": "m:1", "total_p99": 800, "max_total_24h": 896,
                  "request_count": 500}]
        fix = HealthEngine()._check_num_ctx_override_inert(self._nodes(131072), stats)[0].fix
        assert "context_waste" in fix


class TestEqualIsNotInert:
    """A model resident at exactly its configured context is not a problem.

    The injection condition is `override <= already_loaded_ctx`, which is
    correct for deciding whether to inject -- no point sending a value the strip
    branch removes -- but it includes equality, and at equality the override is
    SATISFIED. The log said "cannot apply ... already resident at 32768" about a
    model resident at exactly its configured 32768, with remediation steps for a
    problem that did not exist, and recorded an `override_inert` event for it.

    The health check was always right here (`have != want`); only the log and
    the event were wrong.
    """

    def _proxy(self, overrides):
        from types import SimpleNamespace

        from fleet_manager.server.streaming import StreamingProxy

        px = StreamingProxy(
            SimpleNamespace(
                dynamic_num_ctx=True,
                num_ctx_overrides=overrides,
                max_retries=0,
                debug_request_bodies=False,
            )
        )
        return px

    def test_no_warning_when_resident_equals_configured(self, caplog):
        import logging

        px = self._proxy({"gemma3:27b": 32768})
        with caplog.at_level(logging.WARNING, logger="fleet_manager.server.streaming"):
            px._log_override_inert_once("gemma3:27b", 32768, 32768, "bb")
        assert "cannot apply" not in caplog.text, (
            "equality is the override working, not failing to apply"
        )

    def test_no_event_recorded_when_resident_equals_configured(self):
        from fleet_manager.server.streaming import get_context_protection_events

        px = self._proxy({"gemma3:27b": 32768})
        before = len(get_context_protection_events(hours=24))
        px._log_override_inert_once("gemma3:27b", 32768, 32768, "bb")
        assert len(get_context_protection_events(hours=24)) == before, (
            "an override_inert event for a satisfied override is a false record"
        )

    def test_a_real_mismatch_still_warns(self, caplog):
        """The useful case must survive: resident 4x the configured value."""
        import logging

        px = self._proxy({"gemma3:27b": 32768})
        with caplog.at_level(logging.WARNING, logger="fleet_manager.server.streaming"):
            px._log_override_inert_once("gemma3:27b", 32768, 131072, "bb")
        assert "cannot apply" in caplog.text
        assert "4.0x the configured context" in caplog.text

    def test_the_health_check_never_flagged_equality(self):
        """Pin that the check and the log now agree on what "inert" means."""
        import inspect

        from fleet_manager.server.health_engine import HealthEngine

        src = inspect.getsource(HealthEngine._check_num_ctx_override_inert)
        assert "have != want" in src
