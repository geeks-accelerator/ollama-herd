"""Dashboard routes — fleet overview, trends, model insights, SSE stream, and data APIs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

# ---------------------------------------------------------------------------
# Instant-backend model stats cache
# ---------------------------------------------------------------------------
# Text embedding (fastembed) and vision embedding route outside queue_mgr, so
# they have no live pending/in-flight counts.  We synthesise queue cards for
# them using trace DB stats (completed, failed, avg_latency) refreshed every
# 60s — cheap enough to compute but not worth hammering on every 2s SSE tick.

_instant_stats_cache: dict[str, dict] = {}
_instant_stats_ts: float = 0.0
_INSTANT_STATS_TTL_S: float = 60.0


async def _get_instant_model_stats(trace_store) -> dict[str, dict]:
    """Return per-model stats for instant (non-queued) backends from trace DB.

    Keys are model names; values are {total, completed, failed, avg_latency_ms}.
    Refreshed at most every 60s; returns stale cache on error.
    """
    global _instant_stats_cache, _instant_stats_ts
    now = time.time()
    if now - _instant_stats_ts < _INSTANT_STATS_TTL_S and _instant_stats_cache:
        return _instant_stats_cache
    try:
        cutoff = now - 86400  # last 24h
        cursor = await trace_store._read_db.execute(
            """
            SELECT model,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END) AS failed,
                   AVG(CASE WHEN status = 'completed' THEN latency_ms END) AS avg_ms
            FROM request_traces
            WHERE timestamp >= ?
              AND (model LIKE '%embed%'
                   OR (tags IS NOT NULL AND tags LIKE '%"embed"%'))
            GROUP BY model
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            result[row[0]] = {
                "total": row[1] or 0,
                "completed": row[2] or 0,
                "failed": row[3] or 0,
                "avg_latency_ms": round(row[4], 1) if row[4] else 0.0,
            }
        _instant_stats_cache = result
        _instant_stats_ts = now
        return result
    except Exception:
        return _instant_stats_cache  # return stale on error rather than crash


# ---------------------------------------------------------------------------
# Favicon — FontAwesome horse-saddle (sharp duotone) in brand accent purple
# ---------------------------------------------------------------------------

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 640">'
    '<path opacity=".4" fill="#6c63ff" d="M353.2 188.7L353.5 185.9'
    "C361.1 121.4 415.7 72.8 480.6 72.8L555 72.8L589.9 127.3L600.9 238.6"
    "L519.6 278.8L489.4 216.1L472.3 216.1L472.3 320.3L460.6 350.9L441.1"
    " 372.7L441.1 568L351.8 568L351.8 389.9L232 364.6L232 405.2C232 413.3"
    " 230.5 421.3 227.5 428.8L211.8 468.4L238.1 568.1L137.8 568.1L109.3"
    " 460.1L136.1 397.1L136.1 378L96.8 292.7L96.8 272.3C96.8 232.5 129"
    " 200.3 168.8 200.3L193.2 200.3L193.2 226.6C193.2 270.8 229 303.1"
    ' 273.2 303.1C317.4 303.1 353.2 263.5 353.2 226.6L353.2 188.7z"/>'
    '<path fill="#6c63ff" d="M360.5 200C360.5 133.7 414.2 80 480.5 80'
    "L548.2 80L582 130.8L592 235.3L524.1 269.3L495.7 212.5L493.5 208.1"
    "L464.6 208.1L464.6 304.1C464.4 329.5 453 351.7 435.4 366.6L432.6"
    " 369L432.6 560.2L360.6 560.2L360.6 383.4L354.3 382L280.6 365.8"
    "L280.6 311.8C325.5 307.8 360.6 270.1 360.6 224.2L360.6 200.2z"
    "M272.5 296C232.7 296 200.5 263.8 200.5 224L200.5 208L344.5 208"
    "L344.5 224C344.5 263.8 312.3 296 272.5 296zM264.5 311.6L264.5 362.1"
    "C240.9 356.9 227.5 354 224.5 353.3L224.5 416.1L221.3 423.6L203.9"
    " 464.1L202.8 466.7L203.6 469.4L228.7 559.9L145.7 559.9L122 474.6"
    "L118.2 461.1C119.5 458.2 128 438.2 143.9 401.2L144.5 399.7L144.5"
    " 376.6L143.8 375L110.5 301.7C106.6 293.1 104.6 283.7 104.6 274.3"
    "C104.6 237.7 134.3 208 170.9 208L184.6 208L184.6 224C184.6 269.9"
    " 219.7 307.6 264.6 311.6zM224.3 192L170.8 192C144.3 192 120.7 204.5"
    " 105.7 224L96.5 224C61.2 224 32.5 252.7 32.5 288L32.5 384L48.5 384"
    "L48.5 288C48.5 261.7 69.7 240.3 96 240C91.2 250.4 88.5 262.1 88.5"
    " 274.3C88.5 286 91 297.7 95.9 308.4L128.5 380.2L128.5 396.6C94.1"
    " 476.8 104.2 453.1 101.3 460L102.1 462.7L106.6 479L131.9 570.2"
    "L133.5 576.1L249.7 576.1L246.9 566L219.7 468.1C248.7 400.4 237.1"
    " 427.5 240.5 419.6L240.5 373.4L344.5 396.2L344.5 576.1L448.5 576.1"
    "L448.5 376.2C465.8 360.4 477.6 338.3 480 313.2L480.5 313.3L480.5"
    " 224.2L483.6 224.2L513.4 283.8L517 291L524.2 287.4L604.2 247.4"
    "L609.1 244.9L608.6 239.4L597.9 127.4L597.7 125.4L596.6 123.7"
    "L567.6 80.1L592.7 80.1L592.7 64.1L480.7 64.1C408.1 64 348.9 120.6"
    " 344.7 192L224.3 192zM544.5 144C544.5 135.2 537.3 128 528.5 128"
    "C519.7 128 512.5 135.2 512.5 144C512.5 152.8 519.7 160 528.5 160"
    'C537.3 160 544.5 152.8 544.5 144z"/>'
    "</svg>"
)


@router.get("/favicon.svg")
async def favicon_svg():
    """Serve the horse-saddle favicon as SVG."""
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@router.get("/favicon.ico")
async def favicon_ico():
    """Redirect favicon.ico to SVG favicon."""
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# Same art, alert red.  Swapped into the tab when a node drops, so a
# backgrounded dashboard shows the fleet is down at a glance -- this is the
# half of the alert that still works when notification permission was never
# granted or was denied, which is the common case.
_FAVICON_SVG_ALERT = _FAVICON_SVG.replace("#6c63ff", "#ef4444")

# The dashboard's only third-party script.  Pinned to an exact file with a
# Subresource Integrity hash, so a compromised or mistaken npm publish cannot
# run in the dashboard's origin: the browser refuses any byte that differs.
# To upgrade, change the version and regenerate the hash from the exact URL:
#   curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A
_CHART_JS_HEAD = (
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js" '
    'integrity="sha384-jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2OxczGA3bGNeWe+6DChMTBJemed7ZnvJ" '
    'crossorigin="anonymous"></script>'
)


@router.get("/favicon-alert.svg")
async def favicon_alert_svg():
    """Serve the alert-state (red) favicon."""
    return Response(content=_FAVICON_SVG_ALERT, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# SSE event stream (shared by the fleet overview page)
# ---------------------------------------------------------------------------


@router.get("/dashboard/events")
async def dashboard_events(request: Request):
    """SSE endpoint for real-time fleet state updates."""

    async def event_stream():
        # A client that walks away without a clean close (a laptop sleeping, a
        # browser tab backgrounded, a dropped Wi-Fi) leaves the TCP connection
        # ESTABLISHED but dead.  Without an explicit disconnect check this
        # generator loops forever, rebuilding full fleet state every 2s for a
        # reader that will never read again — one such stuck stream per stale
        # tab, accumulating.  `is_disconnected()` is how Starlette exposes that
        # the peer has gone; check it before doing the work.
        # Captured once, not per tick.  These are set during app startup and
        # never rebound, so re-reading them every 2 s for the life of every open
        # dashboard tab is pure overhead -- and it obscured that the loop body's
        # cost is rebuilding fleet state, not fetching these handles.
        registry = request.app.state.registry
        queue_mgr = request.app.state.queue_mgr
        mlx_proxy = getattr(request.app.state, "mlx_proxy", None)

        while not await request.is_disconnected():
            nodes = []
            for node in registry.get_all_nodes():
                node_data = {
                    "node_id": node.node_id,
                    "status": node.status.value,
                    "hardware": {
                        "memory_total_gb": node.hardware.memory_total_gb,
                        "cores_physical": node.hardware.cores_physical,
                        "chip": node.hardware.chip,
                        "memory_bandwidth_gbps": node.hardware.memory_bandwidth_gbps,
                        "arch": node.hardware.arch,
                    },
                }
                if node.cpu:
                    node_data["cpu"] = {
                        "cores_physical": node.cpu.cores_physical,
                        "utilization_pct": node.cpu.utilization_pct,
                    }
                if node.memory:
                    node_data["memory"] = {
                        "total_gb": round(node.memory.total_gb, 1),
                        "used_gb": round(node.memory.used_gb, 1),
                        "available_gb": round(node.memory.available_gb, 1),
                        "pressure": node.memory.pressure.value,
                        "compressed_gb": round(node.memory.compressed_gb, 1),
                        "swap_used_gb": round(node.memory.swap_used_gb, 1),
                        "swap_total_gb": round(node.memory.swap_total_gb, 1),
                    }
                if node.thermal:
                    node_data["thermal"] = {
                        "state": node.thermal.state.value,
                        "temperature_c": node.thermal.temperature_c,
                        "source": node.thermal.source,
                    }
                if node.ollama:
                    # MLX-prefixed entries are advertised by the node-side
                    # MlxClient when FLEET_NODE_MLX_ENABLED=true.  Surface them
                    # separately so the dashboard renders a "loaded via MLX"
                    # row instead of confusing them with on-disk Ollama models.
                    mlx_models = [
                        m
                        for m in node.ollama.models_available
                        if isinstance(m, str) and m.startswith("mlx:")
                    ]
                    ollama_avail_count = len(node.ollama.models_available) - len(mlx_models)
                    node_data["ollama"] = {
                        "models_loaded": [
                            {
                                "name": m.name,
                                "size_gb": round(m.size_gb, 2),
                                "parameter_size": m.parameter_size,
                                "quantization": m.quantization,
                                "context_length": m.context_length,
                                # Which runner Ollama chose. 0.40 makes MLX the
                                # default on Apple Silicon and decides per model,
                                # so "served by Ollama" is no longer one thing.
                                "runner": m.runner,
                            }
                            for m in node.ollama.models_loaded
                        ],
                        "models_available_count": ollama_avail_count,
                        "mlx_models": mlx_models,
                        "requests_active": node.ollama.requests_active,
                    }
                if node.image and node.image.models_available:
                    node_data["image_models"] = [m.name for m in node.image.models_available]
                if node.transcription and node.transcription.models_available:
                    node_data["stt_models"] = [m.name for m in node.transcription.models_available]
                # Embedding models: Ollama models with "embed" in the name
                if node.ollama:
                    embed_models = [m for m in node.ollama.models_available if "embed" in m.lower()]
                    if embed_models:
                        node_data["embed_models"] = embed_models
                # Vision embedding models (DINOv2, SigLIP, CLIP)
                if node.vision_embedding and node.vision_embedding.models_available:
                    node_data["vision_embed_models"] = [
                        m.name for m in node.vision_embedding.models_available
                    ]
                # Multi-MLX per-server status — empty for nodes without MLX
                # or with an older agent that predates this field.
                if node.mlx_servers:
                    node_data["mlx_servers"] = [
                        {
                            "port": s.port,
                            "model": s.model,
                            "status": s.status,
                            "status_reason": s.status_reason,
                            "kv_bits": s.kv_bits,
                            "model_size_gb": round(s.model_size_gb, 1),
                            "last_ok_ts": s.last_ok_ts,
                            "distributed": s.distributed,
                            "backend": s.backend,
                            "node_count": s.node_count,
                        }
                        for s in node.mlx_servers
                    ]
                    node_data["mlx_bind_host"] = node.mlx_bind_host
                if node.capacity:
                    node_data["capacity"] = {
                        "mode": node.capacity.mode,
                        "ceiling_gb": round(node.capacity.ceiling_gb, 1),
                        "availability_score": round(node.capacity.availability_score, 3),
                        "reason": node.capacity.reason,
                        "override_active": node.capacity.override_active,
                        "learning_confidence": round(node.capacity.learning_confidence, 2),
                        "days_observed": node.capacity.days_observed,
                    }
                nodes.append(node_data)

            queues = queue_mgr.get_queue_info()
            # Merge MLX synthetic queues so the dashboard shows MLX traffic
            # alongside Ollama queues (single unified backends-agnostic view).
            if mlx_proxy is not None:
                with contextlib.suppress(Exception):
                    queues = {**queues, **mlx_proxy.get_queue_info()}

            # Merge synthetic cards for instant backends (fastembed text
            # embedding + vision embedding).  These never go through queue_mgr
            # so they have no live pending/in-flight counts, but we show them
            # so the "Node Models" section covers every model receiving traffic.
            # Loaded-but-idle Ollama models.  Queues are created lazily on the
            # first request, so a model that is resident but hasn't been asked
            # for anything yet has no queue — and therefore vanished from a
            # section literally called "Node Models" while the node card above
            # still listed it.  That mismatch is confusing (reported
            # 2026-07-18: "why does HERD NODES show gpt-oss:120b but NODE
            # MODELS doesn't?").  Synthesize a zeroed card so residency and the
            # model list agree; real queue data overwrites it the moment
            # traffic arrives.
            for node in registry.get_all_nodes():
                if node.status.value != "online" or not node.ollama:
                    continue
                for lm in node.ollama.models_loaded or []:
                    key = f"{node.node_id}:{lm.name}"
                    if key in queues:
                        continue  # a real queue always wins
                    queues[key] = {
                        "node_id": node.node_id,
                        "model": lm.name,
                        "backend": "ollama",
                        "request_type": "text",
                        "idle": True,
                        "pending": 0,
                        "in_flight": 0,
                        "concurrency": 0,
                        "completed": 0,
                        "failed": 0,
                        "stats_samples": 0,
                        "avg_latency_ms": 0.0,
                    }

            trace_store = getattr(request.app.state, "trace_store", None)
            if trace_store:
                with contextlib.suppress(Exception):
                    instant_stats = await _get_instant_model_stats(trace_store)
                    for node in registry.get_all_nodes():
                        if node.status.value != "online":
                            continue
                        # Native text embedding (fastembed, port 11439)
                        if node.text_embedding and node.text_embedding_port > 0:
                            for m in node.text_embedding.models_available:
                                key = f"{node.node_id}:{m.name}"
                                st = instant_stats.get(m.name, {})
                                queues[key] = {
                                    "node_id": node.node_id,
                                    "model": m.name,
                                    "backend": "native",
                                    "request_type": m.kind,  # "embed" | "rerank"
                                    "instant": True,
                                    "pending": 0,
                                    "in_flight": 0,
                                    "concurrency": 0,
                                    "completed": st.get("completed", 0),
                                    "failed": st.get("failed", 0),
                                    "avg_latency_ms": st.get("avg_latency_ms", 0.0),
                                    "stats_samples": st.get("total", 0),
                                    "dimensions": m.dimensions,
                                    "cached": m.cached,
                                }
                        # Vision embedding (DINOv2, SigLIP, CLIP — port 11438)
                        if node.vision_embedding and node.vision_embedding_port > 0:
                            for m in node.vision_embedding.models_available:
                                key = f"{node.node_id}:{m.name}"
                                st = instant_stats.get(m.name, {})
                                queues[key] = {
                                    "node_id": node.node_id,
                                    "model": m.name,
                                    "backend": "vision",
                                    "request_type": "embed",
                                    "instant": True,
                                    "pending": 0,
                                    "in_flight": 0,
                                    "concurrency": 0,
                                    "completed": st.get("completed", 0),
                                    "failed": st.get("failed", 0),
                                    "avg_latency_ms": st.get("avg_latency_ms", 0.0),
                                    "stats_samples": st.get("total", 0),
                                    "dimensions": m.dimensions,
                                }

            data = {
                "nodes": nodes,
                "queues": queues,
                "timestamp": time.time(),
            }

            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(2)

    async def guarded_stream():
        # One bad iteration must not take the connection — or, worse, propagate
        # out through the ASGI layer where an unhandled generator exception can
        # leave the transport in a half-open state.  A transient (a node
        # dropping mid-serialisation, a stat dict momentarily the wrong shape)
        # should end this one stream cleanly; the browser's EventSource
        # reconnects on its own.  CancelledError is normal teardown and is
        # re-raised untouched.
        # This stream IS the "somebody is watching" signal the offline
        # alerter uses to decide whether to open a browser window.  The
        # decrement has to run on every exit path -- a leaked increment would
        # make the fleet look permanently watched and silence the alert
        # exactly when nobody is there, so it lives in a finally.
        alerter = getattr(request.app.state, "offline_alerter", None)
        if alerter is not None:
            alerter.client_connected()
        try:
            async for chunk in event_stream():
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("dashboard SSE stream ended on an unexpected error")
            return
        finally:
            if alerter is not None:
                alerter.client_disconnected()

    return StreamingResponse(
        guarded_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# JSON data APIs for dashboard pages
# ---------------------------------------------------------------------------


@router.get("/dashboard/api/trends")
async def dashboard_trends_data(
    request: Request,
    hours: int = 72,
    start_ts: float = 0,
    end_ts: float = 0,
):
    """Hourly aggregated request counts and latencies for the trends chart."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {"hours": hours, "data": []}
    data = await latency_store.get_hourly_trends(
        hours=hours,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    return {"hours": hours, "data": data}


@router.get("/dashboard/api/models")
async def dashboard_models_data(
    request: Request,
    days: int = 7,
    start_ts: float = 0,
    end_ts: float = 0,
):
    """Per-model daily aggregated stats for the model insights page."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {"days": days, "daily": [], "summary": []}
    daily = await latency_store.get_model_daily_stats(
        days=days,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    summary = await latency_store.get_model_summary(start_ts=start_ts, end_ts=end_ts)
    return {"days": days, "daily": daily, "summary": summary}


@router.get("/dashboard/api/overview")
async def dashboard_overview_data(request: Request):
    """Quick summary stats for dashboard header cards."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {
            "total_requests": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "models_count": 0,
        }
    summary = await latency_store.get_model_summary()
    total_requests = sum(m["total_requests"] for m in summary)
    total_prompt = sum(m["total_prompt_tokens"] for m in summary)
    total_completion = sum(m["total_completion_tokens"] for m in summary)
    return {
        "total_requests": total_requests,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "models_count": len(summary),
    }


@router.get("/dashboard/api/usage")
async def dashboard_usage_data(request: Request, days: int = 7):
    """Per-node, per-model, per-day usage stats from request traces."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"days": days, "data": []}
    data = await trace_store.get_usage_by_node_model_day(days=days)
    return {"days": days, "data": data}


@router.get("/dashboard/api/traces")
async def dashboard_traces(request: Request, limit: int = 50):
    """Recent request traces for debugging and observability."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"traces": []}
    traces = await trace_store.get_recent_traces(limit=limit)
    return {"traces": traces}


@router.get("/dashboard/api/tags")
@router.get("/dashboard/api/apps")  # backwards compat
async def dashboard_tags_data(
    request: Request,
    days: int = 7,
    start_ts: float = 0,
    end_ts: float = 0,
):
    """Per-tag aggregated stats for the Apps analytics page."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"days": days, "data": [], "summary": []}
    data = await trace_store.get_usage_by_tag(
        days=days,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    summary = await trace_store.get_tag_summary()
    return {"days": days, "data": data, "summary": summary}


@router.get("/dashboard/api/tags/daily")
@router.get("/dashboard/api/apps/daily")  # backwards compat
async def dashboard_tags_daily_data(
    request: Request,
    days: int = 7,
    start_ts: float = 0,
    end_ts: float = 0,
):
    """Per-tag, per-day breakdown for the Apps analytics charts."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"days": days, "data": []}
    data = await trace_store.get_tag_daily_stats(
        days=days,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    return {"days": days, "data": data}


@router.post("/dashboard/api/benchmarks")
async def save_benchmark(request: Request):
    """Save benchmark results from the benchmark script."""
    data = await request.json()
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"error": "trace store not available"}
    await trace_store.save_benchmark_run(data)
    return {"status": "saved", "run_id": data.get("run_id")}


@router.get("/dashboard/api/benchmarks")
async def get_benchmarks(request: Request, limit: int = 50):
    """List benchmark runs, newest first."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"data": []}
    data = await trace_store.get_benchmark_runs(limit=limit)
    return {"data": data}


@router.post("/dashboard/api/benchmarks/start")
async def start_benchmark(request: Request):
    """Start a benchmark run. Mode: 'default' or 'smart'."""
    from fleet_manager.server.benchmark_runner import BenchmarkRunner

    body = await request.json()
    mode = body.get("mode", "default")
    duration = body.get("duration", 300)
    model_types = body.get("model_types", ["llm"])

    # Get or create runner on app state
    runner = getattr(request.app.state, "benchmark_runner", None)
    if runner is None:
        settings = request.app.state.settings
        runner = BenchmarkRunner(f"http://localhost:{settings.port}")
        request.app.state.benchmark_runner = runner

    if runner.is_running:
        return JSONResponse(
            status_code=409,
            content={"error": "Benchmark already running", "progress": runner.get_progress()},
        )

    run_id = await runner.start(
        mode=mode,
        duration=duration,
        model_types=model_types,
        registry=request.app.state.registry,
        trace_store=getattr(request.app.state, "trace_store", None),
        streaming_proxy=request.app.state.streaming_proxy,
        scorer=request.app.state.scorer,
    )
    return {
        "status": "started",
        "run_id": run_id,
        "mode": mode,
        "duration": duration,
        "model_types": model_types,
    }


@router.get("/dashboard/api/benchmarks/progress")
async def benchmark_progress(request: Request):
    """Get current benchmark status and progress."""
    runner = getattr(request.app.state, "benchmark_runner", None)
    if runner is None:
        return {"status": "idle", "phase": "No benchmark has been run"}
    return runner.get_progress()


@router.post("/dashboard/api/benchmarks/cancel")
async def cancel_benchmark(request: Request):
    """Cancel a running benchmark."""
    runner = getattr(request.app.state, "benchmark_runner", None)
    if runner is None or not runner.is_running:
        return {"status": "not_running"}
    runner.cancel()
    return {"status": "cancelled"}


@router.get("/dashboard/api/benchmarks/{run_id}")
async def get_benchmark_detail(request: Request, run_id: str):
    """Get a single benchmark run detail."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"data": None}
    data = await trace_store.get_benchmark_run(run_id)
    return {"data": data}


# 30 s response cache for /dashboard/api/health.  The endpoint runs all
# 18 health checks, several of which aggregate the trace DB over 24 h
# windows — measured at ~450 ms per call.  At the dashboard's 15 s poll
# cadence, that's ~3 % continuous CPU per active dashboard tab.
#
# 30 s TTL is the sweet spot: at the 15 s poll cadence, dashboard ALWAYS
# hits cache on alternate polls (50 % reduction = 2× fewer expensive
# computations).  Underlying signals are aggregated over windows of
# minutes-to-hours, so 30 s of staleness is invisible to operators.
# 15 s TTL would coincide with the poll interval and give effectively 0 %
# cache hit due to timing alignment — see the analysis in the commit
# message for ``9ff8a54`` follow-up.
_HEALTH_CACHE: dict = {"ts": 0.0, "payload": None}
_HEALTH_CACHE_TTL_S = 30.0


@router.get("/dashboard/api/health")
async def dashboard_health_data(request: Request):
    """Fleet health analysis with actionable recommendations.

    Response is cached for ``_HEALTH_CACHE_TTL_S`` seconds — see the
    cache definition above for rationale.  Cache slot is shared across
    all callers; no per-request keying because the endpoint doesn't read
    any request-specific input.
    """
    import time as _time

    from fleet_manager.server.health_engine import HealthEngine

    now = _time.monotonic()
    cached_payload = _HEALTH_CACHE["payload"]
    if cached_payload is not None and now - _HEALTH_CACHE["ts"] < _HEALTH_CACHE_TTL_S:
        return cached_payload

    registry = request.app.state.registry
    trace_store = getattr(request.app.state, "trace_store", None)

    engine = HealthEngine()
    report = await engine.analyze(registry, trace_store)
    payload = report.model_dump()
    _HEALTH_CACHE["payload"] = payload
    _HEALTH_CACHE["ts"] = now
    return payload


@router.get("/dashboard/api/context-usage")
async def context_usage(request: Request, days: int = 7):
    """Per-model context usage stats — actual vs allocated."""
    trace_store = getattr(request.app.state, "trace_store", None)
    registry = request.app.state.registry
    settings = request.app.state.settings

    token_stats = await trace_store.get_prompt_token_stats(days=days) if trace_store else []

    # Build model → allocated context map from fleet state
    allocated_ctx: dict[str, int] = {}
    for node in registry.get_online_nodes():
        if not node.ollama:
            continue
        for m in node.ollama.models_loaded:
            allocated_ctx[m.name] = max(allocated_ctx.get(m.name, 0), m.context_length or 0)

    # Get current overrides
    overrides = getattr(settings, "num_ctx_overrides", {})

    models = []
    for stats in token_stats:
        model = stats["model"]
        alloc = allocated_ctx.get(model, 0)
        total_p99 = stats.get("total_p99", 0)
        max_total_24h = stats.get("max_total_24h", 0)

        # Recommend based on p99 of total tokens (not raw max — avoids outlier skew)
        from fleet_manager.server.context_optimizer import compute_recommended_ctx

        recommended = compute_recommended_ctx(total_p99, max_total_24h)
        recommended = min(recommended, alloc) if alloc > 0 else recommended

        util_pct = round((total_p99 / alloc) * 100, 1) if alloc > 0 else 0
        savings_pct = round(max(0, (alloc - recommended) / alloc * 100), 1) if alloc > 0 else 0

        models.append(
            {
                "model": model,
                "allocated_ctx": alloc,
                "override_ctx": overrides.get(model),
                "request_count": stats["request_count"],
                "prompt_tokens": {
                    "avg": stats["avg_prompt"],
                    "p50": stats["prompt_p50"],
                    "p75": stats["prompt_p75"],
                    "p95": stats["prompt_p95"],
                    "p99": stats["prompt_p99"],
                    "max": stats["max_prompt"],
                },
                "total_tokens": {
                    "p95": stats["total_p95"],
                    "p99": total_p99,
                    "max": stats["max_total"],
                    "max_24h": max_total_24h,
                },
                "utilization_pct": util_pct,
                "recommended_ctx": recommended,
                "savings_pct": savings_pct,
            }
        )

    return {"days": days, "models": models}


_recommendations_cache: dict = {"data": None, "generated_at": 0.0}

# Fleet Intelligence briefing cache
_briefing_cache: dict = {
    "briefing": None,
    "model": None,
    "generated_at": 0.0,
    "generating": False,
}
_briefing_history: list[dict] = []  # Last 10 briefings
_BRIEFING_HISTORY_MAX = 10

EMBED_SKIP = ("embed", "nomic", "bge", "e5-")


def _compute_briefing_interval(registry, trace_store) -> float:
    """Adaptive refresh: back off when fleet is busy, refresh faster when idle.

    The briefing itself consumes LLM compute (calls gpt-oss:120b or similar),
    so we back off during heavy load to avoid competing with real requests.
    When idle, we refresh more often so overnight silent failures get caught
    within an hour of starting.
    """
    nodes = registry.get_online_nodes()
    if not nodes:
        # Static "fleet offline" briefing — no LLM to call, no point
        # refreshing often.  Cache for 1h; the static text never changes
        # until a node comes online (which resets the cache anyway).
        return 3600  # 1h

    # Check queue activity
    total_in_flight = 0
    for node in nodes:
        if node.ollama:
            total_in_flight += node.ollama.requests_active or 0

    if total_in_flight > 5:
        return 7200  # 2 hours when very busy (don't steal compute)
    if total_in_flight > 0:
        return 3600  # 1 hour when active
    return 1800  # 30 min when idle (catch silent failures fast)


async def _generate_briefing(request) -> dict:
    """Generate a fleet intelligence briefing using an internal LLM call."""
    settings = request.app.state.settings
    registry = request.app.state.registry
    trace_store = getattr(request.app.state, "trace_store", None)
    health_engine = getattr(request.app.state, "health_engine", None)

    # Static briefing when no nodes are online — no LLM available to call
    if not registry.get_online_nodes():
        return {
            "briefing": (
                "- **Fleet offline** — no nodes are currently online. "
                "Fleet Intelligence cannot generate briefings without "
                "at least one node serving an LLM.\n"
                "- Start a node with `herd-node` on any device running "
                "Ollama. See Health page for connection status."
            ),
            "model": "static",
            "generated_at": time.time(),
        }

    # Auto-select model: first loaded LLM (skip embed/image)
    model = settings.fleet_intelligence_model
    if not model:
        for node in registry.get_online_nodes():
            if not node.ollama:
                continue
            for m in node.ollama.models_loaded:
                nm = m.name.lower()
                if (
                    not any(p in nm for p in EMBED_SKIP)
                    and "image" not in nm
                    and not nm.startswith("x/")
                ):
                    model = m.name
                    break
            if model:
                break
    if not model:
        return {"error": "No LLM models loaded", "briefing": None}

    # Gather fleet data
    nodes = registry.get_online_nodes()
    nodes_online = len(nodes)
    models_loaded = sum(len(n.ollama.models_loaded) if n.ollama else 0 for n in nodes)
    total_available_gb = sum(n.memory.available_gb if n.memory else 0 for n in nodes)

    # Health data — include ALL warnings/criticals
    health_summary = ""
    if health_engine and trace_store:
        try:
            report = await health_engine.analyze(registry, trace_store)
            health_summary = f"Health: {report.vitals.health_score}/100."
            critical = [r for r in report.recommendations if r.severity.value == "critical"]
            warnings = [r for r in report.recommendations if r.severity.value == "warning"]
            if critical:
                health_summary += f" {len(critical)} critical issue(s): "
                health_summary += "; ".join(r.title for r in critical)
                health_summary += "."
            if warnings:
                health_summary += f" {len(warnings)} warning(s): "
                health_summary += "; ".join(r.title for r in warnings)
                health_summary += "."
        except Exception:
            health_summary = "Health: unable to analyze."

    # Traffic data + per-model breakdown
    traffic_summary = ""
    if trace_store:
        try:
            overall = await trace_store.get_overall_stats_24h()
            ttft = overall["avg_ttft_ms"]
            ttft_str = f"{ttft:.0f}ms" if ttft is not None else "N/A"
            traffic_summary = (
                f"Traffic (24h): {overall['total_requests']:,} requests, "
                f"{overall['error_rate_pct']:.1f}% errors, "
                f"{overall['total_retries']} retries, "
                f"avg TTFT {ttft_str}."
            )
            # Per-model breakdown
            model_usage = await trace_store.get_usage_by_node_model_day(days=1)
            if model_usage:
                from collections import defaultdict

                model_reqs: dict[str, int] = defaultdict(int)
                for entry in model_usage:
                    model_reqs[entry["model"]] += entry["request_count"]
                parts = [f"{m}: {c}" for m, c in sorted(model_reqs.items(), key=lambda x: -x[1])]
                traffic_summary += f" Per-model: {', '.join(parts)}."
        except Exception:
            pass

    # Context usage
    context_summary = ""
    if trace_store:
        try:
            stats = await trace_store.get_prompt_token_stats(days=7)
            wasteful = []
            for s in stats:
                alloc = 0
                for node in nodes:
                    if node.ollama:
                        for m in node.ollama.models_loaded:
                            if m.name == s["model"]:
                                alloc = max(alloc, m.context_length or 0)
                total_p99 = s.get("total_p99", 0)
                if alloc > 0 and total_p99 > 0 and alloc / total_p99 > 4:
                    wasteful.append(f"{s['model']} ({(total_p99 / alloc * 100):.0f}% utilization)")
            if wasteful:
                context_summary = f"Context waste: {', '.join(wasteful[:3])}."
        except Exception:
            pass

    # Connection failures
    conn_summary = ""
    for node in nodes:
        total = node.connection_failures_total
        recent = node.connection_failures
        if total > 0:
            if recent > 0:
                conn_summary += (
                    f"ACTIVE: {node.node_id} has {recent} connection failures "
                    f"right now ({total} total). "
                )
            elif total > 50:
                conn_summary += f"{node.node_id} recovered from {total} connection failures. "

    # Silent fallback detection — requests served by wrong model
    # (VRAM fallback routed away from the requested model)
    outage_summary = ""
    if trace_store:
        try:
            fallback_stats = await trace_store.get_silent_fallback_stats(lookback_s=86400)
            if fallback_stats:
                # Show top 3 silent fallback pairs
                parts = []
                total_fallbacks = sum(s["count"] for s in fallback_stats)
                for s in fallback_stats[:3]:
                    parts.append(f"{s['count']} reqs for {s['requested']} served by {s['actual']}")
                outage_summary = (
                    f"SILENT FALLBACK in last 24h: {total_fallbacks} "
                    f"requests were served by a different model than "
                    f"requested. Top pairs: {'; '.join(parts)}. "
                    f"This indicates the requested model was evicted "
                    f"from memory — check priority preloader logs."
                )
        except Exception:
            pass

    # Per-node details
    node_details = ""
    for node in nodes:
        cpu = node.cpu.utilization_pct if node.cpu else 0
        mem_used = node.memory.used_gb if node.memory else 0
        mem_total = node.memory.total_gb if node.memory else 0
        pressure = node.memory.pressure.value if node.memory else "unknown"
        loaded = [m.name for m in node.ollama.models_loaded] if node.ollama else []
        active = node.ollama.requests_active if node.ollama else 0
        disk_avail = f", disk {node.disk.available_gb:.0f}GB free" if node.disk else ""
        node_details += (
            f"  {node.node_id}: CPU {cpu:.0f}%, "
            f"mem {mem_used:.0f}/{mem_total:.0f}GB ({pressure}){disk_avail}, "
            f"models={loaded}, active={active}\n"
        )

    # Queue state
    queue_summary = ""
    queue_mgr = getattr(request.app.state, "queue_mgr", None)
    if queue_mgr:
        queues = queue_mgr.get_queue_info()
        total_pending = sum(q.get("pending", 0) for q in queues.values())
        total_inflight = sum(q.get("in_flight", 0) for q in queues.values())
        total_failed = sum(q.get("failed", 0) for q in queues.values())
        if total_pending > 0 or total_failed > 0:
            queue_summary = (
                f"Queues: {total_pending} pending, {total_inflight} in-flight, "
                f"{total_failed} failed."
            )
        else:
            queue_summary = f"Queues: clear ({total_inflight} in-flight)."

    # Priority model status
    priority_summary = ""
    if trace_store:
        try:
            priorities = await trace_store.get_model_priority_scores()
            if priorities:
                top3 = priorities[:3]
                parts = [
                    f"{p['model']}: score={p['priority_score']:.0f} ({p['requests_24h']} reqs/24h)"
                    for p in top3
                ]
                priority_summary = f"Model priority (by usage): {'; '.join(parts)}."
        except Exception:
            pass

    # Previous briefings for continuity
    prev_briefings = ""
    ts = getattr(request.app.state, "trace_store", None)
    if ts:
        try:
            history = await ts.get_briefings(limit=2)
            if history:
                prev_briefings = (
                    "Your previous briefing(s) (for context — don't repeat, note what changed):\n"
                )
                for h in history:
                    ago = int((time.time() - h["generated_at"]) / 60)
                    prev_briefings += f"  [{ago}m ago]: {h['briefing'][:500]}\n"
        except Exception:
            pass

    # Build prompt
    prompt = f"""Current fleet state:
- {nodes_online} node(s) online, {models_loaded} model(s) loaded, {total_available_gb:.0f}GB available memory
- {health_summary}
- {traffic_summary}
- {context_summary}
- {conn_summary if conn_summary else "No connection issues."}
- {outage_summary if outage_summary else "No silent fallback detected in last 24h."}
- {priority_summary if priority_summary else "No model usage history yet."}
- {queue_summary}
Nodes:
{node_details}
{prev_briefings}
Provide a high-level fleet summary (2-3 short bullet points). Rules:
1. Summarize the overall state — don't repeat specific numbers from the data
2. Point to the right dashboard page for details: "See Health page", "See Recommendations", "See Settings > Context Management"
3. If there are issues, mention the category (health, context, capacity) and which page has details
4. If everything is healthy, say so in one sentence — don't invent problems
5. Keep it brief — this is a summary with signposts, not a detailed report"""

    # Internal LLM call via httpx
    import httpx

    base_url = f"http://localhost:{settings.port}"
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a fleet operations analyst for Ollama Herd, "
                                "an AI inference router. Be concise and actionable. "
                                "Available actions operators can take: "
                                "add a new node (run 'herd-node' on another machine), "
                                'pull a model (curl /api/pull -d \'{"name":"model"}\'), '
                                "enable dynamic num_ctx in Settings, "
                                "set OLLAMA_NUM_PARALLEL=2 in ~/.zshrc, "
                                "check the Health or Recommendations dashboard pages. "
                                "Do NOT suggest commands that don't exist. "
                                "Do NOT suggest unload_context, load_model, or other "
                                "fictional commands."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "options": {"num_predict": 300, "temperature": 0.3},
                    "metadata": {"tags": ["fleet-intelligence"]},
                },
            )
        if resp.status_code != 200:
            return {"error": f"LLM call failed: HTTP {resp.status_code}", "briefing": None}
        data = resp.json()
        return {
            "briefing": data.get("message", {}).get("content", ""),
            "model": model,
            "generated_at": time.time(),
        }
    except Exception as e:
        return {"error": str(e)[:200], "briefing": None}


@router.get("/dashboard/api/briefing")
async def dashboard_briefing(request: Request, refresh: int = 0):
    """Fleet intelligence briefing — LLM-powered analysis of fleet state.

    Cached with adaptive refresh interval based on fleet activity.
    Pass ?refresh=1 to force regeneration.
    """
    settings = request.app.state.settings
    if not settings.fleet_intelligence:
        return {"enabled": False, "briefing": None}

    cache = _briefing_cache
    now = time.time()
    registry = request.app.state.registry
    trace_store = getattr(request.app.state, "trace_store", None)

    interval = _compute_briefing_interval(registry, trace_store)
    is_stale = (now - cache["generated_at"]) > interval

    # Return cached if fresh (or already generating)
    if not is_stale and not refresh and cache["briefing"]:
        next_refresh = max(0, interval - (now - cache["generated_at"]))
        return {
            "enabled": True,
            "briefing": cache["briefing"],
            "model": cache["model"],
            "generated_at": cache["generated_at"],
            "next_refresh_s": int(next_refresh),
            "cached": True,
        }

    if cache["generating"]:
        return {
            "enabled": True,
            "briefing": cache.get("briefing"),
            "model": cache.get("model"),
            "generated_at": cache.get("generated_at", 0),
            "generating": True,
        }

    # Generate new briefing
    cache["generating"] = True
    try:
        result = await _generate_briefing(request)
        if result.get("briefing"):
            cache["briefing"] = result["briefing"]
            cache["model"] = result["model"]
            cache["generated_at"] = result["generated_at"]
            # Save to in-memory history
            _briefing_history.insert(
                0,
                {
                    "briefing": result["briefing"],
                    "model": result["model"],
                    "generated_at": result["generated_at"],
                },
            )
            if len(_briefing_history) > _BRIEFING_HISTORY_MAX:
                _briefing_history.pop()
            # Persist to SQLite
            ts = getattr(request.app.state, "trace_store", None)
            if ts:
                import asyncio

                asyncio.create_task(ts.save_briefing(result["briefing"], result["model"]))
        next_refresh = interval
        return {
            "enabled": True,
            "briefing": cache["briefing"],
            "model": cache.get("model"),
            "generated_at": cache.get("generated_at", 0),
            "next_refresh_s": int(next_refresh),
            "cached": False,
            "error": result.get("error"),
        }
    finally:
        cache["generating"] = False


@router.get("/dashboard/api/briefing/history")
async def dashboard_briefing_history(request: Request, limit: int = 20):
    """Return recent fleet intelligence briefings from SQLite."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if trace_store:
        history = await trace_store.get_briefings(limit=limit)
        if history:
            return {"history": history}
    # Fallback to in-memory
    return {"history": _briefing_history}


@router.get("/dashboard/api/recommendations")
async def dashboard_recommendations_data(request: Request, refresh: int = 0):
    """Model mix recommendations based on fleet hardware and usage patterns.

    Results are cached for 5 minutes.  Pass ?refresh=1 to force re-analysis.
    """
    from fleet_manager.server.model_recommender import ModelRecommender

    cache = _recommendations_cache
    now = time.time()
    stale = now - cache["generated_at"] > 300  # 5 min TTL

    if not stale and not refresh and cache["data"] is not None:
        return {**cache["data"], "generated_at": cache["generated_at"]}

    registry = request.app.state.registry
    trace_store = getattr(request.app.state, "trace_store", None)

    nodes = registry.get_all_nodes()
    usage_data = []
    if trace_store:
        usage_data = await trace_store.get_usage_by_node_model_day(days=1)

    recommender = ModelRecommender()
    report = recommender.analyze(nodes, usage_data)
    result = report.model_dump()

    cache["data"] = result
    cache["generated_at"] = now

    return {**result, "generated_at": now}


def _read_node_telemetry_state() -> dict:
    """Read node-scoped telemetry settings from ~/.fleet-manager/env.

    Deliberately reads the file rather than ``os.environ``: the dashboard
    writes there, the node reads it on next start, and this process's environ
    is a stale snapshot from boot.  A shell export still wins at load time, so
    ``shell_override`` tells the UI when a change here would have no effect.
    """
    import os

    from fleet_manager.common.env_writer import DEFAULT_ENV_FILE

    enabled, nickname = True, ""
    try:
        if DEFAULT_ENV_FILE.exists():
            for raw in DEFAULT_ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].lstrip()
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key == "FLEET_NODE_TELEMETRY":
                    enabled = value.lower() not in ("false", "0", "no", "off")
                elif key == "FLEET_NODE_HERD_NICKNAME":
                    nickname = value
    except OSError:
        pass

    return {
        "enabled": enabled,
        "nickname": nickname,
        "anonymous": nickname == "",
        "shell_override": any(
            k in os.environ for k in ("FLEET_NODE_TELEMETRY", "FLEET_NODE_HERD_NICKNAME")
        ),
    }


@router.get("/dashboard/api/settings")
async def dashboard_settings_data(request: Request):
    """Current configuration values and node list for the settings page."""
    import socket

    from fleet_manager import __version__

    settings = request.app.state.settings
    registry = request.app.state.registry
    hostname = socket.gethostname().split(".")[0]

    # Telemetry lives on NodeSettings in the *node* process, so the router has
    # no in-memory copy to read.  Read the persisted file instead of
    # os.environ: the node picks changes up on restart, and the UI must show
    # what is actually saved rather than what this process booted with.
    telemetry_state = _read_node_telemetry_state()

    config = {
        "telemetry": telemetry_state,
        "toggles": {
            "auto_pull": settings.auto_pull,
            "vram_fallback": settings.vram_fallback,
            "image_generation": settings.image_generation,
            "transcription": settings.transcription,
            "dynamic_num_ctx": settings.dynamic_num_ctx,
            "num_ctx_auto_calculate": settings.num_ctx_auto_calculate,
            "fleet_intelligence": settings.fleet_intelligence,
        },
        "context": {
            "context_protection": settings.context_protection,
            "dynamic_num_ctx": settings.dynamic_num_ctx,
            "num_ctx_overrides": settings.num_ctx_overrides,
            "num_ctx_auto_calculate": settings.num_ctx_auto_calculate,
        },
        "server": {
            "host": hostname if settings.host == "0.0.0.0" else settings.host,
            "port": settings.port,
            "data_dir": settings.data_dir,
            "max_retries": settings.max_retries,
        },
        "heartbeat": {
            "heartbeat_interval": settings.heartbeat_interval,
            "heartbeat_timeout": settings.heartbeat_timeout,
            "heartbeat_offline": settings.heartbeat_offline,
        },
        "scoring": {
            "score_model_hot": settings.score_model_hot,
            "score_model_warm": settings.score_model_warm,
            "score_model_cold": settings.score_model_cold,
            "score_memory_fit_max": settings.score_memory_fit_max,
            "score_queue_depth_max_penalty": settings.score_queue_depth_max_penalty,
            "score_queue_depth_penalty_per": settings.score_queue_depth_penalty_per,
            "score_wait_time_max_penalty": settings.score_wait_time_max_penalty,
            "score_role_affinity_max": settings.score_role_affinity_max,
            "score_role_large_threshold_gb": settings.score_role_large_threshold_gb,
            "score_role_small_threshold_gb": settings.score_role_small_threshold_gb,
            "score_availability_trend_max": settings.score_availability_trend_max,
            "score_context_fit_max": settings.score_context_fit_max,
        },
        "rebalancer": {
            "rebalance_interval": settings.rebalance_interval,
            "rebalance_threshold": settings.rebalance_threshold,
            "rebalance_max_per_cycle": settings.rebalance_max_per_cycle,
        },
        "pre_warm": {
            "pre_warm_threshold": settings.pre_warm_threshold,
            "pre_warm_min_availability": settings.pre_warm_min_availability,
        },
        "auto_pull_config": {
            "auto_pull_timeout": settings.auto_pull_timeout,
        },
        "context_protection": {
            "context_protection": settings.context_protection,
        },
        "reaper": {
            "stale_timeout": settings.stale_timeout,
        },
    }

    nodes_data = []
    for node in registry.get_all_nodes():
        models_count = 0
        if node.ollama:
            models_count = len(node.ollama.models_loaded)
        image_models = []
        if node.image:
            image_models = [m.name for m in node.image.models_available]
        stt_models = []
        if node.transcription:
            stt_models = [m.name for m in node.transcription.models_available]
        embed_models = []
        if node.ollama:
            embed_models = [m for m in node.ollama.models_available if "embed" in m.lower()]
        vision_embed_models = []
        if node.vision_embedding:
            vision_embed_models = [m.name for m in node.vision_embedding.models_available]
        nodes_data.append(
            {
                "node_id": node.node_id,
                "status": node.status.value,
                "agent_version": node.agent_version,
                "ip": node.ollama_base_url,
                "models_loaded_count": models_count,
                "is_router": node.node_id == hostname,
                "image_models": image_models,
                "image_port": node.image_port,
                "stt_models": stt_models,
                "transcription_port": node.transcription_port,
                "embed_models": embed_models,
                "vision_embed_models": vision_embed_models,
                "vision_embedding_port": node.vision_embedding_port,
            }
        )

    return {
        "router_version": __version__,
        "router_hostname": hostname,
        "config": config,
        "nodes": nodes_data,
    }


@router.post("/dashboard/api/settings")
async def dashboard_settings_update(request: Request):
    """Update runtime-mutable settings (toggles only)."""
    body = await request.json()
    settings = request.app.state.settings

    from fleet_manager.common.env_writer import set_env_var, unset_env_var

    mutable_fields = {
        "auto_pull",
        "vram_fallback",
        "image_generation",
        "transcription",
        "dynamic_num_ctx",
        "num_ctx_auto_calculate",
        "fleet_intelligence",
    }
    # Node-scoped settings.  These live on NodeSettings in the *node* process,
    # so there is nothing local to mutate -- we persist them and the node picks
    # them up on its next start.  Reported back as ``restart_required`` so the
    # UI can say so instead of implying an immediate effect.
    node_bool_fields = {"telemetry"}
    node_str_fields = {"herd_nickname"}

    updated = {}
    not_persisted = []
    restart_required = []

    for field in mutable_fields:
        if field in body:
            value = bool(body[field])
            setattr(settings, field, value)
            updated[field] = value
            # Persist, or the change silently reverts on restart.  Every toggle
            # behaved that way before 0.9.2, which is merely annoying for
            # auto_pull and a broken promise for a telemetry opt-out.
            if not set_env_var(f"FLEET_{field.upper()}", value):
                not_persisted.append(field)

    for field in node_bool_fields:
        if field in body:
            value = bool(body[field])
            updated[field] = value
            restart_required.append(field)
            if not set_env_var(f"FLEET_NODE_{field.upper()}", value):
                not_persisted.append(field)

    for field in node_str_fields:
        if field in body:
            value = str(body[field]).strip()
            if field == "herd_nickname" and value:
                # Enforce here, not only in the browser: the API is reachable
                # directly, and the server's truncation is a safety net rather
                # than a UX.  Someone typing a 40-character name should learn
                # that now, not discover a shortened one on the leaderboard.
                from fleet_manager.node.anonymous_rollup import (
                    MAX_NICKNAME_LEN,
                    sanitize_nickname,
                )

                if len(value) > MAX_NICKNAME_LEN:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "status": "error",
                            "message": (
                                f"Herd name must be {MAX_NICKNAME_LEN} characters "
                                f"or fewer (got {len(value)})."
                            ),
                        },
                    )
                if not sanitize_nickname(value):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "status": "error",
                            "message": (
                                "Herd name may contain letters, numbers, spaces, - _ . only."
                            ),
                        },
                    )
            updated[field] = value
            restart_required.append(field)
            env_key = f"FLEET_NODE_{field.upper()}"
            # Empty means "no nickname" -- remove the key entirely so the code
            # default applies, rather than persisting an empty string that
            # reads like a deliberate blank name.
            ok = unset_env_var(env_key) if value == "" else set_env_var(env_key, value)
            if not ok:
                not_persisted.append(field)

    # Handle num_ctx_overrides separately (dict, not bool)
    if "num_ctx_overrides" in body:
        overrides = body["num_ctx_overrides"]
        if isinstance(overrides, dict):
            # Validate: all values must be positive integers
            clean = {k: int(v) for k, v in overrides.items() if int(v) > 0}
            settings.num_ctx_overrides = clean
            updated["num_ctx_overrides"] = clean

    if not updated:
        return {"status": "no_change", "message": "No mutable fields provided"}

    return {
        "status": "updated",
        "updated": updated,
        # Written to ~/.fleet-manager/env unless listed here.  Surfaced rather
        # than swallowed: a toggle that will silently revert on restart is
        # worse than one that says it could not be saved.
        "not_persisted": not_persisted,
        # Node-scoped values take effect when herd-node next starts.
        "restart_required": restart_required,
    }


@router.get("/dashboard/api/image-stats")
async def dashboard_image_stats():
    """Image generation statistics from the last 24 hours."""
    from fleet_manager.server.routes.image_compat import get_image_gen_events

    events = get_image_gen_events(hours=24)
    completed = [e for e in events if e["status"] == "completed"]
    failed = [e for e in events if e["status"] == "failed"]

    return {
        "total": len(events),
        "completed": len(completed),
        "failed": len(failed),
        "avg_generation_ms": (
            int(sum(e["generation_ms"] for e in completed) / len(completed)) if completed else 0
        ),
        "by_node": _group_by(events, "node_id"),
        "by_model": _group_by(events, "model"),
        "recent": events[-10:],
    }


def _group_by(events: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        val = e.get(key, "unknown")
        counts[val] = counts.get(val, 0) + 1
    return counts


@router.get("/dashboard/api/transcription-stats")
async def dashboard_transcription_stats():
    """Transcription statistics from the last 24 hours."""
    from fleet_manager.server.routes.transcription_compat import (
        get_transcription_events,
    )

    events = get_transcription_events(hours=24)
    completed = [e for e in events if e["status"] == "completed"]
    failed = [e for e in events if e["status"] == "failed"]

    return {
        "total": len(events),
        "completed": len(completed),
        "failed": len(failed),
        "avg_processing_ms": (
            int(sum(e["processing_ms"] for e in completed) / len(completed)) if completed else 0
        ),
        "by_node": _group_by(events, "node_id"),
        "by_model": _group_by(events, "model"),
        "recent": events[-10:],
    }


@router.post("/dashboard/api/pull")
async def dashboard_pull_model(request: Request):
    """Pull a model onto a specific node via Ollama."""
    body = await request.json()
    node_id = body["node_id"]
    model = body["model"]
    proxy = request.app.state.streaming_proxy
    success = await proxy.pull_model(node_id, model)
    return {"ok": success, "node_id": node_id, "model": model}


@router.post("/dashboard/api/delete")
async def dashboard_delete_model(request: Request):
    """Delete a model from a specific node via Ollama."""
    body = await request.json()
    node_id = body["node_id"]
    model = body["model"]
    proxy = request.app.state.streaming_proxy
    success = await proxy.delete_model(node_id, model)
    return {"ok": success, "node_id": node_id, "model": model}


@router.get("/dashboard/api/model-management")
async def dashboard_model_management(request: Request):
    """Per-node model details with disk sizes and last-used timestamps."""
    from fleet_manager.server.model_knowledge import classify_model, lookup_model

    registry = request.app.state.registry
    trace_store = getattr(request.app.state, "trace_store", None)
    proxy = request.app.state.streaming_proxy

    nodes = registry.get_all_nodes()
    online_nodes = [n for n in nodes if n.status != "offline"]

    # Get last-used data in one query
    usage_map: dict[tuple[str, str], dict] = {}
    if trace_store:
        usage_rows = await trace_store.get_last_used_by_node_model()
        for row in usage_rows:
            usage_map[(row["node_id"], row["model"])] = row

    now = time.time()
    result = []

    for node in online_nodes:
        if not node.ollama:
            continue

        # Query Ollama for full model details (including disk size)
        model_details = await proxy.query_node_models(node.node_id)
        detail_map = {m["name"]: m for m in model_details}

        # Models currently loaded in VRAM
        loaded_set = {m.name for m in node.ollama.models_loaded}

        models = []
        for model_name in node.ollama.models_available:
            detail = detail_map.get(model_name, {})
            usage = usage_map.get((node.node_id, model_name), {})

            spec = lookup_model(model_name)
            category = classify_model(model_name).value

            last_used = usage.get("last_used")
            days_unused = None
            if last_used:
                days_unused = round((now - last_used) / 86400, 1)

            models.append(
                {
                    "name": model_name,
                    "display_name": spec.display_name if spec else model_name,
                    "category": category,
                    "size_gb": detail.get("size_gb", spec.ram_gb if spec else 0),
                    "parameter_size": detail.get("parameter_size", ""),
                    "quantization": detail.get("quantization", ""),
                    "last_used": last_used,
                    "days_unused": days_unused,
                    "total_requests": usage.get("total_requests", 0),
                    "loaded_in_vram": model_name in loaded_set,
                    "unused": days_unused is None or days_unused >= 7,
                }
            )

        # Sort: loaded first, then by last-used desc, then alphabetical
        models.sort(
            key=lambda m: (
                not m["loaded_in_vram"],
                -(m["last_used"] or 0),
                m["name"],
            )
        )

        result.append(
            {
                "node_id": node.node_id,
                "models": models,
                "total_size_gb": round(sum(m["size_gb"] for m in models), 1),
                "disk_available_gb": (round(node.disk.available_gb, 1) if node.disk else 0),
                "disk_total_gb": round(node.disk.total_gb, 1) if node.disk else 0,
            }
        )

    return {"nodes": result}


@router.get("/dashboard/api/pinned-models")
async def dashboard_pinned_models_get(request: Request):
    """Return env-level + per-node pinned models.

    Shape:
        {
          "env_pins": ["gpt-oss:120b"],        # fleet-wide, read-only
          "per_node": {"Neons-Mac-Studio": [...]},
          "max_count": 3
        }
    """
    from fleet_manager.server.model_preloader import _parse_pinned_models

    settings = request.app.state.settings
    store = request.app.state.pinned_store
    return {
        "env_pins": _parse_pinned_models(getattr(settings, "pinned_models", "")),
        "per_node": store.load(),
        "max_count": getattr(settings, "model_preload_max_count", 3),
    }


@router.post("/dashboard/api/pinned-models")
async def dashboard_pinned_models_set(request: Request):
    """Toggle a per-node pin.  Body: {node_id, model, pinned: bool}.

    Pins only affect the Ollama preloader.  Vision embedding models
    (DINOv2/SigLIP/CLIP) are managed by the embedding service and can't
    be meaningfully pinned here — we reject those to keep the store
    honest.  Unpin is always allowed so stale entries can be cleaned up.
    """
    from fleet_manager.server.model_knowledge import ModelCategory, classify_model

    body = await request.json()
    node_id = body.get("node_id", "").strip()
    model = body.get("model", "").strip()
    pinned = bool(body.get("pinned", False))
    if not node_id or not model:
        return JSONResponse(
            {"ok": False, "error": "node_id and model required"},
            status_code=400,
        )
    if pinned and classify_model(model) == ModelCategory.VISION_EMBEDDING:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"{model} is a vision embedding model managed by the "
                    "embedding service — pinning only affects the Ollama "
                    "preloader and would have no effect here."
                ),
            },
            status_code=400,
        )
    store = request.app.state.pinned_store
    state = store.set_pin(node_id, model, pinned)
    return {"ok": True, "per_node": state}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Overview: the fleet's status first, then what is busy, then the nodes."""
    return _dashboard_page("Overview", "overview", _OVERVIEW_BODY)


# Merged pages keep their old URLs as a second route on the page that absorbed
# them -- the pattern /dashboard/apps set for /dashboard/tags.  Rendered, not
# redirected, so there is one pattern; the shared script scrolls to the section
# the old URL named.


@router.get("/dashboard/health", response_class=HTMLResponse)
@router.get("/dashboard/recommendations", response_class=HTMLResponse)
async def dashboard_health_page():
    """Health: findings, vitals, model recommendations, briefing history."""
    return _dashboard_page(
        "Health",
        "health",
        _HEALTH_BODY + _RECOMMENDATIONS_BODY + _BRIEFING_HISTORY_BODY,
    )


@router.get("/dashboard/models", response_class=HTMLResponse)
@router.get("/dashboard/trends", response_class=HTMLResponse)
async def dashboard_models_page():
    """Models: traffic trends and per-model performance."""
    return _dashboard_page("Models", "models", _MODELS_BODY, extra_head=_CHART_JS_HEAD)


@router.get("/dashboard/tags", response_class=HTMLResponse)
@router.get("/dashboard/apps", response_class=HTMLResponse)  # backwards compat
async def dashboard_tags_page():
    """Usage: requests by tag."""
    return _dashboard_page("Usage", "usage", _TAGS_BODY, extra_head=_CHART_JS_HEAD)


@router.get("/dashboard/settings", response_class=HTMLResponse)
async def dashboard_settings_page():
    """Settings: appearance, toggles, nodes, configuration."""
    return _dashboard_page("Settings", "settings", _SETTINGS_BODY)


@router.get("/dashboard/benchmarks", response_class=HTMLResponse)
async def dashboard_benchmarks_page():
    """Benchmarks: measured fleet throughput under load, and its history.

    Not a nav tab -- a tool run occasionally -- so it is reached from Models
    (whose numbers it measures under controlled load) and Settings.  The nav
    marks Models, the tab it belongs under."""
    return _dashboard_page("Benchmarks", "models", _BENCHMARKS_BODY, extra_head=_CHART_JS_HEAD)


@router.get("/dashboard/color-states", response_class=HTMLResponse)
async def dashboard_color_states_page():
    """Visual reference: utilization bars in all Axis B warning states.

    Developer tool — renders fake cards side-by-side showing what the
    dashboard looks like when memory.pressure is normal/warning/critical
    and when the CPU-thermal proxy fires.  Uses the live CSS + JS from
    the main dashboard so there's no drift between this reference and
    what production users see.  Linked from
    ``docs/guides/dashboard-color-reference.md`` for future designers.
    """
    return _dashboard_page("Colour reference", "", _COLOR_STATES_BODY)


# ---------------------------------------------------------------------------
# Shared layout: tokens, components, helpers
# ---------------------------------------------------------------------------
# One page shell for every tab.  See docs/plans/dashboard-calm-redesign.md:
# colour only for state, glass only on the header, light and dark from one
# token set.  Every colour on every page is a token below; the contrast of the
# text tokens (and of the translucent header at its worst case) is pinned by
# tests/test_server/test_dashboard_design.py.

# Colour tokens, one definition for both appearances.  The CSS below is
# generated from this, and tests/test_server/test_dashboard_design.py reads it
# to check contrast, so a colour cannot change without the check seeing it.
_TOKENS: dict[str, dict[str, str]] = {
    "dark": {
        "surface-0": "#0b0b10",
        "surface-1": "#15151c",
        "surface-2": "#1d1d26",
        "hairline": "#2a2a35",
        "text-1": "#ededf2",
        "text-2": "#a0a0b2",
        "text-3": "#83839a",
        "accent": "#8a83ff",
        "accent-fill": "#5b52f0",
        "accent-ink": "#ffffff",
        "ok": "#34d158",
        "warn": "#ffb340",
        "fail": "#ff6961",
        "info": "#6aaeff",
        "series-1": "#8a83ff",
        "series-2": "#6aaeff",
        "series-3": "#3fcf9a",
        "series-4": "#f2b84b",
        "series-5": "#f472b6",
        "series-6": "#a1a1b5",
    },
    "light": {
        "surface-0": "#f5f5f7",
        "surface-1": "#ffffff",
        "surface-2": "#f0f0f3",
        "hairline": "#e2e2e8",
        "text-1": "#1d1d1f",
        "text-2": "#5c5c66",
        "text-3": "#6b6b75",
        "accent": "#4f46e5",
        "accent-fill": "#4f46e5",
        "accent-ink": "#ffffff",
        "ok": "#1a7f37",
        "warn": "#8a5c00",
        "fail": "#cf222e",
        "info": "#0969da",
        "series-1": "#4f46e5",
        "series-2": "#0969da",
        "series-3": "#0f9f6e",
        "series-4": "#c27c0e",
        "series-5": "#db2777",
        "series-6": "#6e6e7a",
    },
}
# The header's tint over whatever scrolls beneath it; the contrast test blends
# at this opacity, so keep the two in step.
_HEADER_TINT_PCT = 88


def _token_block(theme: str) -> str:
    scheme = f"  color-scheme: {theme};\n"
    return scheme + "".join(f"  --{k}: {v};\n" for k, v in _TOKENS[theme].items())


_TOKEN_CSS = (
    # .theme-dark / .theme-light scope the tokens to one element, so the
    # colour reference can show both appearances side by side.
    ":root, .theme-dark {\n"
    + _token_block("dark")
    + """  --radius-1: 6px;
  --radius-2: 10px;
  --radius-3: 14px;
  --fs-display: 34px;
  --fs-title: 20px;
  --fs-body: 14px;
  --fs-small: 13px;
  --fs-label: 12px;
}
"""
    # System light, unless the viewer chose dark; then an explicit light choice.
    + '@media (prefers-color-scheme: light) {\n  :root:not([data-theme="dark"]) {\n'
    + _token_block("light").replace("\n  ", "\n    ").replace("  color", "    color", 1)
    + "  }\n}\n"
    + ':root[data-theme="light"], .theme-light {\n'
    + _token_block("light")
    + "}\n"
)

_SHARED_CSS = (
    _TOKEN_CSS
    + """
/* Concentric corners: a nested element's radius is its container's radius
   minus the padding between them (never below --radius-1), so nested shapes
   run parallel.  Cards use --radius-3, rows inside them --radius-2, chips and
   inputs --radius-1. */
* { margin: 0; padding: 0; box-sizing: border-box; }
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
  font-size: var(--fs-body);
  line-height: 1.45;
  font-variant-numeric: tabular-nums;
  background: var(--surface-0);
  color: var(--text-1);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, .mono { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: 0.92em; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-1); }

/* --- Header: the only glass in the product.  A strong tint, not just a blur,
   because page content scrolls under it; solid wherever the viewer asks for
   less transparency or more contrast, or the browser cannot blur. --- */
.header {
  position: sticky; top: 0; z-index: 50;
  display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
  padding: 12px 32px;
  background: color-mix(in srgb, var(--surface-0) """
    + str(_HEADER_TINT_PCT)
    + """%, transparent);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 1px solid var(--hairline);
}
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  .header { background: var(--surface-0); }
}
@media (prefers-reduced-transparency: reduce) { .header { background: var(--surface-0); backdrop-filter: none; -webkit-backdrop-filter: none; } }
@media (prefers-contrast: more) { .header { background: var(--surface-0); backdrop-filter: none; -webkit-backdrop-filter: none; } }
:root[data-transparency="off"] .header { background: var(--surface-0); backdrop-filter: none; -webkit-backdrop-filter: none; }
.brand { font-size: 17px; font-weight: 600; letter-spacing: -0.2px; color: var(--text-1); }
.brand:hover { text-decoration: none; }
.nav-tabs { display: flex; gap: 2px; flex-wrap: wrap; }
.nav-tab {
  padding: 6px 14px; border-radius: var(--radius-1);
  font-size: var(--fs-small); font-weight: 500;
  color: var(--text-2); transition: color 0.15s, background 0.15s;
}
.nav-tab:hover { color: var(--text-1); background: var(--surface-2); text-decoration: none; }
.nav-tab.active { color: var(--text-1); background: var(--surface-2); }

/* --- Page --- */
.main {
  width: 100%; max-width: 1240px; margin: 0 auto;
  padding: 28px 32px 40px;
  display: flex; flex-direction: column; gap: 28px; flex: 1;
}
.section { display: flex; flex-direction: column; gap: 12px; scroll-margin-top: 72px; }
.page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.section-title { font-size: var(--fs-small); font-weight: 600; color: var(--text-2); }
.page-title { font-size: var(--fs-title); font-weight: 600; letter-spacing: -0.2px; }
.page-sub { font-size: var(--fs-small); color: var(--text-2); margin-top: 2px; }
.muted { color: var(--text-2); }
.faint { color: var(--text-3); }
.text-fail { color: var(--fail); }
.small { font-size: var(--fs-label); }
.note { font-size: var(--fs-label); color: var(--text-2); line-height: 1.5; }
.subhead { margin-top: 14px; }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.spacer { flex: 1; }
.card {
  background: var(--surface-1);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-3);
  padding: 18px 20px;
}
.card.flush { padding: 0; overflow: hidden; }
.inset {
  background: var(--surface-2); border-radius: var(--radius-2); padding: 10px 12px;
}
.empty-state { color: var(--text-2); font-size: var(--fs-small); text-align: center; padding: 32px 16px; }
.empty-state h3 { color: var(--text-1); font-size: 16px; font-weight: 600; margin-bottom: 6px; }

/* --- Figures: big quiet numbers, small sentence-case labels --- */
.figures { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
.figure {
  background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--radius-3);
  padding: 14px 18px; display: flex; flex-direction: column; gap: 2px; color: var(--text-1);
}
a.figure:hover { text-decoration: none; border-color: color-mix(in srgb, var(--accent) 45%, var(--hairline)); }
.figure-value { font-size: 26px; font-weight: 600; letter-spacing: -0.4px; line-height: 1.15; }
.figure-unit { font-size: 15px; font-weight: 500; color: var(--text-2); margin-left: 2px; }
.figure-label { font-size: var(--fs-label); color: var(--text-2); }
.figure-value.text { font-size: 18px; letter-spacing: -0.1px; line-height: 1.5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.figure.zero .figure-value { color: var(--text-3); }
.figure.warn .figure-value { color: var(--warn); }
.figure.fail .figure-value { color: var(--fail); }

/* --- Status: a dot plus a word, never colour alone, never a filled pill --- */
.status { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-small); color: var(--text-2); white-space: nowrap; }
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-3); }
.status-dot.online, .status-dot.ok { background: var(--ok); }
.status-dot.degraded, .status-dot.warn { background: var(--warn); }
.status-dot.offline, .status-dot.fail { background: var(--fail); }
.status-dot.info { background: var(--info); }
.status-dot.idle { background: var(--text-3); }
.status-dot.live { background: var(--ok); animation: breathe 2.4s ease-in-out infinite; }
@keyframes breathe { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

/* Neutral label: grey text in a hairline outline.  Never coloured. */
.tag {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; font-weight: 500; letter-spacing: 0.2px;
  color: var(--text-2); border: 1px solid var(--hairline); border-radius: var(--radius-1);
  padding: 1px 6px; white-space: nowrap;
}

/* --- Buttons --- */
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  font: inherit; font-size: var(--fs-small); font-weight: 500;
  padding: 6px 12px; border-radius: var(--radius-1); cursor: pointer;
  background: var(--surface-2); color: var(--text-1); border: 1px solid var(--hairline);
  transition: background 0.15s, border-color 0.15s, opacity 0.15s;
}
.btn:hover { border-color: color-mix(in srgb, var(--accent) 50%, var(--hairline)); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.primary { background: var(--accent-fill); color: var(--accent-ink); border-color: transparent; }
.btn.primary:hover { opacity: 0.9; }
/* Destructive actions read as red words on a neutral button, the macOS
   convention: white on --fail is 2.8:1 in dark, and a filled red block shouts
   on a page where red means "failing now". */
.btn.danger { color: var(--fail); border-color: color-mix(in srgb, var(--fail) 45%, var(--hairline)); }
.btn.danger:hover { background: color-mix(in srgb, var(--fail) 10%, var(--surface-2)); border-color: var(--fail); }
.btn.quiet { background: transparent; color: var(--text-2); }
.btn.small { padding: 3px 9px; font-size: var(--fs-label); }
.btn svg { width: 14px; height: 14px; }
.input {
  font: inherit; font-size: var(--fs-small); color: var(--text-1);
  background: var(--surface-0); border: 1px solid var(--hairline); border-radius: var(--radius-1);
  padding: 5px 9px;
}
.segmented { display: inline-flex; background: var(--surface-2); border-radius: var(--radius-2); padding: 3px; gap: 2px; }
.segmented button {
  font: inherit; font-size: var(--fs-label); font-weight: 500; color: var(--text-2);
  background: transparent; border: none; border-radius: calc(var(--radius-2) - 3px); padding: 5px 12px; cursor: pointer;
}
.segmented button.active { background: var(--surface-1); color: var(--text-1); box-shadow: 0 0 0 1px var(--hairline); }

/* --- Disclosure: native <details>, the one collapse pattern --- */
details.disclosure > summary {
  list-style: none; cursor: pointer; display: flex; align-items: center; gap: 8px;
  font-size: var(--fs-small); color: var(--text-2); padding: 10px 0; user-select: none;
}
details.disclosure > summary::-webkit-details-marker { display: none; }
details.disclosure > summary::before {
  content: ''; width: 6px; height: 6px; flex-shrink: 0;
  border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg); transition: transform 0.15s;
}
details.disclosure[open] > summary::before { transform: rotate(45deg); }
details.disclosure > summary:hover { color: var(--text-1); }

/* --- Grouped list: inset rows instead of boxes --- */
.list { background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--radius-3); overflow: hidden; }
.list-row { display: flex; align-items: center; gap: 14px; padding: 12px 18px; border-top: 1px solid var(--hairline); flex-wrap: wrap; }
.list-row:first-child { border-top: none; }
.list-row .name { font-weight: 500; }
a.list-row { color: inherit; flex-wrap: nowrap; }
a.list-row:hover { background: var(--surface-2); text-decoration: none; }
a.list-row .chev { margin-left: auto; color: var(--text-3); font-size: 1.2em; line-height: 1; }
.list-row .stats { display: flex; gap: 22px; margin-left: auto; flex-wrap: wrap; }
.stat { display: flex; flex-direction: column; align-items: flex-end; min-width: 52px; }
.stat .v { font-size: 15px; font-weight: 600; }
.stat .l { font-size: 11px; color: var(--text-2); }
.stat.zero .v { color: var(--text-3); font-weight: 500; }
.stat.fail .v { color: var(--fail); }
.stat.warn .v { color: var(--warn); }

/* --- Tables --- */
.data-table { width: 100%; border-collapse: collapse; font-size: var(--fs-small); }
.data-table th {
  text-align: left; font-size: var(--fs-label); font-weight: 500; color: var(--text-2);
  padding: 9px 14px; border-bottom: 1px solid var(--hairline); white-space: nowrap;
  position: sticky; top: 0; background: var(--surface-1);
}
.data-table td { padding: 10px 14px; border-bottom: 1px solid var(--hairline); vertical-align: middle; }
.data-table tr:last-child td { border-bottom: none; }
.data-table td.num, .data-table th.num { text-align: right; }
.data-table tr.clickable { cursor: pointer; }
.data-table tr.clickable:hover td { background: var(--surface-2); }
.data-table tr.selected td { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.data-table .name { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: var(--fs-label); font-weight: 500; }

/* --- Utilization bars (Axis A: utilization colour; Axis B: warning outline,
   independent of the fill).  See docs/plans/dashboard-color-semantics.md. --- */
.bar-container { height: 6px; background: var(--surface-2); border-radius: 3px; margin-top: 6px; overflow: hidden; position: relative; }
.bar-fill { height: 100%; border-radius: 3px; transition: width 0.8s ease, background 0.5s ease; }
.bar-container.bar-warning  { box-shadow: 0 0 0 1px var(--warn); }
.bar-container.bar-critical { box-shadow: 0 0 0 1px var(--fail); }
.bar-container.bar-thermal  { box-shadow: 0 0 0 1px var(--warn); }

/* --- Charts --- */
.charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.charts-row[hidden] { display: none; }
.chart-card { background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--radius-3); padding: 18px 20px; min-width: 0; }
.chart-card.full-width { grid-column: 1 / -1; }
.chart-card.fixed { height: 300px; display: flex; flex-direction: column; }
.chart-card.fixed .chart-box { position: relative; flex: 1; min-height: 0; }
.chart-card h4 { font-size: var(--fs-small); font-weight: 600; color: var(--text-2); margin-bottom: 14px; }
.chart-card h4 .faint { font-weight: 400; }

/* --- Time range --- */
.time-range { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.tr-btn {
  font: inherit; font-size: var(--fs-label); font-weight: 500; padding: 5px 12px; border-radius: var(--radius-1);
  background: transparent; border: 1px solid var(--hairline); color: var(--text-2); cursor: pointer;
}
.tr-btn:hover { color: var(--text-1); }
.tr-btn.active { color: var(--text-1); background: var(--surface-2); }
.tr-custom { display: flex; align-items: center; gap: 8px; margin-left: 8px; }
.tr-custom span { color: var(--text-2); font-size: var(--fs-label); }

/* --- Dialogs and toast --- */
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 16px; }
.dialog { background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--radius-3); padding: 24px; max-width: 500px; width: 100%; }
.dialog h3 { font-size: 16px; font-weight: 600; margin-bottom: 12px; }
.dialog p { font-size: var(--fs-small); color: var(--text-2); line-height: 1.5; margin-bottom: 8px; }
.dialog-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
.toast {
  position: fixed; bottom: 24px; right: 24px; z-index: 1000;
  background: var(--surface-1); border: 1px solid var(--hairline); color: var(--text-1);
  padding: 11px 18px; border-radius: var(--radius-2); font-size: var(--fs-small); font-weight: 500;
  opacity: 0; transform: translateY(8px); transition: opacity 0.2s, transform 0.2s; pointer-events: none;
}
.toast.show { opacity: 1; transform: translateY(0); }

/* --- Footer: one quiet line --- */
.footer {
  display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 14px 32px; border-top: 1px solid var(--hairline);
  color: var(--text-2); font-size: var(--fs-label);
}
.footer .row { gap: 12px; }

/* --- Motion explains change; nothing moves for viewers who ask it not to --- */
.flash { animation: flash 1.2s ease-out; }
@keyframes flash { from { background: color-mix(in srgb, var(--accent) 18%, transparent); } to { background: transparent; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
}

@media (max-width: 768px) {
  .header { padding: 10px 16px; gap: 10px; }
  .nav-tab { padding: 5px 10px; }
  .main { padding: 20px 16px 32px; gap: 22px; }
  .footer { padding: 12px 16px; }
  .charts-row { grid-template-columns: 1fr; }
  .figures { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .list-row .stats { margin-left: 0; width: 100%; justify-content: space-between; gap: 12px; }
  .data-table th, .data-table td { padding: 7px 9px; }
  .table-scroll { overflow-x: auto; }
}
"""
)

# Shared helpers, loaded on every page before the page's own script.  Each
# one replaces copies that used to live in individual pages (three fmtNum,
# two fmtMs, four chart colour objects); a page must not redefine them.
_SHARED_JS = """
// ---- Appearance: system by default; a per-viewer override lives in the
// browser, never in ~/.fleet-manager/env (that would switch every viewer). ----
var HerdPrefs = {
  get: function(k, d) { try { var v = localStorage.getItem('herd.' + k); return v === null ? d : v; } catch (e) { return d; } },
  set: function(k, v) { try { localStorage.setItem('herd.' + k, v); } catch (e) {} },
};
function applyAppearance() {
  var root = document.documentElement;
  var theme = HerdPrefs.get('theme', 'system');
  if (theme === 'light' || theme === 'dark') root.dataset.theme = theme; else delete root.dataset.theme;
  if (HerdPrefs.get('transparency', 'on') === 'off') root.dataset.transparency = 'off'; else delete root.dataset.transparency;
}
applyAppearance();
var _themeListeners = [];
function onThemeChange(cb) { _themeListeners.push(cb); }
function _fireThemeChange() { _themeListeners.forEach(function(cb) { try { cb(); } catch (e) { console.error(e); } }); }
try { window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', _fireThemeChange); } catch (e) {}
function setAppearance(key, value) { HerdPrefs.set(key, value); applyAppearance(); _fireThemeChange(); }
// Live, not read once: a viewer who turns on Reduce Motion mid-session gets it
// without a reload.  Charts take it at creation, so they are rebuilt.
var REDUCED_MOTION = false;
try {
  var _motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  REDUCED_MOTION = _motionQuery.matches;
  _motionQuery.addEventListener('change', function(e) { REDUCED_MOTION = e.matches; _fireThemeChange(); });
} catch (e) {}

// ---- Formatting: every rendered number goes through these ----
var _nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
function fmtNum(n) { return n == null || isNaN(n) ? '\\u2014' : _nf.format(n); }
function fmtCompact(n) {
  if (n == null || isNaN(n)) return '\\u2014';
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return _nf.format(n);
}
function fmtMs(ms) {
  if (ms == null || isNaN(ms) || ms <= 0) return '\\u2014';
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms';
}
function fmtPct(p, digits) { return p == null || isNaN(p) ? '\\u2014' : p.toFixed(digits == null ? 1 : digits) + '%'; }
function formatGB(gb) {
  if (gb == null || isNaN(gb)) return '\\u2014';
  if (gb >= 100) return Math.round(gb) + ' GB';
  if (gb >= 10) return gb.toFixed(1) + ' GB';
  return gb.toFixed(2) + ' GB';
}
function timeAgo(ts) {
  if (!ts) return '\\u2014';
  var s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function fmtDateTime(ts) {
  var d = new Date(ts * 1000);
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
function fmtDay(ts) { return new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' }); }
function fmtDuration(s) {
  if (s == null) return '\\u2014';
  if (s < 60) return Math.round(s) + 's';
  var m = Math.floor(s / 60), sec = Math.round(s % 60);
  return m + 'm' + (sec ? ' ' + (sec < 10 ? '0' : '') + sec + 's' : '');
}
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

// Ease a number to its new value.  Explains a change instead of jumping;
// skipped under reduced motion and on first render.
function setNum(el, value, fmt) {
  if (!el) return;
  fmt = fmt || fmtNum;
  var prev = parseFloat(el.dataset.value);
  el.dataset.value = value;
  if (REDUCED_MOTION || isNaN(prev) || prev === value || value == null || isNaN(value)) { el.textContent = fmt(value); return; }
  var start = performance.now(), dur = 220;
  (function step(now) {
    var t = Math.min(1, (now - start) / dur), e = 1 - Math.pow(1 - t, 3);
    el.textContent = fmt(t < 1 ? prev + (value - prev) * e : value);
    if (t < 1) requestAnimationFrame(step);
  })(start);
}

// ---- Status: a dot plus a word ----
function statusClass(status) {
  return status === 'online' || status === 'ok' || status === 'healthy' ? 'ok'
       : status === 'degraded' || status === 'warn' || status === 'warning' ? 'warn'
       : status === 'offline' || status === 'fail' || status === 'critical' ? 'fail'
       : status === 'info' ? 'info' : 'idle';
}
function dot(status) { return '<span class="status-dot ' + statusClass(status) + '"></span>'; }
function statusDot(status, label) {
  return '<span class="status">' + dot(status) + escapeHtml(label == null ? status : label) + '</span>';
}
// One threshold set for "how good is this score", shared by every page.
function scoreClass(score) { return score >= 80 ? 'ok' : score >= 50 ? 'warn' : 'fail'; }
var SEVERITY_RANK = { critical: 0, warning: 1, info: 2 };

// ---- Colour scales ----
// barColor: green-to-red.  USE ONLY where high IS a warning (disk usage).
function barColor(pct) {
  var h = 142 - (pct / 100) * 142, s = 71 + (pct / 100) * 13, l = 45 + (pct / 100) * 15;
  return 'hsl(' + h + ',' + s + '%,' + l + '%)';
}
// utilizationColor: blue-to-purple for product-VALUE metrics (CPU, memory).
// Busy is the product working, so it is never red; warning state is carried
// separately by .bar-warning / .bar-critical / .bar-thermal on the container.
function utilizationColor(pct, metric) {
  var hueStart = metric === 'cpu' ? 195 : 225, hueEnd = metric === 'cpu' ? 255 : 270;
  var hue = hueStart + (pct / 100) * (hueEnd - hueStart);
  var saturation = 78 + (pct / 100) * 14, lightness = 62 - (pct / 100) * 10, alpha = 0.72 + (pct / 100) * 0.28;
  return 'hsla(' + hue + ', ' + saturation + '%, ' + lightness + '%, ' + alpha + ')';
}

// ---- Charts: one theme, read from the tokens.  Re-read on every render so
// a light/dark switch reaches charts too (getComputedStyle is a snapshot). ----
function token(name) { return getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim(); }
function withAlpha(hex, a) {
  var h = hex.replace('#', '');
  if (h.length === 3) h = h.split('').map(function(c) { return c + c; }).join('');
  var r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
}
function chartTheme(opts) {
  opts = opts || {};
  var C = {
    text: token('text-1'), dim: token('text-2'), grid: token('hairline'), surface: token('surface-1'),
    accent: token('series-1'), series: [1, 2, 3, 4, 5, 6].map(function(i) { return token('series-' + i); }),
  };
  C.color = function(i) { return C.series[i % C.series.length]; };
  // Category axes keep their labels; only numeric axes get compact numbers.
  function tick(v) { return this.type === 'category' ? this.getLabelForValue(v) : fmtCompact(v); }
  C.defaults = {
    responsive: true,
    maintainAspectRatio: opts.fixed ? false : true,
    animation: REDUCED_MOTION ? false : undefined,
    plugins: {
      legend: { labels: { color: C.dim, font: { size: 11 }, boxWidth: 10, boxHeight: 10 } },
      tooltip: { callbacks: { label: function(ctx) {
        var v = ctx.parsed && typeof ctx.parsed === 'object' ? (ctx.parsed.x != null && ctx.chart.options.indexAxis === 'y' ? ctx.parsed.x : ctx.parsed.y) : ctx.parsed;
        return (ctx.dataset.label ? ctx.dataset.label + ': ' : '') + fmtNum(v);
      } } },
    },
    scales: {
      x: { ticks: { color: C.dim, font: { size: 10 }, maxRotation: 45, callback: tick }, grid: { color: C.grid }, border: { color: C.grid } },
      y: { ticks: { color: C.dim, font: { size: 10 }, callback: tick }, grid: { color: C.grid }, border: { color: C.grid }, beginAtZero: true },
    },
  };
  return C;
}

// ---- Fleet Intelligence briefing text: tiny markdown subset, escaped first ----
function renderBriefing(text) {
  return String(text || '').split(String.fromCharCode(10)).map(function(line) {
    line = escapeHtml(line);
    while (line.indexOf('**') !== -1) {
      var i = line.indexOf('**'), j = line.indexOf('**', i + 2);
      if (j === -1) break;
      line = line.substring(0, i) + '<strong>' + line.substring(i + 2, j) + '</strong>' + line.substring(j + 2);
    }
    var t = line.trimStart();
    if (t.charAt(0) === '-' || t.charAt(0) === '*') line = '&bull; ' + t.substring(1).trimStart();
    return line;
  }).join('<br>');
}

// ---- Tab status: favicon and title carry the worst state ----
var _baseTitle = document.title, _tabState = 'ok';
function setTabAlert(state) {
  if (state === true) state = 'offline';
  if (!state) state = 'ok';
  if (state === _tabState) return;
  _tabState = state;
  var link = document.querySelector('link[rel="icon"]');
  if (link) link.href = state === 'ok' ? '/favicon.svg' : '/favicon-alert.svg';
  var prefix = { offline: '\\u26a0 OFFLINE \\u2014 ', critical: '\\u26a0 CRITICAL \\u2014 ', degraded: '\\u25cf ' }[state] || '';
  document.title = prefix + _baseTitle;
}

// ---- Footer connection state ----
function setLive(mode) {
  var d = document.getElementById('sse-dot'), s = document.getElementById('sse-status');
  if (d) d.className = 'status-dot ' + (mode === 'live' ? 'live' : mode === 'reconnecting' ? 'fail' : 'ok');
  if (s) s.textContent = mode === 'live' ? 'Live' : mode === 'reconnecting' ? 'Reconnecting\\u2026' : 'Up to date';
}

function showToast(msg) {
  var t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(function() { t.classList.remove('show'); }, 2500);
}

// Merged pages keep their old URLs (/dashboard/trends, /recommendations):
// land on the section that URL used to be.
// Sections above load asynchronously and push the target down, so re-align
// while the page settles -- and stop the moment the viewer scrolls.
window.addEventListener('load', function() {
  var target = { '/dashboard/trends': 'trends', '/dashboard/recommendations': 'recommendations' }[location.pathname];
  var el = target && document.getElementById(target);
  if (!el) return;
  var userMoved = false;
  ['wheel', 'touchstart', 'keydown', 'mousedown'].forEach(function(ev) { window.addEventListener(ev, function() { userMoved = true; }, { once: true, passive: true }); });
  [0, 300, 800, 1600, 3000].forEach(function(ms) { setTimeout(function() { if (!userMoved) el.scrollIntoView({ block: 'start' }); }, ms); });
});

function initTimeRange(containerId, callback, defaultRange) {
  var c = document.getElementById(containerId);
  if (!c) return;
  var presets = { '24h': 24*3600, '48h': 48*3600, '72h': 72*3600, '7d': 7*86400, '30d': 30*86400 };
  var def = defaultRange || '7d';
  var html = '<div class="time-range">';
  ['24h','48h','72h','7d','30d'].forEach(function(r) {
    html += '<button class="tr-btn' + (r === def ? ' active' : '') + '" data-range="' + r + '">' + r + '</button>';
  });
  html += '<button class="tr-btn" data-range="custom">Custom</button>';
  html += '<div class="tr-custom" id="' + containerId + '-custom" hidden>';
  html += '<input class="input" type="datetime-local" id="' + containerId + '-start">';
  html += '<span>to</span>';
  html += '<input class="input" type="datetime-local" id="' + containerId + '-end">';
  html += '<button class="btn small primary" id="' + containerId + '-apply">Apply</button>';
  html += '</div></div>';
  c.innerHTML = html;
  function firePreset(range) { var now = Date.now() / 1000; callback(now - presets[range], now); }
  c.querySelectorAll('.tr-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      c.querySelectorAll('.tr-btn').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      var range = btn.dataset.range, customEl = document.getElementById(containerId + '-custom');
      if (range === 'custom') {
        customEl.hidden = false;
        var now = new Date(), start = new Date(now.getTime() - 7*86400000);
        document.getElementById(containerId + '-end').value = now.toISOString().slice(0, 16);
        document.getElementById(containerId + '-start').value = start.toISOString().slice(0, 16);
      } else {
        customEl.hidden = true;
        firePreset(range);
      }
    });
  });
  document.getElementById(containerId + '-apply').addEventListener('click', function() {
    var s = new Date(document.getElementById(containerId + '-start').value).getTime() / 1000;
    var e = new Date(document.getElementById(containerId + '-end').value).getTime() / 1000;
    if (s && e && e > s) callback(s, e);
  });
  firePreset(def);
}
"""

# Five tabs.  Pages that were merged keep their URLs as a second route on the
# page that absorbed them (the pattern /dashboard/apps set for /dashboard/tags).
_NAV_ITEMS = (
    ("overview", "Overview", "/dashboard"),
    ("models", "Models", "/dashboard/models"),
    ("usage", "Usage", "/dashboard/tags"),
    ("health", "Health", "/dashboard/health"),
    ("settings", "Settings", "/dashboard/settings"),
)


def _dashboard_page(title: str, active_tab: str, body_html: str, extra_head: str = "") -> str:
    """Generate a full dashboard HTML page with shared nav, styles, and footer."""
    from fleet_manager import __version__

    nav_html = "".join(
        f'<a href="{href}" class="nav-tab{" active" if key == active_tab else ""}"'
        f"{' aria-current=' + chr(34) + 'page' + chr(34) if key == active_tab else ''}>{label}</a>"
        for key, label, href in _NAV_ITEMS
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark light">
<title>{title} - Ollama Herd</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>{_SHARED_CSS}</style>
<script>{_SHARED_JS}</script>
{extra_head}
</head>
<body>
<header class="header">
  <a class="brand" href="/dashboard">Ollama Herd</a>
  <nav class="nav-tabs" aria-label="Dashboard">{nav_html}</nav>
</header>
<main class="main">
{body_html}
</main>
<footer class="footer">
  <div>Ollama Herd v{__version__} · <a href="https://geeksinthewoods.com/" target="_blank" rel="noopener">Geeks in the Woods</a></div>
  <div class="row">
    <button id="notify-toggle" class="btn quiet small" onclick="requestNotifyPermission()" hidden>Enable alerts</button>
    <span class="status"><span class="status-dot ok" id="sse-dot"></span><span id="sse-status">Up to date</span></span>
  </div>
</footer>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Fleet Overview page body
# ---------------------------------------------------------------------------

_OVERVIEW_BODY = """
<style>
.hero { display: flex; align-items: center; gap: 20px; padding: 22px 24px; }
.hero-text { flex: 1; min-width: 0; }
.hero-title { font-size: 26px; font-weight: 600; letter-spacing: -0.4px; line-height: 1.2; display: flex; align-items: center; column-gap: 12px; flex-wrap: wrap; }
.hero-title .status-dot { width: 12px; height: 12px; }
.hero-facts { flex-basis: 100%; font-size: 15px; font-weight: 400; color: var(--text-2); letter-spacing: 0; margin-top: 2px; }
.hero-sub { font-size: var(--fs-small); color: var(--text-2); margin-top: 6px; }
.score-ring {
  --pct: 0%; --ring: var(--ok);
  width: 64px; height: 64px; border-radius: 50%; flex-shrink: 0; position: relative;
  display: flex; align-items: center; justify-content: center; color: var(--text-1);
  background: conic-gradient(var(--ring) var(--pct), var(--surface-2) var(--pct));
}
.score-ring::before { content: ''; position: absolute; inset: 4px; border-radius: 50%; background: var(--surface-1); }
.score-ring span { position: relative; font-size: 19px; font-weight: 600; }
.score-ring.warn { --ring: var(--warn); }
.score-ring.fail { --ring: var(--fail); }
a.score-ring:hover { text-decoration: none; }
.node-row { display: grid; grid-template-columns: minmax(150px, 1.1fr) 1fr 1fr auto; gap: 18px; align-items: center; padding: 14px 18px; }
.node-row + .node-detail-wrap { padding: 0 18px 4px; }
.node-name { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.node-chip { font-size: var(--fs-label); color: var(--text-2); margin-top: 2px; }
.meter-label { display: flex; justify-content: space-between; font-size: var(--fs-label); color: var(--text-2); }
.meter-label b { color: var(--text-1); font-weight: 600; }
.meter-extra { font-size: 11px; color: var(--text-2); margin-top: 3px; }
.meter-extra:empty { display: none; }
.node-models-count { font-size: var(--fs-label); color: var(--text-2); text-align: right; white-space: nowrap; }
.node-models-count b { color: var(--text-1); font-size: 15px; font-weight: 600; display: block; }
.node-group { border-top: 1px solid var(--hairline); }
.node-group:first-child { border-top: none; }
.detail-grid { display: grid; gap: 14px; padding-bottom: 14px; }
.detail-block h5 { font-size: var(--fs-label); font-weight: 600; color: var(--text-2); margin-bottom: 6px; }
.model-lines { display: flex; flex-direction: column; gap: 4px; }
.model-line { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; font-size: var(--fs-small); }
.model-line .mono { font-weight: 500; }
.kv { display: flex; gap: 22px; flex-wrap: wrap; font-size: var(--fs-small); }
.kv div { display: flex; flex-direction: column; }
.kv .k { font-size: 11px; color: var(--text-2); }
.model-row .name { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: var(--fs-small); }
.model-row .where { font-size: var(--fs-label); color: var(--text-2); }
.load { font-size: var(--fs-label); color: var(--text-2); }
.idle-table td { color: var(--text-2); }
.briefing-title { font-size: var(--fs-small); color: var(--text-1); white-space: nowrap; }
#briefing > summary { flex-wrap: wrap; }
.svc-kind { min-width: 84px; }
.briefing-body { font-size: var(--fs-small); line-height: 1.7; padding: 4px 0 6px; max-width: 72ch; }
.briefing-meta { font-size: var(--fs-label); color: var(--text-2); display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
@media (max-width: 900px) {
  .node-row { grid-template-columns: 1fr 1fr; }
  .node-row > :first-child { grid-column: 1 / -1; }
  .node-models-count { text-align: left; }
}
@media (max-width: 560px) {
  .hero { flex-direction: column-reverse; align-items: flex-start; }
  .hero-title { font-size: 22px; }
  .node-row { grid-template-columns: 1fr; }
}
</style>

<section class="card hero" id="status" aria-live="polite">
  <div class="hero-text">
    <div class="hero-title"><span class="status-dot idle" id="hero-dot"></span><span id="hero-word">Checking the fleet…</span><span class="hero-facts" id="hero-facts"></span></div>
    <div class="hero-sub" id="hero-sub">&nbsp;</div>
  </div>
  <a class="score-ring" id="hero-score" href="/dashboard/health" title="Health score — open Health"><span>—</span></a>
</section>

<section class="figures" id="figures" aria-label="Key figures">
  <a class="figure" href="/dashboard/models"><span class="figure-value" id="fig-requests">—</span><span class="figure-label">Requests, last 24 h</span></a>
  <a class="figure" href="/dashboard/models"><span class="figure-value" id="fig-ttft">—</span><span class="figure-label">Time to first token</span></a>
  <a class="figure" id="fig-errors-card" href="/dashboard/health"><span class="figure-value" id="fig-errors">—</span><span class="figure-label">Error rate, 24 h</span></a>
  <div class="figure" id="fig-queued-card"><span class="figure-value" id="fig-queued">—</span><span class="figure-label">Waiting in queue</span></div>
  <div class="figure" id="fig-inflight-card"><span class="figure-value" id="fig-inflight">—</span><span class="figure-label">Running now</span></div>
</section>

<section class="section" id="nodes">
  <div class="section-head"><h2 class="section-title">Nodes</h2></div>
  <div class="list" id="nodes-container"><div class="empty-state">Waiting for nodes…</div></div>
</section>

<section class="section" id="models">
  <div class="section-head"><h2 class="section-title">Models at work</h2><span class="small muted" id="models-summary"></span></div>
  <div id="queues-container"><div class="empty-state">No models loaded</div></div>
</section>

<section class="section" id="briefing-section" hidden>
  <details class="disclosure card" id="briefing">
    <summary><b class="briefing-title">Fleet Intelligence</b><span class="small" id="briefing-lede">Generating a briefing…</span></summary>
    <div class="briefing-body" id="briefing-content"></div>
    <div class="briefing-meta" id="briefing-meta"></div>
  </details>
</section>

<script>
// ---- Status line: the answer first ----
var _health = null, _sse = null;
function renderStatus() {
  var dot = document.getElementById('hero-dot');
  var word = document.getElementById('hero-word');
  var facts = document.getElementById('hero-facts');
  var sub = document.getElementById('hero-sub');
  var nodes = _sse ? _sse.nodes : [];
  var online = nodes.filter(function(n) { return n.status === 'online'; }).length;
  var offline = nodes.filter(function(n) { return n.status === 'offline'; }).length;
  var loaded = 0, queued = 0, running = 0;
  nodes.forEach(function(n) {
    if (n.status !== 'online') return;
    loaded += (n.ollama ? n.ollama.models_loaded.length + (n.ollama.mlx_models || []).length : 0);
  });
  if (_sse) Object.values(_sse.queues).forEach(function(q) { queued += q.pending || 0; running += q.in_flight || 0; });

  // Worst of the health engine's verdict and what the live stream shows: a node
  // dropping is visible here within 2 s, before the 30 s health cache turns over.
  var state = _health ? _health.status : 'unknown';
  if (_sse && nodes.length && online === 0) state = 'critical';
  else if (offline > 0 && state === 'healthy') state = 'degraded';
  var label = { healthy: 'Healthy', degraded: 'Needs attention', critical: 'Critical', unknown: 'Checking\\u2026' }[state];
  dot.className = 'status-dot ' + ({ healthy: 'ok', degraded: 'warn', critical: 'fail' }[state] || 'idle');
  word.textContent = label;
  if (_sse) {
    var parts = [];
    parts.push(online + (online === 1 ? ' node' : ' nodes') + ' online' + (offline ? ', ' + offline + ' offline' : ''));
    parts.push(loaded + (loaded === 1 ? ' model' : ' models') + ' loaded');
    parts.push(queued ? queued + ' queued' : 'nothing queued');
    facts.textContent = parts.join(' \\u00b7 ');
  }
  if (_health) {
    var recs = (_health.recommendations || []).filter(function(r) { return !(r.data && r.data.resolved); });
    var crit = recs.filter(function(r) { return r.severity === 'critical'; }).length;
    var warn = recs.filter(function(r) { return r.severity === 'warning'; }).length;
    var info = recs.length - crit - warn;
    var msg;
    if (crit || warn) {
      var top = recs.slice().sort(function(a, b) { return SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]; })[0];
      msg = escapeHtml(top.title) + (crit + warn > 1 ? ' and ' + (crit + warn - 1) + ' more' : '') + ' \\u2014 <a href="/dashboard/health">see Health</a>';
    } else if (info) {
      msg = info + (info === 1 ? ' suggestion' : ' suggestions') + ', nothing urgent \\u00b7 <a href="/dashboard/health">Health</a>';
    } else {
      msg = 'No open findings.';
    }
    sub.innerHTML = msg;
    var ring = document.getElementById('hero-score');
    var score = _health.vitals.health_score;
    ring.className = 'score-ring ' + scoreClass(score);
    ring.querySelector('span').textContent = score;
    requestAnimationFrame(function() { ring.style.setProperty('--pct', score + '%'); });
  }
  setTabAlert(offline ? 'offline' : state === 'critical' ? 'critical' : state === 'degraded' ? 'degraded' : 'ok');
  setNum(document.getElementById('fig-queued'), queued);
  setNum(document.getElementById('fig-inflight'), running);
  document.getElementById('fig-queued-card').classList.toggle('zero', !queued);
  document.getElementById('fig-inflight-card').classList.toggle('zero', !running);
}

function renderFigures(v) {
  setNum(document.getElementById('fig-requests'), v.total_requests_24h);
  var ttft = document.getElementById('fig-ttft');
  ttft.textContent = v.avg_ttft_ms != null ? fmtMs(v.avg_ttft_ms) : '\\u2014';
  var err = v.overall_error_rate_pct;
  document.getElementById('fig-errors').textContent = fmtPct(err);
  var card = document.getElementById('fig-errors-card');
  card.classList.toggle('zero', !err);
  card.classList.toggle('warn', err > 1 && err <= 5);
  card.classList.toggle('fail', err > 5);
}

async function loadHealth() {
  try {
    var r = await fetch('/dashboard/api/health');
    _health = await r.json();
    renderFigures(_health.vitals);
    renderStatus();
  } catch (e) { console.warn('health load failed', e); }
}

// ---- Nodes: one calm row each; the details sit behind a disclosure ----
function memExtras(m) {
  if (!m) return '';
  var parts = [];
  if (m.swap_used_gb > 0) parts.push('swap ' + formatGB(m.swap_used_gb));
  if (m.compressed_gb > 0) parts.push('compressed ' + formatGB(m.compressed_gb));
  return parts.join(' \\u00b7 ');
}
function thermalHot(node, cpu) {
  var st = node.thermal ? node.thermal.state : 'unknown';
  return st === 'warning' || (st === 'unknown' && cpu >= 95);
}
var _openNodes = {};
var _lastNodeKey = '';
function nodeDetails(node) {
  var blocks = [];
  var models = node.ollama ? node.ollama.models_loaded : [];
  var mlx = (node.ollama && node.ollama.mlx_models) || [];
  if (models.length || mlx.length) {
    var lines = models.map(function(m) {
      var meta = m.parameter_size ? m.parameter_size + (m.quantization ? ' ' + m.quantization : '') : formatGB(m.size_gb);
      var ctx = m.context_length ? (m.context_length >= 1024 ? Math.round(m.context_length / 1024) + 'K ctx' : m.context_length + ' ctx') : '';
      // Name the runner only when it is not the llama.cpp default (Ollama 0.40+
      // picks per model); an empty runner is pre-0.40 Ollama.
      var rn = m.runner || '';
      var backend = (rn && rn !== 'llamacpp') ? 'ollama/' + rn : 'ollama';
      return '<div class="model-line"><span class="mono">' + escapeHtml(m.name) + '</span><span class="muted small">' + escapeHtml(meta) + (ctx ? ' \\u00b7 ' + ctx : '') + '</span><span class="tag">' + backend + '</span></div>';
    }).concat(mlx.map(function(m) {
      return '<div class="model-line"><span class="mono">' + escapeHtml(m.replace(/^mlx:/, '').split('/').pop()) + '</span><span class="tag">mlx</span></div>';
    }));
    blocks.push('<div class="detail-block"><h5>Loaded</h5><div class="model-lines">' + lines.join('') + '</div></div>');
  }
  if (node.mlx_servers && node.mlx_servers.length) {
    var rows = node.mlx_servers.map(function(s) {
      var st = s.status === 'healthy' ? 'ok' : s.status === 'stopped' ? 'idle' : s.status === 'unhealthy' ? 'fail' : 'warn';
      var dist = s.distributed ? ' <span class="tag" title="Distributed via mlx.launch">' + escapeHtml(s.backend || 'dist') + (s.node_count ? ' \\u00b7 ' + s.node_count + ' hosts' : '') + '</span>' : '';
      return '<tr><td class="mono muted">:' + s.port + '</td><td>' + escapeHtml((s.model || '').split('/').pop()) + dist + '</td><td>' + statusDot(st, s.status.replace('_', ' ')) + '</td><td class="num muted">' + (s.model_size_gb ? s.model_size_gb.toFixed(1) + ' GB' : '\\u2014') + '</td><td class="num muted">' + (s.last_ok_ts ? timeAgo(s.last_ok_ts) : 'never') + '</td></tr>';
    }).join('');
    blocks.push('<div class="detail-block"><h5>MLX servers \\u00b7 bind ' + escapeHtml(node.mlx_bind_host || '127.0.0.1') + '</h5><table class="data-table"><tbody>' + rows + '</tbody></table></div>');
  }
  var services = [].concat(
    (node.image_models || []).map(function(m) { return ['Image', m]; }),
    (node.stt_models || []).map(function(m) { return ['Speech', m]; }),
    (node.vision_embed_models || []).map(function(m) { return ['Vision embed', m]; }),
    (node.embed_models || []).map(function(m) { return ['Embed', m]; })
  );
  if (services.length) {
    blocks.push('<div class="detail-block"><h5>Services</h5><div class="model-lines">' + services.map(function(s) {
      return '<div class="model-line"><span class="muted small svc-kind">' + s[0] + '</span><span class="mono">' + escapeHtml(s[1]) + '</span></div>';
    }).join('') + '</div></div>');
  }
  var cap = node.capacity;
  if (cap) {
    var modeLabels = { full: 'Full capacity', learned_high: 'High availability', learned_medium: 'Medium availability', learned_low: 'Low availability', paused: 'Paused', bootstrap: 'Learning' };
    var capState = cap.mode === 'paused' ? 'warn' : cap.mode === 'bootstrap' ? 'idle' : 'ok';
    blocks.push('<div class="detail-block"><h5>Adaptive capacity</h5><div class="kv">' +
      '<div><span class="k">Mode</span>' + statusDot(capState, modeLabels[cap.mode] || cap.mode) + '</div>' +
      '<div><span class="k">Availability</span>' + Math.round(cap.availability_score * 100) + '%</div>' +
      '<div><span class="k">Ceiling</span>' + (cap.ceiling_gb > 0 ? formatGB(cap.ceiling_gb) : 'None') + '</div>' +
      '<div><span class="k">Confidence</span>' + Math.round(cap.learning_confidence * 100) + '%</div>' +
      '<div><span class="k">Why</span>' + escapeHtml(cap.reason.replace(/_/g, ' ')) + (cap.override_active ? ' (override)' : '') + ' \\u00b7 ' + cap.days_observed + 'd observed</div>' +
      '</div></div>');
  }
  return blocks.join('');
}

function renderNodes(nodes) {
  var container = document.getElementById('nodes-container');
  if (!nodes.length) {
    container.innerHTML = '<div class="empty-state">No nodes connected. Start <code>herd-node</code> on a machine with Ollama.</div>';
    _lastNodeKey = '';
    return;
  }
  // Rebuild only when the set of nodes, their status, or their models change;
  // otherwise update values in place so an open disclosure stays open.
  var key = nodes.map(function(n) {
    var ms = n.ollama ? n.ollama.models_loaded.map(function(m) { return m.name; }).join('+') : '';
    return n.node_id + ':' + n.status + ':' + ms + ':' + (n.mlx_servers || []).map(function(s) { return s.status; }).join('+');
  }).join(',');
  if (key !== _lastNodeKey) {
    _lastNodeKey = key;
    container.innerHTML = nodes.map(function(node) {
      var id = 'node-' + node.node_id.replace(/[^a-zA-Z0-9]/g, '-');
      var hw = node.hardware || {};
      var chip = [hw.chip, hw.memory_total_gb ? Math.round(hw.memory_total_gb) + ' GB' : '', hw.cores_physical ? hw.cores_physical + ' cores' : ''].filter(Boolean).join(' \\u00b7 ');
      var loaded = (node.ollama ? node.ollama.models_loaded.length + (node.ollama.mlx_models || []).length : 0);
      var onDisk = node.ollama ? node.ollama.models_available_count : 0;
      var details = nodeDetails(node);
      return '<div class="node-group" id="' + id + '">' +
        '<div class="node-row">' +
          '<div><div class="node-name">' + dot(node.status) + escapeHtml(node.node_id) + ' <span class="faint small">' + node.status + '</span></div><div class="node-chip">' + escapeHtml(chip) + '</div></div>' +
          '<div><div class="meter-label"><span>CPU</span><b class="cpu-val"></b></div><div class="bar-container cpu-outer"><div class="bar-fill cpu-bar"></div></div></div>' +
          '<div><div class="meter-label"><span class="mem-label">Memory</span><b class="mem-val"></b></div><div class="bar-container mem-outer"><div class="bar-fill mem-bar"></div></div><div class="meter-extra mem-extra"></div></div>' +
          '<div class="node-models-count"><b>' + loaded + '</b>loaded \\u00b7 ' + onDisk + ' on disk</div>' +
        '</div>' +
        (details ? '<div class="node-detail-wrap"><details class="disclosure" data-node="' + escapeHtml(node.node_id) + '"' + (_openNodes[node.node_id] ? ' open' : '') + '><summary>Details</summary><div class="detail-grid">' + details + '</div></details></div>' : '') +
      '</div>';
    }).join('');
    container.querySelectorAll('details[data-node]').forEach(function(d) {
      d.addEventListener('toggle', function() { _openNodes[d.dataset.node] = d.open; });
    });
  }
  nodes.forEach(function(node) {
    var card = document.getElementById('node-' + node.node_id.replace(/[^a-zA-Z0-9]/g, '-'));
    if (!card) return;
    var cpu = node.cpu ? node.cpu.utilization_pct : 0;
    var memUsed = node.memory ? node.memory.used_gb : 0;
    var memTotal = node.memory ? node.memory.total_gb : (node.hardware || {}).memory_total_gb || 0;
    var memPct = memTotal > 0 ? (memUsed / memTotal) * 100 : 0;
    var pressure = node.memory ? node.memory.pressure : 'normal';
    card.querySelector('.cpu-val').textContent = cpu.toFixed(0) + '%';
    var cb = card.querySelector('.cpu-bar'); cb.style.width = cpu + '%'; cb.style.background = utilizationColor(cpu, 'cpu');
    card.querySelector('.mem-val').textContent = formatGB(memUsed) + ' of ' + formatGB(memTotal);
    var mb = card.querySelector('.mem-bar'); mb.style.width = memPct + '%'; mb.style.background = utilizationColor(memPct, 'mem');
    card.querySelector('.mem-extra').textContent = memExtras(node.memory);
    card.querySelector('.mem-label').textContent = pressure === 'normal' ? 'Memory' : 'Memory \\u00b7 pressure ' + pressure;
    var mo = card.querySelector('.mem-outer');
    mo.classList.toggle('bar-warning', pressure === 'warn');
    mo.classList.toggle('bar-critical', pressure === 'critical');
    card.querySelector('.cpu-outer').classList.toggle('bar-thermal', thermalHot(node, cpu));
  });
}

// ---- Models: activity over inventory.  A model that is busy, has served
// traffic, or has failed gets a row; the rest fold into one line.  A failure
// always keeps its row, so calm never hides a problem. ----
var _idleOpen = false;
function statCell(v, label, cls) { return '<div class="stat ' + (cls || '') + '"><span class="v">' + v + '</span><span class="l">' + label + '</span></div>'; }
function modelRow(key, q) {
  var parts = key.split(':');
  var node = q.node_id || parts[0];
  var name = q.model || parts.slice(1).join(':');
  var busy = (q.in_flight || 0) + (q.pending || 0) > 0;
  var failed = q.failed || 0;
  var st = failed > 0 && !busy ? 'warn' : busy ? 'ok' : 'idle';
  var stats;
  if (q.instant) {
    stats = statCell(fmtNum(q.completed || 0), 'done, 24 h', q.completed ? '' : 'zero') +
            statCell(fmtNum(failed), 'failed', failed ? 'fail' : 'zero') +
            statCell(fmtMs(q.avg_latency_ms), 'avg time', q.avg_latency_ms ? '' : 'zero') +
            (q.dimensions ? statCell(q.dimensions, 'dims', '') : '');
  } else {
    var cap = q.concurrency || 1;
    stats = statCell(fmtNum(q.in_flight || 0) + '<span class="faint">/' + cap + '</span>', 'running', q.in_flight ? '' : 'zero') +
            statCell(fmtNum(q.pending || 0), 'queued', q.pending > 3 ? 'warn' : q.pending ? '' : 'zero') +
            statCell(fmtNum(q.completed || 0), 'done', q.completed ? '' : 'zero') +
            statCell(fmtNum(failed), 'failed', failed ? 'fail' : 'zero') +
            statCell(fmtMs(q.avg_latency_ms), 'avg time', q.avg_latency_ms ? '' : 'zero');
  }
  var extras = [];
  if (q.backend && q.backend !== 'ollama') extras.push('<span class="tag">' + escapeHtml(q.backend) + '</span>');
  if (q.warm_hit_rate != null) extras.push('<span class="tag" title="Warm-cache hit rate; share of recent requests that were cold starts">warm ' + Math.round(q.warm_hit_rate * 100) + '% \\u00b7 cold ' + Math.round((q.cold_request_pct || 0) * 100) + '%</span>');
  else if (q.cache_hit_rate != null) extras.push('<span class="tag" title="Rolling prompt-cache hit rate">cache ' + Math.round(q.cache_hit_rate * 100) + '%</span>');
  if (q.cached === false) extras.push('<span class="tag" title="Weights download on the first request">downloads on first use</span>');
  if (!q.instant && q.stats_samples) extras.push('<span class="load">avg ' + fmtNum(Math.round(q.avg_prompt_tokens || 0)) + ' in \\u00b7 ' + fmtNum(Math.round(q.avg_completion_tokens || 0)) + ' out</span>');
  return '<div class="list-row model-row">' +
    '<div><div class="row">' + dot(st) + '<span class="name">' + escapeHtml(name) + '</span>' + extras.join('') + '</div><div class="where">' + escapeHtml(node) + '</div></div>' +
    '<div class="stats">' + stats + '</div></div>';
}
function isActive(q) { return (q.in_flight || 0) + (q.pending || 0) + (q.completed || 0) + (q.failed || 0) > 0; }
function renderQueues(queues) {
  var container = document.getElementById('queues-container');
  var entries = Object.entries(queues);
  if (!entries.length) { container.innerHTML = '<div class="empty-state">No models loaded</div>'; return; }
  var text = entries.filter(function(e) { return !e[1].instant; });
  var embed = entries.filter(function(e) { return e[1].instant; });
  function byLoad(a, b) {
    var qa = a[1], qb = b[1];
    return ((qb.in_flight || 0) + (qb.pending || 0)) - ((qa.in_flight || 0) + (qa.pending || 0)) || (qb.completed || 0) - (qa.completed || 0);
  }
  var active = text.filter(function(e) { return isActive(e[1]); }).sort(byLoad);
  var activeEmbed = embed.filter(function(e) { return isActive(e[1]); }).sort(byLoad);
  var idle = text.filter(function(e) { return !isActive(e[1]); }).concat(embed.filter(function(e) { return !isActive(e[1]); }));
  var html = '';
  if (active.length) html += '<div class="list">' + active.map(function(e) { return modelRow(e[0], e[1]); }).join('') + '</div>';
  if (activeEmbed.length) html += '<div class="section-title subhead">Embeddings and rerank</div><div class="list">' + activeEmbed.map(function(e) { return modelRow(e[0], e[1]); }).join('') + '</div>';
  if (!active.length && !activeEmbed.length) html += '<div class="card empty-state">No requests since the router started.</div>';
  if (idle.length) {
    html += '<details class="disclosure" id="idle-models"' + (_idleOpen ? ' open' : '') + '><summary>' + idle.length + (idle.length === 1 ? ' idle model' : ' idle models') + '</summary>' +
      '<div class="card flush"><table class="data-table idle-table"><thead><tr><th>Model</th><th>Node</th><th>Kind</th></tr></thead><tbody>' +
      idle.map(function(e) {
        var q = e[1], parts = e[0].split(':');
        var kind = q.instant ? (q.request_type === 'rerank' ? 'rerank' : 'embedding') : (q.backend || 'ollama');
        return '<tr><td class="name">' + escapeHtml(q.model || parts.slice(1).join(':')) + '</td><td>' + escapeHtml(q.node_id || parts[0]) + '</td><td>' + escapeHtml(kind) + (q.cached === false ? ' \\u00b7 downloads on first use' : '') + '</td></tr>';
      }).join('') + '</tbody></table></div></details>';
  }
  container.innerHTML = html;
  var d = document.getElementById('idle-models');
  if (d) d.addEventListener('toggle', function() { _idleOpen = d.open; });
  document.getElementById('models-summary').textContent = active.length + activeEmbed.length + ' active \\u00b7 ' + idle.length + ' idle';
}

// ---- Offline alerts.  The SSE payload already carries per-node status, so
// this needs no polling.  Only TRANSITIONS notify: re-alerting every 2 s for a
// node that is still down trains people to dismiss the alert.  The first
// payload seeds the baseline without notifying. ----
var _nodeStatus = null;
var _notifyPermission = (typeof Notification !== 'undefined') ? Notification.permission : 'unsupported';
// Chrome does not reliably render an SVG notification icon, so rasterise the
// favicon once.  Same-origin, so toDataURL works; null if it fails, and the
// notification simply goes out without an icon.
var _iconDataUrl = null;
(function rasterizeIcon() {
  try {
    var img = new Image();
    img.onload = function() {
      try {
        var c = document.createElement('canvas');
        c.width = 192; c.height = 192;
        c.getContext('2d').drawImage(img, 0, 0, 192, 192);
        _iconDataUrl = c.toDataURL('image/png');
      } catch (e) { console.warn('favicon rasterize failed:', e); }
    };
    img.src = '/favicon.svg';
  } catch (e) { console.warn('favicon rasterize setup failed:', e); }
})();
function requestNotifyPermission() {
  if (typeof Notification === 'undefined') return;
  // Chrome only shows the prompt from a user gesture, hence a button.
  Notification.requestPermission().then(function(p) { _notifyPermission = p; renderNotifyButton(); });
}
function renderNotifyButton() {
  var b = document.getElementById('notify-toggle');
  if (!b) return;
  if (_notifyPermission === 'unsupported') { b.hidden = true; return; }
  b.hidden = false;
  if (_notifyPermission === 'granted') { b.textContent = 'Alerts on'; b.disabled = true; }
  else if (_notifyPermission === 'denied') { b.textContent = 'Alerts blocked'; b.disabled = true; }
  else { b.textContent = 'Enable alerts'; b.disabled = false; }
}
function notifyOnOffline(nodes) {
  if (!nodes) return;
  var next = {};
  nodes.forEach(function(n) { next[n.node_id] = n.status; });
  if (_nodeStatus === null) { _nodeStatus = next; renderNotifyButton(); return; }
  for (var id in next) {
    var was = _nodeStatus[id];
    if (next[id] === 'offline' && was && was !== 'offline' && _notifyPermission === 'granted') {
      try {
        var opts = { body: 'herd stopped receiving heartbeats. The fleet may have no capacity.', tag: 'herd-offline-' + id, requireInteraction: true };
        if (_iconDataUrl) { opts.icon = _iconDataUrl; opts.badge = _iconDataUrl; }
        var n = new Notification('Node offline: ' + id, opts);
        n.onclick = function() { window.focus(); n.close(); };
      } catch (err) { console.warn('Notification failed:', err); }
      console.warn('Node went offline:', id);
    }
  }
  _nodeStatus = next;
}

// ---- Live stream ----
var _sseWatchdog = null;
function connect() {
  var es = new EventSource('/dashboard/events');
  function resetWatchdog() {
    clearTimeout(_sseWatchdog);
    _sseWatchdog = setTimeout(function() { console.warn('SSE watchdog: no event in 10s, reconnecting'); es.close(); setTimeout(connect, 1000); }, 10000);
  }
  es.onopen = function() { setLive('live'); resetWatchdog(); };
  es.onmessage = function(e) {
    resetWatchdog();
    try {
      _sse = JSON.parse(e.data);
      notifyOnOffline(_sse.nodes);
      renderNodes(_sse.nodes);
      renderQueues(_sse.queues);
      renderStatus();
    } catch (err) { console.error('Parse error:', err); }
  };
  es.onerror = function() { clearTimeout(_sseWatchdog); setLive('reconnecting'); es.close(); setTimeout(connect, 3000); };
}

// ---- Fleet Intelligence: below the fold, collapsed to its first sentence.
// Never a loading state at the top of the page. ----
async function fetchBriefing(force) {
  try {
    var data = await (await fetch('/dashboard/api/briefing' + (force ? '?refresh=1' : ''))).json();
    var section = document.getElementById('briefing-section');
    if (!data.enabled) { section.hidden = true; return; }
    section.hidden = false;
    var lede = document.getElementById('briefing-lede');
    if (data.generating && !data.briefing) { lede.textContent = 'Generating a briefing\\u2026'; return; }
    if (data.briefing) {
      document.getElementById('briefing-content').innerHTML = renderBriefing(data.briefing);
      var first = data.briefing.replace(/\\*\\*/g, '').replace(/^[-*\\s]+/, '').split(/(?<=[.!?])\\s/)[0];
      lede.textContent = first.length > 140 ? first.slice(0, 137) + '\\u2026' : first;
      var ago = data.generated_at ? Math.round((Date.now() / 1000 - data.generated_at) / 60) : 0;
      document.getElementById('briefing-meta').innerHTML =
        '<span>Generated ' + (ago < 1 ? 'just now' : ago + 'm ago') + ' with <code>' + escapeHtml(data.model || '?') + '</code></span>' +
        (data.next_refresh_s ? '<span>next in ~' + Math.round(data.next_refresh_s / 60) + 'm</span>' : '') +
        '<button class="btn small quiet" onclick="fetchBriefing(true)">Refresh</button>';
    } else if (data.error) {
      lede.textContent = data.error;
    }
  } catch (e) { /* retried on the next poll */ }
}

renderNotifyButton();
connect();
loadHealth();
setInterval(loadHealth, 15000);
fetchBriefing();
setInterval(fetchBriefing, 30000);
</script>
"""

# ---------------------------------------------------------------------------
# Models page body (Model Insights and Trends, merged)
# ---------------------------------------------------------------------------

_MODELS_BODY = """
<div class="page-head">
  <div><h1 class="page-title">Models</h1><p class="page-sub">Traffic across the fleet, and how each model performs</p></div>
  <div id="models-time-range"></div>
</div>

<section class="figures" id="model-summary-cards" aria-label="Summary"></section>

<section class="section" id="trends">
  <div class="section-head"><h2 class="section-title">Trends</h2></div>
  <div class="charts-row">
    <div class="chart-card"><h4>Requests per hour</h4><canvas id="requests-chart"></canvas></div>
    <div class="chart-card"><h4>Average latency per hour</h4><canvas id="latency-chart"></canvas></div>
  </div>
  <div class="charts-row">
    <div class="chart-card full-width"><h4>Tokens per hour</h4><canvas id="tokens-chart"></canvas></div>
  </div>
</section>

<section class="section" id="per-model">
  <div class="section-head"><h2 class="section-title">Per model</h2></div>
  <div class="charts-row">
    <div class="chart-card"><h4>Average latency by model</h4><canvas id="comparison-chart"></canvas></div>
    <div class="chart-card"><h4>Share of tokens</h4><canvas id="token-dist-chart"></canvas></div>
  </div>
  <div class="card flush table-scroll">
    <table class="data-table" id="model-table">
      <thead><tr><th>Model</th><th class="num">Requests</th><th class="num">Avg latency</th><th class="num">Tokens/s</th><th class="num">Prompt tokens</th><th class="num">Completion tokens</th><th class="num">Last seen</th></tr></thead>
      <tbody id="model-tbody"></tbody>
    </table>
  </div>
  <div class="charts-row" id="daily-section" hidden>
    <div class="chart-card"><h4 id="daily-title">Daily requests</h4><canvas id="daily-requests-chart"></canvas></div>
    <div class="chart-card"><h4>Daily average latency</h4><canvas id="daily-latency-chart"></canvas></div>
  </div>
</section>

<section class="section" id="benchmarks-link">
  <a class="list list-row" href="/dashboard/benchmarks">
    <span><span class="name">Benchmarks</span><br><span class="small muted">These figures come from real traffic. Benchmarks measure what the fleet can do under controlled load.</span></span>
    <span class="chev" aria-hidden="true">›</span>
  </a>
</section>

<script>
var requestsChart, latencyChart, tokensChart, comparisonChart, tokenDistChart, dailyReqChart, dailyLatChart;
var allDaily = [], _range = [0, 0], _selected = null;

function noLegend(T) { return Object.assign({}, T.defaults, { plugins: Object.assign({}, T.defaults.plugins, { legend: { display: false } }) }); }

async function loadAll(startTs, endTs) {
  _range = [startTs, endTs];
  var qs = 'start_ts=' + startTs + '&end_ts=' + endTs;
  var res = await Promise.all([fetch('/dashboard/api/trends?' + qs), fetch('/dashboard/api/models?' + qs)]);
  var trends = await res[0].json(), models = await res[1].json();
  renderSummary(models.summary);
  renderTrends(trends.data);
  renderModels(models);
}

function renderSummary(summary) {
  var reqs = summary.reduce(function(s, m) { return s + m.total_requests; }, 0);
  var pin = summary.reduce(function(s, m) { return s + m.total_prompt_tokens; }, 0);
  var pout = summary.reduce(function(s, m) { return s + m.total_completion_tokens; }, 0);
  var lat = reqs ? summary.reduce(function(s, m) { return s + m.avg_latency_ms * m.total_requests; }, 0) / reqs : 0;
  function fig(v, l) { return '<div class="figure"><span class="figure-value">' + v + '</span><span class="figure-label">' + l + '</span></div>'; }
  document.getElementById('model-summary-cards').innerHTML =
    fig(fmtNum(reqs), 'Requests') + fig(fmtMs(lat), 'Average latency') + fig(fmtCompact(pin), 'Tokens in') + fig(fmtCompact(pout), 'Tokens out') + fig(summary.length, 'Models used');
}

function renderTrends(data) {
  var T = chartTheme();
  var labels = data.map(function(d) { return fmtDateTime(d.hour_bucket); });
  [requestsChart, latencyChart, tokensChart].forEach(function(c) { if (c) c.destroy(); });
  requestsChart = new Chart(document.getElementById('requests-chart'), {
    type: 'bar',
    data: { labels: labels, datasets: [{ label: 'Requests', data: data.map(function(d) { return d.request_count; }), backgroundColor: withAlpha(T.color(0), 0.75), borderRadius: 3 }] },
    options: noLegend(T),
  });
  latencyChart = new Chart(document.getElementById('latency-chart'), {
    type: 'line',
    data: { labels: labels, datasets: [{ label: 'Avg latency (s)', data: data.map(function(d) { return d.avg_latency_ms / 1000; }), borderColor: T.color(1), backgroundColor: withAlpha(T.color(1), 0.12), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 }] },
    options: noLegend(T),
  });
  var stacked = JSON.parse(JSON.stringify(T.defaults.scales));
  stacked.x.stacked = true; stacked.y.stacked = true;
  stacked.x.ticks.callback = T.defaults.scales.x.ticks.callback;
  stacked.y.ticks.callback = T.defaults.scales.y.ticks.callback;
  tokensChart = new Chart(document.getElementById('tokens-chart'), {
    type: 'bar',
    data: { labels: labels, datasets: [
      { label: 'Prompt tokens', data: data.map(function(d) { return d.total_prompt_tokens; }), backgroundColor: withAlpha(T.color(1), 0.75) },
      { label: 'Completion tokens', data: data.map(function(d) { return d.total_completion_tokens; }), backgroundColor: withAlpha(T.color(2), 0.75) },
    ] },
    options: Object.assign({}, T.defaults, { scales: stacked }),
  });
}

function renderModels(data) {
  var T = chartTheme();
  var summary = data.summary;
  allDaily = data.daily;
  var tbody = document.getElementById('model-tbody');
  if (!summary.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No requests in this range.</td></tr>';
  } else {
    tbody.innerHTML = summary.map(function(m) {
      var tps = m.avg_latency_ms > 0 && m.total_completion_tokens > 0 ? (m.total_completion_tokens / m.total_requests) / (m.avg_latency_ms / 1000) : null;
      return '<tr class="clickable" data-model="' + escapeHtml(m.model_name) + '">' +
        '<td class="name">' + escapeHtml(m.model_name) + '</td>' +
        '<td class="num">' + fmtNum(m.total_requests) + '</td>' +
        '<td class="num">' + fmtMs(m.avg_latency_ms) + '</td>' +
        '<td class="num">' + (tps == null ? '\\u2014' : fmtNum(tps)) + '</td>' +
        '<td class="num">' + fmtCompact(m.total_prompt_tokens) + '</td>' +
        '<td class="num">' + fmtCompact(m.total_completion_tokens) + '</td>' +
        '<td class="num muted">' + (m.last_seen ? timeAgo(m.last_seen) : '\\u2014') + '</td></tr>';
    }).join('');
    tbody.querySelectorAll('tr[data-model]').forEach(function(tr) {
      tr.addEventListener('click', function() { selectModel(tr.dataset.model, tr); });
    });
  }
  [comparisonChart, tokenDistChart].forEach(function(c) { if (c) c.destroy(); });
  var names = summary.map(function(m) { return m.model_name; });
  comparisonChart = new Chart(document.getElementById('comparison-chart'), {
    type: 'bar',
    data: { labels: names, datasets: [{ label: 'Avg latency (s)', data: summary.map(function(m) { return m.avg_latency_ms / 1000; }),
      backgroundColor: summary.map(function(_, i) { return withAlpha(T.color(i), 0.75); }), borderRadius: 3 }] },
    options: Object.assign(noLegend(T), { indexAxis: 'y' }),
  });
  tokenDistChart = new Chart(document.getElementById('token-dist-chart'), {
    type: 'doughnut',
    data: { labels: names, datasets: [{ data: summary.map(function(m) { return m.total_prompt_tokens + m.total_completion_tokens; }),
      backgroundColor: summary.map(function(_, i) { return T.color(i); }), borderColor: T.surface, borderWidth: 2 }] },
    options: { responsive: true, aspectRatio: 2, cutout: '62%', animation: T.defaults.animation, plugins: { legend: { position: 'right', labels: { color: T.dim, font: { size: 11 }, boxWidth: 10, padding: 10 } },
      tooltip: { callbacks: { label: function(ctx) { return ctx.label + ': ' + fmtCompact(ctx.parsed) + ' tokens'; } } } } },
  });
  var keep = _selected && summary.some(function(m) { return m.model_name === _selected; }) ? _selected : (summary[0] && summary[0].model_name);
  if (keep) selectModel(keep, tbody.querySelector('tr[data-model="' + CSS.escape(keep) + '"]'));
  else document.getElementById('daily-section').hidden = true;
}

function selectModel(name, row) {
  _selected = name;
  document.querySelectorAll('#model-table tr.selected').forEach(function(r) { r.classList.remove('selected'); });
  if (row) row.classList.add('selected');
  var rows = allDaily.filter(function(d) { return d.model_name === name; });
  var section = document.getElementById('daily-section');
  if (!rows.length) { section.hidden = true; return; }
  section.hidden = false;
  document.getElementById('daily-title').textContent = 'Daily requests \\u2014 ' + name;
  var T = chartTheme(), labels = rows.map(function(d) { return fmtDay(d.day_bucket); });
  [dailyReqChart, dailyLatChart].forEach(function(c) { if (c) c.destroy(); });
  dailyReqChart = new Chart(document.getElementById('daily-requests-chart'), {
    type: 'bar',
    data: { labels: labels, datasets: [{ label: 'Requests', data: rows.map(function(d) { return d.request_count; }), backgroundColor: withAlpha(T.color(0), 0.75), borderRadius: 3 }] },
    options: noLegend(T),
  });
  dailyLatChart = new Chart(document.getElementById('daily-latency-chart'), {
    type: 'line',
    data: { labels: labels, datasets: [{ label: 'Avg latency (s)', data: rows.map(function(d) { return d.avg_latency_ms / 1000; }), borderColor: T.color(1), backgroundColor: withAlpha(T.color(1), 0.12), fill: true, tension: 0.3, pointRadius: 2, borderWidth: 2 }] },
    options: noLegend(T),
  });
}

onThemeChange(function() { if (_range[0]) loadAll(_range[0], _range[1]); });
initTimeRange('models-time-range', loadAll, '72h');
</script>
"""


# ---------------------------------------------------------------------------
# Apps analytics page body
# ---------------------------------------------------------------------------

_TAGS_BODY = """
<style>
.howto { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; padding-bottom: 6px; }
.howto pre { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: var(--fs-label); color: var(--text-2); overflow-x: auto; margin-top: 6px; }
.howto h5 { font-size: var(--fs-small); font-weight: 600; }
.swatch { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; vertical-align: middle; }
</style>

<div class="page-head">
  <div><h1 class="page-title">Usage</h1><p class="page-sub">Requests by the tags your apps send</p></div>
  <div id="tags-time-range"></div>
</div>

<section class="figures" aria-label="Summary">
  <div class="figure"><span class="figure-value" id="tagged-count">—</span><span class="figure-label">Tagged requests</span></div>
  <div class="figure"><span class="figure-value" id="unique-tags">—</span><span class="figure-label">Distinct tags</span></div>
  <div class="figure"><span class="figure-value text" id="top-tag">—</span><span class="figure-label">Most used</span></div>
</section>

<section class="section">
  <div class="charts-row">
    <div class="chart-card"><h4>Requests by tag</h4><canvas id="tag-bar-chart"></canvas></div>
    <div class="chart-card"><h4>Tag activity over time</h4><canvas id="tag-daily-chart"></canvas></div>
  </div>
  <div class="card flush table-scroll">
    <table class="data-table" id="apps-table">
      <thead><tr><th>Tag</th><th class="num">Requests</th><th class="num">Avg latency</th><th class="num">Avg TTFT</th><th class="num">Prompt tokens</th><th class="num">Completion tokens</th><th class="num">Error rate</th></tr></thead>
      <tbody id="apps-tbody"></tbody>
    </table>
  </div>
  <div class="charts-row" id="tag-daily-section" hidden>
    <div class="chart-card"><h4 id="tag-daily-title">Daily requests</h4><canvas id="tag-daily-requests-chart"></canvas></div>
    <div class="chart-card"><h4>Daily average latency</h4><canvas id="tag-daily-latency-chart"></canvas></div>
  </div>
</section>

<details class="disclosure card">
  <summary>How to tag requests</summary>
  <p class="note">Add tags to your requests so they appear here. Tags are stripped before reaching Ollama.</p>
  <div class="howto">
    <div class="inset"><h5>OpenAI SDK (Python)</h5><pre>client.chat.completions.create(
  model="llama3.2:3b",
  messages=[...],
  extra_body={"metadata": {"tags": ["my-app"]}}
)</pre></div>
    <div class="inset"><h5>Header</h5><pre>curl http://router:11435/api/chat \\
  -H "X-Herd-Tags: my-app, prod" \\
  -d '{"model": "...", "messages": [...]}'</pre></div>
  </div>
</details>

<script>
var barChart, dailyChart, dailyReqChart, dailyLatChart, _tagRange = null, _tagDaily = [];
function noLegend(T) { return Object.assign({}, T.defaults, { plugins: Object.assign({}, T.defaults.plugins, { legend: { display: false } }) }); }

async function loadTags(startTs, endTs) {
  _tagRange = [startTs, endTs];
  var qs = 'start_ts=' + startTs + '&end_ts=' + endTs;
  var res = await Promise.all([fetch('/dashboard/api/tags?' + qs), fetch('/dashboard/api/tags/daily?' + qs)]);
  var data = (await res[0].json()).data || [];
  var dailyData = (await res[1].json()).data || [];
  _tagDaily = dailyData;
  var T = chartTheme();

  setNum(document.getElementById('tagged-count'), data.reduce(function(s, d) { return s + d.request_count; }, 0));
  document.getElementById('unique-tags').textContent = data.length;
  document.getElementById('top-tag').textContent = data.length ? data[0].tag : 'None';

  if (barChart) barChart.destroy();
  barChart = new Chart(document.getElementById('tag-bar-chart'), {
    type: 'bar',
    data: { labels: data.map(function(d) { return d.tag; }), datasets: [{ label: 'Requests', data: data.map(function(d) { return d.request_count; }),
      backgroundColor: data.map(function(_, i) { return withAlpha(T.color(i), 0.75); }), borderRadius: 3 }] },
    options: noLegend(T),
  });

  if (dailyChart) dailyChart.destroy();
  var tags = Array.from(new Set(dailyData.map(function(d) { return d.tag; })));
  var days = Array.from(new Set(dailyData.map(function(d) { return d.day_bucket; }))).sort();
  dailyChart = new Chart(document.getElementById('tag-daily-chart'), {
    type: 'line',
    data: { labels: days.map(fmtDay), datasets: tags.map(function(tag, i) {
      return { label: tag, borderColor: T.color(i), backgroundColor: T.color(i), tension: 0.3, pointRadius: 2, borderWidth: 2,
        data: days.map(function(day) { var f = dailyData.find(function(d) { return d.tag === tag && d.day_bucket === day; }); return f ? f.request_count : 0; }) };
    }) },
    options: T.defaults,
  });

  var tbody = document.getElementById('apps-tbody');
  if (!data.length) { tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No tagged requests in this range. See \\u201cHow to tag requests\\u201d below.</td></tr>'; return; }
  tbody.innerHTML = data.map(function(d, i) {
    var rate = d.request_count ? d.failed_count / d.request_count * 100 : 0;
    return '<tr class="clickable" data-tag="' + escapeHtml(d.tag) + '">' +
      '<td><span class="swatch" style="background:' + T.color(i) + '"></span>' + escapeHtml(d.tag) + '</td>' +
      '<td class="num">' + fmtNum(d.request_count) + '</td>' +
      '<td class="num">' + fmtMs(d.avg_latency_ms) + '</td>' +
      '<td class="num">' + fmtMs(d.avg_ttft_ms) + '</td>' +
      '<td class="num">' + fmtCompact(d.total_prompt_tokens || 0) + '</td>' +
      '<td class="num">' + fmtCompact(d.total_completion_tokens || 0) + '</td>' +
      '<td class="num' + (rate > 5 ? ' text-fail' : rate > 0 ? '' : ' faint') + '">' + fmtPct(rate) + '</td></tr>';
  }).join('');
  tbody.querySelectorAll('tr[data-tag]').forEach(function(tr) { tr.addEventListener('click', function() { showTagDaily(tr.dataset.tag); }); });
}

function showTagDaily(tag) {
  var section = document.getElementById('tag-daily-section');
  section.hidden = false;
  document.getElementById('tag-daily-title').textContent = 'Daily requests \\u2014 ' + tag;
  section.scrollIntoView({ behavior: REDUCED_MOTION ? 'auto' : 'smooth', block: 'nearest' });
  var rows = _tagDaily.filter(function(d) { return d.tag === tag; }).sort(function(a, b) { return a.day_bucket - b.day_bucket; });
  var T = chartTheme(), labels = rows.map(function(d) { return fmtDay(d.day_bucket); });
  [dailyReqChart, dailyLatChart].forEach(function(c) { if (c) c.destroy(); });
  dailyReqChart = new Chart(document.getElementById('tag-daily-requests-chart'), {
    type: 'bar', data: { labels: labels, datasets: [{ label: 'Requests', data: rows.map(function(d) { return d.request_count; }), backgroundColor: withAlpha(T.color(0), 0.75), borderRadius: 3 }] },
    options: noLegend(T),
  });
  dailyLatChart = new Chart(document.getElementById('tag-daily-latency-chart'), {
    type: 'line', data: { labels: labels, datasets: [{ label: 'Avg latency (s)', data: rows.map(function(d) { return d.avg_latency_ms ? d.avg_latency_ms / 1000 : 0; }), borderColor: T.color(1), backgroundColor: withAlpha(T.color(1), 0.12), fill: true, tension: 0.3, pointRadius: 2, borderWidth: 2 }] },
    options: noLegend(T),
  });
}

onThemeChange(function() { if (_tagRange) loadTags(_tagRange[0], _tagRange[1]); });
initTimeRange('tags-time-range', loadTags, '7d');
</script>
"""


# ---------------------------------------------------------------------------
# Benchmarks page body
# ---------------------------------------------------------------------------

_BENCHMARKS_BODY = """
<style>
details.bench-run > summary { padding: 12px 18px; display: grid; grid-template-columns: 1.4fr repeat(6, minmax(60px, 1fr)); gap: 10px; align-items: center; color: var(--text-1); }
details.bench-run > summary::before { display: none; }
.bench-run > summary .num { text-align: right; }
.bench-run > summary .l { display: block; font-size: 11px; color: var(--text-2); }
.bench-run { border-top: 1px solid var(--hairline); }
.bench-run:first-child { border-top: none; }
.bench-run[open] > summary { background: var(--surface-2); }
.bench-detail { padding: 14px 18px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.bench-detail h5 { font-size: var(--fs-label); font-weight: 600; color: var(--text-2); margin-bottom: 6px; }
.mode-option { border: 1px solid var(--hairline); border-radius: var(--radius-2); padding: 12px 14px; margin-bottom: 8px; cursor: pointer; }
.mode-option.selected { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
.mode-option h4 { font-size: var(--fs-body); font-weight: 600; margin-bottom: 2px; }
.mode-option p { font-size: var(--fs-label); color: var(--text-2); margin: 0; }
.dialog-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 14px 0; }
.dialog-row > label:first-child { font-size: var(--fs-label); color: var(--text-2); min-width: 72px; }
.type-check { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-small); }
.type-check.disabled { opacity: 0.45; }
.type-check input { accent-color: var(--accent); }
.progress { height: 6px; background: var(--surface-2); border-radius: 3px; overflow: hidden; margin: 6px 0 10px; }
.progress .fill { height: 100%; border-radius: 3px; background: var(--accent); transition: width 0.5s ease; }
.progress.done .fill { background: var(--ok); }
.progress.error .fill { background: var(--fail); }
.progress-label { font-size: var(--fs-label); color: var(--text-2); display: flex; justify-content: space-between; }
.progress-stats { display: flex; gap: 20px; font-size: var(--fs-label); color: var(--text-2); flex-wrap: wrap; }
.progress-stats b { color: var(--text-1); font-weight: 600; }
@media (max-width: 768px) {
  details.bench-run > summary { grid-template-columns: 1fr 1fr 1fr; }
  .bench-detail { grid-template-columns: 1fr; }
}
</style>

<div class="page-head">
  <div><h1 class="page-title">Benchmarks</h1><p class="page-sub">Fleet throughput under controlled load, and how it has changed</p></div>
  <button class="btn primary" id="run-bench-btn" onclick="showBenchDialog()">Run benchmark</button>
</div>

<section class="section" id="benchmarks" aria-label="Benchmark results">
  <div id="bench-progress" class="card" hidden></div>
  <div class="figures" id="bench-summary"></div>
  <div class="charts-row">
    <div class="chart-card fixed"><h4>Capacity growth</h4><div class="chart-box"><canvas id="capacity-chart"></canvas></div></div>
    <div class="chart-card fixed"><h4>Throughput trend</h4><div class="chart-box"><canvas id="throughput-chart"></canvas></div></div>
  </div>
  <div class="charts-row">
    <div class="chart-card fixed"><h4>Model throughput <span class="faint">latest run</span></h4><div class="chart-box"><canvas id="model-throughput-chart"></canvas></div></div>
    <div class="chart-card fixed"><h4>Model latency <span class="faint">latest run</span></h4><div class="chart-box"><canvas id="model-latency-chart"></canvas></div></div>
  </div>
  <div class="charts-row">
    <div class="chart-card fixed"><h4>Model performance over time</h4><div class="chart-box"><canvas id="model-history-chart"></canvas></div></div>
    <div class="chart-card fixed"><h4>Node utilization <span class="faint">latest run</span></h4><div class="chart-box"><canvas id="node-util-chart"></canvas></div></div>
  </div>
  <div class="list" id="bench-runs"></div>
  <div class="card empty-state" id="empty-state" hidden>
    <h3>No benchmark runs yet</h3>
    Run one with the button above, or from a shell: <code>python scripts/benchmark.py --duration 60</code>
  </div>
</section>

<script>
var capacityChart, throughputChart, modelThroughputChart, modelLatencyChart, modelHistoryChart, nodeUtilChart;
var _benchData = null;

function short(s, n) { return s.length > n ? s.substring(0, n - 3) + '...' : s; }

async function loadBenchmarks() {
  var data = (await (await fetch('/dashboard/api/benchmarks?limit=100')).json()).data;
  _benchData = data;
  var runs = document.getElementById('bench-runs');
  var empty = document.getElementById('empty-state');
  var summary = document.getElementById('bench-summary');
  if (!data || !data.length) { runs.innerHTML = ''; runs.hidden = true; empty.hidden = false; summary.innerHTML = ''; return; }
  runs.hidden = false; empty.hidden = true;

  var best = Math.max.apply(null, data.map(function(d) { return d.tokens_per_sec || 0; }));
  var latestNodes = data[0].fleet_snapshot ? data[0].fleet_snapshot.nodes || [] : [];
  var tokens = data.reduce(function(s, d) { return s + (d.total_prompt_tokens || 0) + (d.total_completion_tokens || 0); }, 0);
  function fig(v, l) { return '<div class="figure"><span class="figure-value">' + v + '</span><span class="figure-label">' + l + '</span></div>'; }
  summary.innerHTML = fig(data.length, 'Runs') + fig(fmtNum(best), 'Best tokens/s') + fig(latestNodes.length, 'Nodes in latest run') + fig(fmtCompact(tokens), 'Tokens generated');

  function table(head, rows) {
    return '<table class="data-table"><thead><tr>' + head.map(function(h, i) { return '<th' + (i ? ' class="num"' : '') + '>' + h + '</th>'; }).join('') + '</tr></thead><tbody>' +
      rows.map(function(r) { return '<tr>' + r.map(function(c, i) { return '<td' + (i ? ' class="num"' : '') + '>' + c + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table>';
  }
  runs.innerHTML = data.map(function(run) {
    var fleet = run.fleet_snapshot || {};
    var mr = run.per_model_results || [], nr = run.per_node_results || [], pu = run.peak_utilization || [];
    var detail = '<div class="bench-detail">' +
      '<div><h5>Per model</h5>' + (mr.length ? table(['Model', 'Requests', 'tok/s', 'Avg latency', 'Avg TTFT'], mr.map(function(m) { return [escapeHtml(m.model), fmtNum(m.requests), fmtNum(m.tok_s), fmtMs(m.avg_latency_ms), fmtMs(m.avg_ttft_ms)]; })) : '<p class="note">No data</p>') + '</div>' +
      '<div><h5>Per node</h5>' + (nr.length ? table(['Node', 'Requests', 'Share', 'tok/s', 'Tokens'], nr.map(function(n) { return [escapeHtml(n.node_id), fmtNum(n.requests), fmtPct(n.pct), fmtNum(n.tok_s), fmtNum(n.tokens || 0)]; })) : '<p class="note">No data</p>') + '</div>' +
      (pu.length ? '<div><h5>Peak utilization</h5>' + table(['Node', 'CPU peak', 'Memory peak', 'Active peak'], pu.map(function(u) { return [escapeHtml(u.node_id), fmtPct(u.cpu_peak), fmtPct(u.mem_peak), fmtNum(u.active_peak || 0)]; })) + '</div>' : '') +
      '</div>';
    function c(v, l) { return '<span class="num">' + v + '<span class="l">' + l + '</span></span>'; }
    return '<details class="disclosure bench-run"><summary>' +
      '<span>' + fmtDateTime(run.timestamp) + '<span class="l">' + fmtDuration(run.duration_s) + '</span></span>' +
      c((fleet.nodes || []).length, 'nodes') + c((fleet.models || []).length, 'models') + c(fmtNum(run.total_requests), 'requests') +
      c(fmtNum(run.tokens_per_sec), 'tok/s') + c(fmtMs(run.latency_p50_ms), 'latency p50') + c(fmtMs(run.ttft_p50_ms), 'TTFT p50') +
      '</summary>' + detail + '</details>';
  }).join('');
  renderBenchCharts(data);
}

function renderBenchCharts(data) {
  var T = chartTheme({ fixed: true });
  var D = T.defaults;
  function scales(extra) { var sc = { x: Object.assign({}, D.scales.x), y: Object.assign({}, D.scales.y) }; return Object.assign(sc, extra || {}); }
  [capacityChart, throughputChart, modelThroughputChart, modelLatencyChart, modelHistoryChart, nodeUtilChart].forEach(function(ch) { if (ch) ch.destroy(); });
  var sorted = data.slice().reverse();
  var labels = sorted.map(function(d) { return fmtDateTime(d.timestamp); });

  capacityChart = new Chart(document.getElementById('capacity-chart'), {
    type: 'line',
    data: { labels: labels, datasets: [
      { label: 'tok/s', data: sorted.map(function(d) { return d.tokens_per_sec || 0; }), borderColor: T.color(0), backgroundColor: withAlpha(T.color(0), 0.12), fill: true, tension: 0.3, pointRadius: 3, borderWidth: 2, yAxisID: 'y' },
      { label: 'Nodes', data: sorted.map(function(d) { return d.fleet_snapshot && d.fleet_snapshot.nodes ? d.fleet_snapshot.nodes.length : 0; }), borderColor: T.color(2), backgroundColor: T.color(2), stepped: true, pointRadius: 3, borderWidth: 2, yAxisID: 'y1' },
    ] },
    options: Object.assign({}, D, { scales: scales({
      y: Object.assign({}, D.scales.y, { title: { display: true, text: 'Tokens/sec', color: T.dim } }),
      y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: T.dim, stepSize: 1, font: { size: 10 } }, title: { display: true, text: 'Nodes', color: T.dim } },
    }) }),
  });
  throughputChart = new Chart(document.getElementById('throughput-chart'), {
    type: 'bar',
    data: { labels: labels, datasets: [{ label: 'req/s', data: sorted.map(function(d) { return d.requests_per_sec || 0; }), backgroundColor: withAlpha(T.color(1), 0.75), borderRadius: 3 }] },
    options: Object.assign({}, D, { plugins: Object.assign({}, D.plugins, { legend: { display: false } }) }),
  });

  var pm = data[0].per_model_results || [];
  if (pm.length) {
    var mLabels = pm.map(function(m) { return short(m.model, 25); });
    modelThroughputChart = new Chart(document.getElementById('model-throughput-chart'), {
      type: 'bar',
      data: { labels: mLabels, datasets: [{ label: 'tok/s', data: pm.map(function(m) { return m.tok_s || 0; }), backgroundColor: pm.map(function(_, i) { return withAlpha(T.color(i), 0.8); }), borderRadius: 3 }] },
      options: Object.assign({}, D, { indexAxis: 'y', plugins: Object.assign({}, D.plugins, { legend: { display: false } }),
        scales: scales({ x: Object.assign({}, D.scales.x, { title: { display: true, text: 'Tokens/sec', color: T.dim } }) }) }),
    });
    modelLatencyChart = new Chart(document.getElementById('model-latency-chart'), {
      type: 'bar',
      data: { labels: mLabels, datasets: [
        { label: 'Avg latency', data: pm.map(function(m) { return m.avg_latency_ms || 0; }), backgroundColor: withAlpha(T.color(0), 0.8), borderRadius: 3 },
        { label: 'Avg TTFT', data: pm.map(function(m) { return m.avg_ttft_ms || 0; }), backgroundColor: withAlpha(T.color(2), 0.8), borderRadius: 3 },
      ] },
      options: Object.assign({}, D, { indexAxis: 'y', scales: scales({ x: Object.assign({}, D.scales.x, { title: { display: true, text: 'Milliseconds', color: T.dim } }) }) }),
    });
  }

  var allModels = [];
  sorted.forEach(function(d) { (d.per_model_results || []).forEach(function(m) { if (allModels.indexOf(m.model) === -1) allModels.push(m.model); }); });
  if (allModels.length) {
    modelHistoryChart = new Chart(document.getElementById('model-history-chart'), {
      type: 'line',
      data: { labels: labels, datasets: allModels.map(function(model, i) {
        return { label: short(model, 20), borderColor: T.color(i), backgroundColor: T.color(i), tension: 0.3, pointRadius: 3, borderWidth: 2, spanGaps: true,
          data: sorted.map(function(d) { var r = (d.per_model_results || []).find(function(m) { return m.model === model; }); return r ? r.tok_s : null; }) };
      }) },
      options: Object.assign({}, D, { scales: scales({ y: Object.assign({}, D.scales.y, { title: { display: true, text: 'Tokens/sec', color: T.dim } }) }) }),
    });
  }

  var pu = data[0].peak_utilization || [];
  if (pu.length) {
    nodeUtilChart = new Chart(document.getElementById('node-util-chart'), {
      type: 'bar',
      data: { labels: pu.map(function(u) { return short(u.node_id, 20); }), datasets: [
        { label: 'CPU avg %', data: pu.map(function(u) { return u.cpu_avg || 0; }), backgroundColor: withAlpha(T.color(1), 0.8), borderRadius: 3 },
        { label: 'CPU peak %', data: pu.map(function(u) { return u.cpu_peak || 0; }), backgroundColor: withAlpha(T.color(0), 0.8), borderRadius: 3 },
        { label: 'Memory peak %', data: pu.map(function(u) { return u.mem_peak || 0; }), backgroundColor: withAlpha(T.color(2), 0.8), borderRadius: 3 },
      ] },
      options: Object.assign({}, D, { scales: scales({ y: Object.assign({}, D.scales.y, { max: 100, title: { display: true, text: 'Utilization %', color: T.dim } }) }) }),
    });
  }
}
onThemeChange(function() { if (_benchData && _benchData.length) renderBenchCharts(_benchData); });
loadBenchmarks();

// --- Runner ---
var benchMode = 'default', benchDuration = 300, progressPoll = null;

async function showBenchDialog() {
  benchMode = 'default'; benchDuration = 300;
  var hasLlm = false, hasEmbed = false, hasImage = false;
  try {
    var status = await (await fetch('/fleet/status')).json();
    var embedPatterns = ['embed', 'nomic', 'bge', 'e5-'];
    (status.nodes || []).forEach(function(node) {
      ((node.ollama || {}).models_loaded || []).forEach(function(m) {
        var lower = m.name.toLowerCase();
        if (embedPatterns.some(function(p) { return lower.indexOf(p) !== -1; })) hasEmbed = true; else hasLlm = true;
      });
    });
    var imgResp = await fetch('/api/image-models');
    if (imgResp.ok && ((await imgResp.json()).models || []).length) hasImage = true;
  } catch (e) { hasLlm = true; }

  var overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'bench-overlay';
  overlay.onclick = function(e) { if (e.target.id === 'bench-overlay') closeBenchDialog(); };
  overlay.innerHTML =
    '<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="bench-dialog-title">' +
      '<h3 id="bench-dialog-title">Run a fleet benchmark</h3>' +
      '<div class="mode-option selected" id="mode-default" onclick="selectMode(\\'default\\')"><h4>Loaded models</h4><p>Benchmark the models already in memory. Quick and non-disruptive.</p></div>' +
      '<div class="mode-option" id="mode-smart" onclick="selectMode(\\'smart\\')"><h4>Smart</h4><p>Fill free memory with recommended models, then benchmark everything.</p></div>' +
      '<div class="dialog-row"><label>Types</label>' +
        '<label class="type-check' + (hasLlm ? '' : ' disabled') + '"><input type="checkbox" id="type-llm" ' + (hasLlm ? 'checked' : 'disabled') + '> Chat</label>' +
        '<label class="type-check' + (hasEmbed ? '' : ' disabled') + '"><input type="checkbox" id="type-embed" ' + (hasEmbed ? 'checked' : 'disabled') + '> Embeddings</label>' +
        '<label class="type-check' + (hasImage ? '' : ' disabled') + '"><input type="checkbox" id="type-image" ' + (hasImage ? 'checked' : 'disabled') + '> Images</label>' +
      '</div>' +
      '<div class="dialog-row"><label>Duration</label><div class="segmented" id="dur-control">' +
        '<button data-dur="60">1 min</button><button data-dur="300" class="active">5 min</button><button data-dur="600">10 min</button>' +
      '</div></div>' +
      '<div class="dialog-actions"><button class="btn" onclick="closeBenchDialog()">Cancel</button><button class="btn primary" onclick="startBenchmark()">Start</button></div>' +
    '</div>';
  document.body.appendChild(overlay);
  overlay.querySelectorAll('[data-dur]').forEach(function(b) {
    b.addEventListener('click', function() {
      benchDuration = parseInt(b.dataset.dur);
      overlay.querySelectorAll('[data-dur]').forEach(function(x) { x.classList.toggle('active', x === b); });
    });
  });
}
function closeBenchDialog() { var o = document.getElementById('bench-overlay'); if (o) o.remove(); }
function selectMode(mode) {
  benchMode = mode;
  document.getElementById('mode-default').classList.toggle('selected', mode === 'default');
  document.getElementById('mode-smart').classList.toggle('selected', mode === 'smart');
}
function setRunButton(running) {
  var btn = document.getElementById('run-bench-btn');
  btn.disabled = running;
  btn.textContent = running ? 'Running\\u2026' : 'Run benchmark';
}

async function startBenchmark() {
  var types = [];
  if (document.getElementById('type-llm') && document.getElementById('type-llm').checked) types.push('llm');
  if (document.getElementById('type-embed') && document.getElementById('type-embed').checked) types.push('embed');
  if (document.getElementById('type-image') && document.getElementById('type-image').checked) types.push('image');
  if (!types.length) types.push('llm');
  closeBenchDialog();
  setRunButton(true);
  try {
    await fetch('/dashboard/api/benchmarks/start', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: benchMode, duration: benchDuration, model_types: types }) });
  } catch (e) { setRunButton(false); return; }
  showProgress();
  progressPoll = setInterval(pollProgress, 2000);
}

function showProgress() {
  var div = document.getElementById('bench-progress');
  div.hidden = false;
  div.innerHTML =
    '<div class="section-head"><b id="prog-title">Starting benchmark\\u2026</b><button class="btn small" id="prog-cancel" onclick="cancelBenchmark()">Cancel</button></div>' +
    '<p class="note" id="prog-phase"></p>' +
    '<div id="prog-pull-section" hidden>' +
      '<div class="progress-label"><span id="prog-pull-label">Downloading\\u2026</span><span id="prog-pull-pct"></span></div>' +
      '<div class="progress"><div class="fill" id="prog-pull-fill"></div></div>' +
      '<div id="prog-pull-overall" hidden><div class="progress-label"><span>Overall</span><span id="prog-pull-overall-pct"></span></div>' +
      '<div class="progress"><div class="fill" id="prog-pull-overall-fill"></div></div></div>' +
    '</div>' +
    '<div class="progress" id="prog-bench" hidden><div class="fill" id="prog-fill"></div></div>' +
    '<div class="progress-stats"><span>Elapsed <b id="prog-elapsed">0s</b></span><span>Requests <b id="prog-reqs">0</b></span><span>Throughput <b id="prog-toks">0</b> tok/s</span></div>';
}

function setFill(id, pct) { var el = document.getElementById(id); if (el) el.style.width = Math.max(0, Math.min(100, pct)) + '%'; }

async function pollProgress() {
  try {
    var p = await (await fetch('/dashboard/api/benchmarks/progress')).json();
    var phase = document.getElementById('prog-phase');
    if (!phase) return;
    var title = document.getElementById('prog-title');
    var pull = document.getElementById('prog-pull-section');
    var bench = document.getElementById('prog-bench');
    phase.textContent = p.phase || '';
    document.getElementById('prog-elapsed').textContent = fmtDuration(p.elapsed || 0);
    document.getElementById('prog-reqs').textContent = fmtNum(p.requests_completed || 0) + (p.requests_failed ? ' (' + p.requests_failed + ' failed)' : '');
    document.getElementById('prog-toks').textContent = fmtNum(p.tok_per_sec || 0);

    if (p.status === 'running' && p.duration > 0) {
      pull.hidden = true; bench.hidden = false;
      // Only ever moves forward: elapsed over duration, never an estimate.
      setFill('prog-fill', (p.elapsed / p.duration) * 100);
      title.textContent = benchMode === 'smart' ? 'Smart benchmark running' : 'Benchmark running';
    } else if (p.status === 'pulling') {
      pull.hidden = false; bench.hidden = true;
      title.textContent = 'Pulling models';
      var pp = p.pull_progress || {};
      if (pp.model) {
        var action = pp.on_disk ? 'Loading' : 'Downloading';
        document.getElementById('prog-pull-label').textContent = action + ' ' + pp.model;
        var pctEl = document.getElementById('prog-pull-pct');
        if (!pp.on_disk && pp.pct >= 0 && pp.total_gb) { pctEl.textContent = pp.pct + '% (' + (pp.completed_gb || 0) + ' of ' + pp.total_gb + ' GB)'; setFill('prog-pull-fill', pp.pct); }
        else if (pp.on_disk) { pctEl.textContent = 'from disk'; setFill('prog-pull-fill', 100); }
        else if (pp.status) { pctEl.textContent = pp.status; setFill('prog-pull-fill', 0); }
        var overall = document.getElementById('prog-pull-overall');
        overall.hidden = !(pp.total > 1);
        if (pp.total > 1) {
          document.getElementById('prog-pull-overall-pct').textContent = (pp.current - 1) + ' of ' + pp.total + ' models';
          setFill('prog-pull-overall-fill', ((pp.current - 1) / pp.total) * 100);
        }
        phase.textContent = action + ' model ' + pp.current + ' of ' + pp.total;
      }
    } else if (p.status === 'warming_up') {
      pull.hidden = true; bench.hidden = true;
      title.textContent = 'Warming up models';
    } else if (p.status === 'complete' || p.status === 'error' || p.status === 'cancelled') {
      clearInterval(progressPoll); progressPoll = null;
      setRunButton(false);
      document.getElementById('prog-cancel').hidden = true;
      pull.hidden = true;
      if (p.status === 'complete') {
        bench.hidden = false; bench.className = 'progress done'; setFill('prog-fill', 100);
        title.textContent = 'Benchmark complete';
        phase.textContent = fmtNum(p.requests_completed) + ' requests, ' + fmtNum(p.tok_per_sec) + ' tok/s' + (p.models_pulled && p.models_pulled.length ? ', ' + p.models_pulled.length + ' models pulled' : '');
        loadBenchmarks();
        setTimeout(function() { document.getElementById('bench-progress').hidden = true; }, 8000);
      } else if (p.status === 'error') {
        bench.hidden = false; bench.className = 'progress error'; setFill('prog-fill', 100);
        title.textContent = 'Benchmark failed';
        phase.textContent = p.error || 'Unknown error';
      } else {
        title.textContent = 'Benchmark cancelled';
        setTimeout(function() { document.getElementById('bench-progress').hidden = true; }, 3000);
      }
    }
  } catch (e) { /* the next poll retries */ }
}

async function cancelBenchmark() {
  try { await fetch('/dashboard/api/benchmarks/cancel', { method: 'POST' }); } catch (e) {}
}

(async function checkRunningBenchmark() {
  try {
    var p = await (await fetch('/dashboard/api/benchmarks/progress')).json();
    if (p.status && ['pulling', 'warming_up', 'running'].indexOf(p.status) !== -1) {
      setRunButton(true); showProgress();
      progressPoll = setInterval(pollProgress, 2000);
      pollProgress();
    }
  } catch (e) {}
})();
</script>
"""

# ---------------------------------------------------------------------------
# Health page body
# ---------------------------------------------------------------------------

_HEALTH_BODY = """
<style>
.health-head { display: flex; align-items: center; gap: 20px; }
.score-ring {
  --pct: 0%; --ring: var(--ok);
  width: 76px; height: 76px; border-radius: 50%; flex-shrink: 0; position: relative;
  display: flex; align-items: center; justify-content: center;
  background: conic-gradient(var(--ring) var(--pct), var(--surface-2) var(--pct));
  transition: --pct 0.8s ease;
}
.score-ring::before { content: ''; position: absolute; inset: 5px; border-radius: 50%; background: var(--surface-0); }
.score-ring span { position: relative; font-size: 24px; font-weight: 600; }
.score-ring.warn { --ring: var(--warn); }
.score-ring.fail { --ring: var(--fail); }
@property --pct { syntax: '<percentage>'; inherits: false; initial-value: 0%; }
.finding { display: flex; gap: 14px; align-items: flex-start; padding: 16px 18px; border-top: 1px solid var(--hairline); }
.finding:first-child { border-top: none; }
.finding .status-dot { margin-top: 6px; }
.finding-body { flex: 1; min-width: 0; }
.finding-title { font-weight: 600; }
.finding-meta { font-size: var(--fs-label); color: var(--text-2); margin-left: 6px; font-weight: 400; }
.finding-desc { font-size: var(--fs-small); color: var(--text-2); line-height: 1.55; margin-top: 4px; max-width: 80ch; }
.finding-fix { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: var(--fs-label); line-height: 1.55; margin-top: 10px; white-space: pre-wrap; word-break: break-word; }
.finding.resolved { opacity: 0.65; }
.history-entry { font-size: var(--fs-small); line-height: 1.7; max-width: 80ch; }
.history-meta { font-size: var(--fs-label); color: var(--text-2); margin-top: 6px; }
@media (max-width: 560px) { .health-head { align-items: flex-start; } }
</style>

<section class="health-head">
  <div class="score-ring" id="health-score"><span>—</span></div>
  <div><h1 class="page-title" id="health-title">Checking fleet health…</h1><p class="page-sub" id="health-subtitle">Nodes, traces and configuration</p></div>
</section>

<section class="section" id="findings">
  <div class="section-head"><h2 class="section-title">Findings</h2></div>
  <div id="recs-list"><div class="card empty-state">Loading…</div></div>
</section>

<section class="section" id="vitals">
  <div class="section-head"><h2 class="section-title">Fleet vitals, last 24 h</h2></div>
  <div class="figures" id="vitals-grid"></div>
</section>

<script>
function finding(r) {
  var resolved = r.data && r.data.resolved;
  return '<div class="finding' + (resolved ? ' resolved' : '') + '">' +
    dot(resolved ? 'ok' : r.severity) +
    '<div class="finding-body">' +
      '<div class="finding-title">' + escapeHtml(r.title) + '<span class="finding-meta">' + (resolved ? 'resolved' : r.severity) + (r.node_id ? ' \\u00b7 ' + escapeHtml(r.node_id) : '') + '</span></div>' +
      '<div class="finding-desc">' + escapeHtml(r.description) + '</div>' +
      (r.fix ? '<div class="finding-fix inset">' + escapeHtml(r.fix) + '</div>' : '') +
    '</div></div>';
}

var _infoOpen = false;
function renderHealth(report) {
  var v = report.vitals;
  var recs = (report.recommendations || []).slice().sort(function(a, b) { return SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]; });
  var ring = document.getElementById('health-score');
  ring.className = 'score-ring ' + scoreClass(v.health_score);
  ring.querySelector('span').textContent = v.health_score;
  requestAnimationFrame(function() { ring.style.setProperty('--pct', v.health_score + '%'); });

  // Warnings and critical findings always render expanded; only suggestions
  // (INFO) and resolved items fold, so calm never hides a real problem.
  var urgent = recs.filter(function(r) { return r.severity !== 'info' && !(r.data && r.data.resolved); });
  var quiet = recs.filter(function(r) { return urgent.indexOf(r) === -1; });
  var title = { healthy: 'Fleet is healthy', degraded: 'Fleet needs attention', critical: 'Fleet has a critical problem' }[report.status] || 'Fleet health';
  document.getElementById('health-title').textContent = title;
  var crit = urgent.filter(function(r) { return r.severity === 'critical'; }).length, warn = urgent.length - crit;
  var parts = [];
  if (crit) parts.push(crit + ' critical');
  if (warn) parts.push(warn + (warn === 1 ? ' warning' : ' warnings'));
  if (quiet.length) parts.push(quiet.length + (quiet.length === 1 ? ' suggestion' : ' suggestions'));
  document.getElementById('health-subtitle').textContent = (parts.length ? parts.join(' \\u00b7 ') : 'No findings') + ' \\u00b7 checked ' + new Date((report.checked_at || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

  var rl = document.getElementById('recs-list');
  var html = '';
  if (urgent.length) html += '<div class="list">' + urgent.map(finding).join('') + '</div>';
  else html += '<div class="card empty-state"><h3>Nothing needs attention</h3>No warnings or critical findings.</div>';
  if (quiet.length) {
    html += '<details class="disclosure" id="quiet-findings"' + (_infoOpen ? ' open' : '') + '><summary>' + quiet.length + (quiet.length === 1 ? ' suggestion' : ' suggestions') + ', no action required</summary><div class="list">' + quiet.map(finding).join('') + '</div></details>';
  }
  rl.innerHTML = html;
  var qd = document.getElementById('quiet-findings');
  if (qd) qd.addEventListener('toggle', function() { _infoOpen = qd.open; });

  function fig(value, label, cls) { return '<div class="figure ' + (cls || '') + '"><span class="figure-value">' + value + '</span><span class="figure-label">' + label + '</span></div>'; }
  var err = v.overall_error_rate_pct;
  document.getElementById('vitals-grid').innerHTML =
    fig(v.nodes_online + '<span class="figure-unit">/ ' + v.nodes_total + '</span>', 'Nodes online', v.nodes_online < v.nodes_total ? 'warn' : '') +
    fig(fmtNum(v.total_requests_24h), 'Requests', v.total_requests_24h ? '' : 'zero') +
    fig(fmtPct(err), 'Error rate', err > 5 ? 'fail' : err > 1 ? 'warn' : err ? '' : 'zero') +
    fig(fmtMs(v.avg_ttft_ms), 'Time to first token', '') +
    fig(fmtNum(v.cold_loads_24h), 'Cold loads', v.cold_loads_24h > 5 ? 'warn' : v.cold_loads_24h ? '' : 'zero') +
    fig(fmtNum(v.total_retries_24h), 'Retries', v.total_retries_24h ? '' : 'zero') +
    fig(fmtNum(v.client_disconnects_24h || 0), 'Client disconnects', v.client_disconnects_24h ? '' : 'zero') +
    fig(fmtNum(v.incomplete_streams_24h || 0), 'Incomplete streams', v.incomplete_streams_24h ? 'fail' : 'zero') +
    fig(fmtNum(v.image_generations_24h || 0), 'Images', v.image_generations_24h ? '' : 'zero') +
    fig(fmtNum(v.transcriptions_24h || 0), 'Transcriptions', v.transcriptions_24h ? '' : 'zero');
}

async function loadHealth() {
  try { renderHealth(await (await fetch('/dashboard/api/health')).json()); }
  catch (err) { console.error('Health load error:', err); }
}
loadHealth();
setInterval(loadHealth, 15000);
</script>
"""

# Fleet Intelligence history closes the Health page, after the recommendations.
_BRIEFING_HISTORY_BODY = """
<section class="section" id="briefings">
  <div class="section-head"><h2 class="section-title">Fleet Intelligence history</h2><button class="btn small" id="briefing-generate">Generate new</button></div>
  <div id="briefing-history-list"><div class="card empty-state">Loading…</div></div>
</section>
<script>
async function loadBriefingHistory() {
  try {
    var data = await (await fetch('/dashboard/api/briefing/history')).json();
    var list = document.getElementById('briefing-history-list');
    if (!data.history || !data.history.length) {
      list.innerHTML = '<div class="card empty-state">No briefings yet. Generate one, or turn on Fleet Intelligence in Settings.</div>';
      return;
    }
    function entry(b) {
      return '<div class="history-entry">' + renderBriefing(b.briefing) + '</div><div class="history-meta">' + fmtDateTime(b.generated_at) + ' \\u00b7 ' + escapeHtml(b.model || '?') + '</div>';
    }
    var latest = data.history[0], older = data.history.slice(1);
    list.innerHTML = '<div class="card">' + entry(latest) + '</div>' +
      (older.length ? '<details class="disclosure"><summary>' + older.length + ' earlier ' + (older.length === 1 ? 'briefing' : 'briefings') + '</summary><div class="list">' +
        older.map(function(b) { return '<div class="list-row"><div>' + entry(b) + '</div></div>'; }).join('') + '</div></details>' : '');
  } catch (e) { /* retried on the next load */ }
}
document.getElementById('briefing-generate').addEventListener('click', async function(e) {
  var btn = e.currentTarget;
  btn.disabled = true; btn.textContent = 'Generating\\u2026';
  try { await fetch('/dashboard/api/briefing?refresh=1'); await loadBriefingHistory(); } catch (err) {}
  btn.disabled = false; btn.textContent = 'Generate new';
});
loadBriefingHistory();
</script>
"""


# ---------------------------------------------------------------------------
# Recommendations page body
# ---------------------------------------------------------------------------

_RECOMMENDATIONS_BODY = """
<style>
.rec-meta { display: flex; align-items: center; gap: 10px; }
.btn.loading { opacity: 0.5; pointer-events: none; }
.btn.loading svg { animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.coverage-row { display: flex; gap: 14px; flex-wrap: wrap; }
.node-plan { display: flex; flex-direction: column; gap: 14px; }
.node-plan-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
.node-plan-title { font-size: 16px; font-weight: 600; }
.node-bars { display: flex; flex-direction: column; gap: 6px; min-width: 240px; }
.node-ram-bar { display: grid; grid-template-columns: 160px auto; gap: 10px; align-items: center; }
.ram-bar { height: 6px; background: var(--surface-2); border-radius: 3px; overflow: hidden; }
.ram-bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
.ram-bar-label { font-size: var(--fs-label); color: var(--text-2); }
.model-rec { display: flex; align-items: center; gap: 14px; padding: 12px 16px; border-top: 1px solid var(--hairline); flex-wrap: wrap; }
.model-rec:first-child { border-top: none; }
.model-info { flex: 1; min-width: 200px; }
.model-name { font-weight: 600; }
.model-meta { font-size: var(--fs-label); color: var(--text-2); margin-top: 2px; }
.model-reason { font-size: var(--fs-label); color: var(--text-2); margin-top: 4px; line-height: 1.45; max-width: 80ch; }
.model-badges { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
.model-check { flex-shrink: 0; width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
.model-check:disabled { opacity: 0.5; cursor: default; }
.pin-btn { flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; border-radius: var(--radius-1); border: 1px solid var(--hairline); background: transparent; color: var(--text-2); cursor: pointer; padding: 0; }
.pin-btn:hover { color: var(--text-1); }
.pin-btn.pinned { color: var(--accent); border-color: var(--accent); }
.pin-btn.env-pinned { color: var(--accent); cursor: not-allowed; opacity: 0.7; }
.pin-btn.saving { opacity: 0.5; cursor: wait; }
.pin-btn svg { width: 14px; height: 14px; }
.model-rec.pulling .model-name { color: var(--accent); }
.pull-status { font-size: 11px; font-weight: 500; margin-left: 8px; }
.pull-status.pulling { color: var(--accent); }
.pull-status.done { color: var(--ok); }
.pull-status.error { color: var(--fail); }
.pull-cmd { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: var(--fs-label); cursor: pointer; word-break: break-all; display: flex; gap: 12px; justify-content: space-between; }
.pull-cmd .copy-hint { font-size: 11px; color: var(--text-2); flex-shrink: 0; font-family: inherit; }
.pull-actions { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.pull-progress, .delete-progress { font-size: var(--fs-label); color: var(--text-2); }
.mgmt-table td.status-col { white-space: nowrap; }
.mgmt-table code { font-size: 11px; color: var(--text-2); }
.delete-section { display: flex; align-items: center; gap: 10px; padding: 12px 14px; }
.delete-status { font-size: 11px; font-weight: 500; margin-left: 4px; }
.delete-status.deleting { color: var(--fail); }
.delete-status.deleted { color: var(--ok); }
.delete-status.error { color: var(--fail); }
.mgmt-check, .mgmt-check-all { width: 15px; height: 15px; accent-color: var(--accent); cursor: pointer; }
.dialog ul { font-size: var(--fs-small); margin: 8px 0; padding-left: 20px; }
.dialog .warn-text { color: var(--warn); }
@media (max-width: 768px) {
  .node-ram-bar { grid-template-columns: 120px auto; }
  .node-bars { min-width: 0; width: 100%; }
}
</style>

<section class="section" id="recommendations">
  <div class="section-head">
    <div><h2 class="page-title" id="rec-title">Model recommendations</h2><p class="page-sub" id="rec-subtitle">Evaluating hardware, usage patterns, and model benchmarks…</p></div>
    <div class="rec-meta" id="rec-meta" hidden>
      <span class="small muted" id="rec-timestamp"></span>
      <button class="btn small" id="refresh-btn" onclick="refreshRecommendations()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
        Refresh
      </button>
    </div>
  </div>
  <div id="usage-section" hidden>
    <div class="figures" id="usage-grid"></div>
    <div class="section-title subhead">Category coverage</div>
    <div class="coverage-row" id="coverage-row"></div>
  </div>
  <div class="section-title" id="nodes-label" hidden>Per node</div>
  <div id="node-plans"></div>
</section>

<script>
const CAT_LABELS = {
  'general': 'General',
  'coding': 'Coding',
  'reasoning': 'Reasoning',
  'creative': 'Creative',
  'fast-chat': 'Fast chat',
  'vision': 'Vision',
  'vision-embedding': 'Vision embedding',
  'image': 'Image'
};

var _lastGeneratedAt = 0;

function renderRecommendations(data) {
  const titleEl = document.getElementById('rec-title');
  const subtitleEl = document.getElementById('rec-subtitle');
  titleEl.textContent = 'Model recommendations';
  subtitleEl.textContent = data.fleet_summary || 'No nodes available';

  // Timestamp
  if (data.generated_at) {
    _lastGeneratedAt = data.generated_at;
    document.getElementById('rec-meta').hidden = false;
    updateTimestamp();
  }

  // Usage section
  const usageSection = document.getElementById('usage-section');
  const usage = data.usage || {};

  usageSection.hidden = false;
  function fig(v, l) { return '<div class="figure"><span class="figure-value">' + v + '</span><span class="figure-label">' + l + '</span></div>'; }
  var ugHtml = fig(fmtNum(usage.total_requests_24h || 0), 'Requests, 24 h');
  Object.entries(usage.category_breakdown || {}).sort(function(a, b) { return b[1] - a[1]; }).forEach(function(entry) {
    ugHtml += fig(fmtNum(entry[1]), escapeHtml(CAT_LABELS[entry[0]] || entry[0]));
  });
  document.getElementById('usage-grid').innerHTML = ugHtml;

  // Coverage badges
  const covRow = document.getElementById('coverage-row');
  const coverage = usage.category_coverage || {};
  covRow.innerHTML = Object.entries(coverage).map(function(entry) {
    var cat = entry[0], covered = entry[1];
    return statusDot(covered ? 'ok' : 'warn', (CAT_LABELS[cat] || cat) + (covered ? '' : ' \u2014 not covered'));
  }).join('');

  // Node plans
  var nodesLabel = document.getElementById('nodes-label');
  var plansEl = document.getElementById('node-plans');
  var nodes = data.nodes || [];

  if (nodes.length === 0) {
    nodesLabel.hidden = true;
    plansEl.innerHTML = '<div class="card empty-state"><h3>No nodes online</h3>Start a node agent to get model recommendations.</div>';
    return;
  }

  nodesLabel.hidden = false;
  plansEl.innerHTML = nodes.map(function(node) {
    var ramPct = node.usable_ram_gb > 0 ? Math.min(100, Math.round(node.total_recommended_ram_gb / node.usable_ram_gb * 100)) : 0;

    var diskPct = node.disk_total_gb > 0 ? Math.min(100, Math.round((node.disk_total_gb - node.disk_available_gb) / node.disk_total_gb * 100)) : 0;

    var html = '<div class="card node-plan">';
    html += '<div class="node-plan-header">';
    html += '<div class="node-plan-title">' + escapeHtml(node.node_id) + '</div>';
    html += '<div class="node-bars">';
    html += '<div class="node-ram-bar">';
    // RAM bar: product-value utilization (using more RAM = using the box the
    // user paid for).  Blue->purple, NOT a warning scale.
    html += '<div class="ram-bar"><div class="ram-bar-fill" style="width:' + ramPct + '%;background:' + utilizationColor(ramPct, 'mem') + '"></div></div>';
    html += '<div class="ram-bar-label">' + node.total_recommended_ram_gb + ' / ' + node.usable_ram_gb + ' GB RAM</div>';
    html += '</div>';
    if (node.disk_total_gb > 0) {
      html += '<div class="node-ram-bar">';
      // Disk bar: hard-ceiling capacity.  Full disk = model downloads fail,
      // SQLite writes stop.  Busy-IS-bad semantic correct; keep barColor.
      html += '<div class="ram-bar"><div class="ram-bar-fill" style="width:' + diskPct + '%;background:' + barColor(diskPct) + '"></div></div>';
      html += '<div class="ram-bar-label">' + node.disk_available_gb.toFixed(0) + ' / ' + node.disk_total_gb.toFixed(0) + ' GB Disk free</div>';
      html += '</div>';
    }
    html += '</div></div>';

    if (node.recommendations.length === 0) {
      html += '<div class="empty-state">No recommendations: not enough free memory.</div>';
    } else {
      html += '<div class="list">';
      node.recommendations.forEach(function(rec) {
        var recClass = 'model-rec';
        var cardId = 'card-' + node.node_id + '-' + rec.model.replace(/[^a-zA-Z0-9]/g, '-');
        html += '<div class="' + recClass + '" id="' + cardId + '">';

        // Checkbox for non-downloaded models.  Vision embeddings don't use
        // `ollama pull` — the node's embedding service (:11438) fetches them
        // from HuggingFace on the first /api/embed-image request, so we
        // exclude them from the pull flow and label the card accordingly.
        var isVisionEmbedding = rec.category === 'vision-embedding';
        if (rec.already_available) {
          html += '<input type="checkbox" class="model-check" checked disabled title="Already downloaded">';
        } else if (isVisionEmbedding) {
          html += '<input type="checkbox" class="model-check" checked disabled title="Auto-installs on first /api/embed-image request (not an Ollama model)">';
        } else {
          html += '<input type="checkbox" class="model-check" checked data-node="' + node.node_id + '" data-model="' + rec.model + '">';
        }

        html += '<div class="model-info">';
        html += '<div class="model-name">' + rec.display_name + '<span class="pull-status" id="status-' + cardId + '"></span></div>';
        html += '<div class="model-meta"><code>' + escapeHtml(rec.model) + '</code> &middot; ' + rec.ram_gb + ' GB &middot; ' + escapeHtml(CAT_LABELS[rec.category] || rec.category) + (rec.quality_score > 0 ? ' &middot; quality ' + rec.quality_score : '') + '</div>';
        html += '<div class="model-reason">' + rec.reason + '</div>';
        html += '</div>';
        html += '<div class="model-badges">';
        html += '<span class="tag">' + escapeHtml(rec.priority) + ' priority</span>';
        if (rec.already_available) { html += statusDot('ok', 'downloaded'); }
        else if (isVisionEmbedding) { html += '<span class="tag" title="Downloads from HuggingFace on first use; no manual pull needed">installs on first use</span>'; }
        var pinState = isPinned(node.node_id, rec.model);
        if (pinState === 'env') { html += '<span class="tag" title="Pinned via FLEET_PINNED_MODELS">env pin</span>'; }
        html += '</div>';
        // Pin button (env pins disabled — managed via env var)
        var PIN_SVG_FILLED = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z"/></svg>';
        var PIN_SVG_OUTLINE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z"/></svg>';
        // Vision embeddings don't route through the Ollama preloader —
        // the embedding service keeps them resident on its own, so a pin
        // here would be a lie.  Skip the pin button for that category.
        if (isVisionEmbedding) {
          // no pin button — auto-managed by embedding service
        } else if (pinState === 'env') {
          html += '<button class="pin-btn env-pinned" disabled title="Pinned fleet-wide via FLEET_PINNED_MODELS">' + PIN_SVG_FILLED + '</button>';
        } else if (rec.already_available) {
          var pinned = pinState === 'node';
          html += '<button class="pin-btn' + (pinned ? ' pinned' : '') + '" data-pin-node="' + node.node_id + '" data-pin-model="' + rec.model + '" title="' + (pinned ? 'Unpin from ' : 'Pin on ') + node.node_id + '">';
          html += pinned ? PIN_SVG_FILLED : PIN_SVG_OUTLINE;
          html += '</button>';
        }
        html += '</div>';
      });
      html += '</div>';

      // Pull section for models not yet downloaded.  Vision embeddings
      // auto-install on first use, so they're excluded from `ollama pull`.
      var toPull = node.recommendations.filter(function(r) {
        return !r.already_available && r.category !== 'vision-embedding';
      });
      if (toPull.length > 0) {
        html += '<div class="pull-section" id="pull-section-' + node.node_id + '">';
        var cmds = toPull.map(function(r) { return 'ollama pull ' + r.model; }).join(' && ');
        html += '<div class="pull-cmd inset" data-cmd="' + cmds + '" id="pull-cmd-' + node.node_id + '"><span>' + cmds + '</span><span class="copy-hint">click to copy</span></div>';
        html += '<div class="pull-actions">';
        html += '<button class="btn primary pull-btn" id="pull-btn-' + node.node_id + '" data-node="' + node.node_id + '">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
        html += 'Pull ' + toPull.length + ' Model' + (toPull.length > 1 ? 's' : '') + '</button>';
        html += '<span class="pull-progress" id="pull-progress-' + node.node_id + '"></span>';
        html += '</div></div>';
      }
    }

    // Model Management placeholder (populated by loadModelManagement)
    html += '<details class="disclosure" id="mgmt-' + node.node_id + '"><summary>Models on disk <span id="mgmt-summary-' + node.node_id + '">\u2026</span></summary>';
    html += '<div class="card flush table-scroll" id="mgmt-table-' + node.node_id + '"></div>';
    html += '</details>';

    html += '</div>';
    return html;
  }).join('');
}

function getSelectedModels(nodeId) {
  var checks = document.querySelectorAll('.model-check[data-node="' + nodeId + '"]:checked');
  var models = [];
  checks.forEach(function(cb) { models.push(cb.dataset.model); });
  return models;
}

function updatePullSection(nodeId) {
  var models = getSelectedModels(nodeId);
  var cmdEl = document.getElementById('pull-cmd-' + nodeId);
  var btnEl = document.getElementById('pull-btn-' + nodeId);
  var sectionEl = document.getElementById('pull-section-' + nodeId);
  if (!sectionEl) return;

  if (models.length === 0) {
    sectionEl.hidden = true;
    return;
  }
  sectionEl.hidden = false;

  if (cmdEl) {
    var cmds = models.map(function(m) { return 'ollama pull ' + m; }).join(' && ');
    cmdEl.dataset.cmd = cmds;
    cmdEl.innerHTML = '<span>' + cmds + '</span><span class="copy-hint">click to copy</span>';
  }
  if (btnEl && !btnEl.disabled) {
    btnEl.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>' +
      'Pull ' + models.length + ' Model' + (models.length > 1 ? 's' : '');
  }
}

async function pullSelected(nodeId) {
  var models = getSelectedModels(nodeId);
  if (models.length === 0) return;

  var btn = document.getElementById('pull-btn-' + nodeId);
  var progress = document.getElementById('pull-progress-' + nodeId);
  btn.disabled = true;

  // Disable checkboxes during pull
  document.querySelectorAll('.model-check[data-node="' + nodeId + '"]').forEach(function(cb) { cb.disabled = true; });

  var done = 0;
  var failed = 0;

  for (var i = 0; i < models.length; i++) {
    var model = models[i];
    var cardId = 'card-' + nodeId + '-' + model.replace(/[^a-zA-Z0-9]/g, '-');
    var cardEl = document.getElementById(cardId);
    var statusEl = document.getElementById('status-' + cardId);

    if (cardEl) cardEl.classList.add('pulling');
    if (statusEl) { statusEl.className = 'pull-status pulling'; statusEl.textContent = 'pulling...'; }
    if (progress) progress.textContent = 'Pulling ' + (i + 1) + ' of ' + models.length + '...';

    try {
      var resp = await fetch('/dashboard/api/pull', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({node_id: nodeId, model: model})
      });
      var result = await resp.json();

      if (cardEl) cardEl.classList.remove('pulling');
      if (result.ok) {
        done++;
        if (cardEl) cardEl.classList.add('pulled');
        if (statusEl) { statusEl.className = 'pull-status done'; statusEl.textContent = 'pulled!'; }
      } else {
        failed++;
        if (cardEl) cardEl.classList.add('pull-error');
        if (statusEl) { statusEl.className = 'pull-status error'; statusEl.textContent = 'failed'; }
      }
    } catch (err) {
      failed++;
      if (cardEl) { cardEl.classList.remove('pulling'); cardEl.classList.add('pull-error'); }
      if (statusEl) { statusEl.className = 'pull-status error'; statusEl.textContent = 'error'; }
    }
  }

  if (progress) {
    progress.textContent = done + ' pulled' + (failed > 0 ? ', ' + failed + ' failed' : '') + '.';
  }
  btn.disabled = false;

  // Re-enable checkboxes
  document.querySelectorAll('.model-check[data-node="' + nodeId + '"]').forEach(function(cb) { cb.disabled = false; });

  // Refresh recommendations to update available status
  if (done > 0) {
    setTimeout(function() { loadRecommendations(true); }, 2000);
  }
}

var _pinnedState = { env_pins: [], per_node: {}, max_count: 3 };

async function loadPinnedState() {
  try {
    const resp = await fetch('/dashboard/api/pinned-models');
    _pinnedState = await resp.json();
  } catch (err) {
    console.error('Pinned models load error:', err);
  }
}

function isPinned(nodeId, model) {
  if ((_pinnedState.env_pins || []).indexOf(model) !== -1) return 'env';
  var nodePins = (_pinnedState.per_node || {})[nodeId] || [];
  if (nodePins.indexOf(model) !== -1) return 'node';
  return null;
}

async function togglePin(btn) {
  if (btn.classList.contains('env-pinned') || btn.classList.contains('saving')) return;
  var nodeId = btn.dataset.pinNode;
  var model = btn.dataset.pinModel;
  if (!nodeId || !model) return;
  var currentlyPinned = btn.classList.contains('pinned');
  btn.classList.add('saving');
  try {
    const resp = await fetch('/dashboard/api/pinned-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_id: nodeId, model: model, pinned: !currentlyPinned }),
    });
    const data = await resp.json();
    if (data.ok) {
      _pinnedState.per_node = data.per_node;
      btn.classList.toggle('pinned', !currentlyPinned);
      btn.title = !currentlyPinned ? 'Unpin from ' + nodeId : 'Pin on ' + nodeId;
      btn.innerHTML = !currentlyPinned
        ? '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z"/></svg>';
    }
  } catch (err) {
    console.error('Pin toggle error:', err);
  } finally {
    btn.classList.remove('saving');
  }
}

async function loadRecommendations(forceRefresh) {
  try {
    var url = '/dashboard/api/recommendations';
    if (forceRefresh) url += '?refresh=1';
    const [recResp] = await Promise.all([fetch(url), loadPinnedState()]);
    const data = await recResp.json();
    renderRecommendations(data);
  } catch (err) {
    console.error('Recommendations load error:', err);
    document.getElementById('rec-title').textContent = 'Error loading recommendations';
  }
}

function refreshRecommendations() {
  var btn = document.getElementById('refresh-btn');
  btn.classList.add('loading');
  loadRecommendations(true).finally(function() {
    btn.classList.remove('loading');
  });
}

function updateTimestamp() {
  var el = document.getElementById('rec-timestamp');
  if (el && _lastGeneratedAt) {
    el.textContent = 'Last analyzed ' + timeAgo(_lastGeneratedAt);
  }
}

// ---------------------------------------------------------------------------
// Model Management
// ---------------------------------------------------------------------------
var _mgmtData = {};

async function loadModelManagement() {
  try {
    var resp = await fetch('/dashboard/api/model-management');
    var data = await resp.json();
    (data.nodes || []).forEach(function(nodeData) {
      _mgmtData[nodeData.node_id] = nodeData;
      renderModelManagement(nodeData);
    });
  } catch (err) {
    console.error('Model management load error:', err);
  }
}

function renderModelManagement(nodeData) {
  var tableEl = document.getElementById('mgmt-table-' + nodeData.node_id);
  var summaryEl = document.getElementById('mgmt-summary-' + nodeData.node_id);
  if (!tableEl) return;

  var models = nodeData.models || [];
  var unusedCount = models.filter(function(m) { return m.unused; }).length;

  summaryEl.textContent = '\u00b7 ' + models.length + ' model' + (models.length !== 1 ? 's' : '') +
    ', ' + nodeData.total_size_gb + ' GB' +
    (unusedCount > 0 ? ' \u00b7 ' + unusedCount + ' unused' : '');

  if (models.length === 0) {
    tableEl.innerHTML = '<div class="empty-state">No models on this node</div>';
    return;
  }

  var html = '<table class="data-table mgmt-table">';
  html += '<thead><tr>';
  html += '<th><input type="checkbox" class="mgmt-check-all" data-node="' + nodeData.node_id + '" aria-label="Select all"></th>';
  html += '<th>Model</th><th>Category</th><th class="num">Size</th>';
  html += '<th class="num">Requests</th><th class="num">Last used</th>';
  html += '<th>Status</th>';
  html += '</tr></thead><tbody>';

  models.forEach(function(m) {
    var safeId = m.name.replace(/[^a-zA-Z0-9]/g, '-');
    html += '<tr>';
    html += '<td><input type="checkbox" class="mgmt-check" data-node="' + nodeData.node_id + '" data-model="' + m.name + '"';
    if (m.loaded_in_vram) html += ' data-loaded="1"';
    html += '></td>';
    html += '<td>' + escapeHtml(m.display_name) + '<br><code>' + escapeHtml(m.name) + '</code></td>';
    html += '<td class="muted">' + escapeHtml(CAT_LABELS[m.category] || m.category) + '</td>';
    html += '<td class="num">' + m.size_gb.toFixed(1) + ' GB</td>';
    html += '<td class="num">' + fmtNum(m.total_requests || 0) + '</td>';
    html += '<td class="num">';
    if (m.last_used) {
      html += timeAgo(m.last_used);
    } else {
      html += '<span class="faint">never</span>';
    }
    html += '</td>';
    html += '<td class="status-col">';
    if (m.loaded_in_vram) html += statusDot('ok', 'loaded') + ' ';
    if (m.unused && !m.last_used) {
      html += '<span class="tag">never used</span>';
    } else if (m.unused) {
      html += '<span class="tag">unused 7d+</span>';
    }
    html += '<span class="delete-status" id="del-' + nodeData.node_id + '-' + safeId + '"></span>';
    html += '</td>';
    html += '</tr>';
  });

  html += '</tbody></table>';
  html += '<div class="delete-section" id="delete-section-' + nodeData.node_id + '" hidden>';
  html += '<button class="btn danger delete-btn" id="delete-btn-' + nodeData.node_id + '" data-node="' + nodeData.node_id + '">';
  html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
  html += 'Delete Selected</button>';
  html += '<span class="delete-progress" id="delete-progress-' + nodeData.node_id + '"></span>';
  html += '</div>';

  tableEl.innerHTML = html;
}

function getSelectedDeleteModels(nodeId) {
  var checks = document.querySelectorAll('.mgmt-check[data-node="' + nodeId + '"]:checked');
  var models = [];
  checks.forEach(function(cb) { models.push({ name: cb.dataset.model, loaded: cb.dataset.loaded === '1' }); });
  return models;
}

function updateDeleteSection(nodeId) {
  var selected = getSelectedDeleteModels(nodeId);
  var section = document.getElementById('delete-section-' + nodeId);
  if (!section) return;
  section.hidden = selected.length === 0;
  var btn = document.getElementById('delete-btn-' + nodeId);
  if (btn && !btn.disabled) {
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>' +
      'Delete ' + selected.length + ' Model' + (selected.length > 1 ? 's' : '');
  }
}

function showDeleteConfirmation(nodeId) {
  var selected = getSelectedDeleteModels(nodeId);
  if (selected.length === 0) return;

  var loadedModels = selected.filter(function(m) { return m.loaded; });
  var hasLoaded = loadedModels.length > 0;

  var html = '<div class="overlay" id="confirm-overlay">';
  html += '<div class="dialog" role="dialog" aria-modal="true">';
  html += '<h3>Delete ' + selected.length + ' model' + (selected.length > 1 ? 's' : '') + '?</h3>';
  html += '<p>These models will be permanently removed from <strong>' + nodeId + '</strong>:</p>';
  html += '<ul>';
  selected.forEach(function(m) {
    html += '<li>' + m.name + (m.loaded ? ' (loaded in VRAM)' : '') + '</li>';
  });
  html += '</ul>';
  if (hasLoaded) {
    html += '<p class="warn-text">' +
      loadedModels.length + ' model' + (loadedModels.length > 1 ? 's are' : ' is') +
      ' currently loaded in VRAM. Deleting will unload ' + (loadedModels.length > 1 ? 'them' : 'it') +
      ' and may interrupt active requests.</p>';
  }
  html += '<p>Models will need to be re-downloaded to use again.</p>';
  html += '<div class="dialog-actions">';
  html += '<button class="btn" id="confirm-cancel">Cancel</button>';
  html += '<button class="btn danger" id="confirm-proceed" data-node="' + nodeId + '">Delete</button>';
  html += '</div></div></div>';

  document.body.insertAdjacentHTML('beforeend', html);
}

async function executeDelete(nodeId) {
  var selected = getSelectedDeleteModels(nodeId);
  if (selected.length === 0) return;

  var btn = document.getElementById('delete-btn-' + nodeId);
  var progress = document.getElementById('delete-progress-' + nodeId);
  if (btn) btn.disabled = true;

  document.querySelectorAll('.mgmt-check[data-node="' + nodeId + '"]').forEach(function(cb) { cb.disabled = true; });

  var done = 0;
  var failed = 0;

  for (var i = 0; i < selected.length; i++) {
    var model = selected[i].name;
    var safeId = model.replace(/[^a-zA-Z0-9]/g, '-');
    var statusEl = document.getElementById('del-' + nodeId + '-' + safeId);

    if (statusEl) { statusEl.className = 'delete-status deleting'; statusEl.textContent = 'deleting...'; }
    if (progress) progress.textContent = 'Deleting ' + (i + 1) + ' of ' + selected.length + '...';

    try {
      var resp = await fetch('/dashboard/api/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({node_id: nodeId, model: model})
      });
      var result = await resp.json();
      if (result.ok) {
        done++;
        if (statusEl) { statusEl.className = 'delete-status deleted'; statusEl.textContent = 'deleted'; }
      } else {
        failed++;
        if (statusEl) { statusEl.className = 'delete-status error'; statusEl.textContent = 'failed'; }
      }
    } catch (err) {
      failed++;
      if (statusEl) { statusEl.className = 'delete-status error'; statusEl.textContent = 'error'; }
    }
  }

  if (progress) {
    progress.textContent = done + ' deleted' + (failed > 0 ? ', ' + failed + ' failed' : '') + '.';
  }
  if (btn) btn.disabled = false;
  document.querySelectorAll('.mgmt-check[data-node="' + nodeId + '"]').forEach(function(cb) { cb.disabled = false; });

  // Refresh data after deletes
  if (done > 0) {
    setTimeout(function() {
      loadRecommendations(true);
      loadModelManagement();
    }, 2000);
  }
}


document.addEventListener('click', function(e) {
  // Pull command copy
  var el = e.target.closest('.pull-cmd');
  if (el && el.dataset.cmd) {
    navigator.clipboard.writeText(el.dataset.cmd).then(function() {
      var hint = el.querySelector('.copy-hint');
      if (hint) { hint.textContent = 'copied!'; setTimeout(function() { hint.textContent = 'click to copy'; }, 2000); }
    });
    return;
  }
  // Pull button
  var btn = e.target.closest('.pull-btn');
  if (btn && btn.dataset.node) {
    pullSelected(btn.dataset.node);
    return;
  }
  // Pin button
  var pinBtn = e.target.closest('.pin-btn');
  if (pinBtn && pinBtn.dataset.pinNode) {
    togglePin(pinBtn);
    return;
  }
  // Delete button -> show confirmation
  var delBtn = e.target.closest('.delete-btn');
  if (delBtn && delBtn.dataset.node) {
    showDeleteConfirmation(delBtn.dataset.node);
    return;
  }
  // Confirm cancel
  if (e.target.id === 'confirm-cancel') {
    var overlay = document.getElementById('confirm-overlay');
    if (overlay) overlay.remove();
    return;
  }
  // Confirm overlay background click
  if (e.target.id === 'confirm-overlay') {
    e.target.remove();
    return;
  }
  // Confirm proceed
  var proceedBtn = e.target.closest('#confirm-proceed');
  if (proceedBtn) {
    var overlay = document.getElementById('confirm-overlay');
    if (overlay) overlay.remove();
    executeDelete(proceedBtn.dataset.node);
    return;
  }
});

document.addEventListener('change', function(e) {
  // Pull checkboxes
  if (e.target.classList.contains('model-check') && e.target.dataset.node) {
    updatePullSection(e.target.dataset.node);
  }
  // Model management checkboxes
  if (e.target.classList.contains('mgmt-check') && e.target.dataset.node) {
    updateDeleteSection(e.target.dataset.node);
  }
  // Select-all for management
  if (e.target.classList.contains('mgmt-check-all') && e.target.dataset.node) {
    var nodeId = e.target.dataset.node;
    var checked = e.target.checked;
    document.querySelectorAll('.mgmt-check[data-node="' + nodeId + '"]').forEach(function(cb) {
      cb.checked = checked;
    });
    updateDeleteSection(nodeId);
  }
});

loadRecommendations();
loadModelManagement();
// Keep the relative timestamp fresh
setInterval(updateTimestamp, 30000);
</script>
"""


# ---------------------------------------------------------------------------
# Settings page body
# ---------------------------------------------------------------------------


def _color_states_body() -> str:
    """The token reference: every colour token in both appearances, then the
    utilization bars in every warning state.  Built from ``_TOKENS`` and the live
    CSS and JS, so it cannot drift from what the dashboard renders."""
    groups = (
        (
            "Surfaces and text",
            ("surface-0", "surface-1", "surface-2", "hairline", "text-1", "text-2", "text-3"),
        ),
        ("Interaction", ("accent", "accent-fill", "accent-ink")),
        ("Status: state only, never decoration", ("ok", "warn", "fail", "info")),
        ("Chart series", tuple(f"series-{i}" for i in range(1, 7))),
    )

    def panel(theme: str) -> str:
        rows = []
        for title, names in groups:
            cells = "".join(
                f'<div class="swatch-row"><span class="swatch-chip" style="background:var(--{n})"></span>'
                f'<code>--{n}</code><span class="faint">{_TOKENS[theme][n]}</span></div>'
                for n in names
            )
            rows.append(f'<div class="swatch-group"><h5>{title}</h5>{cells}</div>')
        sample = (
            '<div class="swatch-sample"><span class="status"><span class="status-dot ok"></span>Online</span>'
            '<span class="status"><span class="status-dot warn"></span>Degraded</span>'
            '<span class="status"><span class="status-dot fail"></span>Offline</span>'
            '<span class="tag">neutral tag</span><button class="btn primary small">Primary</button>'
            '<button class="btn small">Secondary</button></div>'
        )
        return f'<div class="theme-{theme} swatch-panel"><h4>{theme.title()}</h4>{sample}{"".join(rows)}</div>'

    return (
        """
<style>
.swatch-panels { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.swatch-panel { background: var(--surface-0); color: var(--text-1); border: 1px solid var(--hairline); border-radius: var(--radius-3); padding: 18px; display: flex; flex-direction: column; gap: 14px; }
.swatch-panel h4 { font-size: var(--fs-body); font-weight: 600; }
.swatch-group h5 { font-size: var(--fs-label); font-weight: 600; color: var(--text-2); margin-bottom: 6px; }
.swatch-row { display: grid; grid-template-columns: 22px 1fr auto; gap: 10px; align-items: center; font-size: var(--fs-label); padding: 3px 0; }
.swatch-chip { width: 22px; height: 22px; border-radius: var(--radius-1); border: 1px solid var(--hairline); }
.swatch-sample { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--radius-2); padding: 10px 12px; }
.color-demo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.color-demo-scale { display: grid; grid-template-columns: 44px 1fr; gap: 8px; align-items: center; font-size: 11px; margin-top: 4px; }
.color-demo-scale .pct { color: var(--text-2); text-align: right; }
@media (max-width: 768px) { .swatch-panels { grid-template-columns: 1fr; } }
</style>
<div>
  <h1 class="page-title">Colour reference</h1>
  <p class="page-sub">Developer reference for <code>docs/plans/dashboard-calm-redesign.md</code> and
  <code>docs/plans/dashboard-color-semantics.md</code>. Generated from the live tokens, CSS and JS.</p>
</div>
<section class="section"><div class="section-head"><h2 class="section-title">Tokens in both appearances</h2></div>
<div class="swatch-panels">"""
        + panel("dark")
        + panel("light")
        + """</div></section>
<section class="section"><div class="section-head"><h2 class="section-title">Warning outlines, memory bar at 75%</h2></div>
<div class="color-demo-grid" id="state-cards"></div></section>
<section class="section"><div class="section-head"><h2 class="section-title">Utilization colour sweep</h2></div>
<div class="color-demo-grid" id="gradient-cards"></div></section>
<script>
(function() {
  // Same utilization, different warning classes: busy is never red; a
  // warning is an outline that appears independently of the fill.
  var states = [
    { name: 'Normal', klass: '', label: 'pressure normal: just the utilization fill' },
    { name: 'Memory warning', klass: 'bar-warning', label: 'pressure warn: amber outline' },
    { name: 'Memory critical', klass: 'bar-critical', label: 'pressure critical: red outline' },
    { name: 'CPU thermal', klass: 'bar-thermal', label: 'sustained CPU at or above 95%: amber outline' },
  ];
  document.getElementById('state-cards').innerHTML = states.map(function(s) {
    return '<div class="card"><b>' + s.name + '</b><p class="note">' + s.label + '</p>' +
      '<div class="small muted subhead">Memory \u00b7 384 GB of 512 GB</div>' +
      '<div class="bar-container ' + s.klass + '"><div class="bar-fill" style="width:75%;background:' + utilizationColor(75, 'mem') + '"></div></div></div>';
  }).join('');
  var pcts = [5, 15, 30, 50, 70, 85, 95, 100];
  document.getElementById('gradient-cards').innerHTML = ['cpu', 'mem'].map(function(metric) {
    return '<div class="card"><b>' + (metric === 'cpu' ? 'CPU' : 'Memory') + '</b><p class="note">utilizationColor(pct, "' + metric + '")</p>' +
      pcts.map(function(p) {
        return '<div class="color-demo-scale"><div class="pct">' + p + '%</div><div class="bar-container"><div class="bar-fill" style="width:' + p + '%;background:' + utilizationColor(p, metric) + '"></div></div></div>';
      }).join('') + '</div>';
  }).join('');
})();
</script>
"""
    )


_COLOR_STATES_BODY = _color_states_body()


_SETTINGS_BODY = """
<style>
.toggle-row { display: flex; align-items: center; gap: 16px; padding: 14px 18px; border-top: 1px solid var(--hairline); }
.toggle-row:first-child { border-top: none; }
.toggle-info { flex: 1; min-width: 0; }
.toggle-label { font-weight: 500; }
.toggle-desc { font-size: var(--fs-label); color: var(--text-2); margin-top: 2px; line-height: 1.45; }
.toggle-env { font-size: 11px; color: var(--text-3); font-family: 'SF Mono', ui-monospace, Menlo, monospace; margin-top: 4px; }
.toggle-switch { position: relative; width: 42px; height: 24px; cursor: pointer; flex-shrink: 0; }
.toggle-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
.toggle-slider { position: absolute; inset: 0; background: var(--surface-2); border: 1px solid var(--hairline); border-radius: 12px; transition: background 0.2s; }
.toggle-slider::before { content: ''; position: absolute; height: 18px; width: 18px; left: 2px; top: 2px; background: var(--text-2); border-radius: 50%; transition: transform 0.2s, background 0.2s; }
.toggle-switch input:checked + .toggle-slider { background: var(--accent-fill); border-color: transparent; }
.toggle-switch input:checked + .toggle-slider::before { transform: translateX(18px); background: #fff; }
.toggle-switch input:focus-visible + .toggle-slider { outline: 2px solid var(--accent); outline-offset: 2px; }
.tele-sub { padding: 0 18px 14px; display: flex; flex-direction: column; gap: 8px; }
.tele-sub .input { width: 100%; max-width: 360px; }
.tele-warn { font-size: var(--fs-label); color: var(--warn); }
.config-group + .config-group { margin-top: 0; }
.config-table td.env-name { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: 11px; color: var(--text-2); }
.config-table td.config-val { font-weight: 500; }
.ctx-input { width: 92px; }
.node-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.node-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.node-card-header .node-name { font-weight: 600; }
.node-detail { display: flex; justify-content: space-between; gap: 12px; padding: 3px 0; font-size: var(--fs-small); }
.node-detail .nd-label { color: var(--text-2); }
.node-detail .nd-value { font-weight: 500; text-align: right; word-break: break-word; }
.node-detail .nd-value.drift { color: var(--warn); }
</style>

<div><h1 class="page-title">Settings</h1><p class="page-sub">Router <span id="router-version">…</span> on <span id="router-hostname">…</span></p></div>

<section class="section" id="appearance">
  <div class="section-head"><h2 class="section-title">Appearance</h2><span class="small muted">Saved in this browser only</span></div>
  <div class="list">
    <div class="toggle-row">
      <div class="toggle-info"><div class="toggle-label">Theme</div><div class="toggle-desc">Follow the system, or choose one for this browser.</div></div>
      <div class="segmented" id="theme-control" role="radiogroup" aria-label="Theme">
        <button data-theme-choice="system" role="radio">System</button><button data-theme-choice="light" role="radio">Light</button><button data-theme-choice="dark" role="radio">Dark</button>
      </div>
    </div>
    <div class="toggle-row">
      <div class="toggle-info"><div class="toggle-label">Reduce transparency</div><div class="toggle-desc">Make the header solid. On automatically when your system asks for less transparency or more contrast.</div></div>
      <label class="toggle-switch"><input type="checkbox" id="transparency-toggle"><span class="toggle-slider"></span></label>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-head"><h2 class="section-title">Community telemetry</h2></div>
  <div id="telemetry-container"><div class="card empty-state">Loading…</div></div>
</section>

<section class="section">
  <div class="section-head"><h2 class="section-title">Features</h2></div>
  <div class="list" id="toggles-container"><div class="empty-state">Loading…</div></div>
</section>

<section class="section">
  <div class="section-head"><h2 class="section-title">Context windows</h2></div>
  <div id="context-mgmt"><div class="card empty-state">Loading…</div></div>
</section>

<section class="section">
  <div class="section-head"><h2 class="section-title">Fleet nodes</h2></div>
  <div class="node-grid" id="nodes-grid"><div class="card empty-state">Loading…</div></div>
</section>

<section class="section">
  <div class="section-head"><h2 class="section-title">Router configuration</h2></div>
  <div id="config-tables"></div>
  <p class="note">Set through environment variables with the FLEET_ prefix. Restart the router to apply changes.</p>
</section>

<section class="section" id="benchmarks-link">
  <a class="list list-row" href="/dashboard/benchmarks">
    <span><span class="name">Benchmarks</span><br><span class="small muted">Run a benchmark and see the fleet's throughput history.</span></span>
    <span class="chev" aria-hidden="true">›</span>
  </a>
</section>

<script>
var TOGGLE_META = {
  auto_pull: {label:'Auto-Pull Models', desc:'Automatically download models to nodes when requested but not available'},
  vram_fallback: {label:'VRAM-Aware Fallback', desc:'Route to a loaded model in the same category instead of cold-loading the requested model'},
  image_generation: {label:'Image Generation', desc:'Route mflux image generation requests to nodes with image models available'},
  transcription: {label:'Transcription (STT)', desc:'Route speech-to-text requests to nodes with mlx-qwen3-asr available'},
  dynamic_num_ctx: {label:'Dynamic Context Size', desc:'Inject optimized num_ctx on cold loads to reduce KV cache waste'},
  num_ctx_auto_calculate: {label:'Auto-Calculate Context', desc:'Automatically compute optimal num_ctx per model from trace data every 5 minutes'},
  fleet_intelligence: {label:'Fleet Intelligence', desc:'LLM-powered dashboard briefing that analyzes fleet health and performance'}
};

var CONFIG_LABELS = {
  server: 'Router',
  heartbeat: 'Heartbeat',
  scoring: 'Scoring Weights',
  rebalancer: 'Rebalancer',
  pre_warm: 'Pre-warm',
  auto_pull_config: 'Auto-Pull',
  context_protection: 'Context Protection',
  reaper: 'Reaper'
};

function renderSettings(data) {
  document.getElementById('router-version').textContent = 'v' + data.router_version;
  document.getElementById('router-hostname').textContent = data.router_hostname;

  renderTelemetry(data.config.telemetry || {});

  // Toggles
  var tc = document.getElementById('toggles-container');
  var html = '';
  var toggles = data.config.toggles;
  for (var key in toggles) {
    var meta = TOGGLE_META[key] || {label:key, desc:''};
    var checked = toggles[key] ? 'checked' : '';
    var envName = 'FLEET_' + key.toUpperCase();
    html += '<div class="toggle-row">' +
      '<div class="toggle-info">' +
        '<div class="toggle-label">' + meta.label + '</div>' +
        '<div class="toggle-desc">' + meta.desc + '</div>' +
        '<div class="toggle-env">' + envName + '</div>' +
      '</div>' +
      '<label class="toggle-switch">' +
        '<input type="checkbox" ' + checked + ' onchange="toggleSetting(\\'' + key + '\\', this.checked)">' +
        '<span class="toggle-slider"></span>' +
      '</label>' +
    '</div>';
  }
  tc.innerHTML = html;

  // Context Management
  loadContextUsage();

  // Nodes
  var ng = document.getElementById('nodes-grid');
  if (data.nodes.length === 0) {
    ng.innerHTML = '<div class="card empty-state">No nodes registered yet</div>';
  } else {
    ng.innerHTML = data.nodes.map(function(n) {
      var ver = n.agent_version || 'unknown';
      var drift = n.agent_version && n.agent_version !== data.router_version;
      function row(label, value, cls) { return '<div class="node-detail"><span class="nd-label">' + label + '</span><span class="nd-value' + (cls ? ' ' + cls : '') + '">' + escapeHtml(value) + '</span></div>'; }
      return '<div class="card">' +
        '<div class="node-card-header">' + dot(n.status) +
          '<span class="node-name">' + escapeHtml(n.node_id) + '</span>' +
          (n.is_router ? '<span class="tag">router</span>' : '') +
        '</div>' +
        row('Status', n.status) +
        row('Version', ver + (drift ? ' (router ' + data.router_version + ')' : ''), drift ? 'drift' : '') +
        row('Ollama', n.ip) +
        row('Models loaded', String(n.models_loaded_count)) +
        (n.image_models && n.image_models.length ? row('Image', n.image_models.join(', ')) : '') +
        (n.stt_models && n.stt_models.length ? row('Speech', n.stt_models.join(', ')) : '') +
        (n.embed_models && n.embed_models.length ? row('Embed', n.embed_models.join(', ')) : '') +
        (n.vision_embed_models && n.vision_embed_models.length ? row('Vision embed', n.vision_embed_models.join(', ')) : '') +
      '</div>';
    }).join('');
  }

  // Config tables
  var ct = document.getElementById('config-tables');
  var tablesHtml = '';
  var configGroups = data.config;
  for (var group in configGroups) {
    if (group === 'toggles') continue;
    var label = CONFIG_LABELS[group] || (group.charAt(0).toUpperCase() + group.slice(1)).replace(/_/g, ' ');
    var settings = configGroups[group];
    tablesHtml += '<details class="disclosure config-group"><summary>' + escapeHtml(label) + ' <span class="faint">\u00b7 ' + Object.keys(settings).length + '</span></summary>' +
      '<div class="card flush table-scroll"><table class="data-table config-table"><thead><tr><th>Setting</th><th>Value</th><th>Environment variable</th></tr></thead><tbody>';
    for (var sKey in settings) {
      tablesHtml += '<tr><td>' + escapeHtml(sKey) + '</td><td class="config-val">' + escapeHtml(settings[sKey]) + '</td><td class="env-name">FLEET_' + sKey.toUpperCase() + '</td></tr>';
    }
    tablesHtml += '</tbody></table></div></details>';
  }
  ct.innerHTML = tablesHtml;
}

var telemetryEditing = false;

function renderTelemetry(t) {
  var el = document.getElementById('telemetry-container');
  if (!el) return;
  // The page polls loadSettings() every 15s. Re-rendering mid-edit would wipe
  // a half-typed herd name and silently flip the toggle back, so an unsaved
  // edit wins until the user saves or cancels it.
  if (telemetryEditing && document.getElementById('tele-nick')) return;
  var on = t.enabled !== false;
  var anon = t.anonymous !== false;

  var html = '';
  // Master switch.
  html += '<div class="list"><div class="toggle-row">' +
    '<div class="toggle-info">' +
      '<div class="toggle-label">Share anonymous usage</div>' +
      '<div class="toggle-desc">One summary a day: models used, request counts, error categories. ' +
        'Never prompts, never your hostname. ' +
        '<a href="https://ollamaherd.com/telemetry" target="_blank" rel="noopener">See every field</a>.</div>' +
      '<div class="toggle-env">FLEET_NODE_TELEMETRY</div>' +
    '</div>' +
    '<label class="toggle-switch">' +
      '<input type="checkbox" ' + (on ? 'checked' : '') + ' onchange="setTelemetry(this.checked)">' +
      '<span class="toggle-slider"></span>' +
    '</label>' +
  '</div>';

  if (on) {
    // Anonymity switch. On (default) = pseudonymous: listed under a handle
    // derived from install_id, never a name or hostname.  The wording here and
    // in common/telemetry_notice.py must agree with ollamaherd.com/telemetry.
    html += '<div class="toggle-row">' +
      '<div class="toggle-info">' +
        '<div class="toggle-label">Stay anonymous</div>' +
        '<div class="toggle-desc">' + (anon
            ? 'Listed on the leaderboard as a random handle like &quot;herd-7f3a2b&quot; \u2014 never your name or hostname.'
            : 'Turned off: your herd appears on the public leaderboard under the name below.') +
        '</div>' +
        '<div class="toggle-env">FLEET_NODE_HERD_NICKNAME</div>' +
      '</div>' +
      '<label class="toggle-switch">' +
        '<input type="checkbox" ' + (anon ? 'checked' : '') + ' onchange="setAnonymous(this.checked)">' +
        '<span class="toggle-slider"></span>' +
      '</label>' +
    '</div>';

    if (!anon) {
      var nick = (t.nickname || '').replace(/"/g, '&quot;');
      html += '<div class="tele-sub">' +
        '<input id="tele-nick" class="input" maxlength="30" placeholder="Name your herd" value="' + nick + '">' +
        '<div class="tele-warn">This name is public. It appears on the leaderboard once approved.</div>' +
        '<div><button class="btn primary small" onclick="saveNickname()">Save name</button></div>' +
      '</div>';
    }
  }

  html += '</div><p class="note subhead">Changes are saved to ~/.fleet-manager/env and take effect when herd-node restarts.' +
    (t.shell_override ? ' <b>A shell export is currently overriding this file, so changes here will not apply until you unset it.</b>' : '') +
    '</p>';

  el.innerHTML = html;
}

async function postTelemetry(body, msg) {
  try {
    var resp = await fetch('/dashboard/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    var r = await resp.json();
    if (r.status === 'updated') {
      if (r.not_persisted && r.not_persisted.length) {
        showToast('Could not save - check permissions on ~/.fleet-manager/env');
      } else {
        showToast(msg + ' - restart herd-node to apply');
      }
    }
  } catch (err) { console.error(err); }
  loadSettings();
}

function setTelemetry(on) {
  postTelemetry({telemetry: on}, on ? 'Telemetry enabled' : 'Telemetry disabled');
}

function setAnonymous(anon) {
  // Anonymity is expressed by the absence of a nickname, so turning it back on
  // clears the name rather than keeping a hidden one we might publish later.
  if (anon) {
    telemetryEditing = false;
    postTelemetry({herd_nickname: ''}, 'Now anonymous \u2014 shown as a random handle');
  } else {
    telemetryEditing = true;
    renderTelemetry({enabled: true, anonymous: false, nickname: ''});
    var f = document.getElementById('tele-nick');
    if (f) f.focus();
  }
}

function saveNickname() {
  var v = (document.getElementById('tele-nick').value || '').trim();
  if (!v) { showToast('Enter a name, or switch anonymous back on'); return; }
  // Mirror the server's charset. Without this the name saves happily, then the
  // daily payload 422s and the herd is silently anonymous forever.
  if (!/^[A-Za-z0-9 ._-]+$/.test(v)) {
    showToast('Use letters, numbers, spaces, - _ . only');
    return;
  }
  telemetryEditing = false;
  postTelemetry({herd_nickname: v}, 'Herd name saved');
}

async function toggleSetting(field, value) {
  try {
    var body = {};
    body[field] = value;
    var resp = await fetch('/dashboard/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    var result = await resp.json();
    if (result.status === 'updated') {
      var meta = TOGGLE_META[field] || {label:field};
      showToast(meta.label + (value ? ' enabled' : ' disabled'));
    } else {
      loadSettings();
    }
  } catch (err) {
    console.error('Toggle error:', err);
    loadSettings();
  }
}

async function loadContextUsage() {
  var container = document.getElementById('context-mgmt');
  try {
    var resp = await fetch('/dashboard/api/context-usage');
    var data = await resp.json();
    if (!data.models || data.models.length === 0) {
      container.innerHTML = '<div class="card empty-state">No models with usage data yet. Context recommendations appear after traffic is routed.</div>';
      return;
    }
    // Utilization here is how much of the allocated window is used.  Low is a
    // suggestion, never a failure, so it is amber at most -- and neutral when
    // the context is pinned by an override, which is deliberate (see the
    // context_waste gotcha in CLAUDE.md), or when nothing is allocated.
    var html = '<div class="card flush table-scroll"><table class="data-table config-table"><thead><tr><th>Model</th><th class="num">Allocated</th><th class="num">p99 total</th><th class="num">Used</th><th class="num">Recommended</th><th>Override</th><th></th></tr></thead><tbody>';
    data.models.forEach(function(m) {
      var used = !m.allocated_ctx || m.override_ctx || m.utilization_pct >= 10 ? '' : 'warn';
      var safe = m.model.replace(/[^a-zA-Z0-9]/g, '-');
      html += '<tr data-model="' + escapeHtml(m.model) + '" data-safe="' + safe + '" data-rec="' + m.recommended_ctx + '">' +
        '<td class="name">' + escapeHtml(m.model) + '</td>' +
        '<td class="num">' + (m.allocated_ctx ? fmtNum(m.allocated_ctx) : '\u2014') + '</td>' +
        '<td class="num">' + (m.total_tokens.p99 ? fmtNum(m.total_tokens.p99) : '\u2014') + '</td>' +
        '<td class="num"><span class="stat ' + used + '"><span class="v">' + (m.allocated_ctx ? m.utilization_pct + '%' : '\u2014') + '</span></span></td>' +
        '<td class="num">' + fmtNum(m.recommended_ctx) + ' <span class="faint small">(' + m.savings_pct + '% smaller)</span></td>' +
        '<td><input class="input ctx-input" type="number" id="ctx-override-' + safe + '" value="' + (m.override_ctx || '') + '" placeholder="' + m.recommended_ctx + '"></td>' +
        '<td><div class="row"><button class="btn small primary" data-ctx-apply>Apply</button><button class="btn small quiet" data-ctx-rec>Use recommended</button></div></td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';
    html += '<p class="note subhead">Overrides take effect on the next cold load or Ollama restart. Turn on Dynamic Context Size above to inject them automatically.</p>';
    container.innerHTML = html;
  } catch(e) {
    container.innerHTML = '<div class="card empty-state">Could not load context data.</div>';
  }
}

async function applyCtxOverride(model, safeId) {
  var input = document.getElementById('ctx-override-' + safeId);
  var val = parseInt(input.value);
  if (!val || val < 1024) { showToast('Minimum context: 1024'); return; }
  try {
    var resp = await fetch('/dashboard/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({num_ctx_overrides: {[model]: val}})
    });
    var result = await resp.json();
    if (result.status === 'updated') {
      showToast('Context override set: ' + model + ' = ' + val.toLocaleString());
    }
  } catch(e) { showToast('Error applying override'); }
}

function applyRecommended(model, recommended, safeId) {
  document.getElementById('ctx-override-' + safeId).value = recommended;
  applyCtxOverride(model, safeId);
}

async function loadSettings() {
  try {
    var resp = await fetch('/dashboard/api/settings');
    var data = await resp.json();
    renderSettings(data);
  } catch (err) {
    console.error('Settings load error:', err);
  }
}

// Context override buttons, delegated so the table can re-render freely.
document.addEventListener('click', function(e) {
  var tr = e.target.closest('tr[data-safe]');
  if (!tr) return;
  if (e.target.closest('[data-ctx-apply]')) applyCtxOverride(tr.dataset.model, tr.dataset.safe);
  else if (e.target.closest('[data-ctx-rec]')) applyRecommended(tr.dataset.model, parseInt(tr.dataset.rec), tr.dataset.safe);
});

// Appearance: per-viewer, stored in this browser.
function renderAppearance() {
  var theme = HerdPrefs.get('theme', 'system');
  document.querySelectorAll('[data-theme-choice]').forEach(function(b) {
    var on = b.dataset.themeChoice === theme;
    b.classList.toggle('active', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  document.getElementById('transparency-toggle').checked = HerdPrefs.get('transparency', 'on') === 'off';
}
document.querySelectorAll('[data-theme-choice]').forEach(function(b) {
  b.addEventListener('click', function() { setAppearance('theme', b.dataset.themeChoice); renderAppearance(); });
});
document.getElementById('transparency-toggle').addEventListener('change', function(e) {
  setAppearance('transparency', e.target.checked ? 'off' : 'on');
});
renderAppearance();


loadSettings();
setInterval(loadSettings, 15000);
</script>
"""
