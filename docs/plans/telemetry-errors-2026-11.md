# What community telemetry says after `model_not_found`

**Status:** Proposed (2026-11-17), audited against the code the same day. Nothing
implemented. **"Implementation plan (after the codebase audit)" at the end is the plan**;
the per-item "Plan" sections are kept for their reasoning.
**Found in:** a read-only review of community telemetry, 30 days to 2026-11-17: 53
installs, 319,205 requests, 98.5% successful. This fleet (`f50ca0b2`) and one install
reporting a simulated agent version are excluded.
**Follows:** [`backend-model-not-found.md`](backend-model-not-found.md), which fixed
the largest category.

| Category | Errors | Installs | Status |
|---|---|---|---|
| `model_not_found` | 1,515 | 4 | fixed on `main` (unreleased) |
| `bad_request` | 821 | 17 | **item 1**: cannot be broken down today |
| `context_too_long` | 156 | 1 | **item 2** |
| `other` | 64 | 4 | not pursued (below) |
| `connection_error` | 26 | 9 | scattered, not pursued |
| `timeout` | 7 | 3 | scattered, not pursued |
| `server_error` | 5 | 1 | this fleet's own, 2026-10-22 |

Three items, in the order to do them. Greenfield rules apply throughout: no flags,
reuse before build, one definition per question.

---

## Item 1: traces drop the backend's reason

### Evidence (reproduced 2026-11-17)

A request with `tools` sent through the router to `gemma3:27b` (which reports
`["completion", "vision"]`):

- The client got `400 {"error": {"message": "registry.ollama.ai/library/gemma3:27b does
  not support tools", ...}}`. That is correct, and since step 0 of the previous plan,
  true on every surface.
- The trace recorded
  `Client error '400 Bad Request' for url 'http://localhost:11434/api/chat'`.

The trace is what the dashboard, the health checks and `categorize_error` read, so
"does not support tools", "invalid model name", "does not support chat", a missing blob
and an MLX context overflow all reach telemetry as one `bad_request`. That is why 821
errors across 17 installs say nothing. The embed paths already record
`HTTP {status}: {body}` (`ollama_compat.py`, the embed branch), which is why
embedding errors categorise properly; the main dispatch path never did.

### Plan

1. **One helper, `common/errors.describe_failure(exc)`.** It returns
   `describe_exception(exc)`, then `: ` and `backend_error_detail(exc)` when that adds
   anything. Both pieces already exist, the second from the previous plan. The status
   stays first, because that is what `categorize_error` keys on.
2. **Use it wherever a trace or log line records an `HTTPStatusError`.** That is
   `_stream_with_retry` (three sites: `retried`, `failed` after first chunk or
   non-retryable, `failed` with no alternative or retries exhausted), the non-retry
   process path, and the MLX proxy's equivalents.
   - Client-facing text stays on `client_error_message`, so no URL reaches a client.
   - Traces are local, so the URL in them is fine.
3. **Harden `categorize_error` against digits in the backend's text.** Today it
   substring-matches `"500"`, `"400"` and so on anywhere in the message.
   - **The risk:** a 429 whose detail says `5000 tokens` would become
     `server_error`. `describe_upstream_incomplete` already documents this trap.
   - **The fix:** parse the status from httpx's prefix
     (`(Client|Server) error '(\d{3})`) first and categorise by it, then by
     keywords. Messages without the prefix keep today's rules.
   - **Tests:** the exact Ollama texts gathered so far (`does not support tools`,
     `invalid model name`, `does not support chat`, `model 'x' not found`,
     `model "x" not found, try pulling it first`, and the MLX runner's
     `input length (N tokens) exceeds the model's maximum context length (M tokens)`),
     each with the status it really arrives with.
4. **An in-stream `{"error": ...}` line is a failure with a reason, not a
   truncation.** Today `_recover_backend_errors` logs it and the trace says
   `incomplete` with the digit-free generic message, and telemetry counts only
   `failed`. So a backend that refused mid-stream is invisible to telemetry, and has
   no reason in the trace.
   - **Proposal:** record such a stream as `failed` with the error line's text.
   - **Decision needed:** this moves rows from `incomplete` to `failed`, which the
     `stream_reliability` check and the dashboard read. Keep `incomplete` for a stream
     that simply stops.

### Telemetry contract

- **The categories do not change, only what lands in them.** Some `bad_request` rows
  become `context_too_long` or `model_not_found`, because the text that decides them
  is finally present. `test_community_payload_golden.py` pins the payload's shape,
  not this mapping, and must still pass unchanged.
- **New categories are out of scope here.** ollamaherd.com drops unknown categories
  (its OpenAPI: "unknown categories are dropped"), so a `tools_unsupported` category
  needs the service first, and the page wins over the code. Decide from a week of
  item-1 data on this fleet whether one is worth it.

### Verification

- Unit: `describe_failure` for every exception shape (HTTP with JSON body, with text,
  with no body; transport errors unchanged), and the categoriser table above.
- Live: the `gemma3:27b` + tools request again; the trace must carry
  `does not support tools`, and the category must stay `bad_request`.
- There is no backfill: the reason was never written, so existing rows stay as they
  are.

---

## Item 2: Ollama's MLX runner refuses what llama.cpp truncates

### Evidence

One install (`a2ecc976`, Apple M1 Max and M5 Max, Ollama 0.40.16, agent 0.12.3)
reported 156 `context_too_long` errors in six days, and 157 of its 305
`qwen3.8:27b-mlx` requests failed. Telemetry categories are per install rather than
per model, but that model is the install's only one with errors.

**The mechanism, from Ollama's source (main, read 2026-11-17):**

- `server/routes.go`: `if m.IsMLX() { truncate = false }`, on both the chat and the
  generate paths. An MLX-format model's prompt is never truncated.
- `mlxrunner/pipeline.go`:
  `if len(tokens) >= r.contextLength { return fmt.Errorf("input length (%d tokens)
  exceeds the model's maximum context length (%d tokens)") }`.
- `mlxrunner/runner.go`: `r.contextLength = m.MaxContextLength()`. The limit is the
  model's own maximum, from its config, not `num_ctx`.
- `server/sched.go`: `mlxrunner.NewClient(modelName, req.opts.NumCtx)`, where
  `num_ctx` becomes `softContextLength`, "recommended limit to avoid poor
  performance". `ContextLength()` (what `/api/ps` reports) returns the soft value.

So, for an MLX-run model:
- `num_ctx` is advisory;
- a prompt longer than `num_ctx` but shorter than the model's maximum **succeeds**;
- a prompt over the maximum **fails**;
- the only context number herd can see (`/api/ps`) is the soft one.

### What herd assumes that is wrong for these models

Everything context-related in herd was written against llama.cpp, where `num_ctx` is a
hard window and overflow truncates silently:

- **Scoring and overflow:** the scorer's `context_fit` and `check_context_overflow`
  compare the estimate with the resident `context_length`, which here is the soft
  value. A prompt between soft and hard is penalised and told "input may be
  truncated", which is false. A prompt over the hard limit is not known to fail.
- **Truncation:** `input_truncated` cannot fire. Correct, but only by accident.
- **`num_ctx` handling:** the pin and override logic (`_pin_resident_num_ctx`,
  `FLEET_NUM_CTX_OVERRIDES`, `num_ctx_unmanaged`, `context_waste`) treat `num_ctx` as
  the window. **Unknown:** whether a changed `num_ctx` reloads an MLX-run model
  (`sched.go` `needsReload`). If it does, the pin still matters for avoiding reloads,
  even though it no longer bounds the prompt.
- **Open question:** which herd path produced `a2ecc976`'s `context_too_long` rows.
  The category needs "context" in the recorded text, the main path records httpx's
  text (item 1), and an in-stream error line is traced `incomplete`, which telemetry
  does not count. Phase 0 answers this.

### Plan

0. **Reproduce first.**
   - Pull the smallest MLX-format (`safetensors`) chat model in Ollama's library.
     This fleet has none: its only safetensors model is `x/z-image-turbo`.
   - Through each surface, send:
     - a prompt under `num_ctx`;
     - one between `num_ctx` and the model's maximum;
     - one over the maximum.
   - Record:
     - whether the refusal is an HTTP 400 or an in-stream error line;
     - what herd traces and which category results;
     - whether changing `num_ctx` reloads the model.
   - Everything below depends on these answers.
1. **The node reports the hard limit.**
   - The collector's model metadata comes from `/api/tags`, which has no context
     length. For models whose `format` is `safetensors` (Ollama's `IsMLX()`), also
     read `/api/show` `model_info["<arch>.context_length"]`, cached by digest.
   - That is few models and one call each per digest.
   - Add `context_length` to `ModelTagMeta`. It is heartbeat only, not telemetry.
2. **One definition, `serializers.effective_context_window(node, model)`.**
   - It returns the hard limit for an MLX-run model and the resident per-slot
     context otherwise.
   - It sits beside `resident_context_for` and `model_resident_on_node`, for the
     same reason: several callers need one answer.
   - Use it in `context_fit`, `check_context_overflow`, and the "may be truncated"
     header.
3. **Over the hard limit.**
   - Prefer a node or fallback model that fits: the scorer already eliminates on
     context fit, it just needs the right number.
   - On the Anthropic route, existing compaction can run when the estimate exceeds
     the hard limit.
   - Otherwise, Ollama's own 400 reaches the client, with its reason in the trace
     after item 1. Do **not** add a pre-rejection that says the same thing (the
     step 5b reasoning).
4. **Audit the `num_ctx` checks for MLX-run models.**
   - `context_waste`, `num_ctx_unmanaged`, `num_ctx_override_inert` and
     `kv_cache_bloat` describe llama.cpp semantics.
   - Each either excludes these models or says what `num_ctx` means for them,
     depending on Phase 0's reload answer.

### Verification

Phase 0's matrix, rerun after each phase, plus unit tests for
`effective_context_window` for both runners and for a node that reports no metadata
(an older agent, where it must behave exactly as today).

---

## Item 3: agent clients sent to models that cannot take tools

### Evidence

- **Telemetry (inferred, because item 1 hides the reason):**
  - `gemma3:27b`: 26 of 61 failed on `94b7284b`;
  - `codestral:22b`: 50 of 58 on `d97efe50`, and 20 of 56 on `ca84d0a1`;
  - all three installs are on agent 0.12.x.
  - `gemma3:27b` reports no `tools` capability (checked on this fleet). Codestral
    is believed not to support tools, but it isn't installed here to check.
- **herd chooses such models itself (confirmed).** `anthropic_autoroute.resolve_model`
  filters out embedding, decision and (for image requests) non-vision models, but
  never looks at `tools`. Reproduced:

  ```
  loaded=[gemma3:27b]                  disk=[qwen3-coder:30b]  -> gemma3:27b      (auto-loaded)
  loaded=[gemma3:27b, qwen3-coder:30b] disk=[]                 -> qwen3-coder:30b
  loaded=[gemma3:27b]                  disk=[]                 -> gemma3:27b
  ```

  Claude Code and Codex send tools on essentially every request, and both routes call
  `resolve_model` (`anthropic_compat.py`, `responses_compat.py`). So with a no-tools
  model loaded and a tool-capable one only on disk, herd routes every turn to a 400.

### Plan

1. **Auto-routing prefers tool-capable models when the request has tools.**
   - Pass `has_tools` beside the existing `has_images`. Both route bodies carry
     `tools`.
   - In candidate selection, when `has_tools`, a candidate whose reported
     capabilities exist **and** lack `"tools"` is unusable, but only if some
     candidate, loaded or on disk, reports `"tools"`.
   - That keeps the presence-only contract of `model_has_capability`: Ollama before
     0.34.1 under-reports, and a fleet where nothing reports `tools` behaves exactly
     as today.
   - **A tool-capable model on disk beats a no-tools model that is loaded.** A cold
     load once is better than a 400 on every turn. This is the one place the "prefer
     loaded" rule yields.
   - The `anthropic_no_chat_model` check calls the same `resolve_model`, so it stays
     in step.
2. **Static check for the explicit map.** When `FLEET_ANTHROPIC_MODEL_MAP` points a
   `claude-*` id at a model every node reports without `tools`, warn: every Claude
   Code turn will fail.
   - It needs no traffic, and reuses `_check_mapped_models_hot`'s map parsing and
     `get_fleet_capabilities`.
   - **What does it fire on when everything works?** Nothing: a tool-capable target,
     or a node that reports no capabilities, is skipped.
3. **Trace-based check after item 1:** `tools_unsupported` (name to confirm) per model,
   from failed traces whose backend reason is `does not support tools`, with the fix
   "map Claude/Codex to a tool-capable model". It catches clients other than Claude
   Code and Codex, and the explicit OpenAI-route case herd does not choose.
4. **Not doing:**
   - stripping `tools` (the agent needs them);
   - a pre-rejection (Ollama's 400 is already clear and reaches every surface).

### Verification

- Unit: the three-row table above flips its first row to `qwen3-coder:30b`.
- A fleet with no capability metadata routes exactly as today.
- An image request with tools needs a model that reports both, with the same
  fallback rule.
- Live, on this fleet:
  - auto-routing is off here (`FLEET_ANTHROPIC_MODEL_MAP` is set), so exercise it on
    a scratch router against a fake backend, not the real Ollama (see the as-built
    lesson in `backend-model-not-found.md`);
  - the static check can be exercised with the real map.

---

## Not pursued

- **`9623f6c5`:** 433 `bad_request`, mostly `qwen3-coder:30b` (364 of 410 failed),
  on agent 0.9.6, before 0.12.0's Claude Code and Codex fixes.
- **`d1bcbb94`:** 123 of 123 failed, on one day, to
  `hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF:IQ3_S`, on agent 0.10.1 with Ollama
  0.40.1. Most likely a model that Ollama could not load.
- **`other`:** `a197d025`'s 29 were `dinov2` image embeddings, 1% of 2,805, most
  likely the 0.11.3 image-format check doing its job. `d259d849`'s 32 were on one day.
  Item 1 makes future ones legible.
- **`/v1/completions` is not implemented**, although Ollama serves it and some
  autocomplete clients use it. Telemetry cannot see this, since a request for a route
  that does not exist is never traced. A separate decision.

---

## Codebase audit (2026-11-17)

Greenfield rules: no flag, no second code path, reuse before build. Every step was
checked against what exists. Two proposed helpers are not needed, one step shrinks to a
parameter, and the audit found three pieces of existing debt this work should retire
rather than build beside.

### Item 1: the reason in traces

| Proposed | Existing code | Verdict |
|---|---|---|
| New `describe_failure(exc)` | `describe_exception` has 22 call sites, and since step 0 of the previous plan **every one feeds a trace or a log line**: client-facing text moved to `client_error_message` | **Not needed: extend `describe_exception` itself** to append `backend_error_detail` for an HTTP error. Every trace path gets the reason, including the MLX proxy, with no call-site edits. Its "no category moves" promise changes deliberately, to "a category moves only toward the backend's stated reason", guarded by the categoriser change below |
| Use it at each trace site | Four sites hand-build `HTTP {status}: {text[:200]}`: `ollama_compat` (Ollama embed), `embedding_compat` (vision embed), `text_embedding_compat` (from a response, not an exception), and `benchmark_engine` (not a request trace) | Fold the two exception-based ones onto `describe_exception`. The categoriser reads both prefixes, so the third can stay. `test_error_descriptions.py::test_no_bare_stringification_remains` already scans for bare `str(exc)` and gains this pattern |
| Status-first categorising | `categorize_error` is the only categoriser. Callers: the telemetry rollup, `get_backend_not_found`, tests (including the golden payload) | Change it in place, so it stays one definition. Status from `(Client\|Server) error '(\d{3})` or `HTTP (\d{3}):`, then keywords. `get_backend_not_found`'s `LIKE '%404%'` stays valid because the status prefix is kept |
| In-stream error line as `failed` | `_recover_backend_errors` logs the line and drops its text. `_record_incomplete` is the one place an unfinished stream is traced. Per-request side channels already exist (`_request_partial`, `_request_done_reason`, `_request_meta`, read by routes through `pop_*`) | One more side channel, `_request_backend_error`, written by `_recover_backend_errors`. `_record_incomplete` traces `failed` with that text when it is set, and `incomplete` otherwise. No new path |

**Found: the client also loses the reason in that case.** An Ollama error line ends the
stream without `done`, so every translated surface sends the generic
`UPSTREAM_INCOMPLETE_MESSAGE`:
- OpenAI from `stream_from_node`;
- Anthropic through `anthropic_stream_error()`;
- Responses through `responses_failure_events(state)`.

That message says "Retry the request", which is the wrong advice for "input exceeds
the model's maximum context length". The same side channel supplies the real text to
all three, and each already takes a message argument. The native surface already
forwards the line.

### Item 2: the MLX runner

| Proposed | Existing code | Verdict |
|---|---|---|
| Detect MLX-run models by `format == "safetensors"` | That test is inline in `decode_parallelism_for`. `LoadedModel.runner` (from `/api/ps` since Ollama 0.40) states the runner for resident models, and CLAUDE.md already says "read it instead of inferring from format" | **Extract `serializers.runs_on_mlx_runner(node, model)`**: `runner` when resident, else `format`. `decode_parallelism_for` and item 2 both call it, so it is one definition |
| `effective_context_window` beside `resident_context_for` | `resident_context_for` is the single resident-context answer, and must keep returning the **soft** value: `_pin_resident_num_ctx` sends it to avoid reloads | Keep it unchanged. Add `prompt_limit_for(model, node)`: the hard limit for an MLX-run model, else `resident_context_for` |
| Use it in `context_fit` and overflow | **Debt found:** `scorer._score_context_fit` and `routing.check_context_overflow` still do their own first-match loop over `models_loaded`, the exact drift `resident_context_for`'s docstring says it ended. They were never migrated | Migrate both to `prompt_limit_for`. That also retires the two leftover first-match lookups |
| Node reads the hard limit | Nothing on the node calls `/api/show`; `get_available_model_meta` reads `/api/tags`, which carries no context length. `model_knowledge`'s catalog has `context_length`, but it is a static guess | One `/api/show` call per MLX-format model, cached by digest, into a new `ModelTagMeta.context_length`. **Not the catalog**: the node can be told, so it must not guess (the `kv_cache_bloat` lesson) |
| Compaction over the hard limit | The Anthropic route already clears tool uses at 100K and force-compacts at 150K (`anthropic_auto_clear_tool_uses_trigger_tokens`, `context_compaction_force_trigger_tokens`) | No new mechanism: use `min(trigger, prompt_limit_for(target))`, so the existing layers fire before a model's hard limit. This applies to the always-on layers. Budget compaction sits behind `context_compaction_enabled`, which is off by default (existing debt; see below) |
| Audit the `num_ctx` checks | `_check_kv_cache_bloat`, `_check_num_ctx_unmanaged`, `_check_num_ctx_override_inert`, `_check_context_waste` | Each gets `runs_on_mlx_runner` once Phase 0 says what `num_ctx` does there; one helper, four call sites |

### Item 3: tools

| Proposed | Existing code | Verdict |
|---|---|---|
| `has_tools` into auto-routing | `resolve_model` already takes `has_images` and `capabilities` (`get_fleet_capabilities`, a union across online nodes, presence-only). Both routes compute `has_images` the same way | A parameter mirroring `has_images`, filtered in `_candidate_score` beside `want_vision`. Over a union, "reports capabilities but not `tools`" means no online node reported it, which is the presence-only reading |
| `anthropic_no_chat_model` stays in step | **Debt found:** the check calls `resolve_model(..., auto_route=auto_route)` **without `capabilities`**, so it already disagrees with the routes on anything only Ollama's report detects (embedders, decision models). The plan's "stays in step" was wrong | Pass `capabilities=get_fleet_capabilities(...)` and `has_tools=True` (Claude Code always sends tools). One answer for the route and the check |
| Static map check | **Debt found:** `health_engine` parses `FLEET_ANTHROPIC_MODEL_MAP` from the environment by hand twice (`_check_mapped_models_hot`, `_check_anthropic_no_chat_model`) | Extract one `_anthropic_model_map()` and use it three times. The new rule lives inside `_check_mapped_models_hot`, which already iterates the mapped targets, rather than in a new method |
| Trace-based tools check | `get_backend_not_found` reads failed and retried rows by node and model | Generalise it to `get_backend_failures(lookback)`, returning `(node, model) -> {category, message sample, counts}`. `backend_model_missing` and the tools check read one query. The tools match is on the backend's text (`does not support tools`), not a new telemetry category, so the wire contract is untouched |

### Out of scope, recorded

- `context_compaction_enabled` (default off) is a feature flag under this project's
  own rules. That's for its own decision, not here.
- `/v1/completions` is still not implemented.

## Implementation plan (after the codebase audit)

1. **Item 1, the reason everywhere.**
   - Extend `describe_exception`, and harden `categorize_error` (status first).
   - Fold the two hand-built `HTTP {status}:` exception sites onto it.
   - Add the `_request_backend_error` side channel: the trace becomes `failed` with
     the reason, and all three translated surfaces send that reason instead of
     "retry".
   - Tests: the Ollama message table; the golden payload unchanged; the
     stringification guard extended.
2. **Item 3a/b, routing and static check.**
   - `has_tools` in `resolve_model`.
   - The health check passes the same `capabilities` and `has_tools=True`.
   - One model-map parser.
   - The no-tools target rule inside `_check_mapped_models_hot`.
3. **Item 3c, the trace check.** Generalise `get_backend_not_found` to
   `get_backend_failures`, and add the tools check on it. This depends on item 1.
4. **Item 2.**
   - Phase 0 reproduction first.
   - Then `runs_on_mlx_runner`, the node's `context_length` for MLX-format models,
     and `prompt_limit_for`.
   - Migrate the scorer and the overflow check onto it, retiring the first-match
     loops.
   - Cap the existing compaction triggers.
   - Audit the four `num_ctx` checks.

**Decisions still open** (unchanged from the item sections):
- Whether an Ollama error line is traced `failed` rather than `incomplete`. The audit
  makes it cheap, and the client-facing half is a clear fix either way.
- Whether a tools telemetry category is ever worth the cross-repo change.
