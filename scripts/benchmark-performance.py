#!/usr/bin/env python3
"""Benchmark Claude-Code-shaped performance: before/after knob changes.

Use this to measure the impact of:
  - Speculative decoding (``FLEET_NODE_MLX_DRAFT_MODEL``)
  - Tool-schema fixup (``FLEET_ANTHROPIC_TOOL_SCHEMA_FIXUP``)
  - Per-tier routing (``FLEET_ANTHROPIC_MODEL_MAP``)
  - Context-management layers (anything in the cc-compat pipeline)

How it works:
    1. Read N real captured Claude Code requests from the debug store.
    2. POST each against the router, measuring wall-clock latency, TTFT
       (from the first SSE byte for streaming), prompt + completion tokens.
    3. Compute tokens/sec (generation-only, not including prefill) and
       overall request latency percentiles.
    4. Report alongside a JSON summary file you can diff against a prior
       run to verify a change actually helped.

Usage:
    # Baseline run (current config) — save results
    python3 scripts/benchmark-performance.py \\
        --data-dir ~/.fleet-manager \\
        --target http://localhost:11435 \\
        --sample 20 \\
        --output /tmp/bench-before.json

    # After flipping a knob + restarting router, compare
    python3 scripts/benchmark-performance.py \\
        --data-dir ~/.fleet-manager \\
        --target http://localhost:11435 \\
        --sample 20 \\
        --output /tmp/bench-after.json \\
        --compare /tmp/bench-before.json

The sample is pulled from the *most recent* successful CC requests in the
debug store — that way you're benchmarking against the same kind of
traffic the fleet actually handles, not synthetic load.  Pass
``--filter-model claude-sonnet-4-6`` to pin to one tier.

Samples are replayed sequentially (not concurrently) to isolate per-
request behavior from queueing effects.  For concurrency/queue testing,
use the separate admission-control tests.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx


# ---------------------------------------------------------------------------
# Capture loading
# ---------------------------------------------------------------------------


def _iter_captures(data_dir: Path):
    """Yield JSON records from all debug capture files, newest first."""
    debug = data_dir / "debug"
    if not debug.exists():
        print(f"No debug directory at {debug} — did you enable "
              f"FLEET_DEBUG_REQUEST_BODIES=true?", file=sys.stderr)
        return
    files = sorted(debug.glob("requests.*.jsonl"), reverse=True)
    for f in files:
        # Read each file in reverse-chunk order.  Good enough — debug files
        # are append-only so later lines are more recent.
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            # Last 100 MB per file is plenty for sampling.
            fh.seek(max(0, size - 100_000_000))
            data = fh.read().decode("utf-8", errors="replace")
        lines = data.split("\n")
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def pick_sample(
    data_dir: Path,
    n: int,
    model_filter: str | None = None,
    min_prompt_tokens: int = 1000,
) -> list[dict]:
    """Pick ``n`` realistic Claude Code requests for replay.

    Filters:
      - Must have ``client_body.messages`` (Anthropic-shape request)
      - Must be a claude-* model (not random OpenAI-compat workloads)
      - ``status != 'rejected'`` and no ``error`` field — only replay
        requests that actually succeeded the first time, so timing
        comparison is apples-to-apples.
      - ``prompt_tokens >= min_prompt_tokens`` so we're not benchmarking
        trivial one-token turns.
    """
    picked = []
    seen_ids = set()
    for rec in _iter_captures(data_dir):
        if len(picked) >= n:
            break
        body = rec.get("client_body") or {}
        if not isinstance(body, dict):
            continue
        model = body.get("model", "")
        if not model.startswith("claude-"):
            continue
        if model_filter and model != model_filter:
            continue
        if rec.get("status") in ("rejected", "failed"):
            continue
        if rec.get("error"):
            continue
        prompt_tok = rec.get("prompt_tokens") or 0
        if prompt_tok < min_prompt_tokens:
            continue
        rid = rec.get("request_id")
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        picked.append({
            "request_id": rid,
            "model": model,
            "body": body,
            "original_prompt_tokens": prompt_tok,
            "original_latency_ms": rec.get("latency_ms"),
            "original_ttft_ms": rec.get("ttft_ms"),
            "original_completion_tokens": rec.get("completion_tokens"),
        })
    return picked


# ---------------------------------------------------------------------------
# Replay with timing
# ---------------------------------------------------------------------------


def replay_once(target: str, body: dict, timeout_s: float = 600.0) -> dict:
    """Re-POST a captured request and measure timing.

    For streaming, measure TTFT via the first SSE ``data:`` line.  For
    non-streaming, latency_ms is end-to-end and ttft_ms is None.
    """
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    stream = bool(body.get("stream"))
    t_start = time.perf_counter()
    out = {
        "stream": stream,
        "status_code": None,
        "latency_ms": None,
        "ttft_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "error": None,
    }
    url = f"{target.rstrip('/')}/v1/messages"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
            if stream:
                first_byte_t: float | None = None
                completion_estimate = 0
                prompt_tok = None
                with client.stream("POST", url, json=body, headers=headers) as resp:
                    out["status_code"] = resp.status_code
                    if resp.status_code >= 400:
                        out["error"] = resp.read().decode(errors="replace")[:300]
                        return out
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        if first_byte_t is None and line.startswith("data:"):
                            first_byte_t = time.perf_counter()
                        # Pick out usage from message_delta events
                        if '"output_tokens"' in line:
                            try:
                                payload = json.loads(line[5:].strip())
                                usage = (payload.get("usage") or {})
                                if usage.get("output_tokens") is not None:
                                    completion_estimate = usage["output_tokens"]
                                if usage.get("input_tokens") is not None:
                                    prompt_tok = usage["input_tokens"]
                            except (ValueError, TypeError):
                                pass
                        if '"input_tokens"' in line:
                            try:
                                payload = json.loads(line[5:].strip())
                                usage = (payload.get("message") or {}).get("usage") or {}
                                if usage.get("input_tokens"):
                                    prompt_tok = usage["input_tokens"]
                            except (ValueError, TypeError):
                                pass
                t_end = time.perf_counter()
                out["latency_ms"] = (t_end - t_start) * 1000
                if first_byte_t is not None:
                    out["ttft_ms"] = (first_byte_t - t_start) * 1000
                out["completion_tokens"] = completion_estimate
                out["prompt_tokens"] = prompt_tok
            else:
                resp = client.post(url, json=body, headers=headers)
                t_end = time.perf_counter()
                out["status_code"] = resp.status_code
                out["latency_ms"] = (t_end - t_start) * 1000
                if resp.status_code < 400:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    out["prompt_tokens"] = usage.get("input_tokens")
                    out["completion_tokens"] = usage.get("output_tokens")
                else:
                    out["error"] = resp.text[:300]
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = min(int(round((p / 100) * (len(s) - 1))), len(s) - 1)
    return s[k]


def summarise(results: list[dict]) -> dict:
    """Build a stats summary from a list of replay_once outputs."""
    ok = [r for r in results if r.get("status_code") and r["status_code"] < 400 and r.get("latency_ms")]
    failed = [r for r in results if r.get("error") or (r.get("status_code") and r["status_code"] >= 400)]
    lats = [r["latency_ms"] for r in ok]
    ttfts = [r["ttft_ms"] for r in ok if r.get("ttft_ms")]
    completions = [r["completion_tokens"] for r in ok if r.get("completion_tokens")]
    prompts = [r["prompt_tokens"] for r in ok if r.get("prompt_tokens")]
    # Tokens/sec, generation-only: completion_tokens / (latency_ms - ttft_ms) * 1000
    gen_rates = []
    for r in ok:
        if r.get("completion_tokens") and r.get("latency_ms") and r.get("ttft_ms"):
            gen_ms = r["latency_ms"] - r["ttft_ms"]
            if gen_ms > 50:  # avoid divide-by-tiny
                gen_rates.append(r["completion_tokens"] / (gen_ms / 1000))
    # Overall tokens/sec (prefill + gen included)
    overall_rates = []
    for r in ok:
        if r.get("completion_tokens") and r.get("latency_ms"):
            overall_rates.append(r["completion_tokens"] / (r["latency_ms"] / 1000))
    return {
        "n_requests": len(results),
        "n_ok": len(ok),
        "n_failed": len(failed),
        "latency_ms": {
            "p50": _percentile(lats, 50),
            "p95": _percentile(lats, 95),
            "mean": statistics.mean(lats) if lats else None,
        },
        "ttft_ms": {
            "p50": _percentile(ttfts, 50),
            "p95": _percentile(ttfts, 95),
        },
        "prompt_tokens": {
            "p50": _percentile(prompts, 50),
            "p95": _percentile(prompts, 95),
        },
        "completion_tokens": {
            "p50": _percentile(completions, 50),
            "mean": statistics.mean(completions) if completions else None,
        },
        "generation_tokens_per_sec": {
            "p50": _percentile(gen_rates, 50),
            "p95": _percentile(gen_rates, 95),
            "mean": statistics.mean(gen_rates) if gen_rates else None,
        },
        "overall_tokens_per_sec": {
            "p50": _percentile(overall_rates, 50),
            "mean": statistics.mean(overall_rates) if overall_rates else None,
        },
    }


def print_summary(label: str, s: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"  Requests: {s['n_ok']}/{s['n_requests']} ok ({s['n_failed']} failed)")
    lat = s["latency_ms"]
    print(f"  latency_ms   p50={lat['p50']:.0f}  p95={lat['p95']:.0f}  mean={lat['mean']:.0f}"
          if lat["p50"] is not None else "  latency_ms   (no data)")
    ttft = s["ttft_ms"]
    if ttft["p50"] is not None:
        print(f"  ttft_ms      p50={ttft['p50']:.0f}  p95={ttft['p95']:.0f}")
    gen = s["generation_tokens_per_sec"]
    if gen["p50"] is not None:
        print(f"  gen_tok/s    p50={gen['p50']:.1f}  p95={gen['p95']:.1f}  mean={gen['mean']:.1f}")
    ov = s["overall_tokens_per_sec"]
    if ov["p50"] is not None:
        print(f"  overall t/s  p50={ov['p50']:.1f}  mean={ov['mean']:.1f}")
    prompts = s["prompt_tokens"]
    if prompts["p50"] is not None:
        print(f"  prompt_tok   p50={prompts['p50']}  p95={prompts['p95']}")


def print_delta(before: dict, after: dict) -> None:
    print("\n=== DELTA (after vs before) ===")
    def _pct(a, b):
        if a is None or b is None or a == 0:
            return "n/a"
        d = (b - a) / a * 100
        return f"{d:+.1f}%"
    for name, key in [
        ("latency_ms.p50", ("latency_ms", "p50")),
        ("latency_ms.p95", ("latency_ms", "p95")),
        ("ttft_ms.p50", ("ttft_ms", "p50")),
        ("gen_tok/s.p50", ("generation_tokens_per_sec", "p50")),
        ("gen_tok/s.mean", ("generation_tokens_per_sec", "mean")),
        ("overall t/s.p50", ("overall_tokens_per_sec", "p50")),
    ]:
        a = before["summary"][key[0]][key[1]]
        b = after["summary"][key[0]][key[1]]
        a_s = f"{a:.1f}" if isinstance(a, float) else str(a)
        b_s = f"{b:.1f}" if isinstance(b, float) else str(b)
        print(f"  {name:<22} {a_s:>8} → {b_s:<8}  {_pct(a, b):>7}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="~/.fleet-manager",
                    help="Router data dir containing debug/requests.*.jsonl")
    ap.add_argument("--target", default="http://localhost:11435",
                    help="Router URL to benchmark against")
    ap.add_argument("--sample", type=int, default=10,
                    help="How many captured requests to replay")
    ap.add_argument("--filter-model", default=None,
                    help="Only replay requests for this model (e.g. "
                         "claude-sonnet-4-6, claude-haiku-4-5)")
    ap.add_argument("--min-prompt-tokens", type=int, default=1000)
    ap.add_argument("--output", default=None,
                    help="Save JSON summary to this path")
    ap.add_argument("--compare", default=None,
                    help="Load a prior --output JSON and print before/after delta")
    ap.add_argument("--timeout-s", type=float, default=600.0)
    ap.add_argument("--label", default="current",
                    help="Run label (e.g. 'draft-off', 'draft-4tokens')")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    sample = pick_sample(
        data_dir, args.sample,
        model_filter=args.filter_model,
        min_prompt_tokens=args.min_prompt_tokens,
    )
    if not sample:
        print("No matching captured requests found.", file=sys.stderr)
        return 1
    print(f"Replaying {len(sample)} requests against {args.target} "
          f"(label={args.label}, filter={args.filter_model or '(any claude-*)'})...")
    results = []
    for i, s in enumerate(sample, 1):
        print(f"  [{i}/{len(sample)}] {s['request_id'][:8]} "
              f"model={s['model']} orig_prompt_tok={s['original_prompt_tokens']}", end=" ")
        sys.stdout.flush()
        r = replay_once(args.target, s["body"], timeout_s=args.timeout_s)
        r["original"] = {
            "prompt_tokens": s["original_prompt_tokens"],
            "latency_ms": s["original_latency_ms"],
            "ttft_ms": s["original_ttft_ms"],
            "completion_tokens": s["original_completion_tokens"],
        }
        results.append(r)
        if r.get("error"):
            print(f"ERROR: {r['error'][:80]}")
        else:
            print(f"{r['latency_ms']:.0f}ms (status={r['status_code']})")

    summary = summarise(results)
    print_summary(args.label, summary)

    out_payload = {
        "label": args.label,
        "target": args.target,
        "filter_model": args.filter_model,
        "sample_size": args.sample,
        "min_prompt_tokens": args.min_prompt_tokens,
        "timestamp": time.time(),
        "summary": summary,
        "results": results,
    }
    if args.output:
        Path(args.output).expanduser().write_text(
            json.dumps(out_payload, indent=2, default=str),
        )
        print(f"\nSaved to {args.output}")

    if args.compare:
        try:
            before = json.loads(Path(args.compare).expanduser().read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"Could not load --compare file: {exc}", file=sys.stderr)
            return 2
        print_summary(f"before ({before.get('label','unknown')})", before["summary"])
        print_summary(f"after ({args.label})", summary)
        print_delta(before, out_payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())
