# Review findings and run 6 issues: one plan

**Status:** planned 2026-11-22, then audited against the code the same day (HEAD `b84c71f`). Nothing implemented.

**Sources:**
- **A code review of the 115 commits from 2026-11-20 to 2026-11-22**, covering 0.12.11, 0.12.12 and about 730 unreleased lines. Every finding marked *confirmed* was checked against HEAD. 2768 tests pass and `health.sh` is 5/5, so these are untested paths, not red tests.
- **The `OPEN` entries filed in `docs/issues.md` from run 6** (two 24 GB M4 Macs on EC2, 2026-11-21/22).
- **The open items in [`fleet-benchmark-findings-2026-11.md`](fleet-benchmark-findings-2026-11.md)** (13, 29, 30, 35, 39).

**Ground rules:**
- **Greenfield.** No flags and no second code path. A setting a fix makes redundant is deleted, not kept beside it.
- **Reuse before build.** Every item names what it extends.
- **One definition per quantity.** Where the audit found a quantity computed in several places that disagree, the fix is to unify them, not add one more.
- **Move forward.** Where the right fix generalises cleanly, it does: the building blocks below replace ad-hoc code that several items would otherwise each patch.

## What the audit changed

The first draft was written from the review. Reading the code corrected it in these places; each is folded into the item below.

| Item | First draft said | The code says |
|---|---|---|
| A1 | Filter exclusions before the stranding filter; auto-routing too | Exclusion belongs in `_eliminate`, the single hard filter. Auto-routing picks a model name, not a node, so it has nothing to exclude |
| A2 | Text embeddings and reranks | Also Ollama's own `/api/embed` path (`ollama_compat.py:944-1010`), which retries only `ReadTimeout` |
| A3 | Two rate sites | Six, in `build_report` alone, all dividing by `duration` |
| A5 | Classify each runner's model as forced-serial | The `llama-server` command line names only a blob (`--model …/blobs/sha256-…`), not the model. A `-np` above 1 is evidence; a 1 is ambiguous |
| A6 | One predicate shared with `first_chunk` | `first_chunk` sees translated chunks and is shared with MLX. And as written, every context-too-long would become a retryable failure across all peers |
| A7 | `aclose()` the unstarted stream | `aclose()` on an async generator that never started does not run its `finally` |
| B1 | Report Metal's limit as a GPU-memory backend | That double-counts: `gpu_total_gb` means memory *outside* RAM. Metal's working set is a cap *inside* it. It can be read without a compile step (`ctypes`) |
| B2 | Bound or cancel the pre-warm | Cancelling doesn't unblock Ollama: its scheduler keeps waiting after the client leaves. The bound that works is not sending it |
| B4 | Wait on the target worker's slot event | There's no such event. Admission is "a worker takes it from `pending`" |
| B6 | Prefill rate from the latency store | The latency store has no first-token data. `prompt_eval_duration` arrives on the done line and is discarded today |
| B7 | Reuse `load_pending_on` | That answers "another model is loading on this node". B7 needs "this model's own load is under way" |
| B9 | "16 GB ceiling at high CPU"; three bands | High CPU sets `PAUSED` (already eliminated). The bands are fractions of total memory (12.5 / 25 / 50 / 80%), four of them, already named by `CapacityMode` |

## Shared building blocks

Seven pieces, each replacing code that two or more items would otherwise patch separately. Build each one with the first item that needs it.

| # | Block | Replaces | Used by |
|---|---|---|---|
| S1 | `Scorer._prefer(results, avoid)`: drop the avoided nodes only if something is left | The inline stranding rule (`scorer.py:154`) and the −100 failing penalty | A4, B3, stranding; in `score_request` and `score_loaded_models` |
| S2 | `QueueManager.snapshot() -> QueueSnapshot` (frozen): `depths`, `loading` (keys with an in-flight entry that has `last_progress_at is None`), `free_slots`, `node_load`, `last_switch` | Eight separate `get_queue_depths()` calls (`routing.py:294,353,801,833`, `streaming.py:844`, `rebalancer.py:44,108`, `_must_wait`) and the `startswith` scan at `scorer.py:99-104` | A1, B4, B5, B7, B8, and the scorer's existing parameter (replacing the dict, not added beside it) |
| S3 | `NodeState.fit_budget()` → (total, free without evicting), the min of RAM-or-GPU, Metal's working set and the capacity ceiling; plus one `fit_cost_gb(model, node, settings)` | Three fit-cost computations that disagree: `scorer._model_fit_cost_gb` (weights fallback), `_idle_room_for` re-implementing it inline, and the preloader's `estimate_resident_gb` (weights × 1.2). Plus the ceiling applied two different ways in `memory_for_load_gb` and `memory_free_without_evicting_gb` | B1 (and through it the preloader, scorer, rebalancer, `_idle_room_for`, `has_another_home`, `_pick_pull_node`) |
| S4 | Module-level `post_idempotent(client, path, *, label, **kw)` in `streaming.py`: one retry on `ReadError` / `RemoteProtocolError` | The loop in `embed_image_on_node` (`streaming.py:1525-1533`) | A2 at three sites |
| S5 | `QueueManager.admit(entry)`: put an entry through a target queue's `pending`, so a worker applies `_must_wait`, registers its slot and holds it until it settles | Entries placed straight into `in_flight` (`move_in_flight`) and loads sent outside the queue (`pre_warm`) | B2, B4 |
| S6 | `ollama_line_has_output(line)` in `common/errors.py`, beside `ollama_stream_error` | The inline `output_chunks` condition (`streaming.py:1124-1127`) | A6 |
| S7 | One throughput helper (tokens/s, cold prefill ms/token per `node:model`), cached beside the latency p75 with the same `_refresh_cache` pattern, falling back to the bandwidth estimate now inline in `_score_wait_time` (`scorer.py:572-582`) | The inline bandwidth estimate | B6, B5's session-aware moves |

Migration note for S2: 84 call sites in 10 test files pass plain dicts. A `QueueSnapshot.from_depths()` test helper keeps that a mechanical change.

## Phase A: bugs from the review

### A1. Retries can't reach a node the stranding filter dropped (confirmed)

**Where:** `scorer.py:154-155`. The comment at `:152-153` claims the stranding node is kept "on a retry that has excluded the rest", but the scorer never sees exclusions. The retry (`streaming.py:845-846`) filters `excluded_nodes` after scoring.

**Failure:** the 0.12.5 peer retry. A request goes to the non-stranding node A, A fails before its first chunk, and B (which would strand) was dropped inside scoring. The request fails with "No other node can serve" although B could serve it.

**Fix:**
- `score_request(..., excluded=())` passes the set into `_eliminate` (`scorer.py:278`), which is already the single hard filter. The post-filter at `streaming.py:846` is deleted, so exclusion happens in one place.
- The retry re-score also passes `entry.request.estimated_tokens`, which it drops today, leaving context fit wrong.
- Fix the `:152-153` comment so it matches.

**Tests:** extend `test_retry.py` (A fails retryably, B serves it) and keep `test_eviction_scoring.py`'s stranding tests unchanged.

### A2. Embeddings don't retry a just-closed pooled connection (confirmed)

**Where:**
- `routes/text_embedding_compat.py:243`: native text embeddings and reranks, a single `client.post`.
- `routes/ollama_compat.py:944-1010`: Ollama's `/api/embed`. Its loop (`EMBED_RETRY_BACKOFFS_S`, `:426`) retries only `ReadTimeout`; a `ReadError` falls into the generic `except` and fails.

**Fix:** S4, called from these two sites and from `embed_image_on_node`. Embeddings and reranks are idempotent, so a retry after the server received the request is safe.

**Avoid:**
- `_invalidate_client`: it drops every port's client for the node, too heavy for one closed connection.
- httpx transport `retries=`: it retries connect errors only.
- Image generation and transcription submit: they aren't idempotent and stay out.

**Tests:** `test_text_embedding.py`, `test_native_text_proxy.py`, `test_vision_embedding.py`, `test_embedding_shapes.py`: a transport raising once, then answering, gives 200, one INFO line and one trace.

### A3. Benchmark throughput counts drain-phase work over the load window (confirmed)

**Where:** `benchmark_engine.build_report` divides by `duration` at six sites: `:1721-1722` (totals), `:1731/:1736` (per type), `:1747` (per model), `:1779` (per node). In closed loop `duration` is `LoadResult.window_s = min(elapsed, duration)` (`:1448`), while `success` includes requests that finished during the drain (up to 300 s).

**Callers:**
- `benchmark_runner.py:252-254` and `scripts/benchmark.py:391-393` call `build_report`.
- `docs/experiments/fleet-summarize.py:59-78` fixes tokens only; `release-0-12-12-summarize.py:59` copies the raw figure.
- The dashboard and trace store only display the stored `tokens_per_sec`.

**Fix:** one in-window definition in the engine, and every rate uses it.
- **Closed loop:** a request counts toward rates when `started_ms + latency_ms <= window_s × 1000`, the summarizer's rule. `success` is filtered once into `in_window`, and all six sites read it.
- **Open loop (decision):** the window is the arrivals window, so the same rule would undercount the last requests by about latency × rate. Rate is completions of requests *sent* in the window, divided by the span from the first send to the last completion. That neither inflates nor drops the tail.
- **Report the rest:** add `drained_completed`. Existing records get a `rate_basis` field (`window` / `legacy`) rather than a second rate column.
- **Summarizers:** `fleet-summarize.py` and the release summarizer import the engine's function instead of computing their own.

**Follow-through:** correct published numbers that came from the old figure, in `docs/experiments/*`, the CHANGELOG entries for 0.12.10–0.12.12 and the site's benchmark pages (`ollama-herd-private`). Run 6's summary was already in-window.

**Tests:** `test_benchmark_honesty.py`, `test_benchmark_records.py`, `test_benchmark_harness.py`, `tests/test_experiments/test_fleet_summarize.py`.

### A4. A failing backend can still outrank a working node that must evict

**Where:** `scorer.py:596-605` (`FAILING_BACKEND_PENALTY = 100.0`), and also `score_loaded_models` (`:254`) on the substitution path. A hot failing node keeps +50 thermal and up to +30 affinity; a working node that must evict gets +10 and down to −50. The swing is about 120, more than the penalty.

**Fix:** S1. Failing `node:model` pairs go in `avoid` (after exclusions in `_eliminate`, before stranding) in both scoring paths, and the magic number is deleted. Keep a `failing` marker in the breakdown so the routing log and the only-choice case still say so; only `test_backend_failing.py:97,116` reads that key.

**Tests:** `test_backend_failing.py`, `test_scorer.py`: a failing hot node against a working node with a −50 eviction charge, and a failing node that is the only choice (still tried).

### A5. Parallelism probe trusts a forced `-np 1`; local config read for a remote Ollama

**Where:** `node/collector.py:226-260` (`_runner_num_parallel`: max `-np` over running runners), `:793-797` (the service-config fallback) and `:776` (`OLLAMA_MAX_LOADED_MODELS`). Neither config read is gated by `_is_this_machine` (`:196`).

**Fix:**
- **A runner's `-np` is evidence only when it is above 1.** Ollama forces only serial models (embedders, `OLLAMA_SERIAL_FAMILIES`) to 1, so anything above 1 shows the configured value, and 1 is ambiguous. On ambiguity, fall through to config, else report 0. The router's default is already `OLLAMA_DEFAULT_NUM_PARALLEL = 1`, so routing doesn't change, and `parallelism_unknown` (`health_engine.py:2648`, `num_parallel > 0`) fires correctly with no new field. The runner command line names only a blob, so per-runner classification isn't available without mapping blobs to manifests, and isn't needed.
- **Gate inside `_ollama_env(name, ollama_host)`,** around the `_service_env` step only, so both callers inherit it. herd-node's own environment still counts for a remote Ollama.
- **Accepted trade:** a config file that was never applied wins over runners at 1, which was the behaviour before runners were probed.

**Tests:** `tests/test_node/test_ollama_service_config.py` (only a `-np 1` runner plus config 4 gives 4; a remote host doesn't read config) and `test_request_timings.py` (`parallelism_unknown`).

### A6. A stream whose only line is Ollama's error answers 200 and isn't retried

**Where:** `streaming.py:1114-1116` records the error, `:1167-1168` sets `yielded_any` for any line including the error, and `:1176` is the empty check.

**Fix (in `stream_from_node`; `first_chunk` stays as it is):**
- Use S6 and hold error lines back until a line with output appears. `_recover_backend_errors` already turns a recoverable error into a tool call plus `done`, so that still counts as output.
- A stream that ends with only error lines is decided by `categorize_error`, the existing definition. `context_too_long` and `bad_request` answer the client non-200 with the backend's reason and are **not** retried: retrying them would try every peer and mark healthy nodes failing through `record_failure`. Anything else raises `EmptyUpstreamStream` and retries.

**Reproduction:** `scripts/mock_ollama.py`'s `context_limit` fault (`:564-572`) already sends a 200 stream with one error line, which covers the no-retry branch. Add one error-text fault that isn't a client error, for the retry branch.

**Tests:** `test_empty_upstream_retry.py`, `test_upstream_incomplete.py`, `test_streaming_failures.py`.

### A7. A worker slot leaks when the client leaves as the worker dispatches

**Where:** `routes/routing.py:455-482` (`dispatched_stream`), shared by all six routes (OpenAI, Ollama, Anthropic, Responses, image, transcription). The worker sets the result at `queue_manager.py:465-469`. The comment at `:477-483` saying the stream "is only finalized once nothing references it" is false for an unstarted stream.

**Fix:**
- Turn `_settle_on_exit` (`queue_manager.py:727`) into a small async-iterator class whose `aclose()` settles even if iteration never started: mark failed if still in flight, release the slot, and be idempotent.
- In `dispatched_stream`: `if not future.cancel() and future.exception() is None: await future.result().aclose()`, keeping the existing `client_disconnected` trace.
- Correct the false comment.

That covers any future holder of an unconsumed stream. Neither `first_chunk` nor `first_mlx_chunk` has a future, so they don't share the race.

**Tests:** `test_stream_first_chunk.py`, `test_queue_manager.py` (force the race by resolving the future inside a patched `is_disconnected`), `test_media_traces.py`.

## Phase B: scheduling issues from run 6 and run 4

B1–B3 share one root cause: on a small Mac, herd judges fit by available RAM and its own estimate, while Ollama judges by Metal's working set and `num_ctx × num_parallel`. Do them together, in order.

### B1. Judge fit on Apple Silicon the way Ollama does

**Issues:** the preloader overload into swap (OPEN), and the cause half of B2. Add to that issue entry that `87e3bf0` widened the load gate to allow evict-to-load on a full Mac: correct, but it makes small-Mac loads more frequent while the estimate is wrong.

**Consumers the first draft missed:**
- **The preloader's gate** uses `model_memory_available_gb()` (`model_preloader.py:528`), which ignores the ceiling; only its node choice uses `memory_free_without_evicting_gb` (`:518`).
- **`_score_eviction.has_another_home`** uses `memory.total_gb` for unified memory (`scorer.py:467`), which is wrong for any model above about 17.8 GB on a 24 GB Mac.
- **`routing._pick_pull_node`** (`routing.py:874`).

**Fix:**
1. **Read the limit.** The node reads Metal's `recommendedMaxWorkingSetSize` once at startup through `ctypes`: `MTLCreateSystemDefaultDevice` + `objc_msgSend`, no compile step and no new dependency. Measured on this machine: 107.5 of 128 GiB. Before relying on it, check it against Ollama's own `Metal total 17.8 GiB` log line on an M4, and check that a non-zero `iogpu.wired_limit_mb` moves it. The sysctl reads 0 by default, and ioreg shows current allocation, not the limit.
2. **Report it as its own field.** A separate function in `common/gpu_memory.py` reports `MemoryMetrics.gpu_working_set_gb`. It is **not** a backend of `detect_gpu_memory`: `gpu_total_gb` is memory outside RAM, which `model_memory_available_gb` adds, while the working set is a cap inside RAM. `registry.py:77` copies `memory` wholesale, so the router needs no second change.
3. **One budget, one cost (S3).** `fit_budget()` takes the min of the RAM-or-GPU budget, the working set minus loaded models' `size_vram_gb`, and the ceiling. It is modelled on `gpu_memory_free_gb`, and the ceiling is applied once, the same way for both figures. `fit_cost_gb()` is called by the scorer, `_idle_room_for`, the preloader, `has_another_home` and `_pick_pull_node`, which ends the × 1.2 vs. weights split.
4. **KV cost.** `_observe_kv_cost` divides total KV by the per-slot `context_length`, so the learned MB per token already includes the reporting node's `num_parallel` and transfers wrongly to a node with a different `-np`. Normalise by the reporter's `num_parallel` and multiply by the target's. A5 makes that value right.

**Tests:** `test_gpu_memory_override.py`, `tests/test_common/test_gpu_memory.py`, `test_eviction_scoring.py`, `test_model_preloader.py`, `test_priority_preload_candidates.py`.

**Mock work:** `mock_ollama` evicts by model count (`--max-loaded`), not memory. Add `--memory-gb`, and a working-set option to `fake_fleet`, so run 6's overload replays locally.

**Verify on hardware:** run 6's overload arm on a 24 GB Mac. The preloader stays within the limit, pressure stays below critical, and no empty streams appear.

### B2. A pre-warm that must evict blocks its node's loads

**Issue:** OPEN; findings item 39. `pre_warm` is at `streaming.py:1286`, `PRE_WARM_TIMEOUT_S` at `:171`, and the rebalancer guard at `rebalancer.py:51-55`.

**Fix:**
- `pre_warm` builds a `QueueEntry` with `request_type="load"` (the field exists, `request.py:88`) and goes through S5. `_must_wait`, `should_pin`, `load_pending_on`, the room bound and the queue depth then all see it.
- **A load entry that `_must_wait` would hold is dropped, not held.** A speculative load never waits for a busy model, and since Ollama keeps waiting after a client leaves, not sending it is the only bound that works.
- The preloader and the rebalancer both call `pre_warm`, so one change covers both. `PRE_WARM_TIMEOUT_S` is replaced by the request bounds.

**Avoid:**
- **Polluting the counts:** load entries don't add to `completed_count`, traces or dashboard stats.
- **Client accounting:** check `_acquire_client` is a no-op for an empty `client_ip`.

**Dead config:** `pre_warm_min_availability` (`config.py:39`) is read only by the dashboard. Wire it into `_check_pre_warm` if it is meant to gate, otherwise delete it from the setting, the dashboard and the docs.

**Tests:** `test_rebalancer_decisions.py`, `test_queue_manager.py`, `test_model_preloader.py`. It reproduces today with `mock_ollama --max-loaded 2` plus `fake_fleet --follow-ps`; the mock's evict-busy blocking matches `sched.go`.

**Verify on hardware:** seed 13 mixed models on two M4s (11 of 27 failed at 300 s): zero failures.

### B3. Resident traffic keeps going to a node in critical pressure

**Where:** `scorer._eliminate`'s pressure check (`:298-306`) drops only cold loads; `score_loaded_models` (`:201`) has none, by design.

**Fix:** S1 in both paths. A node at critical pressure goes in `avoid` when another node **holds the model resident** (decision: a cold node that would have to evict doesn't count as an alternative, because sending the request there causes the very load the pressure warns about). Critical pressure never refuses the fleet's only copy.

**Related, not changed here:** "herd pins every model forever, so an idle model goes to swap" (OPEN). Revisit it with B1's limit in hand.

**Tests:** `test_memory_pressure_gating.py`, `test_scorer.py` (`testing/heartbeats.make_node(pressure=)` exists). Add a per-node pressure option to `fake_fleet`.

### B4. A retried request runs outside its new node's worker limit

**Issue:** OPEN (2026-11-21). `move_in_flight` is at `queue_manager.py:809`; the retry calls it at `streaming.py:882`.

**Fix:** S5. The moved entry goes into the target's `pending` with a `process_fn` that hands the retry its turn. The worker applies `_must_wait`, registers `q.slots[rid]`, resolves the future (the retry awaits it within its remaining timeout), and holds the slot until `mark_*` on the target key releases it. `move_in_flight` no longer puts the entry straight into `in_flight`.

**Watch:** the worker's cancelled path calls `_release_client`, which would release the client twice for a moved entry. Make that path check whether the entry was admitted from a move.

**Tests:** `test_queue_manager.py`, `test_retry.py`, `test_streaming_failures.py`, `test_rebalanced_request_counts.py`.

### B5. The rebalancer drains a backlog at a fixed 3 requests per tick

**Where:** `rebalancer.py:96-140`. The move count is `min(pending // 2, rebalance_max_per_cycle)` at `:128`, with `rebalance_max_per_cycle = 3` (`config.py:35`) every 5 s (`:33`). The first draft missed two more problems:
- **A second throttle:** the `target_depth >= rebalance_threshold` gate (`:115`) skips a target the last move just filled.
- **A second residency definition:** `:121` checks residency with `m.name in loaded` instead of `model_resident_on_node`.

**Fix:**
- Move up to the target's free slots, capped by the source's `pending`. Read free slots from S2, with target concurrency from `_compute_queue_concurrency()`: a queue the move just created sits at `_MIN_CONCURRENCY = 1`.
- Free slots replace the depth gate.
- Use `model_resident_on_node`.
- **Delete `rebalance_max_per_cycle`:** `config.py:35`, `routes/dashboard.py:1358`, `docs/api-reference.md:1876` and the test helper. A stale env var is ignored, so nothing breaks; the CHANGELOG says so.

**Later, with S7:** don't move a pending request whose session is pinned to the source when its re-read would cost more than the wait.

**Tests:** rewrite `test_rebalancer_decisions.py:182,194`, which encode the cap; extend `test_queue_manager.py:179` (`move_pending`).

**Measure:** `fake_fleet --churn/--rejoin-after` with mock `--chunks/--chunk-delay-ms`, before and after. Then the same rejoin-mid-batch run on the M4s (71.3 s against 52–58 s).

### B6. Session affinity gives a conversation away without weighing the re-read

**Issue:** OPEN (run 6); findings item 13 is the same family.

**Where:**
- **Scorer:** `SESSION_AFFINITY_BONUS` (`scorer.py:594`), its decay scale (`:618`), the prefix bonus (`:624`) and `_pin_bonus` (`:652-671`).
- **Tracker and pins:** the tracker is `session_affinity.py:57-96`; pins are written in `_remember_session_node` (`routing.py:235`).

**Fix:**
- **Keep what is now discarded.** Capture Ollama's `prompt_eval_duration` (it arrives on the done line, `streaming.py:1143`) into the request's `timings`.
- **Measure cold prefill (S7).** Derive a per-`node:model` cold prefill rate from it: a high percentile over prompts above N tokens. `prompt_eval_count` includes cached tokens (`:1136`), so a naive average counts cache hits as fast prefill.
- **Weigh in seconds.** Express both the re-read (`estimated_tokens` × the candidate's cold rate) and the queue wait in seconds, using `_score_wait_time`'s conversion, so the bonus fades when waiting is genuinely cheaper than re-reading.
- **Retries keep the session.** The retry re-score (`streaming.py:845`) passes the session too, alongside A1's estimate.

**Avoid:**
- **A fourth token estimator:** `routing.py:285` already warns about this.
- **A separate prefill table:** S7 caches beside the latency p75.

**Mock work:** the mock has no prefill cost or prompt cache. Add `--prefill-ms-per-token` and a per-prefix cache to `scripts/mock_ollama.py`; C2 needs it too.

**Tests:** `test_session_affinity.py`, `test_prefix_affinity.py`, `test_fleet_affinity_header.py`.

**Verify on hardware:** eight conversations on two M4s: turn-2 time-to-first-token median and p95, and the share of follow-ups that stay home.

### B7. `joins_load` cancels the eviction charge for any queued depth

**Findings item 30.** `scorer.py:83`, `joins = -s0 if depth and s0 < 0`; `_score_eviction`'s `charge()` (`:475`) reads depth > 0.

**Fix:**
- Apply `joins_load` only while this model's own load is under way. That is S2's `loading`, not `load_pending_on`.
- Extract the expression at `queue_manager.py:687` as `load_under_way(node, model)` and use it from both `_must_wait` and the snapshot, so there is one definition.
- Don't count `held` entries as joining: item 29's swapping requests are exactly the held ones.

**Tests:** `test_eviction_scoring.py`, including the run 2 spray case (`:143`) when the first request is held rather than in flight. Item 30's churn reproduces on `mock_ollama --max-loaded --load-ms`.

### B8. A node swaps two models per request instead of batching

**Findings item 29.** After B7. `_must_wait` is at `queue_manager.py:637-689`.

**Fix:** port `ollama-herd-private/.bench-lambda-2026-11-20-run4/switch-turn.patch` (241 lines) by hand. It was made against `1e7cdb2` and won't apply, since `_must_wait` has since gained `_idle_room_for` and the room bound.
- **Drop the patch's `_resident` override.** It is a second residency definition beside `serializers.model_resident_on_node`. Put the node's last switch into S2 and teach the one helper about it instead.
- **Add the backlog condition** the item calls for: the waiting model's held count against the resident model's arrivals, both read from S2.

**Tests:** the patch's `test_queue_manager.py` cases, rebased. Measure on the mock at 0.25 and 0.5 req/s; it must not double p95 at 0.25, which the patch alone did.

### B9. Loaded models ignore the capacity learner's back-off (regression from `6dbf115`)

**Where:** `QueueManager._compute_queue_concurrency`, the early return at `queue_manager.py:338`.

**What `6dbf115` got right:** memory. Ollama sets aside every slot's KV cache when it loads (`-c NumCtx × numParallel`), and `available_gb` is measured after that. The old code counted the model twice and held a 24 GB Mac to one request against Ollama's four (run 6).

**What it broke:** the learner's "busy, take less" signal. It reached concurrency only through the ceiling, by accident of arithmetic, and the early return removed it. On a capacity-learning node in a learned-low or learned-medium band, a loaded model now gets every slot. The hard pause (camera, microphone, thermal, sustained CPU above 85%, availability below 0.2) still eliminates the node.

**Who it affects:** only nodes with `enable_capacity_learning` on (default off).

**Fix (two concepts, each explicit):**
1. **Memory:** a resident non-MLX model's concurrency is `backend_limit`. Keep `6dbf115`'s logic and comment.
2. **Back-off:** `CapacityMetrics.slot_cap(backend_limit)` in `models/node.py`, mapping from the `CapacityMode` the learner already derives (`_availability_to_ceiling`, `capacity_learner.py:466`). `models/node.py` is shared by the node and the router, so the mapping can't drift across the process boundary.

   | `CapacityMode` | Availability | Slots |
   |---|---|---|
   | `FULL` | 0.8 and up | `backend_limit` |
   | `LEARNED_HIGH` | 0.6–0.8 | `backend_limit` |
   | `LEARNED_MEDIUM` | 0.4–0.6 | half, at least 1 |
   | `LEARNED_LOW` | 0.2–0.4 | 1 |
   | `PAUSED` | below 0.2 | eliminated (unchanged) |

   It applies to every model, resident or not.
3. **The ceiling then means memory only,** through S3's `fit_budget`. Fix the learner's docstring, which still describes the old 16/32/64 GB bands and a CPU rule that now pauses.

**Note:** concurrency is recomputed only in `_ensure_workers` when something is enqueued, so a new slot cap applies from the next request. That's acceptable; say so in the comment.

**Tests:** `test_capacity.py`, `test_queue_manager.py`.
- A resident model with `backend_limit` 4 gets 4 in `FULL` / `LEARNED_HIGH`, 2 in `LEARNED_MEDIUM` and 1 in `LEARNED_LOW`.
- With capacity learning off it gets `backend_limit` (run 6 stays fixed).
- A cold model in `LEARNED_LOW` is capped by the same function.

## Phase C: dashboard and method

### C1. Dashboard throughput tiles barely move during a burst

**Where:** `recent()` (`routes/dashboard.py:2768`), the top tiles (`:2783-2791`) and the node cards (`:3112-3113`, which have no "5 min" label). No live token counter exists today: `output_token_count` (`streaming.py:1132`) is a local chars/4 figure. In-flight counts per node are already live: sampled every 5 s by `fleet_history.py:53-59` and pushed over SSE every 2 s.

**Fix:**
- Replace the two `entry.last_progress_at = time.time()` stamps (`streaming.py:457,734`) with one `QueueManager.progress(entry)`. It stamps progress and adds to a cumulative `streamed_chunks` on the queue.
- `FleetHistory.sample` turns the deltas into a live rate. Label it as chunks per second, distinct from the trace's `eval_count`.
- Keep the five-minute averages beside it, labelled on every card.
- The MLX path (`mlx_proxy`) doesn't pass through these stamps; route its progress through the same method.

**Avoid:** a second sampler or endpoint.

**Tests:** `test_live_overview.py`, `test_dashboard_instant_stats.py`, `test_dashboard_sse.py`. Reproduce with mock `--chunks 200 --chunk-delay-ms`, then film the 96-request batch.

### C2. Benchmark cells share a seed and start warm

**Findings item 35.** Worker generators are `random.Random(f"{seed}-{k}")` (`benchmark_engine.py:1332-1354`), so a concurrency-8 cell replays the concurrency-4 cell's first four worker streams exactly. That is the mechanism behind the warm cache. Each `scripts/benchmark.py` invocation is one cell; the cell loop lives in the private repo's `phase.sh`.

**Fix:**
- Add one `cell_seed(run_seed, workload, concurrency, rate, repetition)` in the engine, and `--run-seed` in `scripts/benchmark.py`, mutually exclusive with `--seed`.
- `--seed` keeps its meaning, and the manifest keeps recording the effective seed (`benchmark.py:400`), so runs 2–6 stay exactly repeatable.
- Mark runs 2–5's scaling cells as possibly warm in their write-ups.

**Tests:** `test_benchmark_workload.py`, `test_benchmark_records.py`. Proving the cache effect needs B6's mock prompt cache.

## Phase D: release

1. **Order:** S1, S4 and S6 with A1–A7; S2 and S5 with B4 (B2, B5, B7 and B8 follow them); S3 with B1; B3 and B9 after S1 and S3. Each item lands with its tests, then `uv run pytest`, `ruff` and `health.sh`.
2. **Refresh counts.** This work adds tests and may add checks; `health.sh` lists stale quotes.
3. **Soak on the Mac fleet for several hours.** None has run since 0.12.12, and about 730 unreleased routing lines are already on `main`. Before trusting it, run `find src/ -newermt "<router start>"`, which must print nothing. Then replay run 6's overload and pre-warm scenarios on the EC2 Macs, the only hardware that reproduces them.
4. **Correct the published numbers from A3** before or with the release notes.
5. **CHANGELOG:** every item, plus the deletions below.
6. **Mark the `docs/issues.md` entries `FIXED`** with commits, and update the findings plan's items 13, 29, 30, 35 and 39.

**Deleted by this plan:**
- the `rebalance_max_per_cycle` setting (B5);
- `FAILING_BACKEND_PENALTY` (A4);
- `PRE_WARM_TIMEOUT_S` (B2);
- `pre_warm_min_availability`, unless B2 wires it in;
- the retry's post-scoring exclusion filter (A1);
- the inline fit cost in `_idle_room_for` (S3);
- the stale capacity-learner docstring and the false comment at `queue_manager.py:477-483`.

**Mock and fake-fleet additions** (for local before-and-after runs):
- `mock_ollama --memory-gb` (B1)
- `--prefill-ms-per-token` plus a per-prefix prompt cache (B6, C2)
- a non-client-error error-line fault (A6)
- `fake_fleet` per-node pressure (B3) and working set (B1)

## Out of scope

These older `OPEN` entries in `docs/issues.md` are unrelated to this review and run 6, and keep their own entries:
- the LAN proxy decision made at startup;
- `qwen3-coder:30b`'s tool calls as text;
- the MLX coder instance;
- `brew` / `flit_core` on macOS 27;
- native embedding models never unloading;
- llama-server's prompt cache;
- the Aug-22 step change;
- fastembed's Pillow cap;
- DiffusionKit;
- the SIGKILLed MLX server;
- the platform flags in 0.14.0;
- the dashboard's inline HTML;
- CI on one OS.

That last one is still the gap that let a whole-platform outage reach a user, and it deserves its own plan.
