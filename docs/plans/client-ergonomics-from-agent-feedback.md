# Client Ergonomics — Feedback from a Benchmark Agent

**Status**: Implemented 2026-07-15 — #1, #2, #3a, #4, #5 done (batches 1–2, tested + committed). **#3b (per-client concurrency cap) NOT done** — needs `client_ip` plumbed through routes → `QueueEntry` → `QueueManager` + a shed-to-429 path; deferred so it isn't shipped half-tested (concurrency code). Changes are in `[Unreleased]`; version/release timing (0.8.1 vs 0.9.0) decided at publish time.
**Created**: 2026-07-15
**Source**: An AI agent tried to benchmark specific models through the herd and lost hours to (a) silent model substitution and (b) queue saturation. Its five recommendations are captured below with an engineering triage against what already exists.

---

## Why this matters

The herd's routing intelligence (VRAM-aware fallback, retries, scoring) is tuned for **production resilience** — keep Claude Code working even when a model gets evicted. But those same behaviors are **hostile to a client that needs determinism** (a benchmark/eval that must run the *exact* model, or explicitly fail). The agent's core pain: the herd substituted gemma3/gpt-oss for a `qwen3-coder:30b` request and the response looked identical; the truth lived only in the trace DB. Every fix below is about **making the herd's behavior legible and controllable to the caller**.

Recurring theme: **global config is a blunt instrument on shared infra.** Setting `FLEET_VRAM_FALLBACK=false` to unblock one benchmark changed behavior for the live soak and every other client. The right primitives are **per-request** and **observable**.

---

## Codebase audit — reuse these, kill this debt

A pass over the routing/streaming/fleet code (2026-07-15) found that most of what these items need **already exists** but is applied inconsistently. Greenfield rule for this work: **consolidate the existing duplication rather than add parallel paths.** These findings override the per-item "Work" notes where they conflict.

### The served model is already computed — the bug is emission, not tracking

`score_with_fallbacks()` ([routing.py:65](../../src/fleet_manager/server/routes/routing.py)) is the **single** place the VRAM fallback is decided (`vram_fallback` read once at line 89) and it returns `(results, actual_model)` — the model that actually served. `openai_compat` then stores it: `inference_req.model = actual_model` (line 132). So there is already **one source of truth** for the served model.

The defect is that response-`model` emission and header-setting **don't consistently read it**:
- Response `model` is emitted in ≥5 spots (`openai_compat:203` uses `actual_model` ✅, but `streaming.py:339/1282/1292` emit `entry.request.model`/`model` — and whether that equals the served model depends on path/ordering).
- **`X-Fleet-*` headers are copy-pasted across 6+ route files** (`openai_compat`, `ollama_compat`, `anthropic_compat`, `image_compat`, `transcription_compat`, `embedding_compat`, `text_embedding_compat`) with **divergent contents** — some set `X-Fleet-Fallback`, some `X-Fleet-Model` vs `X-Fleet-Node`, some `X-Fleet-Backend`, and `X-Fleet-Fallback` is only set *when* a fallback happened.

**Consequence for #1**: this is lower-effort and higher-payoff than first drafted. The work is (a) a **single shared `fleet_headers()` builder** (new `server/fleet_headers.py` or a helper in `routing.py`) that every route + the streaming response path calls, replacing all 6+ copy-pasted blocks; and (b) making every response-`model` emitter read the already-tracked served model. This delivers the feature **and** deletes existing duplication in one move. Emit `X-Fleet-Fallback: true|false` always (not only on fallback), plus `X-Fleet-Served-Model` + `X-Fleet-Node` unconditionally.

### Each behavioral change threads into exactly one function

- **#2 (per-request strict mode)** → `score_with_fallbacks()` is the sole decision point. Add an `allow_fallback: bool | None = None` parameter that, when not `None`, overrides the global `vram_fallback`. Parse the per-request signal (`X-Fleet-No-Fallback` header / `fallback` body field) once in the route and pass it in. No second routing path.
- **#3a (don't amplify)** → `_is_retryable_error()` ([streaming.py:359](../../src/fleet_manager/server/streaming.py)) is the sole retry classifier; today it retries **all** `status_code >= 500`. Refine it to treat Ollama's queue-full 503 (`"maximum pending requests exceeded"` in the body) as **non-retryable**, and surface it upstream as `429` + `Retry-After`. One function.

### #4 and #5 must reuse, not reimplement

- **#4 (pin API)**: `PinnedModelsStore` ([pinned_models.py](../../src/fleet_manager/server/pinned_models.py)) + `model_preloader._load_model_on_best_node()` already do the evict-and-warm + persistence, and `/dashboard/api/pinned-models` (GET/POST) already drives them. `POST /fleet/pin` must **wrap this same service** — ideally the dashboard endpoint and `/fleet/pin` become thin wrappers over one pin handler, not two implementations.
- **#5 (fleet read API)**: `/fleet/status` ([fleet.py:24](../../src/fleet_manager/server/routes/fleet.py)) and the dashboard ([dashboard.py:143](../../src/fleet_manager/server/routes/dashboard.py)) **hand-build the same node dict** (node_id / status / hardware / cpu / memory / ollama…) — there is **no shared serializer**. Extract one `serialize_node(node, queue_mgr)` used by both, then add the new fields (`free_slots`, per-model `queue_depth`, `tokens_per_sec`) **once**. `/fleet/limits` is a small genuinely-new endpoint.

### Greenfield / no-gating notes

- These are **default behaviors, not flags**: consistent served-model + headers (#1) and non-retry of queue-full (#3a) just become how the herd behaves. The only per-request *control* is #2's strict-mode override, which is a per-call parameter, not a feature gate.
- Once #2 lands, **revert the global `FLEET_VRAM_FALLBACK=false`** we set on 2026-07-15 back to the default (`true`); the benchmark uses the per-request header instead, so production keeps its safety default.
- The 6+ duplicated header blocks and the 2 duplicated node serializers are **pre-existing debt**; this work is the natural occasion to collapse each to one — do that rather than adding a 7th header block or a 3rd serializer.

---

## The five items, triaged

### 1. Make substitution visible — served model + headers ⭐ ship first

**Ask**: return the *served* model in the response `model` field (per the OpenAI contract), and always emit `x-fleet-served-model` / `x-fleet-fallback` / `x-fleet-node` headers.

**What exists**: `X-Fleet-Fallback`/`X-Fleet-Node`/`X-Fleet-Score`/`X-Fleet-Retries` are set on the OpenAI non-stream path ([openai_compat.py:154-160](../../src/fleet_manager/server/routes/openai_compat.py)) — but **conditionally** (fallback header only when a fallback happened) and **inconsistently**: the non-stream body uses `actual_model` while the **streaming** body echoes `entry.request.model` ([streaming.py:339](../../src/fleet_manager/server/streaming.py)). Ollama-compat and other routes vary too.

**Work**: make it uniform and always-on across streaming + non-streaming, OpenAI + Ollama + Anthropic routes, via the single `fleet_headers()` builder. The response `model` = served model everywhere; every proxied inference response emits the **same canonical header set** (not the current grab-bag where routes emit different subsets).

**Canonical header contract** (what `fleet_headers()` emits on every inference response — this is the "consistent contents" fix):

| Header | Always? | Value | Fixes today's inconsistency |
|---|---|---|---|
| `X-Fleet-Node` | ✅ | node id that served | already common; now guaranteed on all routes |
| `X-Fleet-Served-Model` | ✅ | the model that actually ran (`actual_model`) | **renames/replaces** the scattered `X-Fleet-Model` (image/transcription/embedding) so there's ONE name for "what ran" |
| `X-Fleet-Requested-Model` | ✅ | what the client asked for | new — lets a client diff requested vs served in one place |
| `X-Fleet-Fallback` | ✅ | `true` / `false` | **semantic fix**: today it's set to a *model name* and only *when* a fallback happened → now a plain boolean, always present |
| `X-Fleet-Backend` | ✅ | `ollama` / `mlx` / `native` / `vision` | today only the anthropic-mlx path sets it → now universal |
| `X-Fleet-Retries` | ✅ | int (`0` if none) | today conditional (`>0` only) → always present |
| `X-Fleet-Score` | when scorer-routed | int | omit only on direct-proxy paths (e.g. embeddings) that don't score |

`X-Fleet-Model` is **retired** in favor of `X-Fleet-Served-Model` (don't keep both — greenfield, one name). The old `X-Fleet-Fallback = <model name>` semantics are replaced by the boolean above; `X-Fleet-Served-Model` already carries the name. Nothing internal consumes the old header (`benchmark_engine` reads only `x-fleet-node`), so the contract change is safe — changelog it.

**Value / effort**: highest value, low–medium effort. Correctness fix (OpenAI says response.model is what ran) **plus** it collapses 6+ divergent header blocks into one contract. Note: a naive client asserting `response.model == request.model` now sees the served name — that's the point; call it out in the changelog.

### 2. Per-request strict mode (not a global flag)

**Ask**: `x-fleet-no-fallback: true` header (or `fallback: false` body param) so one caller can demand the exact model or get an explicit error, while everyone else keeps production fallback.

**What exists**: only the global `vram_fallback` toggle (settings + `/dashboard/api/settings`), read in [routing.py:89](../../src/fleet_manager/server/routes/routing.py).

**Work**: parse a per-request override (header/body) and thread it into the fallback decision in `routing.py`; on a strict miss, return a clear `409`/`422` ("model X not available, no fallback requested") instead of substituting. Retire the need to flip the global flag for benchmarks.

**Value / effort**: high value, medium effort. This is the *right* fix for what we hacked with the global flag today.

### 3. Don't amplify saturation

**Ask**: (a) a 503 that means "Ollama pending-queue full" is **not transient** — don't retry it; return `429` + `Retry-After`. (b) per-client / per-model concurrency cap so one caller can't self-DoS.

**What exists**: `_is_retryable_error` + `_stream_with_retry` (up to `max_retries`, default 2) in [streaming.py:359-394](../../src/fleet_manager/server/streaming.py). Today a queue-full 503 IS retried, turning a flood into a bigger flood (observed 2026-07-15: 156 `glm-4.7-flash` 503s in 5 min under a benchmark flood). QueueManager already has per-`node:model` queues with dynamic concurrency.

**Work**: (3a) in `_is_retryable_error`, classify Ollama's `"maximum pending requests exceeded"` 503 as **non-retryable**; surface it as `429` + `Retry-After` upstream. Small, contained, high value. (3b) add a per-client (IP/key) and/or per-model in-flight cap with shed-to-429 — larger; builds on QueueManager.

**Value / effort**: 3a = high value, low effort (do with #1/#2). 3b = high value, larger effort.

### 4. First-class model-management API

**Ask**: `POST /fleet/pin {model, ttl}` (evict LRU → cold-load → pin → return when resident), `DELETE /fleet/pin/{model}`; and on a cold miss return `202 "loading, retry in Ns"` instead of a silent substitute.

**What exists**: pin logic lives under `/dashboard/api/pinned-models` (GET/POST, [dashboard.py:1385-1407](../../src/fleet_manager/server/routes/dashboard.py)) and `model_preloader._load_model_on_best_node()` already does evict-and-warm. So the machinery exists; it just isn't exposed as a clean, synchronous fleet API.

**Work**: promote a `POST /fleet/pin` / `DELETE /fleet/pin/{model}` that wraps the existing preloader path and blocks until healthy (or returns 202 with a poll token). Replaces the manual `curl :11434 keep_alive` dance.

**Value / effort**: high value, medium–high effort (mostly wrapping + lifecycle/TTL).

### 5. Clean read API for fleet state

**Ask**: `GET /fleet/status` → nodes, loaded models per node, free slots, queue depth, tok/s; `GET /fleet/limits` → effective constraints (slot count, `NUM_PARALLEL`) so a client can auto-serialize instead of self-DoSing.

**What exists**: `/fleet/status` (nodes + loaded models) and `/fleet/queue` already exist ([fleet.py:13,83](../../src/fleet_manager/server/routes/fleet.py)) — the agent hit 404s guessing other paths, so **part of this is discoverability**, not missing endpoints. `/fleet/limits` is genuinely new; `free_slots` / per-model `queue_depth` / `tokens_per_sec` are new fields on `/fleet/status`.

**Work**: (a) document the existing `/fleet/*` endpoints where clients look (README/api-reference); (b) enrich `/fleet/status` with `free_slots`, `queue_depth`, recent `tokens_per_sec`; (c) add `GET /fleet/limits` (slot cap, `OLLAMA_NUM_PARALLEL`, per-model concurrency) so clients self-throttle.

**Value / effort**: medium value, low–medium effort (+ a docs fix).

---

## Recommended sequence

By value-to-effort, and grouping changes that touch the same code:

1. **#1 served-model consistency + headers** — correctness; unblocks *every* client's ability to detect substitution for free.
2. **#3a don't-retry queue-full 503 → 429 + Retry-After** — stops flood amplification; tiny, same streaming file.
3. **#2 per-request strict mode** — the correct replacement for the global `vram_fallback` flag we toggled today.
4. **#5 `/fleet/limits` + enrich `/fleet/status` + document existing endpoints** — cheap; lets clients auto-serialize.
5. **#4 `POST /fleet/pin` API** — wraps existing preloader; the "get model X ready" one-call ergonomics.
6. **#3b per-client / per-model concurrency cap** — largest; backstops self-DoS.

Items 1–3 are a natural first PR (all in `openai_compat.py` / `streaming.py` / `routing.py` + the new shared `fleet_headers()` helper). 4–5 are a second (new `/fleet/*` surface, built on the extracted `serialize_node()` + existing `PinnedModelsStore`). 6 is its own.

**Two consolidations do most of the work and pay down existing debt:**
- `fleet_headers()` — one builder replacing 6+ copy-pasted `X-Fleet-*` blocks (unlocks #1, and every route gets consistent headers for free).
- `serialize_node()` — one node serializer replacing the `/fleet/status`-vs-dashboard duplication (unlocks #5's new fields in one place).

Build these two first within their PRs; the features hang off them.

---

## Timing

Hold implementation until **0.8.0 publishes** — it's mid-soak, and adding features now would reset the soak clock. These land as 0.8.1 / 0.9.0. The other agent's immediate need is covered by the current workaround (pre-warm the model + run serial + read `X-Fleet-Fallback` / the trace DB).

## Non-goals / cautions

- **#1 behavior change**: returning the served model in `response.model` is correct but visible — changelog it, since some clients assert equality with the request.
- Keep production defaults unchanged: fallback + retry stay ON by default; the new controls are **opt-in per request**, so Claude Code's resilience is untouched.
