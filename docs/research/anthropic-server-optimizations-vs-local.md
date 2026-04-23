# Anthropic's Server-Side Optimizations vs Local Inference

**Created**: 2026-04-23
**Status**: Research brief — informed by Claude Code source analysis + tonight's hands-on work on the herd
**Related**:
- [`docs/plans/mlx-prompt-cache-optimization.md`](../plans/mlx-prompt-cache-optimization.md) — the cache work we shipped tonight
- [`docs/research/claude-code-ollama-ecosystem-2026.md`](./claude-code-ollama-ecosystem-2026.md) — companion: the Ollama-side reliability work
- [`docs/research/claude-code-local-models.md`](./claude-code-local-models.md) — model selection for Claude Code CLI
- [`docs/plans/mlx-prompt-pruning.md`](../plans/mlx-prompt-pruning.md) — deprecated plan; this doc explains why

---

## TL;DR

**Anthropic's cost advantage on Claude Code is almost entirely in their caching infrastructure, not in model weights or client code.** A deep read of Claude Code's source plus a day of hands-on work on ollama-herd converges on the same finding from different angles.

For a personal fleet serving a single user, we can replicate ~70-80% of Claude Code's cached-turn UX with reasonable engineering effort (verified: 100% cache hit on warm turns against the 480B tonight). The remaining 20-30% is either:
- **Not applicable** to us (multi-tenant, multi-region, cross-user dedup, tiered TTL billing)
- **A genuine architectural moat** (cache editing API for surgical mid-session compaction)
- **Buildable but low-leverage for single-user** (server-side Advisor-style tool dispatch, deferred tool schema loading)

**The practical takeaway:** we just landed the optimizations that mattered (prefix cache + admission control + translator correctness). The remaining gaps either don't apply to our scenario or require multi-month engineering investments. Sessions under ~50K tokens will feel great on local MLX. Sessions past 100K with mid-session compaction will not match hosted Claude's cost profile — not because our model is worse, but because their cache infrastructure is a moat we can't afford to replicate.

---

## The catalogue: what Anthropic does server-side

Source: deep read of Claude Code's client code (see `services/compact/microCompact.ts`, `services/api/withRetry.ts`, and related) combined with observable server behavior.

### 1. Ephemeral prompt caching with tiered TTLs

**What it does.** Client marks system prompt blocks with `cache_control: {type: 'ephemeral', ttl: '5m' | '1h'}`. Server maintains a distributed KV cache keyed on: *prompt text + tools array + model + beta headers + cache scope*. Cache state preserves **actual attention compute state**, not just tokens. Cache-read tokens billed at ~10% of full input rate. 1-hour TTL gated on `PROMPT_CACHING_SCOPE_BETA_HEADER`.

**Why it's the primary cost lever.** For Claude Code, the system prompt + tools array is ~25-50K tokens. Every follow-up turn without caching would reprocess all of that. With caching, turn 2+ only processes the new user message. That's the difference between $0.50/turn and $0.05/turn at Anthropic's rate card. For long agentic sessions, **this caching layer probably represents 50-80% of the unit-cost optimization in Claude Code as a product**.

**What we replicate.** mlx_lm.server has a simpler prefix-byte-match cache — 4 slots, 16GB budget, no TTL, no billing integration. Tonight's `cch=` normalization fix (`src/fleet_manager/server/anthropic_translator.py`) gets us byte-stable prefixes across turns, which is all mlx needs to hit cache. Verified: 100% hit rate on warm Claude Code turns against the 480B.

**What we can't replicate easily.** Distributed cache across multiple inference nodes. Tiered TTLs. Differential billing (not applicable anyway — we're flat-priced per GPU). Scope-based eligibility.

**Implication for personal fleet.** We've captured the main value. The cache ISN'T a moat for single-tenant scale; the engineering work to get basic prefix match working is modest. The moat lives at multi-tenant scale.

### 2. Cache editing API — the genuine architectural moat

**What it does.** Per `services/compact/microCompact.ts`:

> "cached microcompact path — uses cache editing API to remove tool results from cached prefix without rebuilding entire message list"

Anthropic's server can **surgically remove specific messages from a cached prefix** without invalidating the rest. This requires:
- Per-token offsets into cached KV state
- Recomputing only affected attention positions (causal attention means every later token depended on the deleted one — this is non-trivial)
- Cache version updates without bumping the cache key

**Why it matters.** In long agentic sessions, conversation accumulates stale tool results: "here's the 300-line `ls` output from turn 3," "here's the 500-line file I read in turn 7." By turn 30, most of those are noise. Traditional caching forces you to either keep them all (context bloat) or drop them (invalidate the entire cache and re-prompt). Cache editing lets you **drop the stale content while keeping cache benefits on everything else**.

**Why it's hard.** Attention is causal. If token N attended to token K (K < N) and you delete K, token N's attention state needs recomputation. Done naively, this means rebuilding from K onward — which is what naive caching would force. Anthropic's infrastructure avoids this via (likely) sparse recomputation or approximation techniques we can infer but don't have public details on.

**What we can't replicate.** mlx_lm.server has no cache editing. Neither does vLLM or TGI in their shipping versions. Building this into mlx_lm.server would be a multi-month research engineering project — you're essentially rewriting the attention cache to support random access with dirty-bit tracking.

**Implication for us.** For sessions past ~50K tokens where mid-session compaction becomes necessary, Anthropic has a structural cost advantage of probably 5-10× at the same model size. Either:
- Run the 480B where long-context attention quality is better, letting us delay compaction further
- Accept that we pay full re-prompt cost on every compaction
- Offload specific long sessions to hosted Claude

**This is the single biggest remaining gap between local and hosted Claude Code.**

### 3. Server-side tools (Advisor, WebSearch, computer use)

**What it does.** Certain tools (`advisor_20260301`, `web_search_20250301`, etc.) are marked as server-side. When Claude calls them, Anthropic's infrastructure:
- Spawns a separate inference call (often to a stronger model like Opus 4.6)
- Forwards full conversation context to that sub-inference
- Runs the tool (or a real search provider for WebSearch)
- Returns result inline with main response — all within one API request from the client's perspective
- Encrypts Advisor feedback in-flight so the client never sees the reasoning

**What we could replicate.** We already have the infrastructure: router, multiple models loaded (480B + gpt-oss:120b + gemma3:27b + embed servers), trace store. Adding an `Advisor` tool that spawns a sub-call to a stronger model and returns its response as a tool result is maybe 3-5 days of work.

**Honest tradeoff.** Anthropic's Advisor abstraction is genuinely valuable for agentic coding — small model does most of the work, escalates to the big model for hard reasoning. But replicating it server-side means:
- Losing user visibility into the sub-model choice
- Requiring another layer of tool-call routing logic
- Harder to debug ("why did Advisor pick X?")

We have an easier alternative: **client-side multi-model workflow.** Claude Code can tag specific prompts with a model override (we already support that via `FLEET_ANTHROPIC_MODEL_MAP`). Let the user explicitly route hard prompts to the big model. Keeps control, keeps visibility, adds minor friction.

**Implication.** Worth building server-side only if we're targeting a "workforce platform" scenario where multiple users want turnkey escalation without configuring workflows. For personal use, explicit client-side model choice is simpler and more honest.

### 4. Deferred tool schema loading

**What it does.** Client sends `defer_loading: true` flags on tool definitions (typically MCP tools). Server sends only the tool NAME to the model, keeping full schema in a server-side registry. When the model calls `ToolSearch(tool_name)`, server fetches and injects the full schema inline.

**Why it matters.** A user with 50+ MCP tools can have 30K+ tokens of tool schemas. Sending all of them every turn is wasteful — the model usually only calls 3-5 per session. Deferred loading caps the overhead to names (maybe 1K tokens) + on-demand full schema for actually-used tools.

**What we could build.** A server-side tool schema registry in herd + detection of `defer_loading: true` flags in captured request bodies + a `ToolSearch` tool interception path. ~1 week of work.

**Observed reality.** Current captured Claude Code traffic on our fleet shows NO `defer_loading` markers. Either we don't have enough MCP tools configured for it to kick in (the 10% threshold in Claude Code's client), or the CLI version we're testing doesn't ship it. Worth re-investigating after configuring more MCP tools.

**Implication.** Low leverage until our users actually run into MCP tool bloat. Skip until the signal appears in captures.

### 5. Server-side model aliasing and routing

**What it does.** Client sends `"sonnet"`, `"opus"`, `"haiku"` as string aliases. Server resolves to specific model versions at request time, applies regional routing (AWS Bedrock), handles failover.

**What we do.** `FLEET_ANTHROPIC_MODEL_MAP` is exactly this pattern — client sends `claude-sonnet-4-5`, we map to `mlx:mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit`. Works identically in spirit.

**What they have and we don't.** Regional failover, capacity-aware region selection, silent model version migration across millions of sessions without breaking any of them. Doesn't matter for single-fleet deployment.

**Implication.** Already covered.

### 6. Capacity-aware fallback via HTTP 529

**What it does.** Under overload, Anthropic returns 529 (a non-standard status for "Site is overloaded"). Client distinguishes foreground (user-facing) from background (analytical) requests and retries 529s differently. After N consecutive 529s, client falls back to a different model.

**What we do.** Admission control in `MlxProxy` returns HTTP 503 + `Retry-After: 10` when queue exceeds `max_queue_depth`. Standard semantics. Claude Code respects Retry-After.

**What they have and we don't.** Multi-region fallback, multi-model fallback at the API layer. We don't have alternate regions. We could implement alternate-model fallback (e.g., Claude Code asks for 480B, it's overloaded, fall back to 30B) but haven't bothered.

**Implication.** Our admission control is sufficient for single-fleet. Multi-model fallback is a 2-3 day plan entry if we ever want it.

### 7. Streaming tool input with `eager_input_streaming`

**What it does.** Some tools carry this flag. Server starts streaming tool-call parameter JSON to the client before the model has finished generating the complete parameter object, allowing earlier validation / preparation.

**What we have.** mlx_lm.server streams tool calls token-by-token via OpenAI SSE. Our translator (`openai_sse_to_anthropic_events`) converts them to Anthropic `input_json_delta` events. Functionally equivalent to `eager_input_streaming` — we already do it.

**Implication.** Already covered via standard OpenAI streaming.

### 8. Prompt-too-long error metadata

**What it does.** When a request exceeds context, server returns an error with a specific "token gap" field. Client uses it to know exactly how many tokens to drop. Server is telling the client "you're 12,847 over" rather than "too long."

**What we could build.** `ContextOptimizer` already computes context fit during routing. Exposing the gap as structured error metadata would be ~1 day of work.

**Implication.** Nice-to-have, not blocking. Current behavior: requests past context just fail at mlx_lm.server with a less-structured error. Low priority.

### 9. Implicit context extension via beta headers

**What it does.** `context-1m-2025-08-07` beta header unlocks 1M context. Gating suggests either more expensive attention implementation, scarcer hardware, or differentiated pricing.

**What we have.** Qwen3-Coder family is 256K native. No gating needed (single tenant).

**Implication.** Irrelevant for us.

### 10. Server-side auto-mode classifier

**What it does.** When auto-permission mode is active, Claude Code can invoke a classifier that decides allow/ask/deny for tool calls. Invoked via `sideQuery()` — runs a lightweight model call server-side without the client seeing it as a separate inference.

**What we could build.** Similar to Advisor. ~3 days to add a "permission classifier" route that takes a pending tool call + context and returns a decision. The main model never sees it in its response stream.

**Implication.** Only relevant if we care about auto-permission semantics. For bypass-permissions mode (typical developer workflow), not needed.

---

## Inferred optimizations we can evaluate

### 11. KV cache deduplication across users

**Claim.** If Alice and Bob have nearly identical system prompts, Anthropic shares KV state for the common prefix.

**Likely false.** Information-theoretic risk (timing-correlation attacks to leak one user's prefix content from cache hit behavior of another user) is real. Production multi-tenant systems typically shard cache by tenant/org. The "cache warms up fast" observation is more likely explained by distributed cache replication within a tenant, not cross-tenant dedup.

**Implication.** Don't worry about replicating this. Not applicable and probably not what Anthropic does anyway.

### 12. Attention state compression

**Claim.** 1M context requires compression (quantization, selective retention, hierarchical attention).

**Plausible but not required.** A 1M-token KV cache for a 70B-class model with FP16 KV is ~140 GB — a single H100. Expensive but not fundamentally novel. Gating 1M context is more likely about not subsidizing the hardware allocation as a default tier than about the technology being experimental.

**Implication.** If we ever want 1M context on local hardware, we'd need a memory budget the size of one model's weights just for KV. Qwen3-Coder-480B at 4-bit is 252 GB; adding 140 GB of KV would push us past a single Mac Studio. Worth noting but not a short-term concern.

### 13. Request batching

**Claim.** Server batches low-priority requests to amortize compute.

**Likely true at Anthropic's scale**, but irrelevant for our single-user fleet with mlx_lm.server (which has `num_workers=1` per process).

**Implication.** Not applicable.

### 14. Cross-request context preservation beyond cache

**Claim.** Server tracks session-level metadata: which model version session started with, beta-header latching state, cache warming hints.

**Plausible.** The explicit header-latching logic in Claude Code's client (noted in source: "latch eligibility in bootstrap state for session stability") is evidence that this session state exists server-side.

**Implication.** We have a limited version via request_id correlation in traces. Full session state tracking would be new work, mainly useful if we build user-specific caching strategies.

### 15. Cache invalidation pipelines

**Claim.** Model version changes trigger cache invalidation logic.

**True at Anthropic's scale.** When they ship a new model behind an alias, old cache entries need to either be tagged obsolete or silently invalidated.

**Implication.** We handle this by restart (change model in `mlx_lm.server`, cache starts empty, fills naturally). Not as elegant but works for single-user.

---

## The unified picture

Anthropic's server architecture as inferred from the above:

```
┌─────────────────────────────────────────────────────────┐
│  REQUEST ENTERS SERVER                                   │
│  Keys: prompt hash + tools + model alias + betas         │
├─────────────────────────────────────────────────────────┤
│  1. Model routing (alias → version, region selection)    │
│  2. Beta header evaluation (unlock features)             │
│  3. Cache lookup (KV state for matching prefix)          │
│  4. Capacity check (return 529 if overloaded)            │
├─────────────────────────────────────────────────────────┤
│  IF CACHE HIT:                                           │
│  - Load KV state from distributed cache                  │
│  - Only process new tokens (user message + delta)        │
│  - Bill at ~10% of full input rate                       │
├─────────────────────────────────────────────────────────┤
│  DURING GENERATION:                                      │
│  - Stream tokens out (SSE)                               │
│  - Stream tool inputs eagerly (eager_input_streaming)    │
│  - If Claude calls server-side tool (advisor/search):    │
│    └── spawn sub-inference, encrypt result, inline       │
│  - Auto-mode classifier runs side queries for perms      │
├─────────────────────────────────────────────────────────┤
│  AT REQUEST END:                                         │
│  - Update KV cache with new state                        │
│  - Respect cache_control TTL settings                    │
│  - Return to client with usage metrics                   │
├─────────────────────────────────────────────────────────┤
│  BACKGROUND:                                             │
│  - Cache editing API allows surgical prefix modification │
│  - Cache invalidation on model version changes           │
│  - Capacity load shedding via 529 responses              │
└─────────────────────────────────────────────────────────┘
```

## The herd architecture as comparison

Equivalent pieces on our setup:

```
┌─────────────────────────────────────────────────────────┐
│  REQUEST ENTERS HERD ROUTER                              │
│  /v1/messages (Anthropic-format) or /api/chat (Ollama)   │
├─────────────────────────────────────────────────────────┤
│  1. Model routing (FLEET_ANTHROPIC_MODEL_MAP → mlx: or  │
│     ollama model; scoring for Ollama, direct for MLX)   │
│  2. Anthropic → Ollama → OpenAI translation in the      │
│     anthropic_translator + mlx_proxy                    │
│  3. Cache-busting token normalization (cch= → stable)   │
│  4. Admission control: MlxQueueFullError → 503          │
├─────────────────────────────────────────────────────────┤
│  IF CACHE HIT (mlx prefix match):                        │
│  - mlx_lm.server walks 4-slot ring, finds longest       │
│    matching prefix                                      │
│  - Processes only the new tokens beyond match point     │
│  - Reports cached_tokens in usage metadata              │
├─────────────────────────────────────────────────────────┤
│  DURING GENERATION:                                      │
│  - SSE translation: OpenAI → Anthropic events           │
│  - Tool call translator: dict args → JSON string        │
│  - tool_use_id preservation through translation         │
│  - Token counts captured from final usage chunk         │
├─────────────────────────────────────────────────────────┤
│  AT REQUEST END:                                         │
│  - Record trace to SQLite                               │
│  - Update rolling cache-hit-rate stats                  │
│  - Capture to debug log if FLEET_DEBUG_REQUEST_BODIES    │
├─────────────────────────────────────────────────────────┤
│  GAPS vs hosted Claude Code:                             │
│  - No cache editing (can't surgical-remove stale turns) │
│  - No server-side Advisor (client-side workflow only)   │
│  - No deferred tool loading (send all tools every turn) │
│  - No multi-region failover (single fleet)              │
│  - No cross-tenant optimizations (single user)          │
└─────────────────────────────────────────────────────────┘
```

## What this means for competitive positioning

### For building local-inference agent infrastructure

1. **The prompt cache is the moat.** If you're competing with hosted Claude on agent use cases, the cache infrastructure is where you'll be slower and more expensive. At Anthropic's scale (distributed KV + cross-node coherence + cache editing + TTL management), this represents probably a 3-year head start. For personal-scale deployments, the gap is much smaller: basic prefix matching gets you 80%+ of the value.

2. **Server-side tools are a double-edged sword.** Anthropic has Advisor, WebSearch, and other inline server-side tools. They're fast and well-integrated but opaque. For infrastructure you want full control over, replicate client-side (which is what we do via explicit multi-model workflows).

3. **1M context is a capacity gate, not a technology gate.** Anthropic bundles it with a beta header. If we ever want 1M context on local, it's a hardware-spend problem (adding ~140 GB of KV memory), not a model-capability problem.

4. **Cache-busting changes are expensive.** Any mid-session change to beta headers, tools list, model, or system prompt invalidates the cache. **This is true for Anthropic AND for our fleet.** The `cch=` fingerprint tonight was exactly this class of bug at byte scale.

### For agent architecture generally

1. **Establish stable session configuration at start.** Tools, model, permissions, beta flags. Don't change mid-flight.

2. **Don't dynamically add/remove MCP servers per task.** Costs cache, multiplies inference costs.

3. **Prefer content-addressable caching.** Stable prefixes matter more than small prefixes.

4. **Accept that some workloads belong on hosted Claude.** Not every agent task needs to run locally. Long-context deep-compaction sessions are probably cheaper and faster on hosted Claude than on any local setup.

5. **Observability of cache hit rate is mandatory.** Without it, you don't know if your optimization is working. Tonight's `cache_hit_rate` dashboard metric + `prompt_tokens` + `cached_tokens` in debug logs is the minimum viable measurement. Without that, we'd have spent weeks optimizing phantom bottlenecks.

---

## Concrete actionable items for herd

From this analysis, these are the remaining improvements ranked by leverage:

### High leverage (worth building)

- **Multi-model fallback in the model map.** If the 480B hits admission cap, fall back to 30B-A3B instead of 503. Graceful degradation under load. ~2 days. Same pattern as Anthropic's 529 → alt-model fallback.
- **Phase 4 cache warming** (already planned in `docs/plans/mlx-prompt-cache-optimization.md`). Detect new sessions, fire a synthetic warmup request, make turn 1 also fast. ~2-3 days.

### Medium leverage (worth planning but not urgent)

- **Server-side Advisor analog.** Small model escalates to big model mid-task. ~3-5 days. Real value if we expand beyond personal use.
- **Deferred tool schema loading.** Only if we see `defer_loading: true` markers in captured traffic — currently none present.
- **Structured context-overflow error metadata.** Expose token gap when requests exceed context. ~1 day.

### Low leverage (probably skip)

- **Cache editing API.** Multi-month engineering, requires mlx_lm.server source contribution or custom inference server. Only consider if we commit to sustained local Claude-Code-like product.
- **Cross-user cache optimizations.** Not applicable — single-tenant.
- **1M context support.** Hardware-spend decision, not engineering.
- **Tool pruning plan** (already deprecated in `docs/plans/mlx-prompt-pruning.md`). Cache makes it moot.

### Not worth the control tradeoff

- **Server-side tool execution orchestration.** We could but lose transparency. Prefer client-side orchestration in CLAUDE.md-driven workflows.

---

## Success metrics after tonight's work

| Metric | Target | Achieved |
|---|---|---|
| Cache hit rate on warm Claude Code turns | ≥80% | **100%** (43,123/43,124 tokens cached on a real 101-message session) |
| Synthetic-probe cold → warm latency ratio | ≥5× | **70× measured** (45.5s → 0.6s on identical-prefix streaming probes) |
| Admission control bounds MLX queue | Yes, with clean 503 | ✅ 4 succeed + 4 get 503 on 8 concurrent requests |
| Streaming usage metadata captured | Yes | ✅ `stream_options.include_usage` emits final usage chunk |
| Tool-call translation to strict OpenAI | Yes | ✅ `dict → JSON string`, +id, +type wrappers |
| Operator observability | warm/cold split, samples | ✅ dashboard chip `WARM 100% · COLD 0%` |

We matched Anthropic's basic cache behavior for single-tenant. The gaps are structural (cache editing, multi-tenancy) not effort-gated.

---

## References

- `src/fleet_manager/server/anthropic_translator.py:_normalize_cache_busting_tokens` — the cch= fix
- `src/fleet_manager/server/mlx_proxy.py:_to_openai_body` — stream_options + tool sort + tool_calls translation
- `src/fleet_manager/server/mlx_proxy.py:_acquire_slot` / `MlxQueueFullError` — admission control
- `src/fleet_manager/server/mlx_proxy.py:get_cache_stats` — warm/cold split
- `scripts/diff-sequential-turns.py` — diagnostic for finding future cache busters
- [`docs/plans/mlx-prompt-cache-optimization.md`](../plans/mlx-prompt-cache-optimization.md) — the phased plan for this work
- Claude Code source: `services/compact/microCompact.ts`, `services/api/withRetry.ts` (original inspiration for this analysis)
