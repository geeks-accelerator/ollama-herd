"""Tests for the response cache on /dashboard/api/health.

Validates that the endpoint returns the cached payload within the TTL
window and re-runs the (expensive) HealthEngine after expiry.  Uses a
manual time-mocking approach since the cache is keyed on
``time.monotonic()`` — no asyncio sleep, no flake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from fleet_manager.server.routes import dashboard as dash


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a cleared health cache so order doesn't matter."""
    dash._HEALTH_CACHE["payload"] = None
    dash._HEALTH_CACHE["ts"] = 0.0
    yield
    dash._HEALTH_CACHE["payload"] = None
    dash._HEALTH_CACHE["ts"] = 0.0


def _make_request(registry=None, trace_store=None):
    """Stub a fastapi Request object exposing app.state attributes."""
    class _State:
        pass
    state = _State()
    state.registry = registry or object()
    state.trace_store = trace_store
    class _App:
        pass
    app = _App()
    app.state = state
    class _Req:
        pass
    req = _Req()
    req.app = app
    return req


class TestHealthEndpointCache:
    @pytest.mark.asyncio
    async def test_returns_cached_payload_within_ttl(self):
        """Two calls within TTL should run HealthEngine once and return the
        same payload from cache the second time."""
        analyze_call_count = [0]

        async def fake_analyze(*args, **kwargs):
            analyze_call_count[0] += 1
            class _Report:
                def model_dump(self):
                    return {"call": analyze_call_count[0], "recommendations": []}
            return _Report()

        with patch("fleet_manager.server.health_engine.HealthEngine") as MockEngine, \
             patch("time.monotonic", return_value=1000.0):
            engine_instance = MockEngine.return_value
            engine_instance.analyze = AsyncMock(side_effect=fake_analyze)

            r1 = await dash.dashboard_health_data(_make_request())
            r2 = await dash.dashboard_health_data(_make_request())
            r3 = await dash.dashboard_health_data(_make_request())
        assert r1 == r2 == r3 == {"call": 1, "recommendations": []}
        assert analyze_call_count[0] == 1, "HealthEngine should run only once within TTL"

    @pytest.mark.asyncio
    async def test_recomputes_after_ttl_expires(self):
        analyze_call_count = [0]

        async def fake_analyze(*args, **kwargs):
            analyze_call_count[0] += 1
            class _Report:
                def model_dump(self):
                    return {"call": analyze_call_count[0]}
            return _Report()

        with patch("fleet_manager.server.health_engine.HealthEngine") as MockEngine, \
             patch("time.monotonic") as mock_now:
            engine_instance = MockEngine.return_value
            engine_instance.analyze = AsyncMock(side_effect=fake_analyze)

            mock_now.return_value = 1000.0
            r1 = await dash.dashboard_health_data(_make_request())

            # 15 s elapsed — within 30 s TTL, expect cache hit
            mock_now.return_value = 1015.0
            r2 = await dash.dashboard_health_data(_make_request())

            # 31 s elapsed — past 30 s TTL, expect recompute
            mock_now.return_value = 1031.0
            r3 = await dash.dashboard_health_data(_make_request())

        assert r1 == {"call": 1}
        assert r2 == {"call": 1}, "Should hit cache at 15s elapsed"
        assert r3 == {"call": 2}, "Should recompute past 30s TTL"
        assert analyze_call_count[0] == 2

    @pytest.mark.asyncio
    async def test_does_not_call_engine_when_cache_warm(self):
        """Sanity check that cache-hit path doesn't even instantiate
        HealthEngine — the whole point of the cache is to skip the expensive
        work entirely."""
        async def fake_analyze(*args, **kwargs):
            class _Report:
                def model_dump(self):
                    return {"recommendations": ["ran-the-engine"]}
            return _Report()

        with patch("fleet_manager.server.health_engine.HealthEngine") as MockEngine, \
             patch("time.monotonic", return_value=2000.0):
            MockEngine.return_value.analyze = AsyncMock(side_effect=fake_analyze)

            await dash.dashboard_health_data(_make_request())
            MockEngine.assert_called_once()
            await dash.dashboard_health_data(_make_request())
            await dash.dashboard_health_data(_make_request())
            assert MockEngine.call_count == 1, (
                "HealthEngine should only be instantiated once within TTL"
            )
