"""Which address a machine advertises (router) or reports (node).

2026-11-19: with a macOS VPN on utun8 (10.12.128.118) and Wi-Fi on en0
(192.168.1.8), the routed probe returned the tunnel address, the router
advertised it over mDNS, and its own node's heartbeats timed out. A tunnel's
address now gives way to a private LAN address when one exists; a machine
reachable only over Tailscale keeps its Tailscale address.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from fleet_manager.common import system_metrics
from fleet_manager.common.system_metrics import _prefer_lan_over_tunnel, get_local_ip


def _net(monkeypatch, table: dict[str, tuple[str, bool]]):
    """table: interface -> (IPv4 address, is up)."""
    addrs = {
        name: [SimpleNamespace(family=socket.AF_INET, address=ip)]
        for name, (ip, _) in table.items()
    }
    stats = {name: SimpleNamespace(isup=up) for name, (_, up) in table.items()}
    monkeypatch.setattr(system_metrics.psutil, "net_if_addrs", lambda: addrs)
    monkeypatch.setattr(system_metrics.psutil, "net_if_stats", lambda: stats)
    monkeypatch.setattr(system_metrics, "_logged_preference", set())


def test_plain_lan_is_unchanged(monkeypatch):
    _net(monkeypatch, {"lo0": ("127.0.0.1", True), "en0": ("192.168.1.8", True)})
    assert _prefer_lan_over_tunnel("192.168.1.8") == "192.168.1.8"


def test_vpn_owning_the_route_gives_way_to_the_lan(monkeypatch, caplog):
    _net(
        monkeypatch,
        {
            "lo0": ("127.0.0.1", True),
            "en0": ("192.168.1.8", True),
            "utun8": ("10.12.128.118", True),
        },
    )
    with caplog.at_level("INFO"):
        assert _prefer_lan_over_tunnel("10.12.128.118") == "192.168.1.8"
        _prefer_lan_over_tunnel("10.12.128.118")
    assert caplog.text.count("rather than 10.12.128.118") == 1, "logged once, not every heartbeat"


def test_tailscale_only_keeps_its_tailscale_address(monkeypatch):
    _net(monkeypatch, {"lo": ("127.0.0.1", True), "tailscale0": ("100.96.12.34", True)})
    assert _prefer_lan_over_tunnel("100.96.12.34") == "100.96.12.34"


def test_lan_plus_tailscale_routed_over_lan_is_unchanged(monkeypatch):
    _net(monkeypatch, {"eth0": ("192.168.1.20", True), "tailscale0": ("100.96.12.34", True)})
    assert _prefer_lan_over_tunnel("192.168.1.20") == "192.168.1.20"


def test_vpn_with_only_virtual_interfaces_keeps_the_routed_address(monkeypatch):
    _net(
        monkeypatch,
        {
            "wg0": ("10.8.0.2", True),
            "docker0": ("172.17.0.1", True),
            "bridge100": ("192.168.64.1", True),
        },
    )
    assert _prefer_lan_over_tunnel("10.8.0.2") == "10.8.0.2"


def test_a_down_interface_is_not_chosen(monkeypatch):
    _net(
        monkeypatch,
        {"utun3": ("10.6.0.4", True), "en0": ("192.168.1.8", False), "en1": ("10.0.0.7", True)},
    )
    assert _prefer_lan_over_tunnel("10.6.0.4") == "10.0.0.7"


def test_physical_names_are_preferred(monkeypatch):
    _net(
        monkeypatch,
        {"ppp0": ("10.64.0.9", True), "br0": ("192.168.1.5", True), "eth0": ("192.168.2.5", True)},
    )
    assert _prefer_lan_over_tunnel("10.64.0.9") == "192.168.2.5"


def test_get_local_ip_applies_the_preference(monkeypatch):
    class Probe:
        def __init__(self, *a):
            pass

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("10.12.128.118", 5555)

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", Probe)
    _net(monkeypatch, {"en0": ("192.168.1.8", True), "utun8": ("10.12.128.118", True)})
    assert get_local_ip() == "192.168.1.8"


class TestAdvertisedAddress:
    """A router bound to one address (FLEET_HOST, the Tailscale guide's
    Tailnet-only router) listens only there and must advertise it."""

    def test_bound_host_is_advertised(self, monkeypatch):
        from fleet_manager.common.discovery import FleetServiceAdvertiser

        monkeypatch.setattr(
            "fleet_manager.common.discovery.get_local_ip", lambda **k: "192.168.1.8"
        )
        assert FleetServiceAdvertiser(11435, host="100.96.12.34")._address() == "100.96.12.34"

    @pytest.mark.parametrize("host", ["", "0.0.0.0", "::"])
    def test_wildcard_binds_advertise_the_lan_address(self, monkeypatch, host):
        from fleet_manager.common.discovery import FleetServiceAdvertiser

        monkeypatch.setattr(
            "fleet_manager.common.discovery.get_local_ip", lambda **k: "192.168.1.8"
        )
        assert FleetServiceAdvertiser(11435, host=host)._address() == "192.168.1.8"
