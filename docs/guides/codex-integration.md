# Codex CLI × Ollama Herd

Point [OpenAI Codex](https://github.com/openai/codex) at your own hardware — inference runs on your fleet, and Herd routes each request to the best node and model you have loaded.

> **Agentic coding works** — verified 2026-07-18 against a real `codex-cli 0.145.0-alpha.18`: Codex ran `pytest`, read the source, wrote a correct fix with `apply_patch`, and re-ran the tests green, entirely on local models. **One required setting:** pick a model name that is *not* a `sol`/`terra`/`luna` slug (see [Model name matters](#model-name-matters-a-codex-bug)).

## Quick start

```bash
# 1. Pull a coding model (any one — Herd routes to whatever you have)
ollama pull qwen3-coder:30b

# 2. Start the herd (and a node agent)
uv run herd
uv run herd-node

# 3. Point Codex at it
```

Add this to `~/.codex/config.toml`:

```toml
model_provider = "herd"

[model_providers.herd]
name = "Ollama Herd"
base_url = "http://localhost:11435/v1"
wire_api = "responses"
```

Then just run `codex`.

**No model map needed.** Herd auto-routes whatever model id Codex sends to the best coding model you actually have loaded. Pull a model and go.

> **`wire_api = "responses"` is required.** Codex removed Chat Completions support in **February 2026** — `wire_api = "chat"` is no longer valid, and any guide showing it is stale. Herd serves the Responses API at `/v1/responses`, which is what current Codex speaks.

## How model selection works

Codex sends a model id (e.g. `gpt-5-codex`). Herd resolves it like this:

1. **You pinned it** — an explicit `FLEET_ANTHROPIC_MODEL_MAP` entry for that id wins.
2. **It's a real local model** — if the id names a model your fleet actually has (`qwen3-coder:30b`), it's used as-is.
3. **Auto-routing** *(the default)* — otherwise Herd picks the **best coding model currently loaded** across the fleet, falling back to the best one on disk.
4. **Nothing available** — a clear `404` telling you to pull a model.

So a stock Codex install with no Herd config works, and `codex -m qwen3-coder:30b` also works if you want to force a specific model.

To pin a specific model for Codex without affecting anything else:

```bash
export FLEET_ANTHROPIC_MODEL_MAP='{"gpt-5-codex": "qwen3-coder:30b"}'
```

To disable auto-routing entirely and require an explicit map, set `FLEET_ANTHROPIC_AUTO_ROUTE=false`.

## What you get from the fleet

Pointing Codex at Herd rather than directly at a single Ollama gives you:

- **Multi-node routing** — requests are scored across every node (thermal, memory, queue depth, model affinity, context fit) and sent to the best one.
- **Best-loaded auto-routing** — no hand-maintained model map to keep in sync with what you've pulled.
- **Context management** — tool-result clearing and compaction, the same layers that keep long Claude Code sessions healthy.
- **Observability** — every request lands in the trace store; `X-Fleet-Served-Model` on each response tells you exactly which model and node answered.

## Verifying it works

```bash
# Does the endpoint answer?
curl -s localhost:11435/v1/responses \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-5-codex","input":"say ok","stream":false}' | python3 -m json.tool

# Which model actually served it?
curl -si localhost:11435/v1/responses \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-5-codex","input":"hi","stream":false}' | grep -i x-fleet-served-model

# Recent Codex traffic in the trace store
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT original_model, model, status FROM request_traces
   WHERE original_format='responses' ORDER BY timestamp DESC LIMIT 10"
```

## Recommended models

For the chat path that works today, any capable coding model is fine. (Tool-use quality would matter for agentic work — see [Limitations](#limitations-v1).)

| Model | Tool use | Notes |
|---|---|---|
| `qwen3-coder:30b` | Excellent | Best general-purpose pick. ~19 GB, 256K context |
| `qwen3:32b` | Excellent | Strong reasoning, good tool use |
| `gpt-oss:120b` | Good | Large; thinking tokens can slow agentic loops |

Smaller / non-coding-tuned models tend to drop tool calls or hallucinate arguments.

## Limitations (v1)

- **MLX-backed models aren't served here yet.** Auto-routing skips `mlx:` models for Codex, so you'll get an Ollama-backed model automatically. An explicit `mlx:` mapping returns a clear `503`. (Claude Code's `/v1/messages` endpoint *does* serve MLX.)
- **Stateful chaining isn't supported.** Herd doesn't persist responses, so `previous_response_id` is rejected with a `400`. Codex's default stateless mode — resending the conversation each turn — is what's supported.
- **Local models need more explicit steering than frontier models.** A vague "fix the bug" prompt made qwen3-coder:30b explore until it ran out of budget; adding *"use the apply_patch command to edit the file"* made it converge. Expect higher token counts too — a one-line fix cost ~109K tokens end-to-end.
- **Hosted tools are dropped.** `web_search` / `file_search` / MCP items have no local equivalent.
- **Codex's model picker may not populate.** Codex's model manager decodes `/v1/models` against its own undocumented schema (not the OpenAI one) and logs `failed to refresh available models: ... missing field ...` on each refresh. We emit the fields it asked for as far as we could reverse-engineer them against codex-cli 0.145-alpha; the schema keeps demanding more. **This is cosmetic — inference is unaffected** and every turn routes normally. Specify the model with `-m` or `model_provider` config rather than the picker.

## Model name matters (a Codex bug)

Codex hides its entire tool catalogue from any model whose slug it treats as "Responses-Lite" — the `sol` / `terra` / `luna` families. For those, tools are moved into an `additional_tools` input item and the top-level `tools` array is omitted, so **the model gets nothing callable and will narrate work it never did.** This is [openai/codex#31894](https://github.com/openai/codex/issues/31894) and reproduces against OpenAI's own hosted models — it is not specific to local models or to Herd.

**Use any non-Lite model name** and Codex sends a normal tool array:

```bash
codex exec -m qwen3-coder:30b "..."     # 12 tools — works
codex exec -m gpt-5.6-sol    "..."      # 2 tools  — model can't act
```

Because Herd auto-routes, the name you give Codex doesn't have to be the model that serves it — any non-Lite name still lands on your best loaded local model.

## Common config mistake

If you're appending to an existing `~/.codex/config.toml`, `model_provider = "herd"` **must go above the first `[table]` header**. TOML assigns a bare key to whatever table precedes it, so putting it at the bottom silently makes it `desktop.model_provider` — Codex never sees it, no error is raised, and it quietly keeps using the default provider.

```toml
model_provider = "herd"     # ← first line, before ANY [section]

[some.existing.section]
...
[model_providers.herd]      # ← this is a table header, so it can live anywhere
```

## Troubleshooting

**`404 ... not available on any node`** — you haven't pulled a chat/coding model, or the one you pinned isn't on the fleet. Run `ollama pull qwen3-coder:30b`, or check `curl localhost:11435/v1/models`.

**`400 previous_response_id ... not supported`** — your client is using server-side conversation state. Set `store = false` (stateless) so Codex sends the full conversation each turn.

**Codex errors about the wire protocol** — confirm `wire_api = "responses"` in `~/.codex/config.toml`. `wire_api = "chat"` was removed from Codex in Feb 2026.

**Codex narrates actions instead of performing them** ("I fixed the bug, tests pass" — but nothing changed). Almost always the Lite-slug bug above: check the herd log for `tools=0` or `tools=2`. If it says `tools=12`, tools are reaching the model and the issue is steering — be explicit ("use the apply_patch command"). Either way, verify the files before trusting a success report.

## See also

- [`claude-code-integration.md`](claude-code-integration.md) — the same fleet, for Claude Code
- [`../reference/anthropic-auto-routing.md`](../reference/anthropic-auto-routing.md) — how best-loaded auto-routing picks a model
- [`../plans/codex-responses-api-support.md`](../plans/codex-responses-api-support.md) — design and roadmap for this endpoint
