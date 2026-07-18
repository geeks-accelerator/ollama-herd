# Codex support — an OpenAI Responses API (`/v1/responses`) shim

**Status**: **AGENTIC CODING VERIFIED — 2/2 real tasks.** Codex (CLI + Desktop) runs genuine agentic sessions against the local fleet via `/v1/responses`: it runs pytest, reads sources, edits files with `apply_patch`, and re-runs to green. Verified 2026-07-18 on `codex-cli 0.145.0-alpha.18` + `qwen3-coder:30b`. **Required:** use a non-Lite model name (not `sol`/`terra`/`luna`) — upstream bug openai/codex#31894. Known behaviours: name the tool in the prompt, and don't trust the model's self-report. MLX remains v2.
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

**New surface is small and localized:** a `responses_translator.py` (wire ↔ internal) and a `responses_compat.py` route (route *skeleton* mirrors `anthropic_compat.py`), plus `RequestFormat.RESPONSES`. See the **Codebase audit** below — the request side reuses the OpenAI path (Responses is OpenAI's own format), so the only genuinely new code is the response SSE translator.

## Design principles (greenfield)

- **No feature gating.** `/v1/responses` is just always available, like every other route. No `FLEET_*` toggle to turn Codex support on.
- **No new model-map surface.** Codex sends a model id (e.g. `gpt-5-codex` or a custom name); resolve it with the **same auto-routing** we built for Anthropic — if the id is a real local model, pass it through; otherwise route to the best loaded coding model. One resolution story across both foreign APIs.
- **Reuse before build** (see the table above). The only genuinely new code is wire translation.

## Competitive context — what the research says to build differently

A mid-2026 competitive-marketing study (private repo: `docs/research/codex-claude-code-competitive-marketing-2026.md`) found the Codex-on-local space is **thin and barely contested**, and it changes *how* we should build and frame this — not just *that* we should:

- **Codex-with-local is the uncontested wedge.** Claude Code local support is table-stakes (Ollama + LM Studio ship it natively). But LM Studio has **no Codex story**, Ollama's Codex path is documented-but-buggy (their own [ollama#16578](https://github.com/ollama/ollama/issues/16578) tracks a responses-wire compat bug for local models), and the only dual-CLI shims (opencodex, LocalCodeCli, csurong/codex-proxy) are single-node, self-marketed READMEs with no proof. **Dual-CLI + fleet routing is claimed by nobody.**
- **Proof is the universal gap → make verification a shippable artifact, not just a test.** Every competitor *documents* Codex support; none show a *verified end-to-end* run — not even the market leader. So this plan adds an explicit **Phase 6 — prove it end-to-end** whose output is a shareable artifact backing the "verified, not just documented" positioning. This is also our own safeguard: #16578 shows the wire-compat is genuinely easy to get subtly wrong.
- **Setup-simplicity is the bar.** Ollama's headline is *"no environment variables or config files needed."* Our best-loaded auto-routing already gets us there (no model map to hand-write) — the docs must lead with it (Phase 5).
- **Prior-art shims are open source — study them, don't reinvent.** opencodex / LocalCodeCli / csurong-codex-proxy have already solved Responses↔chat translation edge cases; use them as a reference during Phases 1–3 (with our own capture as the oracle, not their code).

**What the finished build must make claimable** (for the site copy): *dual-CLI (Claude Code **and** Codex) · protocol-current (`wire_api="responses"`) · fleet-routed · verified end-to-end.* No competitor combines these.

## Codebase audit — reuse & no-gating (2026-07-18)

A pre-implementation audit sharpened the reuse story and **shrank the new code below what "mirror the Anthropic route" implied.** The key realization: **Responses is OpenAI's own format, so it's closer to our OpenAI path than to the Anthropic one.** Findings that revise the phases below:

- **The request side is nearly free — reuse the OpenAI path, don't mirror the Anthropic translator.** `routes/openai_compat.py::chat_completions` is essentially *passthrough*: it sets `raw_body=body` and `messages=body["messages"]` and ships them to Ollama's `/api/chat`, which **natively accepts OpenAI-shaped messages and function-shaped tools** — there is no OpenAI→Ollama message translation because none is needed ([openai_compat.py:203](../../src/fleet_manager/server/routes/openai_compat.py:203)). So the Codex request translator is a **thin adapter** — `input` items → messages, `instructions` → a system message, `tools` passthrough — producing an OpenAI-shaped `raw_body`. The heavier `anthropic_to_ollama_messages` block-coercion is for Anthropic's content-block format, which Responses doesn't use — **don't borrow it.**
- **`RequestFormat.RESPONSES` slots in with two one-line touches, not a new body path.** Add it to the `(OLLAMA, ANTHROPIC)` grouping in `_build_ollama_body` ([streaming.py:1199](../../src/fleet_manager/server/streaming.py:1199)) so the pre-translated `raw_body` is used as-is. In the streamer, RESPONSES is simply *not* OPENAI, so it already falls into the raw-lines `else` branch ([streaming.py:678](../../src/fleet_manager/server/streaming.py:678)) — the route owns translation, exactly like Anthropic. No third branch inside `StreamingProxy`.
- **Only the response SSE translation is genuinely new — and it's "Anthropic's structure + OpenAI's field names."** Mirror the Anthropic *route-owned* stateful translator (`ResponsesSSEState`), but borrow field mapping from `_ollama_to_openai_sse` ([streaming.py:1343](../../src/fleet_manager/server/streaming.py:1343)) since Responses is OpenAI-family — not from the Anthropic event mapper.
- **The resolver needs ONE change that *reduces* special-casing, not a parametrization.** Today `resolve_model` passes a model through when `_looks_local()` is true, and `_looks_local` is `":" in model or not model.startswith("claude")` ([anthropic_autoroute.py:111](../../src/fleet_manager/server/anthropic_autoroute.py:111)) — a Codex id like `gpt-5-codex` matches, passes through, and 404s. **Fix: make passthrough conditional on *actual fleet presence* (`model in ondisk_names`) instead of the claude-prefix heuristic.** That single change serves *both* providers with *less* provider-specific logic: a real local name (either CLI sends one) is present → passthrough; a `claude-*` alias or a `gpt-5-codex` id is absent → auto-route. Rename the `claude_model` param to `model` — the resolver is now genuinely shared, not Anthropic's.
- **MLX reuse is even cleaner than the Anthropic route's.** Because we produce an OpenAI-shaped body, `mlx:` targets go through the existing `_serve_openai_via_mlx` ([openai_compat.py:76](../../src/fleet_manager/server/routes/openai_compat.py:76)) — a clean passthrough (mlx_lm.server is OpenAI-native) — then only its OpenAI SSE needs wrapping into Responses events. No new MLX bridge.
- **No gating** — confirmed, and unchanged: `/v1/responses` is always present, no `FLEET_*` toggle. The resolver change removes a heuristic rather than adding a flag.

Net: **new code is one thin request adapter + one response SSE state machine + a `ResponsesRequest` model + a route skeleton.** The resolver change is a simplification; everything else is reuse.

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
4. **Cross-check against the open-source shims** — opencodex, LocalCodeCli, and csurong/codex-proxy already translate Codex Responses traffic; skim how they handle `input` items, `function_call_output` threading, and streaming events to anticipate edge cases. The capture is the oracle; their code is a hint, not a source of truth.

**The fork this resolves — statefulness.** The Responses API allows two chaining modes:
- **Stateless** — client resends the full `input` array (prior output items appended) every turn. A shim needs no storage; each request is self-contained. This is what a custom/local provider almost always gets.
- **Stateful** — client sends only the new message + `previous_response_id`, and the *server* reconstructs history. This would force Herd to persist responses (SQLite, keyed by `resp_…`) and rebuild context — a much larger build.

**Assumption to verify:** Codex against a custom provider uses stateless (`store:false`, full input). The plan below is stateless-first. If Phase 1 shows Codex requires `previous_response_id`, that becomes its own sub-phase (a response store) before the rest can ship.

---

## Phase 2 — Request translation (wire → internal)

New `responses_translator.py`, but targeting the **OpenAI-body shape** (which Ollama accepts natively), not the Anthropic block format — see the audit:
- `responses_to_openai_body(responses_request)` — a **thin adapter**: `input` (string or item array) → OpenAI `messages`; `instructions` → a leading `system` message; `tools` (`{type:"function", name, parameters}`) → passthrough (already our tool shape); `function_call_output` items → `tool`-role messages; assistant `function_call` history items → assistant `tool_calls`. Output is an OpenAI-shaped `raw_body`, so the rest of the pipeline treats it exactly like an `/v1/chat/completions` request. **Do not** reuse `anthropic_to_ollama_messages` — its block-coercion is for Anthropic content blocks Responses doesn't have.
- `apply_tool_choice` — reuse as-is if the semantics match; otherwise a thin map. Confirm from the Phase-1 capture.
- Add `RequestFormat.RESPONSES` to `models/request.py`, and to the `(OLLAMA, ANTHROPIC)` grouping in `_build_ollama_body` ([streaming.py:1199](../../src/fleet_manager/server/streaming.py:1199)).
- **Model resolution — one simplifying change to the shared resolver.** Make `resolve_model` passthrough conditional on *actual fleet presence* (`model in ondisk_names`) rather than the `not startswith("claude")` heuristic in `_looks_local` ([anthropic_autoroute.py:111](../../src/fleet_manager/server/anthropic_autoroute.py:111)), and rename the `claude_model` param to `model`. Then `gpt-5-codex` (absent) auto-routes to the best loaded coding model, a real local name passes through, and Anthropic behavior is unchanged — with *less* provider-specific logic, not more.

## Phase 3 — Response translation (internal → wire)

The only genuinely-new code. Route-owned (like Anthropic), because RESPONSES falls into the streamer's raw-lines `else` branch ([streaming.py:678](../../src/fleet_manager/server/streaming.py:678)) — no branch added to `StreamingProxy`. Non-streaming first (simpler, testable), then streaming:
- **Non-streaming:** `accumulate_responses_object(ollama_response, …)` → the `{object:"response", output:[…], usage}` shape. Mirrors `accumulate_anthropic_response`'s *structure*.
- **Streaming:** `ResponsesSSEState` + `ollama_chunk_to_responses_events(...)` — a state machine emitting the event sequence above with correct `sequence_number`/`item_id`/`output_index`. Take the **structure** from `AnthropicSSEState` + `ollama_chunk_to_anthropic_events` (which already solves interleaving text + streaming tool-call argument deltas and stop-reason mapping), but the **field mapping** from `_ollama_to_openai_sse` ([streaming.py:1343](../../src/fleet_manager/server/streaming.py:1343)) — Responses is OpenAI-family, so deltas/roles/usage map from the OpenAI shape, not the Anthropic one.

## Phase 4 — The route

New `responses_compat.py` (`@router.post("/v1/responses")`), mirroring `anthropic_compat.py::messages` end-to-end and reusing every pipeline stage:
1. auth (reuse the `_check_auth` pattern), parse/validate (a `ResponsesRequest` pydantic model), model resolution (Phase 2).
2. translate → build `InferenceRequest(original_format=RESPONSES)` → `tool_schema_fixup` → context management.
3. `score_with_fallbacks` → `QueueEntry` → `StreamingProxy`.
4. `mlx:` targets → reuse the OpenAI MLX path `_serve_openai_via_mlx` ([openai_compat.py:76](../../src/fleet_manager/server/routes/openai_compat.py:76)) since we already hold an OpenAI-shaped body (mlx_lm.server is OpenAI-native → clean passthrough); then wrap its OpenAI SSE into Responses events with the Phase-3 translator. No new MLX bridge.
5. translate the response back (Phase 3), attach `fleet_headers`, record the trace.
6. `client_error_passthrough` for backend 4xx.
7. Register the router in `app.py` (one line, next to the other `include_router` calls).

## Phase 5 — Tests, docs, coordination

- **Tests:** translator unit tests driven by the Phase-1 capture (request→messages, messages→response object, the full streaming event sequence for a text+tool-call turn), plus a route test through a stubbed pipeline. Mirror `test_anthropic_translator.py`.
- **Docs — a dedicated per-CLI walkthrough** (`docs/guides/codex-integration.md`), because that's the format that ranks and converts (Ollama and LM Studio each ship a dedicated per-CLI page). Mirror the Claude Code guide. It must:
  - **Lead with zero-config** to match Ollama's *"no env vars, no config files"* bar — the `~/.codex/config.toml` `model_providers` block pointing `base_url` at `http://localhost:11435/v1`, and *"auto-routing means no model map — pull a coding model and go."*
  - **Signal protocol-currency explicitly** — show `wire_api = "responses"` in the config block. It's a trust signal that says "we know about the Feb-2026 change"; stale competitors implicitly admit they don't.
- **`/v1/models`** already exists; confirm Codex's model-discovery hits it and is satisfied.
- **Coordination:** once shipped + on PyPI *and verified (Phase 6)*, tell the site agent the Codex claim is true again — and that it's now backed by an end-to-end proof artifact, not just docs. Closes the "self-correcting when Herd ships the Responses shim" loop and upgrades the claim from "documented" to "verified."

## Phase 6 — Prove it end-to-end (the differentiator)

The research's single sharpest finding: **everyone documents Codex-on-local; nobody proves it.** Not LM Studio (no Codex), not the market leader (open bug #16578), not the hobby shims (no benchmarks). So verification isn't cleanup here — it's the *product claim*.

1. **Run a real Codex CLI session against the herd** — a genuine multi-turn coding task with tool calls, against a real local model on the fleet (not a stub). This is the same discipline as this week's over-context probe and the KV-sizing verification against the live fleet.
2. **Capture the proof** using the existing observability: the trace rows (`original_format='responses'`), `X-Fleet-Served-Model` headers showing which fleet node/model answered, tok/s, and a clean run to completion. Reuse `BenchmarkRunner` / `benchmark_engine.send_request` where it fits, exactly as the post-0.32 MLX-eval plan does.
3. **Produce a shareable artifact** — a short "verified: Codex running against an N-node local fleet" writeup with the trace/benchmark evidence, for the site. This is the thing no competitor has.
4. **Guard against the #16578 class of bug** — confirm the streaming event sequence and tool-call round-trip actually satisfy Codex end-to-end, not just our unit tests. A unit test that matches our own translator isn't proof the *client* is happy (the Anthropic route taught us this — real Claude Code traffic surfaced issues fixtures didn't).

---

## Risks & open questions
- **Statefulness (biggest).** If Codex requires `previous_response_id`, Phase 1 must trigger a response-store sub-phase before shipping. Everything else assumes stateless.
- **Reasoning items.** Codex may send/expect `reasoning` output items. For non-reasoning local models we can omit them; for thinking models (gpt-oss etc.) we may map our `thinking` channel → a `reasoning` output item. Confirm what Codex needs from the capture; ship without reasoning first if it's optional.
- **Exact function_call representation** (top-level item vs content part) and `call_id` threading between a tool call and its later `function_call_output`. The capture nails this.
- **Codex model id.** What id Codex actually sends to a custom provider decides the resolver default. Capture answers it.

## v2 — MLX-backed models over `/v1/responses`

Deferred deliberately. `mlx_lm.server` speaks **OpenAI SSE**, not Ollama NDJSON, so serving it here needs a *second* front-end feeding the same `ResponsesSSEState` — plus OpenAI-style fragmented tool-call argument accumulation (the messy part `mlx_proxy._MlxToolState` already solves for the Anthropic path). There's also a concrete trap: `mlx_proxy._to_openai_body` gates on `original_format == OPENAI`, so `RESPONSES` would fall to the Ollama-shaped branch and silently drop top-level params — that needs `RESPONSES` added to the `openai_native` check.

Shipping that without an MLX integration test would be exactly the debt this project avoids, so v1 instead **filters `mlx:` out of Codex auto-routing** (Codex always lands on a working Ollama-backed model) and returns a precise `503` only if an operator *explicitly* maps a Codex alias to `mlx:`. Claude Code's `/v1/messages` still serves MLX normally.

## Out of scope (v1)
- Stateful `previous_response_id` storage (only if Phase 1 proves it's required).
- Non-Codex Responses features: hosted tools (web_search, file_search, code_interpreter), MCP tool items, image/audio output, background mode, `store:true` persistence.
- Anything the Codex CLI doesn't actually exercise — driven by the capture, not the full API surface.

---

## Verification (2026-07-18) — what the real client found

Verified against **both** Codex surfaces — the CLI *and* the Desktop app.

### A. Codex CLI (`codex exec`)

Ran the CLI bundled in ChatGPT.app against the live fleet.
**Result: working end-to-end**, `provider: herd`, `x-fleet-served-model: qwen3-coder:30b`,
traced with `original_format='responses'`.

### B. Codex **Desktop app** — also working, no extra setup

The Desktop app (ChatGPT.app) reads the **same `~/.codex/config.toml`**, so the one
provider block serves both surfaces. A real multi-turn chat in the app was served
entirely by the fleet:

```
21:26:36  qwen3-coder:30b  bb  completed  8201 prompt →  36 completion   8917ms
21:26:38  qwen3-coder:30b  bb  completed  6209 prompt → 145 completion  10760ms
21:26:42  qwen3-coder:30b  bb  completed  8579 prompt → 198 completion   3860ms
```

Findings unique to the Desktop app:
- **It sends more than one model id.** `gpt-5.6-sol` for the conversation (matching the
  in-app picker's "5.6 Sol Light") *and* `gpt-5.6-luna` fired alongside — evidently for
  chat-title generation (the thread auto-titled itself). **Both auto-routed with no map**,
  which is a good incidental proof that the resolver copes with whatever ids a client
  invents. A hand-written model map would have had to guess `luna` existed.
- **Stateless chaining re-confirmed across a real conversation** — `msgs` grew 5 → 7 → 10
  as turns accumulated, i.e. the app resends the whole thread every time.
- **The `/v1/models` picker-schema gap is confirmed cosmetic.** The app works fine despite
  it, because it carries its own model list and never needed our response to populate.
  This retires the concern rather than leaving it open.

### Still unverified

**Every real turn so far had `tools=0`.** The translator's tool-call path — `function_call`
items, `call_id` threading, `response.function_call_arguments.*` events — has only been
exercised by unit tests, never by the actual client. That is the one remaining piece where
"spec-complete ≠ client-verified" still applies. A Codex turn that actually runs a command
would close it.

Confirmed from the capture:
- **Stateless chaining** — Codex sent the whole conversation in `input` (`msgs=7`) and never
  used `previous_response_id`. The plan's core assumption held, so no response store is needed.
- **Model id** is `gpt-5.6-sol` (its default), which auto-routed cleanly — vindicating the
  presence-based resolver change over the old `claude-*` heuristic.

Two bugs only a real client could surface:
1. **`QueueEntry(node_id=…)` was silently ignored** — the field is `assigned_node`, and pydantic
   dropped the unknown kwarg, so the proxy got an empty node and every request 500'd with
   `ValueError: Node  not found in registry`. The route tests only covered pre-pipeline guards,
   so nothing caught it. **Fixed**, plus the routing context (`routing_score`,
   `routing_breakdown`, `fallback_used`) the Anthropic route passes.
2. **`/v1/models` shape** — Codex's model manager rejects the OpenAI schema, wanting `models`
   (not `data`) and then `slug`, `supported_reasoning_levels`, … We now emit both keys (`data`
   stays OpenAI-pure). The chain of required fields didn't terminate within a reasonable number
   of probes, so **the model picker may still not populate — cosmetic, inference is unaffected.**

**Lesson, again:** the unit tests passed the whole time. Spec-complete is not client-verified.


## The agentic gap (2026-07-18) — captured, not inferred

A real coding task (fix a failing pytest) was run through `codex exec`. **It did not work**, and
the failure mode is instructive: the model *narrated* the whole job — "I fixed the bug, all tests
pass" — while `stats.py` was never opened and the test still failed. Confident fabrication, not
an error.

Capturing the raw request explained it. Codex sends:

```
KEYS: [client_metadata, include, input, model, parallel_tool_calls,
       prompt_cache_key, reasoning, store, stream, text, tool_choice]
tools (top-level): ABSENT
input item: {"type":"additional_tools","role":"developer","tools":[
    {"type":"custom",    "name":"exec"},           # JS orchestration ("code mode")
    {"type":"function",  "name":"wait"},
    {"type":"function",  "name":"request_user_input"},
    {"type":"namespace", "name":"collaboration", "tools":[…6]}]}
```

Three findings:
1. **Codex ignores the documented top-level `tools` field.** Its catalogue rides inside `input` as
   an `additional_tools` item. We were dropping it wholesale → the model got zero tools. **Fixed**
   (`collect_tools_from_input`), so `function`-type tools now reach the model.
2. **Its primary tool is untranslatable.** `exec` is a `custom` type whose contract is *"run
   JavaScript to orchestrate tool calls"*. Function-calling has no equivalent, and `namespace`
   nests tools. Only `wait` and `request_user_input` survive translation — neither of which can
   read or edit a file, so **agentic coding still cannot work.**
3. **`features.code_mode_host=false` changes nothing** — this build always sends code-mode tools.

Dropped tools are now logged at **WARNING** rather than silently, because silence is precisely
what let the model pretend it had acted.

**Honest scope, then:** Codex↔Herd is a working *chat* integration and a broken *agentic* one.
Closing the gap is not more wire translation — it needs either a Codex build that emits standard
function tools, or an `exec`-shim that presents a JS-orchestration contract a local model can
actually satisfy. Both are open questions, not scheduled work.


## Agentic verification results (2026-07-18)

Two independent tasks, both green, entirely on local models:

| Task | Before | After |
|---|---|---|
| `median()` even-length bug | `1 failed` | **`2 passed`** — correct odd/even branch written via `apply_patch` |
| `apply_discount()` whole-percent bug | `2 failed` | **`3 passed`** — correct `pct / 100` fix |

**What made it work:** naming the tool. "Diagnose the root cause and fix" wandered until budget-exhausted; the same task
phrased "use the apply_patch command to fix X, then re-run pytest" converged both times.

**Corrections to earlier conclusions in this document.** Two things I asserted were wrong:
- *"Agentic coding is blocked upstream"* — **false.** `apply_patch` is not a tool; Codex injects it as a **binary** on the
  sandbox PATH (`~/.codex/tmp/arg0/…/apply_patch`) and the prompt instructs the model to invoke it as a command. I had
  grepped the tool array, found nothing, and stopped short of checking the prose or the PATH.
- *"The `/v1/models` schema chain is cosmetic"* — **half right.** Completing it (`models`→`slug`→
  `supported_reasoning_levels`→`shell_type`) does NOT unlock `apply_patch` or make Codex recognise the model (it uses its
  own slug table), but it does eliminate a models-refresh error on **every turn**, which was actively obscuring debugging.

**Caveat on the model's honesty:** in a run that produced a correct fix, the model claimed it "wasn't able to run pytest
due to missing Python tools" — it had run pytest minutes earlier in the same session. Trust the diff, not the summary.
