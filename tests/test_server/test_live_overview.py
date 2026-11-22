"""The Overview's live data: per-bucket request series and the node-metric
sampler.  docs/plans/dashboard-live-overview.md."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.fleet_history import FleetHistory
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.trace_store import TraceStore
from tests.conftest import make_heartbeat

NOW = 1_800_000_000.0  # a fixed clock, aligned to the minute
STEP = 60


@pytest_asyncio.fixture
async def store(tmp_path):
    s = TraceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


async def _trace(store, ts, *, node="bb", model="m:1", status="completed", tokens=100, ttft=500.0):
    await store.record_trace(
        request_id=f"r{ts}{status}",
        model=model,
        original_model=model,
        node_id=node,
        status=status,
        completion_tokens=tokens,
        time_to_first_token_ms=ttft,
    )
    await store._db.execute(
        "UPDATE request_traces SET timestamp = ? WHERE id = (SELECT MAX(id) FROM request_traces)",
        (ts,),
    )
    await store._db.commit()


class TestTimeseries:
    @pytest.mark.asyncio
    async def test_window_ends_at_the_current_bucket(self, store):
        out = await store.get_timeseries(3600, STEP, now=NOW + 30)
        assert len(out["buckets"]) == 60
        assert out["buckets"][-1] == NOW  # the bucket holding "now", still filling
        assert out["buckets"][1] - out["buckets"][0] == STEP
        assert out["requests"] == [0] * 60 and out["ttft_p50_ms"] == [None] * 60

    @pytest.mark.asyncio
    async def test_buckets_split_by_model_and_node(self, store):
        await _trace(store, NOW - 50, model="a", tokens=300, ttft=100)
        await _trace(store, NOW - 40, model="a", tokens=100, ttft=300)
        await _trace(store, NOW - 30, model="b", node="mini", tokens=60, ttft=200)
        await _trace(store, NOW + 5, model="a", tokens=10)
        out = await store.get_timeseries(3600, STEP, now=NOW + 10)
        assert out["requests"][-2:] == [3, 1]
        assert out["tokens"][-2] == 460
        assert out["by_model"]["a"][-2:] == [400, 10]
        assert out["by_node"]["mini"][-2] == 60
        assert out["by_node_model"]["bb:a"][-2] == 400
        assert out["requests_by_node_model"]["mini:b"][-2] == 1
        assert out["ttft_p50_ms"][-2] == 200.0  # median of 100, 300, 200

    @pytest.mark.asyncio
    async def test_failures_match_the_error_rate_vital(self, store):
        """Failed = not completed and not retried, as get_overall_stats_24h counts
        it; retried attempts are not requests at all (they would double-count)."""
        await _trace(store, NOW - 30, status="completed")
        await _trace(store, NOW - 29, status="client_disconnected", tokens=None)
        await _trace(store, NOW - 28, status="rejected", node="", tokens=None)
        await _trace(store, NOW - 27, status="retried", tokens=None)
        out = await store.get_timeseries(3600, STEP, now=NOW + 1)
        assert out["requests"][-2] == 3
        assert out["failed"][-2] == 2
        # A rejected request has no node: counted, but never as a node's traffic.
        assert all(":" in k and not k.startswith(":") for k in out["requests_by_node_model"])

    @pytest.mark.asyncio
    async def test_old_rows_are_outside_the_window(self, store):
        await _trace(store, NOW - 7200)
        out = await store.get_timeseries(3600, STEP, now=NOW)
        assert sum(out["requests"]) == 0


def _history():
    registry = NodeRegistry(ServerSettings())
    return registry, FleetHistory(registry, QueueManager(registry=registry))


class TestFleetHistory:
    @pytest.mark.asyncio
    async def test_samples_node_metrics_as_parallel_arrays(self):
        registry, h = _history()
        await registry.update_from_heartbeat(
            make_heartbeat("bb", memory_total=100.0, memory_used=40.0, cpu_pct=12.5)
        )
        h.sample(now=NOW - 10)
        h.sample(now=NOW)
        snap = h.snapshot(60, now=NOW)
        assert snap["t"] == [NOW - 10, NOW]
        assert snap["nodes"]["bb"]["cpu"] == [12.5, 12.5]
        assert snap["nodes"]["bb"]["mem"] == [40.0, 40.0]
        assert snap["queued"] == [0, 0] and snap["running"] == [0, 0]

    @pytest.mark.asyncio
    async def test_a_node_that_joins_later_is_a_gap_not_a_zero(self):
        registry, h = _history()
        await registry.update_from_heartbeat(make_heartbeat("bb"))
        h.sample(now=NOW - 10)
        await registry.update_from_heartbeat(make_heartbeat("mini", cpu_pct=50.0))
        h.sample(now=NOW)
        assert h.snapshot(60, now=NOW)["nodes"]["mini"]["cpu"] == [None, 50.0]

    def test_window_and_bound(self):
        _, h = _history()
        for i in range(10):
            h.sample(now=NOW - 100 + i * 10)
        assert len(h.snapshot(35, now=NOW)["t"]) == 3
        assert h._samples.maxlen == int(3 * 3600 / h.interval_s) + 1  # 3 h, bounded


class TestEndpoints:
    @pytest.fixture
    def client(self, tmp_path):
        from tests.test_server.test_routes import create_test_app

        with TestClient(create_test_app(tmp_path=tmp_path)) as c:
            yield c

    def test_timeseries_snaps_to_a_fixed_window(self, client):
        d = client.get("/dashboard/api/timeseries?window=5000").json()
        assert (d["window_s"], d["step_s"]) == (3600, 60)
        d = client.get("/dashboard/api/timeseries?window=86400").json()
        assert (d["window_s"], d["step_s"]) == (86400, 600)
        assert len(d["buckets"]) == 144

    def test_node_history_without_a_sampler_is_empty_not_an_error(self, client):
        d = client.get("/dashboard/api/node-history").json()
        assert d["t"] == [] and d["nodes"] == {}


class TestOverviewPage:
    def test_overview_fills_the_window_and_loads_charts(self):
        from fastapi import FastAPI

        from fleet_manager.server.routes.dashboard import router

        app = FastAPI()
        app.include_router(router)
        html = TestClient(app).get("/dashboard").text
        assert '<main class="main wide">' in html
        assert "chart.js" in html and 'id="activity-chart"' in html
        for spark in ("rpm", "tps", "ttft", "errors", "inflight", "queued"):
            assert f'id="spark-{spark}"' in html
        # Reading pages keep their measure.
        assert '<main class="main">' in TestClient(app).get("/dashboard/settings").text


def test_the_tokens_figure_says_it_is_an_average_and_shows_what_streams_now():
    """C1 (run 6, filmed): the figure read 0 to 39 tok/s while about 500 were
    streaming, because it averages finished requests over five minutes.  It now
    says so, and a live line computed from the queues' streamed chunks moves
    with a burst."""
    from fleet_manager.server.routes import dashboard

    src = open(dashboard.__file__).read()
    assert "Tokens / s, 5 min" in src
    assert 'id="fig-tps-now"' in src
    assert "streamed_chunks" in src and "function updateLiveRate" in src
