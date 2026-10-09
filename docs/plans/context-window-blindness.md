# Context-window blindness — truncation, unmanaged windows, and empty-completion misattribution

**Status**: **PROPOSED, not started.** No code written. Phases 1–3 are health checks reading data herd already records; Phase 4 is a classification change; Phase 5 is cleanup with a caveat that makes it optional.
**Date**: 2026-10-09
**Trigger**: A field report from an unrelated project (a code-scanning harness) describing how its provider layer lost control of the context window by talking to Ollama over `/v1`. Reproduced below, then checked against herd line by line.
**Prereq context**: the `OLLAMA_CONTEXT_LENGTH` and context-sizing gotchas in [`../../CLAUDE.md`](../../CLAUDE.md), the 2026-09-28 autopsy in [`../observations.md`](../observations.md), and [`context-size-protection.md`](context-size-protection.md) (the existing protection this extends).

## Why this plan exists

The report's mechanism: **an OpenAI-format request has no field for the context window**, so whatever the server loaded applies. Ollama derives that from GPU memory when unconfigured — 4k under 24 GiB, 32k to 48 GiB, 256k above. Their harness sized prompts for 32k, got 4k, and silently truncated 2 of 7 discovery prompts to 2,050 of ~6,000 tokens. Their own truncation check stood down on that path and printed `input truncation: NOT CHECKED`. Exit code 0. At the other extreme a very large window made `qwen3-coder:30b` fail to load, and the harness reported that as a model refusal.

**The root cause does not apply to herd, and that is worth stating first.** herd calls Ollama's native `/api/chat` and `/api/generate` — never `/v1` — so it has a `num_ctx` field and uses it. herd is in fact the report's own option 1 ("detect Ollama behind an OpenAI-compatible address and use the native client") already implemented: a client that sends OpenAI-format to herd's `/v1` cannot express a context, and herd supplies one on their behalf before dispatch.

But four of their five findings land somewhere real.

| # | Their finding | herd's exposure | Verified |
|---|---|---|---|
| 1 | OpenAI path cannot set the window | **Does not apply downstream** — herd uses `/api/chat`. Applies to the **MLX** path, where `mlx_lm.server` is OpenAI-native and context is fixed at launch | `streaming.py:677,679,797`; `ps` shows `--kv-bits 8 --quantized-kv-start 0`, no `--max-kv-size` |
| 2 | Ask the server what window it loaded, and budget to it | **Already done for Ollama** — `context_length` per loaded model rides the heartbeat, surfaces on `/fleet/status`, and feeds `kv_cache_bloat` + `num_ctx_override_inert`. **Not possible for MLX** — `mlx_lm.server` does not expose it | heartbeat `LoadedModel.context_length` |
| 3 | Treat a server token count far below the estimate as a possible cut | **Real gap.** Nothing compares recorded `prompt_tokens` against what was sent | no `truncat*` match in `server/` outside unrelated contexts |
| 4 | Explain an empty completion as a load failure, not a refusal | **Real gap.** `completion_tokens == 0` is classified nowhere. This fleet already hit the adjacent version — the 2026-09-22 model-load deadlock | grep for empty-completion handling finds only `num_predict` advice |
| 5 | Record the context the server used, and any spill to system memory | **Mostly done.** Context recorded as above; swap and compressed memory added 2026-10-04. Spill attribution per model is not tracked | `MemoryMetrics.swap_used_gb`, `compressed_gb` |

**And a different shape of the same class, which their report does not cover because their harness has one model.** herd injects `num_ctx` **only for models with an entry in `FLEET_NUM_CTX_OVERRIDES`**:

```
overrides configured:  5 entries
models on disk:       30
covered:               9
NOT covered:          21   <- Ollama's own heuristic applies
```

So 21 of 30 models serve with no explicit window. Their report describes the **small**-window failure (truncation at 4k). This fleet is exposed at the **large**-window end: `server/routes.go` picks `defaultNumCtx = 262144` for any node with ≥47 GiB VRAM **with no `numParallel` term**, so a request that omits `num_ctx` here asks for `-c 1048576` and KV scales until it spills to RAM ([ollama#14116](https://github.com/ollama/ollama/issues/14116), open; community fix #14120 closed unmerged). Same missing-explicit-context mechanism, opposite consequence.

## Deliberately excluded

- **Changing dispatch or routing.** Every phase below is observation or classification. The 2026-09-22 regression came from changing a context on inference from prompt-size arithmetic; nothing here repeats that.
- **Auto-generating overrides for the 21 uncovered models.** `context_optimizer` can already do this and it is off for a reason — see the context-sizing gotcha. Phase 2 makes the gap *visible*; whether to close it per model is an operator decision.
- **Asking `mlx_lm.server` for its window.** It does not expose one. The existing wall-clock timeout returning 413 `prompt exceeded effective context; try /compact` is a symptom-side guard and remains the available answer.
- **Their option 1 as a code change.** Already how herd works.

## Implementation principles

Greenfield, so: least new surface, most reuse. No `FLEET_*` flags — correct behaviour is the behaviour. Each phase must answer **"what does this fire on when everything is working?"**, because four checks in two weeks fired on correct behaviour (`priority_model_not_loaded`, `context_waste`, the stale reaper/worker, and the `num_ctx cannot apply` log). That question is now the gate on any new check.

## Phase 1 — `prompt_truncated` health check

**The gap.** A backend that truncates leaves no trace herd reads. `prompt_eval_count` is the **full prompt length** — verified 2026-10-02 on Ollama 0.34.4 by resending an identical prompt (count stayed 4074 while `prompt_eval_duration` fell 2.235 s → 0.021 s, so the cache was hit and the count did not move). That verification is what makes this phase possible: if the recorded count falls well short of what was sent, the backend cut it.

**Build.** A check comparing `request_traces.prompt_tokens` against an estimate of the request's own size, flagging a sustained shortfall. Needs the estimate stored per request — see Phase 5 for which estimator.

**Threshold.** Must tolerate the honest reasons a count differs from an estimate: tokenizer mismatch (ours is cl100k, the model's is not), images counted differently, and template overhead the server adds. A shortfall of **>25% sustained over several requests on one model** is the starting proposal, not a single-request alarm.

**What it fires on when everything is working:** nothing, provided the threshold is a ratio and sustained. A single short count is noise; the signature of truncation is *every* request to one model clipping at the same ceiling.

**Verification.** Reproduce deliberately: set a small `num_ctx` for one model via `FLEET_NUM_CTX_OVERRIDES`, cold-load it, send a prompt well past it, and confirm the check fires. Then confirm it stays silent across a normal day. Both directions, as with the reaper.

## Phase 2 — `num_ctx_unmanaged` health check

**The gap.** 21 of 30 models on this node serve with no explicit window, so Ollama's GPU-memory heuristic decides — and on a ≥47 GiB box that heuristic is the ollama#14116 trap.

**Build.** Flag models that **served traffic in the window** and have no `FLEET_NUM_CTX_OVERRIDES` entry. Traffic-gated deliberately: 21 unused models on disk are not a problem, and a card listing them would be exactly the noise this project has been removing.

**Severity.** INFO when the node has memory headroom, WARNING when it does not — reusing `_free_memory_gb` / `_OVERSIZE_HEADROOM_GB`, the same impact-follows-consequence shape as `num_ctx_override_inert`.

**What it fires on when everything is working:** a model deliberately left unmanaged. That is a real state, so the text must say *"no explicit window; Ollama's heuristic applies — on this node that is `defaultNumCtx × OLLAMA_NUM_PARALLEL`"* and offer the override, not imply breakage. If the fleet ever runs mostly unmanaged models by choice, this becomes noise and should be reconsidered rather than tuned.

## Phase 3 — record the gap, do not guess at MLX

**Build.** Extend the `num_ctx_override_inert` data payload, and `/fleet/status`, with the window herd *requested* alongside the one the backend *reports*, so the two are comparable without reading logs. For MLX servers, record that the window is **unknown** rather than absent — an explicit "cannot ask" is the honest value, and it mirrors the `backend_clients` convention where empty means "none seen or could not look".

**Why it matters.** The report's option 5. herd is most of the way there for Ollama; this closes the MLX half honestly instead of leaving a field that reads as zero.

## Phase 4 — stop reading a load failure as a refusal

**The gap.** `completion_tokens == 0` with a 200 is recorded as `completed`. Their report hit exactly this: a large window with several slots made `qwen3-coder:30b` fail to load, and the harness called it a refusal, "which pointed in the wrong direction." This fleet has the adjacent precedent in the 2026-09-22 deadlock.

**Build.** Classify a successful-status response with zero completion tokens as its own outcome. **Not** `failed` — the HTTP exchange succeeded and the distinction is the point. A distinct status, or a flag on the trace, so the dashboard can say *"the model produced nothing"* rather than counting it as a normal completion.

**Care needed.** Thinking models legitimately produce zero *content* tokens when the whole budget goes to reasoning — `model_knowledge.py:898` and `streaming.py:1565` already document this, which is why `thinking_min_predict` exists. So the classification must read total output including reasoning, or it will mislabel every thinking model's tight-budget response. **This is the phase most likely to fire on correct behaviour** and wants the most care.

## Phase 5 — one token estimator, with a caveat that may block it

**The finding.** Seven separate `len(text) // 4` sites plus one tiktoken-backed estimator:

| Location | Form |
|---|---|
| `anthropic_translator.py:558` | tiktoken cl100k, falls back to chars/4 |
| `context_management.py:102` | chars/4, docstring: *"Match the compactor's estimator"* |
| `context_compactor.py:540` | chars/4 |
| `mlx_proxy.py:1298` | chars/4 |
| `streaming.py:707,709` | chars/4 (thinking + output) |
| `scorer.py:628` | chars/4 |
| `routes/anthropic_compat.py:1191` | tiktoken, documented best-effort |

Phase 1 needs an estimate, and the greenfield principle says extract a shared helper rather than add an eighth.

**The caveat that may stop this.** The chars/4 in `context_management` is **deliberate** — its docstring says it exists to match the compactor, because the two must agree on when compaction triggers. Replacing it with tiktoken changes compaction thresholds, and compaction behaviour is not what this plan is about. So either:

- **(a)** add the shared helper, use it for Phase 1 only, and leave the compaction pair alone with a comment saying why they are not unified; or
- **(b)** unify everything and re-tune the compaction triggers, which is a separate piece of work with its own soak.

**Recommend (a).** Unifying estimators to tidy a table is how a cleanup becomes an incident. The duplication is worth *recording* now and consolidating only when something needs it.

## Order and dependencies

```
Phase 5a (shared estimator, Phase-1 use only)
   └─ Phase 1  (prompt_truncated)
Phase 2  (num_ctx_unmanaged)      — independent
Phase 3  (record requested vs reported) — independent
Phase 4  (empty-completion classification) — independent, most care
```

Phases 2 and 3 are the cheapest and read data already in the heartbeat. Phase 1 needs 5a first. Phase 4 is independent but should go last, because distinguishing "produced nothing" from "thinking model spent its budget" is the subtlest judgement here and the one most likely to produce a false card.

## Open questions

1. **Phase 1 threshold.** Is 25% sustained right? It should be derived from measured tokenizer disagreement on this fleet's real traffic, not picked. Measure before implementing.
2. **Phase 2 scope.** Traffic-gated per node, or fleet-wide? Per node is more accurate and noisier.
3. **Phase 4 representation.** New `status` value, or a boolean on the existing `completed` row? A new status touches every consumer of `request_traces.status`, including the telemetry error histogram; a flag does not.
4. **Does any of this want to be upstream?** ollama#14116 is open and unmerged; a `defaultNumCtx` that accounted for `numParallel` would remove Phase 2's reason to exist. Worth a comment on that issue either way.

## What this plan is not

It does not make herd manage the 21 unmanaged models, does not change any routing decision, and does not touch the MLX wall-clock guard. It makes four currently-invisible conditions visible and fixes one misattribution. Everything here reads data herd already has, which is the main reason it is cheap.
