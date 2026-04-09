"""
Dynamic num_ctx optimizer — analyzes actual prompt token usage and recommends
optimal context sizes per model.

When auto_calculate is enabled, periodically updates num_ctx_overrides
on the settings object based on observed p99 prompt sizes. Can trigger
Ollama restarts via the heartbeat command channel.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

logger = logging.getLogger(__name__)

# Minimum time between restart recommendations per node (30 minutes)
RESTART_COOLDOWN_S = 1800


def next_power_of_2(n: int) -> int:
    """Return the smallest power of 2 >= n."""
    if n <= 0:
        return 2048
    return 2 ** math.ceil(math.log2(n))


def compute_recommended_ctx(p99: int, min_ctx: int = 2048) -> int:
    """Compute recommended num_ctx from observed p99 prompt size.

    Adds 25% headroom and rounds up to next power of 2.
    """
    if p99 <= 0:
        return min_ctx
    return max(min_ctx, next_power_of_2(int(p99 * 1.25)))


class ContextOptimizer:
    """Periodically analyzes prompt token usage and optimizes num_ctx."""

    def __init__(self, settings, registry, trace_store):
        self._settings = settings
        self._registry = registry
        self._trace_store = trace_store
        self._last_restart: dict[str, float] = {}  # node_id → timestamp
        self._pending_commands: dict[str, list[dict]] = {}  # node_id → commands

    def get_pending_commands(self, node_id: str) -> list[dict]:
        """Pop pending commands for a node (called from heartbeat response)."""
        return self._pending_commands.pop(node_id, [])

    async def run(self, interval: float = 300):
        """Background loop: check every 5 minutes."""
        await asyncio.sleep(60)  # Wait 1 minute after startup for traces to accumulate
        while True:
            try:
                await self._check_and_optimize()
            except Exception as e:
                logger.error(f"Context optimizer error: {e}", exc_info=True)
            await asyncio.sleep(interval)

    async def _check_and_optimize(self):
        """Compare current num_ctx vs actual usage, update overrides if auto_calculate."""
        if not getattr(self._settings, "num_ctx_auto_calculate", False):
            return

        if not self._trace_store:
            return

        stats = await self._trace_store.get_prompt_token_stats(days=7)
        if not stats:
            return

        # Build allocated context map from registry
        allocated_ctx: dict[str, int] = {}
        model_nodes: dict[str, list[str]] = {}  # model → [node_ids]
        for node in self._registry.get_online_nodes():
            if not node.ollama:
                continue
            for m in node.ollama.models_loaded:
                allocated_ctx[m.name] = max(
                    allocated_ctx.get(m.name, 0), m.context_length or 0
                )
                model_nodes.setdefault(m.name, []).append(node.node_id)

        overrides = self._settings.num_ctx_overrides
        changes: dict[str, int] = {}
        needs_restart: set[str] = set()

        for model_stats in stats:
            model = model_stats["model"]
            p99 = model_stats["p99"]
            alloc = allocated_ctx.get(model, 0)
            request_count = model_stats["request_count"]

            if alloc == 0 or p99 == 0 or request_count < 20:
                continue

            recommended = compute_recommended_ctx(p99)

            # Only recommend reduction if allocated is >4x what's needed
            if alloc > recommended * 4:
                current_override = overrides.get(model, 0)
                if current_override == 0 or current_override > recommended * 2:
                    changes[model] = recommended
                    # Nodes running this model need restart for new ctx to take effect
                    for nid in model_nodes.get(model, []):
                        needs_restart.add(nid)

        if changes:
            overrides.update(changes)
            self._settings.num_ctx_overrides = overrides
            logger.info(
                f"Context optimizer: updated overrides: "
                f"{', '.join(f'{m}={v}' for m, v in changes.items())}"
            )

            # Queue restart commands for affected nodes (respecting cooldown)
            now = time.time()
            for nid in needs_restart:
                last = self._last_restart.get(nid, 0)
                if now - last < RESTART_COOLDOWN_S:
                    logger.info(
                        f"Context optimizer: skipping restart for {nid} "
                        f"(cooldown: {int(RESTART_COOLDOWN_S - (now - last))}s remaining)"
                    )
                    continue

                # Build env overrides for this node's models
                env_overrides = {}
                for model, ctx in overrides.items():
                    if model in [
                        m.name
                        for node in self._registry.get_online_nodes()
                        if node.node_id == nid and node.ollama
                        for m in node.ollama.models_loaded
                    ]:
                        # OLLAMA_NUM_CTX is global, not per-model.
                        # Use the max of all overrides for this node.
                        current = env_overrides.get("OLLAMA_NUM_CTX", 0)
                        env_overrides["OLLAMA_NUM_CTX"] = str(max(current, ctx))

                if env_overrides:
                    self._pending_commands.setdefault(nid, []).append({
                        "type": "restart_ollama",
                        "env": env_overrides,
                        "reason": "Context optimizer: reduced num_ctx to save memory",
                    })
                    self._last_restart[nid] = now
                    logger.info(
                        f"Context optimizer: queued restart for {nid} "
                        f"with env {env_overrides}"
                    )
