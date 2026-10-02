"""A co-tenant on the backend must be *detectable*, not just documented.

herd assumes it is the only client of its backends, and that assumption is
load-bearing: every scoring signal is derived from what herd itself dispatched.
On 2026-08-21 another process sent ~27% of Ollama's load direct to :11434 and
nothing in herd said so, because herd's own requests were genuinely healthy.

These tests pin the two properties that make the probe worth having: it does
not report herd's own processes (a check that cries wolf gets muted), and it
does report a real foreign connection (a check that reports nothing is
indistinguishable from one that is broken -- which is the whole reason the
probe is tested against a live socket here rather than only parsed offline).
"""

import os
import platform
import socket
import subprocess
import sys
import threading
import time

import pytest

from fleet_manager.node.backend_clients import (
    _is_ours,
    _parse_lsof,
    ollama_port,
    probe_backend_clients,
)

# Two lsof rows for ONE loopback connection: the client's socket and the
# server's. Only the client is a bypasser; counting both double-reports every
# peer and inventing one for Ollama itself.
_LSOF_BOTH_ENDS = """\
COMMAND     PID     USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
node      26007 neonsoul    9u  IPv4 0x81ec846ef50a9440      0t0  TCP 127.0.0.1:61641->127.0.0.1:11434 (ESTABLISHED)
ollama    56184 neonsoul    5u  IPv4 0xee0cada05a0c0d1b      0t0  TCP 127.0.0.1:11434->127.0.0.1:61641 (ESTABLISHED)
"""


class TestParsing:
    def test_only_the_client_end_is_a_peer(self):
        peers = _parse_lsof(_LSOF_BOTH_ENDS, 11434)
        assert list(peers) == [26007], "server-side row must not become a peer"
        assert peers[26007]["command"] == "node"

    def test_multiple_connections_from_one_pid_collapse_to_one_peer(self):
        rows = _LSOF_BOTH_ENDS.rstrip("\n") + (
            "\nnode      26007 neonsoul   10u  IPv4 0x1 0t0  TCP "
            "127.0.0.1:61642->127.0.0.1:11434 (ESTABLISHED)"
        )
        peers = _parse_lsof(rows, 11434)
        assert peers[26007]["connections"] == 2

    def test_connections_to_a_different_port_are_ignored(self):
        # A node agent also talks to :11439 (text embeddings) and :11440+ (MLX).
        # Those are ours and on other ports; this probe is scoped to one port.
        assert _parse_lsof(_LSOF_BOTH_ENDS, 11439) == {}

    def test_listening_rows_without_a_peer_are_ignored(self):
        rows = (
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "ollama 1 u 1u IPv4 0x1 0t0 TCP *:11434 (LISTEN)\n"
        )
        assert _parse_lsof(rows, 11434) == {}

    def test_malformed_rows_do_not_raise(self):
        assert _parse_lsof("header\ngarbage\n\nx y\n", 11434) == {}


class TestOwnership:
    @pytest.mark.parametrize(
        "command,cmdline",
        [
            ("python3.1", "/x/.venv/bin/python3 /x/.venv/bin/herd"),
            ("python3.1", "/x/.venv/bin/python3 /x/.venv/bin/herd-node"),
            ("python3.1", "python -m fleet_manager.node.agent"),
            ("python3.1", "mlx_lm.server --model foo --port 11440"),
            ("ollama", "/Applications/Ollama.app/Contents/Resources/ollama serve"),
            ("llama-server", "llama-server -c 524288 -np 4"),
        ],
    )
    def test_herd_and_its_backends_are_never_bypassers(self, command, cmdline):
        assert _is_ours(999999, command, cmdline) is True

    def test_our_own_pid_is_never_a_bypasser(self):
        assert _is_ours(os.getpid(), "python3.1", "") is True

    def test_an_unrelated_process_is_a_bypasser(self):
        assert _is_ours(999999, "node", "/usr/bin/node /opt/some-cli/gateway.js") is False

    def test_attribution_uses_argv_not_the_process_name(self):
        """A Node daemon's name is just `process.title`.

        In the 2026-08 incident that name matched an unrelated project folder
        and sent the investigation the wrong way, so ownership must be decided
        on the full command line.
        """
        assert _is_ours(999999, "herd", "/usr/bin/node /opt/other/gateway.js") is False


class TestPort:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("http://localhost:11434", 11434),
            ("http://127.0.0.1:11999", 11999),
            ("http://localhost", 11434),
            ("", 11434),
            ("not a url", 11434),
        ],
    )
    def test_port_extraction_falls_back_to_the_default(self, host, expected):
        assert ollama_port(host) == expected


class TestProbeFailsSoft:
    def test_a_probe_that_cannot_run_reports_nothing_rather_than_raising(
        self, monkeypatch
    ):
        """A heartbeat must never fail because we could not look.

        "We saw nothing" and "we could not look" are deliberately the same
        answer, because the router only alerts on a positive sighting -- so a
        blind node is a missed detection and never a false alarm.
        """
        monkeypatch.setattr(
            "fleet_manager.node.backend_clients.shutil.which", lambda _: None
        )
        assert probe_backend_clients("http://localhost:11434") == []

    def test_lsof_raising_is_swallowed(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no fork for you")

        monkeypatch.setattr(
            "fleet_manager.node.backend_clients.shutil.which", lambda _: "/usr/bin/lsof"
        )
        monkeypatch.setattr(
            "fleet_manager.node.backend_clients.subprocess.run", boom
        )
        assert probe_backend_clients("http://localhost:11434") == []


@pytest.mark.skipif(
    platform.system() == "Windows", reason="probe is lsof-based on POSIX only"
)
class TestAgainstARealSocket:
    """The detection path, end to end, against a live connection.

    Mocking lsof would test the parser twice and the thing that actually
    matters not at all: whether `lsof` as the ordinary (non-root) user can see
    another process's connections. psutil.net_connections() cannot -- it raises
    AccessDenied on macOS without root -- and that is the reason this module
    shells out instead. If that ever stops holding, this test is what says so.
    """

    def test_a_foreign_connection_is_detected_and_then_clears(self):
        if not __import__("shutil").which("lsof"):
            pytest.skip("lsof not available")

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(8)
        accepted: list = []

        def _accept():
            while True:
                try:
                    accepted.append(srv.accept()[0])
                except OSError:
                    return

        threading.Thread(target=_accept, daemon=True).start()

        # A separate process, so this is genuinely a *foreign* client: a socket
        # opened by the test process itself would be excluded by os.getpid().
        client = subprocess.Popen(
            [
                sys.executable, "-c",
                f"import socket,time\n"
                f"s=[socket.create_connection(('127.0.0.1',{port})) for _ in range(2)]\n"
                f"time.sleep(30)",
            ]
        )
        try:
            found = []
            for _ in range(50):  # lsof needs the connections to be established
                found = probe_backend_clients(f"http://127.0.0.1:{port}")
                if found:
                    break
                time.sleep(0.2)

            assert found, "a foreign process holding connections must be detected"
            peer = next((c for c in found if c.pid == client.pid), None)
            assert peer is not None, f"expected pid {client.pid}, got {found}"
            assert peer.connections == 2
            assert peer.loopback is True
            assert peer.cmdline, "argv must be captured for attribution"
        finally:
            client.kill()
            client.wait(timeout=10)
            for conn in accepted:
                conn.close()
            srv.close()

        for _ in range(50):
            if not probe_backend_clients(f"http://127.0.0.1:{port}"):
                break
            time.sleep(0.2)
        assert probe_backend_clients(f"http://127.0.0.1:{port}") == [], (
            "the peer must clear once it exits, or this check latches on forever"
        )
