"""Swap and compressed memory: what pressure stops showing once paging settles.

On 2026-10-04 herd reported the Mac mini at ``warn`` with ~10 GB available while
swap sat at 96.6% and a 29 GB model was paged out wholesale. The heartbeat had
no swap field at all and ``compressed_gb`` was hard-coded ``0.0``, so nothing in
herd could say so.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from fleet_manager.common import system_metrics
from fleet_manager.models.node import MemoryMetrics
from fleet_manager.server.health_engine import HealthEngine, Severity
from tests.conftest import make_node
from tests.test_server.test_health_engine import FakeRegistry

GiB = 1024**3


def _sysctl_by_name(values: dict[str, str], seen: list[str]):
    """A ``subprocess.run`` stand-in answering ``sysctl -n <name>`` per name."""

    def run(cmd, **kwargs):
        seen.append(cmd[-1])
        return SimpleNamespace(stdout=values.get(cmd[-1], ""), stderr="", returncode=0)

    return run


class TestCollection:
    def test_macos_reports_swap_and_compressor_occupancy(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(system_metrics.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            system_metrics.psutil,
            "swap_memory",
            lambda: SimpleNamespace(used=30 * GiB, total=31 * GiB),
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            _sysctl_by_name(
                {
                    "kern.memorystatus_vm_pressure_level": "2",
                    "vm.compressor_bytes_used": str(15 * GiB),
                },
                seen,
            ),
        )
        m = system_metrics.get_memory_metrics()
        assert (m.swap_used_gb, m.swap_total_gb, m.compressed_gb) == (30.0, 31.0, 15.0)
        # Occupancy, not the logical "stored in compressor" (58.8 GB vs 15.5 GB
        # on the day): occupancy is what the rest of the system cannot use.
        assert "vm.compressor_bytes_used" in seen

    def test_compressor_is_macos_only(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(system_metrics.platform, "system", lambda: "Linux")
        monkeypatch.setattr(subprocess, "run", _sysctl_by_name({}, seen))
        assert system_metrics._get_compressed_bytes() == 0
        assert seen == []

    def test_unreadable_sources_report_zero_rather_than_fail_the_heartbeat(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no")

        monkeypatch.setattr(system_metrics.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(system_metrics.psutil, "swap_memory", boom)
        monkeypatch.setattr(subprocess, "run", boom)
        assert system_metrics._get_swap_bytes() == (0, 0)
        assert system_metrics._get_compressed_bytes() == 0

    def test_older_agent_heartbeat_still_validates(self):
        m = MemoryMetrics(total_gb=48.0, used_gb=12.0, available_gb=20.0)
        assert (m.swap_used_gb, m.swap_total_gb, m.compressed_gb) == (0.0, 0.0, 0.0)


def _node(swap_used: float, swap_total: float = 0.0, compressed: float = 0.0, total=48.0):
    node = make_node("mini", memory_total=total, memory_used=12.0)
    node.memory.swap_used_gb = swap_used
    node.memory.swap_total_gb = swap_total or swap_used
    node.memory.compressed_gb = compressed
    return node


class TestSwapCheck:
    def test_swap_past_half_of_ram_warns_with_the_figures(self):
        """The 2026-10-04 state: 30 GB swapped on a 48 GB node."""
        recs = HealthEngine()._check_swap_usage([_node(30.1, 31.1, compressed=15.5)])
        assert len(recs) == 1
        rec = recs[0]
        assert (rec.check_id, rec.severity, rec.node_id) == ("swap_usage", Severity.WARNING, "mini")
        assert "30.1 GB of swap" in rec.description
        assert "15.5 GB of RAM holding compressed memory" in rec.description
        assert rec.data["swap_used_gb"] == 30.1

    def test_modest_dormant_swap_is_quiet(self):
        """2026-10-02 after recovery: 17.5 GB swapped, 77% of memory free."""
        assert HealthEngine()._check_swap_usage([_node(17.5, 18.4)]) == []

    def test_a_full_but_small_swap_file_is_not_the_signal(self):
        """macOS grows swap on demand, so used/total is ~100% whenever any swap
        exists. Judged against swap total, 1 GB of 1 GB would warn."""
        assert HealthEngine()._check_swap_usage([_node(1.0, 1.0)]) == []

    def test_nodes_without_swap_data_are_skipped(self):
        bare = make_node("old-agent", memory_total=48.0, memory_used=40.0)
        no_memory = SimpleNamespace(node_id="x", memory=None)
        assert HealthEngine()._check_swap_usage([bare, no_memory]) == []

    @pytest.mark.asyncio
    async def test_it_runs_as_part_of_the_fleet_analysis(self):
        report = await HealthEngine().analyze(FakeRegistry([_node(30.0)]), None)
        assert "swap_usage" in {r.check_id for r in report.recommendations}


@pytest.mark.asyncio
async def test_the_dashboard_stream_carries_swap_and_compressor():
    """The node card reads ``/dashboard/events``, which builds its own memory
    dict rather than dumping the model. It lacked the new fields, so the swap
    line rendered blank on a node with 11 GB in swap -- found live, not here."""
    import json

    from fleet_manager.server.routes.dashboard import dashboard_events
    from tests.conftest import make_heartbeat
    from tests.test_server.test_routes import create_test_app

    app = create_test_app()
    async with app.router.lifespan_context(app):
        hb = make_heartbeat(node_id="mini", memory_total=48.0)
        hb.memory.swap_used_gb, hb.memory.swap_total_gb, hb.memory.compressed_gb = 11.2, 12.0, 6.7
        await app.state.registry.update_from_heartbeat(hb)

        async def connected():
            return False

        response = await dashboard_events(SimpleNamespace(app=app, is_disconnected=connected))
        chunk = await response.body_iterator.__anext__()
        await response.body_iterator.aclose()

    memory = json.loads(chunk.removeprefix("data: "))["nodes"][0]["memory"]
    assert (memory["swap_used_gb"], memory["swap_total_gb"], memory["compressed_gb"]) == (
        11.2,
        12.0,
        6.7,
    )
