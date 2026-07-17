# Ollama ships a native MLX runner — our MLX subsystem's premise (and our documented Ollama facts) are stale

**Status:** `OPEN` — investigation required before any code change
**Severity:** Medium–High (strategic: a large subsystem may be redundant; **plus** load-bearing design constants may be wrong)
**Discovered:** 2026-07-17, from an external tip that "Ollama 0.19 now runs on MLX"
**Upstream:** [ollama.com/blog/mlx](https://ollama.com/blog/mlx) — *"Ollama is now powered by MLX on Apple Silicon in preview"* (first-party) · [ollama.com/blog/mlx-performance](https://ollama.com/blog/mlx-performance)

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

## Investigation plan (read-only first — do not disturb the 0.8.2 soak)

1. **Re-verify the hot-model cap on 0.24.** Cheap, highest urgency: it may already be invalidating live behavior (`free_slots`, preloader budget). If the cap moved, fix the constants.
2. **Test `OLLAMA_NEW_ENGINE=1`** and measure `glm-4.7-flash` decode tok/s. If Ollama's MLX serves it near the 59 tok/s we get from `mlx_lm.server`, the `glm4moelite` issue closes at the source.
3. **Map Ollama's MLX model coverage** against what we run on `mlx_lm.server`. **This single fact decides whether the MLX subsystem is legacy or load-bearing.**
4. **Check whether Ollama's MLX prefix-cache (`mlxrunner.trieNode`) actually reuses prefixes** — a client agent measured **zero** prefix reuse on the llama.cpp path (1,873 prompt tokens re-evaluated on an identical-prefix repeat). If the MLX runner fixes that, it changes the caching guidance too.
5. **Consider upgrading Ollama** to the stable-MLX version (0.30, if the third-party claim holds) — but *not* mid-soak.
6. **Then** decide the subsystem's fate: keep for coverage gaps + distributed, or begin deprecation.

## Immediate doc fixes (independent of the above)

- `CLAUDE.md`: correct `0.20.4` → the actual running version, and re-qualify the 3-model-cap claim as **version-specific and unverified on 0.24+**.
- Add the Ollama version to soak checks so this can't silently drift again — the whole issue exists because a documented constant outlived its truth.

---

## Meta

This landed as an unsourced tip ("Ollama 0.19 runs MLX") that was **directionally right and wrong in its specifics** — 0.19 was the preview; we're on 0.24; stable is reportedly 0.30. The version number in the tip is what made it checkable: 0.19 < 0.20.4 (documented) was internally inconsistent, which is what prompted the check that found the real story. **Verify the version claim, not the vibe.**
