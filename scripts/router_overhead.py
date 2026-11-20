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


async def one(client: httpx.AsyncClient, url: str, model: str, max_tokens: int) -> float:
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
    return elapsed


def summary(values: list[float]) -> dict:
    return {
        "median_ms": round(statistics.median(values), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "min_ms": round(min(values), 1),
        "max_ms": round(max(values), 1),
    }


async def main(args: argparse.Namespace) -> int:
    direct: list[float] = []
    routed: list[float] = []
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
                out.append(await one(client, url, args.model, args.max_tokens))
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
        "direct_raw_ms": [round(x, 1) for x in direct],
        "router_raw_ms": [round(x, 1) for x in routed],
    }
    print(f"  {'':8} {'median':>9} {'p95':>9}")
    print(f"  {'direct':8} {d['median_ms']:>7.1f}ms {d['p95_ms']:>7.1f}ms")
    print(f"  {'router':8} {r['median_ms']:>7.1f}ms {r['p95_ms']:>7.1f}ms")
    print(
        f"  {'added':8} {report['overhead_median_ms']:>7.1f}ms {report['overhead_p95_ms']:>7.1f}ms"
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
    sys.exit(asyncio.run(main(p.parse_args())))
