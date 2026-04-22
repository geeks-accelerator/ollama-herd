# Claude Code Integration

Run [Claude Code](https://claude.com/claude-code) against your local fleet. Same agentic CLI, but inference happens on your hardware via ollama-herd routing to a local coding model like `qwen3-coder:30b` or `qwen3:32b`.

This works because ollama-herd exposes a native **Anthropic Messages API** (`/v1/messages`), so Claude Code's `ANTHROPIC_BASE_URL` can point straight at the herd router — no LiteLLM sidecar, no OpenAI-format proxy.

## TL;DR

```bash
herd                                          # start the router
herd-node                                     # start a node (any machine with Ollama)
ollama pull qwen3-coder:30b                   # ~17 GB; choose any coding model

export ANTHROPIC_BASE_URL=http://localhost:11435
export ANTHROPIC_AUTH_TOKEN=dummy             # any non-empty value
claude
```

That's it. Claude Code now talks to your local model with full tool use, streaming, and the standard agentic loop.

## What ollama-herd does for Claude Code

The router accepts requests in Anthropic Messages format (`/v1/messages` and `/v1/messages/count_tokens`), translates them to Ollama's wire format, runs them through the same scoring + queue + trace pipeline as every other route, and translates the response back to Anthropic SSE event sequences.

| Anthropic concept | Translated to | Notes |
|---|---|---|
| `messages[].content` blocks (text, image, tool_use, tool_result) | Ollama `messages` (string `content` + `images[]` + `tool_calls[]` + `role:"tool"` for results) | Order preserved; `thinking` blocks dropped on input |
| `system` (string or text-block array) | Prepended `role:"system"` message | Both forms supported |
| `tools[]` with `input_schema` | Ollama `tools[]` with `parameters` | JSON schema passes through |
| `tool_choice: auto` / `none` / `any` / `tool` | `auto` / strip / system-prompt nudge / system-prompt nudge | `any` and `tool` are best-effort since Ollama doesn't natively force tool calls |
| Streaming SSE | `message_start` → `content_block_start/delta/stop` → `message_delta` → `message_stop` | Full event protocol; tool calls open new content blocks mid-stream |
| `count_tokens` | tiktoken `cl100k` estimate | Best-effort; budget-gating only, not billing |

## Model mapping

Claude Code sends model IDs like `claude-sonnet-4-5`. Ollama-herd maps them to local Ollama models via `FLEET_ANTHROPIC_MODEL_MAP`:

```bash
# Default (no env var needed):
#   claude-opus-4-7    → qwen3:32b
#   claude-sonnet-4-6  → qwen3-coder:30b
#   claude-sonnet-4-5  → qwen3-coder:30b
#   claude-haiku-4-5   → qwen3:14b
#   default (anything claude-*) → qwen3-coder:30b

# Override:
export FLEET_ANTHROPIC_MODEL_MAP='{
  "default": "qwen3-coder:30b",
  "claude-opus-4-7": "deepseek-r1:70b",
  "claude-sonnet-4-5": "qwen3-coder:30b",
  "claude-haiku-4-5": "qwen3:14b"
}'
```

You can also pass a real Ollama model name (e.g. `"model": "qwen3-coder:30b"`) and ollama-herd will pass it through unchanged.

### Running more than 3 models with MLX backend

Ollama on macOS caps concurrent hot models at 3 ([docs/issues.md](../issues.md)).
To keep a 4th model (typically an opus-tier giant like `qwen3-coder:480b`) hot,
route it through the MLX backend — an independent `mlx_lm.server` process
that has its own memory budget, separate from Ollama's.

Setup on the node (single-command everything-auto-starts path):

```bash
# 1. Install mlx-lm
uv tool install mlx-lm  # or: pip install mlx-lm

# 2. Pull an MLX-quantized model (helper command)
herd mlx pull mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit

# 3. Tell herd-node to auto-start and supervise mlx_lm.server
export FLEET_NODE_MLX_ENABLED=true
export FLEET_NODE_MLX_AUTO_START=true
export FLEET_NODE_MLX_AUTO_START_MODEL=mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit
export FLEET_NODE_MLX_KV_BITS=8  # optional — requires patched mlx_lm.server

# 4. Tell the router it can forward to MLX
export FLEET_MLX_ENABLED=true
export FLEET_MLX_URL=http://localhost:11440

# 5. Map opus to the MLX model (note the mlx: prefix)
export FLEET_ANTHROPIC_MODEL_MAP='{"default":"qwen3-coder:30b",
  "claude-sonnet-4-5":"qwen3-coder:30b",
  "claude-opus-4-7":"mlx:mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"}'
```

Restart `herd` and `herd-node`. The router now routes opus-tier requests to
`mlx_lm.server` and sonnet/haiku to Ollama. Response headers expose which
backend served each request: `X-Fleet-Node: mlx-local` + `X-Fleet-Backend: mlx`
for MLX, `X-Fleet-Node: <your-node>` for Ollama.

See [`docs/plans/mlx-backend-for-large-models.md`](../plans/mlx-backend-for-large-models.md)
for the architecture, and [`docs/experiments/mlx-lm-q8kv-benchmark.md`](../experiments/mlx-lm-q8kv-benchmark.md)
for the benchmark showing MLX + `--kv-bits 8` ties Ollama's tuned llama.cpp
(320ms vs 306ms median TTFT on a 25-turn Claude Code workload).

### Recommended models for Claude Code

Claude Code is heavily agentic — it uses tools constantly (Read, Edit, Bash, Grep, Glob, etc.). Model quality for *tool use* matters more than raw chat quality.

| Model | Tool use | Notes |
|---|---|---|
| `qwen3-coder:30b` | Excellent | Best general-purpose pick. ~17GB, 256K context |
| `qwen3:32b` | Excellent | Strong reasoning, good tool use. ~19GB |
| `glm-4.7-flash:latest` | Good | Fast, smaller |
| `devstral-small-2:24b` | Good | Coding-tuned |
| `codestral:22b` | Poor | Doesn't reliably emit Ollama `tool_calls` format — avoid for agentic use |
| `deepseek-r1:14b` | Poor | Thinking-focused, weak tool calling |

If a turn comes back with no tool call when one was needed, it's almost always a model-quality issue, not an integration bug.

## Verify it's working

```bash
# 1. Non-streaming sanity check
curl -s http://localhost:11435/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: dummy" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 80,
    "messages": [{"role": "user", "content": "say hi"}]
  }' | jq .

# Expected: { "id": "msg_...", "type": "message", "content": [{"type":"text","text":"..."}], "stop_reason": "end_turn", ... }

# 2. Streaming
curl -sN http://localhost:11435/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"count to 3"}]}'

# Expected: SSE events — message_start, content_block_*, message_delta, message_stop

# 3. Token count
curl -s http://localhost:11435/v1/messages/count_tokens \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":1,"messages":[{"role":"user","content":"hello"}]}'

# Expected: {"input_tokens": <int>}

# 4. Tool use round-trip
curl -s http://localhost:11435/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model":"claude-sonnet-4-5","max_tokens":300,
    "messages":[{"role":"user","content":"What is the weather in Paris? Use the get_weather tool."}],
    "tools":[{"name":"get_weather","description":"Get current weather for a city",
              "input_schema":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}]
  }' | jq .

# Expected: content contains a {"type":"tool_use","name":"get_weather","input":{"city":"Paris"}} block, stop_reason: "tool_use"
```

## Configuration reference

All env vars use the `FLEET_` prefix:

| Var | Default | Purpose |
|---|---|---|
| `FLEET_ANTHROPIC_MODEL_MAP` | see above | JSON map of `claude-*` model id → local Ollama model name. Always include a `"default"` key. |
| `FLEET_ANTHROPIC_REQUIRE_KEY` | `false` | If true, require `x-api-key` header to match `FLEET_ANTHROPIC_API_KEY` |
| `FLEET_ANTHROPIC_API_KEY` | `""` | Shared secret for `/v1/messages` when `require_key` is true |
| `FLEET_ANTHROPIC_DEFAULT_MAX_TOKENS` | `4096` | Used when client omits `max_tokens` |

## Auth

By default `/v1/messages` is open (local trust boundary, same as the rest of ollama-herd). Lock it down for shared deployments:

```bash
export FLEET_ANTHROPIC_REQUIRE_KEY=true
export FLEET_ANTHROPIC_API_KEY=sk-local-something-long
```

Then Claude Code clients must set `ANTHROPIC_AUTH_TOKEN=sk-local-something-long`.

## Endpoints exposed

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/messages` | Inference (streaming + non-streaming) |
| `POST` | `/v1/messages/count_tokens` | Token estimation for budget gating |
| `GET` | `/v1/messages` | Friendly probe — returns service identity |

## Known limitations

These are honest tradeoffs, not bugs:

- **Tool quality varies by model.** `qwen3-coder:30b` and `qwen3:32b` handle Claude Code's agentic loops well; smaller / non-coding-tuned models drop tool calls or hallucinate args. Prefer the recommended list above.
- **Ollama caps at 3 concurrently-loaded models on macOS.** Unconfigurable via env vars as of Ollama 0.20.4 — see `docs/issues.md`. If `FLEET_ANTHROPIC_MODEL_MAP` references >3 distinct models, some will be evicted and Claude Code requests will silently fall back to whatever model *is* hot. Symptom: Claude Code's tool calls start coming back as plain-text JSON instead of `tool_use` blocks. Detection: check the `X-Fleet-Fallback` response header (present means fallback fired) or `SELECT original_model, model FROM request_traces WHERE original_model != model` in `~/.fleet-manager/latency.db`.
- **No extended thinking.** Anthropic `thinking` content blocks are returned as plain text. Reasoning models (qwen3 thinking variants) emit reasoning into `content` rather than a separate block. Claude Code still works — it just can't show you the thinking pane.
- **No prompt caching.** Every request is a full re-encode. Long Claude Code sessions are slower than against real Claude. Workaround: run a model with a large warm KV cache (`OLLAMA_NUM_PARALLEL=2`, `OLLAMA_KEEP_ALIVE=-1`).
- **Token counts are estimates.** `usage.input_tokens` / `output_tokens` come from tiktoken (`cl100k`) and Ollama's `eval_count` respectively. Use them for budgeting, not billing.
- **Vision needs a vision model.** Code-tuned models like `qwen3-coder` don't see images. If you point Claude Code at an image, map to `gemma3:27b` or `llava:13b` instead.
- **`tool_choice: any` / `tool: <name>` are best-effort.** Ollama has no native forcing mechanism, so we append a system-prompt instruction. The model usually complies but isn't guaranteed.
- **No `cache_control` / Files / Computer Use / Code Execution betas.** These Anthropic-specific betas are accepted-and-ignored or 501'd. Out of scope for local-model parity.

## What you get

Once Claude Code is pointed at the herd:

- Routes to the best-loaded node automatically (scoring across thermal, memory, queue depth, model affinity, etc.)
- Fallback to alternative models if the requested one isn't available
- All requests captured in the trace store (`~/.fleet-manager/latency.db`) — view in the dashboard at [http://localhost:11435/dashboard](http://localhost:11435/dashboard)
- Per-request `X-Fleet-Node`, `X-Fleet-Score`, and `X-Fleet-Fallback` response headers
- Tagging via `metadata.user_id` in the request body (gets logged with the trace)

## Troubleshooting

**Claude Code starts but every request 404s.**
The model id Claude Code is sending isn't in your map and the local model name doesn't match anything Ollama has pulled. Check `curl http://localhost:11435/v1/models` for the available model list and adjust `FLEET_ANTHROPIC_MODEL_MAP`.

**Tool calls never come back.**
Almost always a model issue. Switch to `qwen3-coder:30b` or `qwen3:32b`. Check the herd log for `Anthropic request: ... tools=N` to confirm tools were forwarded.

**Claude Code was working, then suddenly returns text instead of tool calls.**
Likely cause: your mapped model got evicted from VRAM by Ollama's 3-model cap, and requests are falling back to a weaker model (e.g. `gemma3:4b`) that can't emit the `tool_calls` format. Verify with:
```bash
curl -sI -X POST http://localhost:11435/v1/messages \
  -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' \
  | grep -i x-fleet-fallback
```
If `x-fleet-fallback` is present, the mapped model isn't hot. Pre-warm it:
```bash
curl http://localhost:11434/api/generate \
  -d '{"model":"qwen3-coder:30b","prompt":"hi","keep_alive":-1,"stream":false}'
```
Also check `ollama ps` to see what's currently hot. If you consistently need >3 models hot, see `docs/issues.md` for workarounds.

**`auth required` errors from Claude Code.**
You enabled `FLEET_ANTHROPIC_REQUIRE_KEY=true` but Claude Code's `ANTHROPIC_AUTH_TOKEN` doesn't match. Either disable the gate or set the env var to the matching key.

**Streaming hangs partway through.**
Check `~/.fleet-manager/logs/herd.jsonl` for Ollama errors on the chosen node. The translator synthesizes a `message_stop` if Ollama drops the connection without `done:true`, so the client shouldn't hang indefinitely — but a model crash mid-stream will be visible there.

**Latency feels slow on the first request after model swap.**
Cold load — the chosen model has to load into VRAM. Subsequent requests stay warm thanks to `keep_alive=-1`. Pre-warm with `ollama run qwen3-coder:30b` once after restart.

## Implementation reference

If you want to read the code:

- Route: [src/fleet_manager/server/routes/anthropic_compat.py](../../src/fleet_manager/server/routes/anthropic_compat.py)
- Translator (pure, testable): [src/fleet_manager/server/anthropic_translator.py](../../src/fleet_manager/server/anthropic_translator.py)
- Pydantic models: [src/fleet_manager/server/anthropic_models.py](../../src/fleet_manager/server/anthropic_models.py)
- Implementation plan: [docs/plans/anthropic-messages-compat.md](../plans/anthropic-messages-compat.md)
