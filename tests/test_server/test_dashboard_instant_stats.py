"""Embedding cards count each node's own requests, not the fleet's.

Run 6 (two EC2 M4 Macs): the Overview's DINOv2 card showed "34,140 done, 24 h"
on both s1 and s2, and nomic-embed-text showed 2 on both, because the stats
behind the native and vision cards were grouped by model alone and every
node's card read that one row.
"""

from __future__ import annotations

import pytest

from fleet_manager.server.routes import dashboard


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(dashboard, "_instant_stats_cache", {})
    monkeypatch.setattr(dashboard, "_instant_stats_ts", 0.0)


@pytest.mark.asyncio
async def test_each_node_gets_its_own_counts(tmp_path):
    from fleet_manager.server.trace_store import TraceStore

    store = TraceStore(data_dir=str(tmp_path))
    await store.initialize()
    rows = [("s1", "completed", 100.0)] * 3 + [("s2", "completed", 300.0), ("s2", "failed", None)]
    for i, (node, status, ms) in enumerate(rows):
        await store.record_trace(
            request_id=f"r{i}",
            model="dinov2-vit-s14",
            original_model="dinov2-vit-s14",
            node_id=node,
            status=status,
            latency_ms=ms,
            tags=["embed"],
        )
    stats = await dashboard._get_instant_model_stats(store)
    await store.close()

    s1 = stats[("dinov2-vit-s14", "s1")]
    s2 = stats[("dinov2-vit-s14", "s2")]
    assert (s1["completed"], s1["failed"], s1["avg_latency_ms"]) == (3, 0, 100.0)
    assert (s2["completed"], s2["failed"], s2["avg_latency_ms"]) == (1, 1, 300.0)
    assert ("dinov2-vit-s14", "s3") not in stats
