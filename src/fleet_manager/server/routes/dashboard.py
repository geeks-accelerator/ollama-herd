"""Dashboard routes — fleet overview, trends, model insights, SSE stream, and data APIs."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

router = APIRouter(tags=["dashboard"])

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


# ---------------------------------------------------------------------------
# SSE event stream (shared by the fleet overview page)
# ---------------------------------------------------------------------------


@router.get("/dashboard/events")
async def dashboard_events(request: Request):
    """SSE endpoint for real-time fleet state updates."""

    async def event_stream():
        while True:
            registry = request.app.state.registry
            queue_mgr = request.app.state.queue_mgr

            nodes = []
            for node in registry.get_all_nodes():
                node_data = {
                    "node_id": node.node_id,
                    "status": node.status.value,
                    "hardware": {
                        "memory_total_gb": node.hardware.memory_total_gb,
                        "cores_physical": node.hardware.cores_physical,
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
                    }
                if node.ollama:
                    node_data["ollama"] = {
                        "models_loaded": [
                            {
                                "name": m.name,
                                "size_gb": round(m.size_gb, 2),
                                "parameter_size": m.parameter_size,
                                "quantization": m.quantization,
                                "context_length": m.context_length,
                            }
                            for m in node.ollama.models_loaded
                        ],
                        "models_available_count": len(node.ollama.models_available),
                        "requests_active": node.ollama.requests_active,
                    }
                if node.image and node.image.models_available:
                    node_data["image_models"] = [
                        m.name for m in node.image.models_available
                    ]
                if node.transcription and node.transcription.models_available:
                    node_data["stt_models"] = [
                        m.name for m in node.transcription.models_available
                    ]
                # Embedding models: Ollama models with "embed" in the name
                if node.ollama:
                    embed_models = [
                        m for m in node.ollama.models_available
                        if "embed" in m.lower()
                    ]
                    if embed_models:
                        node_data["embed_models"] = embed_models
                # Vision embedding models (DINOv2, SigLIP, CLIP)
                if node.vision_embedding and node.vision_embedding.models_available:
                    node_data["vision_embed_models"] = [
                        m.name for m in node.vision_embedding.models_available
                    ]
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

            data = {
                "nodes": nodes,
                "queues": queue_mgr.get_queue_info(),
                "timestamp": time.time(),
            }

            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# JSON data APIs for dashboard pages
# ---------------------------------------------------------------------------


@router.get("/dashboard/api/trends")
async def dashboard_trends_data(
    request: Request, hours: int = 72, start_ts: float = 0, end_ts: float = 0,
):
    """Hourly aggregated request counts and latencies for the trends chart."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {"hours": hours, "data": []}
    data = await latency_store.get_hourly_trends(
        hours=hours, start_ts=start_ts, end_ts=end_ts,
    )
    return {"hours": hours, "data": data}


@router.get("/dashboard/api/models")
async def dashboard_models_data(
    request: Request, days: int = 7, start_ts: float = 0, end_ts: float = 0,
):
    """Per-model daily aggregated stats for the model insights page."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {"days": days, "daily": [], "summary": []}
    daily = await latency_store.get_model_daily_stats(
        days=days, start_ts=start_ts, end_ts=end_ts,
    )
    summary = await latency_store.get_model_summary()
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
    request: Request, days: int = 7, start_ts: float = 0, end_ts: float = 0,
):
    """Per-tag aggregated stats for the Apps analytics page."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"days": days, "data": [], "summary": []}
    data = await trace_store.get_usage_by_tag(
        days=days, start_ts=start_ts, end_ts=end_ts,
    )
    summary = await trace_store.get_tag_summary()
    return {"days": days, "data": data, "summary": summary}


@router.get("/dashboard/api/tags/daily")
@router.get("/dashboard/api/apps/daily")  # backwards compat
async def dashboard_tags_daily_data(
    request: Request, days: int = 7, start_ts: float = 0, end_ts: float = 0,
):
    """Per-tag, per-day breakdown for the Apps analytics charts."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"days": days, "data": []}
    data = await trace_store.get_tag_daily_stats(
        days=days, start_ts=start_ts, end_ts=end_ts,
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
    return {"status": "started", "run_id": run_id, "mode": mode, "duration": duration, "model_types": model_types}


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


@router.get("/dashboard/api/health")
async def dashboard_health_data(request: Request):
    """Fleet health analysis with actionable recommendations."""
    from fleet_manager.server.health_engine import HealthEngine

    registry = request.app.state.registry
    trace_store = getattr(request.app.state, "trace_store", None)

    engine = HealthEngine()
    report = await engine.analyze(registry, trace_store)
    return report.model_dump()


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

        models.append({
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
        })

    return {"days": days, "models": models}


_recommendations_cache: dict = {"data": None, "generated_at": 0.0}

# Fleet Intelligence briefing cache
_briefing_cache: dict = {
    "briefing": None, "model": None, "generated_at": 0.0, "generating": False,
}
_briefing_history: list[dict] = []  # Last 10 briefings
_BRIEFING_HISTORY_MAX = 10

EMBED_SKIP = ("embed", "nomic", "bge", "e5-")


def _compute_briefing_interval(registry, trace_store) -> float:
    """Adaptive refresh: faster when busy or issues detected, slower when idle."""
    nodes = registry.get_online_nodes()
    if not nodes:
        return 3600  # 1h if no nodes

    # Check queue activity
    total_in_flight = 0
    for node in nodes:
        if node.ollama:
            total_in_flight += node.ollama.requests_active or 0

    if total_in_flight > 5:
        return 1800  # 30 min when very busy
    if total_in_flight > 0:
        return 3600  # 1 hour when active
    return 21600  # 6 hours when idle


async def _generate_briefing(request) -> dict:
    """Generate a fleet intelligence briefing using an internal LLM call."""
    settings = request.app.state.settings
    registry = request.app.state.registry
    trace_store = getattr(request.app.state, "trace_store", None)
    health_engine = getattr(request.app.state, "health_engine", None)

    # Auto-select model: first loaded LLM (skip embed/image)
    model = settings.fleet_intelligence_model
    if not model:
        for node in registry.get_online_nodes():
            if not node.ollama:
                continue
            for m in node.ollama.models_loaded:
                nm = m.name.lower()
                if not any(p in nm for p in EMBED_SKIP) and "image" not in nm and not nm.startswith("x/"):
                    model = m.name
                    break
            if model:
                break
    if not model:
        return {"error": "No LLM models loaded", "briefing": None}

    # Gather fleet data
    nodes = registry.get_online_nodes()
    nodes_online = len(nodes)
    models_loaded = sum(
        len(n.ollama.models_loaded) if n.ollama else 0 for n in nodes
    )
    total_available_gb = sum(
        n.memory.available_gb if n.memory else 0 for n in nodes
    )

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
            ttft = overall['avg_ttft_ms']
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
                    wasteful.append(
                        f"{s['model']} ({(total_p99/alloc*100):.0f}% utilization)"
                    )
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
                conn_summary += (
                    f"{node.node_id} recovered from {total} connection failures. "
                )

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
                    f"{p['model']}: score={p['priority_score']:.0f} "
                    f"({p['requests_24h']} reqs/24h)"
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
                prev_briefings = "Your previous briefing(s) (for context — don't repeat, note what changed):\n"
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
- {conn_summary if conn_summary else 'No connection issues.'}
- {priority_summary if priority_summary else 'No model usage history yet.'}
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
                                "pull a model (curl /api/pull -d '{\"name\":\"model\"}'), "
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
            _briefing_history.insert(0, {
                "briefing": result["briefing"],
                "model": result["model"],
                "generated_at": result["generated_at"],
            })
            if len(_briefing_history) > _BRIEFING_HISTORY_MAX:
                _briefing_history.pop()
            # Persist to SQLite
            ts = getattr(request.app.state, "trace_store", None)
            if ts:
                import asyncio
                asyncio.create_task(
                    ts.save_briefing(result["briefing"], result["model"])
                )
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


@router.get("/dashboard/api/settings")
async def dashboard_settings_data(request: Request):
    """Current configuration values and node list for the settings page."""
    import socket

    from fleet_manager import __version__

    settings = request.app.state.settings
    registry = request.app.state.registry
    hostname = socket.gethostname().split(".")[0]

    config = {
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
            embed_models = [
                m for m in node.ollama.models_available
                if "embed" in m.lower()
            ]
        vision_embed_models = []
        if node.vision_embedding:
            vision_embed_models = [
                m.name for m in node.vision_embedding.models_available
            ]
        nodes_data.append({
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
        })

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

    mutable_fields = {
        "auto_pull", "vram_fallback", "image_generation", "transcription",
        "dynamic_num_ctx", "num_ctx_auto_calculate", "fleet_intelligence",
    }
    updated = {}

    for field in mutable_fields:
        if field in body:
            value = bool(body[field])
            setattr(settings, field, value)
            updated[field] = value

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

    return {"status": "updated", "updated": updated}


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
            int(sum(e["generation_ms"] for e in completed) / len(completed))
            if completed
            else 0
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
            int(sum(e["processing_ms"] for e in completed) / len(completed))
            if completed
            else 0
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

            models.append({
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
            })

        # Sort: loaded first, then by last-used desc, then alphabetical
        models.sort(key=lambda m: (
            not m["loaded_in_vram"],
            -(m["last_used"] or 0),
            m["name"],
        ))

        result.append({
            "node_id": node.node_id,
            "models": models,
            "total_size_gb": round(sum(m["size_gb"] for m in models), 1),
            "disk_available_gb": (
                round(node.disk.available_gb, 1) if node.disk else 0
            ),
            "disk_total_gb": round(node.disk.total_gb, 1) if node.disk else 0,
        })

    return {"nodes": result}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Fleet overview — live node and queue state."""
    return _dashboard_page("Dashboard", "overview", _OVERVIEW_BODY)


@router.get("/dashboard/trends", response_class=HTMLResponse)
async def dashboard_trends_page():
    """Historical trends — requests, latency, and token throughput over time."""
    return _dashboard_page(
        "Trends",
        "trends",
        _TRENDS_BODY,
        extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>',
    )


@router.get("/dashboard/models", response_class=HTMLResponse)
async def dashboard_models_page():
    """Model insights — per-model performance and token usage."""
    return _dashboard_page(
        "Model Insights",
        "models",
        _MODELS_BODY,
        extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>',
    )


@router.get("/dashboard/tags", response_class=HTMLResponse)
@router.get("/dashboard/apps", response_class=HTMLResponse)  # backwards compat
async def dashboard_tags_page():
    """Tags analytics — per-tag performance and usage breakdown."""
    return _dashboard_page(
        "Tags",
        "tags",
        _TAGS_BODY,
        extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>',
    )


@router.get("/dashboard/benchmarks", response_class=HTMLResponse)
async def dashboard_benchmarks_page():
    """Benchmarks — historical benchmark runs and capacity growth tracking."""
    return _dashboard_page(
        "Benchmarks",
        "benchmarks",
        _BENCHMARKS_BODY,
        extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>',
    )


@router.get("/dashboard/health", response_class=HTMLResponse)
async def dashboard_health_page():
    """Fleet health — recommendations and vitals."""
    return _dashboard_page("Health", "health", _HEALTH_BODY)


@router.get("/dashboard/recommendations", response_class=HTMLResponse)
async def dashboard_recommendations_page():
    """Model recommendations — optimal model mix per node."""
    return _dashboard_page("Recommendations", "recommendations", _RECOMMENDATIONS_BODY)


@router.get("/dashboard/settings", response_class=HTMLResponse)
async def dashboard_settings_page():
    """Settings — configuration, toggles, and node list."""
    return _dashboard_page("Settings", "settings", _SETTINGS_BODY)


# ---------------------------------------------------------------------------
# Shared layout helper
# ---------------------------------------------------------------------------

_SHARED_CSS = """
:root {
  --bg: #0a0a0f;
  --card: #12121a;
  --border: #1e1e2e;
  --text: #e0e0e8;
  --text-dim: #8888a0;
  --accent: #6c63ff;
  --green: #22c55e;
  --yellow: #eab308;
  --red: #ef4444;
  --orange: #f97316;
  --blue: #3b82f6;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.header {
  padding: 16px 32px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
.header h1 {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.3px;
  margin-right: 24px;
}
.header-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.nav-tabs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.nav-tab {
  padding: 6px 16px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-dim);
  text-decoration: none;
  transition: all 0.15s;
}
.nav-tab:hover {
  color: var(--text);
  background: rgba(108,99,255,0.1);
}
.nav-tab.active {
  color: var(--accent);
  background: rgba(108,99,255,0.15);
}
.header-stats {
  display: flex;
  gap: 24px;
  flex-shrink: 0;
}
.header-stat {
  text-align: center;
}
.header-stat .value {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.header-stat .label {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.main {
  padding: 24px 32px;
  display: flex;
  flex-direction: column;
  gap: 24px;
  flex: 1;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  color: var(--text-dim);
  margin-bottom: 12px;
}
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}
@keyframes fadeSlideIn {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
.card-animate {
  animation: fadeSlideIn 0.35s ease forwards;
  opacity: 0;
}
.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  position: relative;
  top: -1px;
}
.status-dot.online { background: var(--green); box-shadow: 0 0 8px var(--green); }
.status-dot.degraded { background: var(--yellow); box-shadow: 0 0 8px var(--yellow); }
.status-dot.offline { background: var(--red); }
.badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 500;
}
.badge.online { background: rgba(34,197,94,0.15); color: var(--green); }
.badge.degraded { background: rgba(234,179,8,0.15); color: var(--yellow); }
.badge.offline { background: rgba(239,68,68,0.15); color: var(--red); }
.empty-state {
  color: var(--text-dim);
  font-size: 14px;
  text-align: center;
  padding: 40px;
}
.pulse { animation: pulse 2s infinite; }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.footer {
  padding: 16px 32px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--text-dim);
  font-size: 12px;
}
.connected-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
}
/* Summary cards row */
.summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.summary-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 20px;
  text-align: center;
}
.summary-card .sc-value {
  font-size: 28px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.summary-card .sc-label {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-top: 4px;
}
/* Time range buttons */
.time-range {
  display: flex;
  gap: 4px;
  margin-bottom: 16px;
}
.time-btn {
  padding: 5px 14px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text-dim);
  cursor: pointer;
  transition: all 0.15s;
}
.time-btn:hover { color: var(--text); border-color: var(--accent); }
.time-btn.active { color: var(--accent); background: rgba(108,99,255,0.15); border-color: var(--accent); }
/* Charts grid */
.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.charts-row[hidden] { display: none; }
.chart-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}
.chart-card.full-width {
  grid-column: 1 / -1;
}
.chart-card h4 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 16px;
}
/* Model insights table */
.model-table {
  width: 100%;
  border-collapse: collapse;
}
.model-table th {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
}
.model-table td {
  padding: 10px 12px;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  border-bottom: 1px solid rgba(30,30,46,0.5);
}
.model-table tr { cursor: pointer; transition: background 0.15s; }
.model-table tr:hover { background: rgba(108,99,255,0.05); }
.model-table tr.selected { background: rgba(108,99,255,0.1); }
.model-table .model-name {
  font-weight: 600;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px;
}
/* Responsive */
@media (max-width: 768px) {
  .header { padding: 12px 16px; flex-wrap: wrap; gap: 8px; }
  .header h1 { font-size: 16px; margin-right: 12px; }
  .nav-tab { padding: 5px 10px; font-size: 12px; }
  .header-stats { gap: 12px; }
  .header-stat .value { font-size: 18px; }
  .main { padding: 16px; gap: 16px; }
  .footer { padding: 12px 16px; flex-wrap: wrap; gap: 4px; font-size: 11px; }
  .charts-row { grid-template-columns: 1fr; }
  .summary-cards { grid-template-columns: repeat(2, 1fr); }
  .summary-card .sc-value { font-size: 22px; }
  .model-table th, .model-table td { padding: 6px 8px; font-size: 12px; }
}
@media (max-width: 480px) {
  .header-stats { display: none; }
  .summary-cards { grid-template-columns: 1fr 1fr; }
}
.time-range { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:16px; }
.tr-btn { padding:5px 14px; border-radius:6px; font-size:12px; font-weight:500; background:var(--card); border:1px solid var(--border); color:var(--text-dim); cursor:pointer; transition:all 0.15s; }
.tr-btn:hover { color:var(--text); border-color:var(--accent); }
.tr-btn.active { color:var(--accent); background:rgba(108,99,255,0.15); border-color:var(--accent); }
.tr-custom { display:flex; align-items:center; gap:8px; margin-left:8px; }
.tr-custom input { background:var(--card); border:1px solid var(--border); border-radius:6px; padding:4px 8px; color:var(--text); font-size:12px; }
.tr-custom span { color:var(--text-dim); font-size:12px; }
.tr-apply { padding:4px 12px; border-radius:6px; font-size:12px; background:var(--accent); color:#fff; border:none; cursor:pointer; }
.tr-apply:hover { opacity:0.85; }
"""


def _dashboard_page(title: str, active_tab: str, body_html: str, extra_head: str = "") -> str:
    """Generate a full dashboard HTML page with shared nav, styles, and footer."""
    nav_items = [
        ("overview", "Dashboard", "/dashboard"),
        ("trends", "Trends", "/dashboard/trends"),
        ("models", "Model Insights", "/dashboard/models"),
        ("tags", "Tags", "/dashboard/tags"),
        ("benchmarks", "Benchmarks", "/dashboard/benchmarks"),
        ("health", "Health", "/dashboard/health"),
        ("recommendations", "Recommendations", "/dashboard/recommendations"),
        ("settings", "Settings", "/dashboard/settings"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="nav-tab {"active" if key == active_tab else ""}">{label}</a>'
        for key, label, href in nav_items
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - Ollama Herd</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>{_SHARED_CSS}</style>
<script>
function barColor(pct) {{
  var h = 142 - (pct / 100) * 142;
  var s = 71 + (pct / 100) * 13;
  var l = 45 + (pct / 100) * 15;
  return 'hsl(' + h + ',' + s + '%,' + l + '%)';
}}
function initTimeRange(containerId, callback, defaultRange) {{
  var c = document.getElementById(containerId);
  if (!c) return;
  var presets = {{
    '24h': 24*3600, '48h': 48*3600, '72h': 72*3600,
    '7d': 7*86400, '30d': 30*86400
  }};
  var def = defaultRange || '7d';
  var html = '<div class="time-range">';
  ['24h','48h','72h','7d','30d'].forEach(function(r) {{
    html += '<button class="tr-btn' + (r===def?' active':'') + '" data-range="' + r + '">' + r + '</button>';
  }});
  html += '<button class="tr-btn" data-range="custom">Custom</button>';
  html += '<div class="tr-custom" id="' + containerId + '-custom" style="display:none">';
  html += '<input type="datetime-local" id="' + containerId + '-start">';
  html += '<span>to</span>';
  html += '<input type="datetime-local" id="' + containerId + '-end">';
  html += '<button class="tr-apply" id="' + containerId + '-apply">Apply</button>';
  html += '</div></div>';
  c.innerHTML = html;
  function firePreset(range) {{
    var now = Date.now() / 1000;
    callback(now - presets[range], now);
  }}
  c.querySelectorAll('.tr-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      c.querySelectorAll('.tr-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      var range = btn.dataset.range;
      var customEl = document.getElementById(containerId + '-custom');
      if (range === 'custom') {{
        customEl.style.display = 'flex';
        var now = new Date();
        var start = new Date(now.getTime() - 7*86400000);
        document.getElementById(containerId + '-end').value = now.toISOString().slice(0,16);
        document.getElementById(containerId + '-start').value = start.toISOString().slice(0,16);
      }} else {{
        customEl.style.display = 'none';
        firePreset(range);
      }}
    }});
  }});
  document.getElementById(containerId + '-apply')?.addEventListener('click', function() {{
    var s = new Date(document.getElementById(containerId + '-start').value).getTime() / 1000;
    var e = new Date(document.getElementById(containerId + '-end').value).getTime() / 1000;
    if (s && e && e > s) callback(s, e);
  }});
  firePreset(def);
}}
</script>
{extra_head}
</head>
<body>
<div class="header">
  <div class="header-left">
    <h1>Ollama Herd</h1>
    <nav class="nav-tabs">{nav_html}</nav>
  </div>
  <div class="header-stats" id="header-stats"></div>
</div>
{body_html}
<div class="footer">
  <div>Ollama Herd v0.1.0 — Created by Twins at <a href="https://geeksinthewoods.com/" target="_blank" style="color:var(--accent);text-decoration:none">Geeks in the Woods</a></div>
  <div class="connected-indicator">
    <span class="status-dot online pulse" id="sse-dot"></span>
    <span id="sse-status">Connected</span>
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Fleet Overview page body
# ---------------------------------------------------------------------------

_OVERVIEW_BODY = """
<style>
.nodes-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(380px, 100%), 1fr));
  gap: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.card-header h3 { font-size: 15px; font-weight: 600; }
.metrics-row {
  display: flex;
  gap: 16px;
  margin-bottom: 14px;
}
.metric { flex: 1; }
.metric .label { font-size: 11px; color: var(--text-dim); margin-bottom: 4px; }
.metric .value { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
.bar-container { height: 6px; background: var(--border); border-radius: 3px; margin-top: 6px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; transition: width 0.8s ease, background 0.5s ease; }
.models-list { margin-top: 12px; }
.model-chip {
  display: inline-flex; align-items: center; gap: 4px;
  background: rgba(108,99,255,0.1); border: 1px solid rgba(108,99,255,0.2);
  border-radius: 6px; padding: 3px 10px; font-size: 12px;
  margin: 2px 4px 2px 0; font-variant-numeric: tabular-nums;
  transition: box-shadow 0.3s ease;
}
.model-chip.embed { background: rgba(59,130,246,0.1); border-color: rgba(59,130,246,0.25); color: var(--blue); }
.model-chip.image { background: rgba(249,115,22,0.1); border-color: rgba(249,115,22,0.25); color: var(--orange); }
.model-chip.stt { background: rgba(34,197,94,0.1); border-color: rgba(34,197,94,0.25); color: var(--green); }
.model-chip.hot { box-shadow: 0 0 6px rgba(108,99,255,0.3); }
.model-chip.embed.hot { box-shadow: 0 0 6px rgba(59,130,246,0.3); }
.model-chip.image.hot { box-shadow: 0 0 6px rgba(249,115,22,0.3); }
.model-chip .size { color: var(--text-dim); font-size: 11px; }
.queues-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
  gap: 12px;
}
.queue-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 18px; display: flex; align-items: center; justify-content: space-between;
}
.queue-name { font-size: 13px; font-weight: 500; font-family: 'SF Mono', 'Fira Code', monospace; }
.queue-stats { display: flex; gap: 14px; align-items: center; }
.queue-stat { text-align: center; }
.queue-stat .num { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; }
.queue-stat .lbl { font-size: 10px; color: var(--text-dim); text-transform: uppercase; }
@media (max-width: 768px) {
  .metrics-row { flex-wrap: wrap; gap: 10px; }
  .metric { min-width: calc(50% - 10px); }
  .queue-card { flex-direction: column; align-items: flex-start; gap: 8px; }
  .queue-stats { flex-wrap: wrap; gap: 10px; }
}
</style>

<div class="main">
  <div id="briefing-section" style="display:none">
    <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
      Fleet Intelligence
      <div style="display:flex;gap:6px">
        <button id="briefing-refresh" onclick="fetchBriefing(true)" style="background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer">Refresh</button>
        <button onclick="dismissBriefing()" style="background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer">Dismiss</button>
      </div>
    </div>
    <div class="card" id="briefing-card">
      <div id="briefing-content" style="font-size:13px;line-height:1.7;color:var(--text)">
        <span style="color:var(--text-dim)">Generating fleet briefing...</span>
      </div>
      <div id="briefing-meta" style="margin-top:10px;font-size:11px;color:var(--text-dim);display:flex;justify-content:space-between"></div>
    </div>
  </div>
  <div>
    <div class="section-title">Herd Nodes</div>
    <div class="nodes-grid" id="nodes-container">
      <div class="empty-state">Waiting for nodes...</div>
    </div>
  </div>
  <div>
    <div class="section-title">Request Queues</div>
    <div class="queues-grid" id="queues-container">
      <div class="empty-state">No active queues</div>
    </div>
  </div>
</div>

<script>
function formatGB(gb) {
  if (gb >= 100) return Math.round(gb) + ' GB';
  if (gb >= 10) return gb.toFixed(1) + ' GB';
  return gb.toFixed(2) + ' GB';
}

var _lastNodeIds = '';
function renderNodes(nodes) {
  const container = document.getElementById('nodes-container');
  if (!nodes.length) {
    container.innerHTML = '<div class="empty-state">No nodes connected</div>';
    _lastNodeIds = '';
    return;
  }

  // Rebuild full cards when nodes change, status changes, or models change
  var nodeIds = nodes.map(function(n) {
    var models = n.ollama ? n.ollama.models_loaded.map(function(m) { return m.name; }).join('+') : '';
    return n.node_id + ':' + n.status + ':' + models;
  }).join(',');
  var needsRebuild = (nodeIds !== _lastNodeIds);

  if (!needsRebuild) {
    // Fast path: update values in-place without rebuilding DOM
    var totalModels2 = 0, onlineCount2 = 0;
    nodes.forEach(function(node) {
      if (node.status === 'online') onlineCount2++;
      var models = node.ollama ? node.ollama.models_loaded : [];
      totalModels2 += models.length + (node.vision_embed_models ? node.vision_embed_models.length : 0);
      var safeId = 'node-' + node.node_id.replace(/[^a-zA-Z0-9]/g, '-');
      var card = document.getElementById(safeId);
      if (!card) { needsRebuild = true; return; }
      var cpu = node.cpu ? node.cpu.utilization_pct : 0;
      var memUsed = node.memory ? node.memory.used_gb : 0;
      var memTotal = node.memory ? node.memory.total_gb : node.hardware.memory_total_gb;
      var memPct = memTotal > 0 ? (memUsed / memTotal) * 100 : 0;
      var el;
      el = card.querySelector('.cpu-val'); if (el) el.textContent = cpu.toFixed(1) + '%';
      el = card.querySelector('.cpu-bar'); if (el) { el.style.width = cpu + '%'; el.style.background = barColor(cpu); }
      el = card.querySelector('.mem-val'); if (el) el.textContent = formatGB(memUsed) + ' / ' + formatGB(memTotal);
      el = card.querySelector('.mem-bar'); if (el) { el.style.width = memPct + '%'; el.style.background = barColor(memPct); }
    });
    if (!needsRebuild) {
      var hn = document.getElementById('stat-nodes');
      var hm = document.getElementById('stat-models');
      if (hn) hn.textContent = onlineCount2;
      if (hm) hm.textContent = totalModels2;
      return;
    }
  }

  _lastNodeIds = nodeIds;
  let totalModels = 0, onlineCount = 0;
  container.innerHTML = nodes.map((node, idx) => {
    const status = node.status;
    if (status === 'online') onlineCount++;
    const cpu = node.cpu ? node.cpu.utilization_pct : 0;
    const memUsed = node.memory ? node.memory.used_gb : 0;
    const memTotal = node.memory ? node.memory.total_gb : node.hardware.memory_total_gb;
    const memPct = memTotal > 0 ? (memUsed / memTotal) * 100 : 0;
    const pressure = node.memory ? node.memory.pressure : 'normal';
    const models = node.ollama ? node.ollama.models_loaded : [];
    const visEmbedCount = node.vision_embed_models ? node.vision_embed_models.length : 0;
    totalModels += models.length + visEmbedCount;
    const availCount = (node.ollama ? node.ollama.models_available_count : 0) + visEmbedCount;
    const modelsHtml = models.length > 0
      ? models.map(m => {
          const meta = m.parameter_size ? m.parameter_size + (m.quantization ? ' ' + m.quantization : '') : formatGB(m.size_gb);
          const ctx = m.context_length ? ' · ' + (m.context_length >= 1024 ? Math.round(m.context_length/1024) + 'K ctx' : m.context_length + ' ctx') : '';
          const nm = m.name.toLowerCase();
          const typeClass = (nm.includes('embed') || nm.includes('nomic') || nm.includes('bge')) ? 'embed'
            : (nm.includes('image') || nm.includes('flux') || nm.includes('diffusion') || nm.startsWith('x/') || nm.startsWith('sd')) ? 'image'
            : (nm.includes('asr') || nm.includes('whisper')) ? 'stt' : '';
          return `<span class="model-chip ${typeClass} hot">${m.name} <span class="size">${meta}${ctx}</span></span>`;
        }).join('')
      : '<span style="color:var(--text-dim);font-size:12px">No models loaded</span>';
    // Capacity learner panel (only for nodes with adaptive capacity)
    const cap = node.capacity;
    let capacityHtml = '';
    if (cap) {
      const scoreColor = barColor(100 - cap.availability_score * 100);  // Invert: high availability = green
      const modeLabels = {
        full: 'Full Capacity', learned_high: 'High Avail', learned_medium: 'Medium Avail',
        learned_low: 'Low Avail', paused: 'Paused', bootstrap: 'Learning...'
      };
      const modeLabel = modeLabels[cap.mode] || cap.mode;
      const modeBadgeClass = cap.mode === 'paused' ? 'offline' : cap.mode === 'bootstrap' ? 'degraded' : 'online';
      const reasonText = cap.reason.replace(/_/g, ' ');
      capacityHtml = `
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
            <div class="label" style="font-size:11px;color:var(--text-dim)">Adaptive Capacity</div>
            <span class="badge ${modeBadgeClass}">${modeLabel}</span>
          </div>
          <div class="metrics-row">
            <div class="metric">
              <div class="label">Availability</div>
              <div class="value" style="color:${scoreColor}">${(cap.availability_score * 100).toFixed(0)}%</div>
              <div class="bar-container"><div class="bar-fill" style="width:${cap.availability_score * 100}%;background:${scoreColor}"></div></div>
            </div>
            <div class="metric">
              <div class="label">Ceiling</div>
              <div class="value">${cap.ceiling_gb > 0 ? formatGB(cap.ceiling_gb) : 'None'}</div>
            </div>
            <div class="metric">
              <div class="label">Confidence</div>
              <div class="value">${(cap.learning_confidence * 100).toFixed(0)}%</div>
            </div>
          </div>
          <div style="font-size:11px;color:var(--text-dim);margin-top:4px">
            ${reasonText}${cap.override_active ? ' (override)' : ''} · ${cap.days_observed}d observed
          </div>
        </div>`;
    }
    const safeId = 'node-' + node.node_id.replace(/[^a-zA-Z0-9]/g, '-');
    return `
      <div class="card card-animate" id="${safeId}" style="animation-delay:${idx * 60}ms">
        <div class="card-header">
          <h3><span class="status-dot ${status}"></span>${node.node_id}</h3>
          <span class="badge ${status}">${status}</span>
        </div>
        <div class="metrics-row">
          <div class="metric">
            <div class="label">CPU</div>
            <div class="value cpu-val">${cpu.toFixed(1)}%</div>
            <div class="bar-container"><div class="bar-fill cpu-bar" style="width:${cpu}%;background:${barColor(cpu)}"></div></div>
          </div>
          <div class="metric">
            <div class="label">Memory (${pressure})</div>
            <div class="value mem-val">${formatGB(memUsed)} / ${formatGB(memTotal)}</div>
            <div class="bar-container"><div class="bar-fill mem-bar" style="width:${memPct}%;background:${barColor(memPct)}"></div></div>
          </div>
          <div class="metric">
            <div class="label">Cores</div>
            <div class="value">${node.hardware.cores_physical}</div>
          </div>
        </div>
        <div class="models-list">
          <div class="label" style="font-size:11px;color:var(--text-dim);margin-bottom:6px">
            Models (${models.length + (node.vision_embed_models ? node.vision_embed_models.length : 0)} loaded, ${availCount} on disk)
          </div>
          ${modelsHtml}
        </div>
        ${(node.image_models && node.image_models.length) || (node.stt_models && node.stt_models.length) || (node.embed_models && node.embed_models.length) || (node.vision_embed_models && node.vision_embed_models.length) ? '<div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">' + ((node.image_models || []).map(m => '<span class="badge" style="background:rgba(249,115,22,0.15);color:var(--orange);font-size:10px">IMG ' + m + '</span>').join('')) + ((node.stt_models || []).map(m => '<span class="badge" style="background:rgba(59,130,246,0.15);color:var(--blue);font-size:10px">STT ' + m + '</span>').join('')) + ((node.embed_models || []).map(m => '<span class="badge" style="background:rgba(168,85,247,0.15);color:var(--purple,#a855f7);font-size:10px">EMBED ' + m + '</span>').join('')) + ((node.vision_embed_models || []).map(m => '<span class="badge" style="background:rgba(6,182,212,0.15);color:var(--cyan,#06b6d4);font-size:10px">VIS ' + m + '</span>').join('')) + '</div>' : ''}
        ${capacityHtml}
      </div>`;
  }).join('');
  // Init header stats with IDs on first render, update values on subsequent
  var hs = document.getElementById('header-stats');
  if (!document.getElementById('stat-nodes')) {
    hs.innerHTML = `
      <div class="header-stat"><div class="value" id="stat-nodes">${onlineCount}</div><div class="label">Nodes</div></div>
      <div class="header-stat"><div class="value" id="stat-models">${totalModels}</div><div class="label">Models Loaded</div></div>
      <div class="header-stat"><div class="value" id="stat-queued">0</div><div class="label">Queued</div></div>
      <div class="header-stat"><div class="value" id="stat-completed">0</div><div class="label">Completed</div></div>
    `;
  } else {
    document.getElementById('stat-nodes').textContent = onlineCount;
    document.getElementById('stat-models').textContent = totalModels;
  }
}

function renderQueues(queues) {
  const container = document.getElementById('queues-container');
  const entries = Object.entries(queues);
  if (!entries.length) {
    container.innerHTML = '<div class="empty-state">No active queues</div>';
    return;
  }
  let totalQueued = 0, totalCompleted = 0;
  container.innerHTML = entries.map(([key, q]) => {
    totalQueued += q.pending + q.in_flight;
    totalCompleted += q.completed;
    const pendingColor = q.pending > 3 ? 'var(--orange)' : q.pending > 0 ? 'var(--yellow)' : 'var(--text-dim)';
    const inflightColor = q.in_flight > 0 ? 'var(--blue)' : 'var(--text-dim)';
    const typeColors = {text:'var(--accent)',image:'var(--orange)',stt:'var(--blue)',embed:'var(--purple,#a855f7)'};
    const typeLabels = {text:'TEXT',image:'IMAGE',stt:'STT',embed:'EMBED'};
    const rt = q.request_type || 'text';
    return `
      <div class="queue-card">
        <div class="queue-name"><span style="display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;letter-spacing:0.5px;margin-right:6px;background:${typeColors[rt]}22;color:${typeColors[rt]}">${typeLabels[rt]}</span>${key}</div>
        <div class="queue-stats">
          <div class="queue-stat"><div class="num" style="color:${pendingColor}">${q.pending}</div><div class="lbl">Pending</div></div>
          <div class="queue-stat"><div class="num" style="color:${inflightColor}">${q.in_flight}/${q.concurrency || 1}</div><div class="lbl">In-Flight</div></div>
          <div class="queue-stat"><div class="num" style="color:var(--green)">${q.completed}</div><div class="lbl">Done</div></div>
          <div class="queue-stat"><div class="num" style="color:var(--red)">${q.failed || 0}</div><div class="lbl">Failed</div></div>
        </div>
      </div>`;
  }).join('');
  var sq = document.getElementById('stat-queued');
  var sc = document.getElementById('stat-completed');
  if (sq) sq.textContent = totalQueued;
  if (sc) sc.textContent = totalCompleted;
}

var _sseWatchdog = null;
function connect() {
  const es = new EventSource('/dashboard/events');
  function resetWatchdog() {
    clearTimeout(_sseWatchdog);
    _sseWatchdog = setTimeout(function() {
      console.warn('SSE watchdog: no event in 10s, reconnecting');
      es.close(); setTimeout(connect, 1000);
    }, 10000);
  }
  es.onopen = () => {
    var d = document.getElementById('sse-dot');
    var s = document.getElementById('sse-status');
    if (d) d.className = 'status-dot online pulse';
    if (s) s.textContent = 'Live';
    resetWatchdog();
  };
  es.onmessage = (e) => {
    resetWatchdog();
    try { const data = JSON.parse(e.data); renderNodes(data.nodes); renderQueues(data.queues); }
    catch (err) { console.error('Parse error:', err); }
  };
  es.onerror = () => {
    clearTimeout(_sseWatchdog);
    var d = document.getElementById('sse-dot');
    var s = document.getElementById('sse-status');
    if (d) d.className = 'status-dot offline';
    if (s) s.textContent = 'Reconnecting...';
    es.close(); setTimeout(connect, 3000);
  };
}
connect();

// Fleet Intelligence Briefing
var briefingDismissed = false;
function dismissBriefing() {
  briefingDismissed = true;
  document.getElementById('briefing-section').style.display = 'none';
}
async function fetchBriefing(force) {
  if (briefingDismissed && !force) return;
  if (force) briefingDismissed = false;
  try {
    const url = '/dashboard/api/briefing' + (force ? '?refresh=1' : '');
    const btn = document.getElementById('briefing-refresh');
    if (force && btn) btn.textContent = 'Generating...';
    const resp = await fetch(url);
    const data = await resp.json();
    const section = document.getElementById('briefing-section');
    if (!data.enabled) { section.style.display = 'none'; return; }
    section.style.display = 'block';
    const content = document.getElementById('briefing-content');
    const meta = document.getElementById('briefing-meta');
    if (btn) btn.textContent = 'Refresh';
    if (data.generating) {
      content.innerHTML = '<span style="color:var(--text-dim)">Generating fleet briefing...</span>';
      return;
    }
    if (data.briefing) {
      // Convert newlines to <br> and basic markdown
      var lines = data.briefing.split(String.fromCharCode(10));
      var html = lines.map(function(line) {
        // Bold: **text** -> <strong>text</strong>
        while (line.indexOf('**') !== -1) {
          var i = line.indexOf('**');
          var j = line.indexOf('**', i + 2);
          if (j === -1) break;
          line = line.substring(0, i) + '<strong>' + line.substring(i+2, j) + '</strong>' + line.substring(j+2);
        }
        // Bullet: lines starting with - or *
        var trimmed = line.trimStart();
        if (trimmed.charAt(0) === '-' || trimmed.charAt(0) === '*') {
          line = '&bull; ' + trimmed.substring(1).trimStart();
        }
        return line;
      }).join('<br>');
      content.innerHTML = html;
      const ago = data.generated_at ? Math.round((Date.now()/1000 - data.generated_at) / 60) : 0;
      const agoText = ago < 1 ? 'just now' : ago + 'm ago';
      const nextText = data.next_refresh_s ? Math.round(data.next_refresh_s / 60) + 'm' : '?';
      meta.innerHTML = `<span>Generated ${agoText} using <code style="font-size:11px;background:var(--border);padding:1px 5px;border-radius:3px">${data.model || '?'}</code></span><span>Next update in ~${nextText}</span>`;
    } else if (data.error) {
      content.innerHTML = '<span style="color:var(--text-dim)">' + data.error + '</span>';
      meta.innerHTML = '';
    }
  } catch(e) { /* silently retry next poll */ }
}
fetchBriefing();
setInterval(fetchBriefing, 30000);
</script>
"""

# ---------------------------------------------------------------------------
# Historical Trends page body
# ---------------------------------------------------------------------------

_TRENDS_BODY = """
<div class="main">
  <div class="summary-cards" id="summary-cards"></div>

  <div>
    <div id="trends-time-range"></div>

    <div class="charts-row">
      <div class="chart-card">
        <h4>Requests per Hour</h4>
        <canvas id="requests-chart"></canvas>
      </div>
      <div class="chart-card">
        <h4>Average Latency per Hour</h4>
        <canvas id="latency-chart"></canvas>
      </div>
    </div>
    <div class="charts-row" style="margin-top:16px">
      <div class="chart-card full-width">
        <h4>Token Throughput per Hour</h4>
        <canvas id="tokens-chart"></canvas>
      </div>
    </div>
  </div>
</div>

<script>
const cs = getComputedStyle(document.documentElement);
const C = {
  accent: cs.getPropertyValue('--accent').trim(),
  blue: cs.getPropertyValue('--blue').trim(),
  green: cs.getPropertyValue('--green').trim(),
  border: cs.getPropertyValue('--border').trim(),
  textDim: cs.getPropertyValue('--text-dim').trim(),
  text: cs.getPropertyValue('--text').trim(),
  card: cs.getPropertyValue('--card').trim(),
};
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: { legend: { labels: { color: C.textDim, font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: C.textDim, font: { size: 10 }, maxRotation: 45 }, grid: { color: C.border } },
    y: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border }, beginAtZero: true },
  },
};

let requestsChart, latencyChart, tokensChart;

function fmtNum(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toString();
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], {month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
}

async function loadTrends(startTs, endTs) {
  const [trendsResp, overviewResp] = await Promise.all([
    fetch('/dashboard/api/trends?start_ts=' + startTs + '&end_ts=' + endTs),
    fetch('/dashboard/api/overview'),
  ]);
  const trends = await trendsResp.json();
  const overview = await overviewResp.json();

  // Summary cards
  const avgLat = trends.data.length > 0
    ? Math.round(trends.data.reduce((s,d) => s + d.avg_latency_ms, 0) / trends.data.length)
    : 0;
  document.getElementById('summary-cards').innerHTML = `
    <div class="summary-card"><div class="sc-value">${fmtNum(overview.total_requests)}</div><div class="sc-label">Total Requests</div></div>
    <div class="summary-card"><div class="sc-value">${avgLat > 0 ? (avgLat/1000).toFixed(1)+'s' : '-'}</div><div class="sc-label">Avg Latency</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(overview.total_prompt_tokens)}</div><div class="sc-label">Tokens In</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(overview.total_completion_tokens)}</div><div class="sc-label">Tokens Out</div></div>
  `;

  const labels = trends.data.map(d => fmtTime(d.hour_bucket));
  const requests = trends.data.map(d => d.request_count);
  const latencies = trends.data.map(d => d.avg_latency_ms / 1000);
  const promptTok = trends.data.map(d => d.total_prompt_tokens);
  const completionTok = trends.data.map(d => d.total_completion_tokens);

  // Destroy old charts
  if (requestsChart) requestsChart.destroy();
  if (latencyChart) latencyChart.destroy();
  if (tokensChart) tokensChart.destroy();

  requestsChart = new Chart(document.getElementById('requests-chart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Requests', data: requests, backgroundColor: C.accent + '99', borderColor: C.accent, borderWidth: 1 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  latencyChart = new Chart(document.getElementById('latency-chart'), {
    type: 'line',
    data: { labels, datasets: [{ label: 'Avg Latency (s)', data: latencies, borderColor: C.blue, backgroundColor: C.blue + '22', fill: true, tension: 0.3, pointRadius: 2 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  tokensChart = new Chart(document.getElementById('tokens-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Prompt Tokens', data: promptTok, backgroundColor: C.blue + '99' },
        { label: 'Completion Tokens', data: completionTok, backgroundColor: C.green + '99' },
      ],
    },
    options: {
      ...chartDefaults,
      scales: { ...chartDefaults.scales, x: { ...chartDefaults.scales.x, stacked: true }, y: { ...chartDefaults.scales.y, stacked: true } },
    },
  });
}

// Init time range and load
initTimeRange('trends-time-range', loadTrends, '72h');

window.addEventListener('DOMContentLoaded', () => {
  const dot = document.getElementById('sse-dot');
  const st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});
</script>
"""

# ---------------------------------------------------------------------------
# Model Insights page body
# ---------------------------------------------------------------------------

_MODELS_BODY = """
<div class="main">
  <div id="models-time-range"></div>
  <div class="summary-cards" id="model-summary-cards"></div>

  <div class="charts-row">
    <div class="chart-card">
      <h4>Avg Latency by Model</h4>
      <canvas id="comparison-chart"></canvas>
    </div>
    <div class="chart-card">
      <h4>Token Distribution</h4>
      <canvas id="token-dist-chart"></canvas>
    </div>
  </div>

  <div class="card" style="margin-top:16px;padding:0;overflow:hidden">
    <table class="model-table" id="model-table">
      <thead>
        <tr>
          <th>Model</th>
          <th>Requests</th>
          <th>Avg Latency</th>
          <th>Tokens/sec</th>
          <th>Prompt Tokens</th>
          <th>Completion Tokens</th>
          <th>Last Seen</th>
        </tr>
      </thead>
      <tbody id="model-tbody"></tbody>
    </table>
  </div>

  <div class="charts-row" style="margin-top:16px" id="daily-section" hidden>
    <div class="chart-card">
      <h4 id="daily-title">Daily Requests</h4>
      <canvas id="daily-requests-chart"></canvas>
    </div>
    <div class="chart-card">
      <h4>Daily Avg Latency</h4>
      <canvas id="daily-latency-chart"></canvas>
    </div>
  </div>
</div>

<script>
const cs = getComputedStyle(document.documentElement);
const C = {
  accent: cs.getPropertyValue('--accent').trim(),
  blue: cs.getPropertyValue('--blue').trim(),
  green: cs.getPropertyValue('--green').trim(),
  orange: cs.getPropertyValue('--orange').trim(),
  red: cs.getPropertyValue('--red').trim(),
  border: cs.getPropertyValue('--border').trim(),
  textDim: cs.getPropertyValue('--text-dim').trim(),
  text: cs.getPropertyValue('--text').trim(),
};
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: { legend: { labels: { color: C.textDim, font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border } },
    y: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border }, beginAtZero: true },
  },
};
const MODEL_COLORS = [C.accent, C.blue, C.green, C.orange, C.red, '#a855f7', '#ec4899', '#14b8a6', '#f59e0b', '#8b5cf6'];

let comparisonChart, tokenDistChart, dailyReqChart, dailyLatChart;
let allDaily = [];

function fmtNum(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toString();
}

function timeAgo(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

function fmtDay(ts) {
  return new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

var _modelsStartTs = 0, _modelsEndTs = 0;
async function loadModels(startTs, endTs) {
  if (startTs) _modelsStartTs = startTs;
  if (endTs) _modelsEndTs = endTs;
  var url = '/dashboard/api/models?days=7';
  if (_modelsStartTs && _modelsEndTs) url = '/dashboard/api/models?start_ts=' + _modelsStartTs + '&end_ts=' + _modelsEndTs;
  const resp = await fetch(url);
  const data = await resp.json();
  const summary = data.summary;
  allDaily = data.daily;

  // Summary cards
  const totalReqs = summary.reduce((s, m) => s + m.total_requests, 0);
  const totalPrompt = summary.reduce((s, m) => s + m.total_prompt_tokens, 0);
  const totalCompletion = summary.reduce((s, m) => s + m.total_completion_tokens, 0);
  const avgLat = summary.length > 0
    ? Math.round(summary.reduce((s, m) => s + m.avg_latency_ms * m.total_requests, 0) / (totalReqs || 1))
    : 0;

  document.getElementById('model-summary-cards').innerHTML = `
    <div class="summary-card"><div class="sc-value">${summary.length}</div><div class="sc-label">Models</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(totalReqs)}</div><div class="sc-label">Total Requests</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(totalPrompt + totalCompletion)}</div><div class="sc-label">Total Tokens</div></div>
    <div class="summary-card"><div class="sc-value">${avgLat > 0 ? (avgLat/1000).toFixed(1)+'s' : '-'}</div><div class="sc-label">Avg Latency</div></div>
  `;

  // Table
  const tbody = document.getElementById('model-tbody');
  tbody.innerHTML = summary.map((m, i) => {
    const tokPerSec = m.avg_latency_ms > 0 && m.total_completion_tokens > 0
      ? ((m.total_completion_tokens / m.total_requests) / (m.avg_latency_ms / 1000)).toFixed(1)
      : '-';
    return `<tr data-model="${m.model_name}" onclick="selectModel('${m.model_name}', this)">
      <td class="model-name">${m.model_name}</td>
      <td>${fmtNum(m.total_requests)}</td>
      <td>${(m.avg_latency_ms / 1000).toFixed(1)}s</td>
      <td>${tokPerSec}</td>
      <td>${fmtNum(m.total_prompt_tokens)}</td>
      <td>${fmtNum(m.total_completion_tokens)}</td>
      <td style="color:${C.textDim}">${m.last_seen ? timeAgo(m.last_seen) : '-'}</td>
    </tr>`;
  }).join('');

  // Comparison chart
  if (comparisonChart) comparisonChart.destroy();
  comparisonChart = new Chart(document.getElementById('comparison-chart'), {
    type: 'bar',
    data: {
      labels: summary.map(m => m.model_name),
      datasets: [{
        label: 'Avg Latency (s)',
        data: summary.map(m => m.avg_latency_ms / 1000),
        backgroundColor: summary.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length] + '99'),
        borderColor: summary.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length]),
        borderWidth: 1,
      }],
    },
    options: { ...chartDefaults, indexAxis: 'y', plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  // Token distribution
  if (tokenDistChart) tokenDistChart.destroy();
  const totalTok = summary.map(m => m.total_prompt_tokens + m.total_completion_tokens);
  tokenDistChart = new Chart(document.getElementById('token-dist-chart'), {
    type: 'doughnut',
    data: {
      labels: summary.map(m => m.model_name),
      datasets: [{
        data: totalTok,
        backgroundColor: summary.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length] + 'cc'),
        borderColor: 'transparent',
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: 'right', labels: { color: C.textDim, font: { size: 11 }, padding: 12 } },
      },
    },
  });

  // Auto-select first model
  if (summary.length > 0) {
    const firstRow = tbody.querySelector('tr');
    if (firstRow) selectModel(summary[0].model_name, firstRow);
  }
}

function selectModel(modelName, row) {
  document.querySelectorAll('.model-table tr.selected').forEach(r => r.classList.remove('selected'));
  if (row) row.classList.add('selected');

  const filtered = allDaily.filter(d => d.model_name === modelName);
  if (filtered.length === 0) {
    document.getElementById('daily-section').hidden = true;
    return;
  }

  document.getElementById('daily-section').hidden = false;
  document.getElementById('daily-title').textContent = 'Daily Requests — ' + modelName;

  const labels = filtered.map(d => fmtDay(d.day_bucket));
  const requests = filtered.map(d => d.request_count);
  const latencies = filtered.map(d => d.avg_latency_ms / 1000);

  if (dailyReqChart) dailyReqChart.destroy();
  if (dailyLatChart) dailyLatChart.destroy();

  dailyReqChart = new Chart(document.getElementById('daily-requests-chart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Requests', data: requests, backgroundColor: C.accent + '99', borderColor: C.accent, borderWidth: 1 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  dailyLatChart = new Chart(document.getElementById('daily-latency-chart'), {
    type: 'line',
    data: { labels, datasets: [{ label: 'Avg Latency (s)', data: latencies, borderColor: C.blue, backgroundColor: C.blue + '22', fill: true, tension: 0.3, pointRadius: 3 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });
}

window.addEventListener('DOMContentLoaded', () => {
  const dot = document.getElementById('sse-dot');
  const st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

initTimeRange('models-time-range', loadModels, '7d');
</script>
"""


# ---------------------------------------------------------------------------
# Apps analytics page body
# ---------------------------------------------------------------------------

_TAGS_BODY = """
<div class="main">
  <div id="tags-time-range"></div>
  <div class="summary-cards" id="tags-summary-cards">
    <div class="card summary-card">
      <div class="card-label">Tagged Requests</div>
      <div class="card-value" id="tagged-count">-</div>
    </div>
    <div class="card summary-card">
      <div class="card-label">Unique Tags</div>
      <div class="card-value" id="unique-tags">-</div>
    </div>
    <div class="card summary-card">
      <div class="card-label">Top Tag</div>
      <div class="card-value" id="top-tag">-</div>
    </div>
  </div>

  <div class="charts-row">
    <div class="chart-card">
      <h4>Requests by Tag</h4>
      <canvas id="tag-bar-chart"></canvas>
    </div>
    <div class="chart-card">
      <h4>Tag Activity Over Time</h4>
      <canvas id="tag-daily-chart"></canvas>
    </div>
  </div>

  <div class="card" style="margin-top:16px;padding:0;overflow:hidden">
    <table class="model-table" id="apps-table">
      <thead>
        <tr>
          <th>Tag</th>
          <th>Requests</th>
          <th>Avg Latency</th>
          <th>Avg TTFT</th>
          <th>Prompt Tokens</th>
          <th>Completion Tokens</th>
          <th>Error Rate</th>
        </tr>
      </thead>
      <tbody id="apps-tbody"></tbody>
    </table>
  </div>

  <div class="charts-row" style="margin-top:16px" id="tag-daily-section" hidden>
    <div class="chart-card">
      <h4 id="tag-daily-title">Daily Requests</h4>
      <canvas id="tag-daily-requests-chart"></canvas>
    </div>
    <div class="chart-card">
      <h4>Daily Avg Latency</h4>
      <canvas id="tag-daily-latency-chart"></canvas>
    </div>
  </div>

  <div class="card" style="margin-top:24px;padding:16px">
    <h4 style="margin:0 0 8px">How to tag requests</h4>
    <p style="color:var(--text-dim);margin:0 0 12px;font-size:13px">
      Add tags to your requests so they appear here. Tags are stripped before reaching Ollama.
    </p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px">
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px">
        <div style="font-weight:600;margin-bottom:6px;font-size:13px">OpenAI SDK (Python)</div>
        <pre style="margin:0;font-size:12px;overflow-x:auto;color:var(--text-dim)">client.chat.completions.create(
  model="llama3.2:3b",
  messages=[...],
  extra_body={"metadata": {"tags": ["my-app"]}}
)</pre>
      </div>
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px">
        <div style="font-weight:600;margin-bottom:6px;font-size:13px">Header-based</div>
        <pre style="margin:0;font-size:12px;overflow-x:auto;color:var(--text-dim)">curl http://router:11435/api/chat \\
  -H "X-Herd-Tags: my-app, prod" \\
  -d '{"model": "...", "messages": [...]}'</pre>
      </div>
    </div>
  </div>
</div>

<script>
const cs = getComputedStyle(document.documentElement);
const C = {
  accent: cs.getPropertyValue('--accent').trim(),
  blue: cs.getPropertyValue('--blue').trim(),
  green: cs.getPropertyValue('--green').trim(),
  orange: cs.getPropertyValue('--orange').trim(),
  red: cs.getPropertyValue('--red').trim(),
  border: cs.getPropertyValue('--border').trim(),
  textDim: cs.getPropertyValue('--text-dim').trim(),
  text: cs.getPropertyValue('--text').trim(),
};
const TAG_COLORS = [C.accent, C.blue, C.green, C.orange, C.red, '#e879f9', '#22d3ee', '#a3e635'];
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: { legend: { labels: { color: C.textDim, font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border + '44' } },
    y: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border + '44' } },
  },
};

let barChart, dailyChart, dailyReqChart, dailyLatChart;

function fmtMs(ms) { return ms > 1000 ? (ms/1000).toFixed(1) + 's' : Math.round(ms) + 'ms'; }

var _tagsStartTs = 0, _tagsEndTs = 0;
async function loadTags(startTs, endTs) {
  if (startTs) _tagsStartTs = startTs;
  if (endTs) _tagsEndTs = endTs;
  var qs = _tagsStartTs && _tagsEndTs ? 'start_ts=' + _tagsStartTs + '&end_ts=' + _tagsEndTs : 'days=7';
  const [appsRes, dailyRes] = await Promise.all([
    fetch('/dashboard/api/tags?' + qs),
    fetch('/dashboard/api/tags/daily?' + qs),
  ]);
  const apps = await appsRes.json();
  const daily = await dailyRes.json();

  const data = apps.data || [];
  const dailyData = daily.data || [];

  // Summary cards
  const totalReqs = data.reduce((s, d) => s + d.request_count, 0);
  document.getElementById('tagged-count').textContent = totalReqs.toLocaleString();
  document.getElementById('unique-tags').textContent = data.length;
  document.getElementById('top-tag').textContent = data.length > 0 ? data[0].tag : 'None';

  // Bar chart — requests per tag
  if (barChart) barChart.destroy();
  const tags = data.map(d => d.tag);
  const counts = data.map(d => d.request_count);
  const colors = tags.map((_, i) => TAG_COLORS[i % TAG_COLORS.length]);
  barChart = new Chart(document.getElementById('tag-bar-chart'), {
    type: 'bar',
    data: {
      labels: tags,
      datasets: [{ label: 'Requests', data: counts, backgroundColor: colors.map(c => c + '99'), borderColor: colors, borderWidth: 1 }],
    },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  // Line chart — daily activity per tag
  if (dailyChart) dailyChart.destroy();
  const uniqueTags = [...new Set(dailyData.map(d => d.tag))];
  const uniqueDays = [...new Set(dailyData.map(d => d.day_bucket))].sort();
  const dayLabels = uniqueDays.map(d => new Date(d * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }));
  const datasets = uniqueTags.map((tag, i) => {
    const tagData = uniqueDays.map(day => {
      const found = dailyData.find(d => d.tag === tag && d.day_bucket === day);
      return found ? found.request_count : 0;
    });
    const color = TAG_COLORS[i % TAG_COLORS.length];
    return { label: tag, data: tagData, borderColor: color, backgroundColor: color + '22', fill: false, tension: 0.3, pointRadius: 3 };
  });
  dailyChart = new Chart(document.getElementById('tag-daily-chart'), {
    type: 'line',
    data: { labels: dayLabels, datasets },
    options: chartDefaults,
  });

  // Table
  const tbody = document.getElementById('apps-tbody');
  tbody.innerHTML = '';
  data.forEach((d, i) => {
    const errorRate = d.request_count > 0 ? ((d.failed_count / d.request_count) * 100).toFixed(1) : '0.0';
    const row = document.createElement('tr');
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => showTagDaily(d.tag, dailyData));
    const dot = `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${TAG_COLORS[i % TAG_COLORS.length]};margin-right:6px;vertical-align:middle"></span>`;
    row.innerHTML = `
      <td>${dot}${d.tag}</td>
      <td>${d.request_count.toLocaleString()}</td>
      <td>${d.avg_latency_ms ? fmtMs(d.avg_latency_ms) : '-'}</td>
      <td>${d.avg_ttft_ms ? fmtMs(d.avg_ttft_ms) : '-'}</td>
      <td>${(d.total_prompt_tokens || 0).toLocaleString()}</td>
      <td>${(d.total_completion_tokens || 0).toLocaleString()}</td>
      <td style="color:${parseFloat(errorRate) > 5 ? C.red : C.textDim}">${errorRate}%</td>
    `;
    tbody.appendChild(row);
  });
}

function showTagDaily(tag, dailyData) {
  const section = document.getElementById('tag-daily-section');
  section.hidden = false;
  document.getElementById('tag-daily-title').textContent = `Daily Requests — ${tag}`;
  section.scrollIntoView({ behavior: 'smooth' });

  const tagDaily = dailyData.filter(d => d.tag === tag).sort((a, b) => a.day_bucket - b.day_bucket);
  const labels = tagDaily.map(d => new Date(d.day_bucket * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }));
  const requests = tagDaily.map(d => d.request_count);
  const latencies = tagDaily.map(d => d.avg_latency_ms ? d.avg_latency_ms / 1000 : 0);

  if (dailyReqChart) dailyReqChart.destroy();
  if (dailyLatChart) dailyLatChart.destroy();

  dailyReqChart = new Chart(document.getElementById('tag-daily-requests-chart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Requests', data: requests, backgroundColor: C.accent + '99', borderColor: C.accent, borderWidth: 1 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  dailyLatChart = new Chart(document.getElementById('tag-daily-latency-chart'), {
    type: 'line',
    data: { labels, datasets: [{ label: 'Avg Latency (s)', data: latencies, borderColor: C.blue, backgroundColor: C.blue + '22', fill: true, tension: 0.3, pointRadius: 3 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });
}

window.addEventListener('DOMContentLoaded', () => {
  const dot = document.getElementById('sse-dot');
  const st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

initTimeRange('tags-time-range', loadTags, '7d');
</script>
"""


# ---------------------------------------------------------------------------
# Benchmarks page body
# ---------------------------------------------------------------------------

_BENCHMARKS_BODY = """
<style>
.bench-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
.bench-stat { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; text-align: center; }
.bench-stat .value { font-size: 28px; font-weight: 700; color: var(--accent); }
.bench-stat .label { font-size: 12px; color: var(--text-dim); margin-top: 4px; }
.bench-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.bench-table th { text-align: left; padding: 10px 12px; border-bottom: 2px solid var(--border); color: var(--text-dim); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.bench-table td { padding: 10px 12px; border-bottom: 1px solid var(--border); }
.bench-table tr:hover { background: rgba(108,99,255,0.05); }
.bench-table tr { cursor: pointer; }
.bench-detail { display: none; }
.bench-detail.open { display: table-row; }
.bench-detail td { padding: 12px 16px; background: rgba(108,99,255,0.03); }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.detail-section h5 { margin: 0 0 8px 0; font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
.detail-table { width: 100%; font-size: 12px; border-collapse: collapse; }
.detail-table th, .detail-table td { padding: 4px 8px; text-align: left; }
.detail-table th { color: var(--text-dim); font-weight: 500; border-bottom: 1px solid var(--border); }
.detail-table td { border-bottom: 1px solid rgba(255,255,255,0.03); }
.empty-state { text-align: center; padding: 60px 20px; color: var(--text-dim); }
.empty-state h3 { color: var(--text); margin-bottom: 8px; }
.empty-state code { background: rgba(108,99,255,0.15); padding: 2px 8px; border-radius: 4px; font-size: 13px; }
.bench-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:20px; }
.bench-header h3 { margin:0; font-size:16px; font-weight:600; }
.run-btn { background:var(--accent); color:#fff; border:none; border-radius:8px; padding:10px 20px; cursor:pointer; font-size:13px; font-weight:600; display:flex; align-items:center; gap:8px; }
.run-btn:hover { opacity:0.9; }
.run-btn:disabled { opacity:0.5; cursor:not-allowed; }
.bench-overlay {
  position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.6);
  display:flex; align-items:center; justify-content:center; z-index:1000;
}
.bench-dialog {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:24px; max-width:500px; width:90%;
}
.bench-dialog h3 { margin:0 0 16px; font-size:16px; }
.mode-option { background:rgba(108,99,255,0.05); border:2px solid var(--border); border-radius:10px; padding:14px; margin-bottom:10px; cursor:pointer; transition:border-color 0.2s; }
.mode-option:hover { border-color:var(--accent); }
.mode-option.selected { border-color:var(--accent); background:rgba(108,99,255,0.1); }
.mode-option h4 { margin:0 0 4px; font-size:14px; }
.mode-option p { margin:0; font-size:12px; color:var(--text-dim); line-height:1.4; }
.duration-row { display:flex; gap:8px; margin:14px 0; align-items:center; }
.duration-row label { font-size:12px; color:var(--text-dim); }
.dur-btn { background:var(--border); color:var(--text); border:none; border-radius:6px; padding:6px 14px; cursor:pointer; font-size:12px; }
.dur-btn.selected { background:var(--accent); color:#fff; }
.dur-btn:hover { opacity:0.85; }
.bench-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
.bench-cancel-btn { background:var(--border); color:var(--text); border:none; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:13px; }
.bench-start-btn { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:13px; font-weight:500; }
.bench-start-btn:hover { opacity:0.85; }
.type-checks { margin:14px 0; }
.type-checks label { font-size:12px; color:var(--text-dim); margin-bottom:6px; display:block; }
.type-checks-row { display:flex; gap:12px; flex-wrap:wrap; }
.type-check { display:flex; align-items:center; gap:6px; background:rgba(108,99,255,0.05); border:1px solid var(--border); border-radius:6px; padding:6px 12px; cursor:pointer; font-size:12px; }
.type-check input { accent-color:var(--accent); cursor:pointer; }
.type-check.disabled { opacity:0.4; cursor:not-allowed; }
.type-check.disabled input { cursor:not-allowed; }
.progress-bar-container { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:20px; }
.progress-bar-container h4 { margin:0 0 8px; font-size:14px; }
.progress-bar-container .phase { font-size:12px; color:var(--text-dim); margin-bottom:6px; }
.progress-bar { background:var(--border); border-radius:4px; height:8px; overflow:hidden; margin-bottom:8px; }
.progress-bar .fill { height:100%; border-radius:4px; transition:width 0.5s ease, background 0.5s ease; }
.progress-bar-label { font-size:11px; color:var(--text-dim); margin-bottom:4px; display:flex; justify-content:space-between; }
.progress-stats { display:flex; gap:20px; font-size:12px; color:var(--text-dim); }
.progress-stats .val { color:var(--text); font-weight:600; }
.cancel-running { background:var(--red); color:#fff; border:none; border-radius:6px; padding:6px 14px; cursor:pointer; font-size:12px; margin-top:8px; }
@media (max-width: 768px) {
  .detail-grid { grid-template-columns: 1fr; }
  .bench-summary { grid-template-columns: 1fr 1fr; }
}
</style>

<div class="main">
  <div class="bench-header">
    <div></div>
    <button class="run-btn" id="run-bench-btn" onclick="showBenchDialog()">&#9654; Run Benchmark</button>
  </div>

  <div id="bench-progress" style="display:none"></div>

  <div class="bench-summary" id="bench-summary"></div>

  <div style="display:flex;gap:16px;margin-bottom:20px">
    <div class="chart-card" style="flex:1;position:relative;height:300px">
      <h4 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim)">Capacity Growth</h4>
      <div style="position:relative;height:calc(100% - 30px)">
        <canvas id="capacity-chart"></canvas>
      </div>
    </div>
    <div class="chart-card" style="flex:1;position:relative;height:300px">
      <h4 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim)">Throughput Trend</h4>
      <div style="position:relative;height:calc(100% - 30px)">
        <canvas id="throughput-chart"></canvas>
      </div>
    </div>
  </div>

  <div style="display:flex;gap:16px;margin-bottom:20px">
    <div class="chart-card" style="flex:1;position:relative;height:300px">
      <h4 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim)">Model Throughput <span style="font-weight:400;font-size:11px">(latest run)</span></h4>
      <div style="position:relative;height:calc(100% - 30px)">
        <canvas id="model-throughput-chart"></canvas>
      </div>
    </div>
    <div class="chart-card" style="flex:1;position:relative;height:300px">
      <h4 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim)">Model Latency <span style="font-weight:400;font-size:11px">(latest run)</span></h4>
      <div style="position:relative;height:calc(100% - 30px)">
        <canvas id="model-latency-chart"></canvas>
      </div>
    </div>
  </div>

  <div style="display:flex;gap:16px;margin-bottom:20px">
    <div class="chart-card" style="flex:1;position:relative;height:300px">
      <h4 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim)">Model Performance Over Time</h4>
      <div style="position:relative;height:calc(100% - 30px)">
        <canvas id="model-history-chart"></canvas>
      </div>
    </div>
    <div class="chart-card" style="flex:1;position:relative;height:300px">
      <h4 style="margin:0 0 12px 0;font-size:14px;color:var(--text-dim)">Node Utilization <span style="font-weight:400;font-size:11px">(latest run)</span></h4>
      <div style="position:relative;height:calc(100% - 30px)">
        <canvas id="node-util-chart"></canvas>
      </div>
    </div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <table class="bench-table" id="bench-table">
      <thead>
        <tr>
          <th>Date</th>
          <th>Duration</th>
          <th>Nodes</th>
          <th>Models</th>
          <th>Requests</th>
          <th>Tok/s</th>
          <th>Latency p50</th>
          <th>TTFT p50</th>
        </tr>
      </thead>
      <tbody id="bench-tbody"></tbody>
    </table>
    <div class="empty-state" id="empty-state" style="display:none">
      <h3>No benchmark runs yet</h3>
      <p>Run your first benchmark:</p>
      <p><code>python scripts/benchmark.py --duration 60</code></p>
    </div>
  </div>
</div>

<script>
const cs = getComputedStyle(document.documentElement);
const C = {
  accent: cs.getPropertyValue('--accent').trim(),
  blue: cs.getPropertyValue('--blue').trim(),
  green: cs.getPropertyValue('--green').trim() || '#22c55e',
  text: cs.getPropertyValue('--text').trim(),
  dim: cs.getPropertyValue('--text-dim').trim(),
  grid: cs.getPropertyValue('--border').trim(),
};
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  scales: {
    x: { grid: { color: C.grid + '33' }, ticks: { color: C.dim, font: { size: 11 } } },
    y: { grid: { color: C.grid + '33' }, ticks: { color: C.dim, font: { size: 11 } } },
  },
  plugins: { legend: { labels: { color: C.dim, font: { size: 11 } } } },
};

let capacityChart, throughputChart, modelThroughputChart, modelLatencyChart, modelHistoryChart, nodeUtilChart;

function fmtDate(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtShortDate(ts) {
  const d = new Date(ts * 1000);
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  return date + ' ' + time;
}
function fmtDuration(s) {
  if (s < 60) return s.toFixed(0) + 's';
  return (s / 60).toFixed(1) + 'm';
}
function fmtNum(n) { return n != null ? n.toLocaleString('en-US', { maximumFractionDigits: 1 }) : '-'; }
function fmtMs(ms) { return ms != null ? (ms / 1000).toFixed(1) + 's' : '-'; }

async function loadBenchmarks() {
  const resp = await fetch('/dashboard/api/benchmarks?limit=100');
  const { data } = await resp.json();

  const tbody = document.getElementById('bench-tbody');
  const empty = document.getElementById('empty-state');
  const summary = document.getElementById('bench-summary');

  if (!data || data.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    summary.innerHTML = '';
    return;
  }
  empty.style.display = 'none';

  // Summary cards
  const bestTokS = Math.max(...data.map(d => d.tokens_per_sec || 0));
  const latestNodes = data[0].fleet_snapshot ? data[0].fleet_snapshot.nodes || [] : [];
  const totalRuns = data.length;
  const totalTokens = data.reduce((s, d) => s + (d.total_prompt_tokens || 0) + (d.total_completion_tokens || 0), 0);

  summary.innerHTML = `
    <div class="bench-stat"><div class="value">${totalRuns}</div><div class="label">Total Runs</div></div>
    <div class="bench-stat"><div class="value">${fmtNum(bestTokS)}</div><div class="label">Best tok/s</div></div>
    <div class="bench-stat"><div class="value">${latestNodes.length}</div><div class="label">Fleet Nodes</div></div>
    <div class="bench-stat"><div class="value">${totalTokens.toLocaleString()}</div><div class="label">Total Tokens</div></div>
  `;

  // Table rows
  let rows = '';
  data.forEach((run, i) => {
    const fleet = run.fleet_snapshot || {};
    const nodes = fleet.nodes || [];
    const models = fleet.models || [];
    rows += `<tr onclick="toggleDetail(${i})">
      <td>${fmtDate(run.timestamp)}</td>
      <td>${fmtDuration(run.duration_s)}</td>
      <td>${nodes.length}</td>
      <td>${models.length}</td>
      <td>${run.total_requests}</td>
      <td>${fmtNum(run.tokens_per_sec)}</td>
      <td>${fmtMs(run.latency_p50_ms)}</td>
      <td>${fmtMs(run.ttft_p50_ms)}</td>
    </tr>`;

    // Detail row
    let modelHtml = '', nodeHtml = '', utilHtml = '';
    const mr = run.per_model_results || [];
    if (mr.length) {
      modelHtml = '<table class="detail-table"><thead><tr><th>Model</th><th>Requests</th><th>tok/s</th><th>Avg Latency</th><th>Avg TTFT</th></tr></thead><tbody>';
      mr.forEach(m => { modelHtml += `<tr><td>${m.model}</td><td>${m.requests}</td><td>${fmtNum(m.tok_s)}</td><td>${fmtMs(m.avg_latency_ms)}</td><td>${fmtMs(m.avg_ttft_ms)}</td></tr>`; });
      modelHtml += '</tbody></table>';
    }
    const nr = run.per_node_results || [];
    if (nr.length) {
      nodeHtml = '<table class="detail-table"><thead><tr><th>Node</th><th>Requests</th><th>Share</th><th>tok/s</th><th>Tokens</th></tr></thead><tbody>';
      nr.forEach(n => { nodeHtml += `<tr><td>${n.node_id}</td><td>${n.requests}</td><td>${fmtNum(n.pct)}%</td><td>${fmtNum(n.tok_s)}</td><td>${(n.tokens || 0).toLocaleString()}</td></tr>`; });
      nodeHtml += '</tbody></table>';
    }
    const pu = run.peak_utilization || [];
    if (pu.length) {
      utilHtml = '<table class="detail-table"><thead><tr><th>Node</th><th>CPU Peak</th><th>MEM Peak</th><th>Active Peak</th></tr></thead><tbody>';
      pu.forEach(u => { utilHtml += `<tr><td>${u.node_id}</td><td>${fmtNum(u.cpu_peak)}%</td><td>${fmtNum(u.mem_peak)}%</td><td>${u.active_peak || 0}</td></tr>`; });
      utilHtml += '</tbody></table>';
    }

    rows += `<tr class="bench-detail" id="detail-${i}"><td colspan="8">
      <div class="detail-grid">
        <div class="detail-section"><h5>Per Model</h5>${modelHtml || '<em>No data</em>'}</div>
        <div class="detail-section"><h5>Per Node</h5>${nodeHtml || '<em>No data</em>'}</div>
      </div>
      ${utilHtml ? '<div class="detail-section" style="margin-top:12px"><h5>Peak Utilization</h5>' + utilHtml + '</div>' : ''}
    </td></tr>`;
  });
  tbody.innerHTML = rows;

  // Charts — chronological order (oldest first)
  const sorted = [...data].reverse();
  const labels = sorted.map(d => fmtShortDate(d.timestamp));
  const tokS = sorted.map(d => d.tokens_per_sec || 0);
  const reqS = sorted.map(d => d.requests_per_sec || 0);
  const nodeCount = sorted.map(d => (d.fleet_snapshot && d.fleet_snapshot.nodes) ? d.fleet_snapshot.nodes.length : 0);

  if (capacityChart) capacityChart.destroy();
  if (throughputChart) throughputChart.destroy();

  capacityChart = new Chart(document.getElementById('capacity-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'tok/s', data: tokS, borderColor: C.accent, backgroundColor: C.accent + '22', fill: true, tension: 0.3, pointRadius: 4, yAxisID: 'y' },
        { label: 'Nodes', data: nodeCount, borderColor: C.green, backgroundColor: C.green + '22', fill: false, tension: 0, pointRadius: 4, stepped: true, yAxisID: 'y1' },
      ],
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: { ...chartDefaults.scales.y, title: { display: true, text: 'Tokens/sec', color: C.dim } },
        y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: C.dim, stepSize: 1, font: { size: 11 } }, title: { display: true, text: 'Nodes', color: C.dim } },
      },
    },
  });

  throughputChart = new Chart(document.getElementById('throughput-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'req/s', data: reqS, backgroundColor: C.blue + '88', borderColor: C.blue, borderWidth: 1 },
      ],
    },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  // --- Per-Model Charts (from latest run) ---
  const latest = data[0];
  const modelColors = [C.accent, C.blue, C.green, '#f97316', '#ec4899', '#8b5cf6', '#06b6d4', '#eab308'];

  // Chart 3: Model Throughput (horizontal bar)
  if (modelThroughputChart) modelThroughputChart.destroy();
  const pm = latest.per_model_results || [];
  if (pm.length) {
    const mLabels = pm.map(m => m.model.length > 25 ? m.model.substring(0, 22) + '...' : m.model);
    const mTokS = pm.map(m => m.tok_s || 0);
    const mColors = pm.map((_, i) => modelColors[i % modelColors.length]);
    modelThroughputChart = new Chart(document.getElementById('model-throughput-chart'), {
      type: 'bar',
      data: {
        labels: mLabels,
        datasets: [{ label: 'tok/s', data: mTokS, backgroundColor: mColors.map(c => c + 'cc'), borderColor: mColors, borderWidth: 1 }],
      },
      options: {
        ...chartDefaults,
        indexAxis: 'y',
        plugins: { ...chartDefaults.plugins, legend: { display: false } },
        scales: {
          x: { ...chartDefaults.scales.x, title: { display: true, text: 'Tokens/sec', color: C.dim } },
          y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, font: { size: 11 } } },
        },
      },
    });
  }

  // Chart 4: Model Latency (grouped horizontal bar — avg latency + avg TTFT)
  if (modelLatencyChart) modelLatencyChart.destroy();
  if (pm.length) {
    const mLabels2 = pm.map(m => m.model.length > 25 ? m.model.substring(0, 22) + '...' : m.model);
    modelLatencyChart = new Chart(document.getElementById('model-latency-chart'), {
      type: 'bar',
      data: {
        labels: mLabels2,
        datasets: [
          { label: 'Avg Latency', data: pm.map(m => m.avg_latency_ms || 0), backgroundColor: C.accent + 'aa', borderColor: C.accent, borderWidth: 1 },
          { label: 'Avg TTFT', data: pm.map(m => m.avg_ttft_ms || 0), backgroundColor: C.green + 'aa', borderColor: C.green, borderWidth: 1 },
        ],
      },
      options: {
        ...chartDefaults,
        indexAxis: 'y',
        scales: {
          x: { ...chartDefaults.scales.x, title: { display: true, text: 'Milliseconds', color: C.dim } },
          y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, font: { size: 11 } } },
        },
      },
    });
  }

  // Chart 5: Model tok/s Over Time (multi-line)
  if (modelHistoryChart) modelHistoryChart.destroy();
  const allModels = new Set();
  sorted.forEach(d => (d.per_model_results || []).forEach(m => allModels.add(m.model)));
  if (allModels.size > 0) {
    const histLabels = sorted.map(d => fmtShortDate(d.timestamp));
    const histDatasets = [];
    let ci = 0;
    allModels.forEach(model => {
      const color = modelColors[ci % modelColors.length];
      const pts = sorted.map(d => {
        const mr = (d.per_model_results || []).find(m => m.model === model);
        return mr ? mr.tok_s : null;
      });
      histDatasets.push({
        label: model.length > 20 ? model.substring(0, 17) + '...' : model,
        data: pts,
        borderColor: color,
        backgroundColor: color + '22',
        tension: 0.3,
        pointRadius: 4,
        spanGaps: true,
      });
      ci++;
    });
    modelHistoryChart = new Chart(document.getElementById('model-history-chart'), {
      type: 'line',
      data: { labels: histLabels, datasets: histDatasets },
      options: {
        ...chartDefaults,
        scales: {
          ...chartDefaults.scales,
          y: { ...chartDefaults.scales.y, title: { display: true, text: 'Tokens/sec', color: C.dim } },
        },
      },
    });
  }

  // Chart 6: Node Utilization (latest run — grouped bar: CPU avg/peak + MEM peak)
  if (nodeUtilChart) nodeUtilChart.destroy();
  const pu = latest.peak_utilization || [];
  if (pu.length) {
    const nLabels = pu.map(u => u.node_id.length > 20 ? u.node_id.substring(0, 17) + '...' : u.node_id);
    nodeUtilChart = new Chart(document.getElementById('node-util-chart'), {
      type: 'bar',
      data: {
        labels: nLabels,
        datasets: [
          { label: 'CPU Avg %', data: pu.map(u => u.cpu_avg || 0), backgroundColor: C.blue + '88', borderColor: C.blue, borderWidth: 1 },
          { label: 'CPU Peak %', data: pu.map(u => u.cpu_peak || 0), backgroundColor: C.accent + '88', borderColor: C.accent, borderWidth: 1 },
          { label: 'MEM Peak %', data: pu.map(u => u.mem_peak || 0), backgroundColor: C.green + '88', borderColor: C.green, borderWidth: 1 },
        ],
      },
      options: {
        ...chartDefaults,
        scales: {
          ...chartDefaults.scales,
          y: { ...chartDefaults.scales.y, title: { display: true, text: 'Utilization %', color: C.dim }, max: 100 },
        },
      },
    });
  }
}

function toggleDetail(i) {
  const row = document.getElementById('detail-' + i);
  if (row) row.classList.toggle('open');
}

window.addEventListener('DOMContentLoaded', () => {
  const dot = document.getElementById('sse-dot');
  const st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

loadBenchmarks();

// --- Benchmark Runner UI ---
let benchMode = 'default';
let benchDuration = 300;
let progressPoll = null;

async function showBenchDialog() {
  benchMode = 'default';
  benchDuration = 300;

  // Detect available model types from fleet
  let hasLlm = false, hasEmbed = false, hasImage = false;
  try {
    const statusResp = await fetch('/fleet/status');
    const status = await statusResp.json();
    const embedPatterns = ['embed', 'nomic', 'bge', 'e5-'];
    for (const node of (status.nodes || [])) {
      const ollama = node.ollama || {};
      for (const m of (ollama.models_loaded || [])) {
        const lower = m.name.toLowerCase();
        if (embedPatterns.some(p => lower.includes(p))) hasEmbed = true;
        else hasLlm = true;
      }
    }
    const imgResp = await fetch('/api/image-models');
    if (imgResp.ok) {
      const imgData = await imgResp.json();
      if ((imgData.models || []).length > 0) hasImage = true;
    }
  } catch(e) { hasLlm = true; }

  const overlay = document.createElement('div');
  overlay.className = 'bench-overlay';
  overlay.id = 'bench-overlay';
  overlay.onclick = function(e) { if (e.target.id === 'bench-overlay') closeBenchDialog(); };
  overlay.innerHTML = `
    <div class="bench-dialog">
      <h3>Run Fleet Benchmark</h3>
      <div class="mode-option selected" id="mode-default" onclick="selectMode('default')">
        <h4>Default Benchmark</h4>
        <p>Benchmark currently loaded models only. Quick and non-disruptive.</p>
      </div>
      <div class="mode-option" id="mode-smart" onclick="selectMode('smart')">
        <h4>Smart Benchmark</h4>
        <p>Fill available memory with recommended models, then benchmark everything. Best for comprehensive fleet testing.</p>
      </div>
      <div class="type-checks">
        <label>Model types to benchmark:</label>
        <div class="type-checks-row">
          <label class="type-check"><input type="checkbox" id="type-llm" checked ${hasLlm ? '' : 'disabled'}> LLM (chat)</label>
          <label class="type-check ${hasEmbed ? '' : 'disabled'}"><input type="checkbox" id="type-embed" ${hasEmbed ? 'checked' : 'disabled'}> Embeddings</label>
          <label class="type-check ${hasImage ? '' : 'disabled'}"><input type="checkbox" id="type-image" ${hasImage ? 'checked' : 'disabled'}> Image gen</label>
        </div>
      </div>
      <div class="duration-row">
        <label>Duration:</label>
        <button class="dur-btn" onclick="selectDur(60,this)">1 min</button>
        <button class="dur-btn selected" onclick="selectDur(300,this)">5 min</button>
        <button class="dur-btn" onclick="selectDur(600,this)">10 min</button>
      </div>
      <div class="bench-actions">
        <button class="bench-cancel-btn" onclick="closeBenchDialog()">Cancel</button>
        <button class="bench-start-btn" onclick="startBenchmark()">Start Benchmark</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
}

function closeBenchDialog() {
  const overlay = document.getElementById('bench-overlay');
  if (overlay) overlay.remove();
}

function selectMode(mode) {
  benchMode = mode;
  document.getElementById('mode-default').className = 'mode-option' + (mode === 'default' ? ' selected' : '');
  document.getElementById('mode-smart').className = 'mode-option' + (mode === 'smart' ? ' selected' : '');
}

function selectDur(dur, btn) {
  benchDuration = dur;
  btn.parentElement.querySelectorAll('.dur-btn').forEach(b => b.className = 'dur-btn');
  btn.className = 'dur-btn selected';
}

async function startBenchmark() {
  // Collect selected model types
  const modelTypes = [];
  if (document.getElementById('type-llm')?.checked) modelTypes.push('llm');
  if (document.getElementById('type-embed')?.checked) modelTypes.push('embed');
  if (document.getElementById('type-image')?.checked) modelTypes.push('image');
  if (modelTypes.length === 0) modelTypes.push('llm');

  closeBenchDialog();
  const btn = document.getElementById('run-bench-btn');
  btn.disabled = true;
  btn.innerHTML = '&#9632; Running...';

  try {
    const resp = await fetch('/dashboard/api/benchmarks/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: benchMode, duration: benchDuration, model_types: modelTypes}),
    });
    if (resp.status === 409) {
      btn.innerHTML = '&#9632; Running...';
    }
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = '&#9654; Run Benchmark';
    return;
  }

  // Start polling progress
  showProgress();
  progressPoll = setInterval(pollProgress, 2000);
}

function fmtElapsed(s) {
  if (s < 60) return Math.round(s) + 's';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return m + 'm ' + (sec < 10 ? '0' : '') + sec + 's';
}

function barGradient(pct) {
  // Purple (accent) at 0% → green at 100%
  const r = Math.round(108 + (34 - 108) * pct / 100);
  const g = Math.round(99 + (197 - 99) * pct / 100);
  const b = Math.round(255 + (94 - 255) * pct / 100);
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

function showProgress() {
  const div = document.getElementById('bench-progress');
  div.style.display = 'block';
  div.innerHTML = `<div class="progress-bar-container">
    <h4 id="prog-title">Starting benchmark...</h4>
    <div class="phase" id="prog-phase"></div>
    <div id="prog-pull-section" style="display:none">
      <div class="progress-bar-label"><span id="prog-pull-label">Downloading...</span><span id="prog-pull-pct"></span></div>
      <div class="progress-bar"><div class="fill" id="prog-pull-fill" style="width:0%"></div></div>
      <div class="progress-bar-label" id="prog-pull-overall-label" style="margin-top:6px"><span>Overall</span><span id="prog-pull-overall-pct"></span></div>
      <div class="progress-bar" id="prog-pull-overall-bar"><div class="fill" id="prog-pull-overall-fill" style="width:0%"></div></div>
    </div>
    <div id="prog-bench-section" style="display:none">
      <div class="progress-bar"><div class="fill" id="prog-fill" style="width:0%"></div></div>
    </div>
    <div class="progress-stats">
      <span>Elapsed: <span class="val" id="prog-elapsed">0s</span></span>
      <span>Requests: <span class="val" id="prog-reqs">0</span></span>
      <span>Throughput: <span class="val" id="prog-toks">0</span> tok/s</span>
    </div>
    <button class="cancel-running" onclick="cancelBenchmark()">Cancel</button>
  </div>`;
}

async function pollProgress() {
  try {
    const resp = await fetch('/dashboard/api/benchmarks/progress');
    const p = await resp.json();

    const phase = document.getElementById('prog-phase');
    const elapsed = document.getElementById('prog-elapsed');
    const reqs = document.getElementById('prog-reqs');
    const toks = document.getElementById('prog-toks');
    const title = document.getElementById('prog-title');
    const pullSection = document.getElementById('prog-pull-section');
    const benchSection = document.getElementById('prog-bench-section');
    const benchFill = document.getElementById('prog-fill');

    if (!phase) return;

    phase.textContent = p.phase || '';
    elapsed.textContent = fmtElapsed(p.elapsed || 0);
    reqs.textContent = (p.requests_completed || 0) + (p.requests_failed ? ' (' + p.requests_failed + ' err)' : '');
    toks.textContent = fmtNum(p.tok_per_sec || 0);

    if (p.status === 'running' && p.duration > 0) {
      pullSection.style.display = 'none';
      benchSection.style.display = 'block';
      const pct = Math.min(100, (p.elapsed / p.duration) * 100);
      benchFill.style.width = pct + '%';
      benchFill.style.background = barGradient(pct);
      title.textContent = benchMode === 'smart' ? 'Smart Benchmark Running' : 'Benchmark Running';
    } else if (p.status === 'pulling') {
      pullSection.style.display = 'block';
      benchSection.style.display = 'none';
      title.textContent = 'Smart Benchmark: Pulling Models';

      const pp = p.pull_progress || {};
      const pullFill = document.getElementById('prog-pull-fill');
      const pullLabel = document.getElementById('prog-pull-label');
      const pullPct = document.getElementById('prog-pull-pct');
      const overallFill = document.getElementById('prog-pull-overall-fill');
      const overallPct = document.getElementById('prog-pull-overall-pct');
      const overallLabel = document.getElementById('prog-pull-overall-label');
      const overallBar = document.getElementById('prog-pull-overall-bar');

      if (pp.model) {
        const isOnDisk = pp.on_disk;
        const action = isOnDisk ? 'Loading' : 'Downloading';

        // Individual model progress
        pullLabel.textContent = `${action}: ${pp.model}`;
        if (!isOnDisk && pp.pct >= 0 && pp.total_gb) {
          pullPct.textContent = `${pp.pct}% (${pp.completed_gb || 0}/${pp.total_gb} GB)`;
          pullFill.style.width = pp.pct + '%';
          pullFill.style.background = barGradient(pp.pct);
        } else if (isOnDisk) {
          pullPct.textContent = 'from disk';
          pullFill.style.width = '100%';
          pullFill.style.background = barGradient(80);
        } else if (pp.status) {
          pullPct.textContent = pp.status;
          pullFill.style.width = '0%';
        }

        // Overall progress — show only if more than 1 model
        if (pp.total > 1) {
          overallLabel.style.display = 'flex';
          overallBar.style.display = 'block';
          const overallPctVal = Math.round(((pp.current - 1) / pp.total) * 100);
          overallPct.textContent = `${pp.current - 1}/${pp.total} models`;
          overallFill.style.width = overallPctVal + '%';
          overallFill.style.background = barGradient(overallPctVal);
        } else {
          overallLabel.style.display = 'none';
          overallBar.style.display = 'none';
        }

        phase.textContent = `${action} model ${pp.current} of ${pp.total}`;
      }
    } else if (p.status === 'warming_up') {
      pullSection.style.display = 'none';
      benchSection.style.display = 'none';
      title.textContent = 'Warming Up Models';
    } else if (p.status === 'complete' || p.status === 'error' || p.status === 'cancelled') {
      clearInterval(progressPoll);
      progressPoll = null;
      const btn = document.getElementById('run-bench-btn');
      btn.disabled = false;
      btn.innerHTML = '&#9654; Run Benchmark';

      if (p.status === 'complete') {
        pullSection.style.display = 'none';
        benchSection.style.display = 'block';
        benchFill.style.width = '100%';
        benchFill.style.background = barGradient(100);
        title.textContent = 'Benchmark Complete';
        phase.textContent = `${p.requests_completed} requests, ${fmtNum(p.tok_per_sec)} tok/s` + (p.models_pulled && p.models_pulled.length ? ` — ${p.models_pulled.length} models pulled` : '');
        document.querySelector('.cancel-running').style.display = 'none';
        loadBenchmarks();
        setTimeout(() => { document.getElementById('bench-progress').style.display = 'none'; }, 8000);
      } else if (p.status === 'error') {
        title.textContent = 'Benchmark Failed';
        phase.textContent = p.error || 'Unknown error';
        benchSection.style.display = 'block';
        benchFill.style.width = '100%';
        benchFill.style.background = 'var(--red)';
      } else {
        title.textContent = 'Benchmark Cancelled';
        document.querySelector('.cancel-running').style.display = 'none';
        setTimeout(() => { document.getElementById('bench-progress').style.display = 'none'; }, 3000);
      }
    }
  } catch (e) { /* ignore poll errors */ }
}

async function cancelBenchmark() {
  try {
    await fetch('/dashboard/api/benchmarks/cancel', { method: 'POST' });
  } catch (e) { /* ignore */ }
}

// Check if a benchmark is already running on page load
async function checkRunningBenchmark() {
  try {
    const resp = await fetch('/dashboard/api/benchmarks/progress');
    const p = await resp.json();
    if (p.status && ['pulling','warming_up','running'].includes(p.status)) {
      const btn = document.getElementById('run-bench-btn');
      btn.disabled = true;
      btn.innerHTML = '&#9632; Running...';
      showProgress();
      progressPoll = setInterval(pollProgress, 2000);
      pollProgress();
    }
  } catch (e) { /* ignore */ }
}
checkRunningBenchmark();
</script>
"""

# ---------------------------------------------------------------------------
# Health page body
# ---------------------------------------------------------------------------

_HEALTH_BODY = """
<style>
.health-header { display:flex; align-items:center; gap:20px; margin-bottom:24px; }
.health-score {
  width:88px; height:88px; border-radius:50%; display:flex; align-items:center; justify-content:center;
  font-size:28px; font-weight:700; color:var(--green); position:relative;
  background: conic-gradient(var(--green) var(--score-pct, 0%), var(--border) var(--score-pct, 0%));
  transition: --score-pct 1s ease;
}
.health-score::before {
  content:''; position:absolute; inset:4px; border-radius:50%; background:var(--card);
}
.health-score span { position:relative; z-index:1; }
.health-score.warning { color:var(--yellow); background: conic-gradient(var(--yellow) var(--score-pct, 0%), var(--border) var(--score-pct, 0%)); }
.health-score.critical { color:var(--red); background: conic-gradient(var(--red) var(--score-pct, 0%), var(--border) var(--score-pct, 0%)); }
@property --score-pct { syntax: '<percentage>'; inherits: false; initial-value: 0%; }
.health-title { font-size:20px; font-weight:600; }
.health-subtitle { font-size:13px; color:var(--text-dim); margin-top:4px; }
.vitals-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:12px; margin-bottom:24px; }
.vital-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; text-align:center; }
.vital-card .v-value { font-size:24px; font-weight:700; font-variant-numeric:tabular-nums; }
.vital-card .v-label { font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; margin-top:4px; }
.section-label { font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; color:var(--text-dim); margin-bottom:12px; }
.recs-list { display:flex; flex-direction:column; gap:12px; }
.rec-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px 20px; display:flex; gap:16px; align-items:flex-start; }
.rec-severity { width:8px; min-height:40px; border-radius:4px; flex-shrink:0; }
.rec-severity.critical { background:var(--red); }
.rec-severity.warning { background:var(--yellow); }
.rec-severity.info { background:var(--blue); }
.rec-content { flex:1; }
.rec-title { font-size:14px; font-weight:600; margin-bottom:4px; }
.rec-desc { font-size:13px; color:var(--text-dim); margin-bottom:8px; line-height:1.5; }
.rec-fix { font-size:12px; background:rgba(108,99,255,0.08); border:1px solid rgba(108,99,255,0.2); border-radius:6px; padding:8px 12px; font-family:'SF Mono','Fira Code',monospace; line-height:1.5; }
.rec-badge { font-size:11px; padding:2px 8px; border-radius:4px; font-weight:500; flex-shrink:0; }
.rec-badge.critical { background:rgba(239,68,68,0.15); color:var(--red); }
.rec-badge.warning { background:rgba(234,179,8,0.15); color:var(--yellow); }
.rec-badge.info { background:rgba(59,130,246,0.15); color:var(--blue); }
.rec-card.resolved { opacity:0.6; }
.rec-badge.resolved { background:rgba(34,197,94,0.15); color:var(--green); }
.all-clear { text-align:center; padding:60px 20px; color:var(--text-dim); }
.all-clear h3 { color:var(--green); margin-bottom:8px; font-size:18px; }
@media (max-width:768px) { .health-header{flex-direction:column;align-items:flex-start;} .vitals-grid{grid-template-columns:repeat(2,1fr);} }
</style>

<div class="main">
  <div class="health-header" id="health-header">
    <div class="health-score" id="health-score"><span>--</span></div>
    <div>
      <div class="health-title" id="health-title">Analyzing fleet health...</div>
      <div class="health-subtitle" id="health-subtitle">Checking nodes, traces, and configuration</div>
    </div>
  </div>

  <div class="section-label">Fleet Vitals</div>
  <div class="vitals-grid" id="vitals-grid"></div>

  <div class="section-label">Recommendations</div>
  <div class="recs-list" id="recs-list">
    <div style="text-align:center;padding:40px;color:var(--text-dim)">Loading...</div>
  </div>

  <div class="section-label" style="margin-top:24px;display:flex;align-items:center;justify-content:space-between">
    Fleet Intelligence History
    <button onclick="refreshBriefingFromHealth()" style="background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer">Generate New</button>
  </div>
  <div id="briefing-history-list">
    <div style="text-align:center;padding:40px;color:var(--text-dim)">Loading...</div>
  </div>
</div>

<script>
let refreshTimer;

function scoreClass(score) {
  if (score >= 80) return '';
  if (score >= 50) return 'warning';
  return 'critical';
}

function renderHealth(report) {
  const v = report.vitals;
  const recs = report.recommendations || [];

  const scoreEl = document.getElementById('health-score');
  scoreEl.innerHTML = '<span>' + v.health_score + '</span>';
  scoreEl.className = 'health-score ' + scoreClass(v.health_score);
  // Animate the conic-gradient ring — delay so browser sees 0% first
  requestAnimationFrame(() => {
    scoreEl.style.setProperty('--score-pct', v.health_score + '%');
  });

  const titleEl = document.getElementById('health-title');
  const subtitleEl = document.getElementById('health-subtitle');
  if (recs.length === 0) {
    titleEl.textContent = 'Fleet is healthy';
    subtitleEl.textContent = 'No issues detected. All systems operating normally.';
  } else {
    const critCount = recs.filter(r => r.severity === 'critical').length;
    const warnCount = recs.filter(r => r.severity === 'warning').length;
    const infoCount = recs.filter(r => r.severity === 'info').length;
    const parts = [];
    if (critCount) parts.push(critCount + ' critical');
    if (warnCount) parts.push(warnCount + ' warning' + (warnCount > 1 ? 's' : ''));
    if (infoCount) parts.push(infoCount + ' suggestion' + (infoCount > 1 ? 's' : ''));
    titleEl.textContent = parts.join(', ');
    subtitleEl.textContent = 'Last checked ' + new Date().toLocaleTimeString();
  }

  const vg = document.getElementById('vitals-grid');
  const onlineColor = v.nodes_online > 0 ? 'var(--green)' : 'var(--text-dim)';
  const errorColor = v.overall_error_rate_pct > 5 ? 'var(--red)' : v.overall_error_rate_pct > 1 ? 'var(--yellow)' : 'var(--green)';
  const coldColor = v.cold_loads_24h > 5 ? 'var(--red)' : v.cold_loads_24h > 0 ? 'var(--yellow)' : 'var(--green)';
  vg.innerHTML =
    '<div class="vital-card"><div class="v-value" style="color:' + onlineColor + '">' + v.nodes_online + '/' + v.nodes_total + '</div><div class="v-label">Nodes Online</div></div>' +
    '<div class="vital-card"><div class="v-value">' + v.total_requests_24h.toLocaleString() + '</div><div class="v-label">Requests (24h)</div></div>' +
    '<div class="vital-card"><div class="v-value" style="color:' + errorColor + '">' + v.overall_error_rate_pct.toFixed(1) + '%</div><div class="v-label">Error Rate (24h)</div></div>' +
    '<div class="vital-card"><div class="v-value" style="color:' + coldColor + '">' + v.cold_loads_24h + '</div><div class="v-label">Cold Loads (24h)</div></div>' +
    '<div class="vital-card"><div class="v-value">' + (v.avg_ttft_ms != null ? (v.avg_ttft_ms / 1000).toFixed(1) + 's' : '-') + '</div><div class="v-label">Avg TTFT (24h)</div></div>' +
    '<div class="vital-card"><div class="v-value">' + v.total_retries_24h + '</div><div class="v-label">Retries (24h)</div></div>' +
    '<div class="vital-card"><div class="v-value" style="color:var(--orange)">' + (v.image_generations_24h || 0) + '</div><div class="v-label">Images (24h)</div></div>' +
    '<div class="vital-card"><div class="v-value" style="color:var(--blue)">' + (v.transcriptions_24h || 0) + '</div><div class="v-label">STT (24h)</div></div>' +
    (v.client_disconnects_24h ? '<div class="vital-card"><div class="v-value" style="color:var(--yellow)">' + v.client_disconnects_24h + '</div><div class="v-label">Disconnects (24h)</div></div>' : '') +
    (v.incomplete_streams_24h ? '<div class="vital-card"><div class="v-value" style="color:var(--red)">' + v.incomplete_streams_24h + '</div><div class="v-label">Incomplete (24h)</div></div>' : '');

  const rl = document.getElementById('recs-list');
  if (recs.length === 0) {
    rl.innerHTML = '<div class="all-clear"><h3>All Clear</h3><p>No issues or recommendations at this time.</p></div>';
    return;
  }
  rl.innerHTML = recs.map(function(r) {
    var isResolved = r.data && r.data.resolved;
    var cardClass = 'rec-card' + (isResolved ? ' resolved' : '');
    var badgeClass = isResolved ? 'rec-badge resolved' : 'rec-badge ' + r.severity;
    var badgeText = isResolved ? 'resolved' : r.severity;
    return '<div class="' + cardClass + '">' +
      '<div class="rec-severity ' + r.severity + '"></div>' +
      '<div class="rec-content">' +
        '<div class="rec-title">' + r.title + '</div>' +
        '<div class="rec-desc">' + r.description + '</div>' +
        '<div class="rec-fix">' + r.fix + '</div>' +
      '</div>' +
      '<span class="' + badgeClass + '">' + badgeText + '</span>' +
    '</div>';
  }).join('');
}

async function loadHealth() {
  try {
    const resp = await fetch('/dashboard/api/health');
    const data = await resp.json();
    renderHealth(data);
  } catch (err) {
    console.error('Health load error:', err);
  }
}

window.addEventListener('DOMContentLoaded', function() {
  const dot = document.getElementById('sse-dot');
  const st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

loadHealth();

async function loadBriefingHistory() {
  try {
    const resp = await fetch('/dashboard/api/briefing/history');
    const data = await resp.json();
    const list = document.getElementById('briefing-history-list');
    if (!data.history || data.history.length === 0) {
      list.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-dim)">No briefings generated yet. Click "Generate New" to create one.</div>';
      return;
    }
    list.innerHTML = data.history.map(function(b) {
      var d = new Date(b.generated_at * 1000);
      var dateStr = d.toLocaleDateString('en-US', {month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString('en-US', {hour:'numeric',minute:'2-digit'});
      var lines = b.briefing.split(String.fromCharCode(10));
      var html = lines.map(function(line) {
        while (line.indexOf('**') !== -1) {
          var i = line.indexOf('**');
          var j = line.indexOf('**', i + 2);
          if (j === -1) break;
          line = line.substring(0, i) + '<strong>' + line.substring(i+2, j) + '</strong>' + line.substring(j+2);
        }
        var trimmed = line.trimStart();
        if (trimmed.charAt(0) === '-' || trimmed.charAt(0) === '*') {
          line = '&bull; ' + trimmed.substring(1).trimStart();
        }
        return line;
      }).join('<br>');
      return '<div class="card" style="margin-bottom:12px"><div style="font-size:13px;line-height:1.7">' + html + '</div><div style="margin-top:8px;font-size:11px;color:var(--text-dim)">' + dateStr + ' via ' + (b.model || '?') + '</div></div>';
    }).join('');
  } catch(e) {}
}
loadBriefingHistory();

async function refreshBriefingFromHealth() {
  var btn = event.target;
  btn.textContent = 'Generating...';
  try {
    await fetch('/dashboard/api/briefing?refresh=1');
    await loadBriefingHistory();
  } catch(e) {}
  btn.textContent = 'Generate New';
}
refreshTimer = setInterval(loadHealth, 15000);
</script>
"""


# ---------------------------------------------------------------------------
# Recommendations page body
# ---------------------------------------------------------------------------

_RECOMMENDATIONS_BODY = """
<style>
.rec-header { margin-bottom:24px; }
.rec-header-row { display:flex; align-items:center; gap:12px; margin-bottom:4px; }
.rec-header h2 { font-size:20px; font-weight:600; }
.rec-header p { font-size:13px; color:var(--text-dim); }
.rec-meta { display:flex; align-items:center; gap:12px; margin-top:6px; }
.rec-timestamp { font-size:12px; color:var(--text-dim); }
.refresh-btn {
  background:var(--border); border:1px solid #2a2a3e; border-radius:6px;
  color:var(--text-dim); font-size:12px; padding:4px 10px; cursor:pointer;
  display:inline-flex; align-items:center; gap:5px; transition:all .15s;
}
.refresh-btn:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
.refresh-btn.loading { opacity:.5; pointer-events:none; }
.refresh-btn svg { width:13px; height:13px; }
.refresh-btn.loading svg { animation: spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.usage-section { margin-bottom:28px; }
.usage-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:10px; margin-bottom:16px; }
.usage-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; text-align:center; }
.usage-card .u-value { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }
.usage-card .u-label { font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; margin-top:4px; }
.usage-card.active { border-color:var(--accent); }
.coverage-row { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
.cov-badge { font-size:12px; padding:4px 12px; border-radius:20px; font-weight:500; }
.cov-badge.covered { background:rgba(34,197,94,0.15); color:var(--green); }
.cov-badge.uncovered { background:rgba(239,68,68,0.15); color:var(--red); }
.node-plan { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }
.node-plan-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
.node-plan-title { font-size:16px; font-weight:600; }
.node-bars { display:flex; flex-direction:column; gap:4px; align-items:flex-end; }
.node-ram-bar { display:flex; align-items:center; gap:12px; }
.ram-bar { width:160px; height:8px; background:var(--border); border-radius:4px; overflow:hidden; }
.ram-bar-fill { height:100%; border-radius:4px; background:var(--accent); transition:width 0.3s; }
.ram-bar-label { font-size:12px; color:var(--text-dim); }
.model-recs { display:flex; flex-direction:column; gap:10px; }
.model-rec { display:flex; align-items:center; gap:14px; padding:12px 16px; background:rgba(108,99,255,0.04); border:1px solid rgba(108,99,255,0.1); border-radius:8px; }
.model-rec.available { border-color:rgba(34,197,94,0.3); background:rgba(34,197,94,0.04); }
.model-icon { width:40px; height:40px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:18px; flex-shrink:0; }
.model-icon.general { background:rgba(108,99,255,0.15); color:var(--accent); }
.model-icon.coding { background:rgba(34,197,94,0.15); color:var(--green); }
.model-icon.reasoning { background:rgba(59,130,246,0.15); color:var(--blue); }
.model-icon.creative { background:rgba(234,179,8,0.15); color:var(--yellow); }
.model-icon.fast-chat { background:rgba(168,85,247,0.15); color:#a855f7; }
.model-info { flex:1; min-width:0; }
.model-name { font-size:14px; font-weight:600; }
.model-meta { font-size:12px; color:var(--text-dim); margin-top:2px; }
.model-reason { font-size:12px; color:var(--text-dim); margin-top:4px; line-height:1.4; }
.model-stats { display:flex; gap:12px; flex-shrink:0; text-align:right; }
.model-stat { display:flex; flex-direction:column; align-items:flex-end; }
.model-stat .s-val { font-size:14px; font-weight:600; font-variant-numeric:tabular-nums; }
.model-stat .s-lbl { font-size:10px; color:var(--text-dim); text-transform:uppercase; }
.model-badges { display:flex; gap:6px; flex-shrink:0; }
.m-badge { font-size:11px; padding:2px 8px; border-radius:4px; font-weight:500; }
.m-badge.high { background:rgba(34,197,94,0.15); color:var(--green); }
.m-badge.medium { background:rgba(59,130,246,0.15); color:var(--blue); }
.m-badge.low { background:rgba(108,99,255,0.1); color:var(--text-dim); }
.m-badge.downloaded { background:rgba(34,197,94,0.12); color:var(--green); }
.current-models { margin-top:12px; padding-top:12px; border-top:1px solid var(--border); }
.current-models-label { font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
.current-tags { display:flex; flex-wrap:wrap; gap:4px; }
.current-tag { font-size:11px; padding:2px 8px; background:var(--border); border-radius:4px; color:var(--text-dim); }
.pull-section { margin-top:16px; }
.pull-cmd { font-size:12px; background:rgba(108,99,255,0.08); border:1px solid rgba(108,99,255,0.2); border-radius:6px; padding:10px 14px; font-family:'SF Mono','Fira Code',monospace; cursor:pointer; transition:background 0.2s; word-break:break-all; }
.pull-cmd:hover { background:rgba(108,99,255,0.14); }
.pull-cmd .copy-hint { font-size:10px; color:var(--text-dim); float:right; }
.pull-actions { display:flex; align-items:center; gap:10px; margin-top:10px; }
.pull-btn {
  background:var(--accent); color:#fff; border:none; border-radius:6px;
  font-size:13px; font-weight:500; padding:8px 16px; cursor:pointer;
  display:inline-flex; align-items:center; gap:6px; transition:all .15s;
}
.pull-btn:hover { background:#5b54e0; }
.pull-btn:disabled { opacity:.5; cursor:not-allowed; }
.pull-btn svg { width:14px; height:14px; }
.pull-progress { font-size:12px; color:var(--text-dim); }
.model-check { flex-shrink:0; width:18px; height:18px; accent-color:var(--accent); cursor:pointer; }
.model-check:disabled { opacity:.5; cursor:default; }
.model-rec.pulling { border-color:rgba(108,99,255,0.4); }
.model-rec.pulled { border-color:rgba(34,197,94,0.4); background:rgba(34,197,94,0.04); }
.model-rec.pull-error { border-color:rgba(239,68,68,0.3); }
.pull-status { font-size:11px; font-weight:500; margin-left:8px; }
.pull-status.pulling { color:var(--accent); }
.pull-status.done { color:var(--green); }
.pull-status.error { color:var(--red); }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
.pull-status.pulling { animation: pulse 1.5s ease-in-out infinite; }
.empty-state { text-align:center; padding:60px 20px; color:var(--text-dim); }
.empty-state h3 { color:var(--text); margin-bottom:8px; font-size:18px; }
/* Model Management */
.model-mgmt { margin-top:16px; padding-top:16px; border-top:1px solid var(--border); }
.model-mgmt-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
.model-mgmt-label { font-size:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; font-weight:600; }
.model-mgmt-summary { font-size:11px; color:var(--text-dim); }
.model-table { width:100%; border-collapse:collapse; font-size:12px; }
.model-table th { text-align:left; padding:6px 8px; color:var(--text-dim); font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:0.3px; border-bottom:1px solid var(--border); }
.model-table td { padding:8px; border-bottom:1px solid rgba(255,255,255,0.04); vertical-align:middle; }
.model-table tr:hover { background:rgba(108,99,255,0.04); }
.model-table tr.unused-row { background:rgba(239,68,68,0.04); }
.model-table tr.unused-row:hover { background:rgba(239,68,68,0.08); }
.model-table .name-col { font-weight:500; }
.model-table .name-col code { font-size:11px; color:var(--text-dim); }
.model-table .cat-pill { font-size:10px; padding:1px 6px; border-radius:3px; background:var(--border); }
.model-table .size-col { font-variant-numeric:tabular-nums; text-align:right; }
.model-table .usage-col { text-align:right; font-variant-numeric:tabular-nums; }
.model-table .status-col { text-align:center; white-space:nowrap; }
.vram-badge { font-size:10px; padding:1px 6px; border-radius:3px; background:rgba(34,197,94,0.15); color:var(--green); }
.unused-badge { font-size:10px; padding:1px 6px; border-radius:3px; background:rgba(239,68,68,0.15); color:var(--red); }
.never-used-badge { font-size:10px; padding:1px 6px; border-radius:3px; background:rgba(234,179,8,0.15); color:var(--yellow); }
.delete-section { display:flex; align-items:center; gap:10px; margin-top:10px; }
.delete-btn {
  background:var(--red); color:#fff; border:none; border-radius:6px;
  font-size:13px; font-weight:500; padding:8px 16px; cursor:pointer;
  display:inline-flex; align-items:center; gap:6px; transition:all .15s;
}
.delete-btn:hover { opacity:0.85; }
.delete-btn:disabled { opacity:.4; cursor:not-allowed; }
.delete-btn svg { width:14px; height:14px; }
.delete-progress { font-size:12px; color:var(--text-dim); }
.delete-status { font-size:11px; font-weight:500; margin-left:4px; }
.delete-status.deleting { color:var(--red); animation: pulse 1.5s ease-in-out infinite; }
.delete-status.deleted { color:var(--green); }
.delete-status.error { color:var(--red); }
.mgmt-check { width:16px; height:16px; accent-color:var(--red); cursor:pointer; }
.mgmt-check:disabled { opacity:.4; cursor:default; }
.mgmt-check-all { width:16px; height:16px; accent-color:var(--red); cursor:pointer; }
.confirm-overlay {
  position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.6);
  display:flex; align-items:center; justify-content:center; z-index:1000;
}
.confirm-dialog {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:24px; max-width:480px; width:90%;
}
.confirm-dialog h3 { margin:0 0 12px; font-size:16px; }
.confirm-dialog p { font-size:13px; color:var(--text-dim); line-height:1.5; margin:0 0 8px; }
.confirm-dialog .warn-text { color:var(--yellow); font-size:12px; margin:8px 0; }
.confirm-dialog ul { font-size:12px; margin:8px 0; padding-left:20px; color:var(--text); }
.confirm-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
.confirm-cancel { background:var(--border); color:var(--text); border:none; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:13px; }
.confirm-cancel:hover { background:rgba(255,255,255,0.15); }
.confirm-delete { background:var(--red); color:#fff; border:none; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:13px; font-weight:500; }
.confirm-delete:hover { opacity:0.85; }
@media (max-width:768px) {
  .usage-grid { grid-template-columns:repeat(2,1fr); }
  .node-plan-header { flex-direction:column; align-items:flex-start; gap:8px; }
  .model-rec { flex-direction:column; align-items:flex-start; }
  .model-stats { flex-direction:row; }
  .model-table { font-size:11px; }
  .model-table th, .model-table td { padding:4px 6px; }
}
</style>

<div class="main">
  <div class="rec-header">
    <div class="rec-header-row">
      <h2 id="rec-title">Analyzing fleet...</h2>
    </div>
    <p id="rec-subtitle">Evaluating hardware, usage patterns, and model benchmarks</p>
    <div class="rec-meta" id="rec-meta" style="display:none">
      <span class="rec-timestamp" id="rec-timestamp"></span>
      <button class="refresh-btn" id="refresh-btn" onclick="refreshRecommendations()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
        Refresh
      </button>
    </div>
  </div>

  <div class="usage-section" id="usage-section" style="display:none">
    <div class="section-label">Usage Analysis (24h)</div>
    <div class="usage-grid" id="usage-grid"></div>
    <div class="section-label" style="margin-top:16px">Category Coverage</div>
    <div class="coverage-row" id="coverage-row"></div>
  </div>

  <div class="section-label" id="nodes-label" style="display:none">Per-Node Recommendations</div>
  <div id="node-plans"></div>
</div>

<script>
const CAT_ICONS = {
  'general': '&#x1f30d;',
  'coding': '&#x1f4bb;',
  'reasoning': '&#x1f9e0;',
  'creative': '&#x1f3a8;',
  'fast-chat': '&#x26a1;'
};

const CAT_LABELS = {
  'general': 'General',
  'coding': 'Coding',
  'reasoning': 'Reasoning',
  'creative': 'Creative',
  'fast-chat': 'Fast Chat'
};

function formatTimestamp(epoch) {
  if (!epoch) return '';
  var d = new Date(epoch * 1000);
  var now = new Date();
  var diffMs = now - d;
  var diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return diffMin + 'm ago';
  var diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return diffHr + 'h ' + (diffMin % 60) + 'm ago';
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

var _lastGeneratedAt = 0;

function renderRecommendations(data) {
  const titleEl = document.getElementById('rec-title');
  const subtitleEl = document.getElementById('rec-subtitle');
  titleEl.textContent = 'Model Recommendations';
  subtitleEl.textContent = data.fleet_summary || 'No nodes available';

  // Timestamp
  if (data.generated_at) {
    _lastGeneratedAt = data.generated_at;
    var metaEl = document.getElementById('rec-meta');
    metaEl.style.display = 'flex';
    updateTimestamp();
  }

  // Usage section
  const usageSection = document.getElementById('usage-section');
  const usage = data.usage || {};

  if (usage.total_requests_24h > 0) {
    usageSection.style.display = 'block';
    const ug = document.getElementById('usage-grid');
    let ugHtml = '<div class="usage-card"><div class="u-value">' + usage.total_requests_24h.toLocaleString() + '</div><div class="u-label">Total Requests</div></div>';
    const cats = usage.category_breakdown || {};
    const sortedCats = Object.entries(cats).sort((a, b) => b[1] - a[1]);
    sortedCats.forEach(function(entry) {
      var cat = entry[0], count = entry[1];
      ugHtml += '<div class="usage-card active"><div class="u-value">' + count.toLocaleString() + '</div><div class="u-label">' + (CAT_LABELS[cat] || cat) + '</div></div>';
    });
    ug.innerHTML = ugHtml;
  } else {
    usageSection.style.display = 'block';
    document.getElementById('usage-grid').innerHTML = '<div class="usage-card"><div class="u-value">0</div><div class="u-label">Requests (24h)</div></div>';
  }

  // Coverage badges
  const covRow = document.getElementById('coverage-row');
  const coverage = usage.category_coverage || {};
  covRow.innerHTML = Object.entries(coverage).map(function(entry) {
    var cat = entry[0], covered = entry[1];
    var cls = covered ? 'cov-badge covered' : 'cov-badge uncovered';
    var icon = covered ? '&#10003; ' : '&#10007; ';
    return '<span class="' + cls + '">' + icon + (CAT_LABELS[cat] || cat) + '</span>';
  }).join('');

  // Node plans
  var nodesLabel = document.getElementById('nodes-label');
  var plansEl = document.getElementById('node-plans');
  var nodes = data.nodes || [];

  if (nodes.length === 0) {
    nodesLabel.style.display = 'none';
    plansEl.innerHTML = '<div class="empty-state"><h3>No Nodes Online</h3><p>Start a node agent to get model recommendations.</p></div>';
    return;
  }

  nodesLabel.style.display = 'block';
  plansEl.innerHTML = nodes.map(function(node) {
    var ramPct = node.usable_ram_gb > 0 ? Math.min(100, Math.round(node.total_recommended_ram_gb / node.usable_ram_gb * 100)) : 0;

    var diskPct = node.disk_total_gb > 0 ? Math.min(100, Math.round((node.disk_total_gb - node.disk_available_gb) / node.disk_total_gb * 100)) : 0;

    var html = '<div class="node-plan">';
    html += '<div class="node-plan-header">';
    html += '<div class="node-plan-title">' + node.node_id + '</div>';
    html += '<div class="node-bars">';
    html += '<div class="node-ram-bar">';
    html += '<div class="ram-bar"><div class="ram-bar-fill" style="width:' + ramPct + '%;background:' + barColor(ramPct) + '"></div></div>';
    html += '<div class="ram-bar-label">' + node.total_recommended_ram_gb + ' / ' + node.usable_ram_gb + ' GB RAM</div>';
    html += '</div>';
    if (node.disk_total_gb > 0) {
      html += '<div class="node-ram-bar">';
      html += '<div class="ram-bar"><div class="ram-bar-fill" style="width:' + diskPct + '%;background:' + barColor(diskPct) + '"></div></div>';
      html += '<div class="ram-bar-label">' + node.disk_available_gb.toFixed(0) + ' / ' + node.disk_total_gb.toFixed(0) + ' GB Disk free</div>';
      html += '</div>';
    }
    html += '</div></div>';

    if (node.recommendations.length === 0) {
      html += '<div style="text-align:center;padding:20px;color:var(--text-dim)">No recommendations — insufficient memory</div>';
    } else {
      html += '<div class="model-recs">';
      node.recommendations.forEach(function(rec) {
        var recClass = 'model-rec' + (rec.already_available ? ' available' : '');
        var cardId = 'card-' + node.node_id + '-' + rec.model.replace(/[^a-zA-Z0-9]/g, '-');
        html += '<div class="' + recClass + '" id="' + cardId + '">';

        // Checkbox for non-downloaded models
        if (rec.already_available) {
          html += '<input type="checkbox" class="model-check" checked disabled title="Already downloaded">';
        } else {
          html += '<input type="checkbox" class="model-check" checked data-node="' + node.node_id + '" data-model="' + rec.model + '">';
        }

        html += '<div class="model-icon ' + rec.category + '">' + (CAT_ICONS[rec.category] || '&#x2699;') + '</div>';
        html += '<div class="model-info">';
        html += '<div class="model-name">' + rec.display_name + '<span class="pull-status" id="status-' + cardId + '"></span></div>';
        html += '<div class="model-meta"><code>' + rec.model + '</code> &middot; ' + rec.ram_gb + ' GB &middot; ' + (CAT_LABELS[rec.category] || rec.category) + '</div>';
        html += '<div class="model-reason">' + rec.reason + '</div>';
        html += '</div>';
        html += '<div class="model-stats">';
        if (rec.quality_score > 0) {
          html += '<div class="model-stat"><div class="s-val">' + rec.quality_score + '</div><div class="s-lbl">Quality</div></div>';
        }
        html += '</div>';
        html += '<div class="model-badges">';
        html += '<span class="m-badge ' + rec.priority + '">' + rec.priority + '</span>';
        if (rec.already_available) { html += '<span class="m-badge downloaded">downloaded</span>'; }
        html += '</div>';
        html += '</div>';
      });
      html += '</div>';

      // Pull section for models not yet downloaded
      var toPull = node.recommendations.filter(function(r) { return !r.already_available; });
      if (toPull.length > 0) {
        html += '<div class="pull-section" id="pull-section-' + node.node_id + '">';
        var cmds = toPull.map(function(r) { return 'ollama pull ' + r.model; }).join(' && ');
        html += '<div class="pull-cmd" data-cmd="' + cmds + '" id="pull-cmd-' + node.node_id + '">' + cmds + '<span class="copy-hint">click to copy</span></div>';
        html += '<div class="pull-actions">';
        html += '<button class="pull-btn" id="pull-btn-' + node.node_id + '" data-node="' + node.node_id + '">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
        html += 'Pull ' + toPull.length + ' Model' + (toPull.length > 1 ? 's' : '') + '</button>';
        html += '<span class="pull-progress" id="pull-progress-' + node.node_id + '"></span>';
        html += '</div></div>';
      }
    }

    // Model Management placeholder (populated by loadModelManagement)
    html += '<div class="model-mgmt" id="mgmt-' + node.node_id + '">';
    html += '<div class="model-mgmt-header">';
    html += '<div class="model-mgmt-label">Model Management</div>';
    html += '<div class="model-mgmt-summary" id="mgmt-summary-' + node.node_id + '">Loading...</div>';
    html += '</div>';
    html += '<div id="mgmt-table-' + node.node_id + '"></div>';
    html += '</div>';

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
    sectionEl.style.display = 'none';
    return;
  }
  sectionEl.style.display = 'block';

  if (cmdEl) {
    var cmds = models.map(function(m) { return 'ollama pull ' + m; }).join(' && ');
    cmdEl.dataset.cmd = cmds;
    cmdEl.innerHTML = cmds + '<span class="copy-hint">click to copy</span>';
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

async function loadRecommendations(forceRefresh) {
  try {
    var url = '/dashboard/api/recommendations';
    if (forceRefresh) url += '?refresh=1';
    const resp = await fetch(url);
    const data = await resp.json();
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
    el.textContent = 'Last analyzed ' + formatTimestamp(_lastGeneratedAt);
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

  summaryEl.textContent = models.length + ' model' + (models.length !== 1 ? 's' : '') +
    ' (' + nodeData.total_size_gb + ' GB on disk)' +
    (unusedCount > 0 ? ' \u00b7 ' + unusedCount + ' unused' : '');

  if (models.length === 0) {
    tableEl.innerHTML = '<div style="padding:12px;color:var(--text-dim);font-size:12px">No models on this node</div>';
    return;
  }

  var html = '<table class="model-table">';
  html += '<thead><tr>';
  html += '<th style="width:30px"><input type="checkbox" class="mgmt-check-all" data-node="' + nodeData.node_id + '"></th>';
  html += '<th>Model</th><th>Category</th><th style="text-align:right">Size</th>';
  html += '<th style="text-align:right">Requests</th><th style="text-align:right">Last Used</th>';
  html += '<th style="text-align:center">Status</th>';
  html += '</tr></thead><tbody>';

  models.forEach(function(m) {
    var rowClass = m.unused ? 'unused-row' : '';
    var safeId = m.name.replace(/[^a-zA-Z0-9]/g, '-');
    html += '<tr class="' + rowClass + '">';
    html += '<td><input type="checkbox" class="mgmt-check" data-node="' + nodeData.node_id + '" data-model="' + m.name + '"';
    if (m.loaded_in_vram) html += ' data-loaded="1"';
    html += '></td>';
    html += '<td class="name-col">' + m.display_name + '<br><code>' + m.name + '</code></td>';
    html += '<td><span class="cat-pill">' + (CAT_LABELS[m.category] || m.category) + '</span></td>';
    html += '<td class="size-col">' + m.size_gb.toFixed(1) + ' GB</td>';
    html += '<td class="usage-col">' + (m.total_requests || 0) + '</td>';
    html += '<td class="usage-col">';
    if (m.last_used) {
      html += formatTimestamp(m.last_used);
    } else {
      html += '<span style="color:var(--yellow)">never</span>';
    }
    html += '</td>';
    html += '<td class="status-col">';
    if (m.loaded_in_vram) html += '<span class="vram-badge">VRAM</span> ';
    if (m.unused && !m.last_used) {
      html += '<span class="never-used-badge">never used</span>';
    } else if (m.unused) {
      html += '<span class="unused-badge">unused 7d+</span>';
    }
    html += '<span class="delete-status" id="del-' + nodeData.node_id + '-' + safeId + '"></span>';
    html += '</td>';
    html += '</tr>';
  });

  html += '</tbody></table>';
  html += '<div class="delete-section" id="delete-section-' + nodeData.node_id + '" style="display:none">';
  html += '<button class="delete-btn" id="delete-btn-' + nodeData.node_id + '" data-node="' + nodeData.node_id + '">';
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
  section.style.display = selected.length > 0 ? 'flex' : 'none';
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

  var html = '<div class="confirm-overlay" id="confirm-overlay">';
  html += '<div class="confirm-dialog">';
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
  html += '<div class="confirm-actions">';
  html += '<button class="confirm-cancel" id="confirm-cancel">Cancel</button>';
  html += '<button class="confirm-delete" id="confirm-proceed" data-node="' + nodeId + '">Delete</button>';
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

window.addEventListener('DOMContentLoaded', function() {
  var dot = document.getElementById('sse-dot');
  var st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

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

_SETTINGS_BODY = """
<style>
.settings-header { margin-bottom:24px; }
.settings-header h2 { font-size:20px; font-weight:600; margin-bottom:4px; }
.settings-header .version-info { font-size:13px; color:var(--text-dim); }
.settings-header .version-info span { color:var(--accent); font-weight:500; }

.section-label { font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; color:var(--text-dim); margin-bottom:12px; margin-top:28px; }
.section-label:first-of-type { margin-top:0; }

.toggle-container { display:flex; align-items:center; justify-content:space-between; padding:14px 18px; background:var(--card); border:1px solid var(--border); border-radius:10px; margin-bottom:8px; }
.toggle-info { flex:1; }
.toggle-label { font-size:14px; font-weight:500; }
.toggle-desc { font-size:12px; color:var(--text-dim); margin-top:2px; }
.toggle-env { font-size:11px; color:var(--text-dim); font-family:'SF Mono','Fira Code',monospace; margin-top:4px; opacity:0.7; }
.toggle-switch { position:relative; width:44px; height:24px; cursor:pointer; flex-shrink:0; margin-left:16px; }
.toggle-switch input { opacity:0; width:0; height:0; position:absolute; }
.toggle-slider { position:absolute; inset:0; background:var(--border); border-radius:12px; transition:.2s; }
.toggle-slider:before { content:''; position:absolute; height:18px; width:18px; left:3px; bottom:3px; background:var(--text-dim); border-radius:50%; transition:.2s; }
.toggle-switch input:checked + .toggle-slider { background:var(--accent); }
.toggle-switch input:checked + .toggle-slider:before { transform:translateX(20px); background:#fff; }

.config-table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--border); border-radius:10px; overflow:hidden; margin-bottom:16px; }
.config-table th { text-align:left; padding:10px 16px; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; color:var(--text-dim); border-bottom:1px solid var(--border); background:rgba(108,99,255,0.04); }
.config-table td { padding:10px 16px; font-size:13px; border-bottom:1px solid var(--border); }
.config-table tr:last-child td { border-bottom:none; }
.config-table .env-name { font-family:'SF Mono','Fira Code',monospace; font-size:11px; color:var(--text-dim); }
.config-table .config-val { font-weight:500; font-variant-numeric:tabular-nums; }
.config-note { font-size:11px; color:var(--text-dim); margin-top:4px; font-style:italic; }

.nodes-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:12px; margin-bottom:24px; }
.node-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px 18px; }
.node-card-header { display:flex; align-items:center; gap:8px; margin-bottom:12px; }
.node-card-header .node-name { font-size:14px; font-weight:600; }
.node-badge { font-size:10px; padding:2px 8px; border-radius:4px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }
.node-badge.router { background:rgba(108,99,255,0.15); color:var(--accent); }
.node-detail { display:flex; justify-content:space-between; padding:4px 0; font-size:13px; }
.node-detail .nd-label { color:var(--text-dim); }
.node-detail .nd-value { font-weight:500; font-variant-numeric:tabular-nums; }

.toast { position:fixed; bottom:24px; right:24px; background:var(--card); border:1px solid var(--accent); color:var(--text); padding:12px 20px; border-radius:10px; font-size:13px; font-weight:500; z-index:1000; opacity:0; transform:translateY(10px); transition:opacity .2s, transform .2s; pointer-events:none; }
.toast.show { opacity:1; transform:translateY(0); }

@media (max-width:768px) { .nodes-grid{grid-template-columns:1fr;} .config-table{font-size:12px;} }
</style>

<div class="main">
  <div class="settings-header">
    <h2>Settings</h2>
    <div class="version-info">Router <span id="router-version">...</span> on <span id="router-hostname">...</span></div>
  </div>

  <div class="section-label">Feature Toggles</div>
  <div id="toggles-container">
    <div style="text-align:center;padding:20px;color:var(--text-dim)">Loading...</div>
  </div>

  <div class="section-label">Context Management</div>
  <div id="context-mgmt">
    <div style="text-align:center;padding:20px;color:var(--text-dim)">Loading...</div>
  </div>

  <div class="section-label">Fleet Nodes</div>
  <div class="nodes-grid" id="nodes-grid">
    <div style="text-align:center;padding:20px;color:var(--text-dim)">Loading...</div>
  </div>

  <div class="section-label">Router Configuration</div>
  <div id="config-tables"></div>
  <div class="config-note">Configuration is set via environment variables with the FLEET_ prefix. Restart the router to apply changes.</div>
</div>

<div class="toast" id="toast"></div>

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

function showToast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function() { t.classList.remove('show'); }, 2500);
}

function statusDotHtml(status) {
  var cls = status === 'online' ? 'online' : status === 'degraded' ? 'degraded' : 'offline';
  return '<span class="status-dot ' + cls + '" style="display:inline-block;width:8px;height:8px;border-radius:50;margin-right:6px"></span>';
}

function renderSettings(data) {
  document.getElementById('router-version').textContent = 'v' + data.router_version;
  document.getElementById('router-hostname').textContent = data.router_hostname;

  // Toggles
  var tc = document.getElementById('toggles-container');
  var html = '';
  var toggles = data.config.toggles;
  for (var key in toggles) {
    var meta = TOGGLE_META[key] || {label:key, desc:''};
    var checked = toggles[key] ? 'checked' : '';
    var envName = 'FLEET_' + key.toUpperCase();
    html += '<div class="toggle-container">' +
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
    ng.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-dim)">No nodes registered yet</div>';
  } else {
    ng.innerHTML = data.nodes.map(function(n) {
      var dotCls = n.status === 'online' ? 'online' : n.status === 'degraded' ? 'degraded' : 'offline';
      var badge = n.is_router ? '<span class="node-badge router">Router</span>' : '';
      var ver = n.agent_version || 'unknown';
      var verColor = '';
      if (n.agent_version && n.agent_version !== data.router_version) {
        verColor = ' style="color:var(--yellow)"';
      }
      return '<div class="node-card">' +
        '<div class="node-card-header">' +
          '<span class="status-dot ' + dotCls + '"></span>' +
          '<span class="node-name">' + n.node_id + '</span>' +
          badge +
        '</div>' +
        '<div class="node-detail"><span class="nd-label">Status</span><span class="nd-value">' + n.status + '</span></div>' +
        '<div class="node-detail"><span class="nd-label">Version</span><span class="nd-value"' + verColor + '>' + ver + '</span></div>' +
        '<div class="node-detail"><span class="nd-label">Ollama</span><span class="nd-value">' + n.ip + '</span></div>' +
        '<div class="node-detail"><span class="nd-label">Models Loaded</span><span class="nd-value">' + n.models_loaded_count + '</span></div>' +
        (n.image_models && n.image_models.length ? '<div class="node-detail"><span class="nd-label">Image Models</span><span class="nd-value" style="color:var(--orange)">' + n.image_models.join(', ') + '</span></div>' : '') +
        (n.stt_models && n.stt_models.length ? '<div class="node-detail"><span class="nd-label">STT Models</span><span class="nd-value" style="color:var(--blue)">' + n.stt_models.join(', ') + '</span></div>' : '') +
        (n.embed_models && n.embed_models.length ? '<div class="node-detail"><span class="nd-label">Embed Models</span><span class="nd-value" style="color:var(--purple,#a855f7)">' + n.embed_models.join(', ') + '</span></div>' : '') +
        (n.vision_embed_models && n.vision_embed_models.length ? '<div class="node-detail"><span class="nd-label">Vision Embed</span><span class="nd-value" style="color:var(--cyan,#06b6d4)">' + n.vision_embed_models.join(', ') + '</span></div>' : '') +
      '</div>';
    }).join('');
  }

  // Config tables
  var ct = document.getElementById('config-tables');
  var tablesHtml = '';
  var configGroups = data.config;
  for (var group in configGroups) {
    if (group === 'toggles') continue;
    var label = CONFIG_LABELS[group] || group;
    var settings = configGroups[group];
    tablesHtml += '<table class="config-table"><thead><tr><th>' + label + '</th><th>Value</th><th>Env Var</th></tr></thead><tbody>';
    for (var sKey in settings) {
      var envName = 'FLEET_' + sKey.toUpperCase();
      tablesHtml += '<tr><td>' + sKey.replace(/_/g, '_') + '</td><td class="config-val">' + settings[sKey] + '</td><td class="env-name">' + envName + '</td></tr>';
    }
    tablesHtml += '</tbody></table>';
  }
  ct.innerHTML = tablesHtml;
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
      container.innerHTML = '<div style="padding:14px 18px;background:var(--card);border:1px solid var(--border);border-radius:10px;font-size:13px;color:var(--text-dim)">No models with usage data yet. Context recommendations appear after traffic is routed.</div>';
      return;
    }
    var html = '<table class="config-table"><thead><tr><th>Model</th><th>Allocated</th><th>p99 Total</th><th>Utilization</th><th>Recommended</th><th>Override</th><th></th></tr></thead><tbody>';
    data.models.forEach(function(m) {
      var utilColor = m.utilization_pct > 50 ? 'var(--green)' : m.utilization_pct > 10 ? 'var(--yellow)' : 'var(--red)';
      var override = m.override_ctx || '';
      html += '<tr>' +
        '<td style="font-weight:500">' + m.model + '</td>' +
        '<td class="config-val">' + (m.allocated_ctx ? m.allocated_ctx.toLocaleString() : '-') + '</td>' +
        '<td class="config-val">' + (m.total_tokens.p99 ? m.total_tokens.p99.toLocaleString() : '-') + '</td>' +
        '<td style="color:' + utilColor + ';font-weight:600">' + m.utilization_pct + '%</td>' +
        '<td class="config-val">' + m.recommended_ctx.toLocaleString() + ' <span style="color:var(--green);font-size:11px">(' + m.savings_pct + '% savings)</span></td>' +
        '<td><input type="number" id="ctx-override-' + m.model.replace(/[^a-zA-Z0-9]/g, '-') + '" value="' + override + '" placeholder="' + m.recommended_ctx + '" style="width:80px;background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:4px 8px;color:var(--text);font-size:12px;font-variant-numeric:tabular-nums"></td>' +
        '<td><button onclick="applyCtxOverride(\\'' + m.model + '\\',\\'' + m.model.replace(/[^a-zA-Z0-9]/g, '-') + '\\')" style="background:var(--accent);color:#fff;border:none;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer">Apply</button> ' +
        '<button onclick="applyRecommended(\\'' + m.model + '\\',' + m.recommended_ctx + ',\\'' + m.model.replace(/[^a-zA-Z0-9]/g, '-') + '\\')" style="background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:4px;padding:4px 8px;font-size:11px;cursor:pointer">Use Rec.</button></td>' +
      '</tr>';
    });
    html += '</tbody></table>';
    html += '<div class="config-note">Overrides take effect on next cold load or Ollama restart. Enable "Dynamic Context Size" toggle above to auto-inject these values.</div>';
    container.innerHTML = html;
  } catch(e) {
    container.innerHTML = '<div style="color:var(--text-dim);padding:20px">Error loading context data</div>';
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

window.addEventListener('DOMContentLoaded', function() {
  var dot = document.getElementById('sse-dot');
  var st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

loadSettings();
setInterval(loadSettings, 15000);
</script>
"""
