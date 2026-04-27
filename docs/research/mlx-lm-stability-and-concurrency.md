# MLX-LM stability and concurrency — research findings

**Created:** 2026-04-27
**Status:** Complete. Two recommendations land in a follow-up commit.
**Plan:** [`docs/plans/mlx-stability-and-concurrency-research.md`](../plans/mlx-stability-and-concurrency-research.md)

---

## Phase 1 — Version stability

### Question

Is `mlx-lm v0.31.3` (our current pin in `setup-mlx.sh`) the right version, given that it's also where we hit [#1208 (`load_default → snapshot_download → thread_map`)](https://github.com/ml-explore/mlx-lm/issues/1208)?

### Method

Audited mlx-lm GitHub issues for thread / load_default / snapshot_download / shutdown / crash terms. Pulled the v0.31.2 → v0.31.3 commit range. Downloaded `server.py` from each release and counted occurrences of `load_default`.

### Findings

**`load_default` was introduced in v0.31.3.** Source-line counts:

| Version | Lines in `server.py` | `load_default` occurrences |
|---|---|---|
| v0.30.7 | 1852 | 0 |
| v0.31.0 | 2004 | 0 |
| v0.31.2 | 1891 | 0 |
| **v0.31.3** | **1904** | **2** |

The introducing commit is `ed1fca4c` — [PR #1090: "Thread local generation stream"](https://github.com/ml-explore/mlx-lm/pull/1090). PR description: *"Refactors the model provider to load the default model in the generation thread."* So `load_default()` was added to support running model loading inside the generation thread instead of the main thread — itself a fix for [#1181 (`There is no Stream(gpu, 0) in current thread`)](https://github.com/ml-explore/mlx-lm/issues/1181), which was a regression introduced when mlx 0.31.2 made streams thread-local.

**Each candidate version has its own showstopper:**

| Version | #1208 (`load_default` race) | #1181 (Stream thread-local) | #1166 (Qwen3-Next concurrent shape mismatch) | Verdict |
|---|---|---|---|---|
| v0.31.0 | NO | YES | YES | Worst |
| v0.31.1 | NO | YES | YES | Worst |
| v0.31.2 | NO | YES | YES | Bad |
| **v0.31.3** | **YES** | **NO** | **NO** | **Best of bad options** |

**#1166 is particularly damning for downgrade.** That issue is *literally* about Qwen3-Next family models (we run `Qwen3-Coder-Next-4bit`) and crashes specifically when "clients send concurrent chat-completion requests that mix streaming and non-streaming mode within a short window." Claude Code regularly mixes both. v0.31.2 would silently corrupt requests under our exact workload.

**#1181 affects every server invocation, not just Qwen3-Next.** Pre-v0.31.3, the generation thread crashes on the first request with `RuntimeError: There is no Stream(gpu, 0) in current thread`. Categorical breakage.

**v0.31.3's #1208 bug is contained by our quarantine guard.** When a request triggers it, the supervisor detects the crash, enters quarantine after 5 events in 5 minutes, switches to 10-minute restart cadence, and surfaces a CRITICAL health-check recommendation. The bug is annoying but not catastrophic.

### Recommendation: STAY ON v0.31.3

Downgrading would trade one *contained* bug for two *uncontained* ones. The current pin is correct.

**No code change required.** `setup-mlx.sh`'s `PINNED_VERSION=0.31.3` stays.

**Side effect:** This is also a CHANGELOG-worthy reassurance for operators who saw #1208 and wondered if a downgrade was the answer. It isn't.

---

## Phase 2 — Concurrency model

### Question

Our `MlxProxy._acquire_slot(model_key)` uses `asyncio.Semaphore(1)` to enforce 1 in-flight request per model. Is that consistent with what `mlx_lm.server` can actually handle?

### Method

Read the source for the request handler + dispatch path. Then ran a live concurrent-request test against the idle 30B compactor on `:11441` (small enough to be fast, isolated enough not to disrupt real coding sessions).

### Source-read findings

The architecture is a **queue-dispatched single generation thread**:

1. `APIHandler(BaseHTTPRequestHandler)` — Python's stdlib base class that already spawns one HTTP worker thread per incoming request.
2. `do_POST()` runs on each HTTP thread, parses the body, and calls `self.requests.put((response_queue, request, args))` to enqueue work.
3. `_generate()` runs on **one long-lived generation thread** (`Thread(target=self._generate)`, started in `__init__`). It pops from `self.requests` and runs inference.
4. The generation thread can **batch multiple in-flight requests in one inference pass** via `BatchGenerator`, conditional on:
   - `args.seed is None` (no reproducible-sampling request), AND
   - `model_provider.is_batchable` is True (depends on the model — `is_batchable = draft_model is None and all(hasattr(c, "merge") for c in make_prompt_cache(model))`)

Concretely: when no draft model is configured (we don't use one) and the model's prompt cache supports `merge` (Qwen3-Coder-Next does), the server can batch concurrent requests in a single generation pass.

### Live test

3 concurrent tiny requests against the 30B compactor (after warmup so the model was hot):

```
Wall time for 3 concurrent: 0.55s
  req 1: HTTP 200 in 0.37s
  req 2: HTTP 200 in 0.19s
  req 3: HTTP 200 in 0.55s
  Sum of individual:  1.10s
  Max of individual:  0.55s
  → BATCHING: wall (0.55s) ≪ sum (1.10s)
```

If requests serialized, wall would be ≈ sum (~1.1s). It was ≈ max (~0.55s). **The server batched all three.**

### Implication for our admission control

`Semaphore(1)` per `model_key` is **strictly more restrictive** than what mlx_lm.server can handle. Real workloads with 2+ concurrent agents (multiple Claude Code sessions, parallel tool calls, simultaneous compaction + main inference) hit our admission gate sequentially when they could be batching.

### What we should NOT do

- **Don't remove the gate entirely.** Concurrent-request bugs have been historically rich in mlx_lm.server (#965, #983, #1097, #1166). Many were fixed in v0.31.3, but the pattern of "concurrent path is the bug magnet" is real.
- **Don't crank to a high default.** Memory scales with concurrent KV caches. Two 100K-token prefills running simultaneously = 2× the prompt-cache memory in flight. On a busy node this can trigger memory pressure or evict other models.
- **Don't ship without a guard.** Whatever default we pick, operators should be able to tune it without a code change.

### Recommendation: keep default `1`, add tunable env var

Introduce `FLEET_MLX_MAX_INFLIGHT_PER_MODEL` (default `1`) controlling the per-model semaphore size. Operators with bursty workloads who want to capture the batching benefit can set it to `2` or `3`. Default stays at `1` because:

1. **Provably stable.** That's been our default and the fleet runs without inference-side crashes (only the load_default issue, which is orthogonal).
2. **Memory-safe by default.** A single 100K prefill is the worst case.
3. **The throughput upside is bounded by the model's actual batching capacity.** For Qwen3-Coder-Next at long contexts, 2 concurrent requests is the realistic upper bound before memory pressure kicks in. So even `2` captures most of the gain.

**Code changes:**
- `models/config.py` — add `mlx_max_inflight_per_model: int = 1` to `ServerSettings`
- `server/mlx_proxy.py` — `_acquire_slot` reads from settings, builds `Semaphore(N)` per model with N from config
- `docs/configuration-reference.md` — document the env var with the trade-off
- Tests for the configurable size

---

## Combined summary table

| Recommendation | What changes | Risk | Estimated time |
|---|---|---|---|
| Stay on mlx-lm v0.31.3 | Nothing in code; documentation reassurance only | None — confirms current state | 0 |
| Default `_acquire_slot` to N=1, expose `FLEET_MLX_MAX_INFLIGHT_PER_MODEL` | `config.py` field, `mlx_proxy.py` semaphore read, tests, doc reference entry | Low — default unchanged, opt-in tuning | 1 hour |

---

## References

- [#1208 OPEN — `load_default → snapshot_download → thread_map` crash](https://github.com/ml-explore/mlx-lm/issues/1208) (filed by us)
- [#1181 CLOSED — `There is no Stream(gpu, 0) in current thread`](https://github.com/ml-explore/mlx-lm/issues/1181) (fixed in v0.31.3 by PR #1090)
- [#1166 CLOSED — Qwen3-Next concurrent shape mismatch](https://github.com/ml-explore/mlx-lm/issues/1166) (fixed in v0.31.3 by PR #1169)
- [#1090 PR — Thread local generation stream](https://github.com/ml-explore/mlx-lm/pull/1090) (fixed #1181, introduced #1208)
- [#1133 OPEN — Server falls through to HF lookup for short model names](https://github.com/ml-explore/mlx-lm/issues/1133) (different bug, not affecting us — we always send the full model id)
- [`docs/plans/mlx-stability-and-concurrency-research.md`](../plans/mlx-stability-and-concurrency-research.md) — the plan that triggered this research
- [`docs/observations.md`](../observations.md) — 2026-04-26 (quarantine guard) + 2026-04-27 (orphan reap), the downstream protections
- [`scripts/setup-mlx.sh`](../../scripts/setup-mlx.sh) — current pin (v0.31.3, confirmed correct)
- [`src/fleet_manager/server/mlx_proxy.py`](../../src/fleet_manager/server/mlx_proxy.py) — `_acquire_slot` admission control (becomes configurable)
