# `available_gb` is the wrong ceiling for "can this model fit?" — it ignores that Ollama evicts its own models

**Status:** `OPEN` — needs a design decision, not a patch
**Severity:** Medium — causes real request failures, but only in a narrow window (cold model + dipped reading)
**Discovered:** 2026-07-17, while validating the Ollama 0.32.1 upgrade
**Files:** `src/fleet_manager/server/scorer.py` (~L207, L254), `src/fleet_manager/server/model_preloader.py` (~L258, L263), `src/fleet_manager/node/collector.py` (memory metrics)

---

## Summary

The scorer and preloader both ask *"will this model fit?"* by comparing the model's size against `node.memory.available_gb`, which comes from psutil's `virtual_memory().available`. On macOS with big resident models, that number is **both volatile and semantically wrong for the question being asked**, and it makes the router refuse to load models onto a box with hundreds of gigabytes genuinely free.

**The core mistake:** `available` treats resident model memory as unavailable. But **Ollama evicts its own LRU model to make room** — so that memory *is* available for a new model, just not for arbitrary allocation. We're gating on "free RAM right now" when the real question is "free RAM **plus** what Ollama would evict."

## Evidence

**It's volatile.** Sampling `/fleet/status` every 3 s on an idle-ish 512 GB M3 Ultra:

```
sample 1: available=360.67GB     sample 4: available=358.05GB
sample 2: available=360.67GB     sample 5: available=261.91GB   ← -97GB in 3s
sample 3: available=358.05GB     sample 6: available=275.78GB
```

Observed range across the session: **17 GB → 445 GB**, with no workload change big enough to explain it.

**Why it swings** — psutil's own numbers at one instant:

```
total:      549.8 GB
available:  278.5 GB   ← what the node reports
free:       248.2 GB
wired:      217.4 GB   ← resident models are WIRED; wired is NOT counted as available
inactive:    28.9 GB   ← reclaimable, also not counted
```

Resident models (`gpt-oss:120b` 65 GB + `qwen3-coder:30b` 31 GB + `gemma3:4b` 5.8 GB ≈ 102 GB, plus ~34 GB of `mlx_lm.server`) are **wired**. macOS moves memory between wired/active/inactive as models load and serve, so `available` moves with it.

**It causes real failures.** After the 2026-07-17 reboot, with `gpt-oss:120b` not yet resident and the reading dipped:

```
scorer: All 1 nodes eliminated for model 'gpt-oss:120b'      (×100+)
routing: Holding queue timeout: no node became available for gpt-oss:120b within 30.0s
ollama_compat: No nodes for model=gpt-oss:120b fallbacks=[]
```

The scorer computed *"need 72 GB, have 17 GB → eliminate"* on a machine with ~358 GB actually free. Requests failed until the model happened to load.

## Scope — narrower than it looks

The scorer **already** skips the memory check for a model that's already resident ([`scorer.py:203`](../../src/fleet_manager/server/scorer.py)):

```python
# Check memory can fit if model needs loading
if model not in loaded_names:      # ← correct: resident models aren't gated
```

So this only bites in the window where **(a)** the model isn't loaded yet **and** **(b)** the reading is dipped — i.e. **cold start, first request, or right after a restart.** Once the model is resident it stops mattering. That's why the fleet looks fine in steady state and misbehaves right after a reboot.

This is a *different* bug from the [pin memory-gate issue](../issues.md) fixed the same day (where the gate ran against an already-resident model). The "is it loaded?" guard is present here; the **metric** is what's wrong.

## Why this needs a decision, not a patch

Four options, each with a real trade-off:

1. **`available + evictable`** — add back the resident-model memory Ollama would evict (we now have real per-model sizes from `/api/tags`). *Most correct for the actual question.* Risk: over-promises if Ollama declines to evict (pinned/`keep_alive=-1` models won't be evicted, so those must be excluded).
2. **Smooth the reading** — rolling median over N heartbeats. Kills the 3-second spikes; doesn't fix the semantics, and adds lag when memory genuinely fills.
3. **Use `memory_pressure` instead** — macOS's own pressure signal tracked truer than psutil in observation (56–91% free while `available` swung 97 GB). Platform-specific; no direct GB figure.
4. **Lean on `capacity.ceiling_gb`** — the capacity learner already computes a ceiling and the scorer already does `min(available, ceiling)`. Could become the primary signal.

**Complication for option 1:** a pinned model (`keep_alive=-1`) is *not* evictable, so "evictable" must mean *resident − pinned*. We now have the pin store and real sizes, so this is computable — it's just not a one-liner.

## Not doing yet

Deliberately left open. The failure window is narrow and self-healing, and picking the wrong metric here would fail in the **dangerous** direction — over-reporting capacity means loading a model that doesn't fit, which is exactly the class of mistake that produced the 290 GB thrash loop and a kernel panic on 2026-07-17. An under-report costs a cold-start retry; an over-report costs the box.

## Suggested first step

Instrument before changing: log `available_gb` alongside `wired`/`inactive`/`free` and the resident-model set on every heartbeat for a day, then check how often the dip actually coincides with an elimination. That tells us whether to fix the metric (option 1) or just the volatility (option 2) — and gives a baseline to verify against.

---

## Update 2026-07-17 — this metric has now kernel-panicked the box twice

**Both panics were the same model**, requested by two different callers, and both were `watchdog timeout: no checkins from watchdogd` (a *starvation* panic — userspace never got a chance to log anything):

| Panic | Trigger | Model |
|---|---|---|
| 02:11 | a `/fleet/pin` test that fell through to a real load | `qwen3-coder:480b` |
| 04:08 | a client agent's `run-matrix.sh` run, whose `config.sh` defaults to the full 8-model roster starting with the 480b | `qwen3-coder:480b` |

### Contributing cause now FIXED: the scorer had its own estimator

The scorer kept a **private copy** of the size heuristic. After the preloader's copy was taught to read Ollama's real `/api/tags` sizes, the scorer's kept guessing from the name — and it knew `671b` and `405b` but not `480b`, so it fell through to a **10 GB "default"**:

```
scorer said  qwen3-coder:480b-a35b-q4_K_M =  10.0 GB   (really 290 GB, ~348 GB resident)
scorer said  llama4:maverick              =  10.0 GB   (really 244 GB)
```

**This is the path a client request takes.** The 480b was scored "10 GB, plenty of room", routed, and Ollama loaded 348 GB. Fixed: the scorer now delegates to the shared estimator (real sizes first). Duplicated logic doesn't drift symmetrically — it drifts until one copy is dangerous.

### But the estimator fix alone does NOT prevent this

With correct sizes, the gate still passes:

```
available 431GB, model resident ~348GB  ->  348 < 431  ->  LOADS  ->  panic
```

348 GB of a 512 GB box leaves 164 GB — minus the OS, a 20 GB VM, the MLX servers (~34 GB), and the **pinned `gpt-oss:120b` (76 GB) that the fleet exists to serve**. The box starves.

### The missing check: reserve the fleet's committed set

`available` is a snapshot of "free right now". It does **not** account for memory the fleet has *committed* to but hasn't materialised — chiefly **pinned models that aren't currently resident**. At the moment the 480b loaded, gpt-oss was not resident, so its 76 GB looked free. It wasn't; it was spoken for.

```
available 431GB - pinned-but-not-resident gpt-oss (76 x 1.2 = 91GB) = 340GB
model resident 348GB > 340GB  ->  REFUSE     <-- the check we needed
```

**A flat "max fraction of total RAM" cap was tried and rejected.** At 0.6 it eliminates a 70B model on a 64 GB node — a legitimate, common deployment — while still being the wrong model of the problem (it ignores what else must be resident). The fraction isn't the invariant; the **committed set** is.

### Suggested design

`available_for_new_model = available_gb − Σ(resident cost of pinned models not currently loaded)`, then keep the existing `× 1.2` resident-overhead comparison. This reuses machinery that already exists: the pin store, real per-model sizes from `/api/tags`, and the `_PIN_MEMORY_BUDGET_FRACTION` precedent in `/fleet/pin`'s admission control. Open questions: whether MLX servers' footprint should also be reserved (they're separate processes, so they're already outside `available`), and whether the guard belongs in the scorer's `_eliminate`, the preloader's gate, or both.

**Operationally, right now:** `qwen3-coder:480b` (290 GB) and `deepseek-v3:671b` (404 GB) simply do not fit this fleet alongside its own committed set. Until the guard exists, they should not be requested or benchmarked on this box.

