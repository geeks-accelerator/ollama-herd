# Claude Code Performance: Speculative Decoding, JSON Repair, Per-Tier Routing

**Created**: 2026-04-24
**Status**: Proposed
**Related**:
- [`docs/research/why-claude-code-degrades-at-30k.md`](../research/why-claude-code-degrades-at-30k.md) — the post-mortem that produced this ship list
- [`docs/guides/claude-code-integration.md`](../guides/claude-code-integration.md) — operator-facing Claude Code integration doc (what we advertise)
- [`docs/plans/mlx-backend-for-large-models.md`](./mlx-backend-for-large-models.md) — MLX backend architecture

---

## Motivation

After the 2026-04-23 sprint (tool-schema fixup, context management, MLX wall-clock + 413, Qwen3-Coder-Next swap), Claude Code CLI works reliably on this fleet against `mlx-community/Qwen3-Coder-Next-4bit`. Three remaining levers would make it **faster and more robust** without swapping models again:

1. **Speculative decoding** — ~10–30% throughput win on every turn. Uses a small draft model that proposes tokens the main model either accepts or rejects. Tokens the draft gets right come "free" from the main model's perspective.
2. **JSON repair on malformed `tool_use` blocks** — catches a class of failures where the model produces *almost-valid* JSON (missing bracket, trailing comma, unescaped quote) and Claude Code gives up. Repair server-side, log every correction, expose as metric.
3. **Per-tier model selection** — the `FLEET_ANTHROPIC_MODEL_MAP` already supports distinct models per Claude tier (`haiku` / `sonnet` / `opus`). We've been mapping all three to the same MLX model. Splitting them lets users trade speed for quality on a per-invocation basis (`claude --model claude-haiku-4-5`).

All three are **independent** — ship any in any order.

---

## Status — what shipped, what's blocked

**Shipped 2026-04-24:**
- ✅ **#4 per-tier routing** — `claude-haiku-*` → `gpt-oss:120b` (Ollama), rest → `mlx:Qwen3-Coder-Next-4bit`. Env-file edit only.
- ✅ **#3 tool-call JSON repair** — new `server/tool_call_repair.py` with `json-repair` library. Per-model repair stats on `/fleet/queue`. 13 tests.
- ✅ **Infrastructure for #1 speculative decoding** — `FLEET_NODE_MLX_DRAFT_MODEL` + `FLEET_NODE_MLX_NUM_DRAFT_TOKENS` settings, supervisor wiring in `_build_cmd`, draft weights cached on disk (`mlx-community/Qwen3-1.7B-4bit`, ~940 MB). **Disabled** pending upstream mlx-lm fix.
- ✅ **`scripts/benchmark-performance.py`** — replays captured Claude Code requests through the router, reports p50/p95 latency + gen tokens/sec, supports `--compare` for before/after deltas.

**Blocked:**
- ❌ **#1 enabling speculative decoding** — mlx-lm 0.31.3 has [issue #1081](https://github.com/ml-explore/mlx-lm/issues/1081) (open, March 2026): `ArraysCache.is_trimmable()` returns True but the `trim()` method doesn't exist. Every speculative request fails with `ValueError: Speculative decoding requires a trimmable prompt cache (got {'ArraysCache'})`. Independent of our `--kv-bits` patch — reproduces on stock mlx-lm with zero extra flags. Revisit when upstream resolves #1081. Our infrastructure is ready: flip `FLEET_NODE_MLX_DRAFT_MODEL` to enable.

## Proposed change #1 — Speculative decoding

### What it is

`mlx_lm.server 0.31.3` exposes `--draft-model <repo>` and `--num-draft-tokens N`. On every step, the draft model generates N candidate tokens; the main model verifies in parallel and accepts the longest prefix that matches its own top-K distribution. Acceptance rates of 50–70% are typical on coding workloads — every accepted token skips a main-model forward pass.

Expected win on Qwen3-Coder-Next (80B MoE / 3B active) with a ~1.7B draft:
- Draft forward: ~1.7B compute per proposed token
- Main forward: 80B MoE routing + 3B matrix ops per token (verify or generate)
- At 60% acceptance + 4 draft tokens per step: ~1.6× effective tokens/sec on generation

Prefill is unaffected. Latency-to-first-token stays the same; steady-state generation speeds up.

### Draft-model choice — the constraint that breaks "haiku = draft"

**Your intuition**: since we need a small model anyway, use whatever we pick for `claude-haiku-*` as the draft for `claude-sonnet-*` / `claude-opus-*`. One fewer model to care about.

**Why this doesn't work**: speculative decoding requires the draft and the main model to share the **same tokenizer**. That's a hard structural requirement — the draft's proposed tokens have to be meaningful to the main model's vocabulary, or the verify step rejects everything.

Concretely:

| Candidate | Tokenizer | Usable as draft for Qwen3-Coder-Next? |
|---|---|---|
| `gpt-oss:120b` (Ollama) | OpenAI tiktoken variant | ❌ different tokenizer |
| `gemma3:27b` (Ollama) | Gemma SentencePiece | ❌ different tokenizer |
| `llama3.3:70b` | Llama BPE | ❌ different tokenizer |
| `Qwen3-1.7B-Instruct` | Qwen3 BPE | ✅ same as main |
| `Qwen3-4B-Instruct` | Qwen3 BPE | ✅ same as main |
| `Qwen3-Coder-30B-A3B` | Qwen3 BPE | ✅ same as main, but 3B active matches main's active size → minimal speedup |

So the draft model is **coupled to the main model's family**, not to the haiku tier. These are two independent decisions:

- **Main model for sonnet/opus tier** → `Qwen3-Coder-Next-4bit` (confirmed-working on this fleet)
- **Draft for the main** → must be Qwen3-family → `mlx-community/Qwen3-1.7B-Instruct-4bit` (recommended) or `Qwen3-4B-Instruct-4bit`
- **Model for haiku tier** → can be *anything* — different model, different tokenizer, no constraint. Your preference for `gpt-oss:120b` here is fine (see #4).

### Recommended draft: `mlx-community/Qwen3-1.7B-Instruct-4bit`

- Dense 1.7B params → ~1 GB weights at 4-bit → fits trivially alongside the 80B main model
- Full forward is cheap: probably ~40–50ms per token vs main's ~80–100ms
- Same tokenizer as main (Qwen3 BPE)
- Official `mlx-community` conversion → no patching, no custom work

Alternative: `Qwen3-4B-Instruct-4bit` (~2.4 GB). Higher acceptance rate (smarter draft) at ~3× the per-token cost. Tradeoff worth measuring post-ship, not pre-ship — pick 1.7B first, benchmark, reassess.

### Implementation

1. **Download the draft model** via `herd mlx pull mlx-community/Qwen3-1.7B-Instruct-4bit`.

2. **Add two settings to `src/fleet_manager/models/config.py`**:
   ```python
   # Empty string disables.
   mlx_draft_model: str = ""
   # How many tokens the draft proposes per step.  3-4 is typical;
   # higher increases acceptance opportunity but also waste on rejections.
   mlx_num_draft_tokens: int = 4
   ```

3. **Thread through `node/mlx_supervisor.py::_build_cmd()`**:
   ```python
   if self.draft_model:
       cmd += ["--draft-model", self.draft_model,
               "--num-draft-tokens", str(self.num_draft_tokens)]
   ```

4. **Set env vars in `~/.fleet-manager/env`**:
   ```
   FLEET_NODE_MLX_DRAFT_MODEL=mlx-community/Qwen3-1.7B-Instruct-4bit
   FLEET_NODE_MLX_NUM_DRAFT_TOKENS=4
   ```

5. **Restart node** → supervisor spawns `mlx_lm.server` with draft model. Cold-load takes ~20s longer (loading the draft weights too).

6. **Benchmark** — capture steady-state tokens/sec before and after on a real Claude Code session of comparable size. Measure via the existing trace store's per-request latency + completion_tokens.

### Rollback

`FLEET_NODE_MLX_DRAFT_MODEL=` (empty) in env → supervisor omits the flags → standard single-model behavior.

### Risks

- **Memory overhead**: 1.7B-4bit is ~1 GB additional resident memory. Trivial on 512 GB Mac Studio; document for smaller deployments.
- **Acceptance rate below 50% on coding workloads**: possible but unlikely given shared tokenizer + instruction-tuning. If it happens, drop to `--num-draft-tokens 2` or remove the draft entirely.
- **Cold-load time bumps**: MLX has to load both models. 20s instead of ~15s. Only relevant on restart, not steady state.

### Effort: ~half a day

Most of the work is verifying the speedup on real workloads. Code changes are <50 lines.

---

## Proposed change #3 — JSON repair on malformed `tool_use` blocks

### The failure pattern

Local coding models periodically emit JSON that's *almost* right:

```json
// What the model emits
{"file_path": "/foo", "offset": 0,}
                                ^ trailing comma — strict parsers reject
```

```json
{"command": "echo \"hello world\""}
                    ^ unescaped backslash — depending on the state
```

```json
{"path": "/foo"
// missing closing brace
```

The grammar-constrained decoding inside `mlx_lm.server` catches most of these, but not all — especially when the model is under long-context pressure and its tool-call grammar gets fuzzy. Claude Code receives the malformed `tool_use` block, its Anthropic SDK parser fails, and the session either errors out or the model retries blind.

### What "repair" means

For each `tool_use` block in an outbound response, attempt to parse the `input` field as JSON. If it fails:

1. Try `json-repair` library (pure-Python, no dependencies — handles trailing commas, unescaped quotes, unquoted keys, missing brackets).
2. Re-validate against the tool's `input_schema` (we have it — it's on the request).
3. If repair succeeded AND validation passes, use the repaired input and log a warning.
4. If repair failed OR validation still fails, pass through the original malformed block unchanged — let Claude Code handle it. **Don't silently hide real model failures.**

### Where to hook it

Two natural insertion points:

**A. In the streaming translator** (`openai_sse_to_anthropic_events` in `mlx_proxy.py`, and `ollama_chunk_to_anthropic_events` in `anthropic_translator.py`). Repair each `tool_use` block right before emitting the final Anthropic event.

**B. In `_collect_openai_stream`** (non-streaming path) before returning the assembled response. Simpler — only one place, single-shot repair.

Recommendation: **ship in (B) first** — it's the shorter path and covers non-streaming requests (including `/compact`, which is the most-likely-to-trip-the-bug case). Extend to (A) if we see streaming repair events in the logs.

### Observability

Two new metrics on the `MlxProxy`:
- `tool_call_repair_attempts: dict[str, int]` — per model_key, count of times we tried to repair
- `tool_call_repair_successes: dict[str, int]` — per model_key, count of successful repairs

Surface on `/fleet/queue` so the dashboard shows repair rate per model. If we see > 5% repair rate on a model, that's a signal the model is unreliable enough to reconsider — not something to paper over indefinitely.

Every repair also logs at WARNING level with the original + repaired input (truncated), so we can audit what's happening.

### Implementation

1. **Add dependency**: `json-repair>=0.29` to `pyproject.toml` `dependencies` (pure Python, ~100KB).

2. **Helper in `server/mlx_proxy.py`** (or a new `server/tool_call_repair.py`):
   ```python
   def repair_tool_use_input(
       raw_input: Any, tool_schema: dict | None,
   ) -> tuple[Any, bool]:
       """Return (input, was_repaired).  Failure → original unchanged."""
   ```

3. **Call sites**: `_collect_openai_stream()` for the non-streaming path. Optional: streaming translator for completeness.

4. **Wire metrics** into the existing `_record_stats` machinery.

5. **Setting to disable**: `FLEET_MLX_TOOL_CALL_REPAIR: bool = True` — on by default; allow off for debugging.

### Risks

- **Hiding real model failures.** Biggest risk. The mitigation is: never repair silently (always log), and surface the rate on the dashboard. If the repair rate climbs, that's a warning sign.
- **Incorrect repair that passes validation but changes semantics.** Hard to engineer against — best we can do is use the tool `input_schema` as a structural check and defer to the client parser for the rest.
- **Added per-response latency.** `json-repair` is O(n) over the input string. For a 200-character tool call, sub-millisecond. Negligible.

### Effort: ~3 hours

Core helper + tests + metrics + dashboard wiring.

---

## Proposed change #4 — Per-tier model selection

### The setup

`FLEET_ANTHROPIC_MODEL_MAP` is a JSON object keyed by Claude model id. Each Claude Code invocation sends one of those ids via `--model`. Today all ids map to the same MLX model.

Example current state (from `~/.fleet-manager/env`):
```json
{
  "default": "mlx:mlx-community/Qwen3-Coder-Next-4bit",
  "claude-haiku-4-5": "mlx:mlx-community/Qwen3-Coder-Next-4bit",
  "claude-sonnet-4-5": "mlx:mlx-community/Qwen3-Coder-Next-4bit",
  "claude-sonnet-4-6": "mlx:mlx-community/Qwen3-Coder-Next-4bit",
  "claude-opus-4-7": "mlx:mlx-community/Qwen3-Coder-Next-4bit"
}
```

### Your proposal: `gpt-oss:120b` for `claude-haiku-*`

Honest appraisal: **this is a good call** and independent of the draft-model question. Reasoning:

- `gpt-oss:120b` is already pinned + hot on this fleet. Zero additional memory cost.
- Has been rock-solid for your production scripts.
- Different model family than Qwen3-Coder-Next — diversifies failure modes (if Qwen3 has a bad day, haiku still works).
- Haiku-tier workloads are typically short, tool-light turns where gpt-oss:120b's reasoning quality is overkill *in a good way* (fast convergence, fewer retries).
- Tokenizer independence is fine here — each tier handles its own inference end-to-end, nothing crosses tokenizer boundaries the way speculative decoding would.

**Concern to flag**: you mentioned earlier you didn't want Claude Code contaminating `gpt-oss:120b` because production scripts depend on it. If claude-haiku requests start landing on gpt-oss, they'll queue alongside production script calls. Two mitigations:

1. **Only route haiku → gpt-oss** for short prompts. Not easy to enforce in the router's current shape — would need a prompt-size gate on per-tier mapping. Scope creep.
2. **Accept the shared queue** and monitor. Production scripts run continuously; haiku calls are bursty and short. Likely fine. The MLX queue depth bump to 10 earlier today gives the Ollama side room too.

Recommendation: **accept the shared queue**. Revisit only if you observe production-script latency spikes correlated with Claude Code haiku activity.

### Recommended new map

```json
{
  "default": "mlx:mlx-community/Qwen3-Coder-Next-4bit",
  "claude-haiku-4-5": "gpt-oss:120b",
  "claude-haiku-4-5-20251001": "gpt-oss:120b",
  "claude-sonnet-4-5": "mlx:mlx-community/Qwen3-Coder-Next-4bit",
  "claude-sonnet-4-6": "mlx:mlx-community/Qwen3-Coder-Next-4bit",
  "claude-opus-4-7": "mlx:mlx-community/Qwen3-Coder-Next-4bit"
}
```

### Implementation

Zero code changes. Edit `FLEET_ANTHROPIC_MODEL_MAP` in `~/.fleet-manager/env`, restart the router.

### Usage

From the Claude Code CLI side, users pick a tier per invocation:

```bash
# Fast tier — gpt-oss:120b, short turns
claude --model claude-haiku-4-5

# Quality tier — Qwen3-Coder-Next, long context, tool-heavy
claude --model claude-sonnet-4-5

# (same as sonnet today; room to upgrade later)
claude --model claude-opus-4-7
```

### Effort: ~5 minutes

Config edit + restart. The useful part is deciding the mapping + documenting it; no code touches.

---

## Shipping order + dependencies

None of the three depend on each other. Ship in any order. My recommendation:

1. **#4 first** (5 min). Zero risk, zero code. Gets you speed/quality trade-off on every invocation. Easy win.
2. **#1 next** (half day). Biggest quality-of-life win — every sonnet/opus turn gets faster.
3. **#3 last** (3 hours). Addresses a class of failure that `force_all` + tool-schema-fixup already reduce. Ship when you see repair rate > 1% would be worthwhile; ship pre-emptively if you want the observability now.

## What we're not doing and why

- **Grammar-constrained decoding via `outlines`** — nice-to-have if #3 repair rate turns out to be high, but adds meaningful compile-time overhead and the grammar for 27 tools is non-trivial to maintain.
- **Prompt-cache warming at session start** — negligible benefit; the cache warms naturally on turn 1.
- **Auto-retry after wall-clock 413** — violates correctness invariants for agentic tool use (alters context between attempts). Covered in the research doc.
- **Context-aware routing (Ollama for short turns, MLX for long)** — interesting but requires a per-request classifier we don't have. Revisit if #4's static per-tier routing proves too coarse.
