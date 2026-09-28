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
        assert r.severity.value.upper() == "WARNING"     # oversized is the costly direction
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

        p = self._proxy()
        with caplog.at_level(logging.WARNING):
            p._log_override_inert_once("gemma3:27b", 32768, 32768, "bb")
            p._log_override_inert_once("gemma3:27b", 32768, 32768, "bb")   # dupe
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
