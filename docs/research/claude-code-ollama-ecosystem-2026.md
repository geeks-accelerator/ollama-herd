# Claude Code + Ollama in 2026 — Ecosystem State & Reliability Research

**Created**: 2026-04-22
**Status**: Research brief — grounded in 2026 community reports + upstream issue inspection
**Related**:
- [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md) — deep dive on the Qwen3-Coder optional-param / long-context tool-call bug (filed 2026-04-23)
- [`claude-code-local-models.md`](./claude-code-local-models.md) — model selection guide (what to map to which Claude tier)
- [`docs/issues.md`](../issues.md) — Ollama 3-model cap OPEN issue
- [`docs/plans/mlx-backend-for-large-models.md`](../plans/mlx-backend-for-large-models.md) — our MLX escape hatch
- [`docs/plans/hot-fleet-health-checks.md`](../plans/hot-fleet-health-checks.md) — observability plan

---

## TL;DR

**Claude Code + ollama-herd is the target. This doc is about making it work, not about alternatives.**

The bugs we hit running Claude Code against Ollama on a Mac Studio + MacBook Pro fleet are not anomalies — they are documented upstream issues in a 3-month-old integration. Our exact symptom (runner loads, `/api/chat` hangs, `/api/tags` still works) is [ollama/ollama#15258](https://github.com/ollama/ollama/issues/15258) — an open regression in Ollama 0.20.0 GA on Apple Silicon M4 with no root cause identified and no fix shipped. Multiple other failure modes we observed also correspond to open issues on `ollama/ollama` and `anthropics/claude-code`.

(Historical note: an automated Ollama watchdog (`src/fleet_manager/node/ollama_watchdog.py`) was shipped alongside this research as a workaround for the stuck-runner failure mode, but was **removed on 2026-04-23** after its probe-model selection logic caused a cascade-restart of `ollama serve` and silently evicted all pinned models. See `docs/issues.md` for the post-mortem.  The other mitigations the community recommends — `num_ctx ≥ 64K`, `"reasoning": false`, prefer qwen3-coder:30b — have held up without the watchdog.)

**Ecosystem context (not a pivot recommendation, just reality):** Claude Code + Ollama is the newest and least-mature local-agentic path, explicitly described in [Ollama's own docs](https://docs.ollama.com/integrations/claude-code) as shipped January 2026 with *"edge cases in streaming and tool calling still being patched."* Other tools (Cline, Aider, OpenCode) have more installs and more mature local-model integration paths — that's useful context for understanding why we hit bugs they don't, but our goal is to close the reliability gap **for Claude Code**, not route around it.

This doc exists to turn "we hit bugs" into a concrete engineering plan: which upstream fixes to push, what request-shaping to add to herd, what tests to write, and which Ollama version to pin until the regression is patched.

---

## Our reliability problems mapped to upstream reality

Every failure mode we hit tonight has an open, un-fixed issue on ollama/ollama or anthropics/claude-code. This is the single most important finding: **we're not doing anything weird — we're hitting known bugs.**

### Table of pain

| Symptom we observed | Upstream issue | Status |
|---|---|---|
| `/api/chat` hangs, runner process loaded and idle at 200-380% CPU, `/api/tags` still responsive | [ollama#15258](https://github.com/ollama/ollama/issues/15258) — Ollama 0.20.0 Apple Silicon M4 regression | **Open, no fix** |
| 500 after ~90s of processing with no visible error | [ollama#7526](https://github.com/ollama/ollama/issues/7526) — hard 2-minute timeout, mechanism unclear | Closed, mechanism still opaque |
| Models emit plain text `[Tool call: ...]` instead of structured `tool_use` blocks | [omlx#159](https://github.com/jundot/omlx/issues/159), [claude-code#51239](https://github.com/anthropics/claude-code/issues/51239) | **Open** |
| Claude Code hangs on startup when pointed at local Ollama | [claude-code#25412](https://github.com/anthropics/claude-code/issues/25412) — CLI stuck fetching MCP config from `api.anthropic.com` even with local backend | Closed as duplicate (still no workaround documented) |
| `/v1/messages/count_tokens?beta=true` causes Ollama to wedge | [ollama#13949](https://github.com/ollama/ollama/issues/13949) | **Open** |
| "Invalid tool parameters" loops during Plan Mode / file reading | [ollama#15390](https://github.com/ollama/ollama/issues/15390) | **Open** |

### The vindication of our specific diagnosis

[Issue #15258](https://github.com/ollama/ollama/issues/15258) reports our exact observation:

> *"The runner process spawns and loads models successfully but produces no output, consuming 200-380% CPU during the hang. Non-generative endpoints like `/api/embeddings`, `/api/version`, `/api/tags`, and `/api/pull` function normally."*

Our debug found the same: `ollama list` worked, `ollama ps` worked, but `/api/chat` hung. We spent ~40 minutes tracking this down; the upstream issue is a public record of the same behavior. The regression is specifically between Ollama 0.20.0-rc1 (works) and 0.20.0 GA (broken) on Apple Silicon M4.

**Workaround noted upstream**: revert to Ollama 0.19.0 or 0.20.0-rc1. Trade-off: loses Gemma 4 support.

---

## What the community actually uses

Claude Code + Ollama is not the default. Three tools have far more local-model adoption:

| Tool | GitHub stars | Claimed installs | Stance on local Ollama |
|---|---|---|---|
| **Cline** | — | **5 M** | *"Most popular open-source coding agent for local models"* — explicit Ollama support, VS Code-native, "approve everything" policy |
| **OpenCode** | **95 K** (has passed Claude Code in star count) | — | Terminal-native, 75+ LLM providers including local |
| **Aider** | 39 K | 4.1 M, ~15 B tokens/week | Git-native pair-programming, 100+ languages, 2+ years mature |
| Claude Code | — | — | **Newest, least-mature local path**; January 2026 integration |

Sources: [Top 5 CLI coding agents 2026](https://pinggy.io/blog/top_cli_based_ai_coding_agents/), [AIMultiple agentic CLI comparison](https://aimultiple.com/agentic-cli), [Tembo 2026 guide](https://www.tembo.io/blog/coding-cli-tools-comparison)

### Why Claude Code + Ollama is specifically painful

Three structural reasons, each documented:

**1. The integration is brand new.** Ollama's Anthropic-compat endpoint shipped with **Ollama 0.14.0 in January 2026**. That's 3 months of battle-testing vs 2+ years for the OpenAI-compat path at `/v1/chat/completions` that Cline and Aider use. Edge cases in the Anthropic envelope (streaming event sequence, `tool_use` blocks, `count_tokens` beta endpoint) have no shipped fixes yet. Ollama's own docs acknowledge this:

> *"Ollama's Anthropic API compatibility shipped in January 2026. Edge cases in streaming and tool calling are still being patched."*
>
> — [docs.ollama.com/integrations/claude-code](https://docs.ollama.com/integrations/claude-code)

**2. Claude Code assumes cloud-Claude latency budgets.** Every turn it sends the full 55-message conversation + 27 tools + `max_tokens=32000` because cloud Claude handles that shape in <5 s. Ollama on consumer hardware can't. The tools themselves aren't buggy — the *request shape* assumes infinite throughput and pile-up-tolerant queuing that local runners don't have.

**3. Non-streaming request bodies break more things.** Cline defaults to streaming, which exercises Ollama's most-tested code path. Claude Code sometimes sends `stream=False` with large `max_tokens`, which hits the code paths most likely to wedge (see #15258). Buffer-the-whole-response-before-returning semantics + large input + large output allocation + 2-minute hard timeout = our exact failure.

---

## Community-converged best practices (which we independently reproduced)

The widely-shared 2026 setup for local agentic coding:

| Practice | Community source | Our implementation |
|---|---|---|
| **Use `qwen3-coder:30b` (or `:30b-a3b`) as the daily coder** | [Remote OpenClaw Best Ollama Models 2026](https://www.remoteopenclaw.com/blog/best-ollama-models-for-openclaw), [aimadetools ranking](https://www.aimadetools.com/blog/best-ollama-models-coding-2026/) | ✅ `qwen3-coder:30b-agent` mapped to haiku/sonnet/opus |
| **Set `num_ctx ≥ 64K`** — agentic workflows eat context | [Remote OpenClaw: *"for agentic workflows with Cline, set local context to at least 64K because Cline is an agent workflow, not a lightweight chat tab"*](https://www.remoteopenclaw.com/blog/best-ollama-models-for-openclaw) | ✅ We use 128K after hitting the 40K overflow bug |
| **Avoid thinking models for tool-heavy workflows** | [haimaker.ai: *"set `\"reasoning\": false` in your model config; stick to Qwen3.5 models — they handle OpenClaw's tool-calling format more reliably than Mistral or older Llama models"*](https://haimaker.ai/blog/best-local-models-for-openclaw/) | ✅ `docs/guides/claude-code-integration.md` explicitly warns against `gpt-oss:120b`, `deepseek-r1:70b` for Claude tiers |
| **Use MoE with small active params on Apple Silicon** | Multiple sources note qwen3-coder:30b-a3b has 3.3B active → fits M-series bandwidth well | ✅ Same architecture — 3B active on M3 Ultra |
| **Pull 4-bit MLX builds when using `mlx-lm.server`** | [`mlx-community` HuggingFace org](https://huggingface.co/mlx-community) conventions | ✅ Pulled `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` |

We validated the consensus before we knew we were validating it. That's reassuring: our design choices aren't contrarian, they're convergent.

---

## What we built that isn't in the community yet

Two pieces of shipped work that I can't find elsewhere in public 2026 material:

### 1. ~~Ollama watchdog~~ (removed 2026-04-23)

The Ollama watchdog that originally lived at `src/fleet_manager/node/ollama_watchdog.py` was **removed** after its probe-model selection logic (pick the smallest loaded model for the chat probe) kept selecting embedding-only models like `nomic-embed-text`; `/api/chat` on an embed model returns HTTP 400, which the watchdog interpreted as "runner stuck" and cascade-escalated to a full `ollama serve` restart that wiped all pinned models.

Lessons for anyone considering rebuilding this:
- Never pick probe targets by size — maintain an **explicit allowlist** of chat-capable models.
- Reset escalation counters on **cause change**, not just elapsed time.
- Cap `ollama serve` restart attempts per hour hard — the blast radius (every pinned model evicted) is too big to trigger automatically on soft evidence.

See `docs/issues.md` → "Ollama watchdog cascade-restarted `ollama serve` and wiped pinned models" for the full post-mortem.

### 2. `mlx:` prefix routing to bypass Ollama's 3-model cap

The broader pattern — running `mlx-lm.server` alongside Ollama for extra model slots — exists in community scripts, but doing it **inside a router with automatic request shaping** is novel. We translate Anthropic Messages → OpenAI chat.completions, handle streaming event translation, and the Claude-Code-facing model map just references `mlx:foo` transparently.

See [`docs/plans/mlx-backend-for-large-models.md`](../plans/mlx-backend-for-large-models.md) for the architecture.

---

## Our position, plainly stated

We're doing something harder than most users:

1. Running **Claude Code** (the newest local-Ollama client, 3 months old)
2. Against **Ollama** on **Apple Silicon**, which has an open `/api/chat` hang regression in 0.20.0 GA (#15258)
3. With **large growing context** (55 messages + 27 tools + long sessions) that stresses the codepaths most likely to wedge
4. With a **brand-new integration path** (`/v1/messages` compat, Jan 2026) that's explicitly still being stabilized upstream

Each of those is survivable individually. All four stacked means *"everything that can go wrong, will."*

**The bugs are upstream. They are documented. They are mostly un-fixed. None of them are user error.**

That's the diagnosis. The strategic response is not "pick a different client" — ollama-herd exists specifically to make Claude Code work well against local fleets. The response is **close the reliability gap by shipping infrastructure that works around the upstream bugs until they're fixed, and push fixes upstream when we can.**

The watchdog we shipped today is exactly that pattern. Every remaining bug on the list has a similar defensive move available.

---

## Recommendations — making Claude Code + ollama-herd reliable

### Immediate (live now)

1. **Watchdog is shipped and running.** Will auto-heal the next `/api/chat` wedge in ~90 seconds instead of the 20+ minutes we spent debugging today. Currently logged as active on Neons-Mac-Studio.
2. **Pull paused.** 162 GB of the 480B model on disk, resumable via `uv run herd mlx pull ...` when we want to finish.

### Short term (this week)

1. **Pin Ollama version.** Revert to **Ollama 0.19.0** or **0.20.0-rc1** — both lack the #15258 `/api/chat` hang regression. Trade-off: lose Gemma 4 support (we don't use it). Document the pin in `CLAUDE.md` Gotchas. Until #15258 ships a fix, this alone eliminates the most common failure mode.

2. **Request-shaping protection in the Anthropic route.** Claude Code's default request shape (stream=False, max_tokens=32000, 27 tools) is the exact pattern that triggers Ollama's stuck-runner state. Add to `server/routes/anthropic_compat.py`:
   - **Force `stream=True` on the Ollama side** even when the client sent `stream=False`. Herd already buffers streaming responses into non-streaming replies if needed. This sidesteps Ollama's non-streaming codepath (which is where most hangs happen).
   - **Cap `max_tokens` at `FLEET_ANTHROPIC_DEFAULT_MAX_TOKENS`** (already 4096) instead of passing through Claude Code's 32000. Reduces pre-allocated state in Ollama's runner.
   - **Filter unknown `/v1/messages/*` subpaths** — explicitly 404 `count_tokens?beta=true` instead of letting Ollama choke ([#13949](https://github.com/ollama/ollama/issues/13949)).

3. **Workaround the Claude Code startup hang ([#25412](https://github.com/anthropics/claude-code/issues/25412)).** Claude Code fetches MCP server config from `api.anthropic.com` even when pointed at a local backend. Options:
   - Document the exact env var or `settings.json` option that disables the MCP fetch
   - Have herd serve a stub response at `/v1/mcp_servers` that returns `{"data": []}` so Claude Code gets a quick reply and moves on

4. **File the watchdog pattern upstream.** Comment on [#15258](https://github.com/ollama/ollama/issues/15258) and [#7526](https://github.com/ollama/ollama/issues/7526) with our implementation. Precedent from the mlx-lm PR experience: even if the fix doesn't land immediately, our data + implementation become reference material.

5. **Add Claude Code integration tests.** Build a test that sends the exact shape we saw brother send (55 messages, 27 tools, stream=False, max_tokens=32000) through `/v1/messages` and asserts the response completes or fails cleanly. Without this test, future regressions that break the shape will only be caught in production.

### Medium term (next month)

1. **Write the post: "Running Claude Code locally on ollama-herd."** Target audience: every developer who tried Claude Code + Ollama and hit the same bugs we did. Our watchdog + MLX escape hatch + multi-node routing + request-shaping protection is genuinely the most robust setup published. The post legitimizes ollama-herd as the recommended way to run Claude Code against local models, not just a generic router.

2. **Ship the hot-fleet health checks** from `docs/plans/hot-fleet-health-checks.md`. `fallback_rate`, `mapped_model_missing/cold`, `model_eviction_churn` — all directly catch Claude Code failure modes before they reach the user.

3. **Claude Code user experience improvements via herd:**
   - `X-Fleet-Claude-Code-Mode` header or similar that enables client-aware optimizations
   - Per-tool-schema caching so the 27-tool request doesn't re-parse every turn
   - Prefix-hash cache in the streaming proxy (see plan doc) — would dramatically reduce prefill time on long Claude Code sessions

4. **Track upstream Ollama fixes closely.** When #15258 is fixed, test immediately, unpin. When the Anthropic-compat surface stabilizes, remove request-shaping workarounds. We want to be the first downstream project to validate upstream fixes.

### Long term (ongoing)

1. **ollama-herd becomes the recommended local backend for Claude Code.** Not by accident — by being the one place where all the workarounds are applied, all the upstream bugs are tracked, all the best practices are baked in. A user who runs `herd` + `herd-node` + points Claude Code at it gets a setup that "just works" because herd is doing all the papering-over.

2. **Upstream contributions where we can.** The mlx-lm comment pattern (attach real-world benchmark data to open PRs) worked once; do it again for the Ollama bugs we hit. Specifically worth the effort: a fix or minimal reproducer for #15258 on Apple Silicon M4.

---

## Sources

### Upstream Ollama issues (all verified live in 2026)

- [#15258 — 0.20.0 Apple Silicon M4 `/v1/chat/completions` hangs indefinitely](https://github.com/ollama/ollama/issues/15258) ← **our exact bug**
- [#15390 — Claude Code + Ollama invalid tool parameters + CPU fallback](https://github.com/ollama/ollama/issues/15390)
- [#13949 — Ollama API compatibility issue with Claude Code / Anthropic CLI](https://github.com/ollama/ollama/issues/13949)
- [#7526 — 500 error after LLM computation exceeds 2 minutes](https://github.com/ollama/ollama/issues/7526)
- [#11721 — 500: llama runner terminated, exit status 2](https://github.com/ollama/ollama/issues/11721)
- [#5892 — 500 errors on larger models](https://github.com/ollama/ollama/issues/5892)

### Claude Code issues

- [#25412 — CLI hangs on startup with Ollama (Anthropic Messages API)](https://github.com/anthropics/claude-code/issues/25412)
- [#51239 — hangs with local Ollama on trivial prompt](https://github.com/anthropics/claude-code/issues/51239)
- [#19564 — add Ollama integration guide to third-party docs](https://github.com/anthropics/claude-code/issues/19564)

### Community workarounds / shims

- [hilyin/ollama-anthropic-shim](https://github.com/hilyin/ollama-anthropic-shim) — a community-written Anthropic Messages API shim specifically because Ollama's built-in compat has bugs
- [AUAggy's running-Claude-Code-locally gist](https://gist.github.com/AUAggy/ccf6df83c297e76191ff2de8eb6a5168)
- [omlx#159 — Claude Code emits literal `[Tool call: ...]` text](https://github.com/jundot/omlx/issues/159)

### Ecosystem landscape

- [Ollama Claude Code integration docs](https://docs.ollama.com/integrations/claude-code) ← acknowledges *"edge cases still being patched"*
- [AIMultiple: agentic CLI comparison 2026](https://aimultiple.com/agentic-cli) — Claude Code ranks #3 on accuracy, Cline among lowest
- [Pinggy: top 5 CLI coding agents 2026](https://pinggy.io/blog/top_cli_based_ai_coding_agents/)
- [Morph: 15 AI coding agents tested 2026](https://www.morphllm.com/ai-coding-agent)
- [Tembo: 2026 guide to coding CLI tools](https://www.tembo.io/blog/coding-cli-tools-comparison)

### Model recommendations for agentic work

- [Remote OpenClaw: Best Ollama Models for OpenClaw 2026](https://www.remoteopenclaw.com/blog/best-ollama-models-for-openclaw) — `num_ctx ≥ 64K`, qwen3-coder:30b
- [haimaker.ai: local models for OpenClaw tested](https://haimaker.ai/blog/best-local-models-for-openclaw/) — `"reasoning": false` for tool calling
- [aimadetools: best Ollama models for coding 2026](https://www.aimadetools.com/blog/best-ollama-models-coding-2026/)
- [localaimaster.com: 10 models tested and ranked for coding 2026](https://localaimaster.com/blog/best-local-ai-models-programming)

### Claude Code + Ollama integration reports

- [XDA Developers: I used Claude Code with a local LLM on Ollama](https://www.xda-developers.com/claude-code-local-llm-ollama-capable-costs-nothing/)
- [DataCamp: Using Claude Code With Ollama Local Models](https://www.datacamp.com/tutorial/using-claude-code-with-ollama-local-models)
- [My Developer Planet: Setting Up Claude Code with Ollama](https://mydeveloperplanet.com/2026/03/18/setting-up-claude-code-with-ollama-a-guide/)
- [Luong NGUYEN: How to run Claude Code with local models via Llamacpp, Ollama, LMStudio, vLLM — 2026](https://medium.com/@luongnv89/how-to-run-claude-code-codex-with-local-models-via-llamacpp-ollama-lmstudio-and-vllm-2026-7d00ba7e63a4)

---

## Honest takeaway

The Claude Code + Ollama experience is rough for everyone in April 2026 — the integration is 3 months old, there are multiple open upstream bugs, and we sit on the bleeding edge with more instrumentation than most users have (which is exactly why we saw the bugs clearly). We independently converged on every community best practice and shipped two novel pieces of infrastructure (watchdog + MLX escape hatch) that plausibly belong as upstream contributions.

**ollama-herd's job is to make this setup reliable.** Every bug listed here has a defensive workaround available — some already shipped (watchdog), some on the concrete roadmap above (request-shaping, MCP stub, version pin). We're not waiting for upstream to get perfect; we're shipping the infrastructure that makes Claude Code + local Ollama work *today*.

The reliability gap is closeable, the roadmap is concrete, and we're the project that's going to close it.
