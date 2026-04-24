# Claude Code × Local Models: Competitive Landscape (2026-04-24)

**Created**: 2026-04-24
**Status**: Research brief — ecosystem scan against public GitHub projects as of April 2026
**Related**:
- [`claude-code-ollama-ecosystem-2026.md`](./claude-code-ollama-ecosystem-2026.md) — prior research on upstream bugs + symptom mapping
- [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md) — the research that produced the reliability stack covered here
- [`claude-code-local-models.md`](./claude-code-local-models.md) — model-selection guide
- [`../guides/claude-code-integration.md`](../guides/claude-code-integration.md) — operator-facing setup guide

---

## TL;DR

**The "Claude Code proxy to local models" concept is absolutely not novel.** At least a dozen public projects do the basic Anthropic Messages API → OpenAI/Ollama/MLX translation. The most popular (nicedreamzapp/claude-code-local) has 2.1k GitHub stars, a year of development, and independently implements a subset of the same fixes we did — including tool-call JSON repair.

**What appears to be genuinely distinctive about this repo** isn't any single feature but the *combination*: layered hosted-Claude-parity context management (tool-result clearing + LLM compactor + force-compact + pre-inference 413 + wall-clock timeout), a Qwen3-Coder-specific tool-schema fixup for the [llama.cpp#20164](https://github.com/ggml-org/llama.cpp/issues/20164) bug, multi-node fleet architecture, multimodal routing (chat + image + STT + vision embeddings), and comprehensive observability. None of the public competitors I surveyed ship all of these.

**Honest positioning if we ever publish**: not "yet another Claude Code proxy" — that framing loses to the 2.1k-star competition. Instead: "reliability stack for Claude Code against local models, built from operational lessons on a multi-GPU / multi-node fleet." The differentiator is engineering breadth, not protocol novelty.

---

## Methodology + scope

This scan covers only what's publicly available on GitHub and indexable in web search as of April 2026. It does not cover:

- Enterprise / internal Anthropic customer proxies (if any exist)
- Paid SaaS gateways (Portkey, LiteLLM Cloud, Anthropic's own API Gateway docs)
- Proxies for other AI CLIs (Codex, Cursor, Cline, Aider) that also happen to handle Claude Code as a side effect

Each project below was either fetched directly via the GitHub README or evaluated from its landing-page description. Feature checks are from public documentation, not code review — I may have missed features that exist in source but aren't documented.

---

## The space: what's out there

### Direct MLX-for-Claude-Code competitors

#### 1. nicedreamzapp/claude-code-local (2.1k stars) — primary competitor

MLX-native Anthropic-compatible server for Apple Silicon. Markets itself as "100% on-device." Supports three models: Gemma 4 31B, Llama 3.3 70B, Qwen 3.5 122B (not Qwen3-Coder variants as of the scan).

**What they ship:**
- MLX-native server (~1,000 lines, `proxy/server.py`)
- `MLX_KV_BITS` config (defaults to 8-bit)
- `MLX_KV_QUANT_START` config
- "Garbled Recovery" — `recover_garbled_tool_json()` function catches XML-in-JSON hybrids with retry logic up to 2 attempts
- Speech-to-text via Apple `SFSpeechRecognizer` (sibling repo NarrateClaude)
- Prompt-cache reuse
- "Tool-result handling" (details not documented)

**What they don't ship:**
- Tool-schema fixup (the Qwen3-Coder optional-param bug workaround)
- Layered context management (no LLM-based compactor, no pre-inference 413, no wall-clock timeout)
- Multi-node / fleet
- Observability metrics (cache hit rate split, tool-repair counters, health checks)
- Image generation routing
- Vision embedding routing

**Honest assessment**: This is a well-executed single-machine MLX wrapper with good marketing ("NDA / legal / healthcare workflows"). They independently arrived at the tool-call JSON repair idea, which is validation that the problem is real. We do more, but they have a year's head start on mindshare.

#### 2. chand1012/claude-code-mlx-proxy (40 stars)

Basic MLX proxy for Claude Code. Python, uses `uv`, supports "thousands of MLX Community models from HF."

**What they ship:** Messages endpoint, token counting, streaming, health checks. Default model: `mlx-community/GLM-4.5-Air-3bit`.

**What they don't ship:** Any of the reliability engineering. It's a translation layer, nothing more.

**Honest assessment**: Early-stage personal project. Not competing at the same tier as nicedreamzapp or this repo.

#### 3. waybarrios/vllm-mlx

OpenAI- and Anthropic-compatible server built on vLLM + MLX. Claims 400+ tok/s with continuous batching, MCP tool calling, multimodal (Llama, Qwen-VL, LLaVA). Single-model-per-process focus.

**What they ship:** Performance-focused runtime. Continuous batching. MCP support.

**What they don't ship:** Fleet architecture, context management beyond what the runtime provides, Claude-Code-specific tool fixes.

**Honest assessment**: Different design point than us — they're optimizing single-model throughput, we're optimizing multi-node reliability. Complementary rather than overlapping; you could plausibly put vllm-mlx behind our router as the MLX backend.

### Anthropic → OpenAI-compat translators (broader scope, not MLX-specific)

#### 4. nielspeter/claude-code-proxy

Lightweight HTTP proxy. Supports OpenRouter (200+ models), OpenAI direct (GPT-5), Ollama. Full tool support, streaming, thinking blocks.

**What they ship**: Clean translation, multi-provider routing, tool use.

**What they don't ship**: MLX, context management, multi-node, multimodal.

#### 5. fuergaosi233/claude-code-proxy

Anthropic → OpenAI translator. Complete `/v1/messages` endpoint. Multi-provider (OpenAI, Azure OpenAI, Ollama). Tool-use conversion.

**What they ship**: Protocol translation, same shape as #4.

#### 6. musistudio/claude-code-router

Built around a "transformer pipeline" pattern where you write per-provider transformers to modify request/response payloads. Global transformers apply to all models from a provider.

**What they ship**: Extensibility / transformer framework. Supports Anthropic → anything.

**Honest assessment**: Architecturally different approach — they give you plumbing, you write adapters. Ours is opinionated pipeline with specific fixes baked in.

#### 7. MadAppGang/claudish

CLI that runs Claude Code with any AI model by proxying through a local Anthropic API-compatible server. Supports Ollama, LM Studio, vLLM, MLX.

**What they ship**: User-friendly CLI wrapper, multi-provider.

**What they don't ship**: Specific reliability fixes, fleet, observability.

### Upstream-integrated solutions

#### 8. LM Studio 0.4.1+ native Anthropic endpoint

LM Studio now natively serves `/v1/messages`. Any Anthropic-API tool talks to it with just a `ANTHROPIC_BASE_URL` change.

**What they ship**: Zero-proxy integration. Built into the product.

**What they don't ship**: Fleet, multi-node, our specific context-management layers. LM Studio is single-machine.

#### 9. Ollama 0.14.0+ native Anthropic endpoint

Same shape — Ollama now has built-in Anthropic compat. Their own docs acknowledge "edge cases in streaming and tool calling still being patched."

**What they ship**: Direct integration with the most common local-model runtime.

**What they don't ship**: Same limitations as LM Studio. Known upstream bugs (ollama#15258, the `/api/chat` hang) apply.

### Meta-proxies / mega-routers

#### 10. router-for-me/CLIProxyAPI

Wraps multiple AI CLIs (Gemini CLI, Antigravity, ChatGPT Codex, Claude Code) and re-exposes them as OpenAI/Gemini/Claude/Codex-compatible services. Lets you use "free" Gemini 2.5 Pro / GPT 5 / Claude through API. Quota-arbitrage flavor.

#### 11. decolua/9router

"Connect all AI code tools to 40+ providers and 100+ models." Mega-routing aggregator.

#### 12. Claude-Connect (drbarq)

Universal proxy — Anthropic ↔ any OpenAI-compatible API. No MLX.

### Generic LLM gateways

#### 13. LiteLLM

Generic LLM gateway with Anthropic provider support. Translates both directions. Enterprise-oriented; sprawling feature surface including routing, caching, logging, cost tracking.

**Honest assessment**: Different scope. LiteLLM is a gateway for enterprise LLM ops; we're specifically a Claude-Code reliability stack. Non-overlapping use cases.

---

## What appears genuinely distinctive in this repo

I scanned feature sets across the above. These appear nowhere in the surveyed public competition:

### 1. Tool-schema fixup for the Qwen3-Coder optional-param bug

The upstream bug at [`ggml-org/llama.cpp#20164`](https://github.com/ggml-org/llama.cpp/issues/20164) documents that Qwen3-Coder's tool-call generation starts silently omitting optional parameters at ~30K tokens, producing infinite tool-call loops. Claude Code ships ~27 tools; `Grep` alone has 13 optional parameters. Our workaround is to promote known-safe optional params (e.g. `Bash.timeout=120000`, `Grep.head_limit=250`, `Read.offset=0`) to required-with-default in the outbound schema. Nobody else I found has translated that specific upstream bug into a server-side mitigation.

See: `src/fleet_manager/server/tool_schema_fixup.py`, [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md) §2.

### 2. Hosted-Claude-parity layered context management

Four defenses stacked, cheapest-to-most-expensive:

- **Mechanical tool-result clearing** (`server/context_management.py`): drops stale `tool_result` bodies by age once the prompt crosses a threshold. Matches Anthropic's hosted Context Editing API behavior. No LLM call. Microsecond-scale.
- **LLM-based compactor** (`server/context_compactor.py`) with `force_all` session-level rescue that bypasses per-strategy bloat gates.
- **Pre-inference 413 cap**: if still oversized after both, refuse with HTTP 413 + `/compact` hint. Client owns resubmit decision.
- **MLX wall-clock timeout**: bounds total wall time so wedged generations can't hold the slot indefinitely.

`claude-code-local` (the 2.1k-star competitor) has "prompt-cache reuse" and "tool-result handling" per their README but not the layered defense. No other competitor appears to do anything like this.

See: [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md) §7 for the three-mechanism distinction between this and hosted Claude's own compaction features.

### 3. Multi-node fleet architecture

mDNS discovery, per-node pinning, bandwidth-aware scoring (chip + memory bandwidth → routing weight), 168-slot weekly capacity learning, per-(node, model) queues, structural-privacy telemetry. The other Claude-Code-specific projects are all single-machine.

See: `CLAUDE.md` architecture section.

### 4. Multimodal router

Same `score_with_fallbacks` → queue → streaming pipeline serves: chat (Ollama + MLX), image generation (mflux/DiffusionKit/Z-Image/SD), speech-to-text (Qwen3-ASR), vision embeddings (DINOv2/SigLIP/CLIP). The competition is chat-only.

### 5. Comprehensive observability

- 18 health checks surfaced on `/dashboard/api/health`
- SQLite trace store with per-request latency, tokens, tool-call metadata
- Cache hit rate split into warm/cold with explicit sample counts
- Cross-category VRAM fallback events logged at ERROR with "QUALITY RISK" annotation when vision → non-vision substitution happens
- Per-model `tool_repair: {attempts, successes, failures}` counters
- MLX queue admission state + rejection counts
- Prompt-cache observability for the Anthropic-specific path

None of the competitors expose this depth publicly.

### 6. `scripts/benchmark-performance.py` — replay against real traffic

Loads N captured Claude Code requests from `~/.fleet-manager/debug/requests.*.jsonl`, replays through the router, reports p50/p95/mean for latency, TTFT, generation tokens/sec, overall tokens/sec. `--compare` flag diffs against a prior saved run. Answers "does this knob actually help on MY workload" with real traffic, not synthetic load. I didn't find a public equivalent.

### 7. Proof-of-operational-maturity signals

- Known issues tracked in `docs/issues.md` and `docs/issues/*.md`, including `FIXED` entries with timelines and root causes (the watchdog post-mortem is a good example)
- Observations file (`docs/observations.md`) accumulates operational insights
- Removed features documented, not just deleted silently (the Ollama watchdog removal is a worked example)
- Research docs in `docs/research/` with cited sources

This isn't a feature exactly, but it signals that this project has been *operated*, not just shipped. The competitors mostly don't show this scaffolding.

---

## Where we independently converged with public work

Intellectual honesty matters. These are cases where public projects arrived at a similar fix:

- **Tool-call JSON repair**: `nicedreamzapp/claude-code-local` has `recover_garbled_tool_json()` with retry logic (up to 2 attempts). We have schema-validated single-pass repair using the `json-repair` library with per-model metrics. Same concept, different implementation. Validates the problem is real.

- **MLX `--kv-bits` support**: lots of projects pass this flag when running against `mlx_lm.server` with the KV-quant patch applied. The patch itself is upstream-pending per [mlx-lm PR #1073](https://github.com/ml-explore/mlx-lm/pull/1073).

- **Streaming translation (Anthropic SSE ↔ OpenAI SSE / Ollama NDJSON)**: every proxy in this space does this. Our implementation has some unique tool-call accumulation logic but the concept is commoditized.

- **Prompt caching strategy for the `/compact` command**: `claude-code-local` documents that they re-use the same prefix bytes to maximize Anthropic-style prompt-cache hits. Our research doc §7 notes this is exactly what hosted Claude Code does too. Widely-known idea.

---

## Honest positioning if we published

The pitch matters. Two framings:

### ❌ Framing that loses: "Novel MLX wrapper for Claude Code"

This competes directly with `nicedreamzapp/claude-code-local` (2.1k stars, year head start, simpler setup, good marketing). We would lose this framing war.

### ✅ Framing that wins: "Reliability stack for Claude Code + local models, from a multi-node fleet"

This leans on the legitimate differentiators (combination of layered context management + tool-schema fixup + multimodal + multi-node + observability + benchmark tooling). The audience for this framing is smaller — operators running Claude Code seriously enough to care about reliability engineering — but in that audience, we have real differentiation.

Sub-framings that might work:

- "Hosted-Claude-parity context management for local Claude Code" (specific, demonstrably true)
- "The Claude Code reliability stack we built from a 512GB Mac Studio + MacBook Pro fleet" (honest, personal, shows operational experience)
- "What we learned running Claude Code against local models for 100+ hours" (the real story: research docs + post-mortems + fixes)

The last one is probably the most honest marketing. The research docs (`why-claude-code-degrades-at-30k.md`, `claude-code-ollama-ecosystem-2026.md`, `claude-code-local-models.md`) and the post-mortem format (`docs/issues/ollama-watchdog-cascade-restart.md` style — even though we removed the watchdog, the post-mortem is concrete evidence of the engineering rigor) are actually the *artifacts* a skeptical reader would engage with, not the code itself.

---

## What to do about this

Three options, ordered by commitment:

1. **Do nothing public-facing.** Keep building for our own use. The stack is valuable to us regardless of public positioning. The 2.1k-star competition doesn't affect our productivity on this fleet.

2. **Write a blog post or two.** Topic candidates that lean on what's distinctive:
   - "How we reverse-engineered Anthropic's three-layer Claude Code compaction from observed request traces"
   - "The llama.cpp tool-call bug that broke our Claude Code sessions at 30K tokens, and how we fixed it at the router"
   - "Running Claude Code against a 512GB Mac Studio + MacBook Pro fleet: lessons from 100 hours of operations"
   These posts could drive traffic to the repo without competing on the "wrapper" framing.

3. **Publish to PyPI / promote actively.** Higher effort. Would need to decide whether to compete on the single-machine MLX use case (crowded) or differentiate hard on the multi-node + reliability angle. Probably (2) first, then assess whether (3) is worth doing based on response.

None of these are urgent. The fleet works. We have a solid setup. Public positioning is a separate conversation from operational value.

---

## What we don't know

Gaps in this research worth naming:

- **Closed-source / enterprise competitors**: Anthropic's own Managed Agents, Portkey, etc. May have similar reliability stacks internally. Out of scope for public-landscape analysis.
- **Stars ≠ usage**: a 2.1k-star project might have few production deployments. A 40-star project might run at 20 companies. GitHub star count is a weak quality signal.
- **Recency**: some projects scan as "less mature" based on docs but may have richer internals than their READMEs suggest. A genuine comparison requires cloning each and reading source, not just fetching landing pages.
- **Chinese ecosystem**: my scan biased toward English-language results. There are likely Qwen-specific tool wrappers in the Chinese-speaking developer ecosystem that wouldn't surface in English search queries.
- **The pace of change**: this landscape moves fast. A definitive "we're unique in X" claim could be invalidated by a competitor shipping next week. Treat the analysis as a snapshot.

---

## Sources

Direct-competitor reads (fetched 2026-04-24):
- [`nicedreamzapp/claude-code-local`](https://github.com/nicedreamzapp/claude-code-local) — 2.1k stars, primary competitor
- [`chand1012/claude-code-mlx-proxy`](https://github.com/chand1012/claude-code-mlx-proxy) — 40 stars, basic MLX proxy

Referenced but not deep-dived:
- [`waybarrios/vllm-mlx`](https://github.com/waybarrios/vllm-mlx) — vLLM + MLX server
- [`nielspeter/claude-code-proxy`](https://github.com/nielspeter/claude-code-proxy) — OpenRouter / OpenAI / Ollama proxy
- [`fuergaosi233/claude-code-proxy`](https://github.com/fuergaosi233/claude-code-proxy) — Anthropic → OpenAI translator
- [`musistudio/claude-code-router`](https://github.com/musistudio/claude-code-router) — transformer pipeline
- [`MadAppGang/claudish`](https://github.com/MadAppGang/claudish) — multi-provider CLI wrapper
- [`router-for-me/CLIProxyAPI`](https://github.com/router-for-me/CLIProxyAPI) — meta-proxy across AI CLIs
- [`decolua/9router`](https://github.com/decolua/9router) — mega-router
- [`drbarq/Claude-Connect`](https://github.com/drbarq/Claude-Connect) — universal Anthropic proxy
- [LM Studio × Claude Code](https://lmstudio.ai/blog/claudecode) — native Anthropic endpoint
- [Ollama × Claude Code docs](https://docs.ollama.com/integrations/claude-code) — native integration
- [LiteLLM Anthropic provider](https://docs.litellm.ai/docs/providers/anthropic) — gateway
