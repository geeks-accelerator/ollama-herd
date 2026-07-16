"""Fleet status endpoint for monitoring and future dashboard."""

from __future__ import annotations

import contextlib
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fleet_manager.server.serializers import OLLAMA_HOT_MODEL_CAP, serialize_node

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
        node_limits.append({
            "node_id": node.node_id,
            "status": node.status.value,
            "hot_model_cap": OLLAMA_HOT_MODEL_CAP,
            "models_loaded": loaded,
            "free_slots": max(0, OLLAMA_HOT_MODEL_CAP - loaded),
        })

    return {
        # Router-side retry budget (per request, across nodes).
        "max_retries": getattr(settings, "max_retries", 0),
        # macOS Ollama hot-load cap — the same on every node.
        "hot_model_cap": OLLAMA_HOT_MODEL_CAP,
        # Per-model in-flight cap the MLX backend enforces (Ollama uses
        # OLLAMA_NUM_PARALLEL, set outside the herd on the node).
        "mlx_max_inflight_per_model": getattr(settings, "mlx_max_inflight_per_model", 1),
        "mlx_max_queue_depth": getattr(settings, "mlx_max_queue_depth", 10),
        "nodes": node_limits,
        "timestamp": time.time(),
    }


@router.post("/fleet/pin")
async def fleet_pin(request: Request):
    """Pin a model resident: pre-warm it now (evicting the LRU if needed) and
    persist the pin so the preloader keeps it warm if it's later evicted.

    Body: ``{"model": "<name>", "node_id": "<optional>"}``.  Reuses the same
    ``PinnedModelsStore`` + ``model_preloader`` machinery as the dashboard —
    this is the one-call replacement for the manual ``curl :11434 keep_alive``
    dance a benchmark otherwise needs.
    """
    from fleet_manager.server.model_preloader import _load_model_on_best_node

    body = await request.json()
    model = (body.get("model") or "").strip()
    node_id = (body.get("node_id") or "").strip() or None
    if not model:
        return JSONResponse({"ok": False, "error": "model required"}, status_code=400)

    registry = request.app.state.registry
    proxy = request.app.state.streaming_proxy
    store = request.app.state.pinned_store

    loaded = await _load_model_on_best_node(
        model, registry.get_online_nodes(), proxy,
        why="fleet-pin", target_node_id=node_id,
    )
    if not loaded:
        return JSONResponse(
            {
                "ok": False,
                "model": model,
                "error": f"'{model}' is not on disk on any online node — "
                f"run 'ollama pull {model}' on a fleet device first.",
            },
            status_code=404,
        )

    # Persist the pin on whichever node now holds it (so the preloader
    # reloads it if evicted).  Prefer the requested node.
    pin_node = node_id
    if pin_node is None:
        for n in registry.get_all_nodes():
            if n.ollama and any(m.name == model for m in n.ollama.models_loaded):
                pin_node = n.node_id
                break
    per_node = store.set_pin(pin_node, model, True) if pin_node else store.load()
    return {"ok": True, "model": model, "pinned_node": pin_node, "per_node": per_node}


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
