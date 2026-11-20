#!/usr/bin/env python3
"""Measure the latency the router adds over talking to Ollama directly.

Sends the same short, fully deterministic chat request (temperature 0, a fixed
seed, a fixed reply cap) ``--requests`` times to a node's Ollama and the same
number of times through the router, alternating the two so that drift on the
machine (thermals, other load) lands on both equally.  Reports the median and
p95 wall time of each and the difference.

Run it when the router has a single node online with the model loaded, or the
router may send its half to a different machine than the direct half.

    python scripts/router_overhead.py --ollama http://10.0.0.2:11434 \\
        --router http://10.0.0.1:11435 --model qwen3-coder:30b --output overhead.json

``--rate`` measures the way gateway vendors publish it instead: open-loop
(requests start on schedule whether or not earlier ones finished, so a slow
gateway can't hide its queueing), at each fixed arrival rate, for ``--seconds``,
direct then routed, against an upstream of known latency
(``scripts/mock_ollama.py``).  Reports p50/p95/p99 and errors per rate.

    python scripts/router_overhead.py --ollama http://10.0.0.2:11500 \\
        --router http://10.0.0.1:11435 --model mock:latest --rate 10 50 100 --seconds 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import UTC, datetime

import httpx

PROMPT = "Reply with the word ready."


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def one(
    client: httpx.AsyncClient, url: str, model: str, max_tokens: int
) -> tuple[float, float | None]:
    """Wall time in ms, and the router's own X-Fleet-Overhead-Ms when present."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "options": {"temperature": 0, "seed": 1, "num_predict": max_tokens},
    }
    start = time.perf_counter()
    resp = await client.post(f"{url.rstrip('/')}/api/chat", json=body)
    elapsed = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    reported = resp.headers.get("x-fleet-overhead-ms")
    return elapsed, float(reported) if reported else None


def summary(values: list[float]) -> dict:
    return {
        "median_ms": round(statistics.median(values), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "p99_ms": round(percentile(values, 99), 1),
        "min_ms": round(min(values), 1),
        "max_ms": round(max(values), 1),
        "n": len(values),
    }


async def fixed_rate(
    client: httpx.AsyncClient, url: str, model: str, max_tokens: int, rate: float, seconds: float
) -> dict:
    """Open loop at ``rate`` requests/s for ``seconds``: latencies and errors."""
    latencies: list[float] = []
    errors = 0

    async def timed() -> None:
        nonlocal errors
        try:
            elapsed, _ = await one(client, url, model, max_tokens)
            latencies.append(elapsed)
        except Exception:  # noqa: BLE001 -- counted, the run goes on
            errors += 1

    tasks = []
    start = time.perf_counter()
    for i in range(int(rate * seconds)):
        delay = start + i / rate - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(timed()))
    await asyncio.gather(*tasks)
    out = summary(latencies) if latencies else {"n": 0}
    out["errors"] = errors
    return out


async def rate_mode(args: argparse.Namespace) -> int:
    limits = httpx.Limits(max_connections=2000, max_keepalive_connections=500)
    results = []
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        for url in (args.ollama, args.router):
            await one(client, url, args.model, args.max_tokens)  # warm both paths
        print(f"  {'rate':>6} {'path':7} {'p50':>8} {'p95':>8} {'p99':>8} {'errors':>7}")
        for rate in args.rate:
            row = {"rate": rate}
            for name, url in (("direct", args.ollama), ("router", args.router)):
                r = await fixed_rate(client, url, args.model, args.max_tokens, rate, args.seconds)
                row[name] = r
                if r["n"]:
                    print(
                        f"  {rate:>6g} {name:7} {r['median_ms']:>6.1f}ms {r['p95_ms']:>6.1f}ms "
                        f"{r['p99_ms']:>6.1f}ms {r['errors']:>7}"
                    )
                else:
                    print(f"  {rate:>6g} {name:7} all {r['errors']} failed")
            if row["direct"]["n"] and row["router"]["n"]:
                row["added_p50_ms"] = round(
                    row["router"]["median_ms"] - row["direct"]["median_ms"], 1
                )
                row["added_p99_ms"] = round(row["router"]["p99_ms"] - row["direct"]["p99_ms"], 1)
            results.append(row)
    if args.output:
        report = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": args.model,
            "seconds_per_rate": args.seconds,
            "rates": results,
        }
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  saved to {args.output}")
    return 0


async def main(args: argparse.Namespace) -> int:
    if args.rate:
        return await rate_mode(args)
    direct: list[float] = []
    routed: list[float] = []
    reported: list[float] = []  # the router's own figure, per routed request
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        # Warm both paths: the model load and the router's first-request work
        # are not overhead.
        for url in (args.ollama, args.router):
            await one(client, url, args.model, args.max_tokens)
        for i in range(args.requests):
            pair = [(args.ollama, direct), (args.router, routed)]
            if i % 2:
                pair.reverse()
            for url, out in pair:
                elapsed, overhead = await one(client, url, args.model, args.max_tokens)
                out.append(elapsed)
                if out is routed and overhead is not None:
                    reported.append(overhead)
            print(f"\r  {i + 1}/{args.requests}", end="", flush=True)
    print()
    d, r = summary(direct), summary(routed)
    report = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": args.model,
        "requests_each": args.requests,
        "max_tokens": args.max_tokens,
        "direct": d,
        "router": r,
        "overhead_median_ms": round(r["median_ms"] - d["median_ms"], 1),
        "overhead_p95_ms": round(r["p95_ms"] - d["p95_ms"], 1),
        # X-Fleet-Overhead-Ms: herd's time minus Ollama's own total_duration,
        # from inside the router (includes the hop to the node).
        "router_reported": summary(reported) if reported else None,
        "direct_raw_ms": [round(x, 1) for x in direct],
        "router_raw_ms": [round(x, 1) for x in routed],
    }
    print(f"  {'':8} {'median':>9} {'p95':>9}")
    print(f"  {'direct':8} {d['median_ms']:>7.1f}ms {d['p95_ms']:>7.1f}ms")
    print(f"  {'router':8} {r['median_ms']:>7.1f}ms {r['p95_ms']:>7.1f}ms")
    print(
        f"  {'added':8} {report['overhead_median_ms']:>7.1f}ms {report['overhead_p95_ms']:>7.1f}ms"
    )
    if reported:
        rr = report["router_reported"]
        print(
            f"  {'reported':8} {rr['median_ms']:>7.1f}ms {rr['p95_ms']:>7.1f}ms"
            "  (X-Fleet-Overhead-Ms)"
        )
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  saved to {args.output}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ollama", required=True, help="A node's Ollama, e.g. http://10.0.0.2:11434")
    p.add_argument("--router", required=True, help="The router, e.g. http://10.0.0.1:11435")
    p.add_argument("--model", required=True)
    p.add_argument("--requests", type=int, default=20, help="Requests per path (default 20)")
    p.add_argument("--max-tokens", type=int, default=8, help="Reply cap (default 8)")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--output", help="Write the results as JSON")
    p.add_argument(
        "--rate", type=float, nargs="+", help="Open-loop arrival rates (req/s) instead of pairs"
    )
    p.add_argument("--seconds", type=float, default=30.0, help="Seconds per rate (default 30)")
    sys.exit(asyncio.run(main(p.parse_args())))
