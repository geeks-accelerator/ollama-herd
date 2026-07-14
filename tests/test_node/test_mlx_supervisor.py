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
    find_mlx_launch_binary,
    find_mlx_lm_binary,
)

# ---------------------------------------------------------------------------
# Binary discovery — thin wrappers over the shared which_extended resolver.
# We patch which_extended at the source module so both wrappers exercise the
# same seam.
# ---------------------------------------------------------------------------


_WHICH_EXTENDED = "fleet_manager.node.mlx_supervisor.which_extended"


def test_find_mlx_lm_binary_returns_path_when_on_path():
    with patch(_WHICH_EXTENDED, return_value="/usr/local/bin/mlx_lm.server"):
        assert find_mlx_lm_binary() == "/usr/local/bin/mlx_lm.server"


def test_find_mlx_lm_binary_returns_none_when_missing():
    with patch(_WHICH_EXTENDED, return_value=None):
        assert find_mlx_lm_binary() is None


def test_find_mlx_launch_binary_returns_path_when_on_path():
    with patch(_WHICH_EXTENDED, return_value="/opt/homebrew/bin/mlx.launch"):
        assert find_mlx_launch_binary() == "/opt/homebrew/bin/mlx.launch"


def test_find_mlx_launch_binary_returns_none_when_missing():
    with patch(_WHICH_EXTENDED, return_value=None):
        assert find_mlx_launch_binary() is None


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


def test_base_url_uses_concrete_bind_host():
    # A concrete bind host is dialed directly for health checks.
    sup = MlxSupervisor(model="m", host="127.0.0.1", port=11441)
    assert sup.base_url == "http://127.0.0.1:11441"
    assert sup.health_host == "127.0.0.1"


def test_base_url_falls_back_to_loopback_for_wildcard_bind():
    # Binding 0.0.0.0 (required for LAN exposure / distributed rank-0) must NOT
    # make the health poll dial http://0.0.0.0 — it dials loopback instead.
    # Regression guard for the bind-vs-poll-host latent bug.
    for wildcard in ("0.0.0.0", "", "::", "*"):
        sup = MlxSupervisor(model="m", host=wildcard, port=11441)
        assert sup.health_host == "127.0.0.1", wildcard
        assert sup.base_url == "http://127.0.0.1:11441", wildcard
    # But the bind flag still carries the wildcard through to mlx_lm.server.
    sup = MlxSupervisor(model="m", host="0.0.0.0", port=11441)
    assert "--host" in sup._build_cmd("mlx_lm.server")
    cmd = sup._build_cmd("mlx_lm.server")
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"


# ---------------------------------------------------------------------------
# Distributed mode — mlx.launch wrapper around the inner mlx_lm.server command
# ---------------------------------------------------------------------------


_FIND_LAUNCH = "fleet_manager.node.mlx_supervisor.find_mlx_launch_binary"
_GET_LOCAL_IP = "fleet_manager.node.mlx_supervisor.MlxSupervisor._resolved_hosts"


def test_build_cmd_standalone_is_not_wrapped():
    # No backend ⇒ the command is exactly the inner server invocation.
    sup = MlxSupervisor(model="m", port=11440)
    assert sup.is_distributed is False
    cmd = sup._build_cmd("/bin/mlx_lm.server")
    assert cmd[0] == "/bin/mlx_lm.server"
    assert "mlx.launch" not in " ".join(cmd)
    assert "--pipeline" not in cmd


def test_build_cmd_ring_wraps_with_mlx_launch():
    sup = MlxSupervisor(
        model="m", port=11440, host="0.0.0.0",
        backend="ring", hosts="10.0.0.2", pipeline=True,
    )
    with patch(_FIND_LAUNCH, return_value="/opt/homebrew/bin/mlx.launch"), \
         patch(_GET_LOCAL_IP, return_value="10.0.0.1,10.0.0.2"):
        cmd = sup._build_cmd("/bin/mlx_lm.server")
    # Launcher first, then backend + hosts + env + separator, then inner server.
    assert cmd[0] == "/opt/homebrew/bin/mlx.launch"
    assert cmd[cmd.index("--backend") + 1] == "ring"
    assert cmd[cmd.index("--hosts") + 1] == "10.0.0.1,10.0.0.2"
    # MLX_METAL_FAST_SYNCH must be propagated to remote ranks via --env.
    assert "--env" in cmd
    assert cmd[cmd.index("--env") + 1] == "MLX_METAL_FAST_SYNCH=1"
    assert "--no-verify-script" in cmd
    # The inner server command follows the -- separator, intact.
    sep = cmd.index("--")
    inner = cmd[sep + 1:]
    assert inner[0] == "/bin/mlx_lm.server"
    assert inner[inner.index("--host") + 1] == "0.0.0.0"
    assert "--pipeline" in inner  # pipeline flag rides the inner command


def test_build_cmd_jaccl_uses_hostfile_not_hosts():
    sup = MlxSupervisor(
        model="m", port=11440, backend="jaccl", hostfile="/etc/cluster.json",
    )
    with patch(_FIND_LAUNCH, return_value="/bin/mlx.launch"):
        cmd = sup._build_cmd("/bin/mlx_lm.server")
    assert cmd[cmd.index("--backend") + 1] == "jaccl"
    assert cmd[cmd.index("--hostfile") + 1] == "/etc/cluster.json"
    assert "--hosts" not in cmd


def test_build_cmd_tensor_default_omits_pipeline():
    sup = MlxSupervisor(model="m", backend="ring", hosts="10.0.0.2", pipeline=False)
    with patch(_FIND_LAUNCH, return_value="/bin/mlx.launch"), \
         patch(_GET_LOCAL_IP, return_value="10.0.0.1,10.0.0.2"):
        cmd = sup._build_cmd("/bin/mlx_lm.server")
    assert "--pipeline" not in cmd


def test_build_cmd_distributed_raises_when_launch_missing():
    sup = MlxSupervisor(model="m", backend="ring", hosts="10.0.0.2", pipeline=True)
    with patch(_FIND_LAUNCH, return_value=None), \
         patch(_GET_LOCAL_IP, return_value="10.0.0.1,10.0.0.2"), \
         pytest.raises(RuntimeError, match="mlx.launch not found"):
        sup._build_cmd("/bin/mlx_lm.server")


def test_resolved_hosts_prepends_local_ip_and_dedupes():
    sup = MlxSupervisor(model="m", backend="ring", hosts="10.0.0.2, 10.0.0.1 ,10.0.0.3")
    with patch(
        "fleet_manager.common.system_metrics.get_local_ip",
        return_value="10.0.0.1",
    ):
        # self (10.0.0.1) first, peers appended, duplicate self dropped
        assert sup._resolved_hosts() == "10.0.0.1,10.0.0.2,10.0.0.3"


# ---------------------------------------------------------------------------
# node_count — hosts this server spans (dashboard "distributed" badge)
# ---------------------------------------------------------------------------


def test_node_count_standalone_is_one():
    sup = MlxSupervisor(model="m")
    assert sup.is_distributed is False
    assert sup.node_count == 1


def test_node_count_ring_counts_peers_plus_self():
    sup = MlxSupervisor(model="m", backend="ring", hosts="10.0.0.2,10.0.0.3")
    # two peers + this node = 3, computed from config without network I/O
    assert sup.node_count == 3


def test_node_count_ring_dedupes_peers():
    sup = MlxSupervisor(model="m", backend="ring", hosts="10.0.0.2, 10.0.0.2 ")
    assert sup.node_count == 2  # one distinct peer + self


def test_node_count_jaccl_counts_hostfile_entries(tmp_path):
    hf = tmp_path / "cluster.json"
    hf.write_text('[{"ssh":"a"},{"ssh":"b"},{"ssh":"c"},{"ssh":"d"}]')
    sup = MlxSupervisor(model="m", backend="jaccl", hostfile=str(hf))
    assert sup.node_count == 4


def test_node_count_jaccl_unreadable_hostfile_is_unknown():
    sup = MlxSupervisor(model="m", backend="jaccl", hostfile="/nonexistent/cluster.json")
    assert sup.node_count == 0  # unknown, not a wrong count


def test_statuses_surface_distributed_fields():
    from fleet_manager.node.mlx_supervisor import MlxServerSpec, MlxSupervisorSet

    spec = MlxServerSpec(
        model="m", port=11440, backend="ring", hosts="10.0.0.2", pipeline=True,
    )
    s = MlxSupervisorSet([spec])
    # No start() — statuses() reflects the configured (not-yet-running) server.
    s._children[11440] = s._make_child(spec)
    st = s.statuses()[0]
    assert st.distributed is True
    assert st.backend == "ring"
    assert st.node_count == 2  # one peer + self


def test_statuses_standalone_reports_single_node():
    from fleet_manager.node.mlx_supervisor import MlxServerSpec, MlxSupervisorSet

    spec = MlxServerSpec(model="m", port=11440)
    s = MlxSupervisorSet([spec])
    s._children[11440] = s._make_child(spec)
    st = s.statuses()[0]
    assert st.distributed is False
    assert st.backend == ""
    assert st.node_count == 1


@pytest.mark.asyncio
async def test_start_fails_when_distributed_and_launch_missing():
    sup = MlxSupervisor(model="m", backend="ring", hosts="10.0.0.2", pipeline=True)
    with patch(
        "fleet_manager.node.mlx_supervisor.find_mlx_lm_binary",
        return_value="/bin/mlx_lm.server",
    ), patch(_FIND_LAUNCH, return_value=None):
        result = await sup.start()
    assert result is False
    assert sup._status == "stopped"
    assert "mlx.launch not found" in sup._status_reason


# ---------------------------------------------------------------------------
# MlxServerSpec.from_dict — distributed field parsing + validation
# ---------------------------------------------------------------------------


def test_from_dict_parses_distributed_fields():
    from fleet_manager.node.mlx_supervisor import MlxServerSpec

    spec = MlxServerSpec.from_dict({
        "model": "m", "port": 11440,
        "backend": "ring", "hosts": "10.0.0.2", "pipeline": True,
    })
    assert spec.backend == "ring"
    assert spec.hosts == "10.0.0.2"
    assert spec.pipeline is True


def test_from_dict_defaults_to_standalone():
    from fleet_manager.node.mlx_supervisor import MlxServerSpec

    spec = MlxServerSpec.from_dict({"model": "m", "port": 11440})
    assert spec.backend == ""
    assert spec.hosts == "" and spec.hostfile == "" and spec.pipeline is False


def test_from_dict_rejects_unknown_backend():
    from fleet_manager.node.mlx_supervisor import MlxServerSpec

    with pytest.raises(ValueError, match="invalid backend"):
        MlxServerSpec.from_dict({"model": "m", "port": 11440, "backend": "carrier-pigeon"})


def test_from_dict_ring_requires_hosts():
    from fleet_manager.node.mlx_supervisor import MlxServerSpec

    with pytest.raises(ValueError, match="requires 'hosts'"):
        MlxServerSpec.from_dict({"model": "m", "port": 11440, "backend": "ring"})


def test_from_dict_jaccl_requires_hostfile():
    from fleet_manager.node.mlx_supervisor import MlxServerSpec

    with pytest.raises(ValueError, match="requires 'hostfile'"):
        MlxServerSpec.from_dict({"model": "m", "port": 11440, "backend": "jaccl"})


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
