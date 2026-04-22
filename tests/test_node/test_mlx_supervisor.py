"""Tests for the MLX subprocess supervisor.

We don't actually launch mlx_lm.server in these tests — that needs a real
model on disk and would take ~30s per run.  Instead we test the pieces we
can in isolation: command-line construction, binary discovery, missing-model
handling, and graceful-stop behavior with mocked subprocesses.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from fleet_manager.node.mlx_supervisor import (
    MlxSupervisor,
    find_mlx_lm_binary,
)

# ---------------------------------------------------------------------------
# find_mlx_lm_binary — returns PATH result or None, doesn't crash
# ---------------------------------------------------------------------------


_WHICH = "fleet_manager.node.mlx_supervisor.shutil.which"
_PATH_EXISTS = "fleet_manager.node.mlx_supervisor.Path.exists"


def test_find_mlx_lm_binary_returns_path_when_on_path():
    with patch(_WHICH, return_value="/usr/local/bin/mlx_lm.server"):
        assert find_mlx_lm_binary() == "/usr/local/bin/mlx_lm.server"


def test_find_mlx_lm_binary_returns_none_when_missing():
    # Neither $PATH nor the fallback install paths contain mlx_lm.server
    with patch(_WHICH, return_value=None), patch(_PATH_EXISTS, return_value=False):
        assert find_mlx_lm_binary() is None


# ---------------------------------------------------------------------------
# MlxSupervisor command-line construction
# ---------------------------------------------------------------------------


def test_build_cmd_includes_all_basic_flags():
    sup = MlxSupervisor(model="/tmp/model", port=11440)
    cmd = sup._build_cmd("/usr/local/bin/mlx_lm.server")
    assert cmd[0] == "/usr/local/bin/mlx_lm.server"
    assert "--model" in cmd and "/tmp/model" in cmd
    assert "--port" in cmd and "11440" in cmd
    assert "--host" in cmd and "127.0.0.1" in cmd
    assert "--prompt-cache-size" in cmd
    assert "--prompt-cache-bytes" in cmd


def test_build_cmd_includes_kv_bits_when_set():
    sup = MlxSupervisor(model="m", kv_bits=8)
    cmd = sup._build_cmd("mlx_lm.server")
    assert "--kv-bits" in cmd
    assert "8" in cmd
    assert "--kv-group-size" in cmd


def test_build_cmd_omits_kv_bits_when_zero():
    sup = MlxSupervisor(model="m", kv_bits=0)
    cmd = sup._build_cmd("mlx_lm.server")
    assert "--kv-bits" not in cmd


def test_build_cmd_omits_kv_bits_for_unsupported_values():
    # --kv-bits only valid for 4 or 8; anything else we silently skip
    sup = MlxSupervisor(model="m", kv_bits=3)
    cmd = sup._build_cmd("mlx_lm.server")
    assert "--kv-bits" not in cmd


def test_base_url_reflects_host_and_port():
    sup = MlxSupervisor(model="m", host="0.0.0.0", port=11441)
    assert sup.base_url == "http://0.0.0.0:11441"


# ---------------------------------------------------------------------------
# start() — early-return paths that we can test without launching a subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_fails_when_binary_missing():
    sup = MlxSupervisor(model="some-model")
    with patch(
        "fleet_manager.node.mlx_supervisor.find_mlx_lm_binary",
        return_value=None,
    ):
        result = await sup.start()
    assert result is False
    assert sup._proc is None


@pytest.mark.asyncio
async def test_start_fails_when_model_is_empty():
    sup = MlxSupervisor(model="")
    with patch(
        "fleet_manager.node.mlx_supervisor.find_mlx_lm_binary",
        return_value="/usr/local/bin/mlx_lm.server",
    ):
        result = await sup.start()
    assert result is False


# ---------------------------------------------------------------------------
# stop() — graceful termination with mocked subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_noop_when_no_proc():
    sup = MlxSupervisor(model="m")
    # Nothing started — stop() should complete cleanly and not raise
    await sup.stop()


@pytest.mark.asyncio
async def test_stop_kills_proc_when_running():
    sup = MlxSupervisor(model="m")

    # Fake subprocess: poll() returns None (running), wait() returns 0
    class _FakeProc:
        pid = 12345
        _waited = False

        def poll(self):
            # First poll says running; after terminate it's gone
            return 0 if self._waited else None

        def wait(self, timeout=None):
            self._waited = True
            return 0

    sup._proc = _FakeProc()
    sup._log_fp = None

    terminated_pids = []

    def _fake_killpg(pgid, sig):
        terminated_pids.append((pgid, sig))

    def _fake_getpgid(pid):
        return pid  # treat pgid == pid for the test

    with patch("fleet_manager.node.mlx_supervisor.os.killpg", _fake_killpg), patch(
        "fleet_manager.node.mlx_supervisor.os.getpgid", _fake_getpgid,
    ):
        await sup.stop()

    # We sent SIGTERM to the pgid
    assert terminated_pids
    assert terminated_pids[0][0] == 12345
    assert sup._proc is None


@pytest.mark.asyncio
async def test_stop_falls_back_to_sigkill_on_timeout():
    sup = MlxSupervisor(model="m")

    class _StubbornProc:
        pid = 42

        def poll(self):
            return None  # always "running" — never exits

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="mlx_lm.server", timeout=timeout)

    sup._proc = _StubbornProc()
    sup._log_fp = None

    signals_sent = []

    with patch(
        "fleet_manager.node.mlx_supervisor.os.killpg",
        lambda pgid, sig: signals_sent.append(sig),
    ), patch("fleet_manager.node.mlx_supervisor.os.getpgid", lambda pid: pid):
        await sup.stop()

    # Should have sent SIGTERM then SIGKILL
    import signal
    assert signal.SIGTERM in signals_sent
    assert signal.SIGKILL in signals_sent
