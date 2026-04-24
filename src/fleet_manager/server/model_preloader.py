"""Priority Model Preloader — keeps the right models warm without thrashing.

Two concerns:

1.  **Pinned models** — user-declared "always keep this hot."  Configured
    via FLEET_PINNED_MODELS (comma-separated).  Loaded first on startup
    and actively reloaded if evicted.  Example: ``gpt-oss:120b,gemma3:27b``
    for projects that depend on them across sessions.

2.  **Priority models** — scored by 24h/7d request frequency.  After
    pinned models are loaded, the preloader fills remaining slots up to
    FLEET_MODEL_PRELOAD_MAX_COUNT (default 3, matches Ollama's hardcoded
    hot cap).

Critical invariant: **don't load more models than the backend can hold
concurrently.**  Ollama 0.20.4 on macOS has a hardcoded 3-model cap.
Historically the preloader ignored this and blindly pre-warmed 10+
models based on usage scoring, which caused each new load to evict an
older one — thrashing the LRU and kicking out whatever was loaded
before the restart (including pinned models).  2026-04-23 observation:
restart of the router caused gpt-oss:120b eviction because the
preloader pre-warmed 6+ models past the 3-slot cap.

Design:
  - Startup: query ``/api/ps``, load pinned models first, fill remaining
    slots up to max_count.  Never exceed max_count total loads.
  - Refresh (every 10 min): check pinned models, reload any evicted;
    separately check top N priority models with recent activity.
  - Disable: FLEET_DISABLE_MODEL_PRELOADER=true → no-op, models load
    on demand on first request.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.model_knowledge import lookup_model
from fleet_manager.server.pinned_models import PinnedModelsStore
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.streaming import StreamingProxy
from fleet_manager.server.trace_store import TraceStore

logger = logging.getLogger(__name__)

# Cache priority scores to avoid repeated DB queries
_priority_cache: list[dict] = []
_priority_cache_time: float = 0
_CACHE_TTL = 300  # 5 minutes


async def get_cached_priorities(trace_store: TraceStore) -> list[dict]:
    """Get priority scores, cached for 5 minutes."""
    global _priority_cache, _priority_cache_time
    if time.time() - _priority_cache_time < _CACHE_TTL and _priority_cache:
        return _priority_cache
    _priority_cache = await trace_store.get_model_priority_scores()
    _priority_cache_time = time.time()
    return _priority_cache


def get_model_priority(model: str, priorities: list[dict]) -> float:
    """Look up the priority score for a model name."""
    for entry in priorities:
        if entry["model"] == model:
            return entry["priority_score"]
    return 0.0


def _estimate_model_size(model: str) -> float:
    """Estimate model RAM in GB from catalog or name heuristics."""
    spec = lookup_model(model)
    if spec:
        return spec.ram_gb

    # Heuristic from model name
    lower = model.lower()
    if "embed" in lower or "nomic" in lower:
        return 0.5
    if any(s in lower for s in (":1b", ":0.6b", ":0.5b")):
        return 1.0
    if any(s in lower for s in (":3b", ":4b")):
        return 3.0
    if any(s in lower for s in (":7b", ":8b")):
        return 5.0
    if any(s in lower for s in (":13b", ":14b")):
        return 10.0
    if any(s in lower for s in (":22b", ":27b", ":32b")):
        return 20.0
    if any(s in lower for s in (":70b", ":72b")):
        return 45.0
    if any(s in lower for s in (":120b", ":122b", ":235b")):
        return 75.0
    return 10.0  # Conservative default


def _parse_pinned_models(setting: str) -> list[str]:
    """Parse FLEET_PINNED_MODELS into a clean list of model names."""
    return [m.strip() for m in (setting or "").split(",") if m.strip()]


def _model_is_loaded_anywhere(model: str, nodes) -> bool:
    """True if any online node has the model currently hot."""
    return any(
        n.ollama and model in [m.name for m in n.ollama.models_loaded]
        for n in nodes
    )


def _nodes_with_model_on_disk(model: str, nodes):
    """Nodes that have the model available on disk (pullable, not necessarily hot)."""
    return [n for n in nodes if n.ollama and model in n.ollama.models_available]


async def _load_model_on_best_node(
    model: str, nodes, proxy: StreamingProxy, *,
    why: str = "preload", target_node_id: str | None = None,
) -> bool:
    """Pre-warm model; prefer ``target_node_id`` if given, else pick best-mem node.

    Returns True if load was attempted.  When ``target_node_id`` is set but
    that node doesn't have the model on disk or is offline, we fall back to
    the best-memory node (so a per-node pin still warms the fleet).
    """
    available_nodes = _nodes_with_model_on_disk(model, nodes)
    if not available_nodes:
        logger.info(f"Preloader: {model} not on disk anywhere — skipping ({why})")
        return False
    best = None
    if target_node_id:
        for n in available_nodes:
            if n.node_id == target_node_id:
                best = n
                break
        if best is None:
            logger.info(
                f"Preloader: target node {target_node_id} doesn't have {model} "
                f"on disk; falling back to best-mem node ({why})"
            )
    if best is None:
        best = max(
            available_nodes,
            key=lambda n: n.memory.available_gb if n.memory else 0,
        )
    model_size = _estimate_model_size(model)
    available = best.memory.available_gb if best.memory else 0
    if available < model_size * 1.2:
        logger.info(
            f"Preloader: skipping {model} — need {model_size:.0f}GB "
            f"but only {available:.0f}GB free on {best.node_id} ({why})"
        )
        return False
    logger.info(
        f"Preloader: loading {model} (~{model_size:.0f}GB) "
        f"on {best.node_id} ({why})"
    )
    try:
        await proxy.pre_warm(best.node_id, model)
        return True
    except Exception as exc:
        logger.warning(f"Preloader: failed to load {model}: {exc}")
        return False


def _build_pinned_plan(
    env_pins: list[str],
    per_node_map: dict[str, list[str]],
) -> list[tuple[str, str | None]]:
    """Return ordered (model, target_node_id) list for pin loading.

    Env pins come first with ``None`` target (load on best-mem node).  Per-node
    pins follow with their node id set.  Duplicates within the same target
    bucket are collapsed.  A model pinned both env-wide and per-node will
    appear twice — once as fleet-wide, once targeted — and the preloader's
    "already hot anywhere" check skips the second if the first succeeded.
    """
    plan: list[tuple[str, str | None]] = [(m, None) for m in env_pins if m]
    seen_per_node: set[tuple[str, str]] = set()
    for node_id, models in per_node_map.items():
        for m in models:
            key = (node_id, m)
            if m and key not in seen_per_node:
                seen_per_node.add(key)
                plan.append((m, node_id))
    return plan


async def preload_priority_models(
    registry: NodeRegistry,
    trace_store: TraceStore,
    proxy: StreamingProxy,
    settings: ServerSettings,
    *,
    pinned_store: PinnedModelsStore | None = None,
) -> None:
    """Startup: load pinned models, then fill remaining slots up to cap.

    Refresh loop: every 10 min, reload any pinned model that got evicted,
    plus top priority models with recent activity.  Respects
    ``model_preload_max_count`` as the total-slots budget so the
    preloader never thrashes the Ollama hot cap.
    """
    if getattr(settings, "disable_model_preloader", False):
        logger.info("Preloader disabled via FLEET_DISABLE_MODEL_PRELOADER")
        return

    env_pins = _parse_pinned_models(getattr(settings, "pinned_models", ""))
    per_node_map = pinned_store.load() if pinned_store else {}
    pinned_plan = _build_pinned_plan(env_pins, per_node_map)
    # Flat list preserved for step-2 priority fill exclusions + logging
    pinned = list(dict.fromkeys([m for m, _ in pinned_plan]))
    max_count = getattr(settings, "model_preload_max_count", 3)

    # Wait for at least one node to come online
    for _ in range(60):
        nodes = registry.get_online_nodes()
        if nodes:
            break
        await asyncio.sleep(1)
    else:
        logger.info("Preloader: no nodes registered after 60s, skipping startup")
        return

    # Brief delay to let the node's heartbeat fully populate
    await asyncio.sleep(3)
    nodes = registry.get_online_nodes()

    # --- Step 1: load pinned models ---------------------------------------
    loaded_count = 0
    for model, target in pinned_plan:
        if loaded_count >= max_count:
            logger.warning(
                f"Preloader: pinned-models plan ({len(pinned_plan)}) exceeds "
                f"max_count ({max_count}); truncating at {loaded_count}.  "
                f"Raise FLEET_MODEL_PRELOAD_MAX_COUNT if your backend can "
                f"handle more concurrent models."
            )
            break
        if _model_is_loaded_anywhere(model, nodes):
            logger.info(f"Preloader: {model} already hot (pinned)")
            loaded_count += 1
            continue
        why = f"pinned:{target}" if target else "pinned"
        if await _load_model_on_best_node(
            model, nodes, proxy, why=why, target_node_id=target,
        ):
            loaded_count += 1
            await asyncio.sleep(2)  # let Ollama update /api/ps
            nodes = registry.get_online_nodes()  # refresh after load

    # --- Step 2: fill remaining slots with priority models ----------------
    priorities = await get_cached_priorities(trace_store)
    if priorities:
        logger.info(
            f"Preloader: {len(priorities)} model(s) in usage history; "
            f"will fill up to {max_count - loaded_count} more slot(s)"
        )
        for entry in priorities:
            if loaded_count >= max_count:
                logger.info(
                    f"Preloader: reached max_count ({max_count}) — "
                    f"stopping to avoid Ollama LRU thrash"
                )
                break
            model = entry["model"]
            score = entry["priority_score"]
            if score < 1.0:
                break  # rarely-used models not worth warming
            if model in pinned:
                continue  # already handled above
            if _model_is_loaded_anywhere(model, nodes):
                continue  # already hot, no need to load
            if await _load_model_on_best_node(
                model, nodes, proxy, why=f"priority score={score}",
            ):
                loaded_count += 1
                await asyncio.sleep(2)
                nodes = registry.get_online_nodes()

    logger.info(
        f"Preloader startup complete: {loaded_count}/{max_count} models warm "
        f"({len(pinned)} pinned configured)"
    )

    # --- Step 3: refresh loop ---------------------------------------------
    # Every 10 min, ensure pinned models stay hot + top priorities with
    # recent activity stay hot.  Respects max_count as the overall budget.
    while True:
        await asyncio.sleep(600)
        try:
            # Re-read per-node pins so dashboard toggles land within a cycle
            if pinned_store is not None:
                refreshed_plan = _build_pinned_plan(
                    env_pins, pinned_store.load(),
                )
            else:
                refreshed_plan = pinned_plan
            await _refresh_priority_models(
                registry, trace_store, proxy,
                pinned_plan=refreshed_plan, max_count=max_count,
            )
        except Exception as exc:
            logger.warning(f"Preloader refresh failed: {exc}")


async def _refresh_priority_models(
    registry: NodeRegistry,
    trace_store: TraceStore,
    proxy: StreamingProxy,
    *,
    pinned: list[str] | None = None,
    pinned_plan: list[tuple[str, str | None]] | None = None,
    max_count: int = 3,
) -> None:
    """Keep pinned models hot + top priorities with recent activity hot.

    Ordering matters:
      1. Pinned models FIRST — reload any that were evicted (regardless
         of recent activity; if the user pinned them, they stay hot)
      2. Top priority models with recent activity — fill remaining slots
         after pinned models

    Budget: total post-refresh hot-count stays ≤ max_count.  Pinned
    models get their slots first; priority models only fill what's left.
    """
    if pinned_plan is None:
        pinned_plan = [(m, None) for m in (pinned or [])]
    # Flat de-duped name list for priority-exclusion in step 2
    pinned_names = list(dict.fromkeys([m for m, _ in pinned_plan]))

    nodes = registry.get_online_nodes()
    if not nodes:
        return

    # --- Count currently-hot models, reserving slots for pinned -----------
    hot_models: set[str] = set()
    for n in nodes:
        if n.ollama:
            for m in n.ollama.models_loaded:
                hot_models.add(m.name)
    currently_hot_count = len(hot_models)

    # --- Step 1: ensure pinned models are hot -----------------------------
    loaded_this_cycle = 0
    for model, target in pinned_plan:
        if loaded_this_cycle >= max_count:
            break  # already filled the budget with pins alone
        if model in hot_models:
            continue  # already loaded, nothing to do
        # Pinned-but-missing: ALWAYS reload (no recency check — user pinned it)
        logger.info(
            f"Preloader refresh: pinned model {model} was evicted — reloading"
            + (f" on {target}" if target else "")
        )
        why = f"pinned-refresh:{target}" if target else "pinned-refresh"
        if await _load_model_on_best_node(
            model, nodes, proxy, why=why, target_node_id=target,
        ):
            loaded_this_cycle += 1
            await asyncio.sleep(2)
            nodes = registry.get_online_nodes()
            # Update hot_models snapshot after successful load
            for n in nodes:
                if n.ollama:
                    for m in n.ollama.models_loaded:
                        hot_models.add(m.name)

    # --- Step 2: fill remaining slots with top priority models ------------
    priorities = await get_cached_priorities(trace_store)
    if not priorities:
        return

    # Respect user intent: only reload priorities with recent activity.
    # Pinned models bypass this — they're reloaded regardless.
    recent_models = await trace_store.get_recently_used_models(seconds=3600)

    # Budget: total hot + loaded-this-cycle must stay ≤ max_count
    remaining_budget = max_count - max(currently_hot_count, loaded_this_cycle)
    if remaining_budget <= 0:
        return

    top_priorities = [
        p for p in priorities
        if p["priority_score"] >= 10 and p["model"] not in pinned_names
    ][: max_count]  # cap search scope

    for entry in top_priorities:
        if remaining_budget <= 0:
            break
        model = entry["model"]
        if model in hot_models:
            continue
        if model not in recent_models:
            logger.info(
                f"Preloader refresh: skipping {model} — no requests in last hour"
            )
            continue
        if await _load_model_on_best_node(
            model, nodes, proxy, why=f"priority-refresh score={entry['priority_score']}",
        ):
            remaining_budget -= 1
            await asyncio.sleep(2)
            nodes = registry.get_online_nodes()
