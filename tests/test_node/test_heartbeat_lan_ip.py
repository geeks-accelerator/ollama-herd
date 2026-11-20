"""The heartbeat logs a network outage once, not every tick (2026-11-20)."""

from __future__ import annotations

import logging

import pytest

from fleet_manager.common import system_metrics
from fleet_manager.node import collector


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(collector, "_last_lan_ip", None)


def _probe(monkeypatch, results):
    """Make the UDP probe succeed (an address) or fail (None) in order."""
    seq = iter(results)

    class Sock:
        def __init__(self, *a):
            self.ip = next(seq)

        def connect(self, addr):
            if self.ip is None:
                raise OSError(51, "Network is unreachable")

        def getsockname(self):
            return (self.ip, 1)

        def close(self):
            pass

    import socket

    monkeypatch.setattr(socket, "socket", Sock)
    monkeypatch.setattr(system_metrics, "_prefer_lan_over_tunnel", lambda ip: ip)


def test_outage_warns_once_and_recovery_logs_info(monkeypatch, caplog):
    _probe(monkeypatch, ["10.0.0.92", None, None, None, "10.0.0.92", "10.0.0.92"])
    caplog.set_level(logging.INFO)
    got = [collector._heartbeat_lan_ip() for _ in range(6)]
    assert got == ["10.0.0.92", "127.0.0.1", "127.0.0.1", "127.0.0.1", "10.0.0.92", "10.0.0.92"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Network is unreachable" in warnings[0].getMessage()
    back = [r.getMessage() for r in caplog.records if "LAN IP is back" in r.getMessage()]
    assert back == ["LAN IP is back: 10.0.0.92"]


def test_each_outage_warns_again(monkeypatch, caplog):
    _probe(monkeypatch, [None, None, "10.0.0.92", None])
    for _ in range(4):
        collector._heartbeat_lan_ip()
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 2
