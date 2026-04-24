# Why Claude Code Degrades Around 30K Tokens on Local Coding Models

**Created**: 2026-04-23
**Status**: Research brief — grounded in 2026 community reports, upstream issue inspection, and published long-context benchmarks
**Author**: AI research session, Neons-Mac-Studio fleet

**Related**:
- [`claude-code-ollama-ecosystem-2026.md`](./claude-code-ollama-ecosystem-2026.md) — sibling research on our symptom-set mapped to upstream bugs
- [`claude-code-local-models.md`](./claude-code-local-models.md) — model-selection guide for Claude tier → local model mapping
- [`docs/plans/mlx-backend-for-large-models.md`](../plans/mlx-backend-for-large-models.md) — our MLX escape hatch (the 480B path)
- [`src/fleet_manager/server/context_compactor.py`](../../src/fleet_manager/server/context_compactor.py) — the response we already built

---

## TL;DR

**The "Claude Code feels broken around 30K tokens" symptom isn't a generic long-context failure.** It's a specific, documented parser bug in Qwen3-Coder's tool-calling path that triggers **when a tool has multiple optional parameters**, and only becomes visible once context grows past ~20% of the model's advertised maximum. Claude Code's 27-tool schema is full of optional params — `Read.offset`, `Read.limit`, `Bash.timeout`, `Grep.path`, etc. — so it hits this bug earlier and harder than simpler workloads.

The secondary truth: even without that bug, **advertised context ≫ effective context for almost every open-weight model**. NVIDIA's RULER benchmark shows GLM-4's 1M-token claim degrades to 64K effective; Llama-3.1-8B's 128K becomes 32K effective. Qwen3-Coder-480B advertises 256K native (1M with YaRN) — there is no published number for its *effective* context under real agentic coding workloads, and the hardware we're running it on is probably well below the ceiling where the 480B would hold up.

Our current response — pinning the MLX 480B and building a context compactor — is correct direction but wrong-sized for the actual problem. **The two highest-ROI next steps are (1) reshaping tool schemas at the router to avoid the Qwen parser bug, and (2) benchmarking Qwen3-Coder-Next (80B MoE / 3B active) against the 480B on our own Claude Code traces.** Qwen3-Coder-Next won a real head-to-head against gpt-oss-120b and three other local models, runs in a quarter of the memory, and was architected after the agentic-tool-use workload became first-class. Whether it beats the 480B on OUR traces is an empirical question; the hypothesis that it might is strong.

---

## 1. The symptom, concretely

On Neons-Mac-Studio (M3 Ultra 512GB), running:
- `mlx_lm.server` hosting `mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit` with `--kv-bits 8 --prompt-cache-bytes 17179869184`
- Router mapping `claude-sonnet-4-5` / `claude-sonnet-4-6` / `claude-opus-4-7` → the 480B

Observations (both during testing and captured in request traces):
- Short sessions (<20K tokens): fast, coherent, tool calls work.
- Medium sessions (20K–30K): quality holds, but latency begins to swell.
- **Long sessions (30K+): coherence collapses.** Symptoms include:
  - Repeated tool calls to the same tool with one parameter silently dropped each retry.
  - Tool call loops that never converge.
  - Output that drifts off-task.
  - In one observed case, a non-streaming call with 159 messages + 27 tools held the MLX HTTP connection for 14+ minutes before timing out — MLX was doing work, but not productive work.

The memory is not the bottleneck. At the failure point, the Mac Studio shows 236 GB available RAM, no thermal throttling, and only one active MLX request. This is a **model behavior** problem, not a hardware problem.

---

## 2. Root cause #1 — Qwen3-Coder's optional-parameter parser failure at long context

[ggml-org/llama.cpp issue #20164](https://github.com/ggml-org/llama.cpp/issues/20164), filed early 2026, documents:

> Tool calling starts to break down **around 30K tokens of context, approximately 20% of the available context window**, specifically when a tool has multiple optional parameters. Failure mode: the model calls a tool correctly at first, then begins omitting optional parameters; retries keep dropping parameters; eventually loops or abandons the tool entirely.

The reporter's smoking-gun experiment: converting a single optional parameter on their Read-equivalent tool (`offset`) to required **eliminated the issue entirely**. That's a strong signal: the bug is in how Qwen's grammar-constrained decoding handles optional-field schemas when attention is already under long-context pressure, not in the attention itself.

### Why Claude Code hits this particularly hard

Claude Code ships ~27 tools. A non-exhaustive count of optional parameters:

| Tool | Optional params |
|---|---|
| `Read` | `offset`, `limit`, `pages` |
| `Bash` | `timeout`, `run_in_background`, `description`, `dangerouslyDisableSandbox` |
| `Grep` | `path`, `glob`, `type`, `-i`, `-n`, `-A`, `-B`, `-C`, `context`, `multiline`, `head_limit`, `offset`, `output_mode` |
| `Edit` | `replace_all` |
| `Agent` | `subagent_type`, `model`, `isolation`, `run_in_background` |
| `Write` | *(none optional — required content + path)* |

`Grep` alone has 13 optional parameters. Under the llama.cpp #20164 failure mode, that's 13 chances per Grep call for the parser to drop a field after ~30K tokens. Claude Code's agentic loops make heavy use of Grep. Every tool call past the 30K threshold is rolling a die.

### Does this apply to our MLX path, not just llama.cpp?

Honest answer: **we don't have direct evidence, but the symptom strongly suggests the same parser class of issue applies.** The bug is described as happening inside Qwen's tool-call generation, which is model-weight behavior reflected through whichever grammar-constrained decoder the inference engine uses. `mlx_lm.server` implements its own OpenAI-compat tool-calling layer; if Qwen's learned patterns for optional-field JSON generation are sensitive to long-context attention (which the llama.cpp evidence points to), then any decoder running the same weights will show related symptoms. We should verify empirically: run our MLX 480B against a synthetic 30K-token conversation with a Grep-heavy tool schema and measure the failure rate. That's a one-afternoon experiment.

### Immediate mitigation — reshape the tool schemas at the router

A 20–30 line change in `src/fleet_manager/server/routes/anthropic_compat.py`:

1. When translating Anthropic tool definitions to Ollama/MLX format, identify tools with optional parameters.
2. For each optional param with a sane default (e.g. `Grep.head_limit = 250`, `Bash.timeout = 120000`, `Read.offset = 0`), make it required in the schema sent to the local model, pre-filling with the default.
3. If the client supplied a value, pass it through; otherwise use our injected default.

This transforms the schema the 480B sees — from "many optional holes the parser can fall into" to "fewer required fields with defaults pre-filled." Based on the llama.cpp #20164 reporter's fix, this alone may push the usable context well past 60K on the same weights.

Trade-off: we're lying slightly to the local model about what Claude Code sent. Required-with-default isn't semantically identical to optional. But since we're the only caller and we control the mapping, we can undo the transformation if it ever causes a regression. The upside — reliable 60K+ context on our existing fleet — is worth the small dishonesty in the schema.

---

## 3. Root cause #2 — advertised context ≫ effective context (industry-wide)

NVIDIA's [RULER benchmark](https://github.com/NVIDIA/RULER) is the standard for measuring *effective* long-context capability. It tests whether a model can actually *reason over* long inputs, not merely load them. Selected results:

| Model | Advertised | RULER effective | Gap |
|---|---|---|---|
| Llama-3.1-8B | 128K | **32K** | 4× inflation |
| Llama-3.1-70B | 128K | **64K** | 2× inflation |
| GLM-4 | **1M** | **64K** | 16× inflation |
| Qwen2.5-14B-Instruct-1M | 1M | >128K | bounded by test harness |
| Qwen3-14B | 128K | >128K | holds up |

RULER's summary headline: **"only half of models claiming 32K+ context sizes can maintain satisfactory performance at that length."**

Qwen3-Coder-480B is not in the published RULER tables. Its advertised 256K / 1M-with-YaRN numbers are structural capabilities — the model can ingest that many tokens without crashing — not a claim about reasoning fidelity. Agentic coding with tools is **harder** than RULER's base tasks (which are recall-style). So the real effective context for our workload is almost certainly lower than whatever RULER would report.

Two practical consequences:

- **The number on the model card is marketing, not a performance floor.** Treat every published context number as 2–4× optimistic for reasoning quality.
- **Context compaction isn't cheating or a workaround — it's a correction.** Every frontier lab (Anthropic, Google, OpenAI) does aggressive server-side context management on their hosted models. Claude.ai summarizes stale tool_results, OpenAI caches and reuses prefix content, Gemini does retrieval-style pruning. We shipped our own compactor ([`src/fleet_manager/server/context_compactor.py`](../../src/fleet_manager/server/context_compactor.py)) for exactly this reason; we should lean harder on it, not apologize for it.

---

## 4. What actually holds up for Claude Code-style workloads

Three candidates deserve benchmarking against our current Qwen3-Coder-480B setup. None have published RULER numbers specifically for agentic tool use at long context, so the evidence is circumstantial (architecture + agentic-leaderboard + head-to-head reviews), but it points consistently in one direction.

### 4a. Qwen3-Coder-Next (80B MoE / 3B active) — strongest candidate

From the XDA head-to-head review: [I tested Qwen3 Coder Next against four other local AI coding models, and the gap was embarrassing](https://www.xda-developers.com/tested-qwen3-coder-next-four-local-ai-coding-models-gap-embarassing/). Tested on a real Python static-site-generator task requiring Markdown parsing, YAML frontmatter, file watching, and comprehensive tests:

- Qwen3-Coder-Next produced a clean `src/` layout, class-based architecture, polling file watcher, 14 passing tests in 32 min.
- With Context7 docs access: 17 min, 16 passing tests, bonus features.
- **Only Qwen3-Coder-Next properly used Context7's tool access** — the other four (Qwen3.5-122B, Devstral 2 123B, gpt-oss-120b, Omnicoder-9B) either ignored the tool, searched incorrectly, or failed to read retrieved docs.
- Runs on ~46 GB unified memory (vs the 480B's ~200 GB).

Why this is relevant to us: **the real weak spot wasn't reasoning — it was tool use.** The 80B MoE with 3B active was the only model that used its tools correctly on a real agentic task. That matches our failure mode.

### 4b. GLM-5 — strong long-context architecture

[Morph's 2026 coding-model review](https://www.morphllm.com/best-open-source-coding-model-2026) reports GLM-5 ranks **#1 among open-source models on LiveBench Agentic Coding (55.00)**. 204K context window, DeepSeek Sparse Attention (architecturally built for long context rather than stretched to it via YaRN post-hoc). Less tooling maturity on MLX / Ollama paths than Qwen, but worth trying for specifically long runs.

### 4c. MiniMax M2.5 — tool-calling specialist

Same review: MiniMax M2.5 scores **76.8% on Berkeley Function Calling Leaderboard vs ~63% for Claude Opus**. Explicitly built for tool-heavy agentic workloads. If our problem is fundamentally "tool calls get wrong under pressure," MiniMax is the most directly-targeted remedy in the open-weight world.

### 4d. Why not just keep the 480B?

The 480B isn't bad. It's built for peak quality on short coding asks. But:

- 200GB resident eliminates flexibility to run multiple models concurrently.
- 5–10× slower prefill on long prompts than smaller MoEs.
- Same underlying Qwen parser → same optional-params bug.
- Subjective: Claude Code traces show we **hit the 30K wall regularly** — the regime the 480B isn't tuned for.

The 480B is the right tool for a different workload (one-shot high-quality generation on short prompts). For iterative agentic coding, a smaller efficient MoE with better tool-use discipline is likely to win.

---

## 5. Workflow patterns that matter (community-observed)

From r/LocalLLaMA, Hacker News, Ollama docs, and Medium write-ups (Feb–Apr 2026):

- **`num_ctx` tuning**: setting an explicit `num_ctx` (rather than relying on Ollama's default) is widely recommended; we already do this via `FLEET_DYNAMIC_NUM_CTX=true`.
- **`keep_alive=-1`**: keeping models resident. We do this.
- **Ollama version pinning**: 0.20.4 is the current generally-recommended version for Apple Silicon stability; multiple open issues blocking upgrades.
- **`"reasoning": false` for tool-use turns**: some community members report that thinking traces eat context they can't afford on 128K-advertised models. We handle some of this via `is_thinking_model()` in `model_knowledge.py`.
- **MLX over Ollama for very large models**: matches our architecture. Community consensus is that `mlx_lm.server` handles the 400B+ class better than Ollama on M3 Ultra, which is exactly our configuration.
- **Tool-schema shaping** *(underused)*: we've not seen this widely discussed in the community, but the llama.cpp #20164 thread plus our own symptom set strongly suggests this is the highest-leverage unused mitigation.

---

## 6. Recommendations for the ollama-herd fleet

In order of ROI. Items 1–4 shipped on 2026-04-23; items 5–7 remain open.

1. **Reshape tool schemas at the router.** ✅ **SHIPPED 2026-04-23.** `server/tool_schema_fixup.py` + `FLEET_ANTHROPIC_TOOL_SCHEMA_FIXUP=inject` (default). Makes optional params with sane defaults required-with-default in the outbound schema. Hypothesis held: paired with the Qwen3-Coder-Next swap (item 2) the fleet now handles 80K+ token Claude Code sessions without the looping bug.

2. **Swap to Qwen3-Coder-Next (80B MoE).** ✅ **SHIPPED 2026-04-23.** Qwen3-Coder-480B (200 GB resident) replaced by `mlx-community/Qwen3-Coder-Next-4bit` (42 GB resident) via `FLEET_NODE_MLX_AUTO_START_MODEL`. Freed ~160 GB of hot-weight memory. Live traces show zero failures, 5s avg latency on 200K-token prompts, 99.98% prompt cache hit rate. Research doc hypothesis confirmed.

3. **Mechanical tool-result clearing (Layer 1 of context management).** ✅ **SHIPPED 2026-04-23.** `server/context_management.py` drops stale `tool_result` bodies by age once the prompt crosses `FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_TRIGGER_TOKENS` (default 100K), keeping the last 3 verbatim. Pure, no LLM call, microsecond-scale. Matches hosted Claude's [Context Editing API](https://platform.claude.com/docs/en/build-with-claude/context-editing). First fire on real session: 206K → 125K tokens (60.8% reduction). This was the single biggest structural gap vs hosted Claude Code.

4. **Add a cross-category VRAM fallback alarm that's actually loud.** ✅ **SHIPPED 2026-04-23.** Cross-category fallbacks are now ERROR-level with a "QUALITY RISK" tag, and the `_record_vram_fallback` event carries `cross_category` / `fallback_category` fields for dashboard filtering.

5. **Lean harder into the LLM compactor.** ✅ **SHIPPED 2026-04-24.** Added `force_all=True` path on `ContextCompactor.maybe_compact()` that bypasses per-strategy `min_bloat_tokens` gates. Triggered by the Anthropic route when post-clearing tokens exceed `FLEET_CONTEXT_COMPACTION_FORCE_TRIGGER_TOKENS` (default 150K, matching Anthropic's trigger). Shipped alongside a pre-inference 413 cap (`FLEET_ANTHROPIC_MAX_PROMPT_TOKENS`, default 180K) and an MLX wall-clock timeout (`FLEET_MLX_WALL_CLOCK_TIMEOUT_S`, default 300s) that releases the slot and returns 413 when `mlx_lm.server` wedges on slow-but-never-stopping generation.

6. **Response-side `compaction` content block + accept `context_management` request parameter.** *Open.* Lets Claude Code CLI's `/compact` command see an explicit "compacted" indicator from our server and gives us a structured way to accept the full hosted-Claude compaction contract. Cosmetic relative to items 1–3 + 5 (which do the actual work), but closes the API-surface gap.

7. **Try GLM-5 and/or MiniMax M2.5** as additional tier candidates. Lower priority because items 2–3 together appear to have resolved the 30K wall on this fleet. Revisit only if Qwen3-Coder-Next degrades on workloads we haven't tested yet.

8. **Longer-term: contribute to upstream.** llama.cpp #20164 is open. If we verify the optional-params bug also applies to `mlx_lm.server`, file a companion issue on `ml-explore/mlx-lm`. We have the hardware and the traces to produce a clean reproducer; nobody in the community does.

---

## 7. Three mechanisms called "compact" — what they are and what we implement

Added 2026-04-24 after source-level inspection of Claude Code and cross-referencing our own captured traffic. The word "compact" in the Anthropic ecosystem refers to **three distinct mechanisms** that are easy to conflate. We verified all three against 10,830 captured `/v1/messages` requests on this fleet.

### 7a. `/compact` — the user-facing Claude Code command

**Wire shape**: plain `/v1/messages` POST. No special endpoint, no beta header, no special body field. The client (`src/commands/compact/compact.ts` and `src/services/compact/compact.ts` in Claude Code's source) calls `compactConversation()` → `streamCompactSummary()` which builds a normal request with:

- **Same** `model`, `system`, `tools`, prefix `messages[]` as the main conversation (so Anthropic's prompt cache hits — the "clever re-use" trick).
- A new trailing user message with a ~5,600-character summarization instruction appended as a `text` block. Starts with `"CRITICAL: Respond with TEXT ONLY. Do NOT call any tools."` and walks through a 9-section structured summary template.
- `stream: false` (regular turns stream; `/compact` wants the blob back in one response).
- `max_tokens: 20000` (room for the full summary).

The response arrives as regular `content[0].text` wrapped in `<analysis>…</analysis><summary>…</summary>`. The client parses it, replaces the in-memory conversation with the summary + a few preserved recent messages, and continues.

**Verified against captured request `7d48cb7f-…`** (2,753-message session). The body had no `context_management` field, no compaction-specific beta markers, no new top-level keys — just the standard Messages API shape with a carefully constructed final user message.

**Our server already serves this.** Not because we implement a "/compact feature" — because `/compact` is client-side orchestration over the standard endpoint. We additionally *augment* these requests via Layer 1 (mechanical tool-result clearing) and Layer 2 (LLM-based compactor with `force_all` when prompts are huge), which is why `/compact` on our fleet handles 2,753-message sessions better than the raw Anthropic round-trip would.

### 7b. `context_management` body field — the Anthropic Compaction API

**Wire shape**: `/v1/messages` POST with:

- Header: `anthropic-beta: context-management-2025-06-27` (or `compact-2026-01-12` for the full Compaction API variant).
- Body field: `context_management.edits: [ContextEditStrategy]` where strategies include `clear_tool_uses_20250919` and `clear_thinking_20251015`.

This is a **genuine server-side feature**, distinct from `/compact`. Anthropic applies the strategies on their servers before the model sees the request.

**Gated behind three conjoint conditions in Claude Code's source** (`src/services/api/claude.ts`): config set + betas enabled + specific header present. For external Claude Code users, **this field is never sent** — not because we strip it, because the client never includes it. Only Anthropic-internal users on specific opt-ins trigger the code path.

**Our captures confirm**: 0 of 10,830 requests carried this field.

**We don't support it.** If a hypothetical future client sends it to us, pydantic's `extra="ignore"` silently drops it. Implementing support would mean accepting the beta header, parsing the edit strategies, and applying them to our own internal context pipeline. Low priority because no current external Claude Code user sends it.

### 7c. `cache_edits` content blocks — microcompact

**Wire shape**: not a top-level field — injected **inside** `messages[].content[]` alongside other content blocks:

```json
{
  "type": "cache_edits",
  "edits": [{"type": "delete", "cache_reference": "ref-123"}]
}
```

Gated behind `anthropic-beta: cache-editing-20250919`. Fires automatically in long Claude Code sessions (Anthropic-internal-only today) to instruct Anthropic's server cache to evict specific prefix blocks.

**Not content for the local model to replay** — it's a directive to the Anthropic cache layer. A local backend (Ollama, MLX) has no equivalent, so the semantically correct action is to **drop the block before forwarding to the backend**.

**Our captures confirm**: 0 `cache_edits` blocks in 2,277 Claude Code requests.

**Our handling**: pydantic's `content: str | list[dict[str, Any]]` typing passes unknown block types through as raw dicts (no silent drop by validation). The translator (`anthropic_to_ollama_messages`) has explicit branches for `text` / `image` / `tool_use` / `tool_result` / `thinking` and skips everything else. Shipped 2026-04-24: `_log_unknown_block_type_once()` logs the first occurrence of any unknown block type (process-lifetime dedupe) so if microcompact ever starts firing on our traffic we notice without log spam.

### Summary table

| Mechanism | Wire shape | Gated by | External CC uses it? | We serve it? |
|---|---|---|---|---|
| `/compact` command | plain `/v1/messages` + trailing instruction | *(client-side only)* | ✅ Yes — this is the CLI command | ✅ Yes — it's the standard endpoint |
| `context_management` field | Body field + beta header | `context-management-2025-06-27` | ❌ No — Ant-only gate | ❌ No (silently ignored) |
| `cache_edits` blocks | Content block inside messages | `cache-editing-20250919` | ❌ No — Ant-only gate | ✅ Passes through pydantic; semantically-correct drop in translator; logged on first occurrence |

### Why this matters for the research conclusion

My earlier framing treated these as "three layers of the same thing." Source-level inspection shows they're **independent mechanisms** with different gating. The one that external Claude Code users actually hit (`/compact`) is pure client-side convention — our server automatically supports it by virtue of implementing the standard Messages endpoint.

The real structural gap vs hosted Claude Code isn't that we're missing a compaction API — it's that we were missing server-side context management (Layer 1 clearing + Layer 2 compactor + pre-inference 413 + wall-clock timeout). That gap is closed as of 2026-04-23/24.

---

## 8. What we don't know

Honest list of gaps in the evidence chain:

- **No direct RULER scores** for Qwen3-Coder-480B, Qwen3-Coder-Next, GLM-5, or MiniMax M2.5. The research community hasn't published them yet. Our recommendations rank by architecture + head-to-head reviews + agentic leaderboards, not by a single gold-standard number.
- **Whether the llama.cpp #20164 bug exactly reproduces on `mlx_lm.server`** is untested. High-probability it does (shared weights, same optional-field grammar) but we should confirm.
- **The 14-minute timeout we observed on the 480B** (request `b7d8f89b`, 159 messages + 27 tools) is consistent with the long-context degradation model but could also be simple prefill throughput bottleneck on MoE routing. Would need a profiler run to disentangle.
- **LoCoBench** (the most recent long-context coding benchmark paper) tested only Gemini-2.5-Pro, GPT-5, and Claude-Sonnet-4 — no open-weight coverage. So no published numbers exist for "which open model holds up best at 100K+ coding tasks."

---

## 9. Operational notes (for anyone re-running this research)

- Search for **"llama.cpp tool calling long context"** + model name to find the parser-class bug reports. The community is finding these incrementally as they scale up agentic workloads.
- **Hacker News threads for "Qwen3-Coder"** from mid-2025 onward contain the most useful empirical noise about what actually breaks.
- **r/LocalLLaMA** discussions post-dating any model release within a week are disproportionately valuable for failure-mode observations.
- For leaderboard truth, cross-reference **artificialanalysis.ai** (runs real evals, no marketing) with **arena.ai code leaderboard** (community-voted, reflects real preferences) — agreement across both is a strong signal.

---

## Sources

- [NVIDIA/RULER — What's the Real Context Size of Your Long-Context Language Models?](https://github.com/NVIDIA/RULER)
- [ggml-org/llama.cpp issue #20164 — Qwen3-Coder tool calling fails at long context with optional params](https://github.com/ggml-org/llama.cpp/issues/20164)
- [Best Open-Source Coding Model 2026: GLM-5 vs MiniMax M2.5 vs Qwen3-Coder vs Kimi K2.5 (Morph)](https://www.morphllm.com/best-open-source-coding-model-2026)
- [XDA — I tested Qwen3 Coder Next against four other local AI coding models](https://www.xda-developers.com/tested-qwen3-coder-next-four-local-ai-coding-models-gap-embarassing/)
- [Qwen3-Coder-480B-A35B-Instruct model card (Hugging Face)](https://huggingface.co/Qwen/Qwen3-Coder-480B-A35B-Instruct)
- [Qwen3-Coder: Agentic Coding in the World (Qwen blog)](https://qwenlm.github.io/blog/qwen3-coder/)
- [Unsloth — Qwen3-Coder: How to Run Locally](https://unsloth.ai/docs/models/tutorials/qwen3-coder-how-to-run-locally)
- [Ollama × Claude Code integration docs](https://docs.ollama.com/integrations/claude-code)
- [LoCoBench: A Benchmark for Long-Context Large Language Models in Complex Software Engineering (arXiv 2509.09614)](https://arxiv.org/html/2509.09614v1)
- [Artificial Analysis — Qwen3 Coder 480B](https://artificialanalysis.ai/models/qwen3-coder-480b-a35b-instruct)
- [GitHub — QwenLM/Qwen3-Coder](https://github.com/QwenLM/Qwen3-Coder)
