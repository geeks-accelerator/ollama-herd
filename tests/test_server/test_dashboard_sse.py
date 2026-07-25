"""The dashboard SSE stream must not outlive its client or a bad iteration.

2026-07-25: herd's HTTP accept path stalled for 206s under heavy load. A stuck
dashboard EventSource (ESTABLISHED-but-dead Chrome connection) was implicated —
the old `while True` loop had no disconnect check, so one walked-away tab looped
forever rebuilding fleet state, and the generator had no exception guard, so a
transient could propagate out through the ASGI transport.
"""

from __future__ import annotations

import asyncio

import pytest

from fleet_manager.server.routes import dashboard


class _Req:
    """Minimal stand-in for starlette Request with a controllable peer state."""
    def __init__(self, app, disconnected_after=0):
        self.app = app
        self._calls = 0
        self._after = disconnected_after

    async def is_disconnected(self):
        self._calls += 1
        return self._calls > self._after


class _App:
    class state:
        registry = None
        queue_mgr = None
        mlx_proxy = None


def _make_app_with_empty_fleet():
    class _Reg:
        def get_all_nodes(self): return []
    class _QM:
        def get_queue_info(self): return {}
    app = _App()
    app.state.registry = _Reg()
    app.state.queue_mgr = _QM()
    return app


@pytest.mark.asyncio
async def test_stream_stops_when_client_disconnects():
    """A disconnected peer ends the loop instead of running forever."""
    req = _Req(_make_app_with_empty_fleet(), disconnected_after=1)
    resp = await dashboard.dashboard_events(req)
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c)
        if len(chunks) > 5:
            break  # safety — if the fix regressed, this loop never ends on its own
    # One frame yielded (first pass, still connected), then it stops.
    assert 1 <= len(chunks) <= 2


@pytest.mark.asyncio
async def test_stream_never_started_if_already_disconnected():
    req = _Req(_make_app_with_empty_fleet(), disconnected_after=0)
    resp = await dashboard.dashboard_events(req)
    chunks = [c async for c in resp.body_iterator]
    assert chunks == []


@pytest.mark.asyncio
async def test_bad_iteration_ends_stream_cleanly_not_via_transport():
    """An exception mid-stream is swallowed and logged, not raised out."""
    class _Boom:
        def get_all_nodes(self): raise RuntimeError("node vanished mid-serialize")
    app = _make_app_with_empty_fleet()
    app.state.registry = _Boom()
    req = _Req(app, disconnected_after=5)
    resp = await dashboard.dashboard_events(req)
    # Must complete without propagating the RuntimeError.
    chunks = [c async for c in resp.body_iterator]
    assert chunks == []
