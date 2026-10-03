"""macOS memory pressure, and what critical pressure should actually withhold.

Two defects, found together on 2026-10-02 after a memory incident on two
devices:

1. ``_get_memory_pressure_darwin`` ran ``memory_pressure -Q`` and searched its
   output for "critical" or "warn". ``-Q`` prints only a total and
   ``System-wide memory free percentage: N%``, so neither word can ever appear
   and the function returned NORMAL unconditionally -- on herd's primary
   platform. A Mac mini reported ``normal`` at 50.9/51.2 GB of swap, load 340.

2. Because of (1), the elimination built on the signal had never run on a Mac.
   When it did run it eliminated the node outright, which on a one-node fleet
   means no candidates and a 503 on every request -- while freeing nothing,
   since the memory is Ollama's resident weights, not herd's queue.
"""

import subprocess
from types import SimpleNamespace

import pytest

from fleet_manager.common import system_metrics
from fleet_manager.common.system_metrics import _get_memory_pressure_darwin
from fleet_manager.models.node import MemoryPressure
from fleet_manager.server.serializers import model_resident_on_node


def _sysctl(stdout, stderr="", returncode=0):
    return lambda *a, **k: SimpleNamespace(
        stdout=stdout, stderr=stderr, returncode=returncode
    )


class TestDarwinPressureProbe:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1\n", MemoryPressure.NORMAL),
            ("2\n", MemoryPressure.WARN),
            ("4\n", MemoryPressure.CRITICAL),
            ("  4  ", MemoryPressure.CRITICAL),
        ],
    )
    def test_kernel_levels_map_directly(self, monkeypatch, raw, expected):
        """1/2/4 is an enum, not a scale -- there is no 3."""
        monkeypatch.setattr(subprocess, "run", _sysctl(raw))
        assert _get_memory_pressure_darwin() is expected

    def test_it_reads_the_kernel_not_memory_pressure_dash_q(self, monkeypatch):
        """Pin the actual command, because the old one could not work.

        ``memory_pressure -Q`` never prints the words the old implementation
        searched for, so any future change back to it would silently restore
        "always NORMAL".
        """
        seen = {}

        def capture(cmd, **kwargs):
            seen["cmd"] = cmd
            return SimpleNamespace(stdout="1\n", stderr="", returncode=0)

        monkeypatch.setattr(subprocess, "run", capture)
        _get_memory_pressure_darwin()
        assert "kern.memorystatus_vm_pressure_level" in seen["cmd"]
        assert "-Q" not in seen["cmd"]

    def test_the_old_output_would_have_been_unparseable(self):
        """The exact text ``-Q`` emits, to document why the old code was dead.

        Neither "critical" nor "warn" appears, so the old parse returned NORMAL
        for every possible value of the real pressure level.
        """
        real_q_output = (
            "The system has 549755813888 (33554432 pages with a page size of 16384).\n"
            "System-wide memory free percentage: 98%\n"
        ).lower()
        assert "critical" not in real_q_output
        assert "warn" not in real_q_output

    @pytest.mark.parametrize("raw", ["", "   ", "banana", "3\n", "99\n"])
    def test_unreadable_or_unknown_values_fail_open_to_normal(
        self, monkeypatch, raw
    ):
        """Guessing high on a parse failure would degrade routing for no reason.

        A wrong CRITICAL now withholds cold loads, so the fail-open direction
        matters more than it did while this was broken.
        """
        monkeypatch.setattr(subprocess, "run", _sysctl(raw))
        assert _get_memory_pressure_darwin() is MemoryPressure.NORMAL

    def test_a_raising_sysctl_does_not_propagate(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no sysctl for you")

        monkeypatch.setattr(subprocess, "run", boom)
        assert _get_memory_pressure_darwin() is MemoryPressure.NORMAL

    def test_against_the_real_kernel_on_this_machine(self):
        """Not a mock: the sysctl must exist and return a value we recognise.

        This is what would have caught the original bug -- the old version
        "passed" every mocked test while being unconditionally wrong live.
        """
        import platform

        if platform.system() != "Darwin":
            pytest.skip("macOS only")
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=5,
        )
        assert out.stdout.strip(), "sysctl key is missing on this macOS version"
        assert int(out.stdout.strip()) in system_metrics._DARWIN_PRESSURE_LEVELS
        assert _get_memory_pressure_darwin() in (
            MemoryPressure.NORMAL, MemoryPressure.WARN, MemoryPressure.CRITICAL
        )


class TestSharedResidencyHelper:
    """One definition, because the scorer and the preloader must not drift."""

    def test_ollama_resident(self):
        node = SimpleNamespace(
            ollama=SimpleNamespace(models_loaded=[SimpleNamespace(name="phi4:14b")]),
            mlx_servers=[],
        )
        assert model_resident_on_node("phi4:14b", node) is True
        assert model_resident_on_node("other:1b", node) is False

    def test_mlx_counts_only_when_healthy(self):
        node = SimpleNamespace(
            ollama=None,
            mlx_servers=[SimpleNamespace(model="some/M-4bit", status="healthy")],
        )
        assert model_resident_on_node("mlx:some/M-4bit", node) is True
        node.mlx_servers[0].status = "crashed"
        assert model_resident_on_node("mlx:some/M-4bit", node) is False

    def test_the_preloader_still_uses_the_same_function(self):
        from fleet_manager.server.model_preloader import _model_resident_on_node

        assert _model_resident_on_node is model_resident_on_node
