# Context-window blindness — truncation, unmanaged windows, and empty-completion misattribution

**Status**: **IMPLEMENTED 2026-10-09**, except Phase 1 step 3, which is deliberately deferred until the trace field it depends on has produced calibration data. Deployed and verified live. Revised earlier the same day after a codebase audit that cut the plan roughly in half.

**What landed:** `estimated_tokens` on the request, the trace and the schema; the pre-existing overflow detection in `routing.py` now persisted as an `overflow` event; `num_ctx_unmanaged`; `MlxServerInfo.context_length = None` as an explicit unknown; and `no_output` as a fourth `status` value folded into the existing `_check_stream_reliability` as `empty_generations`. 46 health checks, 1,773 tests.

**Two things the build changed, both caught by running it rather than reasoning about it:**

1. **`num_ctx_unmanaged` fired on `nomic-embed-text` with 2,829 requests on its first live run.** The embedding exclusion used `model_has_capability`, which is presence-only by contract — and native-server models are not in Ollama's metadata at all, so it returned False for exactly the models that most needed excluding. Fixed by using `is_text_embedding_model`, the registry that actually routes them.
2. **Phase 4's documented trap turned out not to apply, and a different one did.** `completion_tokens` is Ollama's `eval_count`, which *includes* reasoning tokens — verified live: a thinking model at `num_predict=24` returned `eval_count=24` with zero content and 41 chars of thinking. So reading that field cannot misfire on a thinking model. But **48,449 completed traces carry NULL counts** (embeddings, MLX), so conflating NULL with zero would have classified all of them. The guard is `is not None`, not the comparison to zero.

**Calibration data already arriving**, and it justifies deferring step 3: measured pairs are `est 5,685 / actual 4,288` (75%) on a real request and `est 10 / actual 73` (730%) on a tiny one. The estimator overestimates long prompts and badly underestimates short ones, because the chat template dominates when the prompt is small. A single ratio threshold in either direction would be wrong.
**Date**: 2026-10-09 (revised same day)
**Trigger**: A field report from an unrelated project (a code-scanning harness) whose provider layer lost control of the context window by talking to Ollama over `/v1`. Notes sent back to them: [`../handoffs/2026-10-09-context-window-notes-for-external-scanner.md`](../handoffs/2026-10-09-context-window-notes-for-external-scanner.md).
**Prereq context**: the `OLLAMA_CONTEXT_LENGTH` and context-sizing gotchas in [`../../CLAUDE.md`](../../CLAUDE.md), the 2026-09-28 autopsy in [`../observations.md`](../observations.md), and [`context-size-protection.md`](context-size-protection.md).

## Why this plan exists

The report's mechanism: **an OpenAI-format request has no field for the context window**, so whatever the server loaded applies. Ollama derives that from GPU memory when unconfigured. Their harness sized prompts for 32k, got 4k, and truncated 2 of 7 discovery prompts to 2,050 of ~6,000 tokens while exiting 0 and printing `input truncation: NOT CHECKED`.

**The root cause does not apply to herd** — it calls `/api/chat` and `/api/generate`, never `/v1`, so it has a `num_ctx` field and uses it. herd is their option 1 already implemented. Four adjacent gaps looked real; after the audit, **two** are.

## What the audit changed

This section exists because the first draft of this plan proposed building things that already exist. Recording that is cheaper than someone rediscovering it.

| First draft said | Audit found | Effect |
|---|---|---|
| Build a `prompt_truncated` check comparing an estimate to `prompt_tokens` | **The comparison already exists.** `routing.py:680–704` computes `ScoringEngine.estimate_tokens(messages)` against the winning node's `context_length`, logs `"input may be truncated by Ollama"`, and returns an `X-Fleet-Context-Overflow` header. Driven off the `context_fit` scorer signal going negative | Phase 1 shrinks from "build a detector" to "persist the detector we have". No new estimator, no new comparison |
| Build a Phase 3 to record requested vs reported window | **Already built.** `_record_context_protection` stores `client_num_ctx` and `loaded_ctx`; `num_ctx_override_inert`'s payload exposes `configured`/`resident`/`ratio` | Phase 3 **cut**, except the MLX half |
| Phase 5: consolidate "seven `len(text) // 4` estimators" | **Overstated.** They are three tiers with different consumers: `ScoringEngine.estimate_tokens(messages)` (routing; handles images at 150 tok each and per-message overhead), `_total_tokens` for compaction triggers (and `context_management`'s 28 lines vs `context_compactor`'s 162 are **not** duplicates), and `anthropic_translator.estimate_tokens(text)` (tiktoken, for the client-facing `count_tokens` endpoint where accuracy matters) | Phase 5 **cut**. Use the existing canonical one |
| Open question: new `status` value or a boolean flag for empty completions? | **Precedent answers it.** `request_traces.status` already carries `incomplete` and `client_disconnected`, queried as a set by `get_stream_reliability_24h` and surfaced by `_check_stream_reliability` | Phase 4 extends that, rather than adding a status *and* a check |

Net: two phases to build, one small addition, two cut.

## Deliberately excluded

- **Changing dispatch or routing.** Everything below is persistence, aggregation or classification. The 2026-09-22 regression came from acting on prompt-size arithmetic; nothing here repeats it.
- **Auto-generating overrides for the uncovered models.** `context_optimizer` can already do this and it is off for a documented reason. Phase 2 makes the gap *visible*; closing it per model stays an operator decision.
- **Asking `mlx_lm.server` for its window.** It does not expose one. The wall-clock 413 (`prompt exceeded effective context; try /compact`) stays the available guard.
- **A new token estimator.** See the audit table.

## Implementation principles

Greenfield: least new surface, most reuse. No `FLEET_*` flags — correct behaviour is the behaviour. Tuning goes in class constants beside `_OVERSIZE_HEADROOM_GB`, not settings. Every phase must answer **"what does this fire on when everything is working?"**, because four checks in two weeks fired on correct behaviour (`priority_model_not_loaded`, `context_waste`, the stale reaper/worker, and the `num_ctx cannot apply` log).

## Phase 1 — persist the overflow detection that already exists

**What exists.** `_context_overflow_headers()` in `routing.py` already detects the condition and tells the *client* via a header. What it doesn't do is leave a record: `_record_context_protection` is never called from `routing.py`, and `estimated_tokens` is not carried onto the trace. So the one signal that would let anyone ask "how often is input being truncated?" is fire-and-forget.

**Build, reusing the established pattern.** The `override_inert` path is the template: streaming notices a context condition → `_record_context_protection(action, model, node_id, client_num_ctx, loaded_ctx)` → `health_engine` reads `get_context_protection_events()` and renders a card.

1. Call `_record_context_protection("overflow", model, node_id, estimated_tokens, ctx_length)` from the existing overflow branch. The signature already fits — `client_num_ctx` carries the estimate, `loaded_ctx` the window. **No new event type machinery.**
2. Add `estimated_tokens` to the trace row, from the value `routing.py:205` already computed, so the estimate and `prompt_tokens` sit side by side for later calibration.
3. A check reading those events, mirroring `_check_num_ctx_override_inert` (stateless, reads events, severity follows impact).

**Threshold.** Derive it, don't pick it. Once (2) has landed for a few days, `estimated_tokens` vs `prompt_tokens` on real traffic gives the honest tokenizer-disagreement baseline — our estimator is chars/4 plus 150/image, the model's is its own. Until that number exists, any ratio is a guess. **Land (1) and (2) first; let (3) wait for the data.**

**What it fires on when everything is working:** nothing, if the check requires *repeated* overflow on one model. A single overflow is a client sending a large prompt, which herd already handles by header. The signature worth a card is the same model overflowing persistently — meaning its window is wrong, not that one request was big.

**Verification.** Set a small override for one model, cold-load it, send a prompt past it; confirm the event records and the card appears. Then confirm silence across a normal day. Both directions.

## Phase 2 — `num_ctx_unmanaged` check

**The gap.** herd injects `num_ctx` only for models with a `FLEET_NUM_CTX_OVERRIDES` entry. Measured on this node: **5 override entries, 9 of 30 models covered, 21 uncovered**. Those 21 get Ollama's GPU-memory heuristic, which on a ≥47 GiB box is the ollama#14116 trap (`defaultNumCtx = 262144`, no `numParallel` term, so `-c 1048576`). The report shows the small-window failure; this is the large-window one.

**Build — assembly, no new helpers.** Every piece exists:

| Need | Existing |
|---|---|
| which models have an override | `_operator_num_ctx_overrides()` (added 2026-10-02 so `context_waste` and `num_ctx_override_inert` can't disagree) |
| which models served traffic | `request_count` from `trace_store.get_prompt_token_stats()`, already fetched by `analyze()` for `context_waste` |
| severity follows impact | `_free_memory_gb(nodes)` vs `_OVERSIZE_HEADROOM_GB` |

Traffic-gated deliberately: 21 unused models on disk are not a problem, and listing them would be exactly the noise this project has been removing.

**What it fires on when everything is working:** a model deliberately left unmanaged — a real state. So the text must say *"no explicit window; Ollama's heuristic applies, which on this node is `defaultNumCtx × OLLAMA_NUM_PARALLEL`"* and offer the override, not imply breakage. If the fleet ever runs mostly-unmanaged by choice, this becomes noise and should be reconsidered rather than tuned.

## Phase 3 (reduced) — record MLX's window as unknown

Everything else in the original Phase 3 is already built. What remains: MLX servers report no window, and the heartbeat has no field for it, so "unknown" and "zero" are indistinguishable.

Add an explicit unknown for MLX entries, following the `backend_clients` convention where an empty list means "none seen *or* could not look" — deliberately indistinguishable, documented as such, because the alternative is a field that reads as a real zero.

## Phase 4 — stop reading a failed load as a completion

**The gap.** A 200 with `completion_tokens == 0` is recorded as `completed`. The report hit exactly this: a large window with several slots made `qwen3-coder:30b` fail to load, and their harness called it a refusal, *"which pointed in the wrong direction."* Precedent here: the 2026-09-22 model-load deadlock.

**Build — extend, don't add.** `status` already carries `incomplete` and `client_disconnected` for "the HTTP exchange succeeded but the output was wrong", set from `streaming.py:334,665`, aggregated by `get_stream_reliability_24h`, surfaced by `_check_stream_reliability`. A fourth value joins a set that is already queried as a set, and the existing check grows a case. No new check, no new query.

**The trap, and it is the main risk in this plan.** Thinking models legitimately emit **zero content tokens** when the whole `num_predict` budget goes to reasoning — documented at `model_knowledge.py:898` and `streaming.py:1565`, and the reason `thinking_min_predict` exists. A check reading content length would mislabel every thinking model on a tight budget. It must read **total** output including the reasoning channel, which `streaming.py:707,709` already counts separately (`thinking_token_count` / `output_token_count`).

**What it fires on when everything is working:** a thinking model that spent its budget reasoning, if implemented carelessly. Hence last, and hence the explicit test for it.

## Order and dependencies

```
Phase 1 step 1  (record the overflow event)        — independent, smallest
Phase 1 step 2  (estimated_tokens onto the trace)  — independent
Phase 2         (num_ctx_unmanaged)                — independent, pure assembly
Phase 3         (MLX window unknown)               — independent, smallest
   └─ Phase 1 step 3 (the check) — WAITS for step 2 to produce calibration data
Phase 4         (empty-completion classification)  — last; highest false-positive risk
```

Phases 1.1, 1.2, 2 and 3 are all small and independent. Phase 1.3 is deliberately gated on having real estimate-vs-actual data rather than a guessed threshold. Phase 4 goes last.

## Open questions

1. **Phase 1.3 threshold** — must come from measured tokenizer disagreement on this fleet, which Phase 1.2 produces. Do not pick a number before then.
2. **Phase 2 scope** — per node or fleet-wide? Per node is more accurate and noisier.
3. **Does any of this belong upstream?** ollama#14116 is open and unmerged; a `defaultNumCtx` accounting for `numParallel` would remove Phase 2's reason to exist. Worth a comment there either way.
4. ~~New status or boolean for Phase 4?~~ **Answered by the `incomplete` precedent.**

## What this plan is not

It does not make herd manage the 21 unmanaged models, change any routing decision, touch the MLX wall-clock guard, or add a token estimator. After the audit it adds **one event call, one trace field, one check, one status value, and one explicit unknown** — and defers the only threshold until data exists to set it from.
