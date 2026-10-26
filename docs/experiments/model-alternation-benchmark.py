#!/usr/bin/env python3
"""Two models, strictly alternating: what a model swap costs on each setup.

Sends N sequential streaming chat requests that alternate between model A and
model B (A, B, A, B, ...) to one OpenAI-compatible endpoint, and records
time-to-first-token and total time per request. When A and B cannot both stay
loaded, every request after the first pays a cold load; when they can (two
machines behind a router, or enough memory), only the first request per model
does. The arms of the experiment differ only in what --url points at:

  1. one machine, plain Ollama                 http://localhost:11434/v1/chat/completions
  2. one machine, llama-swap + llama-server    http://localhost:8080/v1/chat/completions
  3. one machine, llama.cpp router mode        http://localhost:8080/v1/chat/completions
  4. two machines behind Ollama Herd           http://<router>:11435/v1/chat/completions
  5. two machines, llama-swap with a peer      http://<llama-swap>:8080/v1/chat/completions

Method and the published results: model-alternation-benchmark.md.

Usage:
    python3 docs/experiments/model-alternation-benchmark.py \\
        --url http://localhost:11434/v1/chat/completions \\
        --models deepseek-r1:70b qwen2.5:72b --requests 20 \\
        --label "arm 1: Ollama 0.40.3, one Mac" --out results-arm1.json

Standard library only, so it runs unchanged on any machine in the test.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

PROMPT = "List three facts about the moon, one line each."


def stream_chat(url: str, model: str, max_tokens: int, timeout: float) -> dict:
    """POST one streaming chat request; time the first content byte and the end."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    start = time.perf_counter()
    ttft = None
    chunks = 0
    usage = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        node = resp.headers.get("X-Fleet-Node")  # set when the endpoint is Ollama Herd
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            event = json.loads(line[5:])
            usage = event.get("usage") or usage
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                    chunks += 1
                    if ttft is None:
                        ttft = time.perf_counter() - start
    total = time.perf_counter() - start
    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft is not None else None,
        "total_ms": round(total * 1000, 1),
        "completion_tokens": (usage or {}).get("completion_tokens", chunks),
        "node": node,
    }


def summarize(rows: list[dict]) -> dict:
    out = {}
    for model in dict.fromkeys(r["model"] for r in rows):
        ttfts = [r["ttft_ms"] for r in rows if r["model"] == model and r.get("ttft_ms") is not None]
        if not ttfts:
            continue
        ttfts_after_first = ttfts[1:] or ttfts
        out[model] = {
            "requests": len(ttfts),
            "first_ttft_ms": ttfts[0],
            "median_ttft_ms_after_first": round(statistics.median(ttfts_after_first), 1),
            "max_ttft_ms_after_first": max(ttfts_after_first),
        }
    ok = [r for r in rows if r.get("ttft_ms") is not None]
    out["_all"] = {
        "requests": len(rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "wall_s": round(sum(r["total_ms"] for r in ok) / 1000, 1),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="OpenAI-compatible /v1/chat/completions URL")
    ap.add_argument("--models", nargs=2, required=True, metavar=("A", "B"))
    ap.add_argument("--requests", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--timeout", type=float, default=900.0, help="per request, seconds (cold loads are slow)")
    ap.add_argument("--label", required=True, help="the arm, versions and hardware, recorded in the output")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    rows = []
    for i in range(args.requests):
        model = args.models[i % 2]
        row = {"i": i, "model": model}
        try:
            row.update(stream_chat(args.url, model, args.max_tokens, args.timeout))
        except Exception as e:  # recorded, not fatal: a refused load is a result too
            row.update(error=str(e)[:300], total_ms=0)
        rows.append(row)
        print(f"{i:>3} {model:<28} ttft {row.get('ttft_ms')} ms  total {row.get('total_ms')} ms"
              f"{'  node ' + row['node'] if row.get('node') else ''}{'  ERROR ' + row['error'] if row.get('error') else ''}",
              flush=True)

    result = {
        "label": args.label, "url": args.url, "models": args.models, "prompt": PROMPT,
        "max_tokens": args.max_tokens, "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": summarize(rows), "requests": rows,
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))
    print(f"saved to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
