# Codex CLI × Ollama Herd

Point [OpenAI Codex](https://github.com/openai/codex) at your own hardware — inference runs on your fleet, and Herd routes each request to the best node and model you have loaded.

> **Agentic coding works, with no configuration** — end-to-end verified 2026-07-18 on a real `codex-cli 0.145.0-alpha.18` (`codex exec`, `qwen3-coder:30b`). Codex ran `pytest`, read the source, wrote a correct fix through `apply_patch`, and re-ran `pytest` to green — entirely on local hardware, no model map, no per-tool config. Any model name works, including the Desktop app's default `gpt-5.6-sol`.
>
> ```
> $ python3 -m pytest -q
> ..                                                       [100%]
> 2 passed
> ```
>
> Herd bridges two gaps to make this work, because Codex's tool *descriptions*
> document an API its tool *schema* doesn't expose — and local models call what
> the prose tells them to. Both are automatic; see
> [Two Codex tool protocols](#two-codex-tool-protocols-both-handled).

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

Tool-use quality is the thing that matters here — the loop only closes if the model reliably calls tools and edits files.

| Model | Agentic coding | Notes |
|---|---|---|
| `qwen3-coder:30b` | ✅ verified end-to-end via `codex exec` | Best general-purpose pick. ~19 GB, 256K context |
| `gpt-oss:120b` | ⚠️ drives the loop, weak at converging | Reasoning model. Sustained 40+ tool-calling turns but explored without landing an edit; prefer it for analysis, not editing |
| `qwen3:32b` | untested | Strong reasoning, good tool use |

Verified tasks: fix an even-length `median()` bug, fix a whole-percent discount bug, and **create a new module from scratch** to satisfy failing imports — each run ending with the model executing pytest itself and reaching green. The `median()` task was re-run end-to-end on 2026-07-18 against `codex exec`: Codex ran pytest, read the source, patched it via `apply_patch`, and re-ran pytest to `2 passed` — and went beyond the ask, adding an empty-sequence guard.

Smaller / non-coding-tuned models tend to drop tool calls or hallucinate arguments.

## Limitations (v1)

- **MLX-backed models aren't served here yet.** Auto-routing skips `mlx:` models for Codex, so you'll get an Ollama-backed model automatically. An explicit `mlx:` mapping returns a clear `503`. (Claude Code's `/v1/messages` endpoint *does* serve MLX.)
- **Stateful chaining isn't supported.** Herd doesn't persist responses, so `previous_response_id` is rejected with a `400`. Codex's default stateless mode — resending the conversation each turn — is what's supported.
- **Name the tool in your prompt.** This is the single biggest quality lever. "Diagnose the root cause and fix it" made qwen3-coder:30b explore until it exhausted its budget; the *same task* with *"use the apply_patch command to fix X, then re-run pytest"* succeeded. Verified on two independent tasks (2/2 green), and again in the 2026-07-18 end-to-end run.
- **Approval prompts depend on the model asking for them.** Codex escalation is model-initiated: the model must set `sandbox_permissions: "require_escalated"` plus a `justification` on the tool call. There is no approval item type in the protocol for Herd to synthesise. Local models do request escalation, but not always — if a sandboxed command fails with no prompt, that's why. Setting **Full Access** removes the need for the prompt, but it's a workaround, not a fix.
- **Don't trust the model's self-report — check the diff.** In a run that produced a *correct* fix, the model also claimed *"I wasn't able to run pytest due to missing Python tools in the environment"* — it had run pytest successfully minutes earlier in that same session. The code was right; the narration was not.
- **Budget for tokens.** A one-line fix cost ~109K tokens end-to-end. Agentic loops re-send the full conversation each turn, and Codex's system prompt alone is ~27KB.
- **Hosted tools are dropped.** `web_search` / `file_search` / MCP items have no local equivalent.
- **Images auto-route to a vision model.** Codex's `view_image` sends the picture on the next turn. Herd extracts it into Ollama's `images` list and routes the request to a vision-capable model (gemma3, llama4, …) even when the conversation's coding model isn't one — the same `has_images` signal the Claude Code route already uses. Verified 2026-07-18: red/blue/green PNGs identified correctly via `gemma3:27b`. Remote `http(s)` image URLs are still dropped with a warning, since Ollama can't fetch them — send images inline as `data:` URIs. If no vision model is on the fleet, `ollama pull gemma3:4b` is the cheap fix (~6 GB).
- **Codex won't recognise your model's metadata** — it keys off its own built-in table of OpenAI slugs, so you'll see `Model metadata for <name> not found. Defaulting to fallback metadata`. Harmless in practice: tool calling, editing and multi-turn all work anyway (verified). Herd emits the model-listing schema Codex asks for, which silences the per-turn refresh errors, but cannot make Codex *recognise* a non-OpenAI model name.
- **Codex's model picker may not populate.** Codex decodes `/v1/models` against its own **undocumented, strictly-typed** schema (not the OpenAI one) and fails the *whole* decode on the first problem, logging `failed to refresh available models: … missing field X`. Each field you add reveals the next one. As of 2026-07-18 we emit 20 fields discovered this way — including two closed enums that reject anything outside them (`shell_type`: default|local|unified_exec|disabled|shell_command; `visibility`: list|hide|none) and a nested `truncation_policy` struct (`mode`: bytes|tokens, plus `limit`). **It is not yet converged** — the current known-next field is `experimental_supported_tools`. **This is cosmetic for the CLI** (pass `-m`), but it is what empties the Desktop picker. To continue the discovery loop: run `codex exec` three times (a single run right after a restart gives a false clean — the refresh hasn't fired), then `grep -o 'missing field `[a-z_]*`' <log>` for the next field.

## Two Codex tool protocols, both handled

Codex sends its tools one of two ways depending on the model slug, and Herd translates both — you don't need to care which:

| Slug family | What Codex sends | What Herd does |
|---|---|---|
| `sol` / `terra` / `luna` ("Responses-Lite", incl. the Desktop default) | tools hidden in an `additional_tools` input item; the primary tool is a `custom`-typed `exec` taking raw JavaScript | extracts them, bridges `exec` to a function taking one string, converts the call back to a `custom_tool_call` |
| everything else | a normal top-level `tools` array of ~12 plain functions (`exec_command`, `write_stdin`, …) | passes them straight through |

The Lite shape is [openai/codex#31894](https://github.com/openai/codex/issues/31894) — it leaves the model with nothing callable even against OpenAI's own hosted models. Herd handles it so you don't hit that.

### Two calls Herd rewrites, and why

Codex's tool *descriptions* document an API its tool *schema* doesn't expose.
Frontier models are tuned around the discrepancy; local models read the prose and
call exactly what it says. Both rewrites are automatic and logged at WARNING.

**`exec_command` called as a top-level tool.** In code mode the only callable
tool is `exec` — everything else lives behind `await tools.exec_command(…)`
inside the JavaScript it evaluates. The `exec` description is ~14 KB and dense
with `tools.exec_command(...)`, so models call that name directly. Measured on
gpt-oss:120b: **4/4** calls went to a tool that was never offered. Codex receives
a `function_call` it has no handler for, cannot execute it, and **the run stops
with no error displayed** — the transcript just shows a tool call, then nothing.
Herd rewrites it as a `custom_tool_call` on `exec` carrying the equivalent
JavaScript.

**`apply_patch` called as a tool.** `apply_patch` is a **binary**, not a tool —
Codex injects it at the front of the sandbox PATH
(`~/.codex/tmp/arg0/codex-arg0…/apply_patch`, confirmed with `which -a`), and its
instructions describe an argv-array invocation while calling it "the `apply_patch`
tool". Models call it as a tool and Codex rejects it outright:

```
ERROR codex_core::tools::router: error=unsupported call: apply_patch
```

The session then reads and executes fine but **can never write** — it explores
forever and edits nothing. Herd rewrites it into an `exec_command` heredoc, so
patch bodies containing quotes, backslashes or `$` reach the binary intact.

Both rewrites decline to act when they'd be guessing: if the tool really was
offered, or if no `*** Begin Patch` envelope is recognisable, the call passes
through untouched.

## If Codex says it "cannot execute commands"

**Start a new chat.** This is the most common failure and it is not a configuration problem.

Once Codex claims it can't run commands, it conditions on its own prior statements and keeps re-deriving that conclusion — even with working tools sitting in front of it. Measured on the same app, same config, same day:

| Conversation length | `exec` offered | Tools actually called |
|---|---|---|
| 43 messages (after earlier refusals) | ✅ | **0** — 741 tokens explaining why it can't |
| 6 messages (fresh chat) | ✅ | **1** — ran the command |

It also **confabulates specifics** — inventing detailed lists of "blocked operations" and "sandbox security policies" that don't exist. So its explanation is never evidence about your setup. Check the herd log instead:

```bash
grep 'Responses\[' ~/.fleet-manager/logs/herd.jsonl | tail -3
#  tools=3 custom=['exec']  -> tools ARE reaching the model
#  tools=0                  -> genuinely no tools; check your provider config
```

## If commands fail sandboxed instead of asking for approval

With approvals set to **On request** (the Desktop default), a command needing
network or write access should raise a prompt. If it just fails — and the model
then invents a "sandbox security policy" to explain it — the cause is that
**Codex approvals are model-initiated**. There is no approval message type in
the protocol; the model has to ask, by setting `sandbox_permissions:
"require_escalated"` plus a `justification` on the tool call itself.

Local models do request escalation — but on the Desktop app's code-mode path
they often express it as JSON when the tool requires raw JavaScript, which is a
syntax error, so the request never reaches Codex. Herd repairs that shape
automatically. If you still see it:

```bash
grep 'emitted a JSON object instead of' ~/.fleet-manager/logs/herd.jsonl
#  present -> the repair fired; escalation should now reach Codex
```

Setting **Full Access** also makes the symptom disappear, but only by removing
the need for the prompt — it is a workaround, not a fix.

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
