#!/usr/bin/env python3
"""
Ollama Herd Fleet Benchmark (CLI)

Stress-tests the fleet by sending concurrent streaming requests to all loaded
models across all nodes for a configurable duration. Auto-discovers the fleet
topology via /fleet/status and computes saturation concurrency per node.

All requests are tagged with metadata.tags: ["benchmark"] so results appear
in the Apps dashboard tab.

Results are automatically saved to the router's SQLite database and
visible in the Benchmarks dashboard tab at /dashboard/benchmarks.

Core benchmark logic lives in fleet_manager.server.benchmark_engine.
This script is a CLI wrapper.

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
import sys
import time

import httpx

from fleet_manager.server.benchmark_engine import (
    build_report,
    discover_fleet,
    percentile,
    poll_fleet_status,
    warmup,
    worker,
)


def print_report(report, targets, nodes_info):
    """Print formatted benchmark report to terminal."""
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

    peak_util = report.get("peak_utilization", [])
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
    print(f"  Duration:       {report['duration_s']:.1f}s")
    print(f"  Requests:       {report['total_requests']} completed, {report['total_failures']} failed")
    total_tok = report["total_prompt_tokens"] + report["total_completion_tokens"]
    print(
        f"  Tokens:         {total_tok:,} "
        f"({report['total_prompt_tokens']:,} prompt + {report['total_completion_tokens']:,} completion)"
    )
    if report["total_requests"]:
        print(
            f"  Throughput:     {report['requests_per_sec']:.2f} req/s"
            f" | {report['tokens_per_sec']:.1f} completion tok/s"
        )

    if report.get("latency_p50_ms") is not None:
        print()
        print("  LATENCY")
        print("  " + "\u2500" * 66)
        print(f"  {'Metric':<16} {'p50':>8} {'p95':>8} {'p99':>8}")
        print(f"  {'\u2500'*16} {'\u2500'*8} {'\u2500'*8} {'\u2500'*8}")
        print(
            f"  {'Total (ms)':<16} "
            f"{report['latency_p50_ms']:>8,.0f} "
            f"{report['latency_p95_ms']:>8,.0f} "
            f"{report['latency_p99_ms']:>8,.0f}"
        )
        if report.get("ttft_p50_ms") is not None:
            print(
                f"  {'TTFT (ms)':<16} "
                f"{report['ttft_p50_ms']:>8,.0f} "
                f"{report['ttft_p95_ms']:>8,.0f} "
                f"{report['ttft_p99_ms']:>8,.0f}"
            )

    per_model = report.get("per_model_results", [])
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

    per_node = report.get("per_node_results", [])
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

    print()
    print("\u2550" * 70)
    print()


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
                f" \u2014 {t.concurrency} workers"
                f" \u2014 {', '.join(t.nodes)}"
            )
        print(
            f"\n  Total workers: {total_conc} across "
            f"{len(targets)} models\n"
        )

        # Warmup
        if not skip_warmup:
            targets = await warmup(
                client, targets,
                log_fn=lambda msg: print(f"  {msg}"),
            )
            if not targets:
                print("\n  ERROR: All models failed warmup. Nothing to benchmark.")
                sys.exit(1)
            total_conc = sum(t.concurrency for t in targets)
            print()

        print(
            f"Starting {duration:.0f}s benchmark with "
            f"{total_conc} concurrent workers...\n"
        )

        # Run benchmark
        results = []
        fleet_snapshots = []
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

        # Wait for in-flight to finish
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

        # Build and print report
        actual_duration = time.monotonic() - start_time
        report_duration = min(actual_duration, duration * 2)
        report = build_report(
            results, report_duration, targets, nodes_info, fleet_snapshots
        )
        print_report(report, targets, nodes_info)

        # Save results to the router
        if not no_save:
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
