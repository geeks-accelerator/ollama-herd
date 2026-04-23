#!/usr/bin/env python3
"""Phase 0 of the Context Hygiene Compactor: measure the opportunity.

Scans captured Claude Code debug records and answers:
  - What fraction of turns exceed the proposed compaction budget?
  - In over-budget turns, which content types dominate the bloat?
  - What compaction ratio would we realistically achieve?
  - How much duplicate content (same-file-read-twice, etc.) is there?

No compaction is performed — this is pure analysis.  Run this BEFORE
building the ContextCompactor to make sure the design targets the
actual bloat, not imagined bloat.

Usage:
    uv run python docs/experiments/context-bloat-analysis.py
    uv run python docs/experiments/context-bloat-analysis.py --budget 15000
    uv run python docs/experiments/context-bloat-analysis.py --window 24h
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

# ----------------------------------------------------------------------------
# Token estimation — faithful-enough for bucketing.  Not token-perfect; we
# just need to identify which content types dominate bloat.
# ----------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough: 4 chars per token.  Good enough for bucketing analysis."""
    return max(1, len(text) // 4)


def content_tokens(content) -> int:
    """Count tokens in Anthropic content (string or block array)."""
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                total += estimate_tokens(block.get("text") or "")
            elif block.get("type") == "tool_use":
                total += estimate_tokens(json.dumps(block.get("input") or {}))
                total += estimate_tokens(block.get("name") or "")
            elif block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    total += estimate_tokens(c)
                elif isinstance(c, list):
                    for sub in c:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            total += estimate_tokens(sub.get("text") or "")
        return total
    return 0


def load_records(window_seconds: int) -> list[dict]:
    """Load captured Anthropic-format MLX records within the time window."""
    cutoff = time.time() - window_seconds
    out = []
    for p in sorted(Path.home().joinpath(".fleet-manager/debug").glob("requests.*.jsonl")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                r.get("timestamp", 0) >= cutoff
                and r.get("original_format") == "anthropic"
            ):
                out.append(r)
    return out


# ----------------------------------------------------------------------------
# Categorization of content blocks
# ----------------------------------------------------------------------------


def classify_tool_result(r: dict, block: dict) -> str:
    """Classify a tool_result by the preceding tool_use's tool name.

    Walks the messages to find the assistant tool_use with matching id.
    If not found, classify as 'unknown'.
    """
    tool_use_id = block.get("tool_use_id")
    if not tool_use_id:
        return "tool_result_unknown"
    for m in (r.get("client_body") or {}).get("messages") or []:
        if m.get("role") != "assistant":
            continue
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("id") == tool_use_id
            ):
                return f"tool_result_{b.get('name', 'unknown')}"
    return "tool_result_unknown"


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------


def analyze(recs: list[dict], budget: int) -> None:
    """Core analysis — bucket, attribute, report.

    Four questions:
      1. How many requests exceed budget?
      2. In those, what content types dominate bloat?
      3. What compaction ratio could we expect?
      4. How much duplicate content is there (= cache win potential)?
    """
    # --- Q1: over-budget distribution --------------------------------------
    tokens_per_req = [
        (r, content_tokens((r.get("client_body") or {}).get("system") or ""))
        for r in recs
    ]
    totals = []
    for r, sys_tok in tokens_per_req:
        t = sys_tok
        for m in (r.get("client_body") or {}).get("messages") or []:
            t += content_tokens(m.get("content"))
        # Tools dominate the prefix; include them
        for tool in (r.get("client_body") or {}).get("tools") or []:
            t += content_tokens(json.dumps(tool))
        totals.append((r, t))
    totals.sort(key=lambda x: x[1])

    print(f"=== Q1: how many turns exceed the budget? ===")
    print(f"  budget: {budget:,} tokens")
    print(f"  total records analyzed: {len(totals)}")
    buckets = {
        "<5K": 0, "5-10K": 0, "10-20K": 0, "20-30K": 0,
        "30-50K": 0, "50-100K": 0, "100K+": 0,
    }
    for _r, t in totals:
        if t < 5000: buckets["<5K"] += 1
        elif t < 10000: buckets["5-10K"] += 1
        elif t < 20000: buckets["10-20K"] += 1
        elif t < 30000: buckets["20-30K"] += 1
        elif t < 50000: buckets["30-50K"] += 1
        elif t < 100000: buckets["50-100K"] += 1
        else: buckets["100K+"] += 1
    for b, n in buckets.items():
        bar = "█" * min(40, n)
        pct = 100 * n / max(1, len(totals))
        print(f"    {b:<10} {n:>4} ({pct:>5.1f}%) {bar}")
    over = sum(1 for _, t in totals if t > budget)
    print(f"  over budget ({budget:,}+ tokens): {over} / {len(totals)} ({100 * over / max(1, len(totals)):.1f}%)")
    if totals:
        med = totals[len(totals) // 2][1]
        p95_idx = min(len(totals) - 1, int(len(totals) * 0.95))
        p95 = totals[p95_idx][1]
        print(f"  median prompt: {med:,} tokens")
        print(f"  p95 prompt:    {p95:,} tokens")
        print(f"  max prompt:    {max(t for _, t in totals):,} tokens")
    print()

    # --- Q2: what content types dominate bloat in over-budget turns? ---------
    print(f"=== Q2: bloat breakdown in over-budget turns ===")
    over_budget = [(r, t) for r, t in totals if t > budget]
    if not over_budget:
        print("  (no over-budget turns in this window)")
        print()
        return

    type_tokens: Counter = Counter()  # type_key -> total tokens across all over-budget
    type_instances: Counter = Counter()
    for r, _total in over_budget:
        cb = r.get("client_body") or {}
        # System
        sys = cb.get("system") or ""
        if sys:
            tok = content_tokens(sys if isinstance(sys, str) else json.dumps(sys))
            type_tokens["system_prompt"] += tok
            type_instances["system_prompt"] += 1
        # Tools
        for tool in cb.get("tools") or []:
            tok = content_tokens(json.dumps(tool))
            type_tokens["tool_definition"] += tok
            type_instances["tool_definition"] += 1
        # Messages
        for m in cb.get("messages") or []:
            c = m.get("content")
            role = m.get("role", "?")
            if isinstance(c, str):
                tok = content_tokens(c)
                type_tokens[f"{role}_text"] += tok
                type_instances[f"{role}_text"] += 1
            elif isinstance(c, list):
                for block in c:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        tok = content_tokens(block.get("text") or "")
                        type_tokens[f"{role}_text"] += tok
                        type_instances[f"{role}_text"] += 1
                    elif btype == "tool_use":
                        tok = content_tokens(
                            json.dumps(block.get("input") or {})
                        ) + content_tokens(block.get("name") or "")
                        type_tokens["tool_use"] += tok
                        type_instances["tool_use"] += 1
                    elif btype == "tool_result":
                        inner = block.get("content")
                        if isinstance(inner, str):
                            tok = content_tokens(inner)
                        elif isinstance(inner, list):
                            tok = sum(
                                content_tokens(sub.get("text") or "")
                                for sub in inner
                                if isinstance(sub, dict) and sub.get("type") == "text"
                            )
                        else:
                            tok = 0
                        cls = classify_tool_result(r, block)
                        type_tokens[cls] += tok
                        type_instances[cls] += 1

    total_tok = sum(type_tokens.values())
    print(f"  {'content type':<35} {'tokens':>12} {'avg':>8} {'%total':>7}")
    print("  " + "-" * 65)
    for cls, toks in type_tokens.most_common(15):
        avg = toks / max(1, type_instances[cls])
        pct = 100 * toks / max(1, total_tok)
        print(f"  {cls:<35} {toks:>12,} {avg:>8,.0f} {pct:>6.1f}%")
    print()

    # --- Q3: realistic compaction ratio --------------------------------------
    print(f"=== Q3: realistic compaction opportunity ===")
    # Assumption: we can compress each compactable type to these ratios.
    # Based on curator-model behavior on similar content empirically:
    compaction_ratios = {
        "tool_result_Read": 0.08,      # file contents → symbol summary ~8%
        "tool_result_Bash": 0.15,      # shell output → first+last N lines ~15%
        "tool_result_WebFetch": 0.10,  # HTML→md dump → abstract ~10%
        "tool_result_Grep": 1.00,      # already terse, don't compact
        "tool_result_Glob": 1.00,
        "tool_result_Edit": 1.00,
        "tool_result_Write": 1.00,
        "tool_result_TodoWrite": 1.00,
        "tool_result_unknown": 0.30,   # conservative default
    }
    compactable_before = 0
    compactable_after = 0
    for cls, toks in type_tokens.items():
        if cls.startswith("tool_result_"):
            ratio = compaction_ratios.get(cls, 0.30)
            compactable_before += toks
            compactable_after += toks * ratio

    if compactable_before > 0:
        reduction = compactable_before - compactable_after
        print(f"  compactable tokens (tool_results): {compactable_before:,}")
        print(f"  after compaction:                  {int(compactable_after):,}")
        print(f"  reduction:                         {int(reduction):,} ({100 * reduction / compactable_before:.1f}%)")
        print()
        per_req = reduction / max(1, len(over_budget))
        print(f"  per over-budget request: ~{int(per_req):,} tokens saved")
        print(f"  at ~2 ms/token prompt-processing: ~{per_req * 2 / 1000:.1f}s faster")
    else:
        print("  (no compactable tool_results in over-budget turns)")
    print()

    # --- Q4: duplicate content detection (cache-in-cache win) ---------------
    print(f"=== Q4: duplicate content — the summary-cache opportunity ===")
    content_hashes: dict[str, list[dict]] = defaultdict(list)
    for r, _total in over_budget:
        cb = r.get("client_body") or {}
        for m in cb.get("messages") or []:
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for block in c:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                inner = block.get("content")
                text = inner if isinstance(inner, str) else json.dumps(inner)
                if len(text) < 500:  # skip tiny results, they're not worth caching
                    continue
                h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
                content_hashes[h].append({
                    "tokens": estimate_tokens(text),
                    "type": classify_tool_result(r, block),
                    "request_id": r.get("request_id", "?")[:8],
                })

    total_blocks = sum(len(v) for v in content_hashes.values())
    unique_blocks = len(content_hashes)
    dupe_blocks = total_blocks - unique_blocks
    dupe_tokens_saved = sum(
        (len(v) - 1) * v[0]["tokens"] for v in content_hashes.values() if len(v) > 1
    )
    dup_ratio = (dupe_blocks / max(1, total_blocks)) * 100
    print(f"  total tool_result blocks (>500 chars): {total_blocks}")
    print(f"  unique content hashes: {unique_blocks}")
    print(f"  duplicate occurrences: {dupe_blocks} ({dup_ratio:.1f}% of blocks repeat)")
    print(f"  tokens saved by summary cache (dedup alone): {dupe_tokens_saved:,}")
    # Top repeat offenders
    repeats = [(h, v) for h, v in content_hashes.items() if len(v) >= 2]
    repeats.sort(key=lambda x: len(x[1]), reverse=True)
    if repeats:
        print(f"  top repeated tool_results:")
        for h, occurrences in repeats[:5]:
            n = len(occurrences)
            tok = occurrences[0]["tokens"]
            t = occurrences[0]["type"]
            print(f"    {h[:8]}... {n}× {tok:,} tokens [{t}]")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=int, default=20000,
                    help="Tokens above which we'd trigger compaction (default 20000)")
    ap.add_argument("--window", default="7d",
                    help="Time window: 30m, 2h, 7d, 30d (default 7d)")
    args = ap.parse_args()

    unit_map = {"m": 60, "h": 3600, "d": 86400}
    if args.window[-1] in unit_map:
        seconds = int(args.window[:-1]) * unit_map[args.window[-1]]
    else:
        seconds = int(args.window)

    recs = load_records(seconds)
    print(f"Loaded {len(recs)} Anthropic-format records from last {args.window}")
    print()

    if not recs:
        print("(no data)")
        return 0

    analyze(recs, args.budget)

    print("=" * 65)
    print("TAKEAWAY SUMMARY")
    print("=" * 65)
    print("Next step: if over-budget rate is meaningful (say >20%) and")
    print("tool_result blocks dominate the bloat (expected), the compactor")
    print("plan's assumptions hold.  Proceed to Phase 1 (build ContextCompactor).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
