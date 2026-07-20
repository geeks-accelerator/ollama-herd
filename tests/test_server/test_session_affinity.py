"""Session affinity — keeping a conversation on its warm prefix cache.

llama.cpp skips already-processed prompt tokens, so turn N+1 of a coding session
can cost a few hundred tokens of prefill instead of thirty thousand. Since
prefill is the only thing that stalls other requests' decode, that also removes
the interference the session inflicts on everything else on the node.
"""

from __future__ import annotations

import time

from fleet_manager.server.session_affinity import (
    SessionAffinityTracker,
    session_key_for,
)


class _Req:
    def __init__(self, tags=None, client_ip="", model="m", original_model=""):
        self.tags = tags or []
        self.client_ip = client_ip
        self.model = model
        self.original_model = original_model


class TestSessionKey:
    def test_explicit_session_tag_wins(self):
        """A client telling us its conversation id is ground truth."""
        assert session_key_for(
            _Req(tags=["session:abc123", "other"], client_ip="1.2.3.4")
        ) == "session:abc123"

    def test_falls_back_to_client_and_model(self):
        key = session_key_for(_Req(client_ip="1.2.3.4", original_model="claude-sonnet-4"))
        assert key == "1.2.3.4|claude-sonnet-4"

    def test_no_identity_means_no_affinity(self):
        """Stateless callers must score exactly as they did before signal 8."""
        assert session_key_for(_Req()) == ""


class TestTracker:
    def test_remembers_and_returns_the_node(self):
        t = SessionAffinityTracker()
        t.remember("s1", "bb")
        assert t.preferred_node("s1") == "bb"

    def test_pin_expires(self):
        """A stale pin is worse than none — it routes to a cold cache."""
        t = SessionAffinityTracker(ttl_s=0)
        t.remember("s1", "bb")
        time.sleep(0.01)
        assert t.preferred_node("s1") is None

    def test_unknown_session_has_no_pin(self):
        assert SessionAffinityTracker().preferred_node("never-seen") is None

    def test_empty_inputs_are_ignored(self):
        t = SessionAffinityTracker()
        t.remember("", "bb")
        t.remember("s1", "")
        assert t.preferred_node("") is None
        assert t.preferred_node("s1") is None

    def test_bounded_under_churn(self):
        """A busy fleet must not grow this map without limit."""
        t = SessionAffinityTracker(max_sessions=50)
        for i in range(500):
            t.remember(f"s{i}", "bb")
        assert len(t._seen) <= 50
        # The most recent session survives the eviction.
        assert t.preferred_node("s499") == "bb"


class TestScorerSignal:
    def _engine(self, tracker):
        from fleet_manager.server.scorer import ScoringEngine
        e = ScoringEngine.__new__(ScoringEngine)
        e._sessions = tracker
        return e

    def test_pinned_node_is_rewarded(self):
        t = SessionAffinityTracker(); t.remember("s1", "bb")
        e = self._engine(t)
        class N: node_id = "bb"
        assert e._score_session_affinity(N(), "s1") == e.SESSION_AFFINITY_BONUS

    def test_other_nodes_get_nothing(self):
        t = SessionAffinityTracker(); t.remember("s1", "bb")
        e = self._engine(t)
        class N: node_id = "other"
        assert e._score_session_affinity(N(), "s1") == 0.0

    def test_bonus_cannot_outweigh_thermal(self):
        """A warm prefix is worth a lot, but not worth routing into a node
        that's about to throttle. Thermal alone contributes up to 50."""
        e = self._engine(SessionAffinityTracker())
        assert e.SESSION_AFFINITY_BONUS < 50.0

    def test_disabled_tracker_is_inert(self):
        """Callers that never pass a tracker must see pre-existing behaviour."""
        e = self._engine(None)
        class N: node_id = "bb"
        assert e._score_session_affinity(N(), "s1") == 0.0
