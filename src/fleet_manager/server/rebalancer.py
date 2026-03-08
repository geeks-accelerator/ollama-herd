"""Queue Rebalancer — moves pending requests between queues and triggers pre-warm."""

from __future__ import annotations

import asyncio
import logging

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.registry import NodeRegistry

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
        for key, info in queue_info.items():
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
            lock_key = f"{runner_up.node_id}:{model}"
            if lock_key in self._pre_warm_locks:
                continue

            # Check if runner-up needs warming
            node = self._registry.get_node(runner_up.node_id)
            if not node or not node.ollama:
                continue
            loaded = [m.name for m in node.ollama.models_loaded]
            if model in loaded:
                continue

            # Trigger pre-warm
            self._pre_warm_locks.add(lock_key)
            logger.info(
                f"Pre-warm triggered: {model} on {runner_up.node_id} "
                f"(winner {results[0].node_id} queue depth: {depth})"
            )
            from fleet_manager.server.streaming import _create_logged_task
            _create_logged_task(
                self._do_pre_warm(lock_key, runner_up.node_id, model),
                name=f"pre-warm-{model}-{runner_up.node_id}",
            )

    async def _do_pre_warm(self, lock_key: str, node_id: str, model: str):
        """Execute pre-warm and release lock when done."""
        try:
            await self._proxy.pre_warm(node_id, model)
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

            # Find a node that's significantly better than staying in current queue
            for result in results:
                if result.node_id == source_node:
                    continue
                # Only move if the target has the model hot and has low queue
                target_depth = queue_depths.get(result.queue_key, 0)
                if target_depth >= self._s.rebalance_threshold:
                    continue

                target_node = self._registry.get_node(result.node_id)
                if not target_node or not target_node.ollama:
                    continue
                loaded = [m.name for m in target_node.ollama.models_loaded]
                if model not in loaded:
                    continue  # Only rebalance to nodes with model hot

                # Move pending requests
                count = min(pending // 2, self._s.rebalance_max_per_cycle)
                if count <= 0:
                    continue

                moved = await self._queue_mgr.move_pending(key, result.queue_key, count)
                if moved > 0:
                    logger.info(
                        f"Rebalanced {moved} requests: {key} → {result.queue_key}"
                    )
                break  # One rebalance per overloaded queue per cycle
            else:
                logger.debug(
                    f"Queue {key} overloaded ({pending} pending) but no suitable "
                    f"rebalance target found for {model}"
                )
