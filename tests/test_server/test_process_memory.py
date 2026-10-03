"""herd measuring its own memory — the number that was missing.

When the native embedding server held 28 GB in one node agent and took a 48 GB
Mac down with its co-tenants, nothing recorded it. Heartbeats carry *system*
memory, which on a 512 GB box is dominated by Ollama's resident weights and
hides a 20 GB process leak completely. Two devices hit the same bug and neither
could produce a growth curve afterwards.

Two properties matter and both are pinned here: it reads **footprint, not RSS**
(RSS is the metric that hid the original 28 GB), and it reads **peak, not just
current** (ONNX Runtime keeps the high-water mark of the largest run a process
ever does, so one oversized request raises the floor permanently and current
then looks innocent).
"""

import platform
from types import SimpleNamespace

import pytest

from fleet_manager.common.process_memory import (
    _parse_footprint,
    _role_for,
    probe_process_memory,
)
from fleet_manager.models.node import ProcessMemory, ProcessMemoryEntry
from fleet_manager.server.health_engine import HealthEngine, Severity

# Verbatim `/usr/bin/footprint -p <pid>` output.
_REAL = """\
python3 [15346]: 64-bit    Footprint: 249 MB (16384 bytes per page)
    phys_footprint: 249 MB
    phys_footprint_peak: 381 MB
"""


class TestParsing:
    def test_current_and_peak_are_both_read(self):
        cur, peak = _parse_footprint(_REAL)
        assert round(cur, 3) == round(249 / 1024, 3)
        assert round(peak, 3) == round(381 / 1024, 3)

    def test_gb_units_are_handled(self):
        """mlx children report in GB, the agent in MB — both appear live."""
        cur, peak = _parse_footprint(
            "    phys_footprint: 17 GB\n    phys_footprint_peak: 18 GB\n"
        )
        assert (cur, peak) == (17.0, 18.0)

    @pytest.mark.parametrize("junk", ["", "no numbers here", "phys_footprint: banana"])
    def test_unparseable_output_is_zeros_not_an_exception(self, junk):
        assert _parse_footprint(junk) == (0.0, 0.0)

    @pytest.mark.parametrize(
        "cmdline,role",
        [
            ("/x/bin/python /y/bin/mlx_lm.server --model foo", "mlx"),
            ("mlx.launch --backend ring mlx_lm.server", "mlx"),
            ("/Users/x/.local/bin/mlx-qwen3-asr serve --port 11437", "transcription"),
            ("/usr/local/bin/ollama serve", "ollama"),
            ("/x/bin/python -m something.else", "child"),
        ],
    )
    def test_children_are_labelled_by_argv_not_process_name(self, cmdline, role):
        """The transcription server's process name is just "Python".

        Live on this fleet the children report as "Python" and "python3.14", so
        a name-based label would tell an operator nothing.
        """
        assert _role_for(cmdline) == role


class TestProbeFailsSoft:
    def test_a_broken_probe_returns_empty_rather_than_raising(self, monkeypatch):
        """Self-telemetry must never be why a heartbeat fails to send."""
        monkeypatch.setattr(
            "fleet_manager.common.process_memory.subprocess.run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
        )
        m = probe_process_memory()
        assert isinstance(m, ProcessMemory)
        assert m.rss_gb >= 0

    @pytest.mark.skipif(platform.system() != "Darwin", reason="footprint is macOS")
    def test_against_the_real_process_on_this_machine(self):
        """Unmocked: footprint must actually be readable as the ordinary user.

        `psutil.memory_full_info()` raises AccessDenied on macOS without root —
        the same wall backend_clients hit — which is the whole reason this
        shells out. If that ever changes, this is what says so.
        """
        if not __import__("shutil").which("footprint"):
            pytest.skip("footprint not available")
        m = probe_process_memory()
        assert m.rss_gb > 0, "RSS should always be readable for our own process"
        assert m.footprint_gb > 0, "footprint must work without root"
        assert m.peak_gb >= m.footprint_gb, "peak is a high-water mark"


def _node(node_id="bb", **pm):
    return SimpleNamespace(
        node_id=node_id, ollama=None, process_memory=ProcessMemory(**pm)
    )


@pytest.fixture
def engine(monkeypatch):
    # Keep the router's self-probe out of the way so tests assert on node data.
    monkeypatch.setattr(
        "fleet_manager.common.process_memory.probe_process_memory",
        lambda: ProcessMemory(),
    )
    return HealthEngine()


class TestHealthCheck:
    def test_silent_at_normal_size(self, engine):
        assert engine._check_process_memory([_node(footprint_gb=0.3, peak_gb=0.4)]) == []

    def test_fires_on_the_agent_process(self, engine):
        recs = engine._check_process_memory([_node(footprint_gb=9.0, peak_gb=9.0)])
        assert len(recs) == 1
        assert recs[0].check_id == "herd_process_memory"
        assert recs[0].severity is Severity.WARNING

    def test_critical_above_16gb(self, engine):
        recs = engine._check_process_memory([_node(footprint_gb=28.0, peak_gb=28.0)])
        assert recs[0].severity is Severity.CRITICAL

    def test_a_high_peak_fires_even_when_current_looks_fine(self, engine):
        """The ONNX signature, and the reason current-only would have missed it.

        One oversized request raises the high-water mark permanently; the
        process then reports modest current usage while still holding the peak.
        """
        recs = engine._check_process_memory([_node(footprint_gb=0.5, peak_gb=20.0)])
        assert recs, "a 20 GB peak must fire regardless of current usage"
        assert "high-water mark" in recs[0].description
        assert recs[0].data["processes"][0]["retained"] is True

    def test_legitimate_mlx_children_do_not_trip_it(self, engine):
        """Two mlx servers at 17 GB each are model weights, not a leak.

        Including children in the comparison would make any useful threshold
        fire permanently on this fleet.
        """
        node = _node(
            footprint_gb=0.3,
            peak_gb=0.4,
            children_footprint_gb=34.0,
            children_peak_gb=35.0,
            children=[
                ProcessMemoryEntry(pid=1, role="mlx", footprint_gb=17.3, peak_gb=18.0),
                ProcessMemoryEntry(pid=2, role="mlx", footprint_gb=16.2, peak_gb=17.0),
            ],
        )
        assert engine._check_process_memory([node]) == []

    def test_rss_is_only_a_fallback_when_footprint_is_unavailable(self, engine):
        """Off macOS there is no footprint, so RSS has to carry the signal."""
        recs = engine._check_process_memory([_node(rss_gb=12.0)])
        assert recs and recs[0].severity is Severity.WARNING

    def test_the_router_measures_itself(self, monkeypatch):
        """No heartbeat describes the router — it is a separate process."""
        monkeypatch.setattr(
            "fleet_manager.common.process_memory.probe_process_memory",
            lambda: ProcessMemory(footprint_gb=11.0, peak_gb=11.0),
        )
        recs = HealthEngine()._check_process_memory([])
        assert recs, "the router's own memory must be checked with no nodes at all"
        assert recs[0].data["processes"][0]["process"] == "herd (router)"

    def test_a_failing_router_probe_does_not_break_the_check(self, monkeypatch):
        monkeypatch.setattr(
            "fleet_manager.common.process_memory.probe_process_memory",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert HealthEngine()._check_process_memory(
            [_node(footprint_gb=0.2, peak_gb=0.2)]
        ) == []

    def test_runs_without_a_trace_store(self):
        import inspect

        head = inspect.getsource(HealthEngine.analyze).split("if trace_store:")[0]
        assert "_check_process_memory" in head
