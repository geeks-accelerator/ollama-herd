"""Offline alerting: who gets told when a node drops, and who doesn't.

The point of the feature is that an absent fleet announces itself.  The point
of these tests is that it stays quiet in the three cases where announcing
would be wrong: the feature is off, somebody is already watching, or a window
was opened moments ago.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fleet_manager.server.offline_alert import OfflineAlerter


class _Settings:
    def __init__(self, enabled=True, url="", port=11435):
        self.offline_alert = enabled
        self.offline_alert_url = url
        self.port = port


def _alerter(**kw):
    return OfflineAlerter(_Settings(**kw))


@pytest.fixture
def opened():
    """Patch subprocess spawn; yields the list of argv lists actually run."""
    calls = []

    async def fake_exec(*cmd, **kwargs):
        calls.append(list(cmd))
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        yield calls


class TestClientTracking:
    def test_counts_watchers(self):
        a = _alerter()
        assert not a.is_watched
        a.client_connected()
        a.client_connected()
        assert a.client_count == 2
        a.client_disconnected()
        assert a.is_watched
        a.client_disconnected()
        assert not a.is_watched

    def test_unbalanced_disconnect_cannot_go_negative(self):
        """A negative count would read as 'watched' forever and mute the alert.

        The SSE stream can be torn down on a path that skips the paired
        increment; failing open into silence is the exact failure this
        feature exists to remove, so the counter clamps at zero.
        """
        a = _alerter()
        a.client_disconnected()
        a.client_disconnected()
        assert a.client_count == 0
        a.client_connected()
        assert a.is_watched


class TestOpensBrowser:
    async def test_opens_when_nobody_is_watching(self, opened):
        await _alerter().node_went_offline("mac-mini")
        assert len(opened) == 1
        assert any("11435/dashboard" in part for part in opened[0])

    async def test_silent_when_disabled(self, opened):
        await _alerter(enabled=False).node_went_offline("mac-mini")
        assert opened == []

    async def test_silent_when_a_dashboard_is_connected(self, opened):
        """A connected page notifies from the SSE payload on its own."""
        a = _alerter()
        a.client_connected()
        await a.node_went_offline("mac-mini")
        assert opened == []

    async def test_cooldown_blocks_a_flapping_node(self, opened):
        """A node flapping must not spawn one browser tab per transition."""
        a = _alerter()
        await a.node_went_offline("mac-mini")
        await a.node_went_offline("mac-mini")
        await a.node_went_offline("other-node")
        assert len(opened) == 1

    async def test_reopens_after_cooldown(self, opened):
        a = _alerter()
        await a.node_went_offline("mac-mini")
        a._last_open_at -= 10_000  # well past REOPEN_COOLDOWN_S
        await a.node_went_offline("mac-mini")
        assert len(opened) == 2

    async def test_configured_url_wins(self, opened):
        a = _alerter(url="http://herd.local:9999/dashboard")
        await a.node_went_offline("mac-mini")
        assert any("herd.local:9999" in part for part in opened[0])

    async def test_launch_failure_does_not_raise(self):
        """The caller holds the registry lock and must keep monitoring."""
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("boom")):
            await _alerter().node_went_offline("mac-mini")  # must not raise

    async def test_missing_opener_does_not_raise(self, opened):
        with patch("fleet_manager.server.offline_alert._browser_command", return_value=None):
            await _alerter().node_went_offline("mac-mini")
        assert opened == []


class TestRegistryIntegration:
    async def test_offline_transition_fires_callback_once(self):
        """Only the online -> offline edge alerts, not every monitor pass."""
        import time

        from fleet_manager.models.config import ServerSettings
        from fleet_manager.models.node import NodeStatus
        from fleet_manager.server.registry import NodeRegistry

        registry = NodeRegistry(ServerSettings())
        fired = []

        async def on_offline(node_id):
            fired.append(node_id)

        registry.on_node_offline = on_offline

        node = MagicMock()
        node.node_id = "mac-mini"
        node.status = NodeStatus.ONLINE
        node.last_heartbeat = time.time() - 9999
        node.missed_heartbeats = 0
        registry._nodes = {"mac-mini": node}

        # One monitor pass, driven directly so the test doesn't sleep.
        now = time.time()
        for n in registry._nodes.values():
            elapsed = now - n.last_heartbeat
            if elapsed > registry._settings.heartbeat_offline and n.status != NodeStatus.OFFLINE:
                n.status = NodeStatus.OFFLINE
                await registry._fire_offline_callback(n.node_id)

        assert fired == ["mac-mini"]

    async def test_graceful_drain_does_not_alert(self):
        """A node that announced its own shutdown is not an incident.

        Alerting on a deliberate stop is the noise that teaches people to
        dismiss the alert that matters, so only the stale-heartbeat path
        fires.  Verified live on 2026-10-02: ``launchctl bootout`` took the
        drain path and correctly stayed silent, while a SIGKILL 90s later
        went stale and did alert.
        """
        from fleet_manager.models.config import ServerSettings
        from fleet_manager.models.node import NodeStatus
        from fleet_manager.server.registry import NodeRegistry

        registry = NodeRegistry(ServerSettings())
        fired = []

        async def on_offline(node_id):
            fired.append(node_id)

        registry.on_node_offline = on_offline

        node = MagicMock()
        node.node_id = "mac-mini"
        node.status = NodeStatus.ONLINE
        registry._nodes = {"mac-mini": node}

        registry.handle_drain("mac-mini")

        assert node.status == NodeStatus.OFFLINE
        assert fired == []

    async def test_callback_exception_is_contained(self):
        """A broken callback must not stop the heartbeat monitor."""
        from fleet_manager.models.config import ServerSettings
        from fleet_manager.server.registry import NodeRegistry

        registry = NodeRegistry(ServerSettings())

        async def boom(node_id):
            raise RuntimeError("callback is broken")

        registry.on_node_offline = boom
        await registry._fire_offline_callback("mac-mini")  # must not raise


class TestAlertFavicon:
    """The tab badge is the half of the alert that needs no permission.

    Notification permission is frequently never granted (and in Chrome can
    only be requested from a user gesture), so a red favicon plus a title
    prefix is what a backgrounded dashboard can always show.
    """

    def _client(self):
        from fastapi.testclient import TestClient

        from fleet_manager.models.config import ServerSettings
        from fleet_manager.server.app import create_app

        return TestClient(create_app(ServerSettings()))

    def test_alert_favicon_is_served(self):
        r = self._client().get("/favicon-alert.svg")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")

    def test_alert_favicon_is_red_and_normal_is_not(self):
        """A badge that looks identical to the normal state is not a badge."""
        c = self._client()
        alert = c.get("/favicon-alert.svg").text
        normal = c.get("/favicon.svg").text
        assert "#ef4444" in alert and "#6c63ff" not in alert
        assert "#6c63ff" in normal and "#ef4444" not in normal

    def test_both_favicons_are_the_same_artwork(self):
        """Only the colour may differ — a different glyph would read as a
        different site rather than the same fleet in trouble."""
        c = self._client()
        alert = c.get("/favicon-alert.svg").text
        normal = c.get("/favicon.svg").text
        assert alert.replace("#ef4444", "#6c63ff") == normal
