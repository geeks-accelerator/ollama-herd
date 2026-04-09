"""
Core benchmark engine — shared by CLI (scripts/benchmark.py) and server-side runner.

Provides fleet discovery, concurrent request generation, and report building.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# KV cache estimate per concurrent request (same as queue manager)
KV_CACHE_PER_REQUEST_GB = 2.0
MIN_CONCURRENCY = 1
MAX_CONCURRENCY_PER_MODEL = 6
MAX_TOTAL_PER_NODE = 8  # Ollama can't efficiently handle more

# Short prompts that produce quick responses — maximizes request throughput
PROMPTS = [
    "In exactly one sentence, what is distributed computing?",
    "Name three benefits of load balancing in one sentence.",
    "What is a GPU cluster? One sentence only.",
    "Define inference routing in one sentence.",
    "What is model sharding? One sentence.",
    "Explain KV cache in one sentence.",
    "What is tensor parallelism? One sentence.",
    "Define throughput in computing. One sentence.",
    "What is latency? One sentence answer.",
    "Explain horizontal scaling in one sentence.",
]


EMBED_PROMPTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Distributed computing enables horizontal scaling across machines.",
    "Local inference preserves data privacy without cloud dependencies.",
    "GPU memory bandwidth determines token generation throughput.",
    "Model quantization trades precision for lower memory requirements.",
]

IMAGE_PROMPTS = [
    "a serene mountain landscape at sunset, photorealistic",
    "a futuristic robot reading a book in a library",
    "a cat wearing a tiny astronaut helmet floating in space",
    "an abstract painting of neural network connections",
    "a cozy coffee shop interior with warm lighting",
]


@dataclass
class RequestResult:
    model: str
    node_id: str
    latency_ms: float
    ttft_ms: float
    prompt_tokens: int
    completion_tokens: int
    success: bool
    error: str | None = None
    model_type: str = "llm"  # llm, embed, image, stt


@dataclass
class ModelTarget:
    name: str
    size_gb: float
    concurrency: int
    nodes: list[str] = field(default_factory=list)
    model_type: str = "llm"  # llm, embed, image, stt


@dataclass
class NodeInfo:
    node_id: str
    cores: int
    memory_total_gb: float
    memory_used_gb: float
    memory_available_gb: float
    models: list[str] = field(default_factory=list)
    model_sizes: dict[str, float] = field(default_factory=dict)


async def discover_fleet(
    client: httpx.AsyncClient,
) -> tuple[list[ModelTarget], list[NodeInfo]]:
    """Auto-discover loaded models and nodes, compute concurrency per node.

    Concurrency is calculated per-node:
    1. Total node budget = min(available_gb / KV_CACHE, MAX_TOTAL_PER_NODE)
    2. Budget is split evenly across models on that node
    3. Each model's share is capped at MAX_CONCURRENCY_PER_MODEL
    """
    resp = await client.get("/fleet/status")
    resp.raise_for_status()
    data = resp.json()

    nodes_info: list[NodeInfo] = []
    model_map: dict[str, ModelTarget] = {}

    for node in data.get("nodes", []):
        if node.get("status") != "online":
            continue
        node_id = node["node_id"]
        ollama = node.get("ollama") or {}
        mem = node.get("memory", {})
        cpu = node.get("cpu", {})

        loaded = ollama.get("models_loaded", [])
        available_gb = mem.get("available_gb", 0)

        model_sizes = {}
        for m in loaded:
            model_sizes[m["name"]] = m.get("size_gb", 0)

        nodes_info.append(NodeInfo(
            node_id=node_id,
            cores=cpu.get("cores_physical", 0),
            memory_total_gb=mem.get("total_gb", 0),
            memory_used_gb=mem.get("used_gb", 0),
            memory_available_gb=available_gb,
            models=[m["name"] for m in loaded],
            model_sizes=model_sizes,
        ))

        if not loaded:
            continue

        # Available memory is already free (models are loaded and accounted for).
        mem_budget = int(available_gb / KV_CACHE_PER_REQUEST_GB)
        node_budget = max(
            MIN_CONCURRENCY,
            min(mem_budget, MAX_TOTAL_PER_NODE),
        )

        n_models = len(loaded)
        base_per_model = max(1, node_budget // n_models)
        per_model_conc = min(base_per_model, MAX_CONCURRENCY_PER_MODEL)

        for m in loaded:
            name = m["name"]
            size_gb = m.get("size_gb", 0)

            if name not in model_map:
                model_map[name] = ModelTarget(
                    name=name, size_gb=size_gb, concurrency=per_model_conc
                )
            else:
                model_map[name].concurrency = max(
                    model_map[name].concurrency, per_model_conc
                )
            model_map[name].nodes.append(node_id)

    return list(model_map.values()), nodes_info


async def discover_embed_models(
    client: httpx.AsyncClient,
) -> list[ModelTarget]:
    """Find embedding models loaded on the fleet."""
    resp = await client.get("/fleet/status")
    resp.raise_for_status()
    data = resp.json()

    targets = []
    embed_patterns = ("embed", "nomic", "bge", "e5-")
    for node in data.get("nodes", []):
        if node.get("status") != "online":
            continue
        ollama = node.get("ollama") or {}
        for m in ollama.get("models_loaded", []):
            name = m["name"]
            is_embed = any(p in name.lower() for p in embed_patterns)
            already_found = any(t.name == name for t in targets)
            if is_embed and not already_found:
                    targets.append(ModelTarget(
                        name=name, size_gb=m.get("size_gb", 0),
                        concurrency=2, nodes=[node["node_id"]],
                        model_type="embed",
                    ))
    return targets


async def discover_image_models(
    client: httpx.AsyncClient,
) -> list[ModelTarget]:
    """Find image generation models available on the fleet."""
    try:
        resp = await client.get("/api/image-models")
        if resp.status_code != 200:
            return []
        data = resp.json()
        targets = []
        for m in data.get("models", []):
            name = m["name"]
            # Only benchmark one image model to avoid long runs
            if not targets:
                targets.append(ModelTarget(
                    name=name, size_gb=0,
                    concurrency=1,  # Image gen is slow, 1 at a time
                    nodes=m.get("fleet_nodes", []),
                    model_type="image",
                ))
        return targets
    except Exception:
        return []


async def send_embed_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 60.0,
) -> RequestResult:
    """Send an embedding request and measure latency."""
    body = {
        "model": model,
        "input": random.choice(EMBED_PROMPTS),
        "metadata": {"tags": ["benchmark"]},
    }
    start = time.monotonic()
    try:
        resp = await client.post("/api/embed", json=body, timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        if resp.status_code != 200:
            return RequestResult(
                model=model, node_id="unknown", latency_ms=latency_ms,
                ttft_ms=0, prompt_tokens=0, completion_tokens=0,
                success=False, error=f"HTTP {resp.status_code}",
                model_type="embed",
            )
        node_id = resp.headers.get("x-fleet-node", "unknown")
        return RequestResult(
            model=model, node_id=node_id, latency_ms=latency_ms,
            ttft_ms=latency_ms, prompt_tokens=0, completion_tokens=1,
            success=True, model_type="embed",
        )
    except Exception as e:
        return RequestResult(
            model=model, node_id="unknown",
            latency_ms=(time.monotonic() - start) * 1000,
            ttft_ms=0, prompt_tokens=0, completion_tokens=0,
            success=False, error=str(e)[:120], model_type="embed",
        )


async def send_image_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 120.0,
) -> RequestResult:
    """Send an image generation request and measure latency."""
    body = {
        "model": model,
        "prompt": random.choice(IMAGE_PROMPTS),
        "size": "512x512",
        "steps": 4,  # Fast for benchmarking
        "metadata": {"tags": ["benchmark"]},
    }
    start = time.monotonic()
    try:
        resp = await client.post(
            "/api/generate-image", json=body, timeout=timeout
        )
        latency_ms = (time.monotonic() - start) * 1000
        node_id = resp.headers.get("x-fleet-node", "unknown")
        if resp.status_code != 200:
            return RequestResult(
                model=model, node_id=node_id, latency_ms=latency_ms,
                ttft_ms=0, prompt_tokens=0, completion_tokens=0,
                success=False, error=f"HTTP {resp.status_code}",
                model_type="image",
            )
        return RequestResult(
            model=model, node_id=node_id, latency_ms=latency_ms,
            ttft_ms=latency_ms, prompt_tokens=0, completion_tokens=1,
            success=True, model_type="image",
        )
    except Exception as e:
        return RequestResult(
            model=model, node_id="unknown",
            latency_ms=(time.monotonic() - start) * 1000,
            ttft_ms=0, prompt_tokens=0, completion_tokens=0,
            success=False, error=str(e)[:120], model_type="image",
        )


async def embed_worker(
    client: httpx.AsyncClient,
    model: str,
    results: list[RequestResult],
    stop_event: asyncio.Event,
    error_backoff: float = 1.0,
):
    """Continuously send embedding requests until stop_event is set."""
    while not stop_event.is_set():
        result = await send_embed_request(client, model)
        results.append(result)
        if not result.success:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=error_backoff)
                break
            except TimeoutError:
                pass


async def image_worker(
    client: httpx.AsyncClient,
    model: str,
    results: list[RequestResult],
    stop_event: asyncio.Event,
    error_backoff: float = 2.0,
):
    """Continuously send image generation requests until stop_event is set."""
    while not stop_event.is_set():
        result = await send_image_request(client, model)
        results.append(result)
        if not result.success:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=error_backoff)
                break
            except TimeoutError:
                pass


async def send_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 300.0,
) -> RequestResult:
    """Send a streaming request via Ollama format to get token counts."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": random.choice(PROMPTS)}],
        "stream": True,
        "metadata": {"tags": ["benchmark"]},
    }

    start = time.monotonic()
    ttft = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    node_id = ""
    first_content = True

    try:
        async with client.stream(
            "POST",
            "/api/chat",
            json=body,
            timeout=timeout,
        ) as resp:
            node_id = resp.headers.get("x-fleet-node", "unknown")

            if resp.status_code != 200:
                raw = await resp.aread()
                return RequestResult(
                    model=model,
                    node_id=node_id,
                    latency_ms=(time.monotonic() - start) * 1000,
                    ttft_ms=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    success=False,
                    error=f"HTTP {resp.status_code}: {raw.decode()[:200]}",
                )

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = chunk.get("message", {})
                if first_content and msg.get("content"):
                    ttft = (time.monotonic() - start) * 1000
                    first_content = False

                if chunk.get("done"):
                    prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                    completion_tokens = chunk.get("eval_count", 0) or 0

        latency_ms = (time.monotonic() - start) * 1000
        return RequestResult(
            model=model,
            node_id=node_id,
            latency_ms=latency_ms,
            ttft_ms=ttft,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            success=True,
        )

    except Exception as e:
        return RequestResult(
            model=model,
            node_id=node_id or "unknown",
            latency_ms=(time.monotonic() - start) * 1000,
            ttft_ms=ttft,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error=str(e)[:120],
        )


async def worker(
    client: httpx.AsyncClient,
    model: str,
    results: list[RequestResult],
    stop_event: asyncio.Event,
    error_backoff: float = 1.0,
):
    """Continuously send requests until stop_event is set, with error backoff."""
    while not stop_event.is_set():
        result = await send_request(client, model)
        results.append(result)
        if not result.success:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=error_backoff)
                break
            except TimeoutError:
                pass


async def warmup(
    client: httpx.AsyncClient,
    targets: list[ModelTarget],
    log_fn=None,
) -> list[ModelTarget]:
    """Send one request per model to ensure all models are hot.
    Returns the list of targets that passed warmup (failed ones are removed).

    log_fn: optional callback(message: str) for progress reporting.
    """
    if log_fn:
        log_fn("Warming up models...")
    tasks = []
    for t in targets:
        tasks.append(send_request(client, t.name, timeout=120.0))
    results = await asyncio.gather(*tasks)

    passed = []
    for t, r in zip(targets, results, strict=True):
        status = "ok" if r.success else f"FAILED: {r.error}"
        if log_fn:
            log_fn(f"  {t.name}: {status} ({r.latency_ms:,.0f}ms)")
        if r.success:
            passed.append(t)

    return passed


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_v):
        return sorted_v[f]
    return sorted_v[f] + (k - f) * (sorted_v[c] - sorted_v[f])


async def poll_fleet_status(
    client: httpx.AsyncClient,
    stop_event: asyncio.Event,
    snapshots: list[dict],
):
    """Periodically poll /fleet/status to capture utilization during benchmark."""
    while not stop_event.is_set():
        try:
            resp = await client.get("/fleet/status", timeout=5)
            if resp.status_code == 200:
                snapshots.append(resp.json())
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
            break
        except TimeoutError:
            pass


def build_report(
    results: list[RequestResult],
    duration: float,
    targets: list[ModelTarget],
    nodes_info: list[NodeInfo],
    fleet_snapshots: list[dict],
) -> dict:
    """Build report data dict. Returns data for storage."""
    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    total_prompt = sum(r.prompt_tokens for r in success)
    total_completion = sum(r.completion_tokens for r in success)

    latencies = [r.latency_ms for r in success]
    ttfts = [r.ttft_ms for r in success if r.ttft_ms > 0]

    req_s = len(success) / duration if duration > 0 else 0
    tok_s = total_completion / duration if duration > 0 else 0

    # Build per-model results
    per_model = []
    if success:
        for model in sorted(set(r.model for r in success)):
            mr = [r for r in success if r.model == model]
            m_tok = sum(r.completion_tokens for r in mr)
            m_lat = statistics.mean(r.latency_ms for r in mr)
            m_tok_s = m_tok / duration if duration > 0 else 0
            m_ttft = (
                statistics.mean(r.ttft_ms for r in mr if r.ttft_ms > 0)
                if any(r.ttft_ms > 0 for r in mr)
                else 0
            )
            # Determine model type from results
            m_type = mr[0].model_type if mr else "llm"
            per_model.append({
                "model": model,
                "model_type": m_type,
                "requests": len(mr),
                "tok_s": round(m_tok_s, 1),
                "avg_latency_ms": round(m_lat, 1),
                "avg_ttft_ms": round(m_ttft, 1),
            })

    # Build per-node results
    per_node = []
    if success:
        for node in sorted(set(r.node_id for r in success)):
            nr = [r for r in success if r.node_id == node]
            n_pct = len(nr) / len(success) * 100
            n_tok = sum(r.completion_tokens for r in nr)
            n_tok_s = n_tok / duration if duration > 0 else 0
            per_node.append({
                "node_id": node,
                "requests": len(nr),
                "pct": round(n_pct, 1),
                "tok_s": round(n_tok_s, 1),
                "tokens": n_tok,
            })

    # Build peak utilization
    peak_util = []
    if fleet_snapshots:
        node_cpu_max: dict[str, float] = {}
        node_mem_max: dict[str, float] = {}
        node_active_max: dict[str, int] = {}
        node_cpu_samples: dict[str, list[float]] = {}
        node_mem_samples: dict[str, list[float]] = {}
        for snap in fleet_snapshots:
            for node in snap.get("nodes", []):
                nid = node["node_id"]
                cpu = node.get("cpu", {}).get("utilization_pct", 0)
                mem_total = node.get("memory", {}).get("total_gb", 1)
                mem_used = node.get("memory", {}).get("used_gb", 0)
                mem_pct = (mem_used / mem_total) * 100 if mem_total > 0 else 0
                active = node.get("ollama", {}).get("requests_active", 0)
                node_cpu_max[nid] = max(node_cpu_max.get(nid, 0), cpu)
                node_mem_max[nid] = max(node_mem_max.get(nid, 0), mem_pct)
                node_active_max[nid] = max(node_active_max.get(nid, 0), active)
                node_cpu_samples.setdefault(nid, []).append(cpu)
                node_mem_samples.setdefault(nid, []).append(mem_pct)

        for nid in sorted(node_cpu_max.keys()):
            peak_util.append({
                "node_id": nid,
                "cpu_avg": round(statistics.mean(node_cpu_samples.get(nid, [0])), 1),
                "cpu_peak": round(node_cpu_max[nid], 1),
                "mem_avg": round(statistics.mean(node_mem_samples.get(nid, [0])), 1),
                "mem_peak": round(node_mem_max[nid], 1),
                "active_peak": node_active_max[nid],
            })

    # Fleet snapshot for storage
    fleet_snapshot = {
        "nodes": [
            {
                "node_id": ni.node_id,
                "cores": ni.cores,
                "memory_total_gb": ni.memory_total_gb,
                "memory_available_gb": ni.memory_available_gb,
            }
            for ni in nodes_info
        ],
        "models": [
            {
                "name": t.name, "size_gb": t.size_gb,
                "concurrency": t.concurrency, "model_type": t.model_type,
            }
            for t in targets
        ],
    }

    return {
        "run_id": f"bench-{int(time.time())}",
        "timestamp": time.time(),
        "duration_s": round(duration, 1),
        "total_requests": len(success),
        "total_failures": len(failed),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "requests_per_sec": round(req_s, 2),
        "tokens_per_sec": round(tok_s, 1),
        "latency_p50_ms": round(percentile(latencies, 50), 1) if latencies else None,
        "latency_p95_ms": round(percentile(latencies, 95), 1) if latencies else None,
        "latency_p99_ms": round(percentile(latencies, 99), 1) if latencies else None,
        "ttft_p50_ms": round(percentile(ttfts, 50), 1) if ttfts else None,
        "ttft_p95_ms": round(percentile(ttfts, 95), 1) if ttfts else None,
        "ttft_p99_ms": round(percentile(ttfts, 99), 1) if ttfts else None,
        "fleet_snapshot": fleet_snapshot,
        "per_model_results": per_model,
        "per_node_results": per_node,
        "peak_utilization": peak_util,
    }
