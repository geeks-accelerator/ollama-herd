"""The co-tenant check, and the context_waste change that came out of the same audit.

Both exist because of incidents where every herd-side metric stayed clean:

* 2026-08-21 — another process sent ~27% of Ollama's load direct to :11434.
  Fleet decode fell 15%; the dashboard, health engine and traces all looked
  healthy, because herd's own requests were. Six wrong turns, hours lost.
* 2026-09-22 — gpt-oss:120b's per-slot context was cut while its p99 prompt was
  ~1.4K tokens. Prompts still fit 23x over, prefix-cache reuse collapsed anyway,
  TTFT went 1.0s -> 6.3s for six days, and decode throughput never moved.
"""

import json
from types import SimpleNamespace

import pytest

from fleet_manager.server.health_engine import HealthEngine, Severity


def _node(node_id="bb", clients=(), loaded=()):
    return SimpleNamespace(
        node_id=node_id,
        ollama=SimpleNamespace(
            backend_clients=list(clients),
            models_loaded=[
                SimpleNamespace(name=n, context_length=c, size_gb=1.0) for n, c in loaded
            ],
        ),
    )


def _client(pid=26007, process="node", connections=3, cmdline="/usr/bin/node gw.js"):
    return SimpleNamespace(
        pid=pid, process=process, connections=connections,
        cmdline=cmdline, loopback=True,
    )


@pytest.fixture
def engine():
    return HealthEngine()


class TestBypassCheck:
    def test_silent_when_nothing_is_bypassing(self, engine):
        assert engine._check_backend_bypass_clients([_node()]) == []

    def test_a_node_with_no_ollama_is_skipped(self, engine):
        assert engine._check_backend_bypass_clients(
            [SimpleNamespace(node_id="x", ollama=None)]
        ) == []

    def test_fires_warning_and_names_the_process(self, engine):
        recs = engine._check_backend_bypass_clients([_node(clients=[_client()])])
        assert len(recs) == 1
        rec = recs[0]
        assert rec.check_id == "backend_bypass_clients"
        assert rec.severity is Severity.WARNING
        assert "node" in rec.description and "26007" in rec.description

    def test_remedy_is_repoint_not_kill(self, engine):
        """The co-tenant may be deliberate; herd is Ollama-API compatible.

        Telling an operator to kill another team's tool is the wrong advice and
        would make the check something people learn to ignore.
        """
        rec = engine._check_backend_bypass_clients([_node(clients=[_client()])])[0]
        assert ":11435" in rec.fix
        assert "kill" not in rec.fix.lower()

    def test_remedy_mentions_the_fallback_arrival_path(self, engine):
        """It arrived by fallback, not configuration.

        An operator who only checks their config files will not find it: the
        2026-08 culprit was redirected onto the fleet when its cloud provider
        lost its API key.
        """
        rec = engine._check_backend_bypass_clients([_node(clients=[_client()])])[0]
        assert "fallback" in rec.fix.lower()

    def test_single_node_is_attributed_multi_node_is_not(self, engine):
        one = engine._check_backend_bypass_clients([_node("bb", [_client()])])[0]
        assert one.node_id == "bb"
        two = engine._check_backend_bypass_clients(
            [_node("bb", [_client()]), _node("mbp", [_client(pid=5)])]
        )[0]
        assert two.node_id is None
        assert len(two.data["clients"]) == 2

    def test_runs_without_a_trace_store(self, engine):
        """Registered with the registry-based checks, not the trace-based ones.

        A fleet with no trace data is exactly when an unaccounted co-tenant is
        least explainable by any other means, so gating this on trace_store
        would have disabled it when it matters most.
        """
        import inspect
        src = inspect.getsource(HealthEngine.analyze)
        head = src.split("if trace_store:")[0]
        assert "_check_backend_bypass_clients" in head


class TestContextWasteRespectsOperatorIntent:
    @pytest.fixture
    def stats(self):
        # Live 2026-10-02 values: ratio 24.7x and 178.1x, both > the 8x
        # threshold that used to make this an unconditional WARNING.
        return [
            {"model": "gpt-oss:120b", "total_p99": 5309, "max_total_24h": 5908,
             "request_count": 47625},
            {"model": "gemma3:27b", "total_p99": 736, "max_total_24h": 800,
             "request_count": 378},
        ]

    @pytest.fixture
    def nodes(self):
        return [_node(loaded=[("gpt-oss:120b", 131072), ("gemma3:27b", 131072)])]

    def test_unpinned_models_still_get_a_recommendation(
        self, engine, stats, nodes, monkeypatch
    ):
        monkeypatch.delenv("FLEET_NUM_CTX_OVERRIDES", raising=False)
        rec = engine._check_context_waste(stats, nodes)[0]
        assert rec.severity is Severity.WARNING
        assert "gpt-oss:120b: " in rec.fix
        assert all(not w["operator_pinned"] for w in rec.data["wasteful_models"])

    def test_a_pinned_model_is_not_told_to_shrink(
        self, engine, stats, nodes, monkeypatch
    ):
        monkeypatch.setenv(
            "FLEET_NUM_CTX_OVERRIDES",
            json.dumps({"gpt-oss:120b": 131072, "gemma3:27b": 32768}),
        )
        rec = engine._check_context_waste(stats, nodes)[0]
        assert all(w["operator_pinned"] for w in rec.data["wasteful_models"])
        assert "gpt-oss:120b: 16,384" not in rec.fix
        assert "No action recommended" in rec.fix

    def test_an_all_pinned_fleet_does_not_hold_a_standing_warning(
        self, engine, stats, nodes, monkeypatch
    ):
        """A WARNING with no available action is how a board stops being read."""
        monkeypatch.setenv(
            "FLEET_NUM_CTX_OVERRIDES",
            json.dumps({"gpt-oss:120b": 131072, "gemma3:27b": 32768}),
        )
        rec = engine._check_context_waste(stats, nodes)[0]
        assert rec.severity is Severity.INFO

    def test_a_mixed_fleet_still_warns_about_the_actionable_model(
        self, engine, stats, nodes, monkeypatch
    ):
        monkeypatch.setenv(
            "FLEET_NUM_CTX_OVERRIDES", json.dumps({"gpt-oss:120b": 131072})
        )
        rec = engine._check_context_waste(stats, nodes)[0]
        assert rec.severity is Severity.WARNING
        assert "gemma3:27b: " in rec.fix
        assert "gpt-oss:120b: 16,384" not in rec.fix

    def test_the_prefix_cache_caveat_is_stated_not_just_implied(
        self, engine, stats, nodes, monkeypatch
    ):
        """The measurement is prompt size; the cost of being wrong is cache reuse.

        An operator reading this card has to know the check cannot see the thing
        that actually broke in 2026-09, or they will act on it again.
        """
        monkeypatch.setenv(
            "FLEET_NUM_CTX_OVERRIDES", json.dumps({"gpt-oss:120b": 131072})
        )
        rec = engine._check_context_waste(stats, nodes)[0]
        assert "prefix-cache" in rec.description
        assert "TTFT" in rec.fix

    def test_malformed_override_json_does_not_break_the_check(
        self, engine, stats, nodes, monkeypatch
    ):
        monkeypatch.setenv("FLEET_NUM_CTX_OVERRIDES", "{not json")
        rec = engine._check_context_waste(stats, nodes)[0]
        assert rec.check_id == "context_waste"
