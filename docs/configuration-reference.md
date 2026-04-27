# Configuration Reference

All settings are configured via environment variables. No config files needed.

---

## Server Settings (`FLEET_` prefix)

### Network

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_HOST` | `0.0.0.0` | Bind address for the router |
| `FLEET_PORT` | `11435` | Listen port (Ollama default + 1) |

### Heartbeat Monitoring

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_HEARTBEAT_INTERVAL` | `5.0` | Seconds between heartbeat checks |
| `FLEET_HEARTBEAT_TIMEOUT` | `15.0` | Seconds before a node is marked degraded |
| `FLEET_HEARTBEAT_OFFLINE` | `30.0` | Seconds before a node is marked offline |

### Scoring Weights

These control the 7-signal scoring engine. Each incoming request is scored across all candidate nodes. Higher total score wins.

| Variable | Default | Signal | Description |
|----------|---------|--------|-------------|
| `FLEET_SCORE_MODEL_HOT` | `50.0` | Thermal | Points for model currently loaded in memory |
| `FLEET_SCORE_MODEL_WARM` | `30.0` | Thermal | Points for model loaded within last 30 min (likely OS-cached) |
| `FLEET_SCORE_MODEL_COLD` | `10.0` | Thermal | Points for model on disk but not recently used |
| `FLEET_SCORE_MEMORY_FIT_MAX` | `20.0` | Memory | Max points for comfortable memory headroom |
| `FLEET_SCORE_QUEUE_DEPTH_MAX_PENALTY` | `30.0` | Queue | Max penalty for saturated queues |
| `FLEET_SCORE_QUEUE_DEPTH_PENALTY_PER` | `6.0` | Queue | Penalty per queued request (pending + in-flight) |
| `FLEET_SCORE_WAIT_TIME_MAX_PENALTY` | `25.0` | Wait | Max penalty based on estimated wait time |
| `FLEET_SCORE_ROLE_AFFINITY_MAX` | `15.0` | Affinity | Max points for device-model role match |
| `FLEET_SCORE_ROLE_LARGE_THRESHOLD_GB` | `20.0` | Affinity | Models above this size prefer large machines |
| `FLEET_SCORE_ROLE_SMALL_THRESHOLD_GB` | `8.0` | Affinity | Models below this size prefer small machines |
| `FLEET_SCORE_AVAILABILITY_TREND_MAX` | `10.0` | Capacity | Max points for node availability (capacity learning) |
| `FLEET_SCORE_CONTEXT_FIT_MAX` | `15.0` | Context | Max points (or penalty) for context window fit vs estimated tokens |

**Tuning guidance:**
- Increase `SCORE_MODEL_HOT` to more aggressively prefer hot models over empty queues
- Decrease `SCORE_QUEUE_DEPTH_PENALTY_PER` to tolerate deeper queues before spreading load
- Increase `SCORE_ROLE_AFFINITY_MAX` to more strongly enforce "big models on big machines"
- Increase `SCORE_CONTEXT_FIT_MAX` to more strongly prefer nodes with larger context windows for long inputs

### Rebalancer

The rebalancer runs continuously, moving pending requests from overloaded queues to better alternatives.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_REBALANCE_INTERVAL` | `5.0` | Seconds between rebalancer scans |
| `FLEET_REBALANCE_THRESHOLD` | `4` | Queue depth that triggers rebalancing |
| `FLEET_REBALANCE_MAX_PER_CYCLE` | `3` | Max requests moved per rebalancer cycle |

### Auto-Pull

When a requested model doesn't exist on any fleet node, the router can automatically pull it onto the best available node and serve the request seamlessly.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_AUTO_PULL` | `true` | Auto-pull missing models onto the best available node |
| `FLEET_AUTO_PULL_TIMEOUT` | `300.0` | Max seconds to wait for a model pull to complete (5 min) |

The router selects the online node with the most available memory that can fit the estimated model size. Concurrent pulls of the same model are deduplicated. Disable with `FLEET_AUTO_PULL=false` to get a 404 for missing models instead.

### Context Protection

Prevents clients from triggering expensive Ollama model reloads by sending `num_ctx` in request options. When Ollama receives a `num_ctx` different from the loaded model's context window, it unloads and reloads the entire model — which can cause multi-minute hangs or deadlocks on large models.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_CONTEXT_PROTECTION` | `strip` | How to handle client `num_ctx` values: `strip`, `warn`, or `passthrough` |

Modes:
- **`strip`** (default): Remove `num_ctx` from requests when it's ≤ the loaded model's context window. Logs an info message. If `num_ctx` exceeds the loaded context, it's preserved with a warning (client genuinely needs more context).
- **`warn`**: Keep `num_ctx` but log warnings about potential reload triggers.
- **`passthrough`**: No intervention — pass `num_ctx` through to Ollama as-is.

Only applies to Ollama-format requests (`/api/chat`, `/api/generate`). OpenAI-format requests don't have a `num_ctx` equivalent.

### Pre-Warm

Pre-warm proactively loads models on runner-up nodes before they're needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_PRE_WARM_THRESHOLD` | `3` | Winner's queue depth that triggers pre-warm on runner-up |
| `FLEET_PRE_WARM_MIN_AVAILABILITY` | `0.60` | Minimum node availability score to receive pre-warm (capacity learning) |

### Retry & Reaper

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_MAX_RETRIES` | `2` | Max retry attempts on node failure (before first chunk) |
| `FLEET_STALE_TIMEOUT` | `600.0` | Seconds before in-flight requests are considered zombied and reaped (10 min) |

### Device-Aware Scoring

When on (the default), the scorer uses each node's detected chip and memory bandwidth to rank candidates — an M3 Ultra Mac Studio (800 GB/s) outscores a MacBook Pro (300 GB/s) for big models even when both have plenty of free RAM. Nodes with unknown bandwidth fall back to the original memory-tier behaviour, so older agents and unrecognized chips keep working without any operator action.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_BANDWIDTH_AWARE_SCORING` | `true` | Signal 5 (role affinity) scales with memory bandwidth instead of flat memory tiers |
| `FLEET_QUEUE_PENALTY_BANDWIDTH_NORMALIZE` | `true` | Signal 3 (queue depth) divides penalty by each node's bandwidth share of the fleet — a queue of 4 on a 4× faster node is treated like a queue of 1 |

See `docs/plans/device-aware-scoring.md` for the math and expected
steady-state distribution under load (roughly proportional to each
node's bandwidth share of the fleet).

### Debug Request Capture

**DISABLED BY DEFAULT.** When enabled on an internal fleet, every inference request's full lifecycle is appended as one JSON line per request to `~/.fleet-manager/debug/requests.<date>.jsonl` on the router. Captures: original client body, translated Ollama body, reconstructed response, prompt/completion tokens, latency, TTFT, error, status, tags. Intended for reproducing failures on trusted fleets where you own every caller. **Never enable on a public gateway** — this records user prompts and responses verbatim.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DEBUG_REQUEST_BODIES` | `false` | Set to `true` to enable full request/response capture |
| `FLEET_DEBUG_REQUEST_RETENTION_DAYS` | `7` | Auto-prune daily log files older than this. `0` disables pruning |

Replay captured requests with `scripts/replay-debug-requests.py` — e.g. `--list`, `--failures-only --since 1h`, or `--request-id <id>`.

### Dynamic Context Management

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DYNAMIC_NUM_CTX` | `false` | Enable dynamic num_ctx injection on requests. When enabled, the router injects per-model num_ctx overrides to reduce KV cache waste |
| `FLEET_NUM_CTX_AUTO_CALCULATE` | `false` | Auto-calculate optimal num_ctx from trace data. The context optimizer analyzes p99 total token usage (prompt + completion) and updates overrides every 5 minutes |

Per-model overrides are set at runtime via `POST /dashboard/api/settings` with `{"num_ctx_overrides": {"model-name": 16384}}`. When `dynamic_num_ctx` is enabled and `num_ctx_auto_calculate` is true, the optimizer auto-initializes overrides from 7-day trace history on startup.

**Tuning guidance:**
- Enable `FLEET_DYNAMIC_NUM_CTX` when a model's allocated context far exceeds actual usage (check `/dashboard/api/context-usage`)
- The recommended context is p99 of total tokens (prompt + completion) with 50% headroom, rounded to next power of 2
- Overrides only affect cold loads — already-loaded models keep their current context until Ollama restarts

### Image Generation

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_IMAGE_GENERATION` | `false` | Enable `/api/generate-image` endpoint for mflux routing |
| `FLEET_IMAGE_TIMEOUT` | `120.0` | Max seconds to wait for image generation |

### Transcription (Speech-to-Text)

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_TRANSCRIPTION` | `false` | Enable `/api/transcribe` endpoint for Qwen3-ASR routing |
| `FLEET_TRANSCRIPTION_TIMEOUT` | `300.0` | Max seconds to wait for transcription |

### Vision Embeddings (DINOv2, SigLIP2, CLIP)

Serves image embeddings via `/api/embed-image` on `:11438` internally (proxied through the router on `:11435`).  Used for frame deduplication, image similarity, and visual search.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_VISION_EMBEDDING` | `true` | Enable `/api/embed-image` endpoint |
| `FLEET_VISION_EMBEDDING_TIMEOUT` | `30.0` | Max seconds to wait for embedding generation |
| `FLEET_EMBEDDING_USE_COREML` | `false` | Opt-in to CoreMLExecutionProvider on macOS. **Not recommended** — can trigger macOS TCC permission dialogs that block the Python process indefinitely. CPU inference is fast enough (~60ms/image on M-series). |

### Thinking Models

Thinking models (deepseek-r1, gpt-oss, qwq) split `num_predict` between internal reasoning and visible output. Small budgets result in empty responses. The router auto-detects thinking models and inflates the budget.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_THINKING_OVERHEAD` | `4.0` | Multiply client's `num_predict` by this factor for thinking models |
| `FLEET_THINKING_MIN_PREDICT` | `1024` | Minimum `num_predict` sent to Ollama for thinking models (floor) |

Only applies when the client explicitly sets `num_predict` / `max_tokens`. If omitted, Ollama uses the model's default. See [Thinking Models Guide](guides/thinking-models.md).

### mDNS Discovery

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_MDNS_SERVICE_TYPE` | `_fleet-manager._tcp.local.` | Zeroconf service type |
| `FLEET_MDNS_SERVICE_NAME` | `Fleet Manager Router` | Advertised service name |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DATA_DIR` | `~/.fleet-manager` | Directory for SQLite databases and logs |
| `FLEET_ENV_FILE` | `~/.fleet-manager/env` | Path to the env file loaded at process start. Shell env always wins; this is a fallback for non-interactive shells (nohup, launchd, Bash subshells that don't source `~/.zshrc`). Plain `KEY=value` syntax. See `docs/examples/fleet-env.example`. |

### Model Preloader + Pins

Pinned models are always kept hot — if evicted, the preloader reloads them at its next refresh cycle. Env-level pins are fleet-wide; per-node pins live in `<data_dir>/pinned_models.json` and are toggled from the Recommendations dashboard. See `server/model_preloader.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_PINNED_MODELS` | `""` | Comma-separated fleet-wide pin list, e.g. `"gpt-oss:120b,gemma3:27b"`. Per-node pins from the dashboard union with these. |
| `FLEET_MODEL_PRELOAD_MAX_COUNT` | `3` | Total-slots budget the preloader will use. Should be ≤ Ollama's hot cap (3 on macOS 0.20.4) to avoid self-inflicted LRU thrash. |
| `FLEET_DISABLE_MODEL_PRELOADER` | `false` | Disable the preloader entirely — models load on demand on first request. |

### Context Hygiene Compactor

Shrinks bloated `tool_result` blocks (Read/Bash/WebFetch) before main-model inference. Off by default. See `server/context_compactor.py` and `docs/research/` for the design doc.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_CONTEXT_COMPACTION_ENABLED` | `false` | Enable the compactor middleware on `/v1/messages`. |
| `FLEET_CONTEXT_COMPACTION_BUDGET_TOKENS` | `20000` | Requests under this token count pass through unchanged. |
| `FLEET_CONTEXT_COMPACTION_MODEL` | `gpt-oss:120b` | Default curator model. Used when no better candidate is hot + idle. |
| `FLEET_CONTEXT_COMPACTION_PRESERVE_TURNS` | `3` | Recent turns passed through verbatim — never compacted. |
| `FLEET_CONTEXT_COMPACTION_CURATOR_TIMEOUT_S` | `60.0` | Per-summary timeout; failure → fail-open (original content passes through). |
| `FLEET_CONTEXT_COMPACTION_IDLE_WINDOW_S` | `120` | How far back to look for "is this candidate busy?". Set to `0` to always use the default curator model. An idle pinned model scores highest; a busy one gets penalized. |
| `FLEET_CONTEXT_COMPACTION_CURATOR_MIN_PARAMS_B` | `7.0` | Minimum model size (billions) to be considered a viable curator. Below this, summary quality is unreliable — we'd rather skip compaction than use a tiny model. |

### MLX Backend (Apple Silicon)

Opt-in backend that runs `mlx_lm.server` as an independent subprocess alongside Ollama, letting a single node serve models too large for Ollama's 3-model hot cap. Requires `./scripts/setup-mlx.sh` (installs pinned `mlx-lm==0.31.3` + applies the `--kv-bits` patch). See [`docs/guides/mlx-setup.md`](guides/mlx-setup.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_MLX_ENABLED` | `false` | Enable the MLX proxy on the router. |
| `FLEET_MLX_URL` | `http://127.0.0.1:11440` | Where `mlx_lm.server` listens. |
| `FLEET_MLX_MAX_QUEUE_DEPTH` | `10` | Admission control: max queued requests (1 in-flight + this many waiting) against the MLX backend. Enforced by the router's `MlxProxy` via an asyncio semaphore — unrelated to Ollama's own caps. Overflow returns HTTP 503 + `Retry-After`. Default bumped from 3 → 10 on 2026-04-24 after real Claude Code sessions (main turn + `/compact` + tool expansions + any concurrent production scripts) regularly overflowed at 3. |
| `FLEET_MLX_MAX_INFLIGHT_PER_MODEL` | `1` | Concurrent in-flight requests cap **per MLX model**. Default `1` matches historical strict-serialization behavior. `mlx_lm.server v0.31.3` actually supports batching multiple requests in one inference pass (verified 2026-04-27 — 3 concurrent requests batched in wall-time ≈ max-of-individual instead of sum), so `2` or `3` captures real throughput on bursty workloads (multiple Claude Code sessions, parallel tool calls). Default stays at `1` because each in-flight request carries its own KV cache state — concurrent 100K-token prefills multiply memory pressure, and concurrent paths in mlx_lm.server have been historically bug-prone. Tune up only when you've measured headroom. Values <1 are clamped to 1 (typo-safe). See `docs/research/mlx-lm-stability-and-concurrency.md`. |
| `FLEET_MLX_RETRY_AFTER_SECONDS` | `10` | `Retry-After` header value when admission control rejects. |
| `FLEET_MLX_READ_TIMEOUT_S` | `1800.0` | HTTP read timeout for requests to `mlx_lm.server`, in seconds. Applies per-byte-chunk because the proxy internally streams even "non-streaming" calls (via `completions_non_streaming`'s stream-and-collect path) so the timer resets on every token. A large value bounds a truly-stuck server without cutting off legitimate long-prefill work on big models like the 480B. |
| `FLEET_MLX_WALL_CLOCK_TIMEOUT_S` | `300.0` | Total wall-clock bound per MLX request from admission → final byte, in seconds. Catches wedged-request syndrome: `mlx_lm.server` at long context has been observed to enter internal decoding loops where it keeps emitting tokens slowly but never hits a stop condition. The per-byte `read_timeout` doesn't catch that (bytes ARE flowing). When this limit fires, the admission slot is released and the route returns HTTP 413 with a `try /compact` hint. **Tune up to `600` if you run long Claude Code sessions (2000+ messages) on 80B-class MoE models**: Qwen3-Coder-Next-4bit on that workload routinely takes 200-245s per turn, so 300s hits edge-case timeouts. See `server/mlx_proxy.py::MlxWallClockTimeoutError`. |

### Anthropic Messages API Compat (Claude Code)

See [docs/guides/claude-code-integration.md](guides/claude-code-integration.md) for the full setup walkthrough.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_ANTHROPIC_MODEL_MAP` | `{"default":"qwen3-coder:30b","claude-opus-4-7":"qwen3:32b","claude-sonnet-4-6":"qwen3-coder:30b","claude-sonnet-4-5":"qwen3-coder:30b","claude-haiku-4-5":"qwen3:14b"}` | JSON map of `claude-*` model id → local Ollama model. Always include a `"default"` key. Real Ollama model names pass through unchanged. |
| `FLEET_ANTHROPIC_REQUIRE_KEY` | `false` | If true, validate the `x-api-key` header against `FLEET_ANTHROPIC_API_KEY`. Default off — local trust boundary like the rest of the router. |
| `FLEET_ANTHROPIC_API_KEY` | `""` | Shared secret for `/v1/messages` when `require_key` is true. Set the matching value as `ANTHROPIC_AUTH_TOKEN` in Claude Code. |
| `FLEET_ANTHROPIC_DEFAULT_MAX_TOKENS` | `4096` | Used when the client omits `max_tokens` from the request. |
| `FLEET_ANTHROPIC_TOOL_SCHEMA_FIXUP` | `"inject"` | Workaround for Qwen3-Coder's long-context tool-call bug ([llama.cpp#20164](https://github.com/ggml-org/llama.cpp/issues/20164)) — at ~30K tokens, tool-call generation starts silently omitting optional parameters and looping. Modes: `"off"` (don't touch schemas), `"promote"` (only promote properties that already have `default` — currently a no-op on Claude Code, which doesn't emit defaults), `"inject"` (use the built-in Claude Code defaults table + promote; the actual fix). See `docs/research/why-claude-code-degrades-at-30k.md` for the full reasoning. |
| `FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_TRIGGER_TOKENS` | `100000` | Mechanical tool-result clearing — the cheap first layer of context management, matching hosted Claude's [Context Editing API](https://platform.claude.com/docs/en/build-with-claude/context-editing). When the estimated prompt exceeds this threshold, older `tool_result` bodies get replaced with a short placeholder before the request reaches the model — no LLM call, microsecond-scale. Set to `0` to disable and rely solely on the LLM-based compactor. |
| `FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_KEEP_RECENT` | `3` | How many most-recent `tool_result` blocks to preserve verbatim when clearing fires. Matches hosted Claude's observed behavior of keeping the last 3–5 exchanges intact. `tool_use` blocks (the model's own output) are never cleared regardless of this setting — conversation structure is always preserved. |
| `FLEET_CONTEXT_COMPACTION_FORCE_TRIGGER_TOKENS` | `150000` | Session-level rescue for the LLM-based compactor. If the prompt is still larger than this *after* Layer 1 mechanical clearing, pass `force_all=True` to the compactor so it summarises EVERY `tool_result` regardless of the per-strategy `min_bloat` gates. Matches Anthropic's default compaction trigger. Set to `0` to disable and rely only on the per-block thresholds. |
| `FLEET_ANTHROPIC_MAX_PROMPT_TOKENS` | `180000` | Hard pre-inference cap. If the prompt is STILL above this after both Layer 1 clearing and Layer 2 compaction (with force-all), the request is refused with HTTP 413 before it ever reaches the model. Better to surface the error to the client — which can run `/compact` and resubmit — than to let the request wedge for 5+ minutes on MLX prefill. Set to `0` to disable. `180000` leaves headroom under Qwen3-Coder-Next's 256K native context while staying inside effective-context bounds. |
| `FLEET_ANTHROPIC_TOOLS_DENY` | `""` | Comma-separated list of Claude Code tool names to strip from every request before translation to Ollama/MLX (e.g. `"WebSearch,WebFetch,NotebookEdit"`). Saves ~200-600 prompt tokens per turn depending on which tools are removed. Pair with client-side `permissions.deny` in `.claude/settings.json` for belt-and-suspenders — client-side only blocks execution, this removes the definitions from the wire entirely. Empty string disables. Names are matched exactly (case-sensitive). |
| `FLEET_ANTHROPIC_SIZE_ESCALATION_TOKENS` | `0` | Token threshold above which a request auto-routes to a larger/stronger model. Use with `FLEET_ANTHROPIC_SIZE_ESCALATION_MODEL`. Example: set to `50000` to escalate long prompts to MLX while short ones stay on Ollama. `0` disables. |
| `FLEET_ANTHROPIC_SIZE_ESCALATION_MODEL` | `""` | Local model name to escalate to when a request exceeds `FLEET_ANTHROPIC_SIZE_ESCALATION_TOKENS` (e.g. `"mlx:mlx-community/Qwen3-Coder-Next-4bit"`). Empty disables escalation. Bypasses the normal `FLEET_ANTHROPIC_MODEL_MAP` lookup for over-threshold requests only. |

---

## Node Settings (`FLEET_NODE_` prefix)

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_NODE_ID` | *(hostname)* | Unique node identifier (auto-detected from hostname if empty) |
| `FLEET_NODE_OLLAMA_HOST` | `http://localhost:11434` | URL of the local Ollama instance |
| `FLEET_NODE_ROUTER_URL` | *(auto-discover)* | Router URL; set to skip mDNS discovery |
| `FLEET_NODE_HEARTBEAT_INTERVAL` | `5.0` | Seconds between heartbeats to the router |
| `FLEET_NODE_POLL_INTERVAL` | `5.0` | Seconds between local metric collection cycles |
| `FLEET_NODE_ENABLE_CAPACITY_LEARNING` | `false` | Enable adaptive capacity learning (see [Adaptive Capacity](adaptive-capacity.md)) |
| `FLEET_NODE_DATA_DIR` | `~/.fleet-manager` | Directory for capacity learning state and logs |
| `FLEET_NODE_MDNS_SERVICE_TYPE` | `_fleet-manager._tcp.local.` | Zeroconf service type to search for |

### MLX Backend — Node side (Apple Silicon only)

Counterparts to the router's MLX vars. The node agent manages `mlx_lm.server`'s lifecycle (spawn, health-check, auto-restart on crash) when `AUTO_START` is true. Prerequisite: `./scripts/setup-mlx.sh` has been run.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_MLX_ENABLED` | `false` | Node polls `mlx_lm.server /v1/models` and merges loaded models into heartbeat with `mlx:` prefix. |
| `FLEET_NODE_MLX_URL` | `http://127.0.0.1:11440` | Local MLX server URL (should match `FLEET_MLX_URL` on the router for the same node). |
| `FLEET_NODE_MLX_AUTO_START` | `false` | Spawn `mlx_lm.server` on agent start; supervise + restart on crash. |
| `FLEET_NODE_MLX_AUTO_START_MODEL` | `""` | HF repo id or local path (e.g. `mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit`). Required when `AUTO_START` is true. |
| `FLEET_NODE_MLX_KV_BITS` | *(unset — f16)* | KV cache quantization: `4` or `8` (matches `OLLAMA_KV_CACHE_TYPE=q8_0`). Requires the patch from `setup-mlx.sh` — supervisor preflights for this and fails fast with a remediation hint if the patch is missing. |
| `FLEET_NODE_MLX_PROMPT_CACHE_BYTES` | `17179869184` | Max prompt-cache size in bytes (default 16 GiB). |
| `FLEET_NODE_MLX_DRAFT_MODEL` | `""` | Speculative-decoding draft model (HF repo id or local path). Must share the main model's tokenizer family — e.g. `mlx-community/Qwen3-1.7B-4bit` for Qwen3-main. Empty disables. **Currently blocked by [mlx-lm#1081](https://github.com/ml-explore/mlx-lm/issues/1081)**: every request fails with `ArraysCache` cache-type error; re-enable when upstream ships a fix. See `docs/issues/mlx-speculative-decoding-blocked.md`. |
| `FLEET_NODE_MLX_NUM_DRAFT_TOKENS` | `4` | How many tokens the draft model proposes per step. 3–4 is typical; higher increases acceptance opportunity but wastes more on rejections. Only used when `FLEET_NODE_MLX_DRAFT_MODEL` is set. |
| `FLEET_NODE_MLX_SERVERS` | `""` | **Multi-MLX-server** JSON list: each entry spawns one `mlx_lm.server` subprocess. Overrides the single-server fields above when set. Format: `'[{"model":"<hf-id>","port":<int>,"kv_bits":<0\|4\|8>},...]'`. Required keys per entry: `model`, `port`. Optional: `kv_bits`, `prompt_cache_size`, `prompt_cache_bytes`, `draft_model`, `num_draft_tokens`. Example: `'[{"model":"mlx-community/Qwen3-Coder-Next-4bit","port":11440,"kv_bits":8},{"model":"mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit","port":11441,"kv_bits":8}]'`. Duplicate ports are deduplicated (second entry dropped with ERROR log). See [`docs/issues/multi-mlx-server-support.md`](issues/multi-mlx-server-support.md). |
| `FLEET_NODE_MLX_BIND_HOST` | `127.0.0.1` | Host that each `mlx_lm.server` binds to. Leave at default for single-node deploys where the router and MLX live on the same machine. Set to `0.0.0.0` to expose MLX to the LAN so a router on another machine can reach it — required for multi-node MLX aggregation. Note: `mlx_lm.server` has no auth, so `0.0.0.0` assumes a trusted LAN. |
| `FLEET_NODE_MLX_MEMORY_HEADROOM_GB` | `10.0` | Free-RAM headroom required before the supervisor will spawn each MLX server. The gate checks `(estimated_weights_size + headroom) <= psutil.virtual_memory().available` and skips with `memory_blocked` status if it won't fit. Set to `0.0` to disable the gate. The estimated size is computed by walking the HuggingFace cache on disk; if the model isn't cached, the gate proceeds (operator knows best). |

### Platform Connection (opt-in — see [API Reference](api-reference.md#platform-connection))

Connect a node to `gotomy.ai` for future P2P compute sharing, usage dashboards, and credit tracking. Local fleet routing works without a platform connection. When a token is configured, the node auto-connects on startup (equivalent to pasting the token in the dashboard's Settings tab).

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_PLATFORM_TOKEN` | *(none)* | Operator token from `gotomy.ai/web/`. Starts with `herd_`. |
| `FLEET_NODE_PLATFORM_URL` | `https://gotomy.ai` | Platform URL (override for local testing) |
| `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY` | `false` | Opt in to daily usage rollups (per-model counts, tokens, latency, errors). Sent at ~00:05 UTC + jitter. 90 days rolling retention on the platform side. Never sends prompts or completion text. |
| `FLEET_NODE_TELEMETRY_INCLUDE_TAGS` | `false` | **Second opt-in** on top of `TELEMETRY_LOCAL_SUMMARY`: include per-tag request counts. Off by default because tag values like `project:internal-audit` can be mildly identifying. |

Once connected, the node also sends signed heartbeats to the platform every 60 seconds. Heartbeats power the platform's Nodes-detail dashboard (CPU, memory, VRAM, queue depth, loaded models, 24h uptime). No opt-in flag — heartbeats are automatic when the platform is connected. The heartbeat send is fire-and-forget (10s timeout, 1 attempt) so platform slowness can't back up into the node.

---

## Logging Settings

These control the JSONL structured logging system (see [Operations Guide](operations-guide.md#structured-logging)).

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_LOG_LEVEL` | `DEBUG` | Log level written to JSONL file |
| `FLEET_CONSOLE_LOG_LEVEL` | `INFO` | Log level printed to console (Rich handler) |

---

## Example: Full Custom Configuration

```bash
# Server with tuned scoring
export FLEET_PORT=11435
export FLEET_SCORE_MODEL_HOT=60
export FLEET_SCORE_QUEUE_DEPTH_PENALTY_PER=8
export FLEET_REBALANCE_THRESHOLD=3
export FLEET_MAX_RETRIES=3
uv run herd

# Node with capacity learning enabled
export FLEET_NODE_ROUTER_URL=http://macstudio.local:11435
export FLEET_NODE_ENABLE_CAPACITY_LEARNING=true
uv run herd-node
```
