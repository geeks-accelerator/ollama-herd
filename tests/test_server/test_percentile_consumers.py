"""The router's percentiles come from one function, ``common.stats.percentile``.

Before 2026-11-20 the latency store and the decode check each indexed sorted
samples their own way (``values[int(n * p / 100)]``), and the latency cache's
startup load used SQL ``PERCENT_RANK``.  Those pick an observed sample at or
above the rank, which for small samples is the maximum: two latencies of 100
and 1,000 ms gave a p75 of 1,000.  They now interpolate like the benchmark
report and the community payload.

These pin the values on small inputs, where the methods differ most, so a
change to the arithmetic shows up as a changed number here.
"""

from __future__ import annotations

import pytest

from fleet_manager.server.latency_store import LatencyStore
from fleet_manager.server.trace_store import TraceStore


async def _latency_store(tmp_path, latencies):
    store = LatencyStore(data_dir=str(tmp_path))
    await store.initialize()
    for ms in latencies:
        await store.record("studio", "m", ms)
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latencies", "p75"),
    [
        ([500.0], 500.0),
        # Was 1,000 (the maximum): rank 0.75 of the way from 100 to 1,000.
        ([100.0, 1000.0], 775.0),
        # Was 800 (the maximum).
        ([500.0, 600.0, 700.0, 800.0], 725.0),
        # Was 800, the 8th of 10.
        ([float(x) for x in range(100, 1001, 100)], 775.0),
    ],
)
async def test_latency_p75(tmp_path, latencies, p75):
    store = await _latency_store(tmp_path, latencies)
    assert await store.get_percentile("studio", "m", 75) == pytest.approx(p75)
    # The scorer reads the cache, refreshed on every record.
    assert store.get_cached_percentile("studio", "m") == pytest.approx(p75)
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latencies", "p75"),
    [
        ([500.0, 700.0], 650.0),  # was 700 (SQL PERCENT_RANK)
        ([500.0, 600.0, 700.0, 800.0], 725.0),  # was 800
    ],
)
async def test_latency_cache_after_a_restart_matches(tmp_path, latencies, p75):
    """The startup load must give the value recording would have."""
    store = await _latency_store(tmp_path, latencies)
    await store.close()
    reopened = LatencyStore(data_dir=str(tmp_path))
    await reopened.initialize()
    assert reopened.get_cached_percentile("studio", "m") == pytest.approx(p75)
    await reopened.close()


@pytest.mark.asyncio
async def test_decode_p99_and_median(tmp_path):
    store = TraceStore(data_dir=str(tmp_path))
    await store.initialize()
    # 20 requests with TPOT 1..20 ms: 21 tokens, 100 ms to the first.
    for tpot in range(1, 21):
        await store.record_trace(
            request_id=f"r{tpot}",
            model="m",
            original_model="m",
            node_id="n",
            latency_ms=100.0 + 20 * tpot,
            time_to_first_token_ms=100.0,
            completion_tokens=21,
        )
    [st] = await store.get_decode_latency_stats()
    assert st["recent_n"] == 20
    # Was 20.0: below 100 samples the old index always picked the maximum.
    assert st["recent_p99"] == pytest.approx(19.81)
    # Was 11.0, the upper of the two middle samples.
    assert st["recent_median"] == pytest.approx(10.5)
    await store.close()
