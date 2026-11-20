"""Summarize fleet benchmark run 4 (Lambda Cloud, 2026-11-20) into one results file.

The raw reports from ``scripts/benchmark.py --output`` keep every request (46 MB for
the whole run); this writes ``fleet_run4_results.json`` with what the published page
and its readers need: per cell or arm, the totals and per-model answered counts and
first-token percentiles, computed with ``fleet_manager.common.stats.percentile``.
Per-request rows are kept for the failure test (B6) and the open-loop mixed-fleet
arms (B4), where the individual requests are the evidence.

    uv run python docs/experiments/fleet-run4-summarize.py <raw results dir>
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from fleet_manager.common.stats import percentile

HERE = Path(__file__).parent

MACHINES = (
    "Lambda Cloud us-south-2: m1-m4 NVIDIA Quadro RTX 6000 24 GB ($0.69/h each), "
    "m5 RTX A6000 48 GB ($1.09/h); Ubuntu 22.04; one private network"
)
OLLAMA = "0.40.16, OLLAMA_NUM_PARALLEL=4 in its systemd unit, nothing else changed"
ROUTER_RELEASED = "ollama-herd 0.12.10 from PyPI"
ROUTER_STRAND = "dc5779c (first placement fix, released in 0.12.11)"
ROUTER_FIXED = "1e7cdb2 (both placement fixes; the code released as 0.12.11)"
WORKLOAD = "about 1,250 prompt and 200 completion tokens per chat request"


def r1(x):
    return None if x is None else round(x, 1)


def per_model(report: dict) -> dict:
    out = {}
    for model in sorted({q["model"] for q in report["requests"]}):
        asked = [q for q in report["requests"] if q["model"] == model]
        ok = [q for q in asked if q["success"]]
        ttft = [q["ttft_ms"] for q in ok]
        out[model] = {
            "asked": len(asked),
            "answered": len(ok),
            "completion_tokens": sum(q["completion_tokens"] for q in ok),
            "ttft_p50_ms": r1(percentile(ttft, 50)),
            "ttft_p95_ms": r1(percentile(ttft, 95)),
            "nodes": dict(Counter(q["node_id"] for q in ok)),
            "failures": dict(
                Counter((q.get("error") or "")[:60] for q in asked if not q["success"])
            ),
        }
    return out


def arm(report: dict, router: str, keep_requests: bool = False) -> dict:
    ok = [q for q in report["requests"] if q["success"]]
    ttft = [q["ttft_ms"] for q in ok]
    events = report.get("model_events") or []
    out = {
        "router": router,
        "sent": len(report["requests"]),
        "answered": len(ok),
        "failed": len(report["requests"]) - len(ok),
        "chat_tokens_per_sec": report["tokens_per_sec"],
        "ttft_p50_ms": r1(percentile(ttft, 50)),
        "ttft_p95_ms": r1(percentile(ttft, 95)),
        "model_loads": sum(e["event"] == "loaded" for e in events),
        "duration_s": report.get("duration_s"),
        "per_model": per_model(report),
        "settings": (report.get("manifest") or {}).get("workload"),
    }
    if keep_requests:
        out["model_events"] = events
        out["requests"] = [
            {
                k: q.get(k)
                for k in (
                    "started_ms",
                    "model",
                    "node_id",
                    "success",
                    "error",
                    "ttft_ms",
                    "latency_ms",
                    "completion_tokens",
                )
            }
            for q in report["requests"]
        ]
    return out


def main(raw: Path) -> None:
    def load(name):
        return json.loads((raw / f"{name}.json").read_text())

    cells: dict[tuple[int, int], dict] = {}
    for f in sorted((raw / "b1").glob("b1r*_n*_c*.json")):
        rep, n, c = map(int, re.search(r"b1r(\d)_n(\d)_c(\d+)", f.name).groups())
        r = json.loads(f.read_text())
        cells.setdefault((n, c), {"machines": n, "concurrency": c, "reps": []})["reps"].append(
            {
                "rep": rep,
                "tokens_per_sec": r["tokens_per_sec"],
                "requests": r["total_requests"],
                "failed": r["total_failures"] + r.get("cancelled_at_drain", 0),
                "ttft_p50_ms": r["ttft_p50_ms"],
                "ttft_p95_ms": r["ttft_p95_ms"],
                "ttft_p99_ms": r["ttft_p99_ms"],
                "per_node": {p["node_id"]: p["requests"] for p in r["per_node_results"]},
            }
        )
    scaling = []
    for key in sorted(cells):
        cell = cells[key]
        cell["reps"].sort(key=lambda x: x["rep"])
        tps = [x["tokens_per_sec"] for x in cell["reps"]]
        p95 = [x["ttft_p95_ms"] for x in cell["reps"]]
        cell.update(
            tokens_per_sec_median=r1(percentile(tps, 50)),
            tokens_per_sec_min=min(tps),
            tokens_per_sec_max=max(tps),
            ttft_p95_ms_median=r1(percentile(p95, 50)),
            ttft_p95_ms_min=min(p95),
            ttft_p95_ms_max=max(p95),
        )
        scaling.append(cell)

    b6 = load("b6_failure")
    out = {
        "date": "2026-11-20",
        "machines": MACHINES,
        "ollama": OLLAMA,
        "workload": WORKLOAD,
        "telemetry": "off on every router and node",
        "harness": "scripts/benchmark.py from OSS main (per-request records, in-band errors and "
        "drained requests counted as failures, rates over the load phase)",
        "scaling": {
            "router": ROUTER_RELEASED,
            "model": "qwen3-coder:30b",
            "seconds_per_cell": 120,
            "repetitions": 3,
            "cells": scaling,
        },
        "failure": {
            **arm(b6, ROUTER_RELEASED, keep_requests=True),
            "what": "4 machines, 8 concurrent qwen3-coder:30b for 240 s; about 60 s in Ollama was "
            "stopped on m3, about 150 s in herd-node was killed with -9 and Ollama stopped on m4",
        },
        "identical_machines": {
            "what": "one model on four identical machines, 8 concurrent, 120 s",
            "herd": arm(load("tie_herd"), ROUTER_RELEASED),
            "haproxy_roundrobin": arm(load("tie_haproxy_rr"), "HAProxy 2.4 roundrobin"),
        },
        "mixed_fleet": {
            "what": "m1, m2 (24 GB) and m5 (48 GB); qwen3-coder:30b, qwen2.5:14b-instruct-q8_0, "
            "phi4:14b-q8_0 and qwen2.5:32b-instruct-q8_0 (35 GB, only m5 has it and can hold it), "
            "model chosen at random per request, open loop for 180 s, client timeout 300 s; start "
            "state m1 coder, m2 qwen2.5:14b, m5 the 32b",
            "rate_0_25": {
                "herd": arm(load("b4q_herd_s7"), ROUTER_FIXED, keep_requests=True),
                "haproxy_leastconn": arm(
                    load("b4q_haproxy_lc_s7"), "HAProxy 2.4 leastconn", keep_requests=True
                ),
                "haproxy_roundrobin": arm(
                    load("b4q_haproxy_rr_s7"), "HAProxy 2.4 roundrobin", keep_requests=True
                ),
            },
            "rate_0_5": {
                "herd_seed7": arm(load("b4o_herd_fix2_s7"), ROUTER_FIXED, keep_requests=True),
                "herd_seed8": arm(load("b4o_herd_fix2_s8"), ROUTER_FIXED, keep_requests=True),
                "herd_0_12_10_seed7": arm(
                    load("b4o_herd_0_12_10"), ROUTER_RELEASED, keep_requests=True
                ),
                "herd_strand_fix_only_seed7": arm(
                    load("b4o_herd_fixed"), ROUTER_STRAND, keep_requests=True
                ),
                "haproxy_leastconn_seed7": arm(
                    load("b4o_haproxy_lc"), "HAProxy 2.4 leastconn", keep_requests=True
                ),
                "haproxy_leastconn_seed8": arm(
                    load("b4o_haproxy_lc_s8"), "HAProxy 2.4 leastconn", keep_requests=True
                ),
                "haproxy_roundrobin_seed7": arm(
                    load("b4o_haproxy_rr"), "HAProxy 2.4 roundrobin", keep_requests=True
                ),
            },
        },
        "one_large_vs_two_small": {
            "what": "one RTX A6000 48 GB ($1.09/h) against two Quadro RTX 6000 24 GB ($1.38/h), "
            "same router; mix = qwen3-coder:30b, qwen3.5:9b and nomic-embed-text, 4 workers each, "
            "180 s",
            "large": {
                k: arm(load(f"b5r_large_{k}"), ROUTER_STRAND)
                for k in ("coder_c1", "coder_c8", "mix")
            },
            "small": {
                k: arm(load(f"b5r_small_{k}"), ROUTER_STRAND)
                for k in ("coder_c1", "coder_c8", "mix")
            },
        },
        "vision": {
            "what": "qwen3-coder:30b, nomic-embed-text and moondream (an image question per "
            "request), 4 workers each, 180 s; one RTX 6000 with all three loaded, against m1, m2 "
            "and m5",
            "one_machine": arm(load("b8r_one"), ROUTER_STRAND),
            "three_machines": arm(load("b8r_three"), ROUTER_STRAND),
        },
    }
    path = HERE / "fleet_run4_results.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
