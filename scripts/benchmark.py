#!/usr/bin/env python3
"""
Ollama Herd Fleet Benchmark

Stress-tests the fleet by sending concurrent streaming requests to all loaded
models across all nodes for a configurable duration. Auto-discovers the fleet
topology via /fleet/status and computes saturation concurrency per node.

All requests are tagged with metadata.tags: ["benchmark"] so results appear
in the Apps dashboard tab.

Results are automatically saved to the router's SQLite database and
visible in the Benchmarks dashboard tab at /dashboard/benchmarks.

Usage:
    python scripts/benchmark.py                          # defaults: localhost:11435, 5 min
    python scripts/benchmark.py --duration 60            # 1-minute run
    python scripts/benchmark.py --url http://10.0.0.100:11435
    python scripts/benchmark.py --models qwen3:14b       # specific model only
    python scripts/benchmark.py --concurrency 4          # override per-model concurrency
    python scripts/benchmark.py --no-save                # don't store results
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

# KV cache estimate per concurrent request (same as queue manager)
_KV_CACHE_PER_REQUEST_GB = 2.0
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY_PER_MODEL = 6
_MAX_TOTAL_PER_NODE = 8  # Ollama can't efficiently handle more

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


@dataclass
class ModelTarget:
    name: str
    size_gb: float
    concurrency: int
    nodes: list[str] = field(default_factory=list)


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
        # Total concurrent requests this node can handle based on KV cache.
        mem_budget = int(available_gb / _KV_CACHE_PER_REQUEST_GB)
        node_budget = max(
            _MIN_CONCURRENCY,
            min(mem_budget, _MAX_TOTAL_PER_NODE),
        )

        # Split budget across models on this node
        n_models = len(loaded)
        base_per_model = max(1, node_budget // n_models)
        per_model_conc = min(base_per_model, _MAX_CONCURRENCY_PER_MODEL)

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

            # Ollama NDJSON: one JSON object per line
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # TTFT: first chunk with content
                msg = chunk.get("message", {})
                if first_content and msg.get("content"):
                    ttft = (time.monotonic() - start) * 1000
                    first_content = False

                # Final chunk has token counts
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
            except asyncio.TimeoutError:
                pass


async def warmup(
    client: httpx.AsyncClient,
    targets: list[ModelTarget],
) -> list[ModelTarget]:
    """Send one request per model to ensure all models are hot.
    Returns the list of targets that passed warmup (failed ones are removed)."""
    print("  Warming up models...")
    tasks = []
    for t in targets:
        tasks.append(send_request(client, t.name, timeout=120.0))
    results = await asyncio.gather(*tasks)

    passed = []
    for t, r in zip(targets, results):
        status = "ok" if r.success else f"FAILED: {r.error}"
        print(f"    {t.name:<35} {status} ({r.latency_ms:,.0f}ms)")
        if r.success:
            passed.append(t)

    if len(passed) < len(targets):
        skipped = len(targets) - len(passed)
        print(f"\n  Skipping {skipped} model(s) that failed warmup")

    if not passed:
        print("\n  ERROR: All models failed warmup. Nothing to benchmark.")
        sys.exit(1)

    print()
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
        except asyncio.TimeoutError:
            pass


def build_report(
    results: list[RequestResult],
    duration: float,
    targets: list[ModelTarget],
    nodes_info: list[NodeInfo],
    fleet_snapshots: list[dict],
) -> dict:
    """Build report data dict and print to terminal. Returns data for storage."""
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
            per_model.append({
                "model": model,
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
            {"name": t.name, "size_gb": t.size_gb, "concurrency": t.concurrency}
            for t in targets
        ],
    }

    report = {
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

    # Print report to terminal
    print()
    print("Fleet Benchmark Report")
    print("\u2550" * 70)

    print()
    print("  FLEET TOPOLOGY")
    print("  " + "\u2500" * 66)
    for ni in nodes_info:
        print(
            f"  {ni.node_id:<30} "
            f"{ni.cores} cores  "
            f"{ni.memory_total_gb:.0f} GB RAM  "
            f"{ni.memory_available_gb:.0f} GB free  "
            f"{len(ni.models)} models"
        )
    total_conc = sum(t.concurrency for t in targets)
    print(f"\n  Workers: {total_conc} total across {len(targets)} models")
    for t in targets:
        print(
            f"    {t.name:<35} {t.concurrency} workers"
            f"  ({t.size_gb:.0f} GB)"
        )

    if peak_util:
        print()
        print("  PEAK RESOURCE UTILIZATION")
        print("  " + "\u2500" * 66)
        for u in peak_util:
            print(
                f"  {u['node_id']:<25} "
                f"CPU {u['cpu_avg']:5.1f}% avg / {u['cpu_peak']:5.1f}% peak  "
                f"MEM {u['mem_avg']:5.1f}% avg / {u['mem_peak']:5.1f}% peak  "
                f"Active {u['active_peak']}"
            )

    print()
    print("  THROUGHPUT")
    print("  " + "\u2500" * 66)
    print(f"  Duration:       {duration:.1f}s")
    print(f"  Requests:       {len(success)} completed, {len(failed)} failed")
    print(
        f"  Tokens:         {total_prompt + total_completion:,} "
        f"({total_prompt:,} prompt + {total_completion:,} completion)"
    )
    if duration > 0 and success:
        print(
            f"  Throughput:     {req_s:.2f} req/s"
            f" | {tok_s:.1f} completion tok/s"
        )

    if latencies:
        print()
        print("  LATENCY")
        print("  " + "\u2500" * 66)
        print(f"  {'Metric':<16} {'p50':>8} {'p95':>8} {'p99':>8} {'avg':>8}")
        print(f"  {'\u2500'*16} {'\u2500'*8} {'\u2500'*8} {'\u2500'*8} {'\u2500'*8}")
        avg_lat = statistics.mean(latencies)
        print(
            f"  {'Total (ms)':<16} "
            f"{percentile(latencies, 50):>8,.0f} "
            f"{percentile(latencies, 95):>8,.0f} "
            f"{percentile(latencies, 99):>8,.0f} "
            f"{avg_lat:>8,.0f}"
        )
        if ttfts:
            avg_ttft = statistics.mean(ttfts)
            print(
                f"  {'TTFT (ms)':<16} "
                f"{percentile(ttfts, 50):>8,.0f} "
                f"{percentile(ttfts, 95):>8,.0f} "
                f"{percentile(ttfts, 99):>8,.0f} "
                f"{avg_ttft:>8,.0f}"
            )

    if per_model:
        print()
        print("  PER MODEL")
        print("  " + "\u2500" * 66)
        for m in per_model:
            print(
                f"  {m['model']:<30} {m['requests']:>3} req"
                f"  {m['tok_s']:>7.1f} tok/s"
                f"  {m['avg_latency_ms']:>8,.0f}ms lat"
                f"  {m['avg_ttft_ms']:>6,.0f}ms ttft"
            )

    if per_node:
        print()
        print("  PER NODE")
        print("  " + "\u2500" * 66)
        for n in per_node:
            print(
                f"  {n['node_id']:<30} {n['requests']:>3} req ({n['pct']:4.0f}%)"
                f"  {n['tok_s']:>7.1f} tok/s"
                f"  {n['tokens']:>8,} tokens"
            )

    if failed:
        print()
        print("  ERRORS")
        print("  " + "\u2500" * 66)
        error_counts: dict[str, int] = {}
        for r in failed:
            key = r.error or "unknown"
            if len(key) > 80:
                key = key[:80] + "..."
            error_counts[key] = error_counts.get(key, 0) + 1
        for err, count in sorted(error_counts.items(), key=lambda x: -x[1])[:5]:
            print(f"  {count:>4}x {err}")

    print()
    print("\u2550" * 70)
    print()

    return report


async def save_results(client: httpx.AsyncClient, report: dict):
    """POST benchmark results to the router for persistent storage."""
    try:
        resp = await client.post(
            "/dashboard/api/benchmarks",
            json=report,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"  Results saved (run_id: {data.get('run_id', 'unknown')})")
            print("  View at /dashboard/benchmarks")
        else:
            print(f"  Warning: Failed to save results (HTTP {resp.status_code})")
    except Exception as e:
        print(f"  Warning: Could not save results: {e}")


async def run_benchmark(
    url: str,
    duration: float,
    model_filter: list[str] | None,
    concurrency_override: int | None,
    skip_warmup: bool = False,
    no_save: bool = False,
):
    async with httpx.AsyncClient(base_url=url) as client:
        # Discover fleet
        print(f"\nDiscovering fleet at {url}...")
        try:
            targets, nodes_info = await discover_fleet(client)
        except httpx.HTTPError as e:
            print(f"Error: Could not connect to fleet at {url}: {e}", file=sys.stderr)
            sys.exit(1)

        if not targets:
            print("Error: No loaded models found on any online node.", file=sys.stderr)
            sys.exit(1)

        # Filter models if specified
        if model_filter:
            targets = [t for t in targets if t.name in model_filter]
            if not targets:
                print(
                    f"Error: None of the specified models are loaded: {model_filter}",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Override concurrency if specified
        if concurrency_override:
            for t in targets:
                t.concurrency = concurrency_override

        # Print discovery
        total_conc = sum(t.concurrency for t in targets)
        print(f"Found {len(nodes_info)} nodes, {len(targets)} loaded models\n")
        for ni in nodes_info:
            print(
                f"  {ni.node_id}: {ni.cores} cores, "
                f"{ni.memory_total_gb:.0f} GB RAM "
                f"({ni.memory_available_gb:.0f} GB free), "
                f"{len(ni.models)} models loaded"
            )
        print()
        for t in targets:
            print(
                f"  {t.name} ({t.size_gb:.0f} GB)"
                f" — {t.concurrency} workers"
                f" — {', '.join(t.nodes)}"
            )
        print(
            f"\n  Total workers: {total_conc} across "
            f"{len(targets)} models\n"
        )

        # Warmup (also removes models that fail)
        if not skip_warmup:
            targets = await warmup(client, targets)
            total_conc = sum(t.concurrency for t in targets)

        print(
            f"Starting {duration:.0f}s benchmark with "
            f"{total_conc} concurrent workers...\n"
        )

        # Run benchmark
        results: list[RequestResult] = []
        fleet_snapshots: list[dict] = []
        stop_event = asyncio.Event()

        # Create worker tasks
        tasks = []
        for target in targets:
            for _ in range(target.concurrency):
                tasks.append(
                    asyncio.create_task(
                        worker(client, target.name, results, stop_event)
                    )
                )

        # Fleet status poller
        poller_task = asyncio.create_task(
            poll_fleet_status(client, stop_event, fleet_snapshots)
        )

        # Progress ticker
        start_time = time.monotonic()

        async def progress():
            while not stop_event.is_set():
                elapsed = time.monotonic() - start_time
                ok = sum(1 for r in results if r.success)
                fail = sum(1 for r in results if not r.success)
                tok = sum(r.completion_tokens for r in results if r.success)
                tok_s = tok / elapsed if elapsed > 0 else 0
                remaining = max(0, duration - elapsed)
                print(
                    f"\r  [{elapsed:5.1f}s / {duration:.0f}s]"
                    f"  {ok} ok, {fail} err"
                    f"  | {tok_s:,.0f} tok/s"
                    f"  ({remaining:.0f}s left)     ",
                    end="",
                    flush=True,
                )
                await asyncio.sleep(2)

        progress_task = asyncio.create_task(progress())

        # Wait for duration
        await asyncio.sleep(duration)
        stop_event.set()

        # Wait for in-flight to finish (short timeout — don't wait forever)
        print(
            f"\r  Stopping... waiting for in-flight requests."
            f"                                  "
        )
        done, pending = await asyncio.wait(tasks, timeout=30)
        for t in pending:
            t.cancel()

        progress_task.cancel()
        poller_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass
        try:
            await poller_task
        except asyncio.CancelledError:
            pass

        # Use actual wall time but cap at 2x duration to ignore drain overhead
        actual_duration = time.monotonic() - start_time
        report_duration = min(actual_duration, duration * 2)
        report = build_report(
            results, report_duration, targets, nodes_info, fleet_snapshots
        )

        # Save results to the router
        if not no_save:
            await save_results(client, report)


def main():
    parser = argparse.ArgumentParser(
        description="Ollama Herd Fleet Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/benchmark.py                        # 5 min, all models
  python scripts/benchmark.py --duration 60          # 1 min run
  python scripts/benchmark.py --models qwen3:14b     # single model
  python scripts/benchmark.py --concurrency 2        # override workers per model
  python scripts/benchmark.py --no-save              # don't save results
        """,
    )
    parser.add_argument(
        "--url",
        default="http://localhost:11435",
        help="Router URL (default: http://localhost:11435)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Benchmark duration in seconds (default: 300 = 5 minutes)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        help="Specific model(s) to benchmark (default: all loaded)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help="Override per-model concurrency (default: auto-calculated from memory)",
    )
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Skip the warmup phase (models may need cold loading)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Don't save results to the router (skip POST to /dashboard/api/benchmarks)",
    )
    args = parser.parse_args()

    asyncio.run(
        run_benchmark(
            args.url,
            args.duration,
            args.models,
            args.concurrency,
            args.skip_warmup,
            args.no_save,
        )
    )


if __name__ == "__main__":
    main()
