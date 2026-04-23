#!/usr/bin/env python3
"""Diff sequential turns of the same Claude Code conversation byte-for-byte.

Phase 3 of docs/plans/mlx-prompt-cache-optimization.md — find what STILL
changes between turns of the same conversation after the cch= normalization
fix.  Anything that flips between sequential turns of the same conversation
is a cache-busting token; we want to find them all.

Usage:
    python scripts/diff-sequential-turns.py
    python scripts/diff-sequential-turns.py --window 1h
    python scripts/diff-sequential-turns.py --pair-id <request_id>
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

# Import the same translator the proxy uses, so we diff what mlx_lm.server
# actually sees, not the raw client body.
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from fleet_manager.server.anthropic_models import AnthropicMessagesRequest  # noqa: E402
from fleet_manager.server.anthropic_translator import (  # noqa: E402
    anthropic_system_to_text,
    anthropic_to_ollama_messages,
)


def load_records(window_seconds: int) -> list[dict]:
    """Load captured Anthropic-format MLX records within the window."""
    cutoff = time.time() - window_seconds
    out = []
    debug_dir = Path.home() / ".fleet-manager" / "debug"
    for p in sorted(debug_dir.glob("requests.*.jsonl")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                r.get("timestamp", 0) >= cutoff
                and r.get("backend") == "mlx"
                and r.get("original_format") == "anthropic"
            ):
                out.append(r)
    return out


def session_key(r: dict) -> tuple:
    """Group requests likely from the same Claude Code conversation.

    Heuristic: same first-user-message content + same tool count.  A new
    conversation starts with a fresh first message, so this groups
    sequential turns of the same chat.
    """
    cb = r.get("client_body") or {}
    msgs = cb.get("messages", [])
    n_tools = len(cb.get("tools") or [])
    first_user = ""
    for m in msgs:
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                first_user = c[:200]
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        first_user = b.get("text", "")[:200]
                        break
            break
    return (n_tools, first_user)


def build_translated_sections(rec: dict) -> dict[str, str]:
    """Reconstruct what mlx_lm.server saw, by section.

    Returns a dict with keys 'system', 'tools', 'messages_prefix' — diff
    them independently so the section-count headers don't pollute results.
    """
    cb = rec.get("client_body") or {}
    try:
        ant = AnthropicMessagesRequest(**cb)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}

    sys_text = anthropic_system_to_text(ant.system)
    tools = [t.model_dump() for t in (ant.tools or [])]
    tools_dump = json.dumps(tools, indent=2, sort_keys=True)

    sys_for_translator = anthropic_system_to_text(ant.system)
    ollama_msgs = anthropic_to_ollama_messages(
        [m.model_dump() for m in ant.messages],
        system=sys_for_translator,
    )
    # mlx's cache keys on PREFIX — the first N stable messages of the
    # conversation.  Diff just the first 3 messages; the new tail (which
    # contains the latest user turn) is EXPECTED to differ.
    prefix_msgs = ollama_msgs[:3]
    msgs_dump = json.dumps(prefix_msgs, indent=2, sort_keys=True)

    return {
        "system": sys_text,
        "tools": tools_dump,
        "messages_prefix": msgs_dump,
    }


def find_first_diff(a: str, b: str) -> tuple[int, str, str] | None:
    """Return (offset, a_window, b_window) of first byte difference, or None."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            window = 60
            return (
                i,
                a[max(0, i - window) : i + window],
                b[max(0, i - window) : i + window],
            )
    if len(a) != len(b):
        i = min(len(a), len(b))
        return (
            i,
            a[max(0, i - 60) :] if len(a) > len(b) else "<EOF>",
            b[max(0, i - 60) :] if len(b) > len(a) else "<EOF>",
        )
    return None


def report_pair(r1: dict, r2: dict) -> None:
    """Diff two sequential captures from the same session, section-by-section."""
    t1 = time.strftime("%H:%M:%S", time.localtime(r1["timestamp"]))
    t2 = time.strftime("%H:%M:%S", time.localtime(r2["timestamp"]))
    n1 = len((r1.get("client_body") or {}).get("messages", []))
    n2 = len((r2.get("client_body") or {}).get("messages", []))
    print(f"\n{'─' * 70}")
    print(f"PAIR: {t1} (msgs={n1}) → {t2} (msgs={n2})")
    print(f"  request_ids: {r1.get('request_id', '?')[:12]} → {r2.get('request_id', '?')[:12]}")

    s1 = build_translated_sections(r1)
    s2 = build_translated_sections(r2)
    if "_error" in s1 or "_error" in s2:
        print(f"  ⚠️  unparseable: {s1.get('_error') or s2.get('_error')}")
        return

    # Cache hit rate hints (if captured)
    pt1, ct1 = r1.get("prompt_tokens"), r1.get("cached_tokens")
    pt2, ct2 = r2.get("prompt_tokens"), r2.get("cached_tokens")
    if pt1 and ct1 is not None:
        print(f"  r1 cache: {ct1}/{pt1} ({100*ct1/pt1:.0f}% hit)")
    if pt2 and ct2 is not None:
        print(f"  r2 cache: {ct2}/{pt2} ({100*ct2/pt2:.0f}% hit)")

    # Diff each section independently
    for section in ("system", "tools", "messages_prefix"):
        a = s1[section]
        b = s2[section]
        if a == b:
            print(f"  ✅ {section}: IDENTICAL ({len(a)} chars)")
            continue
        diff = find_first_diff(a, b)
        if diff is None:
            print(f"  ⚠️  {section}: differ in length only")
            continue
        offset, a_win, b_win = diff
        print(f"  ⚠️  {section}: FIRST DIFF at offset {offset} of {len(a)}")
        print(f"     r1: ...{a_win!r}...")
        print(f"     r2: ...{b_win!r}...")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", default="2h", help="Time window: 30m, 2h, 7d")
    ap.add_argument("--max-pairs", type=int, default=10, help="Limit reported pairs")
    ap.add_argument("--min-msgs", type=int, default=2,
                    help="Skip turns with fewer messages (filter probes)")
    args = ap.parse_args()

    # Parse window
    unit_map = {"m": 60, "h": 3600, "d": 86400}
    if args.window[-1] in unit_map:
        seconds = int(args.window[:-1]) * unit_map[args.window[-1]]
    else:
        seconds = int(args.window)

    recs = load_records(seconds)
    print(f"Loaded {len(recs)} Anthropic-format MLX records from last {args.window}")

    # Filter probes
    real = [r for r in recs if len((r.get("client_body") or {}).get("messages", [])) >= args.min_msgs]
    print(f"  {len(real)} after filtering probes (msgs ≥ {args.min_msgs})")

    # Group by session
    sessions: dict[tuple, list[dict]] = defaultdict(list)
    for r in real:
        sessions[session_key(r)].append(r)

    # Pair sequential turns within each session
    pairs: list[tuple[dict, dict]] = []
    for key, recs_in_session in sessions.items():
        if len(recs_in_session) < 2:
            continue
        recs_in_session.sort(key=lambda r: r["timestamp"])
        for a, b in zip(recs_in_session, recs_in_session[1:]):
            # Only pair if r2 has more messages (sequential turn growth)
            n_a = len((a.get("client_body") or {}).get("messages", []))
            n_b = len((b.get("client_body") or {}).get("messages", []))
            if n_b > n_a:
                pairs.append((a, b))

    print(f"  found {len(pairs)} sequential-turn pairs across {len(sessions)} sessions")

    if not pairs:
        print("\nNo sequential-turn pairs found. Need a multi-turn Claude Code")
        print("session in the captured window to diagnose cache busting.")
        return 0

    # Report
    for a, b in pairs[: args.max_pairs]:
        report_pair(a, b)

    print(f"\n{'─' * 70}")
    print("Done. To find more cache-busting tokens beyond cch=, look for")
    print("offsets in the SYSTEM or MESSAGES sections that differ slightly")
    print("between turns of the same conversation. Each is a candidate for")
    print("the next normalization regex in anthropic_translator.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
