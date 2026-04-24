# Claude Code Enhancement Plan — Adopted from Field Survey

**Created**: 2026-04-24
**Status**: **P1, P2, P3, P4, P6 shipped 2026-04-24** (soaking on Unreleased). P5 deferred (HIGH risk — needs careful prompt crafting). Derived from [`docs/research/claude-code-proxy-techniques-survey.md`](../research/claude-code-proxy-techniques-survey.md)
**Related**:
- [`claude-code-performance-improvements.md`](./claude-code-performance-improvements.md) — prior plan (speculative decoding, JSON repair, per-tier routing)
- [`why-claude-code-degrades-at-30k.md`](../research/why-claude-code-degrades-at-30k.md)

---

## Motivation

The 2026-04-24 field-survey research doc extracted specific techniques from top Claude Code proxy competitors (notably `musistudio/claude-code-router` at 32.8k stars and `nicedreamzapp/claude-code-local` at 2.1k stars). This plan turns those findings into concrete enhancements, ranked by **ROI × risk**, and commits to implementing the medium-to-high-value, low-risk items.

The user's operational priority: make Claude Code CLI **faster and more stable on larger contexts** with open-source models. Every enhancement below is evaluated against that yardstick.

---

## Priority ranking methodology

Two dimensions:

**Value**: Measurable impact on the target workload (Claude Code + Qwen3-Coder-Next at 60K–150K prompts):
- **HIGH**: >20% improvement on a primary metric (latency, cache-hit rate, failure rate)
- **MEDIUM**: 5–20% improvement OR addresses a specific failure class we've seen
- **LOW**: <5% improvement OR addresses failure mode we haven't actually seen on this fleet

**Risk**: Probability of regression on something that currently works:
- **LOW**: additive change with automatic fallback; can't make a successful request fail
- **MEDIUM**: changes a behavior that's currently working; needs test coverage
- **HIGH**: changes something architectural or a default that affects all traffic

**Priority = (Value - Risk).** A HIGH-value HIGH-risk item is worse than a MEDIUM-value LOW-risk item because the risk eats the value.

---

## Enhancements

### P1 — Expanded JSON repair patterns (HIGH value, LOW risk)

**What**: Port `nicedreamzapp/claude-code-local`'s four-pattern regex catalog for tool-call recovery into our `server/tool_call_repair.py`. Their patterns handle:

- Pattern A: `parameter=key>value` (equals-sign delimited)
- Pattern B: `<parameter_key>value` (XML-ish fragments)
- Pattern C: Malformed JSON inside `"arguments"` with escaped quotes
- Pattern D: Single-arg-tool inference via a known-names table (`Bash→command`, `Read→file_path`, etc.)

**Why**: Our current repair uses the `json-repair` library which handles syntax-level errors (trailing commas, missing brackets). It does not handle **XML-in-JSON hybrids** where the model drops out of JSON mode partway and emits XML tags or free-text. We've confirmed from Claude Code's own debug captures that Qwen3-Coder-Next occasionally emits these.

**Risk assessment**: LOW. Our schema-validation guard stays in place — any repair whose output doesn't pass the tool's `input_schema` falls back to the original. A regex pattern that matches spuriously produces invalid JSON, validator rejects, no harm done. Additive strictly.

**Measurement**: Our per-model `tool_repair.{attempts, successes, failures}` counters on `/fleet/queue` will show a drop in `failures` (currently malformed → `_raw` stub) and growth in `successes` once patterns are active.

**Effort**: ~2 hours implementation + tests.

### P2 — `FLEET_ANTHROPIC_TOOLS_DENY` — server-side tool filtering (MEDIUM value, LOW risk)

**What**: New env var (comma-separated tool names) instructing the Anthropic-compat route to strip named tools from the outbound body before forwarding. Lets operators remove tools they don't use (TodoWrite if their workflow doesn't want it, NotebookEdit if they're not using Jupyter, etc.) without requiring Claude Code CLI config changes on every developer machine.

**Why**: The community has observed that stripping tool descriptions from Claude Code's system prompt (via `permissions.deny` in `~/.claude/settings.json`) saves ~40% of the tools-section tokens. That's ~6K tokens out of a 16K tools budget — enough to move the needle on cold-prefill cost. User-side config works but requires each developer to maintain it. Server-side version is a single env var.

**Risk assessment**: LOW. If a Claude Code client tries to call a tool we've stripped, Claude Code itself handles the error — it sees the tool isn't in the model's output and surfaces the failure to the user. No silent data corruption, no pipeline wedge.

**Measurement**: Token count on outbound body should drop by the summed byte-size of denied tool definitions. Visible on request traces.

**Effort**: ~1 hour.

### P3 — Size-based routing escalation (MEDIUM value, LOW risk)

**What**: Currently our `FLEET_ANTHROPIC_MODEL_MAP` routes by **tier** (`claude-haiku-*` → gpt-oss, `claude-sonnet-*` → MLX). Add a **size-based override**: requests whose prompt exceeds `FLEET_ANTHROPIC_SIZE_ESCALATION_TOKENS` route to `FLEET_ANTHROPIC_SIZE_ESCALATION_MODEL` regardless of tier. Matches `musistudio/claude-code-router`'s `longContext` pattern.

**Why**: We route `claude-sonnet-*` to `mlx:Qwen3-Coder-Next-4bit`. That model's effective context degrades above ~150K. A size-based escalation could route requests over 120K to a different model better suited for that regime (or back to the tier's default for smaller prompts). We don't currently have an opus-class model locally, but if we ever deploy GLM-5 or MiniMax M2.5 specifically for long-context runs, this would be the routing hook.

**Risk assessment**: LOW. Default setting keeps the current behavior (no escalation model). When enabled, the worst case is routing to a model that's *also* fine — no failure mode added.

**Measurement**: Log lines show which route was selected per request; new escalation events are visible.

**Effort**: ~2 hours.

### P4 — Warm-prompt preload on MLX supervisor startup (LOW value, LOW risk)

**What**: After `mlx_lm.server` comes online, fire a single request with a known Claude Code-shaped system prompt + tools to warm its prompt cache. Next real request sees cache hits on the prefix. `waybarrios/vllm-mlx` implements this for the same reason.

**Why**: After any MLX restart, the first Claude Code turn cold-prefills the full 10K-token system prompt. A warmup request pays that cost upfront (once at startup) rather than on the first user-visible turn.

**Risk assessment**: LOW. Even if the warmup fails, it's a no-op — real traffic still works.

**Measurement**: First-post-restart `MLX stream done` should show `cached_tok > 0` for the system prompt portion.

**Effort**: ~30 min.

### P5 — Opt-in slim system prompt for Claude Code (MEDIUM value, HIGH risk)

**What**: Expose `FLEET_ANTHROPIC_SLIM_SYSTEM_PROMPT=true` (default false). When enabled, replace Claude Code's 10K-token system prompt with a compact ~500-token version tuned for local models. Based on nicedreamzapp's approach but less aggressive (we keep more of Claude Code's tool instructions rather than stripping to 100 tokens).

**Why**: Claude Code's system prompt is written for hosted Claude's exact behaviors. Local models (Qwen3-Coder-Next) respond differently to it — sometimes better with simpler prompts. Potential ~10K-token prefill savings per request when enabled.

**Risk assessment**: HIGH. Claude Code's system prompt is carefully engineered. Replacing it can change agentic behavior in hard-to-predict ways. Users enable this explicitly; default stays off; we ship with caveats and measurement tools for users to validate their own workload.

**Effort**: ~2 hours + extensive testing.

**Decision**: Ship as opt-in with clear documentation. Don't enable by default without real-world validation on multiple workloads.

### P6 — Document user-side techniques (LOW value, LOW risk)

**What**: Add a new section to `docs/guides/claude-code-integration.md` covering:
- `permissions.deny` in `~/.claude/settings.json` to strip tool descriptions (40% token reduction cited)
- 80/20 rule — stop complex work at 80% context
- Proactive `/compact` at natural task boundaries (60%, not 95%)
- Session restart pattern for very long work

**Why**: These are widely-documented community practices. Our guide should reference them rather than leaving users to rediscover via blog posts.

**Risk assessment**: LOW. Documentation change only.

**Effort**: ~30 min.

---

## What we're explicitly not doing

### Switch backend to vllm-mlx

`waybarrios/vllm-mlx` has intriguing features (continuous batching, SSD-tiered KV cache, warm prompts, 12 tool-call parsers). But switching from `mlx_lm.server` is a backend migration — days of work, loses our kv-bits patch, re-certifies the whole MLX setup. **Revisit if prompt-cache capacity becomes the bottleneck.** Currently the bottleneck is byte-stability, which our Fix B addressed.

### ReAct XML fallback for tool calling

`vibheksoni/UniClaudeProxy`'s XML-based fallback is designed for models that lack native function calling. Qwen3-Coder-Next and gpt-oss:120b both handle tools natively. Implementing this would address a problem we don't have. **Defer unless we ever route to weaker-tool-calling models.**

### Native Anthropic-on-MLX server

`nicedreamzapp/claude-code-local`'s claimed 7.5× speedup from "eliminating the proxy layer" includes many confounding tricks (slim prompt, prompt cache reuse, etc.). Replacing `mlx_lm.server` with an Anthropic-native server would be a major rewrite, and we'd lose kv-bits patch compatibility, observability depth, and multi-node routing. **Not worth it.**

### Transformer pipeline refactor (claude-code-router style)

Their architecture is more flexible than ours but not faster. We're not building a universal multi-provider router; we're building a Claude-Code-reliability layer against a specific local-model stack. **Different product.**

---

## Implementation order + sequencing

Ship in this order (all in one session if possible):

1. **P1 (expanded JSON repair)** — highest value, easy to verify via test counts.
2. **P2 (tools deny)** — straightforward, adds user control, works alongside P1.
3. **P3 (size-based routing)** — adds a routing axis without changing defaults.
4. **P4 (warm-prompt preload)** — small addition, visible on next restart.
5. **P6 (docs update)** — cheap wrap-up.
6. **P5 (slim system prompt, opt-in)** — ships behind a flag, needs careful prompt crafting. Last because it's the riskiest.

Each step: code + tests + restart + smoke. Run full test suite before moving to the next.

---

## Success criteria

Objective measurements I'll verify after each ship:

**P1 (JSON repair)**:
- `tool_repair.attempts` growth in `/fleet/queue` after restart
- `tool_repair.successes / attempts` ratio rises compared to pre-P1 baseline
- No new failures in `pytest`

**P2 (tools deny)**:
- Outbound tool-schema byte count drops when configured
- `FLEET_ANTHROPIC_TOOLS_DENY=TodoWrite,NotebookEdit` visibly strips those two tools from request body

**P3 (size-based routing)**:
- Route logs show size-based escalation when prompt crosses threshold
- When escalation is disabled (default), routing unchanged

**P4 (warm-prompt preload)**:
- After restart, first real request shows `cached_tok > 0` on the system prompt portion (not full cold prefill)

**P5 (slim prompt opt-in)**:
- Token-count measurement on outbound with flag on vs off
- No regression on test suite

**P6 (docs)**:
- New section appears in guide; cross-linked from troubleshooting

All of the above should be verifiable via our existing `scripts/benchmark-performance.py` + `/fleet/queue` + test suite.
