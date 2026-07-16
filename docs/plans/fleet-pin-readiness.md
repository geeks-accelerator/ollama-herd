# `/fleet/pin` Readiness — Block Until the Model Actually Serves

**Status**: 📋 Planned — not yet implemented. Codebase audit done 2026-07-16 (see **Audit findings**, which supersede the initial framing where they conflict).
**Created**: 2026-07-16
**Source**: A benchmark scanner's smoke test caught it. After `POST /fleet/pin` returned `{"ok": true}`, the scan fired and saw `served=5 fallback=5` — every early request fell back to another model. The pin registered, but routing didn't yet see the model as resident, and with global VRAM fallback ON the scan raced into the gap. The scanner fixed it client-side with a readiness loop that polls until `x-fleet-fallback: false` before scanning. This plan moves that guarantee **server-side** so no client has to rebuild the loop.
**Follows**: [client-ergonomics-from-agent-feedback.md](client-ergonomics-from-agent-feedback.md) #4 (the pin API this hardens)

---

## Why this matters

`POST /fleet/pin` was shipped in 0.8.1 as the one-call replacement for the manual `curl :11434 keep_alive` dance. Its return contract is **"we tried," not "it's ready."** A caller that pins-then-immediately-uses races the router's view of residency.

This is the same class of bug the whole client-ergonomics batch is about: **the herd's behavior isn't legible to the caller.** `{"ok": true}` reads as "pinned and serving," but currently means "warm-up dispatched and (usually) pin row written." The gap between those meanings is where the scanner lost a benchmark run. The fix is a non-breaking, opt-in `wait` mode that returns only once the model is **confirmed resident** — turning `{"ok": true, "ready": true}` into a real guarantee.

---

## Audit findings (2026-07-16) — ground truth, overrides the initial framing

A pass over the pin path ([fleet.py:85](../../src/fleet_manager/server/routes/fleet.py), [model_preloader.py:117](../../src/fleet_manager/server/model_preloader.py), [streaming.py:646](../../src/fleet_manager/server/streaming.py), [scorer.py](../../src/fleet_manager/server/scorer.py)) changed four assumptions. **These govern the design below.**

### 1. The race is heartbeat-reflection lag, NOT load time

`pre_warm()` ([streaming.py:646](../../src/fleet_manager/server/streaming.py)) POSTs Ollama `/api/generate` with `prompt:""`, `keep_alive:-1`, `timeout=120.0`. Ollama **blocks that call until the model is loaded**, then returns. So `/fleet/pin` already waits out the actual load (up to 120s) synchronously inside `pre_warm`.

The remaining race is purely that **routing reads residency from the registry** — `node.ollama.models_loaded`, refreshed only on the node's **heartbeat** (`heartbeat_interval = 5.0s`, [config.py:14](../../src/fleet_manager/models/config.py)). Between "Ollama finished loading" and "next heartbeat lands," the scorer ([scorer.py:198,226,245](../../src/fleet_manager/server/scorer.py)) still sees the model as non-resident and, with fallback ON, substitutes. **That ≤5s window is the entire bug.** Consequence: the readiness wait is *short and bounded by heartbeat cadence* — not a long load wait — so the timeout default can be modest.

### 2. Gap 2 has a second victim — the pin isn't even persisted

The pin-persistence step ([fleet.py:124-130](../../src/fleet_manager/server/routes/fleet.py)) resolves the node to persist by scanning `models_loaded` for the model — the **same heartbeat-lagged signal**:

```python
pin_node = node_id
if pin_node is None:
    for n in registry.get_all_nodes():
        if n.ollama and any(m.name == model for m in n.ollama.models_loaded):
            pin_node = n.node_id
            break
per_node = store.set_pin(pin_node, model, True) if pin_node else store.load()  # None → NOT persisted
```

When `node_id` is omitted and the heartbeat hasn't caught up, `pin_node` is `None` → `store.set_pin` is **skipped** → the pin silently isn't persisted (so the preloader won't reload it after eviction). The readiness wait fixes this for free: **wait for residency → then resolve `pin_node` → then persist.** (When `node_id` *is* supplied, there's no resolution race — `pin_node` is known immediately.)

### 3. `_load_model_on_best_node`'s bool return must not change shape

It has **five callers**: the pin route plus four inside the preloader ([model_preloader.py:246,275,358,400](../../src/fleet_manager/server/model_preloader.py)), all in pure `if await _load_model_on_best_node(...):` boolean context ("did we load one, so count it / sleep / refresh"). Returning a tuple would break them — **a non-empty tuple is always truthy**, so `(False, …)` reads as success. A `__bool__` dataclass would work but adds a subtle footgun.

**Therefore: don't change its signature.** Get honesty from the readiness poll instead (below). This deletes the riskiest part of the original plan (the `warmed` plumbing through 5 callers). `warmed` becomes an *optional* additive later — see design.

### 4. The dashboard pin path is persist-only — do NOT mirror warming into it

`POST /dashboard/api/pinned-models` ([dashboard.py:1407](../../src/fleet_manager/server/routes/dashboard.py)) only calls `store.set_pin()` — it does **not** warm; the preloader's 10-min refresh cycle warms pinned-but-cold models later. That's deliberate (dashboard toggles are declarative; the loop reconciles). So the original "mirror readiness into the dashboard path" step is dropped — the two endpoints are *intentionally* different (`/fleet/pin` = warm-now imperative; dashboard = declarative). No change there.

### Reuse inventory (leverage, don't reinvent)

- **Deadline-poll idiom** already exists twice — `_wait_port_free` / `_wait_healthy` ([mlx_supervisor.py:578,592](../../src/fleet_manager/node/mlx_supervisor.py)):
  ```python
  deadline = asyncio.get_running_loop().time() + timeout
  while asyncio.get_running_loop().time() < deadline:
      if <ready>: return True
      await asyncio.sleep(_POLL_INTERVAL)
  return False
  ```
  `_wait_until_resident` mirrors this exactly (monotonic loop clock, module-level poll-interval constant). No new pattern introduced.
- **Residency check** already exists: `_model_is_loaded_anywhere(model, nodes)` ([model_preloader.py:104](../../src/fleet_manager/server/model_preloader.py)) reads `n.ollama.models_loaded`. We need a **per-node** variant (pin targets one node) — a 3-line `_model_resident_on_node(model, node)` beside it, reused by the poll. Do not duplicate the field access inline.
- **MLX detection**: `is_mlx_model()` ([mlx_proxy.py:68](../../src/fleet_manager/server/mlx_proxy.py)). MLX residency = `node.mlx_servers[].status == "healthy"` ([node.py:301](../../src/fleet_manager/models/node.py) — `NodeState.mlx_servers`).
- **Registry accessors**: `get_node(node_id)`, `get_online_nodes()` ([registry.py:121,124](../../src/fleet_manager/server/registry.py)) — the poll targets `get_node(pin_node)` for a precise per-node check.
- **The `sleep(2)`-then-refresh in the preloader** ([model_preloader.py:250,278,363,404](../../src/fleet_manager/server/model_preloader.py)) is the crude ancestor of this readiness wait (fixed 2s guess vs. poll-until-true). A future consolidation could point those at `_wait_until_resident`; **out of scope here** to avoid churning the preloader — noted so we don't fork the concept.

---

## The three gaps (as refined by the audit)

1. **Pin reports success it can't guarantee** — `{"ok": true}` means warm-up dispatched, not serving. Fixed by the readiness wait (`ready`), not by changing return signatures (finding #3).
2. **Routing lags residency by one heartbeat (~5s)** — the scorer gates on `models_loaded`, which updates on the 5s heartbeat, so a pinned-and-loaded model still falls back briefly. Fixed by polling that exact signal until it flips. *Also* silently drops the pin persistence when `node_id` is omitted (finding #2) — same fix.
3. **`mlx:` models aren't understood** — `pre_warm` would 404 against Ollama (swallowed), and MLX models can't be loaded on demand anyway (configured via `FLEET_NODE_MLX_SERVERS`). Fixed by short-circuiting: report readiness from `mlx_servers` health, never warm.

---

## Design

Non-breaking, opt-in. Default (`wait` absent/false) is byte-identical fire-and-return.

### Request

```jsonc
POST /fleet/pin
{
  "model": "qwen3-coder:30b",
  "node_id": "…",       // optional, unchanged
  "wait": true,          // NEW — block until confirmed resident (default false)
  "timeout_s": 30        // NEW — cap the wait (default 30; only used when wait=true)
}
```

`timeout_s` default is **30**, not 60: the load already completed inside `pre_warm`, so the wait is just heartbeat lag (≤ a few `heartbeat_interval`s). 30s is generous headroom over the 5s cadence.

### Response (readiness is the honesty mechanism)

```jsonc
{
  "ok": true,              // pin accepted
  "model": "qwen3-coder:30b",
  "pinned_node": "…",      // now reliably resolved (finding #2)
  "ready": true,           // NEW — confirmed resident; only present when wait=true
  "ready_after_ms": 2140,  // NEW — observability; only present when wait=true
  "per_node": { … }
}
```

- `wait=false`: omit `ready`/`ready_after_ms`; behavior unchanged.
- `wait=true`: poll until resident or `timeout_s`. `ready:true` is the guarantee the scanner wanted; on timeout `ready:false` (client learns the truth instead of racing). If pre_warm silently failed, residency never flips → `ready:false` surfaces it — **which is why we don't need to plumb `warmed` through 5 callers.**

### Readiness = the signal the scorer gates on

`_wait_until_resident(registry, node_id, model, timeout_s)` polls `get_node(node_id)` until `_model_resident_on_node(model, node)` is true (Ollama `models_loaded` **or** MLX `mlx_servers[].status=="healthy"`), else timeout. Returns `(ready: bool, elapsed_ms: int)`. Because it watches the *same* `models_loaded` the scorer reads, "resident" here ⇒ the scorer won't fall back. Poll interval ~1s.

### `mlx:` short-circuit

If `is_mlx_model(model)`: skip `pre_warm` entirely (MLX isn't Ollama-loadable), resolve readiness from `mlx_servers` health, and return with a `note` that MLX models are always resident / configured via env. Pin persistence is a no-op for them.

### Ordering that fixes finding #2

`pre_warm` → **(if wait) `_wait_until_resident`** → resolve `pin_node` from now-current registry → `store.set_pin`. The wait moves *before* node resolution so persistence stops racing the heartbeat.

---

## Implementation steps (minimal-debt)

1. **`_model_resident_on_node(model, node) -> bool`** beside `_model_is_loaded_anywhere` ([model_preloader.py:104](../../src/fleet_manager/server/model_preloader.py)) — Ollama `models_loaded` OR MLX `mlx_servers` healthy. (Refactor `_model_is_loaded_anywhere` to `any(_model_resident_on_node(model, n) for n in nodes)` so there's one residency definition.)
2. **`_wait_until_resident(registry, node_id, model, timeout_s) -> (bool, int)`** — mirror the `_wait_healthy` deadline-poll idiom; module-level `_RESIDENCY_POLL_INTERVAL = 1.0`. Lives in `model_preloader.py` (next to the helper it uses).
3. **`/fleet/pin`** ([fleet.py:85](../../src/fleet_manager/server/routes/fleet.py)): parse `wait`/`timeout_s`; `is_mlx_model` short-circuit; when `wait`, call step 2 **before** resolving `pin_node`; populate `ready`/`ready_after_ms`. No change to `_load_model_on_best_node`.
4. *(Optional, additive, zero-breakage)* **`pre_warm -> bool`** (True on 200) so a future `warmed` field is cheap. Its two other callers (`rebalancer._do_pre_warm`, `model_preloader:160`) ignore the return, so this can't break anything. **Not required for v1** — the readiness poll already provides honesty. Include only if a `wait=false` caller asks for warm-confirmation.
5. **Docs**: `docs/api-reference.md` (pin `wait` contract + response schema), CLAUDE.md Current State, CHANGELOG under the unpublished 0.8.1 (folds into the client-ergonomics batch).

Explicitly **not** doing: touching the dashboard pin path (finding #4), changing `_load_model_on_best_node`'s signature (finding #3), or repointing the preloader's `sleep(2)` sites (out-of-scope consolidation).

---

## Testing

- `_model_resident_on_node`: true for an Ollama model in `models_loaded`; true for a healthy `mlx_servers` entry; false for a `starting`/`unhealthy` MLX entry; false when absent.
- `_wait_until_resident`: returns `(True, ms)` once a mocked registry flips `models_loaded` mid-poll; `(False, ms)` on timeout when it never flips. Drive registry state directly; cap iterations / inject the clock — **no real sleeps** beyond the tiny interval.
- `is_mlx_model` pin short-circuits to `ready:true` (healthy server) / `ready:false` (no healthy server) **without calling Ollama `pre_warm`** — assert the node HTTP client is never hit.
- Route: `wait=true` includes `ready` + `ready_after_ms`; `wait=false` omits them and returns without polling.
- Regression for finding #2: with `node_id` omitted and residency reflected only after the wait, the pin **is** persisted (`store.set_pin` called with the resolved node).

---

## What this does NOT change

- Default `/fleet/pin` behavior when `wait` is absent — byte-identical fire-and-return.
- The scorer, fallback decision, routing, `_load_model_on_best_node`, and the dashboard pin path — untouched. Readiness only *observes* the residency the router already tracks; it never forces a route.
- The scanner keeps its client-side readiness loop as belt-and-suspenders — complementary, not redundant.

---

## Open questions (mostly answered by the audit)

- **Timeout default**: settled at **30s** — the wait is heartbeat-lag (≤ a few × 5s), not load time, since `pre_warm` already blocked through the load. Revisit only if `ready_after_ms` telemetry shows a long tail.
- **Faster residency signal?** Readiness can't beat the heartbeat cadence (5s) because `models_loaded` only updates then. A push/event on load-complete would cut it, but that's a larger heartbeat-protocol change — not worth it for a ≤5s win. Log `ready_after_ms` first; optimize only if the data justifies it.
- **Tier-3 end-to-end serve check** (assert `X-Fleet-Fallback:false` via a real request): deliberately out of scope — registry residency is the *same* signal the scorer gates on, so it's sufficient by construction. Add only if residency-true-but-still-falling-back ever shows up in practice.
