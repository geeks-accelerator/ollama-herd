# Model sizing counts weights and ignores the KV cache — under-estimates by up to 5.4×

**Status:** `PARTIAL` — resident-cost learning implemented 2026-07-17 and wired into all three fit decisions (preloader gate, `/fleet/pin` admission, scorer); the committed-set reserve (below) is still open
**Severity:** **High** — the memory gate has twice let a load through that kernel-panicked the box
**Discovered:** 2026-07-17, by a client agent auditing their own benchmark chart
**Files:** `src/fleet_manager/server/model_preloader.py`, `src/fleet_manager/server/scorer.py`, `src/fleet_manager/server/routes/fleet.py`
**Related:** [`available-gb-is-the-wrong-ceiling-for-model-fit.md`](available-gb-is-the-wrong-ceiling-for-model-fit.md) (the volatility this explains)

---

## Summary

Every "will this model fit?" decision in the herd sized models by their **on-disk weights** × a constant `1.2`. What a model actually costs resident is **weights + KV cache**, and the KV cache scales with the **context window** — which can dwarf the weights.

> *"`ollama ps` shows qwen3-coder:30b resident at 114-122GB, not 18GB. The weights are 18GB; a 262,144-token context KV cache is the rest."* — the client agent who caught it, while noticing their own benchmark's memory axis had the same flaw.

## Evidence (measured on the fleet, 2026-07-17)

```
model             disk    resident      ctx        KV≈    ratio
qwen3-coder:30b  18.6G     122.9G   262144     104.3G    6.6x
gpt-oss:120b     65.4G      65.4G   131072       0.0G    1.0x
```

| Model | Gate assumed | Actual | Error |
|---|---|---|---|
| `qwen3-coder:30b` | `18.6 × 1.2 = 22.8 GB` | **122.9 GB** | **5.4× under** |
| `gpt-oss:120b` | `65.4 × 1.2 = 86.4 GB` | 65.4 GB | 1.3× over |

**`_RESIDENT_OVERHEAD = 1.2` was a fiction.** The true ratio spans **1.0×–6.6×**, and it's dominated by *context*, not by the model. (gpt-oss showing ~zero KV is itself instructive — its attention layout is far cheaper per token. The between-model variance is exactly why a constant can't work.)

## The KV cost is linear in context and predictable per model

Two observations of the same model at different contexts:

```
qwen3-coder:30b @  32768:  18.6G weights + 12.4G KV = 31.0G    -> 0.387 MB/token
qwen3-coder:30b @ 262144:  18.6G weights + 104.3G KV = 122.9G  -> 0.407 MB/token
```

Consistent to ~5%. So:

```
resident ≈ weights + kv_per_token × num_ctx
```

**Same weights, 4× the memory, purely from context** — 31 GB @ 32K vs 122 GB @ 262K.

## Why this is the root cause of the 2026-07-17 crashes

- **Both kernel panics** (`watchdog timeout`, 02:11 and 04:08): models cost multiples of what the gate believed, so "it fits" was never true.
- **The `available_gb` volatility** in the sibling issue: the 17 GB ↔ 445 GB swings **are** KV caches inflating and being reclaimed. That issue described the symptom; this is the mechanism.
- **The line logged two minutes before the 04:08 panic:**
  ```
  Preloader: skipping qwen3-coder:30b — need 19GB but only 8GB free
  ```
  The gate was reasoning about a **19 GB** model that was really consuming **122 GB**.

This **supersedes** the "reserve the committed set" theory in the sibling issue — that's real but second-order next to a 5.4× sizing error.

## The data and the lever both already existed, unused

- **Data:** `LoadedModel` carries **both** `size_gb` (real resident) **and** `context_length`. Every heartbeat was a free `(weights, ctx, resident)` observation, discarded. One observation per model yields that model's `kv_per_token`.
- **Lever:** `FLEET_DYNAMIC_NUM_CTX` + `num_ctx_overrides` already let the router pick `num_ctx` on cold loads. **Context is a memory dial the herd already owns** — capping qwen3-coder:30b at 32K instead of 262K saves 92 GB on one model.

## Implemented 2026-07-17

1. **`_observe_kv_cost()`** learns `kv_per_token` per model from heartbeat data: `(resident − weights) / context_length`, from any node that has the model loaded. Cheap, no new plumbing — the numbers were already arriving. It also records each model's **observed default context**, which is what it runs at when nobody overrides `num_ctx` (qwen3-coder:30b defaults to 262144, and that default *is* the bug).
2. **`measured_resident_gb(model, node, num_ctx)`** answers **only from evidence**:
   - the model's **actual resident size** if it's loaded (ground truth, no guessing);
   - else **weights + learned `kv_per_token` × num_ctx**;
   - else **`None`** — it does not manufacture evidence it doesn't have.
3. **`estimate_resident_gb(...)`** wraps it for callers that need a number, falling back to the previous `weights × 1.2`. An un-observed model is therefore sized exactly as it was before, so this change cannot regress it.
4. **All three fit decisions now use the one estimator** — the preloader's memory gate, `/fleet/pin` admission, and the scorer. `_PIN_RESIDENT_OVERHEAD` is gone; it was a third private copy of the same `1.2`, and a private copy of this arithmetic is *precisely* what caused the 04:08 panic.
5. **`pre_warm()` now sends the same `num_ctx` the streaming path injects.** It didn't before: the preloader warmed a model at its own default and the first real request reloaded it at the override — churn, and it made "what will this cost?" unanswerable, since the answer depends entirely on a context we weren't controlling at load time.
6. Learned costs are logged once per model, so the values are auditable rather than magic.

**Deliberately incremental:** prediction only engages where a learned observation exists. In the **scorer** the fallback is plain weights, *not* `× 1.2` — inflating an unmeasured guess on the request path would eliminate nodes that legitimately fit today (a 70B on a 64GB box) to defend against a case we have no evidence for. Evidence tightens these gates; guesswork doesn't. Given this box kernel-panicked twice the same day, a strictly-better change beat a bigger, riskier one.

## Still open

- **Reserve the committed set** — pinned models that aren't resident yet still look like free memory. See the sibling issue.
- **Cold, never-observed large models** remain the risk: with no observation there's no `kv_per_token`, so the old approximation applies. `qwen3-coder:480b` and `deepseek-v3:671b` are the live examples — **they do not fit this fleet and should not be requested on it.** The learning is also per-`(model)`, not per-quantisation-per-node; a model observed on one node is assumed to cost the same per token on another.
- **num_ctx as a fitting strategy:** with correct sizing, *"doesn't fit at 262K, fits at 32K"* becomes a decision the router could make, instead of refusing or crashing. The gate now *reads* the override; it still can't *choose* one.
- **The learned cost is in-memory only** — it resets on router restart, so the first load after a restart falls back to the approximation until a heartbeat re-teaches it. Persisting it alongside the trace DB would close that window.

## Test gap this exposed

`_refresh_priority_models` — the loop that keeps pinned models hot — was monkeypatched away by **every** test that touched it, so an undefined-name bug in it survived a fully green 1112-test suite and was caught by `ruff`, not pytest. Now covered by `test_refresh_reloads_evicted_pinned_model`. Worth remembering: stubbing the function under test is not coverage.

## Operationally, until the above lands

Resident cost is **not** predictable from `ollama list` / `/api/tags` sizes. Anyone sizing this fleet — benchmarks included — must read **`ollama ps` with the CONTEXT column**, not on-disk weights.
