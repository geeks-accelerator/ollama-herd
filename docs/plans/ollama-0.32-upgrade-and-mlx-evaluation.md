# Ollama 0.24 → 0.32.1 Upgrade + Native-MLX Evaluation

**Status**: 📋 Planned — not yet executed
**Created**: 2026-07-17
**Issue**: [`issues/ollama-native-mlx-runner.md`](../issues/ollama-native-mlx-runner.md) — this plan is how that issue gets resolved
**Related**: [`issues/glm-4.7-flash-ollama-glm4moelite-slow.md`](../issues/glm-4.7-flash-ollama-glm4moelite-slow.md) (may close at the source), `CLAUDE.md` (stale Ollama facts)

---

## TL;DR

We are running **Ollama 0.24.0**. Latest is **v0.32.1** — **eight minor versions ahead**, and our version **predates stable MLX** (which landed in the 0.30 line). Ollama now has a first-party native MLX runner compiled into the binary we already have; we're getting none of its benefit.

**Upgrade to `v0.32.1`, then answer four questions with measurements** — each of which invalidates or confirms a load-bearing assumption in this codebase. The answers decide whether our entire `mlx_lm.server` subsystem still earns its keep.

**Sequencing note (decided 2026-07-17):** 0.8.2 shipped ~1 h before this plan, so the soak is young. **Upgrade now and soak once on the final stack** rather than soaking 0.24.0, publishing, and then re-soaking after the upgrade. Cost of restarting the soak now ≈ 1 hour; cost of discovering the need later ≈ a full cycle.

---

## Why 0.32.1 (and not a "safer" older version)

| Version | Bake | MLX state | Verdict |
|---|---|---|---|
| **v0.32.1** (2026-07-16) | 1 day | **Fixes a recurrent MLX model cache leak**; MLX text-model loading respects `OLLAMA_LOAD_TIMEOUT` | ✅ **pick** |
| v0.32.0 (2026-07-11) | 5 days | ⚠️ **has** the MLX memory leak | no |
| v0.31.2 (2026-07-06) | 11 days | MLX + llama.cpp engine update; ⚠️ **also has the leak** | fallback only |
| v0.31.1 (2026-06-30) | 17 days | New MLX small-batch matmul kernel; improved MTP | older MLX |
| v0.30.11 (2026-06-25) | ~3 wks | `mlxrunner`: unified/tuned speculative decoding | stable-MLX line, too far back |

**The counterintuitive part:** 0.32.1 is only a day old, but it's a *small hotfix* on a 0.32.0 that already has 5 days of bake, and its headline fix is *"a recurrent MLX model cache leak that could increase memory use across requests."* **"Recurrent" means every earlier version carries that leak — including the "safer" 0.31.2.** We are acutely memory-sensitive (512 GB box, 76 GB models, and an open issue where an idle MLX server was externally SIGKILLed under memory pressure). Choosing an older version to be conservative would mean **deliberately choosing the memory leak.**

**Risk that is NOT in play:** 0.32.0 changed the bare `ollama` command to launch an interactive agent. **Verified 2026-07-17: the herd never shells out to the Ollama CLI — it is API-only (`:11434`).** So this change cannot affect us.

---

## Install mechanics — two landmines

Both were found by inspection on 2026-07-17 and would have silently wasted the upgrade.

### 1. `brew upgrade ollama` would do nothing useful

Ollama here is **the Mac app**, not a brew formula:

```
/usr/local/bin/ollama -> /Applications/Ollama.app/Contents/Resources/ollama
running:  /Applications/Ollama.app/Contents/MacOS/Ollama hidden
```

The upgrade path is **Ollama.app's own updater** (menu bar → check for updates) or the official download from ollama.com. **Not brew.**

### 2. A stale Homebrew formula sits at `ollama 0.16.3`

`brew list --versions ollama` → **`ollama 0.16.3`**. It is shadowed by the app symlink today, but it is a live PATH hazard: if anything ever resolves to it, we'd silently run a version **eight releases behind the one we're already behind on**. **Remove it** (`brew uninstall ollama`) as part of this work, after confirming nothing depends on it.

---

## The four questions this upgrade answers

Each maps to an assumption currently baked into the code. **Measure, don't assume** — every one of these is currently taken on faith.

### Q1 — Is the 3-model hot cap still real?

**Assumption in code:** `model_preload_max_count = 3`; `OLLAMA_HOT_MODEL_CAP = 3` in `serializers.py` → drives `free_slots` on `/fleet/status` and `/fleet/limits`; the `OLLAMA_MAX_LOADED_MODELS=-1` gotcha; the eviction/pin logic.

**Current state:** `OLLAMA_MAX_LOADED_MODELS=10` is already set (shell + launchctl + live process) — the `-1` gotcha was fixed at some point. 0.24's help says *"Maximum number of loaded models **per GPU**."* But the herd **caps itself at 3 regardless**, so we'd never observe more.

**If the cap is gone/raised:** we are throttling the fleet against a fiction and reporting `free_slots` from a fiction. Fix the constants.

> ⚠️ **This test was attempted 2026-07-16 and blocked.** With the soak live, `OLLAMA_NUM_PARALLEL=2` was saturated by gpt-oss inference; model-load requests queued behind it and timed out at 180 s each — a second model never became resident, so there was nothing to measure. (This reproduced the known contention documented in CLAUDE.md: *"in-flight LLM inference holds both slots; embed requests queue indefinitely inside Ollama regardless of available hardware."*) **Run this in the post-restart quiet window, before traffic resumes.**

### Q2 — Does Ollama's MLX fix glm-4.7-flash?

**Assumption:** glm decodes at ~13.7 tok/s on Ollama (dense speed) because Ollama's `glm4moelite` path CPU-offloads the experts ([ollama#14045](https://github.com/ollama/ollama/issues/14045)); the fix is to serve it via `mlx_lm.server` (~59 tok/s).

**If Ollama's MLX serves glm near 59 tok/s:** [`glm-4.7-flash-ollama-glm4moelite-slow.md`](../issues/glm-4.7-flash-ollama-glm4moelite-slow.md) **closes at the source**, and the advice we gave client agents ("use `mlx:` for GLM") expires.

### Q3 — Does prefix caching now work on the Ollama path?

**Assumption:** it doesn't. A client agent measured **zero** prefix reuse — an identical-prefix repeat re-evaluated all **1,873** prompt tokens (`prompt_eval_count` unchanged). We attributed this to per-slot KV caching with `OLLAMA_NUM_PARALLEL≥2` spreading requests across slots.

**Evidence it may change:** the binary contains **`mlxrunner.trieNode`** — a trie, the standard structure for prompt-prefix caches — and 0.32.1 mentions *"improved cache snapshot"* handling.

**If it works:** big win for identical-system-prompt workloads (the scanner re-processes ~1,870 tokens per concept today), and the caching guidance we gave expires too.

### Q4 — What is Ollama's MLX **model coverage**?

**This is the decisive question.** The 0.19 preview accelerated exactly **one** model (Qwen3.5-35B-A3B). By 0.32.1 the notes say *"**MLX text model** loading"* — generic phrasing suggesting broad support. `mlx_lm` runs **arbitrary HuggingFace MLX conversions** (`GLM-4.7-Flash-MLX-4bit`, `Qwen3-Coder-Next-4bit`, `Qwen3-Coder-30B-A3B-Instruct-4bit`).

**Coverage decides the subsystem's fate:**
- **Broad coverage** → `mlx_lm.server` becomes legacy for single-host use; begin deprecation.
- **Narrow coverage** → it stays load-bearing for the models Ollama can't serve.

---

## Execution plan

### Phase 0 — Pre-flight (before touching anything)

- [ ] Record the baseline so the comparison is real: current glm-4.7-flash decode tok/s, prefix-reuse `prompt_eval_count`, `ollama ps` residents, and `/dashboard/api/health`.
- [ ] Note the current Ollama version (`0.24.0`) and the herd version (`0.8.2`) in the soak log.
- [ ] Confirm `~/.fleet-manager/env` + `~/.zshrc` Ollama vars are as expected (`OLLAMA_NUM_PARALLEL=2`, `OLLAMA_MAX_LOADED_MODELS=10`, `OLLAMA_KEEP_ALIVE=-1`).

### Phase 1 — Upgrade

- [ ] Update **Ollama.app** → **v0.32.1** (app updater, or official download from ollama.com — **not brew**).
- [ ] Verify: `ollama --version` **and** `GET :11434/api/version` both report `0.32.1` (the symlink must point at the upgraded app).
- [ ] `brew uninstall ollama` — remove the stale 0.16.3 formula (confirm nothing depends on it first).
- [ ] Confirm every model on disk still loads (no format break expected, but verify `gpt-oss:120b` + `glm-4.7-flash`).

### Phase 2 — Measure in the quiet window (before traffic resumes)

**Order matters — Q1 must run before the soak saturates the slots.**

- [ ] **Q1 — cap test.** Load 3 generate-capable small models (`gemma3:4b` 3.3 GB, `qwen2.5-coder:7b` 4.7 GB, `qwen3:8b` 5.2 GB — all on disk) with a short `keep_alive` so they self-clean. Count residents via `ollama ps`. **Do not use `nomic-embed-text` — it's embed-only and `/api/generate` rejects it** (that mistake wasted the first attempt).
- [ ] **Q2 — glm speed.** Measure decode tok/s for `glm-4.7-flash` via Ollama; compare against the 13.7 baseline and the 59 tok/s `mlx_lm` reference.
- [ ] **Q3 — prefix reuse.** Repeat the client agent's test: same-prefix repeat, compare `prompt_eval_count` (1,873 → low = reuse working).
- [ ] **Q4 — coverage.** Determine which of our models Ollama actually serves on MLX vs llama.cpp (per-model, via logs/telemetry).

### Phase 3 — Reconcile the docs to reality

- [ ] `CLAUDE.md`: correct `0.20.4` → `0.32.1`; re-qualify or remove the 3-model-cap gotcha per Q1; update the `OLLAMA_MAX_LOADED_MODELS` note (the `-1` trap is already fixed in our env).
- [ ] **Add the Ollama version to soak checks** so this can never silently drift again — the whole issue exists because a documented constant outlived its truth.
- [ ] Update the glm issue per Q2; update the prefix-caching guidance per Q3.
- [ ] If Q1 changed the cap: fix `model_preload_max_count` + `OLLAMA_HOT_MODEL_CAP` and the `free_slots` math.

### Phase 4 — Soak

- [ ] Restart herd on the final stack (0.8.2 + Ollama 0.32.1). **This is the soak that counts** — publish 0.8.2 only after it's clean on this stack.
- [ ] Watch specifically: memory trend (the 0.32.1 leak fix is why we're on this version), MLX server stability, and — the 0.8.2 fix that most needs real traffic — that **failed requests now appear as `failed` rows** in the trace DB rather than vanishing.

### Phase 5 — Decide the MLX subsystem's fate (separate work)

Only with Q1–Q4 answered. Options, in order of likelihood:
- **Narrow coverage** → keep `mlx_lm.server` for the gap models; no change.
- **Broad coverage** → begin deprecating `MlxSupervisor`/`mlx_proxy`/`setup-mlx.sh` and the `--kv-bits` patch for single-host use, **keeping distributed multi-Mac** (`mlx.launch`), which Ollama does not do.
- Either way: **routing remains the herd's value** — a faster Ollama makes the herd better for free.

---

## Rollback

- Keep the current **Ollama.app** (0.24.0) — archive a copy before upgrading so rollback is a drag-and-drop, not a re-download hunt for an old version.
- The herd is **version-agnostic over the Ollama HTTP API**; nothing in 0.8.2 depends on 0.24-specific behavior, so rollback is app-only (no herd changes).
- If 0.32.1 misbehaves: revert the app, restart herd, and re-soak on 0.24.0 as originally planned.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| 8-version jump changes several behaviors at once, hard to attribute | **Med-High** | Isolate: upgrade Ollama **only** — no herd changes in the same window. Measure Q1–Q4 before resuming traffic |
| 0.32.1 is 1 day old; unknown regressions | Med | It's a hotfix on a 5-day-baked 0.32.0; rollback is drag-and-drop. Alternative (0.31.2) carries the MLX leak — a known bad trade |
| MLX becomes active and changes perf/memory profile under us | Med | That's the *point* — but measure the baseline in Phase 0 so the delta is attributable |
| The cap changes and our constants throttle or over-promise | Med | Q1 gates the constants fix; `free_slots` is reported to clients, so a wrong cap is externally visible |
| Model formats break on upgrade | Low | Verify the two critical models load in Phase 1; rollback if not |
| Upgrading invalidates the fresh 0.8.2 soak | **Certain** | Accepted deliberately — soak is ~1 h old; re-soaking once on the final stack is cheaper than soaking twice |

---

## Open questions

1. **Upgrade mechanism** — app's built-in updater, or fetch the official installer? (A download needs an explicit go-ahead.)
2. **Pin the Ollama version?** We pin `mlx-lm` at 0.31.3 for exactly this reason. Ollama auto-updating out from under the fleet is how we ended up 8 versions adrift while documenting `0.20.4`. Consider disabling the app's auto-update and treating Ollama upgrades as deliberate, verified events.
3. **If Q4 says coverage is broad** — deprecate `mlx_lm.server` on what timeline, and does distributed multi-Mac alone justify keeping the subsystem?
