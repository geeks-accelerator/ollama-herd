"""mDNS advertise and browse — the path that fails when a network changes.

`common/discovery.py` had **zero** test coverage (`docs/issues.md` item 10), and
it is how a node finds the router at all. Its failure mode is the quiet kind:
the node simply never discovers anything and falls back to a timeout, which is
exactly what a changed network produces. This fleet has been bitten by the
adjacent version of that — `node_id` drifting with the network because macOS
derives the hostname from it (see the node-identity gotcha in CLAUDE.md).

Three behaviours are worth pinning and none of them need a real network:

* the **stale-registration retry** in `start()` — the `except` branch that
  re-creates `AsyncZeroconf` and retries with `allow_name_change=True` after a
  crashed previous process left a registration behind;
* the **timeout message**, because its whole job is to tell an operator about
  `--router-url` rather than leaving them with a bare `TimeoutError`;
* that `stop()` is called **even when discovery fails**, since it runs in a
  `finally` and a leaked `AsyncZeroconf` holds a socket.

Mocked with `MagicMock` / `AsyncMock` per the pattern in
`tests/test_node/test_mlx_client.py`, rather than adding a fixture layer.
"""

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_manager.common import discovery
from fleet_manager.common.discovery import (
    SERVICE_TYPE,
    FleetServiceAdvertiser,
    FleetServiceDiscoverer,
)


async def _note(sink):
    """Record that resolution was attempted, without resolving anything."""
    sink.append(1)


@pytest.fixture
def azc(monkeypatch):
    """Replace AsyncZeroconf with a mock, returning the list of instances made.

    A list, not a single mock, because the stale-registration path constructs a
    *second* one — which is the detail worth asserting.
    """
    made = []

    def _factory(*a, **k):
        m = MagicMock()
        m.async_register_service = AsyncMock(return_value=None)
        m.async_close = AsyncMock(return_value=None)
        m.zeroconf = MagicMock()
        made.append(m)
        return m

    monkeypatch.setattr(discovery, "AsyncZeroconf", _factory)
    monkeypatch.setattr(discovery, "AsyncServiceInfo", lambda *a, **k: MagicMock())
    monkeypatch.setattr(discovery, "get_local_ip", lambda: "10.0.0.7")
    return made


class TestAdvertiser:
    async def test_registers_once_on_the_happy_path(self, azc):
        a = FleetServiceAdvertiser(port=11435)
        await a.start()
        assert len(azc) == 1, "no need to re-create AsyncZeroconf when registration works"
        azc[0].async_register_service.assert_awaited_once()
        # no allow_name_change on the first attempt -- renaming should be the
        # fallback, not the default, or two routers silently both advertise
        assert azc[0].async_register_service.await_args.kwargs == {}

    async def test_stale_registration_is_retried_with_a_rename(self, azc, monkeypatch):
        """A crashed previous process leaves a registration; recover, don't die."""
        calls = {"n": 0}

        def _factory(*a, **k):
            m = MagicMock()
            calls["n"] += 1
            if calls["n"] == 1:
                m.async_register_service = AsyncMock(side_effect=OSError("name in use"))
            else:
                m.async_register_service = AsyncMock(return_value=None)
            m.async_close = AsyncMock(return_value=None)
            m.zeroconf = MagicMock()
            azc.append(m)
            return m

        monkeypatch.setattr(discovery, "AsyncZeroconf", _factory)
        a = FleetServiceAdvertiser(port=11435)
        await a.start()

        assert len(azc) == 2, "the failed AsyncZeroconf must be replaced, not reused"
        azc[0].async_close.assert_awaited_once()  # the stale one is closed
        assert azc[1].async_register_service.await_args.kwargs == {"allow_name_change": True}

    async def test_stop_closes_and_is_safe_before_start(self, azc):
        a = FleetServiceAdvertiser(port=11435)
        await a.stop()  # never started -- must not raise
        await a.start()
        await a.stop()
        azc[0].async_close.assert_awaited_once()

    async def test_advertises_the_local_ip_and_given_port(self, monkeypatch, azc):
        captured = {}

        def _info(stype, name, **kw):
            captured.update(kw)
            captured["type"] = stype
            captured["name"] = name
            return MagicMock()

        monkeypatch.setattr(discovery, "AsyncServiceInfo", _info)
        await FleetServiceAdvertiser(port=12345, service_name="Herd").start()
        assert captured["type"] == SERVICE_TYPE
        assert captured["name"] == f"Herd.{SERVICE_TYPE}"
        assert captured["port"] == 12345
        assert captured["addresses"] == [socket.inet_aton("10.0.0.7")]


class TestDiscoverer:
    async def test_timeout_names_the_manual_escape_hatch(self, azc, monkeypatch):
        """A bare TimeoutError leaves an operator with nothing to do."""
        monkeypatch.setattr(
            discovery,
            "AsyncServiceBrowser",
            lambda *a, **k: MagicMock(async_cancel=AsyncMock(return_value=None)),
        )
        d = FleetServiceDiscoverer()
        with pytest.raises(TimeoutError) as e:
            await d.discover(timeout=0.01)
        msg = str(e.value)
        assert "--router-url" in msg, "the message must offer the manual alternative"
        assert "mDNS" in msg

    async def test_cleans_up_even_when_discovery_fails(self, azc, monkeypatch):
        """stop() is in a `finally`; a leaked AsyncZeroconf holds a socket."""
        browser = MagicMock(async_cancel=AsyncMock(return_value=None))
        monkeypatch.setattr(discovery, "AsyncServiceBrowser", lambda *a, **k: browser)
        d = FleetServiceDiscoverer()
        with pytest.raises(TimeoutError):
            await d.discover(timeout=0.01)
        browser.async_cancel.assert_awaited_once()
        azc[0].async_close.assert_awaited_once()

    async def test_returns_the_resolved_address_and_port(self, azc, monkeypatch):
        """The success path, driven through the real handler closure.

        `discover()` keeps `result` and `found` as locals and closes over them in
        `on_state_change`, which it passes to `AsyncServiceBrowser(handlers=[...])`.
        So the honest way to exercise the success path is to capture that handler
        and invoke it the way zeroconf would — then the production closure writes
        into the production locals. Patching around it instead (an earlier draft
        of this test did) proves nothing about `discover()`.
        """
        captured = {}

        def _browser(zc, stype, handlers):
            captured["handler"] = handlers[0]
            return MagicMock(async_cancel=AsyncMock(return_value=None))

        monkeypatch.setattr(discovery, "AsyncServiceBrowser", _browser)

        async def _resolve(zeroconf, stype, name, result, found):
            result["ip"], result["port"] = "10.0.0.9", 11435
            found.set()

        d = FleetServiceDiscoverer()
        monkeypatch.setattr(d, "_resolve_service", _resolve)

        task = asyncio.create_task(d.discover(timeout=3.0))
        await asyncio.sleep(0)  # let discover() build the browser
        assert "handler" in captured, "discover() must register a state-change handler"
        captured["handler"](
            MagicMock(),
            SERVICE_TYPE,
            f"Herd.{SERVICE_TYPE}",
            discovery.ServiceStateChange.Added,
        )
        assert await task == ("10.0.0.9", 11435)

    async def test_a_removed_service_does_not_resolve(self, azc, monkeypatch):
        """Only `Added` should trigger resolution; Removed/Updated must not."""
        captured = {}

        def _browser(zc, stype, handlers):
            captured["handler"] = handlers[0]
            return MagicMock(async_cancel=AsyncMock(return_value=None))

        monkeypatch.setattr(discovery, "AsyncServiceBrowser", _browser)
        called = []
        d = FleetServiceDiscoverer()
        monkeypatch.setattr(
            d,
            "_resolve_service",
            lambda *a, **k: _note(called),
        )

        task = asyncio.create_task(d.discover(timeout=0.2))
        await asyncio.sleep(0)
        captured["handler"](MagicMock(), SERVICE_TYPE, "x", discovery.ServiceStateChange.Removed)
        with pytest.raises(TimeoutError):
            await task
        assert not called, "a Removed event must not trigger resolution"


class TestResolveService:
    async def test_sets_the_event_only_when_an_address_came_back(self):
        d = FleetServiceDiscoverer()
        result: dict = {}
        found = asyncio.Event()

        info = MagicMock()
        info.async_request = AsyncMock(return_value=None)
        info.addresses = []  # resolution produced nothing
        info.port = 11435
        import fleet_manager.common.discovery as mod

        orig = mod.AsyncServiceInfo
        mod.AsyncServiceInfo = lambda *a, **k: info
        try:
            await d._resolve_service(MagicMock(), SERVICE_TYPE, "x", result, found)
            assert not found.is_set(), "no addresses means not found"
            assert result == {}

            info.addresses = [socket.inet_aton("10.0.0.9")]
            await d._resolve_service(MagicMock(), SERVICE_TYPE, "x", result, found)
            assert found.is_set()
            assert result == {"ip": "10.0.0.9", "port": 11435}
        finally:
            mod.AsyncServiceInfo = orig


class TestServiceTypeIsConfigurable:
    """``FLEET_MDNS_SERVICE_TYPE`` / ``FLEET_NODE_MDNS_SERVICE_TYPE`` are documented
    and were read into settings, but never reached zeroconf -- every herd used the
    constant.  Found 2026-10-22 when an isolated test router advertised itself
    beside the live one under the live type.  Two herds on one LAN could not be
    kept apart, which is the whole reason the setting exists."""

    CUSTOM = "_herd-staging._tcp.local."

    async def test_advertiser_uses_the_configured_type(self, monkeypatch, azc):
        captured = {}

        def _info(stype, name, **kw):
            captured["type"], captured["name"] = stype, name
            return MagicMock()

        monkeypatch.setattr(discovery, "AsyncServiceInfo", _info)
        await FleetServiceAdvertiser(port=1, service_name="Herd", service_type=self.CUSTOM).start()
        assert captured["type"] == self.CUSTOM
        assert captured["name"] == f"Herd.{self.CUSTOM}"

    async def test_discoverer_browses_the_configured_type(self, azc, monkeypatch):
        seen = {}

        def _browser(zc, stype, handlers):
            seen["type"] = stype
            return MagicMock(async_cancel=AsyncMock(return_value=None))

        monkeypatch.setattr(discovery, "AsyncServiceBrowser", _browser)
        with pytest.raises(TimeoutError):
            await FleetServiceDiscoverer(self.CUSTOM).discover(timeout=0.01)
        assert seen["type"] == self.CUSTOM

    def test_both_entry_points_pass_the_setting_through(self):
        """The wiring is exactly what was missing, so pin it at the call sites."""
        import inspect

        from fleet_manager.node import agent
        from fleet_manager.server import app

        assert "settings.mdns_service_type" in inspect.getsource(app)
        assert "FleetServiceDiscoverer(self.settings.mdns_service_type)" in inspect.getsource(agent)

    async def test_an_invalid_type_fails_fast_and_names_the_setting(self, azc, monkeypatch):
        """Configurable now, so a bad value is reachable.  It is a config error:
        no retry, no "stale service" warning, and a message naming the setting.
        Found when a 16-byte test type crashed the router behind exactly that
        misleading warning."""
        from zeroconf import BadTypeInNameException

        def _factory(*a, **k):
            m = MagicMock()
            m.async_register_service = AsyncMock(
                side_effect=BadTypeInNameException("Service name (x) must be <= 15 bytes")
            )
            m.async_close = AsyncMock(return_value=None)
            azc.append(m)
            return m

        monkeypatch.setattr(discovery, "AsyncZeroconf", _factory)
        with pytest.raises(ValueError, match="FLEET_MDNS_SERVICE_TYPE"):
            await FleetServiceAdvertiser(
                port=1, service_type="_herd-e2e-scratch._tcp.local."
            ).start()
        assert len(azc) == 1, "a config error must not be retried"
        azc[0].async_close.assert_awaited_once()
