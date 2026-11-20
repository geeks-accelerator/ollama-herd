"""Reading Ollama's configuration from where the OS keeps it.

On 2026-11-20 a Linux node with ``OLLAMA_NUM_PARALLEL=4`` in its systemd unit
reported 0: herd-node only read its own environment (and launchctl on macOS),
so the router assumed Ollama's default of 1 and sent the node one request at a
time.  Two sources now cover it: the service's configuration, and the ``-np``
Ollama actually started each model's runner with.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_manager.node import collector

UNIT_ENV = (
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/bin OLLAMA_HOST=0.0.0.0:11434 "
    'OLLAMA_NUM_PARALLEL=4 "OLLAMA_ORIGINS=a b"'
)


@pytest.fixture
def no_env(monkeypatch):
    for name in ("OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS"):
        monkeypatch.delenv(name, raising=False)


def _fake_run(stdout="", exc=None, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if exc is not None:
            raise exc
        return SimpleNamespace(stdout=stdout, returncode=0)

    return run


class TestServiceConfiguration:
    def test_linux_reads_the_systemd_unit(self, monkeypatch, no_env):
        calls = []
        monkeypatch.setattr(collector.sys, "platform", "linux")
        monkeypatch.setattr(collector.subprocess, "run", _fake_run(UNIT_ENV, calls=calls))
        assert collector._ollama_env("OLLAMA_NUM_PARALLEL") == "4"
        assert calls[0][:3] == ["systemctl", "show", "ollama.service"]

    def test_quoted_values_keep_their_spaces(self):
        env = collector._parse_systemd_environment(UNIT_ENV)
        assert env["OLLAMA_ORIGINS"] == "a b"
        assert env["OLLAMA_HOST"] == "0.0.0.0:11434"

    def test_own_environment_wins(self, monkeypatch):
        calls = []
        monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "2")
        monkeypatch.setattr(collector.sys, "platform", "linux")
        monkeypatch.setattr(collector.subprocess, "run", _fake_run(UNIT_ENV, calls=calls))
        assert collector._ollama_env("OLLAMA_NUM_PARALLEL") == "2"
        assert calls == []

    def test_unset_in_the_unit_reads_as_unset(self, monkeypatch, no_env):
        monkeypatch.setattr(collector.sys, "platform", "linux")
        monkeypatch.setattr(collector.subprocess, "run", _fake_run("PATH=/usr/bin"))
        assert collector._ollama_env("OLLAMA_NUM_PARALLEL") == ""

    @pytest.mark.parametrize(
        "exc",
        [FileNotFoundError("systemctl"), subprocess.TimeoutExpired("systemctl", 5)],
    )
    def test_no_systemctl_or_a_hang_reads_as_unset(self, monkeypatch, no_env, exc):
        monkeypatch.setattr(collector.sys, "platform", "linux")
        monkeypatch.setattr(collector.subprocess, "run", _fake_run(exc=exc))
        assert collector._ollama_env("OLLAMA_NUM_PARALLEL") == ""

    def test_macos_still_asks_launchctl(self, monkeypatch, no_env):
        calls = []
        monkeypatch.setattr(collector.sys, "platform", "darwin")
        monkeypatch.setattr(collector.subprocess, "run", _fake_run("3\n", calls=calls))
        assert collector._ollama_env("OLLAMA_NUM_PARALLEL") == "3"
        assert calls[0] == ["launchctl", "getenv", "OLLAMA_NUM_PARALLEL"]

    def test_other_platforms_shell_out_to_nothing(self, monkeypatch, no_env):
        calls = []
        monkeypatch.setattr(collector.sys, "platform", "win32")
        monkeypatch.setattr(collector.subprocess, "run", _fake_run("x", calls=calls))
        assert collector._ollama_env("OLLAMA_NUM_PARALLEL") == ""
        assert calls == []

    def test_looked_up_once(self, monkeypatch, no_env):
        calls = []
        monkeypatch.setattr(collector.sys, "platform", "linux")
        monkeypatch.setattr(collector.subprocess, "run", _fake_run(UNIT_ENV, calls=calls))
        collector._ollama_env("OLLAMA_NUM_PARALLEL")
        collector._ollama_env("OLLAMA_NUM_PARALLEL")
        assert len(calls) == 1


class _Proc:
    def __init__(self, cmdline, parent_name="ollama"):
        self.info = {"name": cmdline[0].rsplit("/", 1)[-1], "cmdline": cmdline}
        self._parent = SimpleNamespace(name=lambda: parent_name) if parent_name else None

    def parent(self):
        return self._parent


def _runner(np, parent="ollama"):
    return _Proc(
        ["/usr/local/lib/ollama/llama-server", "--model", "m", "-c", "8192", "-np", str(np)], parent
    )


class TestRunnerParallelism:
    def _with(self, monkeypatch, procs):
        psutil = pytest.importorskip("psutil")
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))

    def test_reads_np_from_ollamas_runner(self, monkeypatch):
        self._with(monkeypatch, [_runner(4)])
        assert collector._runner_num_parallel() == 4

    def test_largest_across_runners(self, monkeypatch):
        """A family Ollama forces to 1 must not hide the node's real value."""
        self._with(monkeypatch, [_runner(1), _runner(4)])
        assert collector._runner_num_parallel() == 4

    def test_someone_elses_llama_server_is_ignored(self, monkeypatch):
        self._with(monkeypatch, [_runner(8, parent="bash")])
        assert collector._runner_num_parallel() == 0

    def test_nothing_loaded_reads_as_unknown(self, monkeypatch):
        self._with(monkeypatch, [_Proc(["/usr/bin/python3", "x.py"])])
        assert collector._runner_num_parallel() == 0


def _ollama():
    ollama = MagicMock()
    for name, value in (
        ("get_running_models", []),
        ("get_available_models", []),
        ("get_version", "0.40.16"),
        ("get_available_model_sizes", {}),
        ("get_available_model_meta", {}),
    ):
        setattr(ollama, name, AsyncMock(return_value=value))
    return ollama


@pytest.mark.asyncio
async def test_heartbeat_prefers_what_the_runner_was_started_with(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "2")
    monkeypatch.setattr(collector, "_runner_num_parallel", lambda: 4)
    payload = await collector.collect_heartbeat("n1", _ollama())
    assert payload.ollama.num_parallel == 4


@pytest.mark.asyncio
async def test_heartbeat_falls_back_to_the_unit_before_anything_loads(monkeypatch, no_env):
    monkeypatch.setattr(collector, "_runner_num_parallel", lambda: 0)
    monkeypatch.setattr(collector.sys, "platform", "linux")
    monkeypatch.setattr(collector.subprocess, "run", _fake_run(UNIT_ENV))
    payload = await collector.collect_heartbeat("n1", _ollama())
    assert payload.ollama.num_parallel == 4
