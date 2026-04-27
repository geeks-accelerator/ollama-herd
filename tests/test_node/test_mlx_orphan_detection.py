"""Tests for orphan mlx_lm.server detection in MlxSupervisor.

Background: 2026-04-27, an `pkill -9 -f bin/herd-node` mid-session left
mlx_lm.server children alive (Popen used start_new_session=True so they
survive the parent's death; they get reparented to launchd). The next
herd-node startup tried to spawn its own mlx_lm.server, failed to bind
because the orphan held the port, exited rc=1 — and the crash-loop logic
logged 204 "QUARANTINED" warnings over 17 hours against a process that
didn't exist while the orphan kept serving requests.

`find_orphan_mlx_pids_on_port` filters strictly: only mlx_lm.server
processes (by cmdline) that are actually bound to the configured port
(by net_connections). Other random processes on that port don't get
killed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fleet_manager.node import mlx_supervisor


def _stub_proc(pid, name, cmdline, port_bound=None):
    """Build a psutil-Process stub matching the attribute surface that
    find_orphan_mlx_pids_on_port reads."""
    proc = MagicMock()
    proc.info = {"pid": pid, "name": name, "cmdline": cmdline}
    if port_bound is not None:
        conn = MagicMock()
        conn.laddr = MagicMock()
        conn.laddr.port = port_bound
        proc.net_connections.return_value = [conn]
    else:
        proc.net_connections.return_value = []
    return proc


class TestOrphanDetection:
    def test_returns_empty_when_no_processes(self):
        with patch("psutil.process_iter", return_value=[]):
            assert mlx_supervisor.find_orphan_mlx_pids_on_port(11440) == []

    def test_finds_orphan_mlx_lm_server_on_target_port(self):
        orphan = _stub_proc(
            pid=12345,
            name="python3.14",
            cmdline=["/path/python", "/path/mlx_lm.server", "--port", "11440"],
            port_bound=11440,
        )
        with patch("psutil.process_iter", return_value=[orphan]):
            pids = mlx_supervisor.find_orphan_mlx_pids_on_port(11440)
        assert pids == [12345]

    def test_skips_mlx_processes_on_other_ports(self):
        """Don't kill the mlx_lm.server on port 11441 when looking for 11440."""
        wrong_port = _stub_proc(
            pid=22222,
            name="python",
            cmdline=["/path/mlx_lm.server", "--port", "11441"],
            port_bound=11441,
        )
        with patch("psutil.process_iter", return_value=[wrong_port]):
            assert mlx_supervisor.find_orphan_mlx_pids_on_port(11440) == []

    def test_skips_unrelated_processes_on_target_port(self):
        """A process bound to our port but NOT mlx_lm.server is left alone.
        The user might be running something else there intentionally — better
        to fail to bind and log loud than to kill an unrelated service."""
        unrelated = _stub_proc(
            pid=33333,
            name="some_other_server",
            cmdline=["./run_my_thing.sh"],
            port_bound=11440,
        )
        with patch("psutil.process_iter", return_value=[unrelated]):
            assert mlx_supervisor.find_orphan_mlx_pids_on_port(11440) == []

    def test_handles_multiple_orphans(self):
        """If somehow there are multiple mlx_lm.server processes on the same
        port (shouldn't happen, but defense in depth), return all of them."""
        # Note: realistically only ONE process can bind to the same port,
        # but psutil might briefly see two during a TIME_WAIT race.  The
        # killer should clear all of them, not just one.
        orphans = [
            _stub_proc(pid=p, name="python", cmdline=["mlx_lm.server"], port_bound=11440)
            for p in (1001, 1002)
        ]
        with patch("psutil.process_iter", return_value=orphans):
            pids = sorted(mlx_supervisor.find_orphan_mlx_pids_on_port(11440))
        assert pids == [1001, 1002]

    def test_skips_processes_with_inaccessible_cmdline(self):
        """psutil.AccessDenied / NoSuchProcess on individual procs shouldn't
        bring down the whole scan."""
        import psutil

        bad = MagicMock()
        bad.info = {"pid": 99}
        type(bad).info = patch.object  # forced lookup, simpler:
        # Easier: make .info raise on access.
        bad = MagicMock()
        bad.info.get = MagicMock(side_effect=psutil.NoSuchProcess(99))
        good = _stub_proc(
            pid=100, name="python",
            cmdline=["mlx_lm.server"], port_bound=11440,
        )
        with patch("psutil.process_iter", return_value=[bad, good]):
            pids = mlx_supervisor.find_orphan_mlx_pids_on_port(11440)
        assert pids == [100]

    def test_skips_when_psutil_unavailable(self):
        """If psutil somehow isn't importable, return [] instead of raising.
        Belt-and-suspenders: psutil is a hard dep, but defensive."""
        import sys
        original_psutil = sys.modules.get("psutil")
        sys.modules["psutil"] = None  # force ImportError on `import psutil`
        try:
            assert mlx_supervisor.find_orphan_mlx_pids_on_port(11440) == []
        finally:
            if original_psutil is not None:
                sys.modules["psutil"] = original_psutil
            else:
                sys.modules.pop("psutil", None)

    def test_processes_without_mlx_in_cmdline_skipped_even_if_port_matches(self):
        """Strict identity check: must be mlx_lm.server in cmdline AND on the
        target port.  Just port is not enough."""
        wrong_proc = _stub_proc(
            pid=44444,
            name="python",
            cmdline=["python", "-m", "http.server", "11440"],
            port_bound=11440,
        )
        with patch("psutil.process_iter", return_value=[wrong_proc]):
            assert mlx_supervisor.find_orphan_mlx_pids_on_port(11440) == []
