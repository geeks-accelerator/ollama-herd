# Plan: make session-affinity routing observable

**Created:** 2026-08-16
**Status:** 📋 PLAN — nothing built.
**Why:** A router is invisible. A request goes in, a response comes out, and it looks identical to a single machine. Session affinity is the signal most worth demonstrating and the only one a user currently cannot observe at all.

The precedent is `X-Cache: HIT/MISS` — a header no RFC ever specified, adopted by every CDN because legibility won — and Turborepo's `>>> FULL TURBO`, which made cache reuse legible with one line of stdout.

---

## ⛔ It cannot report a cache hit, and the reason is upstream

Ollama **deliberately discards** llama.cpp's cache signal. [PR #16428](https://github.com/ollama/ollama/pull/16428) (merged 2026-06-02) folds `cache_n` back into `prompt_n` to preserve pre-0.30 `prompt_eval_count` semantics:

```go
func (t llamaServerTimings) promptEvalCount() int { return t.CacheN + t.PromptN }
```

So `prompt_eval_count` **structurally cannot** yield a hit ratio. That is not an oversight to work around — it is a deliberate upstream decision, and the comment at `server/streaming.py:668` now has a citable cause. Inferring a hit from it once produced a false "zero prefix-cache reuse" report in this project.

**Therefore the header reports the routing decision we actually make, not a cache state we cannot observe:**

```
X-Fleet-Affinity: matched   # routed back to the node holding this session
X-Fleet-Affinity: new       # no prior session, or the sticky node was unavailable
```

TTFT carries the proof — it is already measured, and a warm prefix shows up as a large drop. Showing the outcome beats asserting a cache state.

---

## Work, audited against the codebase — mostly wiring

| # | Item | Reuse or build | Notes |
|---|---|---|---|
| 1 | `X-Fleet-Affinity: matched\|new` | **Reuse — one parameter** | `server/fleet_headers.py` is already the single canonical builder, called from **all 8 route files**. Its docstring exists *because* headers had drifted per-route before. Adding the field there lands it on every endpoint at once. |
| 2 | `usage.prompt_tokens_details.cached_tokens` | Build (small) | Value already exists on MLX; this surfaces it. **⚠️ the one real debt risk — see below.** |
| 3 | Per-request cached-vs-total logging, split matched/new | **Reuse — thread existing values** | `server/mlx_proxy.py` already parses `cached_tokens`, returns it in a 3-tuple, *and* folds each observation into a per-model 50-sample rolling window for the dashboard. Collection and aggregation are done. |
| 4 | Raise `--prompt-cache-size` on MLX servers | **Config only** | Default is 10, with longest-prefix matching. On a large-memory box this is cheap and directly multiplies how many sessions stay warm. |
| 5 | Decay the affinity bonus by queue depth | **BUILD — the only genuine design work** | See below. |
| 6 | Opaque session token | Build — upgrade, lowest priority | See below. |
| 7 | Tests pinning the semantics | Build | Name them for the mistake so nobody "improves" it back into inferring `hit` from `prompt_eval_count`. |

Suggested order, cheapest first: **1 → 3 → 4 → 2 → 5 → 6.**

### ⚠️ The single tech-debt risk is item 2

`cached_tokens` must be **omitted entirely on the Ollama path, never emitted as `0`.** Zero conflates "cache miss" with "cannot measure", and Ollama is structurally in the second category. This is the bug vLLM shipped ([#44383](https://github.com/vllm-project/vllm/pull/44383)) and SGLang still has ([#28801](https://github.com/sgl-project/sglang/pull/28801)). Everything else in this plan fails loudly; this one would be quietly wrong.

The field itself is a real convention — llama.cpp, `mlx_lm.server`, vLLM (behind `--enable-prompt-tokens-details`) and SGLang (behind `--enable-cache-report`) all emit it. Ollama emits it nowhere, and hardcodes `"cached_tokens": 0` on `/v1/responses`.

### Why item 5 is the real work

`ScoringEngine._score_session_affinity(self, node, session_key)` has **no queue depth in scope**. Signal 3 computes it elsewhere in the same class, so the value exists, but the decay needs a signature change *and* must compose with the existing queue penalty without double-counting the same congestion.

Every production router decays the cache bonus as the favoured node loads up — NVIDIA Dynamo's `overlap_score_credit_decay_factor`, Ray Serve's `imbalanced_threshold`, SGLang's `--balance-abs-threshold`. Without it, a warm-but-saturated node keeps winning; that is the specific bug vLLM's production-stack shipped `loadaware` routing to fix.

Note `SESSION_AFFINITY_BONUS = 20.0` is not arbitrary — the comment reasons explicitly about 20 against thermal's 50, so a hot node still wins. **The decay must preserve that ceiling relationship, not replace it.**

### Item 6 is an upgrade, not a defect

`session_key_for` already documents the limitation and reasons about it correctly:

> *"wrong in a way that costs only a cold prefill: two concurrent sessions from one IP on one model share a pin and may collide. That is strictly better than no affinity, which is a cold prefill every turn."*

The bounded downside is real, so an opaque round-tripped session token (llm-d ships `x-session-token`) is an improvement on a known tradeoff rather than a bug fix. Lowest priority.

---

## A correction to the existing docstring

`session_affinity.py` claims `OLLAMA_NUM_PARALLEL` slots "rotate", so even the right node may have evicted the conversation. **That is inverted.** llama.cpp selects the slot whose cached tokens share the **longest common prefix** with the incoming request ([discussion #13606](https://github.com/ggml-org/llama.cpp/discussions/13606)), so a node with `OLLAMA_NUM_PARALLEL=4` holds **four** warm conversations concurrently and finds the right one.

The pin is a *better* bet than documented. And the real limit is **more than N concurrent conversations per node** — a capacity constraint — not the 900s TTL, which models the wrong variable. LiteLLM shipped this exact class of bug ([#28427](https://github.com/BerriAI/litellm/issues/28427), "affinity TTL is hardcoded to 5 minutes").

**Fix the docstring as part of item 5**, since the capacity limit is what the decay should key on.

---

## ⚠️ Before publishing any benchmark

Session affinity has **never changed a routing decision on the development fleet** — it has one online node, so every request routes there regardless. The "~30K tokens of re-prefill avoided" figure is inherited from llama.cpp's documented behaviour, not measured here.

Before a number goes in the README: **two nodes, a controlled A/B (same conversation, affinity on vs off), and the MLX `cached_tokens` telemetry from item 3.** Every published benchmark in this space is at 8+ GPUs, and SGLang notes gains scale with worker count — so expect materially less at 2–5 nodes, and our own measurement will be better evidence than anything citable.

Publishing an inherited number would undercut the exact thing the header exists to enable: letting a skeptic verify the claim.

---

## The demo this unlocks

```
turn 1 -> X-Fleet-Affinity: new       X-Fleet-Node: bb    TTFT 4200ms
turn 2 -> X-Fleet-Affinity: matched   X-Fleet-Node: bb    TTFT  180ms
```

Two lines, verifiable against your own fleet. That is the artifact — for the README, the Open WebUI guide, and the "why a router beats round-robin" argument.

---

## Explicitly out of scope

- **Precise KV-event indexing** (ZMQ `KVEvents`, as in llm-d/Dynamo/AIBrix). Requires backend cooperation Ollama will never provide, and solves a problem — thousands of blocks across dozens of pods — that does not exist at 2–5 nodes.
- **KV offload tiering** (LMCache, HiCache L2/L3). Built for GPU-memory-starved fleets; on unified memory the tier below GPU *is* the GPU.
- **P/D disaggregation, prefix-aware batching.** Multi-node-per-model techniques whose coordination overhead exceeds the win at this scale.
- **Inferring cache hits from `prompt_eval_count`.** See the top of this document.
