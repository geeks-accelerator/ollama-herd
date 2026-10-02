# Post-0.35 Enhancements — MLX-default readiness, mlx-lm 0.32, and three router capabilities

**Status**: **IMPLEMENTED and verified live 2026-10-02.** All 8 phases are done (code, plus the Ollama 0.35.0 upgrade). **Open:** the 24-hour soak (Phase 2); the Mac Studio soak (Phase 3); a two-machine cache measurement (Phase 7). **Phase 1's limit is computed but not enforced.** Herd's queue workers never held a slot for a request's duration, a pre-existing bug since the initial commit, now re-opened in `docs/issues.md`. See [Implementation notes](#implementation-notes-2026-10-02).
**Date**: 2026-10-02
**Prereq context**: [`post-0.32-enhancements.md`](post-0.32-enhancements.md) (the last Ollama audit) and [`../issues/ollama-native-mlx-runner.md`](../issues/ollama-native-mlx-runner.md) (the MLX-subsystem question this plan partly answers).

## Why this plan exists

A research pass on 2026-10-02 compared the reference Mac mini's stack (Ollama `0.33.3`, `mlx-lm` pin `0.31.3`) against what has shipped since. Most of what changed doesn't touch a router. Five items do, and three new capabilities are worth building. **Every finding was checked against a primary source:** release notes from the GitHub API, upstream source on `main`, or a live probe of this Mac. None of it is taken from blog posts.

| # | Finding | Evidence | Our exposure |
|---|---|---|---|
| 1 | **Ollama's MLX runner decodes one request at a time**, and `0.40.0-rc0` (2026-09-25) runs supported architectures on MLX **by default** on Apple Silicon | `mlxrunner/runner.go` `Run()`: a single goroutine takes one request off `r.Requests` and runs it to completion. Release note: *"model architectures supported by the MLX runtime will automatically run on MLX"* | Herd dispatches up to `OLLAMA_NUM_PARALLEL` concurrent requests per model. Everything past the first waits inside Ollama, where herd can't see it |
| 2 | **Ollama forces `numParallel = 1`** for embedding models and 11 architectures | `server/sched.go:502–515`: non-completion models, plus `mllama qwen3vl qwen3vlmoe qwen35 qwen35moe qwen3next lfm2 lfm2moe nemotron_h nemotron_h_moe nemotron_h_omni`. Logs a warning herd never sees | Same over-dispatch. Includes `qwen3vl`, which CLAUDE.md recommends for vision |
| 3 | **`mlx-lm` 0.32.0 (2026-10-01) adds KV-cache quantization to the server natively** | [PR #1832](https://github.com/ml-explore/mlx-lm/pull/1832), merged 2026-09-09. `server.py@v0.32.0` defines `--kv-bits`, `--kv-group-size`, `--quantized-kv-start` | The same three flags our patch adds. The patch, the exact version pin, and "re-run setup-mlx.sh after every upgrade" can all go |
| 4 | **Ollama reports capabilities per model, including `thinking`** | `types/model/capability.go`: `completion tools insert vision embedding thinking image audio decision`. `/api/tags` returns `capabilities`. `0.34.3`: `/api/show` adds `thinking.values` / `thinking.default` | `is_thinking_model()` is a hand-kept name list. It reports `qwen3.8` (thinking and vision per ollama.com) as neither (checked live) |
| 5 | **The reference Mac is two minor versions behind** (`0.33.3`; stable is `0.35.0`) | GitHub releases API | It misses faster structured outputs on Apple Silicon (`0.34.0`), consistent capability reporting and better MLX memory handling (`0.34.1`), single-pass structured outputs on thinking models (`0.34.4`), and `/v1/systemone` (`0.35.0`). It's also the only machine available to verify Phases 1, 4, and 6 |
| T3a | **Ollama has no rerank endpoint** | [ollama#16076](https://github.com/ollama/ollama/issues/16076), open | `fastembed`, which we already depend on, supports 6 cross-encoder rerankers (checked against the installed package) |
| T3b | **`0.35.0` adds `/v1/systemone`** ("decision models": choices plus probabilities) | Handler at `server/routes.go:2076`: non-streaming JSON, a 64 KiB body cap (32 MiB with images), **local GGUF only** | Herd returns **404** for it today (probed live) |
| T3c | **Prefix-aware routing is the 2026 standard for multi-replica LLM routers** | llm-d, SGLang, vLLM router, GKE Inference Gateway | `session_key_for()` keys on a `session:` tag or `client_ip\|model`. Two clients sending the same ~20K-token system prompt get no cache locality |

**Checked and deliberately excluded:**
- **MTP speculative decoding.** Ollama detects MTP heads in a GGUF (`llm/llama_server.go`, `hasMTPDraft` → `EnableMTP`) and `draft_num_predict` defaults to `4` (`api/types.go`), so it's already on wherever a model supports it. Herd needs no code for it; the Phase 2 upgrade covers it.
- **Adopting `0.40.0-rc0`.** Phase 1 makes herd correct whether or not a node takes it.

## Implementation principles

This is a greenfield project. **The goal is the least new surface and the most reuse.**

- **No feature gating.** There are no new `FLEET_*` settings of any kind. Correct behavior *is* the behavior. New endpoints aren't toggles, and tuning constants follow the existing pattern of class constants (Phase 7), not settings.
- **Reuse before build.** Each phase maps onto machinery that already exists (audit table below). Where two features would otherwise duplicate an inline block, **extract it once and have both call it**, rather than copying it.
- **Mirror Ollama's own decision, never guess at it.** Ollama's `IsMLX()` is exactly `Config.ModelFormat == "safetensors"` (`server/images.go:93`). The scheduler's family check reads `Config.ModelFamily`, and `/api/tags` reports both strings as `details.format` and `details.family` (`server/routes.go`). Our heartbeat already carries them in `ModelTagMeta`.
- **Trust what's present, never what's absent.** Act on a capability Ollama *reports*; never infer anything from one it *omits*. Ollama `0.33.x` under-reports in `/api/tags`. Checked live on this Mac: `gemma3:27b` shows `['completion']` in `/api/tags` but `['completion', 'vision']` in `/api/show`. `0.34.1` fixed this ("capabilities are now reported consistently"). The presence-only rule is correct on both versions, so herd needs no version parsing (and has no version-comparison helper to reuse).
- **Missing data reproduces today's behavior exactly.** Older node agents, `mlx:` models, and models without metadata keep the current heuristics and limits. Nothing changes for them.
- **Write tests for current behavior before changing untested code.** Phase 3 and 4 touch functions with **zero** tests today.

## Codebase audit (2026-10-02)

Each phase was read against the code it touches. The audit changed the plan in three ways.

### Reuse map

| Need | Existing code that already does it |
|---|---|
| Parse `/api/tags` details with null coercion | `OllamaClient.get_available_model_meta()` — [ollama_client.py:142](../../src/fleet_manager/common/ollama_client.py:142), where `families` is coerced to a list |
| Carry per-model meta between heartbeats | [registry.py:80](../../src/fleet_manager/server/registry.py:80) carries the previous value forward when a heartbeat omits it |
| Normalize a model name to Ollama's tag form | `normalize_model_name()` — [request.py:29](../../src/fleet_manager/models/request.py:29). `InferenceRequest` already applies it on construction |
| Home for Ollama-mirroring constants with dated provenance | [serializers.py:22–30](../../src/fleet_manager/server/serializers.py:22) (`OLLAMA_HOT_MODEL_CAP`, `OLLAMA_DEFAULT_NUM_PARALLEL`) |
| Per-node decode limit | `decode_parallelism_for()` — [serializers.py:33](../../src/fleet_manager/server/serializers.py:33), sole caller [queue_manager.py:259](../../src/fleet_manager/server/queue_manager.py:259) |
| Show true concurrency in the UI | Dashboard already renders `in_flight/concurrency` — [dashboard.py:2583](../../src/fleet_manager/server/routes/dashboard.py:2583) |
| Wait estimate that is correct for serial backends | `_score_wait_time` = `depth × p75` — [scorer.py:350](../../src/fleet_manager/server/scorer.py:350). Never divides by concurrency |
| Fail-fast MLX flag probe | `_binary_supports_kv_bits()` — [mlx_supervisor.py:440](../../src/fleet_manager/node/mlx_supervisor.py:440) |
| Native fastembed model registry | `TEXT_EMBEDDING_MODELS` + `get_fastembed_name` / `is_model_cached` / `canonical_model_names` — [text_embedding_models.py:30](../../src/fleet_manager/node/text_embedding_models.py:30) |
| Lazy, lock-guarded fastembed loader | `_get_model()` — [text_embedding_server.py:55](../../src/fleet_manager/node/text_embedding_server.py:55) |
| Native-server node scoring | `_score_text_embedding_candidates()` — [text_embedding_compat.py:39](../../src/fleet_manager/server/routes/text_embedding_compat.py:39) |
| Backend-missing health check | `text_embedding_backend_missing` — [health_engine.py:125](../../src/fleet_manager/server/health_engine.py:125) |
| Find a node holding a model, loaded first, with failover | Inline in `ollama_show` — [ollama_compat.py:394](../../src/fleet_manager/server/routes/ollama_compat.py:394) |
| Trace a request rejected before routing | `record_routing_rejection()` — [routing.py:320](../../src/fleet_manager/server/routes/routing.py:320) |
| TTL'd, bounded key → node map | `SessionAffinityTracker` — [session_affinity.py:55](../../src/fleet_manager/server/session_affinity.py:55) |
| **Hot-spot guard for affinity bonuses** | Signal 8 already decays its bonus by queue depth (`SESSION_AFFINITY_DECAY_SCALE`) — [scorer.py:393](../../src/fleet_manager/server/scorer.py:393) |
| Strip Claude Code's per-request `cch=` fingerprint | `_normalize_cache_busting_tokens()` — [anthropic_translator.py:99](../../src/fleet_manager/server/anthropic_translator.py:99), applied by `anthropic_system_to_text()` |
| Token estimate for routing thresholds | `ScoringEngine.estimate_tokens()` — [scorer.py:548](../../src/fleet_manager/server/scorer.py:548) |

### Removed from the first draft (each was new surface with no payoff)

| Removed | Why |
|---|---|
| `scripts/check-ollama-drift.sh` | No precedent. The existing Ollama-mirroring constants use dated provenance comments and are re-checked on upgrade; the family list joins them |
| A per-queue "concurrency reason" field | The dashboard already shows `1/1`. The number is the truth |
| Scorer changes for serial backends | `_score_wait_time` is `depth × p75` and never assumed parallelism |
| Optional `/api/show` `thinking.default` lookup + digest cache | `num_predict` is a **ceiling**. Inflating it for a model that doesn't think costs nothing, because generation still stops at end-of-sequence. The cache would be a new mechanism with no benefit |
| Ollama version parsing for `/v1/systemone` | Only Ollama `≥0.35` can report a `decision` capability, so the capability filter already excludes older nodes |
| A separate reranker name map | Rerankers go in a sibling dict with the same shape, served by the existing module helpers |
| `/api/rerank` alias | Ollama has no such endpoint. Inventing API surface is debt. `/v1/rerank` is what Cohere, Jina, and llama-server clients call |
| Prefix weight as a setting | Signal 8's weight is a class constant (`SESSION_AFFINITY_BONUS`, [scorer.py:384](../../src/fleet_manager/server/scorer.py:384)). A setting would break the pattern and add a `FLEET_*` field |
| A new hot-spotting guard | Signal 8's queue-depth decay already *is* that guard |
| Per-format prompt canonicalization | Translators already emit system as `role: system` in `InferenceRequest.messages`, with the `cch=` fingerprint already normalized |

### Extractions (de-duplications done before building on them)

- **Phase 5:** `embed_text` is a single ~175-line handler with four inline `record_trace` calls ([text_embedding_compat.py:56](../../src/fleet_manager/server/routes/text_embedding_compat.py:56)). Extract the "pick native node → POST → time → trace → map errors" core first. The existing `test_text_embedding.py` proves the extraction changes nothing, and `/v1/rerank` is then a thin caller instead of a ~150-line copy.
- **Phase 6:** `/api/show`'s ~50-line node selection (name candidates → loaded first → on-disk → failover) is inline. Extract `_nodes_with_model()`. The existing `/api/show` tests in `test_client_compat.py` prove no change, and `/v1/systemone` reuses it.

### Test gaps found

- `is_thinking_model()` ([model_knowledge.py:893](../../src/fleet_manager/server/model_knowledge.py:893)) and `_apply_thinking_overhead()` ([streaming.py:1469](../../src/fleet_manager/server/streaming.py:1469)): **no tests**.
- `_binary_supports_kv_bits()` ([mlx_supervisor.py:440](../../src/fleet_manager/node/mlx_supervisor.py:440)): **no tests**.

Both get tests for current behavior *before* the phase that changes them.

## Mapping from the research

| Research item | Phase |
|---|---|
| Shared foundation for #1, #2, #4, T3b | **Phase 0** — carry `capabilities` in heartbeat metadata |
| #1 MLX serial + #2 forced families | **Phase 1** — per-model decode parallelism |
| #5 Ollama upgrade | **Phase 2** — upgrade the reference Mac, soak |
| #3 mlx-lm 0.32 | **Phase 3** — retire the `--kv-bits` patch |
| #4 thinking detection | **Phase 4** — capabilities from Ollama replace name heuristics |
| T3a rerank | **Phase 5** — `/v1/rerank` |
| T3b decision models | **Phase 6** — `/v1/systemone` passthrough |
| T3c prefix routing | **Phase 7** — prefix-aware session affinity |

---

## Phase 0 — Carry Ollama's per-model `capabilities` in the heartbeat *(foundation, S)*

**Problem.** Phases 1, 4, and 6 all need to know what each model *is*: embedding-only, thinking, vision, or decision. Ollama already reports this in `/api/tags` as `capabilities`, which herd fetches. We drop the field.

**Approach.**
1. Add `capabilities: list[str] = Field(default_factory=list)` to [`ModelTagMeta`](../../src/fleet_manager/models/node.py:86). An empty list meets its "non-null, list for multi-valued" contract.
2. In `get_available_model_meta()` ([ollama_client.py:142](../../src/fleet_manager/common/ollama_client.py:142)), populate it **exactly as `families` is populated**: `[str(c) for c in (m.get("capabilities") or []) if c is not None]`.
3. Add one helper in `serializers.py`, beside `decode_parallelism_for`, so every Ollama-truth lookup lives together:
   ```python
   def model_has_capability(node, model: str, capability: str) -> bool:
       """True only when the node's Ollama *reports* the capability.

       Presence-only: Ollama 0.33.x under-reports capabilities in /api/tags
       (fixed in 0.34.1), so an absent entry proves nothing.  Callers treat
       False as "unknown" and fall back to today's behavior.
       """
   ```
   The lookup is `node.ollama.models_available_meta.get(normalize_model_name(model))` and reuses the existing normalizer. No new name handling.

**Reuse.** The heartbeat carry-forward ([registry.py:80](../../src/fleet_manager/server/registry.py:80)) and the change-or-once-a-minute send already exist, and a newly pulled model changes the meta, so it gets sent automatically.

**Wire-contract note.** `capabilities` is herd-internal (node → router). It is **not** the community telemetry payload, so the `extra="forbid"` hazard in CLAUDE.md doesn't apply. Don't add it to `anonymous_rollup.py`'s `ALLOWED_*` sets.

**Verification.**
- Unit (extend `test_client_compat.py`, which already covers `get_available_model_meta`): `{"capabilities": null}` → `[]`, and `None` entries are dropped. `model_has_capability` returns True on presence and False on absence or missing meta.
- Live: after a node restart, the router's meta for `nomic-embed-text:latest` contains `embedding`, matching `curl localhost:11434/api/tags`.

**Risk.** Very low. The field is additive. Older routers ignore unknown heartbeat keys.

---

## Phase 1 — Per-model decode parallelism *(#1 + #2, M, **time-sensitive**)*

**Problem.** [`decode_parallelism_for(node)`](../../src/fleet_manager/server/serializers.py:33) returns one number per **node** (its reported `OLLAMA_NUM_PARALLEL`). Its only caller ([queue_manager.py:259](../../src/fleet_manager/server/queue_manager.py:259)) applies it to every model. But Ollama decides per **model**: MLX-run models decode serially, and 11 families plus non-completion models are forced to 1. With `NUM_PARALLEL=4`, herd runs 4 workers against `qwen3-vl:32b` or any `-mlx` model, and 3 of them sit inside Ollama, invisible to herd. This is CLAUDE.md's co-tenancy failure mode: the scheduling math is silently wrong while every herd-side metric looks healthy. **It's time-sensitive** because `0.40` makes MLX the default, and the Mac app self-updates on relaunch and can pull prereleases.

**Approach.**
1. In `serializers.py`, beside `OLLAMA_HOT_MODEL_CAP` and `OLLAMA_DEFAULT_NUM_PARALLEL` and in their dated-provenance style:
   ```python
   # Ollama server/sched.go load(): "Some architectures are not safe with
   # num_parallel > 1."  Read from main 2026-10-02.  Re-check on every Ollama
   # upgrade, like the two constants above.
   OLLAMA_SERIAL_FAMILIES = frozenset({
       "mllama", "qwen3vl", "qwen3vlmoe", "qwen35", "qwen35moe", "qwen3next",
       "lfm2", "lfm2moe", "nemotron_h", "nemotron_h_moe", "nemotron_h_omni",
   })
   ```
2. Change the signature to `decode_parallelism_for(node, model: str | None = None) -> int`. Return **1** when the model's meta shows any of:
   - `family in OLLAMA_SERIAL_FAMILIES`
   - `format == "safetensors"` (mirrors `IsMLX()`; the MLX runner's serial loop)
   - `model_has_capability(node, model, "embedding")` or `model_has_capability(node, model, "decision")`. This is the presence-only form of `sched.go`'s `!completion` rule; it never infers from a missing `completion`.

   Otherwise return today's node-level value. **Absent or empty meta → today's behavior, unchanged.**
3. Pass `model` from the single call site. Workers re-read concurrency on every `_ensure_workers` pass ([queue_manager.py:267](../../src/fleet_manager/server/queue_manager.py:267)), so the change takes effect live.
4. **Docs/issue hygiene, no code:**
   - Mark [`docs/issues.md`](../issues.md) *"Queue concurrency ignores OLLAMA_NUM_PARALLEL"* `FIXED`. Its fix (node-reported `num_parallel`) shipped as `decode_parallelism_for`; link this phase as the per-model follow-up.
   - In CLAUDE.md, replace the stale `x/mlxrunner` reference (upstream moved it out of `x/` on 2026-09-16, `2e036e7c`) and "ignores `OLLAMA_NUM_PARALLEL`" with the actual mechanism, a serial request loop.
   - Add `OLLAMA_SERIAL_FAMILIES` to CLAUDE.md's existing "re-check after any Ollama restart" guidance. This adds no new checklist.

**Not needed (audit):** no scorer change (`_score_wait_time` is `depth × p75`, [scorer.py:350](../../src/fleet_manager/server/scorer.py:350)), no new queue field or dashboard work (it already renders `in_flight/concurrency`), and no drift script.

**Out of scope.** Herd's own `mlx:` models (`mlx_lm.server`) have their own continuous batching and keep their current path.

**Verification.**
- Unit, extending `test_queue_manager.py` (it already tests `decode_parallelism_for`). Parametrize:
  - each of the 11 families → 1
  - `format="safetensors"` → 1
  - `capabilities=["embedding"]` → 1
  - `["completion","vision"]` + `family="gemma3"` + `format="gguf"` → node value
  - meta absent → node value
  - `capabilities=[]` → node value, so an old agent is never throttled
  - two models on one node get different limits
- **Live on this Mac (after Phase 2).** Pull `qwen3.8:27b-mlx` (18 GB; unload `gemma3:27b` first) and fire 4 concurrent `/v1/chat/completions` through herd.
  - **Before**: 4 in-flight, with TTFT stepping up as requests wait behind each other (serialization).
  - **After**: the dashboard shows `1/1`, requests 2–4 wait in herd's queue where they're visible, and total wall time is about the same. The goal is visibility and correct waits, not speed.
  - Also record `qwen3.8`'s reported `family`, which isn't stated on ollama.com. If it's `qwen35`, it's serial under both rules.

**Risk.** Low–medium. The failure mode is under-dispatch: a wrong family string would throttle a model to 1. Mitigations: the constant mirrors upstream exactly, absent meta falls back, and the presence-only rule can't fire on missing data.

---

## Phase 2 — Upgrade the reference Mac to Ollama 0.35.x and soak *(#5, S + 24 h soak)*

**Problem.** `0.33.3` predates what Phases 1, 4, and 6 need to be verified live: consistent capabilities (`0.34.1`), production MLX `create` and memory handling (`0.34.1`), and `/v1/systemone` (`0.35.0`). It also misses free performance work.

**Approach.** Follow CLAUDE.md's existing procedure; nothing new is invented here:
1. **Baseline first.** Record `gemma3:27b` decode percentiles with the soak recipe in CLAUDE.md. Last measured 2026-10-02: median 13.5 / p25 13.4 / p10 13.2 tok/s.
2. `launchctl bootout` the **node** agent, so it can't grab `:11434` mid-swap.
3. Manual Mac-app swap to the latest **stable** `0.35.x`, not `0.40.0-rc0`. `brew upgrade ollama` does nothing (stale formula).
4. Quit the **Electron parent**, not just `ollama serve`, and reap orphaned `llama-server` processes (`ppid=1`).
5. `launchctl bootstrap` the node back, then **verify `curl -s localhost:11434/api/version`**.

**Verification.**
- `api/version` reports `0.35.x`.
- `/api/tags` capabilities now match `/api/show`. `gemma3:27b` should list `vision` in both. This is the before/after evidence for the presence-only rule.
- `POST /v1/systemone` on `:11434` returns 4xx (no model yet), not 404.
- Decode percentiles are within noise of baseline. `gemma3` has no MTP heads, so a *drop* is the only signal that matters.
- 24 h soak per CLAUDE.md's day-after checklist. Use `grep '"level": "ERROR"'` with the space after the colon, and compare p25 vs median.
- Note in `docs/observations.md` whether the auto-updater offers prereleases. That's the path by which `0.40`'s MLX default would arrive unannounced.

**Risk.** Low, with a known recovery: re-swap the previous app bundle.

---

## Phase 3 — Retire the `mlx-lm` `--kv-bits` patch *(#3, S, net code deletion)*

**Problem.** Herd pins `mlx-lm==0.31.3` ([setup-mlx.sh:24](../../scripts/setup-mlx.sh:24)) and applies [`mlx-lm-server-kv-bits.patch`](../experiments/mlx-lm-server-kv-bits.patch) to add three flags that upstream `0.32.0` now ships under **identical names** (PR #1832).

**Approach.**
1. **Tests for current behavior first:** add tests for `_binary_supports_kv_bits()`. Help text containing `--kv-bits` → True. Help text without it → False. Probe failure (`OSError`/timeout) → True, the existing fail-open behavior.
2. In `setup-mlx.sh`: bump `PINNED_VERSION` to `0.32.0` and **delete the embedded patch-applier** (~110 of the script's 184 lines). Keep an exact pin: `0.32.0` spans 133 upstream commits, and this subsystem's history argues for a tested version over a floor.
3. In `mlx_supervisor.py`, the probe and the flags passed at line 506 stay as they are, since both match upstream. Only text changes:
   - the probe docstring ("Stock upstream mlx-lm omits this flag")
   - the preflight comment
   - the `logger.error` and `_status_reason` messages, from "patch is missing" to "`mlx-lm` < 0.32.0 — run `scripts/setup-mlx.sh`"
4. Update the two health-check remediation strings that say the patch was wiped ([health_engine.py:1259, 1294](../../src/fleet_manager/server/health_engine.py:1259)).
5. Docs:
   - Delete CLAUDE.md's "re-run the script after any `uv tool upgrade mlx-lm`" gotcha.
   - Mark the patch file superseded with a header note. Don't delete it; history stays readable.
   - Update `docs/guides/mlx-setup.md` and any other doc that gives **instructions**. Leave historical records (observations, past plans) alone.

**Reuse.** The supervisor's probe-then-fail-fast design is unchanged. Only its wording assumed a patch.

**Also in 0.32.0, relevant to open MLX issues** (watch for these in the soak, but don't claim them as fixes): the `mlx_lm.server` model-swap leak and dead generation thread (#1837), the unbounded `ArraysCache` graph during decode (#1632), and the `quantized_kv_start` default fix (#1819).

**Verification.**
- **On this Mac**, which runs no MLX servers today: `uv tool install mlx-lm==0.32.0`. Confirm `mlx_lm.server --help` lists all three flags and `_binary_supports_kv_bits()` returns True against it. Then serve `mlx-community/Qwen3-0.6B-4bit` with `--kv-bits 8 --kv-group-size 64` and complete one chat.
- **On the reference MLX fleet (Mac Studio)**, which this Mac can't stand in for: upgrade in place and soak 24 h. Compare `mlx:` decode percentiles and crash/quarantine counts against the prior week.

**Risk.** Low for the deletion. Medium for "133 upstream commits", which is why the Mac Studio soak gates it.

---

## Phase 4 — Capabilities from Ollama replace name heuristics *(#4, S–M)*

**Problem.** Three detectors guess from model names, and all three already miss current models:

| Heuristic | Used by | Misses (checked live) |
|---|---|---|
| `is_thinking_model()` — [model_knowledge.py:893](../../src/fleet_manager/server/model_knowledge.py:893) | 4× `num_predict` inflation, [streaming.py:1484](../../src/fleet_manager/server/streaming.py:1484) | `qwen3.8` → False (Ollama: thinks by default) |
| `is_vision_model()` — [model_knowledge.py:928](../../src/fleet_manager/server/model_knowledge.py:928) | Claude-model autoroute vision filter ([anthropic_autoroute.py:145](../../src/fleet_manager/server/anthropic_autoroute.py:145)); `/v1/models` `supports_vision` ([openai_compat.py:117](../../src/fleet_manager/server/routes/openai_compat.py:117)) | `qwen3.8` → False (Ollama: text + image) |
| `_is_embedding_name()` — [anthropic_autoroute.py:88](../../src/fleet_manager/server/anthropic_autoroute.py:88) | Excludes embedders from Claude autoroute ([:135](../../src/fleet_manager/server/anthropic_autoroute.py:135)) | Any embedder not matching `embed/bge-/gte-/e5-/minilm/arctic` |

A small `num_predict` on an undetected thinking model can return an empty response, the exact failure the inflation prevents. (`muse-glimmer` was hand-added after hitting this on 2026-08-14.)

**Approach.** Reuse Phase 0's one helper at every site that has node context. **OR it with the existing heuristic**, so detection only widens and nothing regresses:
1. **Tests for current behavior first:** add tests for `is_thinking_model()` and `_apply_thinking_overhead()` ([streaming.py:1469](../../src/fleet_manager/server/streaming.py:1469)): inflate when set, the minimum floor, no-op when `num_predict` is unset. Neither has any tests today.
2. **Thinking:** `_apply_thinking_overhead` already knows the target node. Inflate when `model_has_capability(node, model, "thinking") or is_thinking_model(model)`.
3. **Respect the request:** if the Ollama-format body this function already receives has `think: false`, don't inflate. That's one line, no translation-layer change. (Mapping OpenAI `reasoning_effort` or Anthropic `thinking` onto Ollama `think` is a separate feature and out of scope.)
4. **Vision and embedding:** at the autoroute and `/v1/models` sites, OR the capability (`"vision"` / `"embedding"`) with the existing heuristic, checked against the node(s) that hold the model.
5. Update the CLAUDE.md gotcha "add new ones to `is_thinking_model()`". Ollama-served models are now detected automatically, and the list only covers `mlx:` models and older agents.

**Not needed (audit):** no `/api/show` lookups and no digest cache. `num_predict` is a ceiling, so over-inflating for a model that doesn't think is free.

**Verification.**
- Unit:
  - capability present → inflate
  - absent but name matches → inflate (fallback)
  - `think:false` → don't inflate
  - no meta → identical to today
  - autoroute offers a capability-reported vision model for an image request
- **Live (after Phase 2):** request a model that reports `thinking` with `num_predict: 50`. Before the change the trace shows no inflation and the response can be empty. After, it's inflated and non-empty.

**Risk.** Low. Detection only widens, and the inflation itself is unchanged and already in production.

---

## Phase 5 — `/v1/rerank` via the native text-embedding server *(T3a, M)*

**Problem.** RAG pipelines (Open WebUI, Dify, LangChain, LlamaIndex) use embed → retrieve → **rerank**. Ollama has no rerank endpoint ([#16076](https://github.com/ollama/ollama/issues/16076)). Herd already runs a native fastembed server, and fastembed ships `TextCrossEncoder` with 6 rerankers (checked against the installed `fastembed 0.8.0`):

| fastembed model | Size |
|---|---|
| `Xenova/ms-marco-MiniLM-L-6-v2` | 0.08 GB |
| `Xenova/ms-marco-MiniLM-L-12-v2` | 0.12 GB |
| `jinaai/jina-reranker-v1-tiny-en` | 0.13 GB |
| `jinaai/jina-reranker-v1-turbo-en` | 0.15 GB |
| `BAAI/bge-reranker-base` | 1.04 GB |
| `jinaai/jina-reranker-v2-base-multilingual` | 1.11 GB |

That makes this a capability Ollama lacks, with **zero new dependencies**.

**Approach.**
1. **Extract first** (no behavior change): move the native-proxy core of `embed_text` ([text_embedding_compat.py:56](../../src/fleet_manager/server/routes/text_embedding_compat.py:56)) into one helper: candidate filter → `_score_text_embedding_candidates` → POST to node path → timing → `record_trace` → error mapping. `embed_text` becomes a caller, and the existing `test_text_embedding.py` must pass unchanged.
2. **Registry:** add a sibling `RERANK_MODELS` dict in `text_embedding_models.py` with the same spec shape as `TEXT_EMBEDDING_MODELS`. Make the existing lookups (`get_fastembed_name`, `get_model_spec`, `is_model_cached`, `canonical_model_names`) resolve across both dicts. **Leave `is_text_embedding_model()` and `TEXT_EMBEDDING_MODEL_NAMES` embed-only**, so the `/api/embed` dispatcher can't route an embed request to a reranker. Add `is_rerank_model()` beside it.
3. **Loader:** generalize `_get_model()` ([text_embedding_server.py:55](../../src/fleet_manager/node/text_embedding_server.py:55)) to keep one slot per fastembed class (`TextEmbedding`, `TextCrossEncoder`) with the same lock-and-swap logic, so loading a reranker never evicts nomic. Don't copy the loader.
4. **Node route:** `POST /rerank` beside `/embed` on port 11439. The default model is `ms-marco-MiniLM-L-6-v2` (80 MB, downloads on first request like nomic). `bge-reranker-base` and `jina-reranker-v2-base-multilingual` are the documented quality picks.
5. **Heartbeat:** add `kind: str = "embed"` to `TextEmbeddingModel` ([node.py:204](../../src/fleet_manager/models/node.py:204)). The default keeps old agents valid, and rerankers report `dimensions=0`. There's no new port or metrics block.
6. **Router:** `POST /v1/rerank` in Jina/Cohere format. Request: `{model, query, documents: [str], top_n?, return_documents?}`. Response: `{model, results: [{index, relevance_score, document?}], usage}`. It's a thin caller of the helper from step 1. No `/api/rerank` alias.
7. **Health:** extend the existing `text_embedding_backend_missing` check to cover rerank, with the same `uv sync --extra embedding` remedy.

**Limits.** Cap `documents` (e.g. 1,000) and per-document length, returning a clean 400 rather than OOMing the node. Cross-encoders cost one forward pass per document.

**Verification.**
- Unit: `test_text_embedding.py` is unchanged and green after step 1. Rerank validation, `top_n`, `return_documents`, descending scores with original indices preserved, and empty `documents` → 400. An `/api/embed` request naming a reranker is **not** sent to the rerank backend.
- **Live on this Mac:** query "how do I stop herd now that launchd is installed" against 4 docs (launchctl `bootout`, the `pkill` warning, an unrelated MLX note, an embedding note). `bootout` should rank first. Record cold latency (includes download) and warm latency in the PR.

**Risk.** Low–medium. The endpoint is isolated, and the hazard is memory: 1 GB models load lazily and only on request.

---

## Phase 6 — `/v1/systemone` passthrough *(T3b, S)*

**Problem.** `0.35.0` added `/v1/systemone` for decision models (`nimble`, `tev1`). Ollama lists model routing and ticket triage as intended uses. Through herd it returns **404** (probed 2026-10-02).

**Approach.**
1. **Extract first** (no behavior change): pull `/api/show`'s inline node selection ([ollama_compat.py:394](../../src/fleet_manager/server/routes/ollama_compat.py:394)) into `_nodes_with_model(registry, model) -> list[(node, resolved_name)]`, ordered loaded-first. `/api/show` keeps its failover loop over that list, and the existing `test_client_compat.py` `/api/show` tests must pass unchanged.
2. Add `POST /v1/systemone` in `openai_compat.py`. Candidates are `_nodes_with_model()` filtered by `model_has_capability(node, model, "decision")` (Phase 0). **This filter also excludes nodes older than 0.35**, which can't report `decision`, so no version check is needed. Forward the body unchanged; it's non-streaming JSON.
3. Enforce upstream's caps at the router (64 KiB body; 32 MiB with images) so oversized requests fail fast without a network hop.
4. No eligible node → 404 with a clear message ("no node serves decision model 'X' — needs Ollama ≥ 0.35 and a local GGUF decision model"), traced via the existing `record_routing_rejection()`. Upstream 400s (`:cloud`/MLX models) pass through unchanged.
5. Trace successes with `request_type="decision"`.

**Explicitly out of scope:** using a decision model *inside* herd for semantic routing. That's a separate design.

**Verification.**
- Unit: `/api/show` tests unchanged and green after the extraction. Capability filtering. Size cap → 413 before any network call. No-node → 404 with a rejection trace.
- **Live (after Phase 2):** `ollama pull nimble`, then send the release-notes ticket-triage example through `:11435` and directly to `:11434`. The answers should match (`bug` with the highest probability).

**Risk.** Very low. It's a passthrough on an endpoint nothing uses today, and the extraction is covered by existing tests.

---

## Phase 7 — Prefix-aware session affinity *(T3c, M)*

**Problem.** Signal 8 sends a returning conversation to the node holding its warm prefix cache. But [`session_key_for()`](../../src/fleet_manager/server/session_affinity.py:96) only knows a `session:` tag or `client_ip|model`, never what the prompt *contains*. Five Claude Code sessions on five machines each send the same ~20K-token system prompt and tools, and each takes a cold prefill on whichever node wins, even if another node already holds that exact prefix.

**Two keys, because they answer different questions.** The session/IP key approximates *this conversation's* full history, so it's the stronger signal for turn N+1. The prefix key catches the *first turn* of a new session that shares a system prompt. Keep both, with the session key taking precedence.

**Approach, almost entirely reuse:**
1. **Key extraction, one path for all formats:** take the leading `role: system` messages from `InferenceRequest.messages` (translators already put them there, with Claude Code's `cch=` fingerprint already normalized by `_normalize_cache_busting_tokens`, [anthropic_translator.py:99](../../src/fleet_manager/server/anthropic_translator.py:99)), plus `raw_body["tools"]` canonicalized. `prefix_key = "prefix:" + sha256(model ‖ system ‖ tools)`. Hash only; prompt text is never stored.
2. **Threshold:** skip prefixes under about 2K tokens, using the existing `ScoringEngine.estimate_tokens()` ([scorer.py:548](../../src/fleet_manager/server/scorer.py:548)).
3. **Storage:** the existing `SessionAffinityTracker` holds `prefix:` keys alongside session keys. It's a generic TTL'd key → node map, and needs no new class.
4. **Plumbing:** `session_key_for()` returns both keys. `_remember_session_node()` ([routing.py:157](../../src/fleet_manager/server/routes/routing.py:157), 4 call sites) remembers both.
5. **Scoring:** signal 8 checks the session pin first, then the prefix pin. Add `PREFIX_AFFINITY_BONUS` as a **class constant** beside `SESSION_AFFINITY_BONUS` ([scorer.py:384](../../src/fleet_manager/server/scorer.py:384)), below it in value, and route it through **the same queue-depth decay**. That decay already prevents hot-spotting, so there's no new guard. Record `prefix_affinity` in `scores_breakdown`.
6. **Single-node fleets are unaffected:** with one candidate the signal changes nothing.

**Known risk to verify, not to pre-build for:** Ollama `0.33.0` had to disable a Claude Code "tokens left" countdown message that busted the KV cache. If something similar lands in the system prompt, prefix keys would churn. If a real capture shows that, **extend the single existing normalizer** (`_normalize_cache_busting_tokens`) rather than adding a second one.

**Verification.**
- Unit:
  - same system and tools from different IPs → same key
  - whitespace and tool order normalized
  - different tools → different key
  - under 2K tokens → no key
  - two `cch=` variants of one prompt → same key, proving the existing normalizer is in the path
- Unit (scorer): with the prefix held on A, A wins at equal load, and once A's queue is deep, the **existing** decay lets B win. Encode the hot-spot guard as a test.
- **Routing decisions, on this Mac:** run a second `herd-node` with a different `FLEET_NODE_NODE_ID` against the same Ollama (two logical nodes). Two requests sharing a 3K-token system prompt from different client IPs route to the same node, with `prefix_affinity` in `scores_breakdown`. This proves the decision, but not a cache win, since both share one Ollama.
- **Cache benefit, on the reference fleet (Mac Studio + MacBook Pro):** TTFT percentiles for a second client's first turn, with and without the signal, per CLAUDE.md's measure-the-tail rule. Also capture a real Claude Code request to confirm key stability across turns (the countdown risk above).

**Risk.** Medium, since it changes routing decisions fleet-wide. Mitigated by the existing decay, a bonus below the session bonus, and the 2K threshold. That's why it's last.

---

## Implementation notes (2026-10-02)

Each phase was built against the code rather than this document, and four places changed when the code disagreed with the plan.

| Phase | Plan said | Built | Why |
|---|---|---|---|
| 3 | The flags passed at `mlx_supervisor.py:506` "stay as they are" | The supervisor now passes `--quantized-kv-start 0` | Upstream defaults it to **5000**, while the retired patch defaulted to 0. Without the explicit 0, every sequence's first 5K tokens would silently stay unquantized. A test pins it |
| 5 | `test_text_embedding.py` proves the `embed_text` extraction | Five new behavioral tests, written and passed against the unchanged code first | That file never exercised the proxy path. The only coverage was one success case and a test that grepped the source for two strings, which is now behavioral too |
| 5 | Put the route beside the embed routes in `text_embedding_compat.py` | `/v1/rerank` lives in `openai_compat.py` beside `/v1/embeddings` | `text_embedding_compat`'s router is never mounted, so mounting it would have made `/api/embed-text` public as a side effect |
| 5 | (not in plan) | Fixed `is_model_cached`, which was always False for nomic | fastembed's cache is keyed by HF *source repo*, and nomic's `-Q` file lives in `nomic-embed-text-v1.5`. Heartbeats said `cached=False`, `cached_model_count` was 0, and `text_embedding_backend_missing` could never fire. Rerank's cached-model advertising depended on it |
| 7 | Normalize whitespace and tool order in the prefix key | Only dict key order is normalized | The backend's prefix cache is exact-match. Prompts differing in whitespace or tool order share no cache, so grouping them would route for nothing |

**Smaller decisions:**
- `/v1/rerank` scores pass through a sigmoid (0–1, like Cohere and Jina), so Open WebUI's relevance threshold works.
- Rerank validation lives once, on the node.
- `/api/tags` now passes `capabilities` through, and the herd-synthesized models (`mlx:`, vision-embedding, image) read one set of constants, so `/api/tags` and `/api/show` can't disagree.
- `X-Fleet-Affinity` gains a `prefix` value.
- `/v1/systemone` traces with the `decision` tag, the way embeds tag themselves.

**Verified live on the Mac mini (after the upgrade to Ollama 0.35.0):**

| Phase | Result |
|---|---|
| 0 | herd's `/api/tags` matches Ollama's. dinov2 agrees across `/api/tags` and `/api/show`. On 0.35.0, gemma3 reports `vision` in both Ollama endpoints (on 0.33.3, `/api/tags` left it out) |
| 1 | Real metadata gives the right per-model limits. With a hypothetical `NUM_PARALLEL=4`: `qwen3.8:27b-mlx`=1 (safetensors), `nimble`=1 (qwen35), `gemma3:27b`=4, `nomic`=1. **But it isn't enforced:** four concurrent requests ran with `concurrency=1, in_flight=4, pending=0` (see Phase 1 finding below) |
| 2 | 0.35.0 installed with a matching SHA-256 and notarized signature. Decode 13.6 vs 13.7 tok/s baseline. `/v1/systemone` exists. Details in `docs/observations.md` (2026-10-02) |
| 3 | The rewritten `setup-mlx.sh` installs and verifies 0.32.0. Stock `mlx_lm.server` serves a chat with herd's exact flags (`--kv-bits 8 --kv-group-size 64 --quantized-kv-start 0`). The probe returns True against the real binary |
| 4 | `qwen3.8:27b-mlx` with `num_predict: 50`. **Direct to Ollama, the budget is exhausted (50 tokens) and content is `''`.** Through herd it's inflated to 1024, takes 153 tokens, and answers `6:44pm` (correct). `think:false` is not inflated |
| 5 | The right document ranks first (0.99). Cold 4.8 s (includes the 80 MB download), warm 17–31 ms. nomic and the reranker now read `cached=True` |
| 6 | `nimble` through herd matches direct Ollama exactly: `bug` 0.9781, confidence 0.8906, the same as Ollama's release-notes example |
| 7 | Two logical nodes, three sessions with a shared 1.9K-token head. The second and third followed the first with `X-Fleet-Affinity: prefix` (102 vs 92). One shared Ollama, so this proves the decision, not the cache causality |

**Phase 1 finding (pre-existing, not fixed here):** queue workers call `process_fn()`, receive an unconsumed generator, and immediately take the next request. So `concurrency` has never limited backend in-flight requests. The per-model values from this plan become effective once workers hold their slot. That's a hot-path change needing its own design. See the re-opened issue in `docs/issues.md`.

**Live testing also corrected Phase 1's rule:** `nimble` reports `completion` alongside `decision`, so `sched.go` doesn't force decision models serial. Only embedders (`embedding` without `completion`) are forced, plus the families list.

**Also found, out of scope:** non-streaming `/api/chat` through herd drops `message.thinking`, because `ollama_compat.py`'s aggregation keeps only `content`. Streaming passes it through.


## Sequencing & effort

| Phase | Effort | Net code | Depends on | Verify where | Ship gate |
|---|---|---|---|---|---|
| 0 — capabilities in meta | S | + small | — | this Mac | tests + live meta read |
| 1 — per-model parallelism | M | + small | 0 | this Mac (live after 2) | unit now; live MLX serial test after 2 |
| 2 — Ollama 0.35 upgrade | S + 24 h | none (ops) | — | this Mac | soak clean, percentiles in noise |
| 3 — retire mlx-lm patch | S | **− ~110 lines** | — | this Mac + **Mac Studio soak** | Mac Studio 24 h soak |
| 4 — capabilities replace heuristics | S–M | + small | 0 (+2 live) | this Mac | live empty-response repro fixed |
| 5 — `/v1/rerank` | M | extract first, then thin | — | this Mac | ranking sane, embed tests unchanged |
| 6 — `/v1/systemone` | S | extract first, then thin | 0, 2 | this Mac | matches direct-Ollama answer |
| 7 — prefix affinity | M | + small (reuses tracker/decay) | — | this Mac (decisions) + **2-node fleet** (benefit) | TTFT tail improves, no hot-spotting |

**Recommended order:** **0 → 1** first, since they're the only time-sensitive work, and Phase 1's unit tests can ship before the upgrade. Then **2** (it unblocks live verification of 1, 4, and 6), then **4 → 6 → 5**, then **3** (needs the Mac Studio), and **7** last.

Each phase is one commit or PR, with its own tests and CLAUDE.md's test count updated in the commit that adds the tests, since `health.sh` gates on it.

## Open questions

- **Which models does `0.40` actually move to MLX?** The rc note says "architectures supported by the MLX runtime", while `IsMLX()` is a *format* check. Phase 1 reads each model's format, so it doesn't depend on the answer. Record what's observed if a node runs the rc.
- **`qwen3.8`'s family string** isn't stated on ollama.com. Phase 1's live test answers it.
- **`/v1/systemone` with images:** the 32 MiB cap implies image input, but no decision model advertising vision was observed. Pass it through and don't build for it.
