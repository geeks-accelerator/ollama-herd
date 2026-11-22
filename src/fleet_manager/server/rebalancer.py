"""Queue Rebalancer — moves pending requests between queues and triggers pre-warm."""

from __future__ import annotations

import asyncio
import logging

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.serializers import model_resident_on_node

logger = logging.getLogger(__name__)


class Rebalancer:
    def __init__(self, settings, registry, scorer, queue_mgr, streaming_proxy):
        self._s: ServerSettings = settings
        self._registry: NodeRegistry = registry
        self._scorer = scorer
        self._queue_mgr = queue_mgr
        self._proxy = streaming_proxy
        # Track active pre-warm operations to avoid duplicates
        self._pre_warm_locks: set[str] = set()

    async def run(self):
        """Background loop: rebalance queues and trigger pre-warms."""
        while True:
            await asyncio.sleep(self._s.rebalance_interval)
            try:
                await self._check_pre_warm()
                await self._check_rebalance()
            except Exception as e:
                logger.error(f"Rebalancer error: {e}")

    async def _check_pre_warm(self):
        """Trigger pre-warm on runner-up nodes when winner's queue is deep."""
        queue_info = self._queue_mgr.get_queue_info()
        for _key, info in queue_info.items():
            depth = info["pending"] + info["in_flight"]
            if depth < self._s.pre_warm_threshold:
                continue

            model = info["model"]
            # Score to find runner-up
            queue_depths = self._queue_mgr.get_queue_depths()
            results = self._scorer.score_request(model, queue_depths)
            if len(results) < 2:
                continue

            runner_up = results[1]
            # A pre-warm is a speculative load: never let one evict a resident
            # model.  It did, back and forth, in the 2026-11-20 fleet benchmark:
            # warming qwen3.5:9b on a node serving qwen3-coder:30b, then the
            # coder back again, each a 15 s cold load for the next request.
            # Request routing weighs that eviction; pre-warm just declines it.
            if runner_up.scores_breakdown.get("eviction", 0) < 0:
                continue
            lock_key = f"{runner_up.node_id}:{model}"
            if lock_key in self._pre_warm_locks:
                continue

            # Check if runner-up needs warming
            node = self._registry.get_node(runner_up.node_id)
            if not node or not node.ollama:
                continue
            if model_resident_on_node(model, node):
                continue
            # A speculative load never goes to a machine whose owner is busy
            # (capacity learning).  Documented for this, and read by nothing
            # until 2026-11-22.
            if (
                node.capacity
                and node.capacity.availability_score < self._s.pre_warm_min_availability
            ):
                continue

            # Trigger pre-warm
            self._pre_warm_locks.add(lock_key)
            logger.info(
                f"Pre-warm triggered: {model} on {runner_up.node_id} "
                f"(winner {results[0].node_id} queue depth: {depth})"
            )
            from fleet_manager.server.streaming import _create_logged_task

            _create_logged_task(
                self._do_pre_warm(lock_key, runner_up.node_id, model, runner_up.scores_breakdown),
                name=f"pre-warm-{model}-{runner_up.node_id}",
            )

    async def _do_pre_warm(
        self, lock_key: str, node_id: str, model: str, breakdown: dict | None = None
    ):
        """Execute pre-warm and release lock when done.

        num_ctx is deliberately left to ``pre_warm`` to resolve: this call site
        omitted it for as long as it existed, warming the runner-up at Ollama's
        default context instead of the configured one.  That is worse than it
        sounds — the override only applies on a cold load, and pre-warming IS the
        cold load, so the model then stays mis-sized until something unloads it.
        """
        try:
            await self._proxy.pre_warm(node_id, model, breakdown=breakdown)
        finally:
            self._pre_warm_locks.discard(lock_key)

    async def _check_rebalance(self):
        """Move pending requests from overloaded queues to better alternatives."""
        queue_info = self._queue_mgr.get_queue_info()
        for key, info in queue_info.items():
            pending = info["pending"]
            if pending < self._s.rebalance_threshold:
                continue

            model = info["model"]
            source_node = info["node_id"]

            # Find better nodes
            queue_depths = self._queue_mgr.get_queue_depths()
            results = self._scorer.score_request(model, queue_depths)

            # Move to a node that has the model hot and slots free now, as many
            # as it has free.  A fixed few per tick drained a backlog onto a
            # node that had just come back at 0.6 requests a second while it
            # could take 2.6 (run 6: 33 of 96 served there, 71 s against 52-58).
            for result in results:
                if result.node_id == source_node:
                    continue
                target_node = self._registry.get_node(result.node_id)
                if not target_node or not model_resident_on_node(model, target_node):
                    continue  # never trade a queue wait for a cold load
                count = min(self._queue_mgr.free_slots(result.queue_key), pending)
                if count <= 0:
                    continue

                moved = await self._queue_mgr.move_pending(key, result.queue_key, count)
                if moved > 0:
                    logger.info(f"Rebalanced {moved} requests: {key} → {result.queue_key}")
                break  # One rebalance per overloaded queue per cycle
            else:
                logger.debug(
                    f"Queue {key} overloaded ({pending} pending) but no suitable "
                    f"rebalance target found for {model}"
                )
