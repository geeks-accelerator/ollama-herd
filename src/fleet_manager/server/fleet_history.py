"""A short, in-memory history of what the fleet looked like, for live charts.

Request history lives in SQLite (``TraceStore.get_timeseries``).  Node metrics
do not: the registry keeps only each node's latest heartbeat, so a sparkline of
CPU or memory would start empty on every page load and take minutes to say
anything.  This samples the registry and the queues on one clock and keeps the
last few hours in a bounded deque.

Memory only, on purpose.  Persisting heartbeats is ~17K rows per node per day
for a chart nobody reads past a few hours; a router restart clears this, which
is honest about what it is.  Sizing: one sample per 5 s for 3 h is 2,160
samples, each a few numbers per node -- well under a megabyte for ten nodes.
See docs/plans/dashboard-live-overview.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 5.0
KEEP_S = 3 * 3600


class FleetHistory:
    def __init__(self, registry, queue_mgr, interval_s: float = SAMPLE_INTERVAL_S):
        self._registry = registry
        self._queue_mgr = queue_mgr
        self.interval_s = interval_s
        self._samples: deque[dict] = deque(maxlen=int(KEEP_S / interval_s) + 1)

    def sample(self, now: float | None = None) -> dict:
        """Record one sample: per-node CPU, memory and queue, plus fleet totals."""
        now = time.time() if now is None else now
        nodes = {}
        for node in self._registry.get_all_nodes():
            if node.status.value != "online":
                continue
            mem = node.memory
            nodes[node.node_id] = {
                "cpu": round(node.cpu.utilization_pct, 1) if node.cpu else None,
                "mem": (
                    round(100.0 * mem.used_gb / mem.total_gb, 1) if mem and mem.total_gb else None
                ),
                "queued": 0,
                "running": 0,
            }
        queued = running = 0
        for q in self._queue_mgr.get_queue_info().values():
            queued += q["pending"]
            running += q["in_flight"]
            n = nodes.get(q["node_id"])
            if n is not None:
                n["queued"] += q["pending"]
                n["running"] += q["in_flight"]
        s = {"t": round(now, 1), "queued": queued, "running": running, "nodes": nodes}
        self._samples.append(s)
        return s

    def snapshot(self, window_s: float, now: float | None = None) -> dict:
        """Samples from the last ``window_s`` seconds, as parallel arrays.

        ``nodes[id]`` arrays line up with ``t``; a node absent from a sample
        (not yet joined, or offline then) has ``None`` there, which the page
        draws as a gap rather than a fake zero.
        """
        now = time.time() if now is None else now
        rows = [s for s in self._samples if s["t"] >= now - window_s]
        ids = sorted({nid for s in rows for nid in s["nodes"]})
        nodes = {
            nid: {
                k: [s["nodes"].get(nid, {}).get(k) for s in rows]
                for k in ("cpu", "mem", "queued", "running")
            }
            for nid in ids
        }
        return {
            "interval_s": self.interval_s,
            "t": [s["t"] for s in rows],
            "queued": [s["queued"] for s in rows],
            "running": [s["running"] for s in rows],
            "nodes": nodes,
        }

    async def run(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:  # a bad sample must never stop the sampler
                logger.exception("fleet history sample failed")
            await asyncio.sleep(self.interval_s)
