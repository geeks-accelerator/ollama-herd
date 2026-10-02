"""Prefix-aware affinity — routing a new conversation to its warm prompt head.

Session affinity follows one conversation.  A prefix key follows a prompt head
many conversations share (system prompt + tools), so the first turn of a new
Claude Code session lands where another session already warmed ~20K tokens.

The prefix cache it chases is exact-match, which shapes every test of the key:
what reaches the backend as different tokens must hash differently.
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.server.session_affinity import (
    PREFIX_AFFINITY_MIN_TOKENS,
    SessionAffinityTracker,
    prefix_key_for,
)
from tests.conftest import make_heartbeat

# Comfortably over the threshold at ~4 chars/token.
LONG_SYSTEM = "You are a careful coding agent. " * (PREFIX_AFFINITY_MIN_TOKENS // 4)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


class _Req:
    def __init__(
        self,
        system=LONG_SYSTEM,
        tools=None,
        model="qwen3-coder:30b",
        user="hi",
        client_ip="1.1.1.1",
    ):
        self.messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user},
        ]
        self.raw_body = {"tools": tools} if tools is not None else {}
        self.model = model
        self.client_ip = client_ip


class TestPrefixKey:
    def test_same_head_from_different_clients_and_turns_shares_a_key(self):
        a = prefix_key_for(_Req(client_ip="1.1.1.1", user="fix the bug"))
        b = prefix_key_for(_Req(client_ip="2.2.2.2", user="write a test"))
        assert a and a == b and a.startswith("prefix:")

    def test_tools_are_part_of_the_head(self):
        assert prefix_key_for(_Req(tools=TOOLS)) != prefix_key_for(_Req())

    def test_tool_order_matters_because_the_cache_is_exact(self):
        """Reordered tools reach the backend as different tokens and share no
        cache — grouping them would be routing for nothing."""
        two = TOOLS + [{"type": "function", "function": {"name": "write_file"}}]
        assert prefix_key_for(_Req(tools=two)) != prefix_key_for(_Req(tools=two[::-1]))

    def test_whitespace_matters_because_the_cache_is_exact(self):
        assert prefix_key_for(_Req(system=LONG_SYSTEM)) != prefix_key_for(
            _Req(system=LONG_SYSTEM + " ")
        )

    def test_dict_key_order_does_not_matter(self):
        """Backends parse tool JSON, so key order never reaches the prompt."""
        reordered = [{"function": TOOLS[0]["function"], "type": "function"}]
        assert prefix_key_for(_Req(tools=TOOLS)) == prefix_key_for(_Req(tools=reordered))

    def test_model_is_part_of_the_head(self):
        assert prefix_key_for(_Req(model="a:1b")) != prefix_key_for(_Req(model="b:1b"))

    def test_short_heads_get_no_key(self):
        """A cold prefill of a short prompt costs less than the routing distortion."""
        assert prefix_key_for(_Req(system="Be brief.")) == ""

    def test_no_system_and_no_tools_gets_no_key(self):
        assert prefix_key_for(_Req(system=None)) == ""

    def test_claude_code_fingerprint_does_not_change_the_key(self):
        """Claude Code puts a per-request cch= hash in its system prompt. The
        Anthropic translator already normalizes it, and the key is read from the
        translated messages — so two requests differing only in it match."""
        from fleet_manager.server.anthropic_translator import (
            anthropic_system_to_text,
            anthropic_to_ollama_messages,
        )

        def via_anthropic_route(cch: str) -> str:
            raw = f"x-anthropic-billing-header: cc_version=2.1; cch={cch}; " + LONG_SYSTEM
            req = _Req(system=None)
            req.messages = anthropic_to_ollama_messages(
                [{"role": "user", "content": "hi"}],
                anthropic_system_to_text(raw),
            )
            return prefix_key_for(req)

        assert via_anthropic_route("3247f") == via_anthropic_route("a91c0") != ""


# ---------------------------------------------------------------------------
# Scoring — signal 8's second tier, through the existing decay
# ---------------------------------------------------------------------------


@pytest.fixture
def fleet():
    settings = ServerSettings()
    registry = NodeRegistry(settings)
    tracker = SessionAffinityTracker()
    return ScoringEngine(settings, registry, sessions=tracker), registry, tracker


async def _two_identical_nodes(registry, model="qwen3-coder:30b"):
    for node_id in ("a", "b"):
        await registry.update_from_heartbeat(
            make_heartbeat(
                node_id=node_id,
                memory_total=128.0,
                memory_used=40.0,
                loaded_models=[(model, 18.0)],
            )
        )


@pytest.mark.asyncio
class TestPrefixScoring:
    async def test_node_holding_the_prefix_wins_at_equal_load(self, fleet):
        scorer, registry, tracker = fleet
        await _two_identical_nodes(registry)
        tracker.remember("prefix:abc", "b")

        results = scorer.score_request("qwen3-coder:30b", {}, prefix_key="prefix:abc")
        assert results[0].node_id == "b"
        assert results[0].scores_breakdown["prefix_affinity"] == scorer.PREFIX_AFFINITY_BONUS
        assert results[1].scores_breakdown["prefix_affinity"] == 0.0

    async def test_this_conversations_pin_outranks_a_shared_prefix(self, fleet):
        """The session pin means this conversation's whole history is warm there;
        the prefix pin means only the shared head is."""
        scorer, registry, tracker = fleet
        await _two_identical_nodes(registry)
        tracker.remember("1.1.1.1|claude-sonnet", "a")
        tracker.remember("prefix:abc", "b")

        results = scorer.score_request(
            "qwen3-coder:30b",
            {},
            session_key="1.1.1.1|claude-sonnet",
            prefix_key="prefix:abc",
        )
        assert results[0].node_id == "a"
        assert scorer.PREFIX_AFFINITY_BONUS < scorer.SESSION_AFFINITY_BONUS
        a = next(r for r in results if r.node_id == "a").scores_breakdown
        assert a["session_affinity"] > 0 and a["prefix_affinity"] == 0.0

    async def test_hot_spot_guard_is_the_existing_decay(self, fleet):
        """Every client sharing one popular system prompt must not pile onto one
        node: once its queue is deep the bonus shrinks and an idle peer wins."""
        scorer, registry, tracker = fleet
        await _two_identical_nodes(registry)
        tracker.remember("prefix:abc", "a")

        deep = {"a:qwen3-coder:30b": 8}
        results = scorer.score_request("qwen3-coder:30b", deep, prefix_key="prefix:abc")
        assert results[0].node_id == "b"
        a = next(r for r in results if r.node_id == "a").scores_breakdown
        assert 0.0 < a["prefix_affinity"] < scorer.PREFIX_AFFINITY_BONUS / 4

    async def test_no_prefix_key_changes_nothing(self, fleet):
        scorer, registry, tracker = fleet
        await _two_identical_nodes(registry)
        tracker.remember("prefix:abc", "b")
        for r in scorer.score_request("qwen3-coder:30b", {}):
            assert r.scores_breakdown["prefix_affinity"] == 0.0


class TestPlumbingAndHeader:
    def test_winner_is_remembered_under_both_keys(self):
        from types import SimpleNamespace

        from fleet_manager.server.routes.routing import _remember_session_node

        tracker = SessionAffinityTracker()
        scorer = SimpleNamespace(_sessions=tracker)
        _remember_session_node(
            scorer,
            "1.1.1.1|m",
            [SimpleNamespace(node_id="b")],
            "prefix:abc",
        )
        assert tracker.preferred_node("1.1.1.1|m") == "b"
        assert tracker.preferred_node("prefix:abc") == "b"

    def test_header_reports_a_prefix_hit_distinctly(self):
        from fleet_manager.server.fleet_headers import affinity_from_breakdown

        assert affinity_from_breakdown({"session_affinity": 20.0}) == "matched"
        assert (
            affinity_from_breakdown({"session_affinity": 0.0, "prefix_affinity": 10.0}) == "prefix"
        )
        assert affinity_from_breakdown({"session_affinity": 0.0}) == "new"
