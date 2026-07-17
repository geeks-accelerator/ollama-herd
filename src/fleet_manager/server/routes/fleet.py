"""Fleet status endpoint for monitoring and future dashboard."""

from __future__ import annotations

import contextlib
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fleet_manager.server.serializers import (
    OLLAMA_HOT_MODEL_CAP,
    hot_model_cap_for,
    serialize_node,
)

router = APIRouter(tags=["fleet"])


@router.get("/fleet/status")
async def fleet_status(request: Request):
    """Full fleet state — nodes, queues, and health summary."""
    registry = request.app.state.registry
    queue_mgr = request.app.state.queue_mgr

    nodes = []
    total_models_loaded = 0
    total_requests_active = 0

    for node in registry.get_all_nodes():
        nodes.append(serialize_node(node))
        if node.ollama:
            total_models_loaded += len(node.ollama.models_loaded)
            total_requests_active += node.ollama.requests_active

    online_count = sum(1 for n in registry.get_all_nodes() if n.status.value == "online")

    return {
        "fleet": {
            "nodes_total": len(nodes),
            "nodes_online": online_count,
            "models_loaded": total_models_loaded,
            "requests_active": total_requests_active,
        },
        "nodes": nodes,
        "queues": queue_mgr.get_queue_info(),
        "timestamp": time.time(),
    }


@router.get("/fleet/limits")
async def fleet_limits(request: Request):
    """Effective serving constraints, so a client can auto-serialize instead of
    self-DoSing a saturated box.

    Reports the per-node hot-model cap and free slots, the router's max
    in-flight retry budget, and (when set) the node's ``OLLAMA_NUM_PARALLEL``
    so a caller knows how many concurrent requests the fleet can actually
    absorb before requests start queueing / 503-ing.
    """
    registry = request.app.state.registry
    settings = request.app.state.settings

    node_limits = []
    for node in registry.get_all_nodes():
        loaded = len(node.ollama.models_loaded) if node.ollama else 0
        cap = hot_model_cap_for(node)
        node_limits.append({
            "node_id": node.node_id,
            "status": node.status.value,
            # Per-node: each node reports its own OLLAMA_MAX_LOADED_MODELS.
            "hot_model_cap": cap,
            "models_loaded": loaded,
            "free_slots": max(0, cap - loaded),
        })

    return {
        # Router-side retry budget (per request, across nodes).
        "max_retries": getattr(settings, "max_retries", 0),
        # Fallback cap for nodes that don't report one. Kept for compatibility;
        # `nodes[].hot_model_cap` is authoritative and can differ per node,
        # since each reports its own OLLAMA_MAX_LOADED_MODELS.
        "hot_model_cap": OLLAMA_HOT_MODEL_CAP,
        # Per-model in-flight cap the MLX backend enforces (Ollama uses
        # OLLAMA_NUM_PARALLEL, set outside the herd on the node).
        "mlx_max_inflight_per_model": getattr(settings, "mlx_max_inflight_per_model", 1),
        "mlx_max_queue_depth": getattr(settings, "mlx_max_queue_depth", 10),
        "nodes": node_limits,
        "timestamp": time.time(),
    }


#: Fraction of a node's RAM the pinned set may claim. Pins must all be resident
#: *simultaneously*, so whatever they take is permanently unavailable for the
#: load-and-swap the fleet exists to do. Leave room for the OS, MLX servers
#: (separate processes, ~34GB on our box), and non-pinned models to cycle.
_PIN_MEMORY_BUDGET_FRACTION = 0.8

#: Resident-size multiplier over on-disk weights — the same 1.2 the preloader's
#: memory gate uses, kept consistent on purpose. A model costs more resident
#: than it does on disk (gpt-oss:120b: 65GB on disk, 76GB resident at 131K
#: context ≈ +17%). Summing raw disk sizes under-counts and let a 355GB pin set
#: "fit" a 512GB box that in practice thrashed.
_PIN_RESIDENT_OVERHEAD = 1.2


def _pin_would_not_fit(model: str, node_id: str | None, registry, store) -> dict | None:
    """Return a refusal payload if pinning ``model`` over-commits the node.

    Returns None when the pin is fine. Uses real model sizes (nodes report
    on-disk sizes from /api/tags), so this is arithmetic on ground truth rather
    than the name-guessing that let a 290GB model look like 10GB.
    """
    from fleet_manager.server.model_preloader import _estimate_model_size

    node = registry.get_node(node_id) if node_id else None
    if node is None:
        # No specific target — judge against the roomiest online node, since
        # that's where the loader would put it.
        online = [n for n in registry.get_online_nodes() if n.memory]
        if not online:
            return None  # nothing to reason about; let the normal path 404/503
        node = max(online, key=lambda n: n.memory.total_gb or 0)

    total_gb = (node.memory.total_gb if node.memory else 0) or 0
    if total_gb <= 0:
        return None  # unknown capacity — don't block on a guess

    per_node = store.load() or {}
    already = [m for m in (per_node.get(node.node_id) or []) if m != model]
    budget = total_gb * _PIN_MEMORY_BUDGET_FRACTION

    # Compare *resident* cost, not on-disk weights — see _PIN_RESIDENT_OVERHEAD.
    sizes = {
        m: _estimate_model_size(m, node) * _PIN_RESIDENT_OVERHEAD
        for m in [*already, model]
    }
    pinned_gb = sum(sizes[m] for m in already)
    new_gb = sizes[model]
    if pinned_gb + new_gb <= budget:
        return None

    return {
        "ok": False,
        "model": model,
        "error": (
            f"Pinning '{model}' (~{new_gb:.0f}GB resident) would bring the "
            f"pinned set on '{node.node_id}' to ~{pinned_gb + new_gb:.0f}GB, "
            f"over the {budget:.0f}GB budget "
            f"({_PIN_MEMORY_BUDGET_FRACTION:.0%} of {total_gb:.0f}GB). Pinned "
            f"models must ALL stay resident simultaneously, so over-committing "
            f"makes the preloader evict and reload them in a loop forever. "
            f"Unpin something first (DELETE /fleet/pin/<model>), or pass "
            f'"force": true if you know this fits.'
        ),
        "node_id": node.node_id,
        "requested_gb": round(new_gb, 1),
        "already_pinned_gb": round(pinned_gb, 1),
        "budget_gb": round(budget, 1),
        "node_total_gb": round(total_gb, 1),
        "already_pinned": {m: round(sizes[m], 1) for m in already},
        "hint": "Pin only what you need resident; unpin after benchmarking.",
    }


@router.post("/fleet/pin")
async def fleet_pin(request: Request):
    """Pin a model resident: pre-warm it now (evicting the LRU if needed) and
    persist the pin so the preloader keeps it warm if it's later evicted.

    Body: ``{"model": "<name>", "node_id": "<optional>", "wait": <bool>,
    "timeout_s": <float>}``.  Reuses the same ``PinnedModelsStore`` +
    ``model_preloader`` machinery as the dashboard — this is the one-call
    replacement for the manual ``curl :11434 keep_alive`` dance a benchmark
    otherwise needs.

    ``wait=true`` blocks until the router's routing view confirms the model
    resident (``models_loaded`` / healthy ``mlx_servers``) before returning,
    so a caller that pins-then-uses doesn't race the heartbeat and get
    fallback-substituted.  See ``docs/plans/fleet-pin-readiness.md``.
    """
    from fleet_manager.server.mlx_proxy import is_mlx_model, strip_mlx_prefix
    from fleet_manager.server.model_preloader import (
        _load_model_on_best_node,
        _model_resident_on_node,
        _nodes_with_model_on_disk,
        _wait_until_resident,
    )

    body = await request.json()
    model = (body.get("model") or "").strip()
    node_id = (body.get("node_id") or "").strip() or None
    wait = bool(body.get("wait", False))
    timeout_s = float(body.get("timeout_s", 30))
    if not model:
        return JSONResponse({"ok": False, "error": "model required"}, status_code=400)

    registry = request.app.state.registry
    proxy = request.app.state.streaming_proxy
    store = request.app.state.pinned_store

    # Validate the target node exists. Without this, a pin to a node_id that
    # never existed (or stopped existing after a rename) is accepted and rots
    # in the store forever — every preloader cycle logs "target node X doesn't
    # have <model> on disk; falling back to best-mem node" and loads it
    # somewhere else anyway. Found 2026-07-17: a `gemma3:27b` pin targeting
    # "Neons-Mac-Studio" long after that node became "bb".
    if node_id and registry.get_node(node_id) is None:
        known = [n.node_id for n in registry.get_all_nodes()]
        return JSONResponse(
            {
                "ok": False,
                "model": model,
                "error": (
                    f"node_id '{node_id}' is not in the fleet. "
                    f"Known nodes: {known or '(none online)'}. "
                    f"Omit node_id to let the router choose."
                ),
                "known_nodes": known,
            },
            status_code=400,
        )

    # MLX models are always-resident subprocesses configured via
    # FLEET_NODE_MLX_SERVERS — they can't be loaded on demand, so pinning is a
    # no-op.  Report readiness from mlx_servers health instead of warming
    # (pre_warm would POST to Ollama and 404).
    if is_mlx_model(model):
        target = strip_mlx_prefix(model)
        ready_node = None
        for n in registry.get_online_nodes():
            if any(
                s.model == target and s.status == "healthy"
                for s in (n.mlx_servers or [])
            ):
                ready_node = n.node_id
                break
        return {
            "ok": True,
            "model": model,
            "pinned_node": ready_node,
            "ready": ready_node is not None,
            "note": (
                "MLX models are always resident (configured via "
                "FLEET_NODE_MLX_SERVERS); pin is a no-op."
            ),
        }

    # Admission control — refuse a pin that can't physically co-reside with the
    # pins already in place.  Pins bypass every other safety net: the preloader
    # reloads them unconditionally ("no recency check — user pinned it"), and
    # nothing else asks whether the pinned SET fits.  On 2026-07-17 a client
    # agent benchmarking models pinned each one as our own docs instructed and
    # never unpinned; the set reached 307GB (incl. a 290GB model) on a 512GB
    # box, and the preloader spent hours evicting and reloading them in a loop.
    # The agent did nothing wrong — we accepted every pin silently.
    # `force: true` overrides for operators who know what they're doing.
    if not bool(body.get("force", False)):
        refusal = _pin_would_not_fit(model, node_id, registry, store)
        if refusal is not None:
            return JSONResponse(refusal, status_code=409)

    # Distinguish the failure causes BEFORE relying on the loader's bare bool.
    # `_load_model_on_best_node` returns False for three different reasons —
    # not-on-disk, memory-gate refusal, and a pre_warm error — so reporting them
    # all as "not on disk" produces a factually false error.  Observed
    # 2026-07-17: a pin for gpt-oss:120b (resident, serving 30/30 requests) was
    # refused with "not on disk — run 'ollama pull'"; the router log showed the
    # real cause was the memory gate ("need 72GB but only 49GB free").  Check
    # on-disk explicitly here so each failure reports the truth.
    nodes = registry.get_online_nodes()
    if not _nodes_with_model_on_disk(model, nodes):
        return JSONResponse(
            {
                "ok": False,
                "model": model,
                "error": f"'{model}' is not on disk on any online node — "
                f"run 'ollama pull {model}' on a fleet device first.",
            },
            status_code=404,
        )

    loaded = await _load_model_on_best_node(
        model, nodes, proxy, why="fleet-pin", target_node_id=node_id,
    )
    if not loaded:
        # On disk but wouldn't load — in practice the memory gate refusing for
        # lack of free RAM.  The router log carries the exact need-vs-free.
        return JSONResponse(
            {
                "ok": False,
                "model": model,
                "error": f"'{model}' is on disk but could not be loaded on any "
                f"online node — most likely insufficient free memory. Check "
                f"/fleet/status for node memory (the router log records the "
                f"exact need-vs-free numbers), or free a slot and retry.",
            },
            status_code=503,
        )

    # Optionally wait for the router's residency view to catch up.  pre_warm
    # already blocked through the actual Ollama load, so this only waits out
    # the heartbeat-reflection lag.  Doing it BEFORE resolving pin_node also
    # fixes a persistence race: when node_id is omitted and the heartbeat
    # hasn't landed, the scan below would find no node and the pin would
    # silently not persist.
    ready: bool | None = None
    ready_after_ms: int | None = None
    if wait:
        ready, ready_after_ms = await _wait_until_resident(
            registry, node_id, model, timeout_s,
        )

    # Persist the pin on whichever node now holds it (so the preloader
    # reloads it if evicted).  Prefer the requested node.
    pin_node = node_id
    if pin_node is None:
        for n in registry.get_all_nodes():
            if _model_resident_on_node(model, n):
                pin_node = n.node_id
                break
    per_node = store.set_pin(pin_node, model, True) if pin_node else store.load()
    resp = {"ok": True, "model": model, "pinned_node": pin_node, "per_node": per_node}
    if wait:
        resp["ready"] = bool(ready)
        resp["ready_after_ms"] = ready_after_ms
    return resp


@router.delete("/fleet/pin/{model:path}")
async def fleet_unpin(model: str, request: Request):
    """Release a pin so the model can be evicted normally.  ``{model:path}``
    accepts names with ``/`` and ``:`` (e.g. ``mlx-community/Foo`` or
    ``qwen3-coder:30b``).  Unpins from every node that had it pinned."""
    store = request.app.state.pinned_store
    unpinned_from = []
    for node_id, models in list(store.load().items()):
        if model in models:
            store.set_pin(node_id, model, False)
            unpinned_from.append(node_id)
    return {"ok": True, "model": model, "unpinned_from": unpinned_from, "per_node": store.load()}


@router.get("/fleet/queue")
async def fleet_queue(request: Request):
    """Lightweight queue status for client-side backoff decisions.

    Returns current queue depths, estimated wait, and per-tag active request
    counts. Designed for high-frequency polling (sub-second response).
    Clients can use this to decide whether to send a request now or wait.
    """
    queue_mgr = request.app.state.queue_mgr
    registry = request.app.state.registry

    queue_info = queue_mgr.get_queue_info()
    # Merge in MLX synthetic queues so the MLX backend is visible alongside
    # Ollama queues. mlx_lm.server is single-threaded per process — its
    # in-flight count tells you when MLX is busy and how many are waiting.
    mlx_proxy = getattr(request.app.state, "mlx_proxy", None)
    if mlx_proxy is not None:
        # Never break /fleet/queue on MLX stats hiccups
        with contextlib.suppress(Exception):
            queue_info = {**queue_info, **mlx_proxy.get_queue_info()}
    total_pending = sum(q["pending"] for q in queue_info.values())
    total_in_flight = sum(q["in_flight"] for q in queue_info.values())
    total_completed = sum(q["completed"] for q in queue_info.values())
    total_failed = sum(q["failed"] for q in queue_info.values())

    # Estimate wait time from recent latency (rough: pending * avg_latency / concurrency)
    latency_store = getattr(request.app.state, "latency_store", None)
    estimated_wait_ms = None
    if total_pending > 0 and latency_store:
        # Use cached p75 latencies across all queues
        latencies = []
        for _key, q in queue_info.items():
            p75 = latency_store.get_cached_percentile(q["node_id"], q["model"])
            if p75 is not None:
                latencies.append(p75)
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            total_concurrency = sum(
                q.get("concurrency", 1) for q in queue_info.values()
            )
            estimated_wait_ms = int(
                total_pending * avg_latency / max(total_concurrency, 1)
            )

    online_count = sum(
        1 for n in registry.get_all_nodes() if n.status.value == "online"
    )

    return {
        "queue_depth": total_pending + total_in_flight,
        "pending": total_pending,
        "in_flight": total_in_flight,
        "completed": total_completed,
        "failed": total_failed,
        "estimated_wait_ms": estimated_wait_ms,
        "nodes_online": online_count,
        "queues": {
            key: {
                "pending": q["pending"],
                "in_flight": q["in_flight"],
                "concurrency": q.get("concurrency", 1),
                "model": q["model"],
                "node_id": q["node_id"],
                "backend": q.get("backend", "ollama"),
                # MLX-only fields: admission control exposure so clients +
                # dashboard can distinguish "backend overloaded" from
                # "request errored" and tune retry behavior accordingly.
                "completed": q.get("completed", 0),
                "failed": q.get("failed", 0),
                "rejected": q.get("rejected", 0),
                "max_queue_depth": q.get("max_queue_depth"),
                # Rolling MLX prompt-cache hit rate (fraction in [0, 1]).
                # None until the proxy has observations; clients should
                # treat as "no data yet" rather than 0%.
                "cache_hit_rate": q.get("cache_hit_rate"),
                # MLX-only: warm/cold split is more honest than the simple
                # average — see MlxProxy.get_cache_stats().  warm_hit_rate
                # is the average of requests that DID cache-hit (≥80%);
                # cold_request_pct is the fraction that were cold-start
                # (<10% hit).  Both None when no observations.
                "warm_hit_rate": q.get("warm_hit_rate"),
                "cold_request_pct": q.get("cold_request_pct"),
                "cache_sample_count": q.get("cache_sample_count"),
                # Per-queue running averages (Ollama + MLX both populate).
                # 0 with stats_samples=0 means "no data yet" rather than
                # genuinely-zero performance — clients should check samples
                # before drawing conclusions from a 0 average.
                "stats_samples": q.get("stats_samples", 0),
                "avg_latency_ms": q.get("avg_latency_ms", 0),
                "avg_prompt_tokens": q.get("avg_prompt_tokens", 0),
                "avg_completion_tokens": q.get("avg_completion_tokens", 0),
            }
            for key, q in queue_info.items()
        },
        "timestamp": time.time(),
    }
