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

``--output FILE`` writes the whole report as JSON, with every request
(``requests``), the model loads and evictions seen (``model_events``) and the
run manifest (versions, commit, settings, seed): the file a published result
is.  The dashboard copy leaves the per-request records out.

Usage:
    python scripts/benchmark.py                          # defaults: localhost:11435, 5 min
    python scripts/benchmark.py --duration 60            # 1-minute run
    python scripts/benchmark.py --url http://10.0.0.100:11435
    python scripts/benchmark.py --models qwen3:14b       # specific model only
    python scripts/benchmark.py --concurrency 4          # override per-model concurrency
    python scripts/benchmark.py --no-save                # don't store results
    python scripts/benchmark.py --types llm,vision       # images to vision-capable models

Two models strictly alternating, one request at a time (what a swap costs):
    python scripts/benchmark.py --models A B --sequence alternate --concurrency 1 \\
        --requests 20 --max-tokens 128 --temperature 0 --skip-warmup \\
        --prompt "List three facts about the moon, one line each." --output arm4.json
"""

import argparse
import asyncio
import contextlib
import json
import math
import sys

import httpx

from fleet_manager.server.benchmark_engine import (
    DEFAULT_TYPES,
    WORKLOAD_TYPES,
    bench_client,
    build_report,
    dashboard_record,
    discover_fleet,
    explicit_targets,
    match_requested,
    parse_models,
    run_load,
    select_workloads,
    session_system_prompt,
    warmup,
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
        print(f"    {t.name:<35} {t.concurrency} workers  ({t.size_gb:.0f} GB, {t.workload})")

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
    cut = report.get("cancelled_at_drain")
    print(
        f"  Requests:       {report['total_requests']} completed, {report['total_failures']} failed"
        + (f" ({cut} still running when the drain ended)" if cut else "")
    )
    total_tok = report["total_prompt_tokens"] + report["total_completion_tokens"]
    print(
        f"  Tokens:         {total_tok:,} "
        f"({report['total_prompt_tokens']:,} prompt"
        f" + {report['total_completion_tokens']:,} completion)"
    )
    if report.get("served_model_mismatches"):
        print(
            f"  Mismatches:     {report['served_model_mismatches']} answered by another model"
            " (counted as failures)"
        )
    if report["total_requests"]:
        print(
            f"  Throughput:     {report['requests_per_sec']:.2f} req/s"
            f" | {report['tokens_per_sec']:.1f} completion tok/s (chat requests)"
        )
    per_type = report.get("per_type_results", [])
    if len(per_type) > 1:
        for t in per_type:
            p50 = t["latency_p50_ms"]
            print(
                f"    {t['model_type']:<12} {t['requests']:>5} ok {t['failures']:>4} failed"
                f"  {t['requests_per_sec']:>7.2f} req/s"
                + (f"  latency p50 {p50:,.0f}ms" if p50 is not None else "")
            )

    if report.get("latency_p50_ms") is not None:
        print()
        print("  LATENCY")
        print("  " + "\u2500" * 66)
        pcts = ("p50", "p90", "p95", "p99")
        print(f"  {'Metric':<16} " + " ".join(f"{p:>8}" for p in pcts))
        rule = "\u2500"  # a backslash inside an f-string needs Python 3.12; we support 3.11
        print(f"  {rule * 16} " + " ".join(rule * 8 for _ in pcts))
        for label, name in (("Total (ms)", "latency"), ("TTFT (ms)", "ttft")):
            if report.get(f"{name}_p50_ms") is not None:
                values = " ".join(f"{report[f'{name}_{p}_ms']:>8,.0f}" for p in pcts)
                print(f"  {label:<16} {values}")
        lag = report.get("schedule_lag_ms")
        if lag:
            print(
                f"  Schedule lag:   p50 {lag['p50']:.2f} ms, p99 {lag['p99']:.2f} ms,"
                f" max {lag['max']:.2f} ms (open loop: above ~2 ms the client fell behind)"
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
            if "first_ttft_ms" in m:
                print(
                    f"  {'':<30} first ttft {m['first_ttft_ms']:,.0f}ms,"
                    f" after it p50 {m['ttft_p50_ms_after_first']:,.0f}ms"
                    f" / max {m['ttft_max_ms_after_first']:,.0f}ms"
                )

    events = report.get("model_events", [])
    if events:
        print()
        print("  MODEL LOADS AND EVICTIONS (from fleet polls, ~3-8 s resolution)")
        print("  " + "\u2500" * 66)
        for e in events:
            print(f"  {e['t_ms'] / 1000:>7.1f}s  {e['node_id']:<25} {e['event']:<9} {e['model']}")

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


async def run_benchmark(args: argparse.Namespace) -> None:
    names, weights = parse_models(args.models or [])
    observe = args.fleet_url != "none"
    fleet_url = args.fleet_url or args.url
    # Weights mean a draw by weight; an open loop needs some order: alternate.
    sequence = args.sequence or ("random" if weights else "alternate" if args.rate else None)
    duration = (
        args.duration if args.duration is not None else (math.inf if args.requests else 300.0)
    )
    async with contextlib.AsyncExitStack() as stack:
        client = await stack.enter_async_context(bench_client(args.url, args.timeout))
        fleet_client = None
        if observe:
            fleet_client = client
            if fleet_url != args.url:
                fleet_client = await stack.enter_async_context(bench_client(fleet_url))
            print(f"\nDiscovering fleet at {fleet_url}...")
            try:
                targets, nodes_info = await discover_fleet(fleet_client, include_on_disk=names)
            except httpx.HTTPError as e:
                print(f"Error: Could not connect to fleet at {fleet_url}: {e}", file=sys.stderr)
                sys.exit(1)
            if names:
                targets, weights, missing = match_requested(targets, names, weights)
                if missing:
                    # Fail, never shrink: a run that quietly drops a requested
                    # model measures something other than its label says.  Run
                    # 6's embeddings arm warned, ran without nomic-embed-text,
                    # and its report looked complete.
                    print(
                        f"Error: not loaded or on disk anywhere: {', '.join(missing)}",
                        file=sys.stderr,
                    )
                    sys.exit(2)
        else:
            # Nothing exposes /fleet/status (plain Ollama, llama-swap): the
            # models as named, no nodes to warm or observe.
            targets, nodes_info = explicit_targets(names, args.concurrency or 1), []
        found = targets
        targets = select_workloads(found, args.types)
        if not targets:
            if found:
                kinds = sorted({t.model_type for t in found})
                print(
                    f"Error: no models of the types asked for ({','.join(args.types)});"
                    f" found {', '.join(kinds)}",
                    file=sys.stderr,
                )
            else:
                wanted = f": {names}" if names else ""
                print(f"Error: none of the models are loaded or on disk{wanted}", file=sys.stderr)
            sys.exit(1)
        if args.url != fleet_url and observe:
            print(f"  Sending traffic to {args.url}, observing the fleet at {fleet_url}")

        # Per-model workers take --concurrency each; a sequence or a rate shares
        # --concurrency workers (or the arrival rate) across all the models.
        shared = bool(sequence or args.rate)
        if args.concurrency and not shared:
            for t in targets:
                t.concurrency = args.concurrency

        total_conc = args.concurrency or sum(t.concurrency for t in targets)
        print(f"Found {len(nodes_info)} nodes, {len(targets)} models\n")
        for ni in nodes_info:
            print(
                f"  {ni.node_id}: {ni.cores} cores, "
                f"{ni.memory_total_gb:.0f} GB RAM "
                f"({ni.memory_available_gb:.0f} GB free), "
                f"{len(ni.models)} models loaded"
            )
        print()
        for t in targets:
            weight = f", weight {weights[t.name]:g}" if t.name in weights else ""
            kind = (
                t.model_type if t.workload == t.model_type else f"{t.model_type}, {t.workload} work"
            )
            print(
                f"  {t.name} ({t.size_gb:.0f} GB, {kind}{weight})"
                + ("" if shared else f" — {t.concurrency} workers")
                + (f" — {', '.join(t.nodes)}" if t.nodes else "")
            )
        if args.rate:
            print(f"\n  Open loop: {args.rate:g} requests/s, models {sequence}\n")
        elif shared:
            print(f"\n  {total_conc} workers sharing the models, {sequence}\n")
        else:
            print(f"\n  Total workers: {total_conc} across {len(targets)} models\n")

        # Warmup: every node that has each model, directly
        if not args.skip_warmup and nodes_info:
            targets = await warmup(targets, nodes_info, log_fn=lambda msg: print(f"  {msg}"))
            if not targets:
                print("\n  ERROR: All models failed warmup. Nothing to benchmark.")
                sys.exit(1)
            print()

        stop = f"{args.requests} requests" if args.requests else ""
        if math.isfinite(duration):
            stop = f"{stop} or {duration:.0f}s" if stop else f"{duration:.0f}s"
        print(f"Starting benchmark: {stop}...\n", flush=True)

        results: list = []

        # Multi-turn sessions over one shared system prompt; either through the
        # router (sessions tagged) or round-robin straight to each node's Ollama.
        sessions = None
        if args.session_turns:
            clients = [(client, "")]
            if args.round_robin:
                urls = sorted({ni.ollama_url for ni in nodes_info if ni.ollama_url})
                clients = [
                    (
                        await stack.enter_async_context(bench_client(u)),
                        u.split("//")[-1].split(":")[0],
                    )
                    for u in urls
                ]
                print(f"  Round-robin across {len(clients)} nodes' Ollama, bypassing the router\n")
            sessions = {
                "clients": clients,
                "system_prompt": session_system_prompt(args.system_tokens),
                "turns": args.session_turns,
                "user_tokens": args.user_tokens,
                "max_tokens": args.max_tokens or 150,
                "tag_sessions": not args.round_robin,
            }

        def progress(elapsed: float) -> None:
            ok = sum(1 for r in results if r.success)
            fail = len(results) - ok
            tok = sum(r.completion_tokens for r in results if r.success)
            tok_s = tok / elapsed if elapsed > 0 else 0
            left = f"  ({max(0, duration - elapsed):.0f}s left)" if math.isfinite(duration) else ""
            print(
                f"\r  [{elapsed:5.1f}s]  {ok} ok, {fail} err  | {tok_s:,.0f} tok/s{left}     ",
                end="",
                flush=True,
            )

        think = False if args.no_think else None
        load = await run_load(
            client,
            targets,
            duration,
            asyncio.Event(),
            results,
            prompt_tokens=args.prompt_tokens,
            max_tokens=args.max_tokens,
            think=think,
            prompt=args.prompt,
            temperature=args.temperature,
            api=args.api,
            timeout=args.timeout,
            on_tick=progress,
            sessions=sessions,
            fleet_client=fleet_client,
            sequence=sequence,
            weights=weights or None,
            concurrency=args.concurrency if shared else None,
            rate=args.rate,
            max_requests=args.requests,
            seed=args.seed,
        )
        print("\r  Done.                                                                         ")

        # Saved with the run, so a result always says what load produced it.
        workload = {
            "url": args.url,
            "fleet_url": fleet_url if observe else None,
            "api": args.api,
            "types": args.types,
            "models": names or None,
            "weights": weights or None,
            "sequence": sequence,
            "concurrency": args.concurrency,
            "rate": args.rate,
            "requests": args.requests,
            "duration_s": duration if math.isfinite(duration) else None,
            "prompt": args.prompt,
            "prompt_tokens": args.prompt_tokens,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "think": think,
            "timeout_s": args.timeout,
            "warmup": not args.skip_warmup and bool(nodes_info),
        }
        if args.session_turns:
            workload.update(
                session_turns=args.session_turns,
                system_tokens=args.system_tokens,
                user_tokens=args.user_tokens,
                target="round-robin" if args.round_robin else "router",
            )
        report = build_report(
            results,
            load.window_s,
            targets,
            nodes_info,
            load.snapshots,
            run={
                "label": args.label,
                "workload": workload,
                "seed": load.seed,
                "repetition": args.repetition,
            },
            started_at=load.started_at,
            ended_at=load.ended_at,
        )
        print_report(report, targets, nodes_info)

        if args.output:
            with open(args.output, "w") as f:
                json.dump(report, f, indent=2)
                f.write("\n")
            print(f"  Report written to {args.output} ({len(report['requests'])} requests)")

        # Save results to the router (the one observing, when that isn't --url)
        if not args.no_save and fleet_client is not None:
            try:
                resp = await fleet_client.post(
                    "/dashboard/api/benchmarks",
                    json=dashboard_record(report),
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


def _types(value: str) -> list[str]:
    types = [t.strip() for t in value.split(",") if t.strip()]
    unknown = sorted(set(types) - set(WORKLOAD_TYPES))
    if unknown or not types:
        raise argparse.ArgumentTypeError(
            f"unknown type(s) {unknown}; choose from {','.join(WORKLOAD_TYPES)}"
        )
    return types


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
  python scripts/benchmark.py --models a=3,b=1 --sequence random --seed 7 --concurrency 4
  python scripts/benchmark.py --models a b --rate 5 --duration 60   # open loop, 5 req/s
        """,
    )
    parser.add_argument(
        "--url",
        default="http://localhost:11435",
        help="Where requests go: the router, or any endpoint under test (default: "
        "http://localhost:11435)",
    )
    parser.add_argument(
        "--fleet-url",
        help="The router polled for fleet state (discovery, model loads and evictions, "
        "utilization) when traffic goes elsewhere, e.g. an HAProxy or a node's Ollama "
        "(default: --url). 'none' for an endpoint with no router at all (plain Ollama, "
        "llama-swap): --models are then used as named, with no warmup and nothing observed",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Benchmark duration in seconds (default: 300, or no limit with --requests)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        help="Stop after this many requests in total (with --duration, whichever comes first)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        help="Specific model(s) to benchmark (default: all loaded), optionally weighted: "
        "'a b', 'a=3,b=1' or 'a=3 b'. Weights imply --sequence random unless set",
    )
    parser.add_argument(
        "--types",
        type=_types,
        default=list(DEFAULT_TYPES),
        help=f"Comma-separated workloads to run, of {','.join(WORKLOAD_TYPES)} (default: "
        f"{','.join(DEFAULT_TYPES)}). A vision-capable chat model (gemma3, llava) is a chat "
        "model: it gets the text workload under llm, and an image with each request only "
        "when vision is listed",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help="Workers per model (default: auto-calculated from memory); with --sequence, "
        "workers in total, sharing the models",
    )
    parser.add_argument(
        "--sequence",
        choices=("alternate", "random"),
        help="Workers share the models: each request goes to the next model in turn "
        "(alternate, weights repeat a model) or to one drawn by weight (random)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        help="Open loop: start this many requests per second on schedule whether or not "
        "earlier ones finished, instead of fixed concurrency (models: --sequence, "
        "default alternate)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Seed for every random choice (prompts, model sequence, session ids); the "
        "report records the one used, so any run can be repeated (default: a random seed)",
    )
    parser.add_argument(
        "--repetition", type=int, help="Which repetition of this arm the run is (recorded)"
    )
    parser.add_argument(
        "--label",
        help="Free text recorded in the manifest: what the manifest can't see, such as an "
        "endpoint that isn't herd (llama-swap's version, the hardware)",
    )
    parser.add_argument(
        "--api",
        choices=("ollama", "openai"),
        default="ollama",
        help="Chat API: Ollama /api/chat (default) or OpenAI /v1/chat/completions, for "
        "endpoints that speak only that",
    )
    parser.add_argument(
        "--output",
        help="Write the whole report as JSON: every request, model events, the manifest",
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
    parser.add_argument(
        "--prompt",
        help="Send this user message on every chat request instead of a generated one",
    )
    parser.add_argument(
        "--prompt-tokens",
        type=int,
        default=None,
        help="Pad each chat prompt to about this many tokens, with different text every request "
        "so no cached prefix helps (default: a ~10-token question). Real requests average "
        "~1,250 (ollamaherd.com/stats)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Cap each reply at this many tokens (Ollama num_predict; default: no cap). "
        "Real replies average ~200",
    )
    parser.add_argument(
        "--temperature", type=float, help="Sampling temperature (default: the model's own)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Seconds per chat request (default 300); raise it when first requests are cold "
        "loads of large models",
    )
    parser.add_argument(
        "--no-think",
        action="store_true",
        help='Send "think": false, so a thinking model answers without reasoning and the '
        "router does not raise --max-tokens to its thinking minimum (1,024)",
    )
    parser.add_argument(
        "--session-turns",
        type=int,
        default=0,
        help="Multi-turn sessions of this many turns over one shared system prompt "
        "(what coding agents send) instead of one-shot requests; --concurrency sessions at once",
    )
    parser.add_argument("--system-tokens", type=int, default=2400, help="Shared system prompt size")
    parser.add_argument("--user-tokens", type=int, default=250, help="Each user turn's size")
    parser.add_argument(
        "--round-robin",
        action="store_true",
        help="Baseline: send each session request straight to the next node's Ollama in turn, "
        "bypassing the router (needs the nodes' Ollama reachable from here)",
    )
    args = parser.parse_args(argv)
    try:
        _, weights = parse_models(args.models or [])
    except ValueError as e:
        parser.error(str(e))
    if args.fleet_url == "none" and not args.models:
        parser.error("--fleet-url none needs --models: there is no fleet to discover them from")
    if args.fleet_url == "none" and args.round_robin:
        parser.error(
            "--round-robin needs the fleet's node list; it can't run with --fleet-url none"
        )
    if args.session_turns and (args.sequence or args.rate or weights):
        parser.error("--session-turns runs per model; it doesn't combine with a sequence or --rate")
    if args.no_think and args.api == "openai":
        parser.error("--no-think is an Ollama API option; it has no effect with --api openai")
    if args.rate is not None and args.rate <= 0:
        parser.error("--rate must be positive")
    if args.sequence == "alternate" and any(w != int(w) for w in weights.values()):
        parser.error("--sequence alternate needs whole-number weights")
    return args


def main():
    asyncio.run(run_benchmark(parse_args()))


if __name__ == "__main__":
    main()
