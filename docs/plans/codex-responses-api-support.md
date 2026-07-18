# Codex support — an OpenAI Responses API (`/v1/responses`) shim

**Status**: Planning
**Date**: 2026-07-18
**Motivation**: [`docs/research/`] investigation, 2026-07-17 — Codex CLI **removed Chat Completions support in Feb 2026** (`wire_api="chat"` is gone; `responses` is the only value). Codex now speaks *only* the Responses API. Herd exposes `/v1/chat/completions`, `/v1/models`, `/v1/messages` (Anthropic) — but **no `/v1/responses`**, so a Codex request 404s and Codex cannot use Herd at all. This plan adds the shim that makes it work, and makes the marketing site's Codex claim true again.

## The core insight: this is the Anthropic route, again

We already do exactly this shape of work for Claude Code: accept a foreign provider's API, translate it into our internal `InferenceRequest`, run it through the **same** scorer → queue → streaming pipeline, and translate the response back. `/v1/responses` is the same pattern with a different wire format. The plan is therefore mostly *"mirror `anthropic_compat.py` + `anthropic_translator.py`"*, and the reuse is deep:

| Concern | Existing component reused |
|---|---|
| Model resolution (no hand-written map) | `anthropic_autoroute.resolve_model` / `rank_candidates` |
| Node selection + fallback | `score_with_fallbacks` |
| Queue + streaming to Ollama | `QueueEntry`, `StreamingProxy` |
| MLX-backed models (`mlx:`) | the `_serve_via_mlx` path (mlx_lm.server is OpenAI-native) |
| Backend 4xx surfacing | `client_error_passthrough` |
| Tool-schema fixup (Qwen long-ctx bug) | `tool_schema_fixup` (already applied on the Anthropic route) |
| Context management / compaction | `context_management` (Codex is agentic + tool-heavy, same as Claude Code) |
| Response headers | `fleet_headers` |
| Trace recording | the existing trace path |

**New surface is small and localized:** a `responses_translator.py` (wire ↔ internal, mirroring `anthropic_translator.py`) and a `responses_compat.py` route (mirroring `anthropic_compat.py`), plus `RequestFormat.RESPONSES`.

## Design principles (greenfield)

- **No feature gating.** `/v1/responses` is just always available, like every other route. No `FLEET_*` toggle to turn Codex support on.
- **No new model-map surface.** Codex sends a model id (e.g. `gpt-5-codex` or a custom name); resolve it with the **same auto-routing** we built for Anthropic — if the id is a real local model, pass it through; otherwise route to the best loaded coding model. One resolution story across both foreign APIs.
- **Reuse before build** (see the table above). The only genuinely new code is wire translation.

---

## The Responses API shape (reference for the translator)

**Request** (`POST /v1/responses`):
- `model` — model id.
- `input` — *either* a string *or* an array of input items. Items are messages (`{role, content}`) and tool results (`{type:"function_call_output", call_id, output}`). This is the conversation.
- `instructions` — system prompt (separate from `input`).
- `tools` — `[{type:"function", name, parameters, description}]` (close to OpenAI function shape).
- `tool_choice`, `stream`, `max_output_tokens`, `reasoning`, `store`, `previous_response_id`.

**Non-streaming response**:
```json
{ "id": "resp_…", "object": "response", "status": "completed",
  "output": [
    { "type": "message", "role": "assistant",
      "content": [ { "type": "output_text", "text": "…" } ] },
    { "type": "function_call", "call_id": "…", "name": "…", "arguments": "{…}" }
  ],
  "usage": { "input_tokens": N, "output_tokens": N, "total_tokens": N } }
```
(Function calls are **top-level output items**, siblings of the message — *not* nested content parts. Phase 1 confirms the exact shape from a real capture.)

**Streaming event sequence** (the state machine `ResponsesSSEState` must emit):
```
response.created → response.in_progress
  → response.output_item.added (message, status in_progress)
    → response.content_part.added (output_text)
      → response.output_text.delta × N
    → response.output_text.done
  → response.output_item.done (message)
  [per tool call:]
  → response.output_item.added (function_call)
    → response.function_call_arguments.delta × N
    → response.function_call_arguments.done
  → response.output_item.done (function_call)
→ response.completed (status completed, full output[], usage)
```
Each event carries a monotonic `sequence_number` and, where relevant, `item_id` / `output_index` / `content_index`.

---

## Phase 1 — Capture ground truth *(do this first — it decides the design)*

The Anthropic translator was built against real Claude Code traffic; the over-context work this week was decided by one probe. Same discipline here: **do not translate against the docs alone.**

1. Point a real Codex CLI at a throwaway OpenAI-compatible endpoint that logs the raw request (a tiny local echo server, or Herd with a debug dump on `/v1/responses`), and run one real coding turn with a tool call.
2. Capture: the exact `input` array shape, whether `instructions` is used, the `tools` schema, `stream` value, and **critically — `store` and `previous_response_id`**. This answers the one design fork below.
3. Save the capture under `docs/reference/` as the translator's fixture and test oracle.

**The fork this resolves — statefulness.** The Responses API allows two chaining modes:
- **Stateless** — client resends the full `input` array (prior output items appended) every turn. A shim needs no storage; each request is self-contained. This is what a custom/local provider almost always gets.
- **Stateful** — client sends only the new message + `previous_response_id`, and the *server* reconstructs history. This would force Herd to persist responses (SQLite, keyed by `resp_…`) and rebuild context — a much larger build.

**Assumption to verify:** Codex against a custom provider uses stateless (`store:false`, full input). The plan below is stateless-first. If Phase 1 shows Codex requires `previous_response_id`, that becomes its own sub-phase (a response store) before the rest can ship.

---

## Phase 2 — Request translation (wire → internal)

New `responses_translator.py`, mirroring the request half of `anthropic_translator.py`:
- `responses_input_to_ollama_messages(input, instructions)` — `input` (string or item array) + `instructions` → the `messages` list our pipeline uses. `function_call_output` items → tool-result messages; assistant `function_call` items in history → assistant tool-call messages. Reuse the block-coercion approach from `anthropic_to_ollama_messages`.
- `responses_tools_to_ollama(tools)` — `{type:"function", name, parameters}` → our tool shape. Nearly identical to the OpenAI tool shape we already handle; likely a thin adapter.
- `apply_tool_choice` — reuse as-is (same semantics).
- Add `RequestFormat.RESPONSES` to `models/request.py`.
- **Model resolution:** reuse `resolve_model` (Phase-2 auto-routing). Codex ids aren't `claude-*`, so extend/parametrize the resolver so a non-local id routes to the best loaded coding model instead of falling through `_looks_local` → passthrough → 404. One shared resolver, a small generalization.

## Phase 3 — Response translation (internal → wire)

Two paths, non-streaming first (simpler, testable), then streaming:
- **Non-streaming:** `accumulate_responses_object(ollama_response, …)` → the `{object:"response", output:[…], usage}` shape. Mirrors `accumulate_anthropic_response`.
- **Streaming:** `ResponsesSSEState` + `ollama_chunk_to_responses_events(...)` — a state machine emitting the event sequence above with correct `sequence_number`/`item_id`/`output_index`. Mirrors `AnthropicSSEState` + `ollama_chunk_to_anthropic_events`, which already solves the hard part (interleaving text + streaming tool-call argument deltas, mapping stop reasons). The Responses event names differ but the state transitions are the same problem.

## Phase 4 — The route

New `responses_compat.py` (`@router.post("/v1/responses")`), mirroring `anthropic_compat.py::messages` end-to-end and reusing every pipeline stage:
1. auth (reuse the `_check_auth` pattern), parse/validate (a `ResponsesRequest` pydantic model), model resolution (Phase 2).
2. translate → build `InferenceRequest(original_format=RESPONSES)` → `tool_schema_fixup` → context management.
3. `score_with_fallbacks` → `QueueEntry` → `StreamingProxy`.
4. `mlx:` targets → `_serve_via_mlx`, translating Responses ↔ OpenAI chat (mlx_lm.server is chat-native; extend the existing Anthropic→OpenAI MLX bridge).
5. translate the response back (Phase 3), attach `fleet_headers`, record the trace.
6. `client_error_passthrough` for backend 4xx.
7. Register the router in `app.py` (one line, next to the other `include_router` calls).

## Phase 5 — Tests, docs, coordination

- **Tests:** translator unit tests driven by the Phase-1 capture (request→messages, messages→response object, the full streaming event sequence for a text+tool-call turn), plus a route test through a stubbed pipeline. Mirror `test_anthropic_translator.py`.
- **Docs:** a Codex integration guide (`docs/guides/codex-integration.md`) mirroring the Claude Code one — the `~/.codex/config.toml` `model_providers` block pointing `base_url` at `http://localhost:11435/v1`, and a note that auto-routing means no model map is needed.
- **`/v1/models`** already exists; confirm Codex's model-discovery hits it and is satisfied.
- **Coordination:** once shipped + on PyPI, tell the site agent the Codex claim is true again (it's currently gated correctly — see the investigation). This closes the "self-correcting when Herd ships the Responses shim" loop.

---

## Risks & open questions
- **Statefulness (biggest).** If Codex requires `previous_response_id`, Phase 1 must trigger a response-store sub-phase before shipping. Everything else assumes stateless.
- **Reasoning items.** Codex may send/expect `reasoning` output items. For non-reasoning local models we can omit them; for thinking models (gpt-oss etc.) we may map our `thinking` channel → a `reasoning` output item. Confirm what Codex needs from the capture; ship without reasoning first if it's optional.
- **Exact function_call representation** (top-level item vs content part) and `call_id` threading between a tool call and its later `function_call_output`. The capture nails this.
- **Codex model id.** What id Codex actually sends to a custom provider decides the resolver default. Capture answers it.

## Out of scope (v1)
- Stateful `previous_response_id` storage (only if Phase 1 proves it's required).
- Non-Codex Responses features: hosted tools (web_search, file_search, code_interpreter), MCP tool items, image/audio output, background mode, `store:true` persistence.
- Anything the Codex CLI doesn't actually exercise — driven by the capture, not the full API surface.
