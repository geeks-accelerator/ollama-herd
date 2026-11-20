#!/usr/bin/env python3
"""Router ceiling: open-loop load from several processes, raw latencies merged.

``scripts/router_overhead.py --rate`` runs in one Python process, which spends about 65 % of a
core at 200 requests/s and carries a backlog from one rate into the next.  This splits each rate
across fresh processes (``--per-proc`` requests/s each), sends it direct to the upstream and then
through the router, and merges the raw latencies before taking percentiles.  ``lag`` is how far
the generator fell behind its own schedule (the latest request's start, over every process): above
a few milliseconds, the client was the limit, not the router.  The schedule is the benchmark
engine's ``open_loop`` and the percentile ``fleet_manager.common.stats.percentile``, so it runs
where herd is installed (``uv run`` in a checkout).

    uv run python docs/experiments/router-ceiling-loadgen.py --direct http://<node>:11500 \
        --router http://<router>:11435 --model mock:latest --rate 100 150 200 --out ceiling.json

Run against ``scripts/mock_ollama.py --workers 8`` and a node started with its own
``OLLAMA_NUM_PARALLEL`` well above rate x delay, or herd's per-model queue cap is what you measure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import multiprocessing as mp
import time

import httpx

from fleet_manager.common.stats import percentile
from fleet_manager.server.benchmark_engine import open_loop


def body(model: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the word ready."}],
        "stream": False,
        "options": {"temperature": 0, "seed": 1, "num_predict": 8},
    }


async def run(url: str, model: str, rate: float, seconds: float):
    """One process: open loop at ``rate`` for ``seconds``; latencies, errors, header, lag."""
    lat: list[float] = []
    rep: list[float] = []
    errs = 0
    limits = httpx.Limits(max_connections=1000, max_keepalive_connections=200)
    async with httpx.AsyncClient(timeout=30, limits=limits) as client:

        async def one() -> None:
            nonlocal errs
            start = time.perf_counter()
            try:
                resp = await client.post(url + "/api/chat", json=body(model))
                resp.raise_for_status()
                lat.append((time.perf_counter() - start) * 1000)
                header = resp.headers.get("x-fleet-overhead-ms")
                if header:
                    rep.append(float(header))
            except Exception:  # noqa: BLE001 -- counted, the run goes on
                errs += 1

        await one()  # open the connection; not counted
        lat.clear()
        rep.clear()
        errs = 0
        lags = await open_loop(lambda _i, _lag: one(), rate, int(rate * seconds))
    return lat, errs, rep, max(lags, default=0.0)  # > 0: the generator fell behind


def worker(args: tuple) -> tuple:
    return asyncio.run(run(*args))


def measure(url: str, model: str, rate: float, seconds: float, per_proc: float) -> dict:
    procs = max(1, math.ceil(rate / per_proc))
    with mp.Pool(procs) as pool:
        results = pool.map(worker, [(url, model, rate / procs, seconds)] * procs)
    lat = [x for r in results for x in r[0]]
    rep = [x for r in results for x in r[2]]
    out = {
        "procs": procs,
        "n": len(lat),
        "errors": sum(r[1] for r in results),
        "max_schedule_lag_s": round(max(r[3] for r in results), 3),
    }
    if lat:
        out.update(
            p50_ms=round(percentile(lat, 50), 1),
            p95_ms=round(percentile(lat, 95), 1),
            p99_ms=round(percentile(lat, 99), 1),
            max_ms=round(max(lat), 1),
        )
    if rep:
        out.update(
            reported_p50_ms=round(percentile(rep, 50), 1),
            reported_p99_ms=round(percentile(rep, 99), 1),
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Router ceiling, open loop from several processes")
    p.add_argument("--direct", required=True, help="The upstream, e.g. http://10.0.0.2:11500")
    p.add_argument("--router", required=True, help="The router, e.g. http://10.0.0.1:11435")
    p.add_argument("--model", required=True)
    p.add_argument("--rate", type=float, nargs="+", required=True, help="Requests/s to try")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--per-proc", type=float, default=100, help="Requests/s per client process")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    rows = []
    cols = ("rate", "path", "p50", "p95", "p99", "err", "lag", "hdr")
    print("  " + " ".join(f"{c:>8}" for c in cols))
    for rate in args.rate:
        row: dict = {"rate": rate}
        for name, url in (("direct", args.direct), ("router", args.router)):
            r = measure(url, args.model, rate, args.seconds, args.per_proc)
            row[name] = r
            print(
                f"  {rate:>6g} {name:7} {r.get('p50_ms', 0):>6.1f}ms {r.get('p95_ms', 0):>6.1f}ms "
                f"{r.get('p99_ms', 0):>6.1f}ms {r['errors']:>5} {r['max_schedule_lag_s']:>5.2f}s "
                f"{r.get('reported_p50_ms', ''):>6}",
                flush=True,
            )
            time.sleep(3)
        if "p50_ms" in row["direct"] and "p50_ms" in row["router"]:
            row["added_p50_ms"] = round(row["router"]["p50_ms"] - row["direct"]["p50_ms"], 1)
            row["added_p99_ms"] = round(row["router"]["p99_ms"] - row["direct"]["p99_ms"], 1)
        rows.append(row)
        report = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": args.model,
            "seconds_per_rate": args.seconds,
            "rates": rows,
        }
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
