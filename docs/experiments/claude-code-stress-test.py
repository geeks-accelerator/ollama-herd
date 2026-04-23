#!/usr/bin/env python3
"""Claude Code stress test — replay real failure shapes against MLX or Ollama.

Extracted patterns from ~/.fleet-manager/logs/herd.jsonl show Claude Code
requests to qwen3-coder:30b-agent cluster around:

  - stream=False, tools=27, msgs=55, max_tokens=32000  (agentic middle-of-task)
  - stream=False, tools=27, msgs=3,  max_tokens=32000  (early agentic turn)
  - stream=False, tools=27, msgs=1,  max_tokens=32000  (first turn w/ tools)
  - stream=True,  tools=27, msgs=1,  max_tokens=32000  (streamed first turn)
  - stream=False, tools=0/1, msgs=1, max_tokens=512    (haiku/verify probes)

This script synthesises Claude-Code-shaped requests matching those patterns
and fires them at a target (Anthropic Messages via herd, OR raw OpenAI
chat.completions at mlx_lm.server) to compare outcomes.

Usage:
  python docs/experiments/claude-code-stress-test.py --target mlx
  python docs/experiments/claude-code-stress-test.py --target herd
  python docs/experiments/claude-code-stress-test.py --target mlx --runs 3 --patterns big_agentic
  python docs/experiments/claude-code-stress-test.py --target mlx --output results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Claude Code's real tool surface (27 tools — roughly what Claude Code advertises)
# ---------------------------------------------------------------------------

TOOL_NAMES = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite", "WebFetch",
    "WebSearch", "Task", "NotebookEdit", "SlashCommand", "ExitPlanMode",
    "KillShell", "BashOutput", "MultiEdit", "AskUserQuestion", "Agent",
    "ScheduleWakeup", "mcp__ide__executeCode", "mcp__ide__getDiagnostics",
    "mcp__github__create_pr", "mcp__github__list_issues",
    "mcp__filesystem__read", "mcp__filesystem__write",
    "mcp__sqlite__query", "mcp__playwright__navigate",
]


def make_tools(n: int) -> list[dict[str, Any]]:
    """Build n Anthropic-format tool definitions with chunky JSON schemas."""
    out = []
    for i in range(n):
        name = TOOL_NAMES[i] if i < len(TOOL_NAMES) else f"tool_{i}"
        out.append({
            "name": name,
            "description": (
                f"Tool {name}. Use this when you need to perform the action "
                f"described. Provides structured access to system capabilities. "
                f"Follow the schema carefully — malformed arguments are rejected."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Target path"},
                    "content": {"type": "string", "description": "Payload content"},
                    "options": {
                        "type": "object",
                        "properties": {
                            "recursive": {"type": "boolean"},
                            "force": {"type": "boolean"},
                            "timeout": {"type": "number"},
                        },
                    },
                },
                "required": ["path"],
            },
        })
    return out


def tools_openai_format(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool schema → OpenAI tools=[{type:function,function:...}]."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Message fabrication — make the conversation history look Claude-Code-ish
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are Claude Code, Anthropic's official CLI for coding tasks. "
    "You help with software engineering: read files, edit code, run commands, "
    "debug issues. Be concise. Use tools when needed. Follow the user's "
    "codebase conventions. Never invent APIs."
)


def make_messages(n: int) -> list[dict[str, Any]]:
    """Build n messages alternating user/assistant, Claude-Code-shaped."""
    msgs: list[dict[str, Any]] = []
    # First user turn — sets the task
    msgs.append({
        "role": "user",
        "content": (
            "I'm debugging a Python fleet manager that routes LLM requests. "
            "The watchdog in src/fleet_manager/node/ollama_watchdog.py isn't "
            "firing when /api/chat hangs but /api/tags still answers. Can you "
            "read the file and figure out why the cooldown isn't respecting "
            "the consecutive-failures threshold?"
        ),
    })
    # Alternate assistant/user turns. Assistant turns reference tool usage.
    sample_assistant_bodies = [
        "I'll read the watchdog file first to see the current logic.",
        "Looking at _record_failure: the counter increments before the cooldown "
        "check, which is correct. Let me check _can_kick.",
        "The cooldown elapsed check uses time.time() - self._last_kick_ts. "
        "If _last_kick_ts starts at 0.0, that's ~1.7e9 seconds elapsed — "
        "always passes. That's fine for first kick.",
        "Now the probe loop in _one_cycle. Tags ok + no hot models → resets "
        "counter to 0. That's the bug you're describing: if Ollama is wedged "
        "but nothing is loaded (e.g. crashed runner cleared /api/ps), we "
        "never fire.",
    ]
    sample_user_bodies = [
        "Right. What if the model fell off /api/ps because the runner crashed? "
        "Tags responds because `ollama serve` is fine, but ps is empty.",
        "Can you also check the _probe_chat timeout path? I saw a 200-second "
        "Ollama stall where tags=200 in 4ms but chat never returned.",
        "What's the right fix — probe chat even with no hot model? Or detect "
        "the empty-ps state as suspicious after a known-loaded model vanished?",
        "Try reading the agent.py file next — I want to see how the watchdog "
        "is wired into the node agent startup.",
    ]
    a = 0
    u = 0
    while len(msgs) < n:
        if len(msgs) % 2 == 1:
            msgs.append({
                "role": "assistant",
                "content": sample_assistant_bodies[a % len(sample_assistant_bodies)],
            })
            a += 1
        else:
            msgs.append({
                "role": "user",
                "content": sample_user_bodies[u % len(sample_user_bodies)],
            })
            u += 1
    # Ensure conversation ends on a user turn so the model has something to answer.
    if msgs[-1]["role"] != "user":
        msgs.append({
            "role": "user",
            "content": "Please continue — propose the concrete patch and show me the diff.",
        })
    return msgs


# ---------------------------------------------------------------------------
# Pattern definitions — real failure shapes from herd.jsonl
# ---------------------------------------------------------------------------

@dataclass
class Pattern:
    name: str
    description: str
    tools: int
    msgs: int
    stream: bool
    max_tokens: int


@dataclass
class ContextPattern:
    """Like Pattern, but stuffs the first user message with N tokens of
    realistic-looking source code to simulate Claude Code reading a big file
    (or many files concatenated) into context."""
    name: str
    description: str
    target_tokens: int  # approximate prompt token count to hit
    tools: int = 27
    stream: bool = True
    max_tokens: int = 4096


CONTEXT_PATTERNS: dict[str, ContextPattern] = {
    # max_tokens=128 so total≈prompt-processing time (TTFT captures PP exactly for stream=True)
    "ctx_50k":  ContextPattern("ctx_50k",  "50K-token prompt (medium repo dump)",  50_000,  max_tokens=128),
    "ctx_100k": ContextPattern("ctx_100k", "100K-token prompt (large file set)",   100_000, max_tokens=128),
    "ctx_150k": ContextPattern("ctx_150k", "150K-token prompt (deep agentic)",     150_000, max_tokens=128),
    "ctx_200k": ContextPattern("ctx_200k", "200K-token prompt (Claude max)",       200_000, max_tokens=128),
}


# Realistic-ish "source code" chunk used to inflate context. Mixes English,
# punctuation, code keywords — closer to real tokenization than lorem ipsum.
_CHUNK = """
# fleet_manager/server/scorer.py — 7-signal scoring engine
def score_node(node: Node, request: InferenceRequest, weights: ScoringWeights) -> float:
    \"\"\"Combine thermal, memory, queue depth, wait time, model affinity,
    availability, and context-fit signals into a single score in [0, 1].
    Higher is better. Returns 0 if any hard constraint is violated.\"\"\"
    if node.is_offline or node.is_draining:
        return 0.0
    if not node.has_model(request.model):
        affinity = 0.0
    else:
        affinity = 1.0 if node.model_is_hot(request.model) else 0.5
    thermal = 1.0 - clamp01(node.thermal_pressure)
    memory  = 1.0 - clamp01(node.memory_pressure)
    queue   = 1.0 / (1.0 + node.queue_depth(request.model))
    wait    = 1.0 / (1.0 + node.estimated_wait_s(request))
    avail   = node.availability_score()
    ctxfit  = 1.0 if request.estimated_tokens <= node.max_context_for(request.model) else 0.3
    return (
        weights.thermal * thermal
      + weights.memory  * memory
      + weights.queue   * queue
      + weights.wait    * wait
      + weights.affinity * affinity
      + weights.avail   * avail
      + weights.ctxfit  * ctxfit
    )
"""


def make_padded_message(target_tokens: int) -> str:
    """Approximate target_tokens by repeating a code chunk. ~4 chars/token rule.
    Uses a unique nonce prefix per call so the mlx_lm.server prompt cache can't
    short-circuit cold prompt-processing — we want real PP numbers."""
    import uuid
    target_chars = target_tokens * 4
    repeats = max(1, target_chars // len(_CHUNK))
    body = _CHUNK * repeats
    preface = (
        f"Session nonce {uuid.uuid4()} — unique run.\n"
        "I'm working on the ollama-herd codebase and I've pasted in the relevant "
        "source files below. Please read them carefully, then at the end I'll ask "
        "a specific question.\n\n"
        "=== BEGIN SOURCE DUMP ===\n"
    )
    suffix = (
        "\n=== END SOURCE DUMP ===\n\n"
        "Now: based on the scoring code above, identify the one signal whose "
        "weight has the largest impact on routing decisions when the fleet is "
        "thermally constrained, and explain why in 2 sentences."
    )
    return preface + body + suffix


def build_anthropic_body_ctx(p: ContextPattern, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": p.max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": make_padded_message(p.target_tokens)}],
        "stream": p.stream,
    }
    if p.tools:
        body["tools"] = make_tools(p.tools)
    return body


def build_openai_body_ctx(p: ContextPattern, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": p.max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": make_padded_message(p.target_tokens)},
        ],
        "stream": p.stream,
    }
    if p.tools:
        body["tools"] = tools_openai_format(make_tools(p.tools))
    return body


PATTERNS: dict[str, Pattern] = {
    "big_agentic": Pattern(
        "big_agentic",
        "Dominant failure shape: agentic mid-task, 27 tools, 55 msgs, non-stream",
        tools=27, msgs=55, stream=False, max_tokens=32000,
    ),
    "medium_agentic": Pattern(
        "medium_agentic",
        "Early agentic turn: 27 tools, 3 msgs, non-stream",
        tools=27, msgs=3, stream=False, max_tokens=32000,
    ),
    "first_turn": Pattern(
        "first_turn",
        "First turn with full tool surface: 27 tools, 1 msg, non-stream",
        tools=27, msgs=1, stream=False, max_tokens=32000,
    ),
    "first_turn_streamed": Pattern(
        "first_turn_streamed",
        "Streamed first turn: 27 tools, 1 msg, stream",
        tools=27, msgs=1, stream=True, max_tokens=32000,
    ),
    "haiku_probe": Pattern(
        "haiku_probe",
        "Claude Code verify probe: 0 tools, 1 msg, small max_tokens",
        tools=0, msgs=1, stream=False, max_tokens=512,
    ),
}


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class Result:
    pattern: str
    run: int
    target: str
    model: str
    ok: bool
    status: int
    total_ms: float
    ttft_ms: float | None = None
    bytes_received: int = 0
    error: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Request drivers
# ---------------------------------------------------------------------------

def build_anthropic_body(p: Pattern, model: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": p.max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": make_messages(p.msgs),
        "stream": p.stream,
    }
    if p.tools:
        body["tools"] = make_tools(p.tools)
    return body


def build_openai_body(p: Pattern, model: str) -> dict[str, Any]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + make_messages(p.msgs)
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": p.max_tokens,
        "messages": msgs,
        "stream": p.stream,
    }
    if p.tools:
        body["tools"] = tools_openai_format(make_tools(p.tools))
    return body


def run_request(
    *,
    url: str,
    body: dict[str, Any],
    pattern: Pattern,
    run: int,
    target: str,
    model: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> Result:
    headers = headers or {}
    started = time.perf_counter()
    ttft: float | None = None
    total_bytes = 0
    status = 0
    err = ""
    ok = False
    try:
        if pattern.stream:
            with httpx.Client(timeout=httpx.Timeout(timeout)) as c:
                with c.stream("POST", url, json=body, headers=headers) as resp:
                    status = resp.status_code
                    for chunk in resp.iter_bytes():
                        if ttft is None and chunk:
                            ttft = (time.perf_counter() - started) * 1000.0
                        total_bytes += len(chunk)
                    ok = status == 200
        else:
            with httpx.Client(timeout=httpx.Timeout(timeout)) as c:
                resp = c.post(url, json=body, headers=headers)
                status = resp.status_code
                total_bytes = len(resp.content)
                ok = status == 200
    except httpx.TimeoutException as exc:
        err = f"timeout: {exc}"
    except httpx.HTTPError as exc:
        err = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    total_ms = (time.perf_counter() - started) * 1000.0
    return Result(
        pattern=pattern.name,
        run=run,
        target=target,
        model=model,
        ok=ok,
        status=status,
        total_ms=total_ms,
        ttft_ms=ttft,
        bytes_received=total_bytes,
        error=err,
    )


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def print_table(results: list[Result]) -> None:
    # Group by pattern
    by_pattern: dict[str, list[Result]] = {}
    for r in results:
        by_pattern.setdefault(r.pattern, []).append(r)

    header = f"{'PATTERN':<22} {'RUNS':>4} {'OK':>3} {'FAIL':>4} {'p50 ms':>9} {'p95 ms':>9} {'ttft p50':>9} {'bytes avg':>11}"
    print(header)
    print("-" * len(header))
    for name, rs in by_pattern.items():
        oks = [r for r in rs if r.ok]
        fails = [r for r in rs if not r.ok]
        totals = sorted(r.total_ms for r in rs)
        p50 = totals[len(totals) // 2] if totals else 0
        p95_idx = min(len(totals) - 1, int(len(totals) * 0.95))
        p95 = totals[p95_idx] if totals else 0
        ttfts = sorted(r.ttft_ms for r in rs if r.ttft_ms is not None)
        ttft_p50 = ttfts[len(ttfts) // 2] if ttfts else None
        avg_bytes = int(statistics.mean(r.bytes_received for r in rs)) if rs else 0
        print(
            f"{name:<22} {len(rs):>4} {len(oks):>3} {len(fails):>4} "
            f"{p50:>9.0f} {p95:>9.0f} "
            f"{(f'{ttft_p50:.0f}' if ttft_p50 is not None else '-'):>9} "
            f"{avg_bytes:>11}"
        )
    print()
    # Failure detail
    fails = [r for r in results if not r.ok]
    if fails:
        print("Failures:")
        for r in fails:
            print(f"  [{r.pattern} run={r.run}] status={r.status} {r.total_ms:.0f}ms  {r.error}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["mlx", "herd", "ollama"], default="mlx",
                    help="mlx=direct mlx_lm.server, herd=via /v1/messages, ollama=OpenAI-compat")
    ap.add_argument("--url", default=None, help="Override endpoint base URL")
    ap.add_argument("--model", default=None,
                    help="Model id. Default depends on target.")
    ap.add_argument("--runs", type=int, default=1, help="Runs per pattern")
    ap.add_argument("--patterns", nargs="+", default=None,
                    help=f"Subset of: {', '.join(PATTERNS)}")
    ap.add_argument("--context", nargs="+", default=None,
                    help=f"Context-stress patterns: {', '.join(CONTEXT_PATTERNS)}. "
                         "Replaces --patterns when set.")
    ap.add_argument("--timeout", type=float, default=300.0, help="Per-request timeout (s)")
    ap.add_argument("--output", default=None, help="Write JSON results to this path")
    ap.add_argument("--anthropic-key", default="sk-dummy",
                    help="x-api-key header when target=herd (herd doesn't check, but clients send it)")
    args = ap.parse_args()

    # Resolve URL + model defaults per target
    if args.target == "mlx":
        base = args.url or "http://localhost:11440"
        url = f"{base}/v1/chat/completions"
        model = args.model or "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
        headers = {"Content-Type": "application/json"}
        build = build_openai_body
    elif args.target == "herd":
        base = args.url or "http://localhost:11435"
        url = f"{base}/v1/messages"
        model = args.model or "claude-sonnet-4-5-20250929"  # herd maps this
        headers = {
            "Content-Type": "application/json",
            "x-api-key": args.anthropic_key,
            "anthropic-version": "2023-06-01",
        }
        build = build_anthropic_body
    else:  # ollama
        base = args.url or "http://localhost:11434"
        url = f"{base}/v1/chat/completions"
        model = args.model or "qwen3-coder:30b-agent"
        headers = {"Content-Type": "application/json"}
        build = build_openai_body

    if args.context:
        unknown = [p for p in args.context if p not in CONTEXT_PATTERNS]
        if unknown:
            print(f"Unknown context patterns: {unknown}. Valid: {list(CONTEXT_PATTERNS)}", file=sys.stderr)
            return 2
        pattern_names = args.context
        ctx_mode = True
    else:
        pattern_names = args.patterns or list(PATTERNS.keys())
        unknown = [p for p in pattern_names if p not in PATTERNS]
        if unknown:
            print(f"Unknown patterns: {unknown}. Valid: {list(PATTERNS)}", file=sys.stderr)
            return 2
        ctx_mode = False

    print(f"target={args.target}  url={url}  model={model}  runs={args.runs}  timeout={args.timeout}s")
    print(f"patterns: {pattern_names}")
    print()

    # Pick the right body builder for context vs replay mode
    if ctx_mode:
        ctx_build = build_anthropic_body_ctx if args.target == "herd" else build_openai_body_ctx
    results: list[Result] = []
    for name in pattern_names:
        p = CONTEXT_PATTERNS[name] if ctx_mode else PATTERNS[name]
        for run in range(1, args.runs + 1):
            if ctx_mode:
                body = ctx_build(p, model)  # type: ignore[arg-type]
                print(f"[{name} run={run}/{args.runs}] target_tokens≈{p.target_tokens} "
                      f"tools={p.tools} stream={p.stream} max_out={p.max_tokens} ...", flush=True)
            else:
                body = build(p, model)
                print(f"[{name} run={run}/{args.runs}] tools={p.tools} msgs={p.msgs} "
                      f"stream={p.stream} max_tokens={p.max_tokens} ...", flush=True)
            r = run_request(
                url=url, body=body, pattern=p, run=run,
                target=args.target, model=model, timeout=args.timeout, headers=headers,
            )
            results.append(r)
            flag = "OK " if r.ok else "FAIL"
            ttft_s = f" ttft={r.ttft_ms:.0f}ms" if r.ttft_ms is not None else ""
            err_s = f"  err={r.error}" if r.error else ""
            print(f"  → {flag} status={r.status} total={r.total_ms:.0f}ms{ttft_s} "
                  f"bytes={r.bytes_received}{err_s}")

    print()
    print_table(results)

    if args.output:
        payload = {
            "target": args.target,
            "url": url,
            "model": model,
            "runs": args.runs,
            "patterns": pattern_names,
            "results": [asdict(r) for r in results],
        }
        with open(args.output, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {len(results)} results to {args.output}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
