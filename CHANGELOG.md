# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Release status:** `0.9.2` is the current release on PyPI and git tags — a fixes-and-hardening patch on `0.9.0`. `0.9.0` was the first release since **`0.7.0`**; the `0.8.x` milestones below were never published separately and ship inside it. Those dated `0.8.x` headers mark when each milestone was cut on `main`, not a PyPI release.

## [Unreleased]

## [0.9.2] - 2026-08-16

### Added

- **Anonymous community telemetry, on by default.** The router sends one summary a day: which models ran, request and token counts, latency percentiles, and error counts by category. Never prompts, never completions, never raw error text, never your hostname. Opt out with **`FLEET_NODE_TELEMETRY=false`**, or from the dashboard's new Community Telemetry section. A one-time notice is printed the first time `herd-node` starts, before anything is ever sent, and the opt-out is honoured on that first run — a node that has opted out writes no files at all, not even the identifier. Every field that can be sent is published at [ollamaherd.com/telemetry](https://ollamaherd.com/telemetry); that page is the contract, and the payload builder mirrors it under tests that fail the build if the two drift.

  Identity is a random `install_id` UUID stored in `~/.fleet-manager/install_id` — delete it and you are a new install, delete it and opt out and you are gone. It is never derived from anything about the machine; tests assert it is neither the hostname in any form nor a hash of it, so it cannot be turned into a stable fingerprint later.

  Reporting is **per herd, not per machine**. The router is the only component that knows a fleet is one fleet, so it sends one payload containing a `devices[]` row per node (chip, memory, cores, agent and Ollama version, that node's request share). Fleet totals are derived from those rows server-side rather than sent as scalars, so a total can never disagree with its parts. Each device carries a `device_id` derived from its `node_id` but hashed and salted with the herd's `install_id`: stable enough to aggregate across days, and impossible to reverse to a hostname or correlate across herds.

- **Name your herd for the public leaderboard** — a second, separate opt-in on top of telemetry. Set it in the dashboard or via `FLEET_NODE_HERD_NICKNAME`. Telemetry alone is never public; a nickname is the only field that is, and the UI says so before you set one. Names are limited to 30 characters of letters, numbers, spaces, `-`, `_`, `.`, validated where they are typed rather than only in the browser.

- **`X-Fleet-Affinity` makes session routing visible.** Every scored endpoint now reports `matched` when a request went back to the node already holding that conversation, or `new` when it did not — and omits the header entirely on routes that never score, because "did not apply" and "missed" are different facts.

  It reports the **routing decision, not a cache hit**, and deliberately so: Ollama folds llama.cpp's `cache_n` back into `prompt_n` ([ollama/ollama#16428](https://github.com/ollama/ollama/pull/16428)), so `prompt_eval_count` cannot yield a hit ratio and inferring one from it produced a false "zero prefix-cache reuse" report here once already. Time-to-first-token is the honest proof: a matched turn shows a large drop.

- **`usage.prompt_tokens_details.cached_tokens`** is reported on backends that actually measure it (MLX), and **omitted — never zeroed — on backends that cannot** (Ollama). Zero means "measured, nothing reused"; absent means "cannot measure". Conflating them is a bug vLLM shipped and SGLang still has.

- **The Ollama and mlx-lm versions are collected in the heartbeat** and surfaced to the router, so a fleet can answer "which runtimes are actually out there?" before a version-gated model or a changed default lands.

### Changed

- **Session affinity now decays with queue depth.** A pinned node contributes the full bonus when idle and progressively less as its queue grows, so a warm but saturated node stops winning while an idle peer sits free. Every production router does this — NVIDIA Dynamo, Ray Serve, SGLang — and a flat bonus is the bug vLLM's production-stack shipped `loadaware` routing to fix. Signal 3 already prices congestion, so this shrinks the *bonus* rather than adding a second penalty, and because decay can only shrink, affinity still never outweighs a thermal warning.

- **MLX prompt cache raised from 4 to 10**, matching `mlx_lm.server`'s own default. The previous value had no recorded rationale and sat *below* upstream, silently halving how many conversations could keep their KV cache warm — which is the actual limit on how many sessions affinity can honour.

### Fixed

- **Telemetry now sends a missed day on startup instead of sleeping past it.** The scheduler slept until the next 00:05 UTC before its first send, so *start time of day* decided whether an install reported at all: a router started at 00:10 UTC waited 23h55m, and one restarted daily after 00:05 never sent. Both are indistinguishable from "nobody uses this" on the receiving end. Found because our own fleet ran 12 hours of clean uptime with zero automatic sends.

- **The published telemetry opt-out worked in name only.** Moving the sender from the node to the router silently repointed which environment variables it read (`ServerSettings` carries a different prefix), so `FLEET_NODE_TELEMETRY=false` — the opt-out documented on the website — stopped disabling anything, with no error anywhere, because "off" and "unset" are indistinguishable in a boolean default. Both that and `FLEET_NODE_HERD_NICKNAME` are now read correctly, with tests pinning the names as a published contract.

- **Dashboard toggles now survive a restart.** `POST /dashboard/api/settings` only mutated settings in memory, so every Feature Toggle silently reverted on the next start. They are now written to `~/.fleet-manager/env` with a line-based writer that preserves the comments and hand edits in that file, and the response reports `not_persisted` / `restart_required` instead of implying an effect it cannot deliver. Merely annoying for `auto_pull`; it would have been a broken promise for a telemetry opt-out.

- **The gotomy.ai platform panel is removed from Settings** — that service is shut down.

### Removed

- The account-based platform telemetry opt-ins are untouched, but the dashboard's platform-connection UI is gone along with the service it targeted.

## [0.9.1] - 2026-07-30

A fixes-and-hardening release on top of `0.9.0` — no breaking changes, no new dependencies. Three reliability bugs found while operating the local fleet, plus a new routing signal and two health checks that came out of the same soak.

### Added

- **Session affinity — an 8th scoring signal.** A conversation is now pinned to the node already holding its warm prefix cache, so turn N+1 reuses the processed prompt (llama.cpp's `get_common_prefix` skips already-seen tokens) instead of re-prefilling ~30K tokens on a fresh node — which also removes the decode interference that a cold prefill inflicts on any stream co-resident on the new node. Keyed by an explicit `session:<id>` request tag when present, else `client_ip|model`; TTL 900s. The bonus (20) sits below thermal (50) so a hot node never wins on affinity alone. See `docs/fleet-manager-routing-engine.md` § Signal 8.
- **`decode_degraded` health check (#39).** Watches per-model TPOT — `(latency − time_to_first_token) / (completion_tokens − 1)` — against each model's own rolling baseline, so it isolates genuine decode contention from queueing and prefill. Fires WARNING when decode slows against a node's own history rather than a fleet-wide constant.
- **`pin_cannot_fit` health check (#40).** Surfaces a pinned model that can never load on its node because the pinned set can't physically co-reside, instead of letting the preloader retry it forever.

### Changed

- **Queue concurrency is capped at what the backend will actually decode.** Per-`node:model` concurrency is now `min(memory_slots, OLLAMA_NUM_PARALLEL)` — Ollama admits only `num_parallel` requests into decode at once, so provisioning more queue workers than that just built a second queue in front of the real one. Nodes report `num_parallel` in the heartbeat; the router mirrors it the same way it already mirrors the hot-model cap.

### Fixed

- **`FLEET_NODE_NODE_ID` now actually sets the node id.** `herd-node` passed its empty `--node-id` default *explicitly* into `NodeSettings`, and an explicit kwarg shadows pydantic-settings' env lookup — so the env var was inert and the node fell back to `socket.gethostname()`. On macOS the hostname is network-derived when the static `HostName` is unset, so it silently changed between networks (`bb` at home → `Neons-Mac-Studio` travelling) and orphaned every pin bound to the old id. The same shadowing affected `FLEET_NODE_ROUTER_URL`; both are fixed.
- **Image requests carrying `image` / `input_image` content parts no longer fall back to a blind text model.** The image-detection guard only recognised `image_url`; requests using the other two shapes slipped past it, routed to `gpt-oss`, and 400'd — while the guard built to prevent exactly that sat inert. The detector now matches the full superset the runtimes accept and skips non-dict messages.
- **A stale dashboard tab can no longer wedge the router.** The `/dashboard/events` SSE loop now checks `request.is_disconnected()` each iteration and wraps the stream so an exception is logged and returned rather than escaping, while `CancelledError` still propagates — a disconnected tab stops generating work instead of accumulating it on the event loop.

## [0.9.0] - 2026-07-19

**The first release since `0.7.0`, and it contains breaking changes.** Nothing between `0.7.0` and this was ever published, so upgrading from `0.7.0` lands **all** of `0.8.0`, `0.8.1`, and `0.8.2` at once — see those sections below for the full detail. This section is the upgrade guide: what breaks, and what's new enough to matter.

Versioned `0.9.0` rather than `0.8.2` deliberately: a `0.7.0 → 0.8.2` jump reads like "two patches on a release I already have," which would invite a casual deploy straight into removed env vars and a retired header. Under 0.x SemVer, MINOR is where breaking changes belong.

### ⚠️ Breaking — read before upgrading from 0.7.0

- **Legacy single-server MLX env vars are gone.** `FLEET_NODE_MLX_AUTO_START`, `FLEET_NODE_MLX_AUTO_START_MODEL`, `FLEET_NODE_MLX_URL`, `FLEET_NODE_MLX_KV_BITS`, `FLEET_NODE_MLX_PROMPT_CACHE_SIZE/BYTES`, `FLEET_NODE_MLX_DRAFT_MODEL`, `FLEET_NODE_MLX_NUM_DRAFT_TOKENS`. **Migrate to a one-entry `FLEET_NODE_MLX_SERVERS` array.** (`FLEET_MLX_ENABLED` and the server-side `FLEET_MLX_URL` fallback remain; the `herd`/`herd-node` CLI, `FLEET_*` internals and `~/.fleet-manager/` are unchanged.)
- **`X-Fleet-Model` is retired** in favour of `X-Fleet-Served-Model`. `X-Fleet-Fallback` also changed meaning: it is now an always-present `"true"`/`"false"` boolean, not a model name emitted only on substitution.
- **Queue-full is now `429`, not `503`.** Ollama's "maximum pending requests exceeded" is no longer retried (retrying a saturated node amplified the flood).
- **`POST /fleet/pin` can now refuse.** `409` when the pinned set can't physically co-reside (`"force": true` overrides), `400` for an unknown `node_id`. Previously every pin was accepted silently — which is how a 307 GB pinned set on a 512 GB box produced hours of thrash.
- **Image requests can now fail instead of silently succeeding.** A request carrying images will no longer fall back to a model that can't see them; it fails loudly rather than returning a confident answer about an image the model never received.
- **Backend client errors surface as themselves.** A 4xx from Ollama (e.g. "model does not support tools") now reaches you as that 4xx with the backend's message, instead of an opaque `500`.
- **The built-in default `anthropic_model_map` is now empty.** It previously hard-coded `qwen3-coder:30b` / `qwen3:32b` / `qwen3:14b` — model names a given deployment may never have pulled. Deployments that relied on that built-in default (never set `FLEET_ANTHROPIC_MODEL_MAP`) now get **auto-routing** instead (best loaded model per tier), which is strictly more likely to resolve to something they actually have. Explicit maps set via env are unaffected. See the auto-routing feature below.

### Headline features

- **OpenAI Codex support — a native Responses API at `/v1/responses`.** Codex removed Chat Completions in Feb 2026 (`wire_api = "chat"` is gone), so this is the only endpoint current Codex can speak. Agentic coding is **verified end-to-end** against a real `codex-cli 0.145.0-alpha.18`: it ran pytest, read sources, created a module from scratch, patched files via `apply_patch`, and reached green on its own. Zero configuration — a `gpt-5-codex`/`gpt-5.6-sol` id auto-routes to the best coding model you have loaded, the same resolver Claude Code uses. See `docs/guides/codex-integration.md`.

  Getting there meant bridging a gap that only appears with local models: **Codex's tool *descriptions* document an API its tool *schema* doesn't expose**, and local models call what the prose names. Herd now normalises three cases automatically, each logged at WARNING:
  - Tools hidden inside an `additional_tools` input item (the `sol`/`terra`/`luna` "Responses-Lite" slugs, including the ChatGPT Desktop default) are extracted, and the grammar-constrained `custom` `exec` tool is bridged to something Ollama function-calling can express. Without this the model has nothing callable and rationalises the failure — [openai/codex#31894](https://github.com/openai/codex/issues/31894).
  - A top-level `exec_command` call — a *nested* tool only reachable from inside code-mode JavaScript — is rewritten as a `custom_tool_call` on its host tool. Passing it through makes Codex stop with no error displayed.
  - An `apply_patch` **tool** call is rewritten as an `exec_command` heredoc. `apply_patch` is a binary Codex injects on the sandbox PATH, not a tool; calling it as one returns `unsupported call: apply_patch`, and the session can then read and execute but never write.

- **Images route to a model that can see them, on every endpoint.** An image-bearing request auto-selects a vision-capable model even when the conversation's model is a code-tuned one, and image content now reaches Ollama as its `images` list instead of being silently dropped in translation. A dropped image is worse than a dropped tool call: it produces a fluent, specific, wrong answer while every server-side metric reports success.

- **Distributed MLX inference** — run one model across multiple Macs via `mlx.launch` (`ring` over LAN today; `jaccl`/Thunderbolt 5 targeted). The herd sees one endpoint whether one Mac or four are behind it.
- **Fleet control API** — `GET /fleet/limits`, `POST /fleet/pin` (with `wait` for readiness), `DELETE /fleet/pin/{model}`.
- **`mlx:` models reachable over the OpenAI endpoint**, not just Anthropic — OpenAI-only clients get the fast backend instead of a slow fallback.
- **Canonical `X-Fleet-*` headers** on every proxied response, so a caller can always tell what actually ran.
- **Per-request strict mode** (`X-Fleet-No-Fallback`) and a **per-client concurrency cap** (`FLEET_CLIENT_MAX_IN_FLIGHT`, default off).
- **`FLEET_ANTHROPIC_MODEL_MAP` is now optional — Claude Code works with zero configuration.** A `claude-*` id with no explicit mapping is resolved to the best *currently-loaded* local model for its tier (coding models preferred for Claude Code's workload; a loaded vision model chosen automatically for image requests), falling back to the best on-disk model, then a configured `default`. So a fresh install routes to whatever the user pulled — no hand-written map that has to match your downloads, and no map entry silently pointing at a model you never pulled. Explicit map entries still win as per-alias overrides; set `FLEET_ANTHROPIC_AUTO_ROUTE=false` to require an explicit map (the pre-0.9 behaviour). See `docs/reference/anthropic-auto-routing.md`.

### Notable fixes

- **`finish_reason` is recorded on every trace**, so a turn that ends mid-task is distinguishable from one that exhausted its token budget without eyeballing `completion_tokens`.
- **A `num_ctx` override that cannot apply now says so, once, instead of logging like it worked.** `FLEET_NUM_CTX_OVERRIDES` sets the context a *cold* load comes up with; it cannot shrink a resident model without forcing an unload/reload. Previously it logged an "injected" line and a "stripped" line per request — 393 pairs in nine hours — which read as a working feature doing nothing.
- **OpenAI function calling was dropped in both directions** on `/v1/chat/completions`; tool calls now survive the round trip.
- **`/v1/models` emits the schema Codex actually decodes.** Codex validates against its own undocumented, strictly-typed schema and fails the *whole* decode on the first problem — `visibility: "public"` alone (not in its `list`/`hide`/`none` enum) emptied the model picker, which pushes ChatGPT Desktop onto its Lite slugs. `supports_vision` is now reported per model rather than hardcoded `false`. The field set is not converged; see `docs/issues.md`.
- **Image requests are never answered by a blind model** (the 2026-04-23 incident class — see below).
- **Failed-request traces no longer vanish**, so the dashboard's success rate stops hiding failures.
- **Model sizes come from Ollama's real `/api/tags` data** instead of being guessed from the name — the guess called a 290 GB model "10 GB" and defeated the memory gate.
- **Models are now sized by what they actually cost in RAM — weights *plus* KV cache.** Every "will this fit?" decision previously counted on-disk weights and ignored the KV cache, which scales with context and routinely dwarfs the weights: `qwen3-coder:30b` is 18.6 GB of weights and **122.9 GB resident** at its default 262K context, so the gate was under-counting it by **5.4×**. The router now learns each model's KV cost per token from heartbeat data it was already receiving (`(resident − weights) / context_length`) and predicts the real footprint at the context the model will actually run with. The preloader gate, `/fleet/pin` admission and the scorer all share one estimator. Models the fleet has never observed keep their previous sizing — evidence tightens these gates, guesswork doesn't. See `docs/issues/model-sizing-ignores-kv-cache.md`.
- **Preloading warms a model at the same `num_ctx` requests will use.** It previously warmed at the model's default and let the first real request reload it at the override — pointless churn, and it made the model's cost unknowable at load time.
- **The hot-model cap is no longer hardcoded to 3** — nodes report their own, and `free_slots` follows it.

### Recommended alongside this release

**Upgrade Ollama to `0.32.1`.** Not required, but measured on the same hardware: `glm-4.7-flash` **13.7 → 77.8 tok/s** (the `glm4moelite` expert-offload bug is fixed upstream), `gpt-oss:120b` 50.9 → 74.5, and prefix caching demonstrably works. See `docs/plans/ollama-0.32-upgrade-and-mlx-evaluation.md`.

---

## [0.8.2] - Unreleased

Observability + reliability fixes surfaced by an 8-hour soak review, plus two issue-diagnosis corrections (see `docs/issues.md`).

### Fixed

- **Image requests can no longer be answered by a model that cannot see them.** The VRAM fallback substituted *any* loaded model when the requested one was cold — including swapping a vision model for a text model on a request carrying actual images. The text model then answers confidently about content it was never shown, which is worse than an error because nothing downstream can detect it. This is the 2026-04-23 incident (20 `gemma3:27b` vision requests silently answered by `gpt-oss:120b`, images dropped) — and the risk had been *logged as a QUALITY RISK for months* while `InferenceRequest.has_images`, auto-computed on every request, went unread by the fallback decision. Cross-category fallback is now **hard-blocked** when `has_images` is set, so the router falls through to its normal path and loads the correct model. A vision-*model* request with no images still falls back (survivable — deliberately not over-blocked). Verified live: an image request bound for `llama4:maverick` was blocked, waited for `gemma3:4b`, and answered correctly.
- **`/fleet/pin` no longer claims a resident model "is not on disk".** A pin for `gpt-oss:120b` was refused with *"not on disk — run `ollama pull`"* while the model was loaded (70.96 GB) and serving. Two compounding bugs: (1) `_load_model_on_best_node` returns a bare `False` for *not-on-disk*, *memory-gate refusal*, and *pre_warm error* alike, and the route hardcoded the not-on-disk message for all three; (2) the memory gate ran against an **already-resident** model — gpt-oss:120b needs `72 × 1.2 = 86.4 GB` free, but its own ~71 GB footprint is subtracted from free memory, so the gate saw 49 GB and refused to load a model already in memory. Pinning a hot model could fail *because it was hot*, intermittently, as free memory fluctuated. Now: the gate is skipped when the model is already resident on the chosen node (`pre_warm` still runs so `keep_alive=-1` is re-applied), and the route checks on-disk explicitly — genuine not-on-disk → `404` with the pull hint, on-disk-but-wouldn't-load → `503` naming insufficient memory. See `docs/issues.md`.
- **Pin admission control + honest hot-model cap.** `POST /fleet/pin` now refuses (`409`) a pin whose set can't physically co-reside on the node, showing the arithmetic and what's already pinned (`"force": true` overrides); and it rejects (`400`) an unknown `node_id` instead of silently persisting a pin that rots forever. Pins previously bypassed every safety net — the preloader reloads them unconditionally ("no recency check — user pinned it") and nothing asked whether the pinned *set* fits. A client agent benchmarking models pinned each one **exactly as our API docs instructed** and never unpinned; the set reached 307 GB (incl. a 290 GB model) on a 512 GB box and the preloader spent hours evicting and reloading them in a loop. The docs now say, loudly, to unpin when done. Separately, the **hot-model cap is no longer hardcoded to 3**: nodes report their own `OLLAMA_MAX_LOADED_MODELS` and `free_slots` follows it. The long-documented "macOS Ollama hardcodes a 3-model cap regardless of config" claim was **disproven on Ollama 0.32.1** — with the var set to 10 we observed 4 concurrent residents, meaning the herd had been throttling the fleet and reporting `free_slots` from a fiction.
- **Failed-request traces no longer vanish before they persist.** `_create_logged_task` scheduled fire-and-forget writes (traces, latency records, client closes) with `asyncio.create_task` but kept **no strong reference** — and asyncio only *weakly* references tasks, so one could be garbage-collected before it ran. Completed-request traces survived (the route keeps `await`-ing afterward, so the loop runs the task); but the error path records a trace and `raise`s on the very next line with no further `await`, so the weakly-referenced write was collected before persisting. Net effect: the dashboard's success rate was computed over *completed* traffic only and was **blind to failures** — an 8-hour window logged 211 Ollama `503`s with **0** trace records. The helper now holds each task in a module-level set until its done-callback fires. Additionally, a request that exhausts every retry now records a terminal `failed` trace instead of leaving only per-attempt `retried` rows. See `docs/issues.md` → "Failed-request traces get garbage-collected before they persist."

### Changed

- **Corrected the MLX idle-server-churn diagnosis** (docs + a misleading comment; no behavior change). An earlier issue draft blamed a supervisor "false-positive health kill" and proposed a health-poll debounce. On inspection that mechanism doesn't exist: the supervisor **never restarts a running-but-unhealthy server** — `_monitor` acts only on an actual process exit, and `poll_health`/`refresh_health` only update the status string for the dashboard. The `rc=-9` kills of an idle ~35 GB MLX server under load came from **outside** the supervisor; the signature (selective — only the idle server died while its busy siblings survived — silent, clustered in load peaks) points at **OS memory pressure**, not a health check. No code fix applies; the mitigation is operational — don't keep an unused large model resident (Qwen3-Coder-Next was dropped from `FLEET_NODE_MLX_SERVERS`). The misleading `poll_health` "monitor will restart" comment was corrected.

## [0.8.1] - 2026-07-15

Client ergonomics from a benchmark agent's feedback — make the herd's routing
behavior legible and controllable to callers (see
`docs/plans/client-ergonomics-from-agent-feedback.md`). Production defaults are
unchanged: fallback and retry stay ON, per-client cap defaults to unlimited;
the new controls are opt-in per request / per operator.

### Added

- **Per-request strict mode.** Send `X-Fleet-No-Fallback: true` (or `"fallback": false` in the body) and the herd serves the *exact* model requested or returns an explicit error — no silent same-category substitution. Overrides the global `FLEET_VRAM_FALLBACK` for that one call, so a benchmark can demand determinism without changing behavior for every other client.
- **`GET /fleet/limits`.** Reports effective serving constraints — per-node hot-model cap + free slots, the router's retry budget, MLX in-flight caps — so a client can auto-serialize instead of self-DoSing a saturated box.
- **`POST /fleet/pin` / `DELETE /fleet/pin/{model}`.** One-call model pinning: pre-warm a model resident (evicting LRU as needed) and persist the pin so it's reloaded if evicted; delete to release. Reuses the existing `PinnedModelsStore` + preloader. Replaces the manual `curl :11434 keep_alive` dance. **`{"wait": true, "timeout_s": 30}`** blocks until the router's routing view confirms the model resident (returning `ready` + `ready_after_ms`) so a caller that pins-then-scans doesn't race the heartbeat into fallback substitution — the server-side version of the readiness loop every client would otherwise build. `mlx:` models short-circuit as a no-op (always resident; reports readiness from `mlx_servers` health, never warms via Ollama). Also fixes a latent persistence race: when `node_id` was omitted and the heartbeat hadn't landed, the pin silently wasn't persisted. See `docs/plans/fleet-pin-readiness.md`.
- **Per-client concurrency cap.** `FLEET_CLIENT_MAX_IN_FLIGHT` (default `0` = unlimited) caps how many requests one client (by IP) may have in flight across all queues; excess is shed with `429` + `Retry-After` (`FLEET_CLIENT_CONCURRENCY_RETRY_AFTER`, default `2`s) instead of piling onto the queue, so one unthrottled caller can't monopolize the box or starve other clients.
- **`mlx:` models reachable via the OpenAI endpoint.** `POST /v1/chat/completions` now routes `mlx:`-prefixed models straight to `mlx_lm.server` (a clean passthrough — it's OpenAI-native — with the same admission control, queue-full 503s, and trace recording as the Anthropic path). Previously only `/v1/messages` (Anthropic) could reach the MLX backend, so any OpenAI-only client (or one that supports just openai-compatible + ollama providers) had its `mlx:` request fall through to a slow Ollama fallback — e.g. GLM-4.7-Flash at ~13 tok/s instead of the ~59 tok/s MLX path. `/v1/models` now also advertises healthy `mlx:` models so clients that validate against the model list can discover them.

### Changed

- **Canonical `X-Fleet-*` response headers.** Every proxied response now emits the same set via a shared `fleet_headers()` builder (was 6+ divergent copy-pasted blocks): `X-Fleet-Served-Model` (what actually ran) + `X-Fleet-Requested-Model` (what was asked) + `X-Fleet-Fallback` (a real always-present boolean — previously a model *name* set only when a fallback happened) + `X-Fleet-Node` / `X-Fleet-Backend` / `X-Fleet-Retries`, and `X-Fleet-Score` when scorer-routed. A client reads three headers on **any** endpoint and knows exactly what ran. **`X-Fleet-Model` is retired** in favor of `X-Fleet-Served-Model`.
- **`/fleet/status` node fields** now include `models_loaded_count` and `free_slots`, via a shared `serialize_node()` serializer (replaces hand-built duplication).

### Fixed

- **Queue-full 503 no longer amplifies floods.** Ollama's `"maximum pending requests exceeded"` 503 is now classified non-retryable — retrying a saturated node just piled more load on it (2026-07-15: a benchmark's requests + 3× retries produced 156 503s in 5 minutes). Other 5xx are still retried.
- **MLX reasoning-model output no longer dropped.** Reasoning models served via `mlx_lm.server` (GLM-4.7-Flash / `glm4_moe_lite`) stream their chain-of-thought in a separate `reasoning` / `reasoning_content` field and keep `content` empty until thinking ends. The MLX→Anthropic translation (and the non-streaming stream-and-collapse accumulator `_collect_openai_stream`) only ever read `content`, so a GLM request came back **empty**. Reasoning is now surfaced — an Anthropic `thinking` block on the Anthropic route, coalesced into the visible text stream on streaming, and preserved as the `reasoning` field on the OpenAI passthrough.
- **OpenAI `max_tokens` no longer dropped on the MLX path.** `_to_openai_body` assumed an Ollama-shaped body (generation params under `options`); OpenAI bodies carry them at the top level, so `max_tokens` was silently lost when an OpenAI request hit an `mlx:` model — fatal for reasoning models, which never finish thinking without a large budget. The builder is now `original_format`-aware.

## [0.8.0] - 2026-07-14

Distributed MLX inference — run one model across multiple Macs — plus a consolidation of the MLX config surface down to a single `FLEET_NODE_MLX_SERVERS` array. A `mlx_lm.server` can now be wrapped in `mlx.launch` so rank 0 serves HTTP while peer ranks hold shards; the herd sees one endpoint whether one Mac or four are behind it. The `ring` backend works over plain LAN today (memory pooling via pipeline parallelism); the `jaccl` backend targets RDMA over Thunderbolt 5 for tensor-parallel speedup. This release also removes the legacy single-server MLX env vars (see **Removed** — breaking) and fixes a latent bug where a `0.0.0.0` bind made health checks dial `http://0.0.0.0`.

### Added

- **Distributed MLX inference.** `FLEET_NODE_MLX_SERVERS` entries gain four optional keys: `backend` (`ring`\|`jaccl`\|`mpi`), `hosts` (comma-separated peer IPs — the node prepends its own; `ring` only), `hostfile` (path to a JSON hostfile — `jaccl`/`mpi`), and `pipeline` (`true` = pipeline parallelism for memory pooling, `false` = tensor). The supervisor wraps the inner `mlx_lm.server` in `mlx.launch --backend … --env MLX_METAL_FAST_SYNCH=1 --no-verify-script -- …`; `MLX_METAL_FAST_SYNCH=1` is passed via `--env` so it reaches remote ranks. The whole-model memory gate is skipped for distributed servers (each node holds only a shard). See `docs/plans/distributed-mlx-inference.md` and `docs/guides/testing-distributed-mlx.md`.
- **Distributed status on the dashboard.** `MlxServerInfo` heartbeat fields gain `distributed`, `backend`, and `node_count`; the "Node Models" card renders a `backend · N nodes` chip per distributed server. Fields default back-compatibly so heartbeats from older agents still parse.

### Changed

- **`FLEET_NODE_MLX_SERVERS` is now the single node MLX config surface.** A single-model deploy is a one-entry array: `[{"model":"…","port":11440}]`. Optional per-entry keys: `kv_bits`, `prompt_cache_size`, `prompt_cache_bytes`, `draft_model`, `num_draft_tokens`, plus the distributed keys above.
- **Binary discovery unified into `common/binaries.py::which_extended`.** The collector, image server, and MLX supervisor previously carried three near-duplicate copies; `find_mlx_lm_binary` / `find_mlx_launch_binary` now wrap the shared helper (which also fixes the supervisor's Windows fallback gap).

### Removed

- **Legacy single-server MLX env vars (breaking).** `FLEET_NODE_MLX_AUTO_START`, `FLEET_NODE_MLX_AUTO_START_MODEL`, `FLEET_NODE_MLX_URL`, `FLEET_NODE_MLX_KV_BITS`, `FLEET_NODE_MLX_PROMPT_CACHE_SIZE/BYTES`, `FLEET_NODE_MLX_DRAFT_MODEL`, and `FLEET_NODE_MLX_NUM_DRAFT_TOKENS` are gone, along with the poll-only `MlxClient` heartbeat path. Migrate to a one-entry `FLEET_NODE_MLX_SERVERS` array (the internal `FLEET_*` vars, `~/.fleet-manager/`, and the `herd`/`herd-node` CLI are unchanged). The server-side `FLEET_MLX_URL` proxy fallback and `FLEET_MLX_ENABLED` remain.

### Fixed

- **`0.0.0.0` bind no longer breaks MLX health polling.** `MlxSupervisor` built its health-poll URL from the bind host, so setting `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` (required for multi-node aggregation and distributed rank-0) made it poll `http://0.0.0.0/v1/models` — not a valid connect target. Health polling now always dials loopback (`health_host`), while `--host` still carries the real bind.
- **Port-release wait before MLX respawn.** After a `SIGKILL`, TIME_WAIT entries from the 5s health-check connections could briefly hold the port; the 1s-later respawn then hit `EADDRINUSE` and burned an extra restart cycle. The supervisor now polls until the port is bindable (10s cap, honors shutdown) before spawning.
- **`--kv-bits` preflight only gates when quantization is requested.** The check fired for every start (the condition was always-true on an int), so stock/unpatched `mlx_lm.server` couldn't serve even `f16` (`kv_bits=0`). Now it only requires the patch when `kv_bits` is `4` or `8` — important for distributed peer nodes that don't run the patch.
- **Embedding sidecar bind failure no longer crashes the whole node.** When the native text (port 11439) or vision (port 11438) embedding server couldn't bind its port — e.g. a restart race where the previous `herd-node` hadn't released the port yet — uvicorn's `sys.exit(1)` raised a `SystemExit` that escaped the fire-and-forget `asyncio.create_task(server.serve())` and propagated out of the event loop, exiting the entire node process (heartbeats, LAN proxy, and LLM routing all died with it). The sidecars now bind their socket eagerly during setup via a shared `_bind_sidecar_socket()` helper: a port conflict surfaces as a catchable `OSError`, the node logs `… embedding server port … unavailable — … disabled; node continues serving`, sets the port to `0`, and keeps running. The pre-bound socket is handed to `Server.serve(sockets=[sock])`, and the "started" log now fires only after a confirmed bind (previously it logged success before the async bind actually happened). An optional embedding capability failing to grab its port must degrade only itself, not core inference routing. See `docs/issues/embedding-sidecar-bind-failure-crashes-node.md`.

## [0.7.0] - 2026-06-07

Native text embedding backend and full-fleet Node Models dashboard. The embed timeout incident from June 1 exposed a fundamental contention problem: `OLLAMA_NUM_PARALLEL=2` means a running gpt-oss:120b inference holds both Ollama slots, so nomic-embed-text requests queue indefinitely inside Ollama regardless of available hardware (14% CPU, 291 GB free RAM). The structural fix routes text embedding out of Ollama entirely — a dedicated fastembed server on port 11439 handles nomic-embed-text via ONNX Runtime with zero inference slot contention. Verified on the local fleet: 573 embed requests over 24h, avg 792ms, 0.0% error rate, no timeouts. Dashboard now shows cards for every model backend (Ollama, MLX, native fastembed, vision embedding) with live per-model stats, renamed from "Request Queues" to "Node Models" to reflect the expanded scope.

### Added

- **Native text embedding server (fastembed, port 11439).** A dedicated FastAPI server runs `nomic-ai/nomic-embed-text-v1.5-Q` (130 MB int8, 768 dims, 8192 token context) via ONNX Runtime — no PyTorch, no Ollama. The router intercepts `/api/embed` calls for `nomic-embed-text` before they reach Ollama and proxies them to the best available node's text embedding server. `fastembed>=0.4.0` added to the existing `--extra embedding` group (same `uv sync --extra embedding` command as vision embeddings — one command enables both). Model weights download automatically on first request (~130 MB from HuggingFace) and are cached in `~/.fleet-manager/models/text-embedding/`. Zero contention with LLM inference slots. See `src/fleet_manager/node/text_embedding_server.py`, `src/fleet_manager/node/text_embedding_models.py`, `src/fleet_manager/server/routes/text_embedding_compat.py`.

- **4 new health checks (32 → 36 total):**
  - `embed_error_rate` — WARNING at ≥5 embed errors/hour, CRITICAL at ≥25/hour. Closes the observability gap that let 202 embed timeouts accumulate undetected for 1.5h on June 1.
  - `text_embedding_backend_missing` — WARNING when nomic-embed-text weights are cached on disk but `fastembed` isn't installed. Fix: `uv sync --extra embedding`.
  - `text_embedding_ollama_bypass` — WARNING when nomic-embed-text is available in Ollama but the native server isn't running, meaning embed requests still contend for LLM inference slots. Includes Apple Silicon platform note.
  - `nomic_loaded_in_ollama` — INFO when the native server is running and handling all embed traffic but nomic-embed-text remains loaded in Ollama's hot set, consuming VRAM and a model slot unnecessarily.

- **`TextEmbeddingModel` and `TextEmbeddingMetrics` heartbeat fields.** Node heartbeats now report `text_embedding` (available models + cached status), `text_embedding_port`, and `text_embedding_status` (`backend_available`, `cached_model_count`). The router registry copies these fields to `NodeInfo` and `/fleet/status` includes them in node serialization.

- **Dashboard "Node Models" — cards for all backends.** Renamed from "Request Queues". Now shows a card for every model receiving traffic, not just Ollama-queued models: Ollama (grey badge), MLX (purple), native fastembed (green), and vision embedding (cyan). Cards for instant backends (fastembed, vision) show 24h completed/failed counts and avg latency from a 60s-TTL trace DB cache rather than live queue depth. "DL ON DEMAND" amber chip appears when model weights are not yet cached. Stats counter in the header includes all backends, not just Ollama queues.

### Fixed

- **Embed timeout visibility and resilience.** The `/api/embed` handler previously had no `record_trace()` calls — all embed outcomes (success and failure) were invisible to the dashboard and health checks. Added trace recording for all paths. Added a retry loop (1s then 3s backoff) on `ReadTimeout` before returning 504, matching the retry behavior of the LLM streaming path. The 504 response includes a human-readable hint when the timeout is likely caused by a model download in progress.

- **`embed_error_rate` SQL uses `model LIKE '%embed%'` (not `tags LIKE '%embed%'`).** Using the `tags` column caused false positives — the local VOD processing pipeline tags its LLM requests with "embed" as a pipeline stage label. Restricting to `model` column matches only actual embedding models.

- **`filelock` DEBUG log suppression in fastembed download path.** fastembed's HuggingFace download emits ~20 DEBUG lines per file lock during the initial model download. Suppressed with `logging.getLogger("filelock").setLevel(logging.WARNING)` in the text embedding server.

### Changed

- **`CLAUDE.md` updated:** architecture table + routes updated for new text embedding modules; current state reflects nomic-embed-text → native fastembed; health count 32→36; test count 986→1006; `--extra embedding` description updated to mention fastembed; `skills/` grep updated.

## [0.6.2] - 2026-05-16

Reliability hardening for the trace_store SQLite layer and structured logging — both addressing a real production incident on the local fleet that ran undetected for ~4 days. The headline finding: a long-running read transaction held off WAL checkpoints, the WAL grew to 2.5 GB, and the 5-second `busy_timeout` couldn't absorb the resulting writer contention. ~40,000 background `record_trace` tasks failed with `database is locked` over May 10-15 while requests themselves still succeeded — observability was the only visible casualty (dashboard `reqs_24h` quietly dropped to 0). Adjacent: the daily log rotation handler raced between `herd` and `herd-node` writing to the same file, leaving one day's log growing for the entire incident window. Both are fixed; both now have health checks or architectural separation to prevent silent recurrence. Initial fix (busy_timeout + retry + autocheckpoint, committed 2026-05-15) reduced failure amplitude but didn't eliminate it — see the follow-up Part C + A fix below, which addresses the structural cause and was verified clean under live load on 2026-05-16.

### Fixed

- **`TraceStore` and `LatencyStore` now use a dedicated read connection** (Part C in [`docs/plans/trace-store-read-connection-and-checkpoint.md`](docs/plans/trace-store-read-connection-and-checkpoint.md)). Each store opens two `aiosqlite` connections: `_db` for writes, `_read_db` for every dashboard analytics + scoring read path. Two purposes — (1) aiosqlite serializes operations per-connection through one background thread, so a slow read on the shared connection blocks queued writes for the read's duration; with separate connections they run concurrently in separate threads. (2) Read snapshots pin the WAL checkpoint barrier; on a separate connection the writer's view of the WAL can advance independently. Combined effect verified on the local fleet 2026-05-16 — a 30-concurrent-write + 120-dashboard-poll burst held the WAL at 410 KB peak vs 103 MB on the same workload before this change. `PRAGMA query_only=1` on the read connection rejects accidental writes immediately rather than silently competing with the writer. 7 new tests cover routing (reads → `_read_db`, writes → `_db`), query-only enforcement, and connection lifecycle.

- **Explicit periodic `PRAGMA wal_checkpoint(PASSIVE)`** every 10 seconds on each store's writer connection (Part A in same plan). Defense-in-depth on top of `wal_autocheckpoint=100` — autocheckpoint is tied to write *volume*, so under bursty traffic the WAL can sit at 99 pages for a long time while readers accumulate snapshots; by the time the 100th page write fires autocheckpoint, those snapshots have pinned the checkpoint barrier so far back that very little can advance. Tying checkpoints to wall-clock makes them fire in the gaps between reader snapshots rather than only when a write lands on a threshold. PASSIVE is non-blocking, so this is safe to run on a tight cadence. Logged at DEBUG every tick; promotes to INFO if a tick comes back contested with >100 unwritten pages — surfaces sustained contention without flooding the log under healthy operation. 3 new tests cover the return shape, closed-connection safety, and error-swallow behavior (must never crash the background task).

- **`TraceStore` and `LatencyStore` SQLite write resilience.** `PRAGMA busy_timeout` bumped from 5s → 30s in both stores so a transient WAL checkpoint stall can't immediately fail writes. `TraceStore.record_trace` now retries on `database is locked` errors with exponential backoff (200ms → 800ms → 2s, 3 attempts) before giving up — so the busy_timeout absorbs short contention and the retry loop covers longer stalls, for ~90s cumulative patience before a trace is declared lost. Added `PRAGMA wal_autocheckpoint=100` to both stores to bound WAL growth even when an external reader is slow. Failures after all retries are exhausted are counted in a rolling deque so the new health check (below) can surface them without operators having to grep logs. See the 2026-05-15 observation in `docs/observations.md` for the full incident timeline.

- **Daily log rotation race between `herd` and `herd-node`.** Both processes previously called `setup_logging` with the same default file path (`~/.fleet-manager/logs/herd.jsonl`) and registered their own `TimedRotatingFileHandler`. At UTC midnight, one process would rename `herd.jsonl` → `herd.jsonl.YYYY-MM-DD` and the other would keep writing to the renamed inode via its still-open file descriptor for the rest of the file's life. Observed in the wild: a single day's log grew to 131 MB while peer days were 6 MB; the orphaned file kept receiving writes for five days after its supposed rotation. Fix: `setup_logging(log_name=...)` is now parameterized; router uses `herd` (default, back-compat) and node uses `herd-node`. Each process owns its rotation. Cross-file audits (`grep -c '"level": "ERROR"' ~/.fleet-manager/logs/herd*.jsonl*`) still work via glob.

### Added

- **`trace_store_write_failures` health check (now 32 distinct checks).** Reads `TraceStore.get_write_failure_count(window_s=300)` and emits a WARNING at 1+ failures in the last 5 minutes, CRITICAL at 50+. Closes the observability black hole that hid the May 10-15 incident — the only visible signal before this was `dashboard reqs_24h=0` for a router that was clearly serving traffic, which is easy to dismiss as "nobody's running anything right now." 10 new tests in `tests/test_server/test_trace_store_resilience.py` cover retry-on-locked-then-succeed, retry-exhaustion-then-record-failure, non-lock-errors-don't-retry, window-pruning semantics, and the severity threshold transitions.

### Changed

- **`CLAUDE.md` "Gotchas" — two new entries** to keep future operators (and AI agents reviewing logs) from repeating the failure modes that delayed detection of this incident. (1) JSONL log scans must use `'"level": "ERROR"'` with a space after the colon — `json.dumps` writes whitespace by default and the no-space variant silently matches zero lines. A wrong grep pattern was the root cause for ~4 days of "clean fleet" soak reports during the incident. (2) Trace DB write failures are invisible from the dashboard because `record_trace` is fire-and-forget — explicit pointer to the new health check + the three most common root causes (long-running read, disk-full, stale `db-shm`/`-wal`).

## [0.6.1] - 2026-04-27

Reliability + observability hardening on top of 0.6.0's multi-MLX foundation. The big wins: an MLX supervisor that can no longer get stuck restarting an orphan-port forever (orphan reap + crash-window quarantine), vision embedding chips that stop lying when the backend isn't installed, a `brew install ollama-herd` path that actually works, a tunable `FLEET_MLX_MAX_INFLIGHT_PER_MODEL` for operators willing to trade memory for batched throughput, dashboard color semantics that finally agree with the product's "idle hardware is waste" thesis, and speculative decoding live on the dedicated 30B compactor for an immediate Claude-Code-summarization-pass speedup. Source-read research in `docs/research/mlx-lm-stability-and-concurrency.md` confirmed v0.31.3 is the right pin; v0.31.2 would have traded our quarantine-able bug for two uncontainable ones.

### Added

- **Speculative decoding enabled on the dedicated context compactor (port 11441, `Qwen3-Coder-30B-A3B-Instruct-4bit`).** Re-tested on our pinned `mlx-lm==0.31.3` and confirmed it works on standard transformer MoEs with a `Qwen3-1.7B-4bit` draft (≈94 tok/s on M3 Ultra, no `ArraysCache` error). Live config in `~/.fleet-manager/env` adds `"draft_model":"mlx-community/Qwen3-1.7B-4bit","num_draft_tokens":4` to the 30B-A3B server entry only — every Claude Code request's pre-summarization pass benefits, which is the hot path. **Not enabled** on port 11440 (`Qwen3-Coder-Next-4bit`, the main coding model): the Qwen3-Next architecture uses Mamba/SSM-style linear attention layers that mlx-lm represents with a non-trimmable `ArraysCache`, so spec decoding still hits [ml-explore/mlx-lm#1081](https://github.com/ml-explore/mlx-lm/issues/1081) at `speculative_generate_step:531`. The earlier "always blocked on `ArraysCache`" framing was wrong — the bug is architecture-specific, not version-specific. Updated post-mortem in [`docs/issues/mlx-speculative-decoding-blocked.md`](docs/issues/mlx-speculative-decoding-blocked.md). Per-spec `draft_model` + `num_draft_tokens` were already plumbed through `MlxServerSpec.from_dict` in 0.6.0 — this release is config-only on the production fleet.

### Fixed

- **`FLEET_MLX_MAX_INFLIGHT_PER_MODEL` env var** — tunable per-model concurrent-request cap on the MLX proxy. Default `1` (strict serialization, matching historical behavior). Bump to `2` or `3` to let `mlx_lm.server`'s `BatchGenerator` process multiple requests in one inference pass — empirically validated 2026-04-27 on the local fleet (3 concurrent requests took 0.55s wall vs 1.10s sum, confirming real parallelism). The default stays conservative because each in-flight request carries its own KV cache state, so 2× concurrent 100K-token prefills is 2× memory pressure; concurrent paths in `mlx_lm.server` have historically been bug magnets (#965, #1166 — both fixed in v0.31.3, but the pattern is real). Operator opt-in only. Full source-read + live test in `docs/research/mlx-lm-stability-and-concurrency.md`. 5 new tests cover defaults, clamping (negative/zero values floor to 1), the 2-concurrent-acquires-then-third-blocks behavior, and per-model semaphore independence. Confirmed in the same research that **mlx-lm v0.31.3 is the right pin** even though it's where we hit #1208 — downgrading to v0.31.2 would trade our quarantine-able bug for two uncontainable ones (#1166 Qwen3-Next concurrent crash + #1181 thread-local stream crash).

- **MLX supervisor now detects and SIGKILLs orphan `mlx_lm.server` processes on the configured port before spawning its own.** A previous herd-node session killed via `pkill -9 -f "bin/herd-node"` (without also killing `mlx_lm.server`) leaves the MLX subprocesses alive — `Popen(start_new_session=True)` makes them survive their parent's death; they get reparented to launchd and keep holding ports 11440 / 11441. The next supervisor startup couldn't bind, exited rc=1 in ~2 seconds, and the quarantine guard kicked in against a process that didn't exist while the orphan kept serving requests. Result was 17 hours of `mlx_server_quarantined` warnings against a fleet that was actually serving traffic, just not under any supervisor's control. New `find_orphan_mlx_pids_on_port()` (psutil-based, identity-strict — only kills processes whose cmdline mentions `mlx_lm.server` AND whose net_connections show binding to our port). `MlxSupervisor.start()` calls it before its own `Popen` and SIGKILLs anything found, with a loud WARNING explaining what was reaped. 8 new tests for the strict identity check (right cmdline + right port = kill; wrong port = leave alone; non-mlx process on right port = leave alone; permission errors handled). Operator restart recipe in `CLAUDE.md` updated to `pkill -9 -f "bin/herd|mlx_lm.server"` so the manual path doesn't reproduce the trap. See observation in `docs/observations.md` 2026-04-27.

- **MLX supervisor now quarantines a crash-looping subprocess instead of restarting it forever.** On 2026-04-26 a stuck-state in `mlx_lm.server v0.31.3` (latest) triggered a 420-restart, 2.5-hour crash loop on the local fleet — the supervisor's `_monitor` correctly detected each crash and restarted at the (capped) 60s exponential-backoff cadence, but had no upper bound on how many times it would keep doing that. Now: ≥5 crashes within a 5-minute rolling window switches the supervisor into "quarantined" state with a 10-minute restart interval, and surfaces a CRITICAL `mlx_server_quarantined` health-check recommendation pointing at logs + likely causes. Quarantine clears automatically once a restart stays up for the full window — so transient bursts still recover gracefully; only persistent failures get throttled. Tunable via `_QUARANTINE_FAILURE_COUNT` / `_QUARANTINE_WINDOW_S` / `_QUARANTINE_RESTART_INTERVAL` in `mlx_supervisor.py`. 8 new tests for the threshold + windowing logic. Underlying mlx-lm bug filed upstream as [ml-explore/mlx-lm#1208](https://github.com/ml-explore/mlx-lm/issues/1208). See observation in `docs/observations.md` 2026-04-26.

- **Vision embedding chips no longer lie about availability when `onnxruntime` is missing.** The dashboard previously rendered DINOv2 / SigLIP / CLIP chips on a node card whenever the model weights were cached on disk, regardless of whether the embedding backend (`onnxruntime`) could actually load them. Operators saw "available" chips and assumed the service worked; the first real `/embed` call returned HTTP 500. The collector now probes for `onnxruntime` import on every heartbeat and refuses to advertise vision-embedding models when the backend isn't loadable, so the chips disappear (honest) instead of misleading. Paired with a new **`vision_backend_missing` health check** (WARNING) that fires when weights ARE cached but the backend is missing — operators see "Vision embedding backend not installed on `<node>`. Run `uv sync --extra embedding` ..." in the Recommendations panel instead of silently-disappearing chips. Also closes the recurring root cause: the local-deploy snippet in `CLAUDE.md` was `uv sync` (without `--extra embedding`), which is destructive — every restart stripped the embedding deps. Updated to `uv sync --all-extras` so optional capabilities stay resident across restarts. New `vision_embedding_status: dict` field on heartbeat + `NodeState` carries `{backend_available, cached_model_count}` so future health checks have a clean signal to read. 9 new tests in `tests/test_server/test_health_vision_backend.py` covering both fires-on-missing and silent-when-fine paths plus the collector probe directly.

- **`brew install ollama-herd` now actually works.** The Homebrew formula at `geeks-accelerator/homebrew-ollama-herd` had been broken throughout 0.5.x — Homebrew's `pip install --no-binary :all:` policy forced source builds for `pydantic-core`, which required Rust to bootstrap `maturin`, which the formula didn't depend on; six `pyproject.toml` deps (`cryptography`, `cffi`, `pycparser`, `tiktoken`, `regex`, `websockets`) were also missing from the formula's `resource` blocks; and `pydantic-core` was version-mismatched against the bundled `pydantic` (2.45.0 vs the required 2.41.5). Fix shipped to the tap as `geeks-accelerator/homebrew-ollama-herd@71856f3` (no PyPI republish needed). Verified end-to-end on macOS Apple Silicon: clean fresh-user install in ~5 minutes, all critical imports clean, both `herd` and `herd-node` CLIs functional. The release checklist in `CLAUDE.md` was updated to make the brew end-to-end install test a non-negotiable gate so this class of failure can't recur. Background and post-mortem in `docs/observations.md` (entry: 2026-04-25).

### Added

- **Platform-aware thermal signal** — new `ThermalMetrics` on the heartbeat with `state` (`nominal` / `warning` / `unknown`), `temperature_c`, and `source` fields. Linux nodes now report real peak temps from `psutil.sensors_temperatures()` (scanning `coretemp` / `k10temp` / `zenpower` / `cpu_thermal` drivers) and flag `warning` above 85°C. macOS and Windows honestly report `unknown` — Apple Silicon's `machdep.xcpm` is Intel-only, `powermetrics` requires sudo, and `pmset -g therm` only reports past events; `psutil.sensors_temperatures()` isn't implemented on macOS at all. The dashboard's `.bar-thermal` overlay now uses the reported signal when available and falls back to the CPU≥95% proxy only when state is `unknown`, so Linux operators get first-class thermal detection and macOS operators keep the existing behavior with a clean seam for future upgrades. See `src/fleet_manager/common/system_metrics.py::get_thermal_metrics`.
- **`/dashboard/color-states` dev route** — renders the utilization bar in every Axis B warning state side-by-side (normal / memory warning / memory critical / CPU thermal) plus gradient sweeps for CPU (cyan→purple) and memory (soft-blue→deep-purple). Uses the live dashboard CSS + JS so any change to production styling reflects here automatically. Linked from `docs/guides/dashboard-color-reference.md` and used by the marketing-site agent for consistent screenshot captures.
- **`docs/handoffs/ollamaherd-com-color-refresh.md`** — self-contained brief for the agent maintaining the marketing site. Documents the palette change, explains why the old landing-page screenshots now contradict the product's own messaging, and provides the live reference URL for capturing replacements.

### Changed

- **Dashboard color semantics: utilization is no longer a warning.** CPU, memory, and per-node RAM bars now render in a blue→purple gradient (`utilizationColor(pct, metric)`) instead of the old green-to-red busy-is-bad scale. A Mac Studio at 95% memory is the product working as designed, not a problem — the previous coloring contradicted the product's own thesis that idle hardware is waste. Warning state moved to a separate visual axis: `.bar-warning` / `.bar-critical` outlines fire on OS-reported memory pressure (`psutil.virtual_memory().pressure`), and `.bar-thermal` fires on sustained ≥95% CPU as a throttling proxy. **Preserved:** disk bar still uses the busy-is-bad scale (disk full genuinely breaks things), and the capacity-score bar keeps its green-at-high-availability semantic. Also added `docs/guides/dashboard-color-reference.md` as a one-page cheat sheet for future UI additions so the semantic doesn't drift back into server-ops defaults. See `docs/plans/dashboard-color-semantics.md` for the full rationale and audit.

## [0.6.0] - 2026-04-24

Multi-MLX, Claude Code reliability, and layered context management. The long-context failure modes that made Claude Code CLI feel broken around 30K tokens on local Qwen3-Coder models are systematically addressed: tool-schema fixup for the llama.cpp#20164 optional-param trap, mechanical tool-result clearing with stable-cut prefix-cache preservation, LLM-based compactor with force_all escape, pre-inference 413 cap, MLX wall-clock timeout. Multi-MLX-server support lets a single node run main + dedicated-compactor models side-by-side without Ollama eviction risk. Tool-use reliability layer repairs malformed JSON tool-call arguments. Ollama watchdog removed after it caused production incidents.

### Added

- **Multi-MLX-server support** — the node agent can now spawn N `mlx_lm.server` subprocesses on N ports simultaneously, with per-server memory-pressure gate, per-URL health reporting, and multi-node aggregation. Closes [`docs/issues/multi-mlx-server-support.md`](docs/issues/multi-mlx-server-support.md). Config shape: `FLEET_NODE_MLX_SERVERS='[{"model":"mlx-community/Qwen3-Coder-Next-4bit","port":11440,"kv_bits":8},{"model":"mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit","port":11441,"kv_bits":8}]'`. Legacy single-server config (`FLEET_NODE_MLX_AUTO_START_MODEL` + `FLEET_NODE_MLX_URL`) is synthesized into a one-entry list when the new var is unset — no breaking change.
  - **`MlxSupervisorSet`** (`node/mlx_supervisor.py`) — owns N child `MlxSupervisor` instances, parallel start/stop, one failure doesn't block the others, per-child status snapshots (`healthy`/`starting`/`unhealthy`/`memory_blocked`/`stopped`) published in the heartbeat.
  - **Memory-pressure startup gate** (`memory_gate_ok()`, `estimate_model_size_gb()`) — before spawning each server, estimates weight size from the HuggingFace disk cache and compares to `psutil.virtual_memory().available`. Refuses to start when the total (model + headroom) won't fit. `FLEET_NODE_MLX_MEMORY_HEADROOM_GB` default 10 GB. Surfaces the skip reason in the heartbeat so the operator sees WHY on the dashboard, not just that the server is down.
  - **Multi-node aggregation** — `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` exposes MLX servers on the LAN. Heartbeat carries per-server `{port, model, status, model_size_gb, kv_bits, last_ok_ts}`; the router's `NodeRegistry.resolve_mlx_url(model)` walks every online node and returns the LAN URL of whichever healthy server hosts the model. `MlxProxy` now takes an optional `url_resolver` callable with a per-URL `httpx.AsyncClient` cache, so a slow server's connection pool can't back-pressure into a fast one. Back-compat: legacy `base_url` positional still works.
  - **Dashboard per-URL health table** — each node card renders a compact MLX servers table showing port, short model name, colour-coded status, size in GB, and time-since-last-healthy. Drops in below the model chip row; absent on nodes without MLX configured.
  - **Two new health checks**: `mlx_memory_blocked` (WARNING when a server skipped start due to memory gate) and `mlx_server_down` (CRITICAL when a server that should be healthy has failed; WARNING when stuck in `starting`). Fix hints point at the three most common causes: missing weights, wiped `--kv-bits` patch, port collision from a leftover subprocess.
  - **Context compactor to a dedicated MLX server** — enables `FLEET_CONTEXT_COMPACTION_ENABLED=true` with `FLEET_CONTEXT_COMPACTION_MODEL=mlx:mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` so summarization runs on an 80B-class MoE (3B active) model without competing for the main coding model's MLX process. Shipped side-by-side on the Mac Studio: Next-4bit @ 41.8 GB on port 11440 + 30B-A3B @ 16 GB on port 11441, both hot, ~260 GB RAM headroom remaining.
  - **45 new tests**: `MlxServerSpec.from_dict` validation, memory gate accept/reject/unknown-size paths, HF cache walk, `MlxSupervisorSet` parallel orchestration + one-failure isolation + duplicate-port dedup + healthy-models filter, registry resolution (single node, multi-node, bare/prefixed, offline/unhealthy skip, full-map aggregation), `MlxProxy` resolver priority + exception fallback + per-URL client cache isolation + unresolvable-URL error, and two per-server health check emitters.

### Fixed

- **Per-port MLX log files** — `mlx_lm.server` subprocesses now write to `~/.fleet-manager/logs/mlx-server-<port>.log` (e.g. `mlx-server-11440.log`) instead of sharing a single `mlx-server.log`. Uncovered during post-deploy investigation of a timed-out request: with both MLX subprocesses appending to the same log file, per-server crash diagnosis was effectively impossible. Single-server deploys get a stable `mlx-server-11440.log` path; the old shared `mlx-server.log` is left untouched for archival value but no longer written to. Also updated the module docstring path reference so `tail -f ~/.fleet-manager/logs/mlx-server-*.log` does the right thing for any deploy shape.

- **`FLEET_MLX_WALL_CLOCK_TIMEOUT_S` guidance** — default stays at 300s (reasonable for most workloads), but the configuration reference now explicitly calls out that long Claude Code sessions (2000+ messages) on Qwen3-Coder-Next-4bit routinely run 200-245s and need `600` to avoid edge-case 300.5s-type timeouts. No default change; the existing env var has always been tunable — just making the tuning knob discoverable for the workload that most benefits from it.

- **Four field-survey-driven Claude Code CLI enhancements (P1–P4).** Landed after a competitive audit of 13+ open-source Claude Code proxies (musistudio/claude-code-router 32.8k⭐, nicedreamzapp/claude-code-local, and others). Full research in [`docs/research/claude-code-proxy-techniques-survey.md`](docs/research/claude-code-proxy-techniques-survey.md); priority matrix + rationale in [`docs/plans/claude-code-enhancements-from-field-survey.md`](docs/plans/claude-code-enhancements-from-field-survey.md).
  - **P1 — Expanded JSON repair patterns.** `tool_call_repair.py` now falls through to a four-pattern regex catalog (Pattern A: `parameter=key>value`, Pattern B: `<parameter_key>value`, Pattern C: malformed `"arguments"` objects, Pattern D: single-arg tool inference via an 8-entry defaults table for Bash/Read/Write/Glob/Grep/WebFetch/WebSearch/TodoWrite) when `json-repair` produces schema-invalid output. Adapted from `nicedreamzapp/claude-code-local`'s `recover_garbled_tool_json`. Still schema-gated — no repair substitutes unless it passes `_structurally_valid_against_schema`. 10 new tests covering each pattern + end-to-end single-arg inference. The repair cascade is now strict-parse → json-repair → regex-patterns → pass-through original.
  - **P2 — `FLEET_ANTHROPIC_TOOLS_DENY`.** Comma-separated list of Claude Code tool names to strip from every `/v1/messages` request before translation (e.g. `"WebSearch,WebFetch,NotebookEdit"`). Saves 200–600 prompt tokens per turn depending on which tools get removed. Pairs with client-side `permissions.deny` in `.claude/settings.json` — client-side only blocks execution, this removes the definitions from the wire entirely. Names matched exactly (case-sensitive). 7 new tests covering empty deny, single/multi strip, whitespace tolerance, exact-not-substring, strip-everything.
  - **P3 — Size-based model escalation.** `FLEET_ANTHROPIC_SIZE_ESCALATION_TOKENS` + `FLEET_ANTHROPIC_SIZE_ESCALATION_MODEL` let operators auto-route prompts over N tokens to a different (larger) model without changing the normal `FLEET_ANTHROPIC_MODEL_MAP`. Example: map Sonnet to `qwen3-coder:30b` for fast turns, escalate to `mlx:Qwen3-Coder-Next-4bit` above 50K tokens. Trades small-request throughput for large-request quality where it matters. Pre-route token count uses the same `_total_tokens()` used by context management so it's consistent with clearing/compaction decisions.
  - **P4 — Warm-prompt preload on MLX supervisor start.** After `mlx_lm.server` passes its health check, `MlxSupervisor` fires a fire-and-forget 1-token request to prime the prompt cache with the system prompt prefix. Based on `waybarrios/vllm-mlx`'s reported 1.3–2.25× TTFT improvement on the first real request. Non-fatal on failure (DEBUG log). Confirmed live after deploy — `mlx_lm.server warmup complete — prompt cache primed` appears ~15s after supervisor start.
  - **P6 — Documentation.** New "Stability techniques for long-context local sessions" section in [`docs/guides/claude-code-integration.md`](docs/guides/claude-code-integration.md) covering `permissions.deny` pairing, the 80/20 token-range rule, fresh-session cadence, and the new size-escalation knobs. Three new env vars documented in [`docs/configuration-reference.md`](docs/configuration-reference.md).

- **Per-tier model routing: `claude-haiku-*` → `gpt-oss:120b` (Ollama); `claude-sonnet-*` / `claude-opus-*` → `mlx:Qwen3-Coder-Next-4bit`.** `FLEET_ANTHROPIC_MODEL_MAP` edit only — zero code change. Lets Claude Code users trade speed for quality per-invocation (`claude --model claude-haiku-4-5`). Haiku goes to a smaller hot Ollama model (fast, already pinned) while heavier tiers stay on the 80B MoE. Different model families on different tiers also diversifies failure modes — if Qwen3 has a bad day, haiku still works. See [`docs/plans/claude-code-performance-improvements.md`](docs/plans/claude-code-performance-improvements.md) §#4.

- **Tool-call JSON repair: `server/tool_call_repair.py` + metrics.** Local coding models occasionally emit `tool_use.input` with minor syntax errors (trailing commas, unescaped quotes, missing brackets). Without repair, Claude Code's strict SDK parser rejects these and the session errors. New module uses the `json-repair` library (added to core deps, pure Python ~100KB) to attempt recovery, validates the repaired dict against the tool's `input_schema`, and only substitutes the repaired version if it passes structural check. **Never hides real failures silently** — every repair attempt logs at WARNING, and `tool_repair: {attempts, successes, failures}` counters are exposed per model on `/fleet/queue` so operators can see if a model's repair rate is climbing (>1% sustained = signal to reconsider the model). Integrated in `build_anthropic_non_streaming_response` (the `/compact` path). 13 tests covering happy paths, schema-gated acceptance/rejection, edge cases (non-dict input, pure garbage, no schema). See [`docs/plans/claude-code-performance-improvements.md`](docs/plans/claude-code-performance-improvements.md) §#3.

- **Speculative decoding infrastructure (shipped disabled — blocked upstream).** `FLEET_NODE_MLX_DRAFT_MODEL` + `FLEET_NODE_MLX_NUM_DRAFT_TOKENS` config settings, supervisor `--draft-model` flag wiring in `_build_cmd`, draft weights (`mlx-community/Qwen3-1.7B-4bit`, 940MB) pre-downloaded. **Runtime enable requires upstream mlx-lm fix**: issue [#1081](https://github.com/ml-explore/mlx-lm/issues/1081) causes every speculative request to fail with `ArraysCache` cache-type error in 0.31.3. Filed [`docs/issues/mlx-speculative-decoding-blocked.md`](docs/issues/mlx-speculative-decoding-blocked.md) with the full reproduction matrix and enable-when-fixed instructions. The moment upstream ships the fix, setting `FLEET_NODE_MLX_DRAFT_MODEL=mlx-community/Qwen3-1.7B-4bit` in `~/.fleet-manager/env` flips it on.

- **`scripts/benchmark-performance.py` — before/after perf measurement against real Claude Code traffic.** Replays N captured requests (from `~/.fleet-manager/debug/requests.*.jsonl`) through the router, reports p50/p95/mean for latency, TTFT, generation tokens/sec, overall tokens/sec. `--compare` flag diffs against a prior saved run so you can verify whether a config change actually helped — no synthetic workloads, uses the fleet's real traffic patterns. Use case: flip any knob (tool-schema fixup mode, compaction trigger, MLX kv-bits, draft model if upstream fixes it), save baseline + post-change runs, read the delta table.

- **Source-level clarification: three "compact" mechanisms, one of which we serve automatically.** Cross-referenced Claude Code's own source (`src/commands/compact/compact.ts`, `src/services/api/claude.ts`) against 10,830 captured requests on this fleet to settle the ambiguity. (1) The user-facing `/compact` command is pure client-side orchestration over plain `/v1/messages` — no beta header, no special body field. Our server already serves it correctly; the layered context management we shipped augments it. (2) The `context_management` body field (with `clear_tool_uses_20250919` / `clear_thinking_20251015` edit strategies) is a distinct Anthropic server-side beta gated behind `context-management-2025-06-27` — external CC users never send it. (3) `cache_edits` content blocks (microcompact) are injected INSIDE `messages[].content[]` behind `cache-editing-20250919`, also Ant-only today. Confirmed our pydantic model (`AnthropicMessage.content: str | list[dict[str, Any]]`) passes all three without validation errors. New `_log_unknown_block_type_once()` in `anthropic_translator.py` logs first-occurrence of any unknown block type (process-lifetime dedupe) so if microcompact ever starts firing we notice without spam. Documented in `docs/research/why-claude-code-degrades-at-30k.md` §7 (new section) + `docs/guides/claude-code-integration.md` (advertises `/compact` support with honest scoping). 2 new translator tests covering skip + dedupe behavior.

- **Pre-inference 413 cap + session-level force-compact + MLX wall-clock timeout** — three-part defense against long-session wedging that mirrors hosted Claude Code's layered behavior. After Layer 1 clearing and Layer 2 LLM compaction both run, the route checks total tokens: if still > `FLEET_ANTHROPIC_MAX_PROMPT_TOKENS` (default 180K), return HTTP 413 with a clear `"run /compact and resubmit"` message before the request ever reaches the model — no 5-minute MLX prefill wedge. The compactor itself gains a `force_all=True` path that bypasses per-strategy `min_bloat_tokens` gates; it's triggered when post-clearing tokens exceed `FLEET_CONTEXT_COMPACTION_FORCE_TRIGGER_TOKENS` (default 150K, matching Anthropic's own compaction trigger). Independently, `server/mlx_proxy.py` now enforces `FLEET_MLX_WALL_CLOCK_TIMEOUT_S` (default 300s) on every request — catches wedged-request syndrome where `mlx_lm.server` keeps emitting tokens slowly but never stops. On timeout, the slot is released and the route returns 413 with the same `/compact` hint. New `MlxWallClockTimeoutError` exception class. Tests added for force_all (1), wall-clock-timeout config + exception shape (3). **No silent server-side retry** — client owns the decision of whether to resubmit because correctness of agentic tool-use workflows depends on not altering context mid-turn. Verified live: synthetic 250K-token request got clearing 250,995 → 7,119 tokens and served cleanly in 18s; pre-inference cap correctly did NOT fire because Layer 1 was already aggressive enough.

- **Mechanical tool-result clearing** (`server/context_management.py`) — new first-layer context-management module that closes the biggest structural gap vs hosted Claude Code. When the Anthropic request prompt exceeds `FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_TRIGGER_TOKENS` (default 100K), older `tool_result` blocks are replaced with a short placeholder before the request reaches the model — no LLM call, microsecond-scale, matches hosted Claude's [Context Editing API](https://platform.claude.com/docs/en/build-with-claude/context-editing). Configurable via `FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_KEEP_RECENT` (default 3 most-recent `tool_result` blocks preserved verbatim). `tool_use` blocks (the model's own output) are never cleared — conversation structure stays intact, only stale bodies are dropped. Runs on the native Anthropic message shape BEFORE translation so the block-level structure is still visible. Per-request log line shows `tokens_before → tokens_after` and cleared count for observability. Real Claude Code session verified: first fire reclaimed 81K tokens (206K → 125K, 60.8% reduction). 11 tests covering trigger gating, keep-recent policy, non-mutation, edge cases (empty, string-content, zero-keep, multiple-per-message). Ships as the new Layer 1 ahead of the existing LLM-based compactor (Layer 2). Research + reasoning in `docs/research/why-claude-code-degrades-at-30k.md`.

- **Tool-schema fixup for Qwen3-Coder long-context tool-call bug** — new `server/tool_schema_fixup.py` module + `FLEET_ANTHROPIC_TOOL_SCHEMA_FIXUP` setting (default `"inject"`). Claude Code's 27-tool schema has heavy optional-param usage (Grep has 13 optional params); [llama.cpp#20164](https://github.com/ggml-org/llama.cpp/issues/20164) documents that Qwen3-Coder starts silently dropping optional params at ~30K tokens and loops tool calls with a field consistently missing. The fix promotes optional params with known-safe defaults (`Bash.timeout=120000`, `Grep.head_limit=250`, `Read.offset=0`, etc.) to required-with-default in the outbound schema. Backed by a `CLAUDE_CODE_TOOL_DEFAULTS` table keyed by `(tool, param)` — unknown tools pass through unchanged. Three modes: `off` / `promote` (existing defaults only) / `inject` (default, actually does the fix). 14 tests anchored on real Bash/Grep/Read/Agent schemas captured via `FLEET_DEBUG_REQUEST_BODIES`. Full reasoning in `docs/research/why-claude-code-degrades-at-30k.md`.

- **MLX proxy: non-streaming path now forwards `stream=True` internally** and accumulates OpenAI SSE chunks into the single response shape the client asked for. Fixes a class of silent `httpx.ReadTimeout` failures where non-streaming calls to large models (observed on 480B with 159 messages + 27 tools = 14-minute silent prefill) held the HTTP connection open without byte-level progress, tripping the read timer. By consuming the stream we see bytes per token → read timer resets naturally → only a truly stuck server trips timeout. New `_collect_openai_stream()` helper rebuilds tool-call arguments from partial-JSON delta chunks. Also adds `FLEET_MLX_READ_TIMEOUT_S` (default 1800s) as a tunable backstop. Five new tests covering text-only, tool-calls, trailing usage chunks, malformed lines, and contract compatibility with `build_anthropic_non_streaming_response`.

- **Clearer error when `onnxruntime` is missing** (`node/embedding_models.py:ONNXBackend.__init__`). Previously: generic `No module named 'onnxruntime'` that the dashboard label "Services: 8 loaded" contradicted, because the code underneath was just checking if files exist on disk. Now raises an `ImportError` with the exact fix: `uv sync --extra embedding`. Dashboard header corrected to "Services: N available" to match reality — only truly-in-RAM Ollama + MLX models get the "loaded" count.

- **Research doc: `docs/research/why-claude-code-degrades-at-30k.md`** (207 lines, 2,560 words, 11 cited sources). Maps the user-visible "Claude Code feels broken around 30K tokens" symptom to two root causes: (1) Qwen3-Coder's optional-param parser failure (cited upstream bug), (2) the industry-wide gap between advertised and effective context (RULER benchmark numbers for GLM-4, Llama-3.1, Qwen3 variants). Recommends Qwen3-Coder-Next (80B MoE / 3B active) as the highest-ROI swap candidate based on published head-to-head reviews. Documents what we don't know (no published RULER for these specific models, MLX reproduction of the parser bug untested).

- **Issue filed: `docs/issues/multi-mlx-server-support.md`** — the current MLX integration assumes one `mlx_lm.server` process per node; A/B testing a new model requires a destructive swap. Proposal sketched for running N servers on N ports with per-model URL routing in the proxy, gated on `FLEET_NODE_MLX_SERVERS` env. Estimated 1.5–2 days of work when the need becomes concrete.

- **Per-node model pins via dashboard** — pin button on each Recommendations card toggles `<data_dir>/pinned_models.json` through `GET/POST /dashboard/api/pinned-models`. Env-level pins (`FLEET_PINNED_MODELS`) union with per-node pins; the preloader re-reads the file every 10 min so toggles land without restart. Vision-embedding models are excluded from the UI (no pin button) and rejected server-side — pins only affect the Ollama preloader, which has no levers over the embedding service. Pin button hidden for models ineligible for Ollama management. See `server/pinned_models.py`, `server/routes/dashboard.py`.
- **Dynamic curator selection in the Context Compactor** — summary work now goes to whatever capable model is already hot and idle rather than always cold-loading the configured default. Ranking: hot + eligible + idle (pinned models preferred when idle, penalized when busy, quality tiebreaks by params_b); falls back to the configured default when nothing suitable is hot; fails-open (no compaction) when even the default is saturated. Cache key deliberately excludes `curator_model` so MLX prefix-cache bytes stay stable across curator-selection events — each content block locks in whichever curator happened to run first. Two new env vars: `FLEET_CONTEXT_COMPACTION_IDLE_WINDOW_S` (default 120s, set to 0 to disable dynamic selection), `FLEET_CONTEXT_COMPACTION_CURATOR_MIN_PARAMS_B` (default 7.0 — below this, skip compaction rather than use an unreliable small curator). New `TraceStore.get_request_count_by_model(seconds)` surfaces recent activity.
- **`~/.fleet-manager/env` auto-loaded at process startup** — both CLI entry points call `load_env_file()` before any pydantic-settings instantiation, so `FLEET_*` vars work even when `herd` / `herd-node` are launched from non-interactive shells that don't source `~/.zshrc` (Bash subshells, nohup, launchd plists, CI). Shell env always wins; the file is a fallback, not an override. Accepts plain `KEY=value`, optional `export` prefix, `#` comments, quoted values. Override path with `FLEET_ENV_FILE=/some/other/path`. Template at `docs/examples/fleet-env.example`. Closes a silent-failure class that bit us twice in one day — node agent starting without MLX env (supervisor didn't auto-start the 480B) and router starting without the Anthropic model map (Claude Code requests silently fell back to `qwen3-coder:30b-agent` instead of the intended MLX 480B). 8 loader tests.
- **`scripts/setup-mlx.sh` — idempotent MLX installer** — pins `mlx-lm==0.31.3` via `uv tool`, applies the ollama-herd KV-quant patch (exposes `--kv-bits`, `--kv-group-size`, `--quantized-kv-start` — required by `mlx_supervisor`, absent in upstream mlx-lm), verifies flags are live. Re-run after any `uv tool upgrade mlx-lm` — upgrades wipe the patched `server.py`. Full setup guide at `docs/guides/mlx-setup.md`.
- **`mlx_supervisor` preflight check for `--kv-bits`** — probes `mlx_lm.server --help` before launch; if the patch is missing and KV quantization was requested, fails fast with an error pointing at `./scripts/setup-mlx.sh` instead of letting a 120s health-check timeout mask the root cause.

### Removed

- **Ollama watchdog (`node/ollama_watchdog.py`) removed entirely** after it caused more harm than good in production. The probe-model picker chose the smallest loaded model for its chat probe, which kept selecting embedding-only models like `nomic-embed-text` — `/api/chat` on an embed model returns HTTP 400, which the watchdog interpreted as "stuck runner," kicked runners 13 times in ~13 minutes, then cascade-escalated to a full `ollama serve` restart that wiped all pinned models. During the window, 20 `gemma3:27b` requests were silently routed to `gpt-oss:120b` via cross-category VRAM fallback (vision → reasoning), which drops image inputs. Fleet had been running cleanly without a watchdog before we added it; removing is the honest response. 5 `ollama_watchdog_*` settings deleted from `config.py` with a comment explaining the failure mode so nobody re-adds it naively. `tests/test_node/test_ollama_watchdog.py` deleted alongside. `docs/troubleshooting.md`, `docs/research/claude-code-ollama-ecosystem-2026.md`, and `docs/experiments/claude-code-stress-test.py` updated to reflect the removal. Post-mortem in `docs/issues.md`.

### Fixed

- **Cross-category VRAM fallback now logs at ERROR with a QUALITY RISK annotation** (`server/routes/routing.py`). Previously INFO — easy to miss that a vision request was being served by a reasoning model. Fallback event records carry `cross_category` and `fallback_category` fields for dashboard filtering. Existing `X-Fleet-Fallback` response header continues to flag substitutions to clients. Same-category fallbacks stay at INFO (expected behavior, not a quality concern).

- **DINOv2 and other vision embeddings wrongly flagged "not downloaded" on Recommendations page** — `ModelRecommender._plan_node` only looked at `node.ollama.models_available` when computing `already_available`, so models served by the vision embedding service on `:11438` always showed as needing `ollama pull`. That pull command would have failed — DINOv2 isn't in Ollama's registry. Recommender now merges `node.vision_embedding.models_available` into the availability check. UI also excludes `vision-embedding` category from the pull command and shows an `auto-install` badge explaining those models download from HuggingFace on first `/api/embed-image` request. Regression test in `test_model_recommender.py::TestVisionEmbeddingAvailability`.

### Added (earlier in the Unreleased cycle)

- **Device-aware scoring (bandwidth-proportional routing)** — chip detection + memory bandwidth now flow through the heartbeat into three scoring signals. Signal 5 (role affinity) scales continuously with bandwidth instead of flat memory tiers (M3 Ultra 800 GB/s → +25, M4 Max 546 GB/s → +18, M3 Pro 150 GB/s → +8.75). Signal 3 (queue depth) normalizes its penalty by each node's bandwidth share of the fleet median — a queue of 4 on a node 4× faster is treated like a queue of 1, so routing doesn't prematurely flip away from a fast node. Signal 4 (wait time) cold-starts from a bandwidth-derived throughput estimate when the latency store has no data yet, so day-one routing is correct on fresh fleets. Expected steady-state load distribution equals each node's bandwidth share of the fleet total — e.g. 67/33 for a Studio+MacBook pair. Two new env vars (default on): `FLEET_BANDWIDTH_AWARE_SCORING`, `FLEET_QUEUE_PENALTY_BANDWIDTH_NORMALIZE`. Falls back to the original memory-tier scoring when bandwidth is unknown, so older agents keep working unchanged. See `docs/plans/device-aware-scoring.md` for the math.
- **Chip + memory bandwidth in node heartbeats** — new `chip` (e.g. `"Apple M4 Max"`) and `memory_bandwidth_gbps` fields on `HardwareProfile` and `HeartbeatPayload`. Collector auto-detects at agent startup via `sysctl` (macOS), `/proc/cpuinfo + nvidia-smi` (Linux), or `wmic` (Windows). Bandwidth resolved from a lookup table covering M1–M4 Apple Silicon plus common discrete GPUs (RTX 20–50 series, A100, H100, L40). Surfaced in `/fleet/status` and `/dashboard/api/status` responses.
- **Opt-in debug request capture** — `FLEET_DEBUG_REQUEST_BODIES=true` appends every request's full lifecycle (client body, translated Ollama body, reconstructed response, tokens, timings, error, status) as one JSON line per request to `~/.fleet-manager/debug/requests.<date>.jsonl`. Crash-safe append-only JSONL; errors are always captured. `scripts/replay-debug-requests.py` lists/filters/replays captured requests (`--failures-only --since 1h`, `--request-id <id>`). `FLEET_DEBUG_REQUEST_RETENTION_DAYS=7` auto-prunes. Off by default — captures user prompts and responses verbatim, only enable on trusted fleets.

### Fixed

- **`/fleet/status` and `/dashboard/api/status` weren't exposing new HardwareProfile fields** — chip, memory_bandwidth_gbps, and arch were populated internally and used in scoring (traces confirmed role_affinity=25.0 for 800 GB/s nodes), but the JSON responses only serialized memory_total_gb + cores_physical. Dashboards and debug tooling couldn't see the inputs the scorer was using. Added regression test asserting all hardware fields appear in `/fleet/status`.
- **`qwen3-coder:30b-agent` at 131K ctx on 128 GB MacBooks triggered Jetsam OOM kills** under real Claude Code load (big prompts + 27 tools + multi-turn). Root cause: `OLLAMA_NUM_PARALLEL` defaults to 4 on macOS, pre-allocating KV cache for 4 × 131K tokens per slot = ~60 GB reserved on top of the 18 GB weights, leaving no headroom for generation-time growth. Documented the four-env-var combination that makes it reliable: `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KEEP_ALIVE=-1`. Observed result on an M4 Max 128 GB: 0% → 100% success on the `big_agentic` (55 msgs, 27 tools) stress pattern, ~500 MB → 14 GB free memory during sustained load. See `docs/troubleshooting.md` and `docs/operations-guide.md`.

- **Platform connection UX** — opt-in Settings-tab card to connect a node to `gotomy.ai`. Three new OSS routes: `GET /api/platform/status`, `POST /api/platform/connect`, `POST /api/platform/disconnect`. Paste operator token in the dashboard instead of SSHing into the node to edit YAML. Validates token via `GET /api/auth/me`, generates Ed25519 keypair (mode 0600), registers the node, persists state to `~/.fleet-manager/platform.json` (mode 0600). CLI + env var parity (`--platform-token` / `FLEET_NODE_PLATFORM_TOKEN`). No data is transmitted until a feature is opted into separately. Prerequisite for usage telemetry (next plan).
- **Platform additive extensions** — three new fields accepted by the platform, all non-breaking for older herd-node versions:
  - `device_info` on `POST /api/nodes/register` — hardware probe (OS, chip, CPU cores, memory, GPU, VRAM, hardware summary). Platform-specific probes for macOS (sysctl + system_profiler), Linux (/proc + nvidia-smi), Windows (wmic + nvidia-smi). Never raises — absent keys just mean "unknown", dashboard renders only what we report.
  - `success_count`, `error_count`, `error_breakdown` on daily telemetry entries. Error categorization (`model_not_found`, `context_too_long`, `vram_exceeded`, `timeout`, `permission_error`, `client_disconnected`, `bad_request`, `server_error`, `connection_error`, `other`) — free-form strings, can evolve without platform migration.
  - Signed platform heartbeats via new `platform_heartbeat.py` — Ed25519-signed POST to `/api/heartbeats` every 60 seconds. Powers the platform's Nodes-detail dashboard: current CPU%, memory, VRAM, queue depth, per-model queue depths, loaded models, 24h uptime, request counts since last beat. Signature contract (platform agreement 2026-04-20): sign canonical JSON of the body, then add `signature` field — no separate `raw_payload` envelope, eliminates re-serialization drift risk.
- **Platform telemetry — daily usage rollup emitter** — `--telemetry-local-summary` (opt-in, default off) builds per-model aggregates of yesterday's usage and POSTs to `gotomy.ai/api/telemetry/local-summary`. Daily at ~00:05 UTC + jitter (±10 min). 90-day rolling retention on the platform side. Structural privacy enforcement: payload keys are whitelisted and tests assert no drift. Tag transmission is a **separate** opt-in (`--telemetry-include-tags`) because tag values (e.g. `project:internal-audit`) can be mildly identifying. State file `~/.fleet-manager/telemetry_state.json` tracks last-sent day to avoid duplicates across restarts. 409 responses treated as idempotent success.
- **Platform HTTP client** — new shared `platform_client.py` with exponential-backoff retry (3 attempts, 1s/2s/4s) for 5xx and network errors. 401 fails fast (token revoked). 409 raises `TelemetryDuplicateError` so callers can treat as success. Reused across telemetry and future P2P features.
- **Benchmark-from-trace-data** — `benchmark_estimate.py` computes `tokens_per_sec` for platform registration from real latency observations (last 7 days, 100 samples). Falls back to hardware-derived estimate on first connect when no history exists. Also exposes `total_ram_gb`, `arch`, `platform` for richer registration.
- **Cryptography dependency** — `cryptography>=42.0.0` added for Ed25519 keypair generation used by platform connection.
- **`__version__` reads from package metadata** — `src/fleet_manager/__init__.py` now uses `importlib.metadata.version("ollama-herd")` so it can't drift from `pyproject.toml` again. Previously hardcoded at 0.3.0 while pyproject was 0.5.2.
- **Vision embedding service** — new `/api/embed-image` endpoint serves image embeddings via DINOv2 (384-dim, 85MB), SigLIP2 (768-dim, 90MB int8), CLIP (512-dim) via ONNX Runtime. Auto-downloads from HuggingFace, runs on port 11438 internally, proxied through router on 11435. `/api/embed` auto-routes vision model names (clip, dinov2, siglip) to the embedding service. Added to `/api/tags` for client discovery.
- **Priority model preloading** — on restart, loads most-used models first based on weighted scoring: `(24h_requests * 3) + (7d_daily_avg)`. Prevents primary models like gpt-oss:120b from being evicted by whatever model happens to be requested first.
- **Priority model refresh** — every 10 minutes, reloads priority models if evicted. Respects user intent: only refreshes models with requests in the last hour (so manual `ollama stop X` isn't overridden).
- **VRAM fallback priority protection** — blocks fallback from a high-priority model to a low-priority one. Request for gpt-oss:120b no longer silently routes to gemma3:27b.
- **`/api/version` endpoint** — returns Ollama version (compatibility) + `herd_version`. Health checks from Open WebUI, LangChain, etc. now work.
- **Connection failure tracking** — node agent tracks connection failures, heartbeat reports them, health check (#17) surfaces active failures and recoveries.
- **SSE watchdog** — dashboard auto-reconnects after 10s of silence, preventing stale state after network drops. The dashboard model list now updates live (model loads/unloads trigger card rebuild).
- **Vision model support** — new `VISION` model category for image understanding (image → text). 7 vision models in catalog: gemma3 (4B/12B/27B), llama3.2-vision (11B/90B), llava (7B/13B/34B), moondream, minicpm-v
- **OpenAI image format conversion** — OpenAI `image_url` content blocks auto-convert to Ollama's `images` field. HTTP image URLs auto-fetched and converted to base64.
- **Image token estimation** — `estimate_tokens()` accounts for image tokens (~150 per image) in both OpenAI and Ollama formats
- **`is_vision_model()` helper** — programmatic detection of vision-capable models
- **Vision in model recommender** — VISION included in default category priorities
- **Fleet Intelligence enrichment** — per-model traffic breakdown, per-node disk space, all health warnings (not just first 3), previous briefing continuity (500 chars), priority model status, 2 runtime bugs fixed (KeyError + AttributeError that were silently failing briefings)
- **Health checks** — now 18 (was 16): connection failures (#17), priority models (#18)
- **Model preloader in module table** — `node/embedding_models.py`, `node/embedding_server.py`, `server/model_preloader.py`
- **Route: `server/routes/embedding_compat.py`** — vision embedding endpoint
- **Config: `FLEET_VISION_EMBEDDING`**, `FLEET_VISION_EMBEDDING_TIMEOUT`, `FLEET_EMBEDDING_USE_COREML` (opt-in)
- **Silent model fallback detection** — `trace_store.get_silent_fallback_stats()` detects requests where `original_model != model` (VRAM fallback routed away from requested model). Fleet Intelligence now surfaces these as "SILENT FALLBACK in last 24h" — catches silent degradation where requests succeed but are served by the wrong model.
- **Static "Fleet offline" briefing** — when no nodes are online, Fleet Intelligence returns a static message explaining the state instead of trying to call an LLM that doesn't exist.

### Changed

- **Fleet Intelligence refresh intervals rebalanced** — backs off under load, refreshes faster when idle:
  - Very busy (>5 in-flight): 2 hours (was 30 min) — don't compete with real requests
  - Active (1-5 in-flight): 1 hour (was 1 hour) — unchanged
  - Idle (0 in-flight): 30 min (was 6 hours) — catch overnight silent failures
  - No nodes online: 1 hour static (was 1 hour LLM call)

### Fixed

- **CoreML provider triggered macOS TCC dialogs that froze the node overnight** — `CoreMLExecutionProvider` requested Neural Engine access on first inference, producing a permission dialog that blocked the Python process until someone dismissed it. Happened twice in 5 days (April 14 + 19). Fixed by defaulting to CPU-only inference (opt-in to CoreML via `FLEET_EMBEDDING_USE_COREML=true`). CPU is fast enough on M-series (~60ms/image).
- **`/api/generate` returned empty `response` field** — proxy converted generate to chat format internally, populated `message.content` but left `response` empty. Non-streaming clients got empty strings despite model generating tokens. Now both fields populated.
- **Fleet Intelligence briefings were silently failing** — `report.score` (AttributeError) and `overall['avg_latency_ms']` (KeyError) bugs in the prompt assembly caught by bare except, so briefings appeared to work but had no health/traffic content. Fixed.
- **Priority cache wasn't populated** — VRAM fallback couldn't read priority scores because preloader called `get_model_priority_scores()` directly instead of `get_cached_priorities()`. Also fixed Python import rebinding issue where `routing.py` imported `_priority_cache` by value and saw empty list after module rebind.
- **Dashboard model list didn't auto-update** — SSE fast-path signature only checked `node_id:status`, not the loaded model list. Model loads/unloads didn't trigger card rebuild.
- **Dashboard model counts** — now shows "Ollama Models: 3 loaded, 17 on disk | Services: 8 loaded" instead of misleading unified count.
- **Vision embedding tests** — added 7 edge case tests (HTTP URL fetch, HTTP fetch failure, empty base64, mixed data URI + HTTP, token estimation, vision model fallback). 507 tests total (was 445).
- **Stale references updated** — 445 → 507 tests, 17 → 18 health checks, 0.4.1 → 0.5.2 version across all skill files and docs

## [0.5.2] - 2026-04-13

### Fixed

- **Dashboard header stats going stale** — the in-place DOM update (added in 0.5.0 to prevent card flashing) was replacing header-stats innerHTML with only Nodes + Models Loaded, wiping Queued + Completed on every SSE tick. All 4 stats now use stable element IDs updated via textContent — no more innerHTML replacement race.

## [0.5.1] - 2026-04-09

### Fixed

- **Dashboard SSE stale data** — `connect()` ran before footer DOM elements existed, causing TypeError that prevented SSE event handlers from registering. Dashboard would show "Waiting for nodes..." and never update. Added null checks to onopen/onerror handlers.

## [0.5.0] - 2026-04-09

### Added

- **`/api/pull` endpoint** — Ollama-compatible model pulling through the router. Auto-selects best node by available memory, streams NDJSON progress, supports `node_id` targeting. Returns install instructions for non-Ollama models (mflux, DiffusionKit, MLX)
- **Smart benchmark system** — run benchmarks from the dashboard with two modes:
  - **Default**: benchmark currently loaded models
  - **Smart**: fill available memory with recommended models (prefers on-disk, then downloads), then benchmark everything
  - Dashboard UI: run button, mode selector, duration picker, model type checkboxes (LLM/Embeddings/Image gen), dual progress bars during pull phase, gradient color bar, elapsed in m:ss format
  - Real-time progress polling with live tok/s counter
  - Multimodal: benchmarks LLM chat, embeddings, and image generation simultaneously
- **Dynamic num_ctx management** (Issue #21) — 3-phase system to eliminate KV cache waste:
  - **Phase 1 (Observe)**: `GET /dashboard/api/context-usage` — per-model p50/p75/p95/p99 of total tokens (prompt+completion), 24h rolling max, utilization %, recommended ctx, savings estimate
  - **Phase 2 (Control)**: `FLEET_DYNAMIC_NUM_CTX` toggle + per-model `num_ctx_overrides` — router injects optimal num_ctx on cold loads, configurable via dashboard settings API
  - **Phase 3 (Auto-adjust)**: `ContextOptimizer` background task auto-calculates from 7-day traces, auto-initializes overrides on startup, queues Ollama restarts via heartbeat command channel
- **4 new benchmark charts** — Model Throughput (horizontal bar), Model Latency (grouped bar: latency vs TTFT), Model Performance Over Time (multi-line across runs), Node Utilization (CPU/MEM grouped bar)
- **Context waste health check** — WARNING when allocated context > 4× actual p99 total usage, with specific per-model recommended num_ctx values in the fix message
- **Heartbeat command channel** — router can send commands (e.g., `restart_ollama` with env overrides) to nodes via heartbeat response
- **Node agent Ollama restart** — `_restart_ollama()` processes commands from router, applies env overrides, gracefully restarts
- `POST /dashboard/api/benchmarks/start` — start benchmarks from dashboard
- `GET /dashboard/api/benchmarks/progress` — real-time benchmark progress
- `POST /dashboard/api/benchmarks/cancel` — cancel running benchmarks
- `GET /dashboard/api/context-usage` — per-model context utilization analysis
- **Fleet Intelligence briefing** — LLM-powered dashboard card that analyzes fleet health, context usage, and traffic using the fleet's own models. Adaptive refresh (30min when busy, 6h when idle), dismiss/refresh buttons, history persisted to SQLite
- **Dashboard visual enhancements:**
  - Gradient progress bars — smooth HSL color transition (green→yellow→red) on all CPU, memory, availability, and benchmark bars
  - Animated health score ring — conic-gradient fills from 0% to score on page load
  - Staggered card entry — node cards fade in sequentially with 60ms delay
  - Hover card lift — cards rise 2px with shadow on hover
  - Model badge colors by type — purple (LLM), blue (embed), orange (image), green (STT) with glow on hot models
  - In-place SSE updates — node card values update without rebuilding DOM (no more flashing)
- **Shared date range selector** on Trends, Model Insights, and Tags pages — presets (24h, 48h, 72h, 7d, 30d) + custom datetime-local picker in user's local timezone
- **Settings context management UI** — per-model table showing allocated ctx, p99 total tokens, utilization %, recommended ctx, savings %, with override input and Apply/Use Rec. buttons
- **Briefing history** — `GET /dashboard/api/briefing/history` reads from SQLite, viewable on Health page with "Generate New" button
- `GET /dashboard/api/briefing` — fleet intelligence briefing with adaptive caching
- `GET /dashboard/api/tags` + `/dashboard/api/tags/daily` — renamed from `/api/apps`
- 16 health checks total (up from 15 in 0.4.1)

### Fixed

- **`_request_tokens` encapsulation** (#4) — added `pop_token_counts()` and `pop_request_meta()` public methods on `StreamingProxy`, replaced all direct private dict access in route handlers
- **`asyncio.ensure_future` deprecated** (#5) — replaced with `asyncio.create_task()` in discovery.py
- **KV cache bloat fix message** (#16) — added Windows instructions alongside macOS/Linux
- **Benchmark chart x-axis** — shows date + time ("Apr 8 2:30 PM") instead of just date, so same-day runs are distinguishable
- **Smart benchmark skips cloud models** — filters out `:cloud` suffix models (API proxies that don't load locally)
- **Smart benchmark skips embedding/image models** for LLM category coverage — `nomic-embed-text` no longer blocks loading a general-purpose LLM
- **Context recommendation uses total tokens** — was using prompt-only p99 (caused truncation at 8K), now uses p99 of prompt+completion with 50% headroom and 24h rolling max floor
- **Node card flashing** — SSE updates now modify individual values in-place instead of rebuilding entire DOM every 2 seconds
- **Fleet Intelligence prompt** — lists real commands only (herd-node, curl /api/pull, Settings toggles), bans hallucinated commands

### Changed

- `benchmark_engine.py` extracted from `scripts/benchmark.py` — shared core logic between CLI and server-side runner
- `scripts/benchmark.py` is now a thin CLI wrapper importing from `benchmark_engine`
- Dashboard settings API accepts `dynamic_num_ctx`, `num_ctx_auto_calculate`, and `num_ctx_overrides`
- Settings GET response includes `context` section with all num_ctx state
- `StreamingProxy.pull_model()` accepts optional `progress_cb` callback for download progress tracking
- Benchmark `per_model_results` includes `model_type` field (llm/embed/image)
- **Apps → Tags rename** — dashboard tab, routes (`/dashboard/tags`), and APIs (`/dashboard/api/tags`) renamed for clarity. Old `/dashboard/apps` URLs still work (backwards compat)
- Trends, Models, Tags pages use `start_ts`/`end_ts` query params instead of just `hours`/`days`
- CLAUDE.md optimized from 246 → 143 lines (42% token reduction per turn)

## [0.4.1] - 2026-04-02

### Added

- **Thinking model support** — auto-detects thinking models (gpt-oss, deepseek-r1, qwq, phi-4-reasoning) and inflates `num_predict` by 4× (configurable via `FLEET_THINKING_OVERHEAD`) to prevent empty responses where reasoning consumes the entire token budget
- **Thinking-aware response headers** — `X-Thinking-Tokens`, `X-Output-Tokens`, `X-Budget-Used`, `X-Done-Reason` on non-streaming responses
- **Queue depth API** — `GET /fleet/queue` for client-side backoff decisions with `estimated_wait_ms`
- **KV cache bloat health check** — detects when `OLLAMA_NUM_PARALLEL` is too high by comparing VRAM vs estimated weights. Surfaces actionable fix
- **Stream reliability health checks** — "Client Disconnects" and "Incomplete Streams" dashboard cards with per-model breakdowns
- **Embedding model badges** — purple EMBED badges on Fleet Overview and Settings
- **Thinking models guide** — `docs/guides/thinking-models.md`
- 15 health checks total (up from 11 in 0.4.0)

### Fixed

- **Embeddings proxy routed to `/api/chat`** — embed requests went through the chat streaming pipeline. Now proxies directly to Ollama's `/api/embed` via the managed HTTP client with 600s timeout
- **Image/STT binary detection** — `shutil.which()` couldn't find mflux/DiffusionKit installed via `uv tool` because `~/.local/bin` wasn't in PATH. Added `_which_extended()` that checks common tool install locations
- **Client disconnects recorded as "completed"** — `GeneratorExit` now records as `client_disconnected`
- **Incomplete streams recorded as "completed"** — missing `done: true` now detected and recorded as `incomplete`
- **Error rate queries undercounting** — now counts all non-success statuses
- **LatencyStore unbounded memory** — capped to last 500 observations
- **N+1 query on cache refresh** — single SQL query with window functions
- **O(n) in-flight tracking** — dict keyed by request_id, all O(1)
- **Ollama non-streaming missing headers** — changed to explicit JSONResponse

### Changed

- `image_generation` and `transcription` default to `true` (was `false` — caused silent 503s after every restart)
- SSE stream and fleet/status include `embed_models` per node
- Queue EMBED badge color changed to purple

## [0.4.0] - 2026-04-02

### Added

- **Embeddings proxy** — `/api/embed` and `/api/embeddings` endpoints route embedding requests to the best available node via Ollama's native `/api/embed`. Supports both `input` (single or batch) and `prompt` (legacy) fields
- **OpenAI-compatible image generation** — `/v1/images/generations` wraps the fleet's image generation in OpenAI's standard API format. Works with the OpenAI SDK (`client.images.generate()`)
- **Image model discovery** — `/api/image-models` lists all image models across the fleet with backend type and which nodes have them. Image models also now appear in `/api/tags` and `/v1/models` responses
- **Request tagging for image and STT** — `metadata.tags` and `X-Herd-Tags` header now work on `/api/generate-image` and `/api/transcribe`. All four model types appear in the Apps dashboard tab
- **DeepSeek-V3 in model catalog** — 3 variants: `deepseek-v3:7b`, `deepseek-v3:32b`, `deepseek-v3:671b` (671B MoE, 404GB)
- **KV cache bloat health check** — detects when OLLAMA_NUM_PARALLEL is too high by comparing loaded model VRAM against estimated weight sizes. Surfaces actionable fix with exact commands
- **Stream reliability health checks** — "Client Disconnects" and "Incomplete Streams" cards on the Health dashboard with per-model breakdowns and active/resolved state
- **Stream reliability vitals** — `client_disconnects_24h` and `incomplete_streams_24h` counters on the Health page
- **Thinking model support** — auto-detects thinking models (gpt-oss, deepseek-r1, qwq, phi-4-reasoning) and inflates `num_predict` by 4× (configurable via `FLEET_THINKING_OVERHEAD`) with 1024 minimum to prevent empty responses where reasoning consumes the entire token budget
- **Thinking-aware response headers** — `X-Thinking-Tokens`, `X-Output-Tokens`, `X-Budget-Used`, `X-Done-Reason` on non-streaming responses for instant debugging of thinking model behavior
- **Queue depth API** — `GET /fleet/queue` returns lightweight queue depths, estimated wait time, and per-queue concurrency for client-side backoff decisions
- **Embedding model badges** — purple EMBED badges on Fleet Overview node cards and Settings page for models like nomic-embed-text
- **Expanded README** — comprehensive usage docs for all 4 model types with SDK examples, model comparison tables, discovery endpoints, and batch examples
- **Thinking models guide** — `docs/guides/thinking-models.md` with recommended settings, client-side tips, and debugging patterns
- **PyPI release process** documented in CLAUDE.md (build commands, credential location, changelog expectations)
- 32 new tests (444 total)

### Fixed

- **Client disconnects recorded as "completed"** — `GeneratorExit` (HTTP timeout, connection drop) was caught but silently marked successful. Now records as `client_disconnected` and increments `failed_count`
- **Incomplete streams recorded as "completed"** — when Ollama drops the connection without `done: true` (process death, OOM, TCP drop), the request was marked completed. Now detects missing `done: true` and records as `incomplete`
- **Embeddings proxy routing** — embed requests were going through `/api/chat` instead of Ollama's `/api/embed`. Now proxies directly to the correct Ollama endpoint via the managed HTTP client
- **Error rate queries undercounting** — `get_error_rates_24h` and `get_overall_stats_24h` only counted `status = 'failed'`, missing `client_disconnected` and `incomplete`. Now counts all non-success statuses
- **LatencyStore unbounded memory** — `get_percentile()` loaded all history into memory. Now capped to last 500 observations per (node, model) pair
- **N+1 query on cache refresh** — startup queried each (node, model) pair individually. Replaced with single SQL query using `ROW_NUMBER()` + `PERCENT_RANK()` window functions
- **O(n) in-flight tracking** — queue `in_flight` changed from list to dict keyed by request_id. All operations now O(1)

### Changed

- `/api/tags` response includes mflux, DiffusionKit, and Ollama native image models alongside LLM models
- `/v1/models` response includes image models with `type: "image"` in metadata
- SSE stream and `/fleet/status` include `embed_models` per node
- Queue EMBED type badge color changed to purple for consistency
- Embed proxy timeout increased to 600s to handle first-time model loading
- Health check count: 11 → 15 (added KV cache bloat, client disconnects, incomplete streams, stream reliability)

## [0.3.0] - 2026-03-30

### Added

- **Expanded image generation** — three backends through one endpoint
  - DiffusionKit backend: Stable Diffusion 3 Medium and SD 3.5 Large via MLX-native `diffusionkit-cli`
  - Ollama native backend: `x/z-image-turbo` and `x/flux2-klein` via standard `/api/generate`
  - mflux preferred over Ollama native to prevent LLM eviction from VRAM
  - 8 image models total across 3 backends
- **IMAGE model category** in model knowledge catalog with `is_image_model()` helper
- **DiffusionKit macOS 26 patch script** (`scripts/patch-diffusionkit-macos26.sh`)
- 19 ClawHub skills (5 new: `llama-llama3`, `mistral-codestral`, `phi-phi4`, `private-ai`, `local-coding`)
- 16 #1 keyword rankings on ClawHub
- ClawHub SEO optimization guide (`docs/guides/optimizing-skills-for-clawhub.md`)
- 34 new tests (412 total)

### Changed

- Queue type badge uses `classify_model()` from model knowledge instead of string heuristic — DiffusionKit models now correctly show `[IMAGE]` badge
- `/api/generate` detects Ollama native image models, forces non-streaming, decodes base64 PNG response
- `/api/generate-image` accepts Ollama native models alongside mflux, falls through to Ollama pipeline when needed
- Node collector detects DiffusionKit binary and reports SD3 models in heartbeat
- Image server generalized CLI builder handles both mflux and DiffusionKit flag differences

### Fixed

- mflux preferred over Ollama native to prevent LLM eviction from VRAM (was causing 500 errors on text requests)

## [0.2.0] - 2026-03-30

### Added

- **Multimodal routing** — 4 model types through one fleet
  - Image generation via mflux (`z-image-turbo`, `flux-dev`, `flux-schnell`)
  - Speech-to-text via Qwen3-ASR
  - Embeddings via Ollama (nomic-embed-text, mxbai-embed)
  - `request_type` field on InferenceRequest (text, image, stt, embed)
- **Dashboard multimodal badges** — `[TEXT]`, `[IMAGE]`, `[STT]`, `[EMBED]` on queue cards
- **Node capability badges** — `IMG z-image-turbo`, `STT qwen3-asr` on node cards
- **Transcription health check** and `/dashboard/api/transcription-stats` endpoint
- **Fleet status** includes image and transcription data per node
- **SSE events** include `image_models` and `stt_models` for real-time updates
- **Settings page** shows Image Models and STT Models rows with ports per node
- **Health vitals** grid adds Images (24h) and STT (24h) counters
- Image generation event tracking for health monitoring (last 200 events)
- 7 ClawHub skills published (ollama-herd, local-llm-router, ollama-load-balancer, gpu-cluster-manager, ollama-manager, ai-devops-toolkit, distributed-inference)
- Context protection for streaming requests
- VRAM-aware model fallback
- Request tagging with per-app analytics dashboard
- Model recommendations engine based on hardware capabilities
- Settings dashboard page with runtime toggles

### Changed

- Scoring engine updated with context fit signal (7th signal)
- Dashboard rewritten with 8 tabs (overview, trends, insights, apps, benchmarks, health, recommendations, settings)

## [0.1.0] - 2025-03-10

### Added

- Smart inference router with 7-signal scoring engine (thermal, memory fit, queue depth, wait time, role affinity, availability trend, context fit)
- OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- Ollama-compatible API (`/api/chat`, `/api/generate`, `/api/tags`, `/api/ps`)
- Zero-config node discovery via mDNS
- Node agent with heartbeat-based health reporting
- Per-node:model queues with dynamic concurrent workers
- Streaming proxy with auto-retry on node failure
- Model fallback chains for resilient routing
- Holding queue for requests when no nodes are immediately available
- Auto-pull for missing models
- Real-time web dashboard with SSE updates
- Benchmark tab for model performance comparison
- Capacity learner with 168-slot weekly behavioral model
- Meeting detection (macOS camera/microphone) for automatic pause
- App fingerprinting for resource-aware scheduling
- SQLite-backed latency store and request trace log
- Fleet status API (`/fleet/status`)
- JSONL structured logging
- LAN proxy for bridging localhost-bound Ollama to network
- Graceful drain on SIGTERM
- 212 tests with full async coverage

[0.4.1]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.4.1
[0.4.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.4.0
[0.3.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.3.0
[0.2.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.2.0
[0.1.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.1.0
