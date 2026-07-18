# Post-0.32.1 Enhancements — acting on the Ollama 0.24 → 0.32.1 audit

**Status**: Phases 1 & 3 **IMPLEMENTED** 2026-07-17. Phase 2 **verified a non-issue** by probe (below) — no code needed. Phase 4 (native-MLX eval) is the only remaining work; it needs the held Ollama restart.
**Date**: 2026-07-17
**Prereq context**: [`ollama-0.32-upgrade-and-mlx-evaluation.md`](ollama-0.32-upgrade-and-mlx-evaluation.md) (the upgrade itself, EXECUTED) and [`../issues/ollama-native-mlx-runner.md`](../issues/ollama-native-mlx-runner.md) (the MLX-subsystem question).

## Why this plan exists

Ollama moved 0.24.0 → 0.32.1 (skipping 0.25–0.29). An audit of the 15 intervening releases against the codebase found **one candidate behavior change** (which a probe then showed doesn't affect us), **one accuracy change to record**, and **two opportunities** the upgrade unlocks. This plan turns those findings into scoped, independently-shippable work.

The releases that touch a router/proxy like ours are a small subset — most of the changelog is `ollama launch`, the interactive agent, ChatGPT/Codex integrations, and Windows/CUDA fixes. The relevant items:

| Ollama change | Release | Our exposure |
|---|---|---|
| Over-context single message "now errors" per changelog | 0.30.9 | **Probed on 0.32.1: still truncates (200), doesn't error — non-issue** |
| Token accounting **includes cached prompt tokens** | 0.30.2 | `prompt_eval_count` → traces → num_ctx sizing (now *more* accurate) |
| Native MLX runner matured (MTP, spec-decode, cache-leak fix) | 0.30.11–0.32.1 | We bypass it with standalone `mlx_lm.server` |
| Gemma 4 family shipped (multimodal, MoE, QAT) | 0.30.3–0.31.1 | Catalog stops at Gemma 3 |
| Deprecation warnings for qwen2.5(-coder), llama3.x, etc. | 0.32.0 | Catalog still recommends several |
| `ollama ps` mmap double-count **fixed** | 0.30.11 | *Helps* our new KV-sizing — no action |

Ordered below by **independence from the restart you're holding** — Phases 1–3 ship now; Phase 4 needs the restart.

## Implementation principles (from the codebase audit, 2026-07-17)

This is a greenfield project. Two rules shaped the phases below after auditing the touch-points:

- **No feature gating.** These enhancements introduce **no new `FLEET_*` toggles**. The correct behavior *is* the behavior — a flag defaulted-off would just be debt and a second code path to test. (The one existing knob these interact with, `FLEET_ANTHROPIC_AUTO_ROUTE`, already shipped and stays as-is.)
- **Reuse before build.** Every phase maps onto machinery that already exists; the audit found no case needing a new subsystem:
  - **Vision auto-routing is automatic** — `rank_candidates` → `_candidate_score` → `is_vision_model` already consults `secondary_categories`, so a Gemma-4 catalog entry with `category=VISION` becomes a vision candidate with **zero new code** (Phase 1).
  - **Over-context is already surfaced** — `client_error_passthrough` ([routing.py:69](../../src/fleet_manager/server/routes/routing.py:69)) already turns any backend **4xx** into a clean 4xx, and `check_context_overflow` ([routing.py:533](../../src/fleet_manager/server/routes/routing.py:533)) is already wired into all three routes. Phase 2 becomes *correct a stale message + one classifier*, not a new guard (Phase 2).
  - **A benchmark harness exists** — `BenchmarkRunner` + `benchmark_engine.send_request` already measure `tokens_per_sec`/`ttft`; the MLX evaluation reuses them for apples-to-apples numbers (Phase 4).

---

## Phase 1 — Model catalog: Gemma 4 + hygiene — ✅ IMPLEMENTED *(data + tests, no restart)*

**Problem.** `model_knowledge.py` (50 models) stops at Gemma 3. Gemma 4 shipped across 0.30.3–0.31.1: `gemma4:12b` (multimodal), `gemma4:26b-a4b` (MoE), `gemma4:31b`, plus QAT variants (`e2b/e4b/12b/26b-a4b/31b-it-qat`). A pulled `gemma4:*` is scored by name-heuristic only, and — critically — it is **not offered as a vision candidate** by the auto-routing we just built, despite being "high-performance multimodal." Nemotron-3-Ultra and Command A / Cohere2Moe are also absent.

Separately, 0.32.0 added deprecation warnings for `qwen2.5(-coder)`, `llama3.x`, `mistral`, `codellama`, `starcoder`, base `deepseek-r1` — several of which our catalog still lists as recommended coders.

**Approach.**
1. Add Gemma 4 entries to `MODEL_CATALOG` with `category=VISION`, `secondary_categories=[GENERAL, CODING]`, MoE `active_params_b` where applicable (`26b-a4b` → 4B active), QAT `ram_gb` reflecting the smaller footprint, and benchmarks from the model card. Mirror the existing `gemma3:*` shape.
2. Add `nemotron-3-ultra` (REASONING) and a Command A / Cohere2Moe entry (GENERAL) — lighter touch; benchmarks may be sparse, that's fine (auto-routing tolerates unknown quality).
3. **Hygiene:** update `notes=` on the `qwen2.5-coder:*` entries to point at `qwen3-coder:30b` as the current pick; do **not** delete them (users may still run them) — just stop leading with them.

**Reuse — this is data-only.** Confirmed by audit: no new code path. `rank_candidates` already routes the vision filter through `is_vision_model`, which already reads `secondary_categories`; `_candidate_score` already reads `spec.category` for the tier bonus. A `category=VISION` entry (matching the existing `gemma3:*` shape) is picked up automatically. The catalog-hygiene change is `notes=` string edits — no logic.

**Verification.**
- `lookup_model("gemma4:12b").category == VISION`; `is_vision_model("gemma4:12b")` is True.
- A resolver test: with `gemma4:12b` loaded and an image request, `rank_candidates([...], "claude-sonnet-4-5", want_vision=True)` returns it. Extends `test_anthropic_autoroute.py`.
- Full suite + `ruff` clean.

**Risk.** Very low — additive data. The only behavioral effect is that auto-routing gains a vision option and better-scored gemma4 candidates.

---

## Phase 2 — Over-context request handling (0.30.9) — ✅ PROBED: NON-ISSUE, no code

**Outcome (2026-07-17).** The probe ran and the predicted behavior change **does not manifest on our path**. Direct against the installed 0.32.1: `gemma3:4b` at `num_ctx=512` (clamped up to Ollama's 2048 floor), `/api/chat` with a ~5000-token single message → **HTTP 200**, `prompt_eval_count: 1027` (truncated to fit), *not* an error. So `check_context_overflow`'s "may be truncated" message is **still accurate**, there's no new error class to classify, and no code changes. Recorded in `docs/observations.md`. The analysis below is retained for the record — this is exactly why the plan front-loaded the probe.

<details><summary>Original Phase 2 analysis (superseded by the probe)</summary>

**Problem.**
> "Ollama will now return an error if a single message is larger than the current context window."

- **Anthropic route is covered** — `FLEET_ANTHROPIC_MAX_PROMPT_TOKENS` (413 pre-check) + tool-result clearing/compaction.
- **OpenAI & Ollama routes are not** — no prompt-size guard (`openai_compat.py`/`ollama_compat.py` have none), and `check_context_overflow` ([routing.py:533](../../src/fleet_manager/server/routes/routing.py:533)) only adds *warning headers*. Requests that used to be silently truncated now hard-error.
- **Retry risk unknown.** `_is_retryable_error` retries any `>= 500` ([streaming.py:413](../../src/fleet_manager/server/streaming.py:413)). If Ollama returns this as **500**, we burn 3 retries on a request that can never succeed; if **4xx**, our 0.9.0 `client_error_passthrough` already surfaces it cleanly.

**Reuse — no new guard, no new config.** The audit collapsed this phase. `check_context_overflow` already estimates tokens vs the loaded context and is already called on all three routes; `client_error_passthrough` already surfaces any backend 4xx cleanly. So the "optional config-gated prompt-size guard" from the first draft is **cut** — it would be a feature gate (ruled out) *and* a duplicate of Ollama's own check. What's actually needed:

1. **Confirm the status code first** (the branch point). Cheapest safe probe: load a throwaway model at `num_ctx=512` and POST `/api/chat` a ~2K-token single message; read the HTTP status. Do this in the post-restart quiet window against a *dedicated* small model — never fire a 131K-token prompt at the live gpt-oss.
2. **If 4xx → nothing to build.** `client_error_passthrough` already returns it verbatim with Ollama's message. Add only a `test_streaming.py` case that pins this (guards against a future regression).
3. **If 5xx → one classifier.** Mirror the existing `_is_queue_full_error` exactly (status + body-substring match) as `_is_context_overflow_error`, and return `False` from `_is_retryable_error` for it — so we surface immediately instead of burning 3 retries on a request that can never succeed. Same shape, same place, same test style as the queue-full classifier already there.
4. **Correct the stale message** in `check_context_overflow`: its "input may be truncated by Ollama" is wrong as of 0.30.9 (Ollama now rejects, not truncates). One-line docstring/log fix — the helper otherwise stays exactly as-is.

**Verification.** `test_streaming.py` asserting the over-context error is non-retryable (zero retry attempts logged), mirroring the existing queue-full test.

**Risk.** Low, and lower than the first draft — no new route logic, no config surface. Steps 3–4 are a classifier + a string, both cloning patterns already in the file.

</details>

---

## Phase 3 — Token-accounting note (0.30.2) — ✅ IMPLEMENTED *(docs + a code comment)*

**Not a bug — an accuracy improvement to record.** `prompt_eval_count` ([streaming.py:650](../../src/fleet_manager/server/streaming.py:650)) now includes cached prompt tokens. Previously, on a prefix-cache hit Ollama reported only newly-evaluated tokens, **undercounting** the true prompt — so `context_optimizer`'s `total_p99` num_ctx sizing could size too small. Now it sees the full prompt: our dynamic num_ctx and the KV-sizing work from earlier today both run on better data.

**Approach.**
1. `docs/observations.md` entry: the semantics changed at 0.30.2; historical (pre-0.30.2) prompt-token traces undercount on cache hits, so the 7-day p99 window briefly blends old + new until it rolls over. Self-correcting.
2. A one-line comment at [streaming.py:650](../../src/fleet_manager/server/streaming.py:650) noting `prompt_eval_count` is logical prompt size (incl. cached) as of Ollama 0.30.2 — so nobody later mistakes it for "tokens actually computed" (the exact error that produced the false "zero prefix-cache reuse" report; see the MLX-runner issue Q3).

**Verification.** Docs only. Confirm `benchmark_estimate.py` still uses `completion_tokens / latency` (it does — unaffected).

**Risk.** None.

---

## Phase 4 — Native MLX runner evaluation *(needs the Ollama restart — the big one)*

**This is the highest-value item, and it's already scoped** as Phase 5 of [`ollama-0.32-upgrade-and-mlx-evaluation.md`](ollama-0.32-upgrade-and-mlx-evaluation.md) and the open question in [`../issues/ollama-native-mlx-runner.md`](../issues/ollama-native-mlx-runner.md). This plan adds the *new evidence* for why it's worth doing now and the decision it feeds.

**Why now.** Since we last looked, Ollama's native MLX engine gained: spec-decoding unification/tuning (0.30.11), **Gemma-4 multi-token prediction ~90% faster, auto-tuned, zero-config** (0.31.1), a **recurrent-model cache-leak fix** (0.32.1), and MLX MTP env knobs (`OLLAMA_MLX_MTP_*`). Our standalone `mlx_lm.server` stack — supervisor, proxy, `mlx:` prefix, kv-bits patch, setup script, and their ongoing bug tax — gets **none** of it, and exists only because Ollama couldn't run MLX. That premise is expiring.

**Approach.**
1. **In the post-restart quiet window** (before traffic resumes — a model load can't win a slot while `OLLAMA_NUM_PARALLEL=2` is saturated, the exact trap that blocked the last attempt): set `OLLAMA_NEW_ENGINE=1`, restart Ollama, and confirm from the server log that MLX is actually active (`mlx` mentions / `mlxrunner`, not `ggml_metal_init`).
2. **Measure** the same models we run via `mlx_lm.server` today (glm-4.7-flash, Qwen3-Coder-30B-A3B) under Ollama-native MLX: tok/s, prefill, and memory across requests (the 0.32.1 cache-leak fix should show here). Compare against both our current `mlx_lm.server` numbers **and** the llama.cpp path (glm 77.8 tok/s — the number to beat).
   - **Reuse the existing harness, not ad-hoc curl.** `BenchmarkRunner` + `benchmark_engine.send_request` already produce `tokens_per_sec`/`ttft` in the exact shape our prior numbers were taken in — using them makes the comparison apples-to-apples and keeps the results in the same `/dashboard/api/benchmarks` store instead of a scratch file.
3. **Decide the subsystem's fate** (Phase 5 of the upgrade plan):
   - **If native MLX ≥ our stack:** plan the retirement of `mlx_lm.server` — a large simplification squarely in line with the project's "choose simple" principle. Non-trivial (it touches the supervisor, proxy, `mlx:` routing, config surface, docs) → its own follow-up plan, not a same-day change.
   - **If it loses or is unstable:** record the numbers, keep `mlx_lm.server`, and re-check next Ollama minor.

**Verification.** Numbers recorded in the upgrade plan's results table; a clear ≥/< verdict vs 77.8 tok/s and vs `mlx_lm.server`; memory-across-requests trend captured.

**Risk.** Medium *operationally* (it's an Ollama restart + engine switch on the production box) — hence the quiet-window requirement and the standing rule to have a rollback (`unset OLLAMA_NEW_ENGINE`, restart). **Zero code risk** — it's a measurement, not a change.

---

## Sequencing & effort

| Phase | Restart? | Effort | Ship when |
|---|---|---|---|
| 1 — Gemma 4 + catalog hygiene | No | ~S | ✅ Done |
| 2 — Over-context handling | No | — | ✅ Probed: non-issue |
| 3 — Token-accounting note | No | ~XS | ✅ Done |
| 4 — Native MLX evaluation | **Yes** | ~M (measure) + separate plan if it wins | ⏳ Needs the restart |

**Recommended:** land Phases 1 + 3 immediately (safe, no restart). Do Phase 2's probe and Phase 4's measurement together in the post-restart quiet window, since both want that window and the restart is already pending. Each phase is independently committable.

## Out of scope (verified non-issues from the audit)
- `ollama ps` mmap double-count fix (0.30.11) — *helps* our KV-sizing; no action.
- `/api/generate` chat-template alignment (0.30.11) — `pre_warm` uses empty-prompt generate; verified working on 0.32.1.
- `/v1/models` alignment (0.30.7) — we build our own aggregated list.
- `nomic-embed-text` lowercasing (0.30.0) — we bypass Ollama via fastembed; moot.
