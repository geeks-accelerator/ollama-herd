"""Tests for the per-heartbeat TTL cache decorator in collector.py.

Verifies hit/miss behavior, expiry, and that cache_clear works for
test isolation.  See ``_ttl_cache`` in ``fleet_manager/node/collector.py``
for design rationale.
"""

from __future__ import annotations

from unittest.mock import patch

from fleet_manager.node.collector import _ttl_cache


class TestTTLCache:
    def test_returns_cached_value_within_ttl(self):
        call_count = [0]

        @_ttl_cache(ttl_seconds=10.0)
        def expensive():
            call_count[0] += 1
            return f"call-{call_count[0]}"

        # Anchor monotonic time so the test is deterministic
        with patch("fleet_manager.node.collector.time.monotonic", return_value=100.0):
            assert expensive() == "call-1"
            assert expensive() == "call-1"  # cache hit, no recompute
            assert expensive() == "call-1"
        assert call_count[0] == 1

    def test_recomputes_after_ttl_expires(self):
        call_count = [0]

        @_ttl_cache(ttl_seconds=10.0)
        def expensive():
            call_count[0] += 1
            return f"call-{call_count[0]}"

        with patch("fleet_manager.node.collector.time.monotonic") as mock_now:
            mock_now.return_value = 100.0
            assert expensive() == "call-1"
            mock_now.return_value = 105.0  # within TTL
            assert expensive() == "call-1"
            mock_now.return_value = 110.5  # 10.5s elapsed, past TTL
            assert expensive() == "call-2"
            mock_now.return_value = 115.0  # within new TTL
            assert expensive() == "call-2"
        assert call_count[0] == 2

    def test_cache_clear_forces_recompute(self):
        call_count = [0]

        @_ttl_cache(ttl_seconds=10.0)
        def expensive():
            call_count[0] += 1
            return call_count[0]

        with patch("fleet_manager.node.collector.time.monotonic", return_value=100.0):
            assert expensive() == 1
            assert expensive() == 1
            expensive.cache_clear()
            assert expensive() == 2
        assert call_count[0] == 2

    def test_caches_none_results(self):
        """The detection helpers can return None when no models are present —
        that's a valid result and should be cached, not retried each call."""
        call_count = [0]

        @_ttl_cache(ttl_seconds=10.0)
        def maybe_none():
            call_count[0] += 1
            return None

        with patch("fleet_manager.node.collector.time.monotonic", return_value=100.0):
            assert maybe_none() is None
            assert maybe_none() is None
            assert maybe_none() is None
        assert call_count[0] == 1, "None should be cached just like any other value"

    def test_zero_ttl_caches_within_same_tick(self):
        """Edge case: TTL=0 means cache is valid for exactly the same instant
        only.  Documented behavior — ttl_seconds=0 effectively disables caching."""
        call_count = [0]

        @_ttl_cache(ttl_seconds=0.0)
        def expensive():
            call_count[0] += 1
            return call_count[0]

        with patch("fleet_manager.node.collector.time.monotonic") as mock_now:
            mock_now.return_value = 100.0
            assert expensive() == 1
            # With TTL=0 the strict less-than comparison fails immediately
            # → recompute every call.
            mock_now.return_value = 100.0
            assert expensive() == 2

    def test_distinct_decorations_have_independent_caches(self):
        """Two separate @_ttl_cache decorations should not share storage —
        each call site needs its own cache slot."""
        a_count = [0]
        b_count = [0]

        @_ttl_cache(ttl_seconds=10.0)
        def fn_a():
            a_count[0] += 1
            return ("a", a_count[0])

        @_ttl_cache(ttl_seconds=10.0)
        def fn_b():
            b_count[0] += 1
            return ("b", b_count[0])

        with patch("fleet_manager.node.collector.time.monotonic", return_value=100.0):
            assert fn_a() == ("a", 1)
            assert fn_b() == ("b", 1)
            assert fn_a() == ("a", 1)
            assert fn_b() == ("b", 1)
        assert a_count[0] == 1
        assert b_count[0] == 1
