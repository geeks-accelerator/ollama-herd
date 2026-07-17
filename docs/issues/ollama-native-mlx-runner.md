# Ollama ships a native MLX runner — our MLX subsystem's premise (and our documented Ollama facts) are stale

**Status:** `OPEN` — **investigated 2026-07-17; the premise was half-wrong. See "Answers" below.** The subsystem decision is still open, but for a *different and stronger* reason than this issue originally argued.

---

## ✅ Answers (measured 2026-07-17, on Ollama 0.32.1)

We upgraded `0.24.0` → `0.32.1` and measured. **The headline finding inverts this issue's premise:**

> **Ollama's MLX runner is NOT active — and Ollama is faster than our MLX anyway.**

| Q | Answer |
|---|---|
| **Q1 — is the 3-model cap real?** | ❌ **No.** With `OLLAMA_MAX_LOADED_MODELS=10` we observed **4 concurrent residents**. The "hardcoded 3 on macOS" claim is dead. Nodes now report their own cap; `free_slots` went 2 → 9. |
| **Q2 — does it fix glm?** | ✅ **Yes, spectacularly.** **13.7 → 77.8 tok/s (5.7×)**. `gpt-oss:120b`: 50.9 → 74.5. See [`glm-4.7-flash-ollama-glm4moelite-slow.md`](glm-4.7-flash-ollama-glm4moelite-slow.md) — **now FIXED**. |
| **Q3 — does prefix caching work?** | ✅ **Yes — and the "zero reuse" report was a measurement error.** Cold prefill 568 ms → **38 ms** warm on the same prefix (**29× faster per token**). The client agent measured `prompt_eval_count`, which reports the **logical prompt size, not tokens computed** — it stays flat whether caching works or not. Their "1,873 tokens re-evaluated" was never evidence of a miss. |
| **Q4 — MLX model coverage?** | ⚠️ **Zero — MLX isn't running at all.** `ggml_metal_init` + `llama_model_loader` throughout; **0 `mlx` mentions in a 5 MB server log**; `OLLAMA_NEW_ENGINE` unset. Every gain above came from **llama.cpp**, with MLX dormant behind an opt-in (`--mlx-engine` / `OLLAMA_NEW_ENGINE`). |

### What this means — the case against our MLX subsystem got *stronger*, not weaker

This issue argued: *"Ollama has native MLX, so our `mlx_lm.server` stack may be redundant."* The reality is sharper:

> **Ollama's llama.cpp path (77.8 tok/s) already beats our `mlx_lm.server` (59 tok/s) for glm — and Ollama's MLX isn't even switched on yet.**

So we maintain `MlxSupervisor`, `mlx_proxy`, the `mlx:` prefix, `setup-mlx.sh`, the `--kv-bits` patch that breaks on every `mlx-lm` upgrade, plus a week of bug tax (kv_bits crash, dropped `reasoning` field, port races, jetsam churn) — to be **32% slower** than the backend we route around. Enabling Ollama's MLX could widen that gap further.

**Still ours:** distributed multi-Mac inference (`mlx.launch`) — Ollama is single-host. That is now the *only* load-bearing justification for the subsystem.

**Unanswered:** whether `OLLAMA_NEW_ENGINE=1` makes MLX faster than 77.8. Requires an Ollama restart; not yet tested.

---
**Severity:** Medium–High (strategic: a large subsystem may be redundant; **plus** load-bearing design constants may be wrong)
**Discovered:** 2026-07-17, from an external tip that "Ollama 0.19 now runs on MLX"
**Upstream:** [ollama.com/blog/mlx](https://ollama.com/blog/mlx) — *"Ollama is now powered by MLX on Apple Silicon in preview"* (first-party) · [ollama.com/blog/mlx-performance](https://ollama.com/blog/mlx-performance)
**▶ Plan to resolve this:** [`plans/ollama-0.32-upgrade-and-mlx-evaluation.md`](../plans/ollama-0.32-upgrade-and-mlx-evaluation.md) — upgrade `0.24.0` → **`v0.32.1`** (we are 8 versions behind and predate stable MLX), then answer the four questions below with measurements.

---

## Summary

Ollama now has a **first-party native MLX runner**. Our entire `mlx_lm.server` subsystem — supervisor, proxy, `mlx:` prefix, setup script, kv-bits patch, and their ongoing bug tax — exists **because Ollama couldn't run MLX**. That premise is expiring.

Separately and more urgently: **our documented Ollama facts are wrong**, and design constants depend on them.

This issue is filed to force verification *before* anyone acts. Nothing here yet justifies deleting code.

---

## Verified (checked on this fleet, 2026-07-17)

| Claim | Evidence |
|---|---|
| **Ollama here is `0.24.0`** — not the `0.20.4` CLAUDE.md documents | `ollama --version` and `GET :11434/api/version` both return `0.24.0` |
| **The binary contains a full native MLX runner** | `strings $(which ollama)` → **6,925** `mlx` hits, incl. the Go package **`github.com/ollama/ollama/x/mlxrunner`** |
| **Real MLX C API bindings (CGo), not a mention** | `mlx.Array`, `mlx._Ctype_struct_mlx_array_`, `mlx_enable_compile`, `mlx_set_memory_limit`, `mlx_get_active_memory`, `mlx_get_peak_memory` |
| **Runtime lifecycle plumbing exists** | *"failed to stop mlx worker"*, *"mlx runner closed response before completion"*, *"mlx scanner EOF without Done response — subprocess may have crashed"*, *"converting source E4M3 block-FP8 to MLX %s"* |
| **MLX-specific env knobs exist** | `OLLAMA_MLX_MTP_DRAFT_SCHEDULE`, `OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS`, `OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS`, `OLLAMA_MLX_MTP_SERIAL_VALIDATE`, `OLLAMA_MLX_MTP_COMPARE_SERIAL_VALIDATE`, **`OLLAMA_NEW_ENGINE`** |
| **Prefix-caching machinery is plausibly present** | `mlxrunner.trieNode` — a trie, the standard structure for prompt-prefix caches |
| **Speculative decoding is present** | `MTP` = multi-token prediction; *"MTP greedy decode enabled"*, *"MTP sample decode enabled"* |
| **First-party confirmation** | Ollama's own blog (above): MLX preview on Apple Silicon, **32 GB+ unified memory required** |
| **The preview was narrow** | Blog: *"This preview release of Ollama accelerates the new Qwen3.5-35B-A3B model"* — **one model** |

## Not yet verified (do not act on these)

- **That `OLLAMA_NEW_ENGINE` is the gate for MLX.** The var exists; its exact semantics are inferred, not confirmed.
- **That MLX went stable in `0.30`.** This comes from a **third-party** blog ([runaihome](https://runaihome.com/blog/ollama-v030-mlx-stable-upgrade-2026/)), not Ollama. If true, we are two minor versions behind stable.
- **Ollama's current MLX model coverage.** This is the decisive unknown (see below).
- **Whether the 3-model hot cap still exists on 0.24+.**

---

## Why this matters — two separate problems

### 1. Our documented Ollama facts are stale, and they're load-bearing

`CLAUDE.md` states: *"Ollama 0.20.4 macOS has a hardcoded 3-model hot cap — no env configuration we've found will raise it."* We are on **0.24.0**.

That cap is not trivia — it is the foundation of live design:

- `model_preload_max_count` default `3` (the preloader's "don't thrash the LRU" invariant)
- `OLLAMA_HOT_MODEL_CAP = 3` in `serializers.py` → `free_slots` on `/fleet/status` and `/fleet/limits`
- The `OLLAMA_MAX_LOADED_MODELS=-1` gotcha
- The eviction/pin logic and the memory-gate assumptions around it

**If 0.24/0.30 lifted or changed that cap, the herd is enforcing a constraint that no longer exists** — throttling the fleet and reporting `free_slots` from a fictional limit. This is the most immediately actionable item in this issue.

### 2. The MLX subsystem's reason to exist is expiring

Built on the premise "Ollama can't do MLX":

- `node/mlx_supervisor.py` — `MlxSupervisor`/`MlxSupervisorSet`: spawn, health, quarantine, orphan reap, port-release races
- `server/mlx_proxy.py` — the `mlx:` prefix, OpenAI↔Anthropic translation, per-URL client pool
- `scripts/setup-mlx.sh` + the `--kv-bits` patch — **breaks on every `uv tool upgrade mlx-lm`**
- Its ongoing bug tax, all in the last week: the `kv_bits` crash on `glm4_moe_lite`, the dropped `reasoning` field (empty GLM responses), port-release races, and the idle-server external-SIGKILL churn

If Ollama serves our models on MLX natively, most of that is **complexity we maintain for zero gain**.

### 3. Advice we've already given may be expiring

We told client agents: *"use `mlx:` for GLM — the Ollama path is slow (13.7 tok/s) and can't prefix-cache."* That was measured against Ollama's **llama.cpp** path. Against Ollama's MLX runner (which has a prefix-cache trie and MTP speculative decoding) it may simply be false. See [`glm-4.7-flash-ollama-glm4moelite-slow.md`](glm-4.7-flash-ollama-glm4moelite-slow.md) — its "Fix path: serve via MLX, not Ollama" may now be achievable *inside* Ollama.

---

## What is NOT threatened

Worth stating plainly so this doesn't get over-read:

- **The herd's core value is routing, not MLX.** 7-signal scoring, per-`node:model` queues, health engine, dashboard, multi-node coordination. **A faster Ollama makes the herd better for free.**
- **Distributed inference across multiple Macs** (0.8.0's `mlx.launch` ring/jaccl work) is still ours — Ollama is single-host. If MLX coverage lands everywhere else, *this* stays the differentiator.
- **Model coverage may still favor `mlx_lm`**, which runs arbitrary HuggingFace MLX conversions (`GLM-4.7-Flash-MLX-4bit`, `Qwen3-Coder-Next-4bit`). Ollama's MLX started at **one** model. Coverage is the whole question.

---

## Investigation plan

**Superseded 2026-07-17 by [`plans/ollama-0.32-upgrade-and-mlx-evaluation.md`](../plans/ollama-0.32-upgrade-and-mlx-evaluation.md)**, which carries the full execution plan, version analysis, install mechanics, rollback, and risks. Summary of what it resolves:

1. **Upgrade `0.24.0` → `v0.32.1`.** Verified 2026-07-17: latest is **v0.32.1** (2026-07-16) — we are **8 minor versions behind**, and 0.24 **predates stable MLX** (the 0.30 line). 0.32.1 is the pick because it fixes a *recurrent MLX model cache leak* that **every earlier version carries** — including the "safer" 0.31.2. Given this fleet's memory sensitivity, an older version means deliberately choosing the leak.
2. **Q1 — the hot-model cap.** Highest urgency; may already be invalidating `free_slots` + the preloader budget. ⚠️ **Attempted 2026-07-16 and blocked**: with the soak live, `OLLAMA_NUM_PARALLEL=2` was saturated by gpt-oss, so model loads queued and timed out at 180 s — a second model never became resident. **Must run in the post-restart quiet window.** (Also: don't test with `nomic-embed-text` — it's embed-only and `/api/generate` rejects it.)
3. **Q2 — glm-4.7-flash speed** on Ollama's MLX vs the 13.7 baseline / 59 tok/s `mlx_lm` reference.
4. **Q3 — prefix reuse** (`mlxrunner.trieNode`) vs the client agent's measured zero-reuse (1,873 tokens re-evaluated).
5. **Q4 — MLX model coverage.** The decisive question for this subsystem's fate.
6. **Then** decide: keep for coverage gaps + distributed multi-Mac, or begin deprecation.

**Sequencing (decided 2026-07-17):** upgrade **now** rather than after the 0.8.2 soak — 0.8.2 shipped ~1 h prior, so re-soaking costs ~1 hour, versus a full cycle if the need surfaces later. Soak once, on the final stack.

**Install landmines found:** Ollama here is the **Mac app** (`/usr/local/bin/ollama` → `/Applications/Ollama.app/…`), so **`brew upgrade ollama` would do nothing useful** — and a **stale brew formula sits at `ollama 0.16.3`**, a live PATH hazard worth removing.

## Immediate doc fixes (independent of the above)

- `CLAUDE.md`: correct `0.20.4` → the actual running version, and re-qualify the 3-model-cap claim as **version-specific and unverified on 0.24+**.
- Add the Ollama version to soak checks so this can't silently drift again — the whole issue exists because a documented constant outlived its truth.

---

## Meta

This landed as an unsourced tip ("Ollama 0.19 runs MLX") that was **directionally right and wrong in its specifics** — 0.19 was the preview; we're on 0.24; stable is reportedly 0.30. The version number in the tip is what made it checkable: 0.19 < 0.20.4 (documented) was internally inconsistent, which is what prompted the check that found the real story. **Verify the version claim, not the vibe.**
