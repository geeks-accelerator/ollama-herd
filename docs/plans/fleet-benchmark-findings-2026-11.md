# Fixes from the first fleet benchmark (2026-11-20)

**Created:** 2026-11-20 · **Revised:** 2026-11-20, after a code audit of `main` (file:line below are
`main` at `b156472`) · **Status:** ✅ **implemented on `main`, unreleased** (2026-11-20; suite 2334
passed). Open: run 2 (item 11), item 10 step 4 (fix what run 2's timings show), investigations 7-8,
and the release decision (end). Implementation notes are in "Implemented" just below.
**Source:** a benchmark of herd 0.12.9 with Ollama 0.40.16 on rented NVIDIA machines (four Quadro
RTX 6000 24 GB and one RTX A6000 48 GB, Ubuntu 22.04, Ollama as a systemd service, one private
network), using `scripts/benchmark.py` at `56993ff` with `--prompt-tokens 1250 --max-tokens 200`
and `docs/experiments/model-alternation-benchmark.py`.

## Implemented (2026-11-20, OSS `main`)

| Item | Commit | Notes |
|---|---|---|
| 1 | `3b470a0`, `ee27af2` | systemd unit read through `_ollama_env`; **runner `-np` built** (Ollama 0.40's `llama-server` carries `-np N` per model; the largest across Ollama's runners, someone else's `llama-server` ignored); restart guard; one howto helper; `parallelism_unknown` (INFO) from item 10's `queue_ms` |
| 2 | `817e42a` | `TraceStore.has_served` + `native_text_embeddings_in_use` (also counts a known-but-offline native node); docker smoke embeds through the router |
| 10 | `e2949f6`, `b98af1f` | one pool keyed (node, port); `/api/version` from heartbeats (no request at all); `timings` column; `X-Fleet-Overhead-Ms`; async image fetch. Local check: 5.7 ms measured outside, 5.4 ms reported; ~1.3 routing, ~0.5 queue, ~3.7 in the upstream leg beyond Ollama's own time |
| 5 | `fa4e0da`, `af32336` | one engine path (`discover_fleet` labels embeddings, `warmup` direct per node, `run_load`); `discover_embed_models` deleted |
| debt | `5d67a8c` | `score_role_affinity_max` deleted; canonical headers on the OpenAI image route and the Anthropic MLX passthrough. Pull and `/api/show` responses keep only `X-Fleet-Node`: they are not model answers |
| 9 | `9aded6f` | fallback is the largest context any node ran the model with (cautious), not a prediction of Ollama's VRAM rule |
| 3 | `d721663` | weights worked through run 1's two cases: alternation, the empty node wins by memory fit (8) + the penalty; mixed, evicting the busy coder costs 40 |
| 4 | `621e5f2` | verified live on a Mac: with Ollama's 3-model cap full a same-category model answered (`X-Fleet-Fallback: true`); with a slot free the requested model loaded (`false`) |
| 6 | `847929d` | changelog, configuration and API references, Docker guide, guides, skills |

Not done, on purpose: the four test fixtures that patch `httpx.AsyncClient` module-wide still do (they
keep working because the pool builds clients from the same `httpx` module; converting them was churn
in passing tests). The site describes released behaviour, so it is synced at the release, not now.

## Benchmark run 2 (2026-11-20): fixed during the run, and what it opened

Fixed during the run: harness warm-up listing disk-only nodes for a model loaded elsewhere
(`156bad9`); pre-warm evicting busy models (`c786c64`, the rebalancer chose the runner-up without the
eviction signal: missed by the audit); eviction scored 0 mid-swap, when Ollama lists no model while
one loads (`3a225b3`). Also `a324ef0`: herd-node read the runners of an Ollama it does not serve.

Open, from run 2:

12. **Router CPU per request.** Through a 20 ms mock the router collapsed between 50 and 80 req/s on a
    Lambda vCPU (held 200/s on an M-series Mac); ~24 ms of herd time per request over the network,
    reported identically by `X-Fleet-Overhead-Ms`. py-spy at 80/s: ~43 % the SQLite trace-writer
    thread (a commit per request; 10 writes failed "cannot commit" under load), ~15 %+ Rich console
    logging (the per-request "Routing …" INFO line and access logs), ~12 % scoring (`_eliminate`
    recomputing fit costs). Batch trace writes, log routing decisions at DEBUG or without Rich
    rendering, cache fit cost per (node, model) per heartbeat; then re-measure with
    `scripts/router_overhead.py --rate`.
13. **Shared-prompt sessions: first turn ~2x slower through herd.** 16 sessions over 4 nodes: herd's
    median TTFT 1.4-1.5 s against round-robin's 1.07 s, almost all of it on first turns (1.3 s
    against 0.66 s), although herd's own queue wait was 3 ms. Later turns favour herd (+26 % growth
    over four turns against +171 %), and p95 is 23 % better. Read Ollama's per-request
    prompt-eval counts and slot assignment for first turns placed by prefix affinity.

## Rules for this work

herd is greenfield, so:
- **No feature gating.** New behaviour replaces old; no setting to turn a fix on or off, and no
  setting kept alive for behaviour that is gone. Where a fix makes a setting pointless, the setting
  is deleted (item 4 deletes `vram_fallback`).
- **Extend, don't add.** Every change below names the existing function it extends. A second way to
  do the same thing (a second cache, a second client pool, a second "available memory" formula) is
  a bug in the plan.
- **Remove what the fix makes dead,** in the same change: code, tests, health-check text, docs.

## What the run showed

Scaling is real once the router is allowed to use each machine: `qwen3-coder:30b` went from
111.7 tok/s on one machine to 220.6 on two and 434.9 on four, with 0 failures. But getting there took
a workaround, three of the four tests hit a bug, and the router added 24 ms per request across the
network:

| # | Problem | Who it hits | Release |
|---|---|---|---|
| 1 | Router serialises every node whose Ollama has `OLLAMA_NUM_PARALLEL` set in a systemd unit | Linux users who raised parallelism the documented way | A |
| 2 | `/api/embed`, `/v1/embeddings`, `/api/embeddings` for `nomic-embed-text` 503 when no node has fastembed, even with the model loaded in Ollama | Every install without the `embedding` extra, and every container | A |
| 3 | Placement ignores what a cold load evicts, and the warm bonus rewards the node that just evicted | Anyone with more models than one node holds | B |
| 4 | `vram_fallback` answers a request for a model that is not loaded with *any* loaded model, across categories, even when another node could load it into free memory | Every multi-model herd | B |
| 5 | Benchmark harness: warms one node, needs models loaded, cannot demand the exact model | Our own measurements | A |
| 6 | Docs say things the code does not do | Docker and Linux guide readers | A |
| 9 | Cold-load cost uses one fleet-wide context size, though Ollama sizes context by each card's VRAM | Mixed-card herds | B |
| 10 | 24.4 ms router overhead across a network (6.5 ms on loopback), cause unknown; native backends open a client per request | Everyone, mildly | A |

7 and 8 are investigations; 11 is run 2's method. "Debt found by the audit" lists bugs and
duplication to fix alongside.

---

## 1. `OLLAMA_NUM_PARALLEL` is invisible when Ollama is a systemd service

**Seen:** `OLLAMA_NUM_PARALLEL=4` in `/etc/systemd/system/ollama.service.d/override.conf` (Ollama's
documented Linux method). One node stayed flat at 77 tok/s from 1 to 8 concurrent requests, time to
first token growing one request per step (18.6 s at 8); `/fleet/queue` showed `"concurrency": 1`.
Passing the variable to `herd-node` too fixed it (96 → 112 tok/s at 2 → 4). Ollama's own default is
1, so an unconfigured install is unaffected; only a raised value goes unseen.

**Cause:** `node/collector.py:125-151` `_ollama_env()` reads herd-node's own environment, then
`launchctl getenv` on macOS only (`:139` gates on `darwin`). It is the only place the node reads
Ollama's configuration (callers `:667` `OLLAMA_MAX_LOADED_MODELS`, `:683` `OLLAMA_NUM_PARALLEL`).
Downstream (`models/node.py:265`, `serializers.py:33,58-104`, `queue_manager.py:300`) needs no change.

**Build on:** `_ollama_env` and its `_LAUNCHCTL_CACHE` (`collector.py:105-107`) with the
subprocess/timeout pattern at `:143-149`.

**Change:**
1. Make `_ollama_env`'s fallback per platform: `launchctl getenv` on macOS, `systemctl show
   ollama.service -p Environment --value` on Linux (unprivileged; worked on Ubuntu 22.04 in the run),
   parsed for the one variable. One cache for both (rename `_LAUNCHCTL_CACHE`). Both callers get it.
2. Rewrite the comments at `:660-665` and `:678-681`, which say the node shares Ollama's environment.
3. **Runner command line, investigated first, then either built or dropped.** `health_engine.py`
   (`:657-659`, `:1243-1246`, `:1418-1420`) already tells users to read `-np N` from `llama-server`'s
   arguments, so the per-model slot count is there. If 0.40's runner shows it, read it in the
   `num_parallel` block (`:678-688`) using the `psutil.process_iter(attrs=["pid","name","cmdline"])`
   pattern from `node/mlx_supervisor.py:307-335` (and `backend_clients.py:128`, which already knows
   `llama-server` is Ollama's). It beats systemd: it is what Ollama applied, per model.
4. **Warning without new plumbing.** No health check sees queue saturation today
   (`HealthEngine.analyze` has no queue access). Item 10 writes queue wait into the trace; the warning
   is then a trace query ("requests waited in herd's queue while the node reported `num_parallel`
   0"), shaped like `trace_store.get_embed_error_stats` (`:716`), checked in a `_check_*` method like
   `_check_embed_error_rate` (`health_engine.py:2922`). Built after item 10.
5. Containers see neither systemd nor host processes: document `-e OLLAMA_NUM_PARALLEL` (item 6).

**Remove / fix alongside:** the macOS-only fix text for `NUM_PARALLEL` exists three times
(`health_engine.py:664-670`, `:2974-2977`, `routes/dashboard.py:1079`): one platform-aware string.
`routes/fleet.py:57` documents a `num_parallel` field `/fleet/limits` does not return.

**Tests:** none exist for `_ollama_env`. Add them through `collect_heartbeat`, as
`tests/test_server/test_gpu_memory_override.py:136-146` does: faked `systemctl` output; env wins;
`systemctl` missing / failing / timing out reads as unset; macOS path unchanged. Clear the renamed
cache in `tests/conftest.py:93-120` `_clear_collector_ttl_caches`. If step 3 lands, fake processes as
`tests/test_node/test_mlx_orphan_detection.py` does.

**Verify:** Lima Ubuntu VM, Ollama under systemd with the variable in the unit only:
`/fleet/status` `num_parallel: 4`, `/fleet/queue` concurrency 4.

## 2. `nomic-embed-text` fails without fastembed, even when Ollama has it

**Seen:** 1,055 of 1,055 embed requests 503'd ("No node is running the native text embedding
server…") with `nomic-embed-text:latest` loaded in Ollama. With the `embedding` extra installed,
~17,000 succeeded.

**Cause:** `routes/ollama_compat.py:817-819` `dispatch_embed` sends any name in
`TEXT_EMBEDDING_MODEL_NAMES` (`node/text_embedding_models.py:64`: exactly `nomic-embed-text` and
`:latest`) to `embed_text`, which only knows the native server. `/v1/embeddings`
(`openai_compat.py:826`) and `/api/embeddings` (`ollama_compat.py:750`) share it. The container
image has no fastembed, and `docs/guides/docker.md` says embeddings go through Ollama.

**Decided:** fall back to Ollama only while no node has *ever* served it natively, surviving a router
restart, so one index never mixes the two backends' vectors.

**Build on:**
- **"Ever served natively" is already in the trace store; no new state.** Native embeds are traced
  with `original_format="embed"` under the requested name (`text_embedding_compat.py:89-104`, via
  `routing.direct_proxy_tracer` `:457`); Ollama embeds as `ollama`/`openai`
  (`ollama_compat.py:884-894`). One query (completed, model in `TEXT_EMBEDDING_MODEL_NAMES`,
  `original_format='embed'`), covered by `idx_traces_model_timestamp` (`trace_store.py:137`),
  shaped like `get_recently_used_models` (`:997`), memoised in memory once true. The registry is
  memory-only (`registry.py:26`), so SQLite is the only thing that survives a restart anyway.
- **The Ollama path below the branch already works** (`ollama_compat.py:821-960`): the bare name is
  normalised to `:latest` (`models/request.py:29,106-112`) and the response already sets
  `backend="ollama"` (`:947-954`).

**Change:** extend the branch at `ollama_compat.py:817-819`: native when an online node serves it
(`EMBED.serves`, `text_embedding_compat.py:93`) or the trace store says one ever has; otherwise fall
through. Keep `_no_native_server` (`:140-173`) as is for the native case, and reword its last message
(`:168-172`) to name both fixes.

**Remove / fix alongside:**
- `health_engine.py:2583-2588` `_NATIVE_TEXT_EMBED_MODELS` duplicates `TEXT_EMBEDDING_MODEL_NAMES`:
  import it.
- Health text that is false today or after the fix: `:2537-2538` and `:2563-2565` ("will fall back
  to Ollama"), `_check_text_embedding_ollama_bypass` (`:2591`), `_check_nomic_loaded_in_ollama`
  (`:2661`, "never used").
- `ollama_compat.py:849-850` checks the raw bare name against the fleet's list, so a bare-name miss
  returns 404 instead of 503: use `inference_req.model`.

**Tests:** the 503 cases in `tests/test_server/test_native_text_proxy.py` (`:119-162`; its fixture
mocks `record_trace` at `:30`, so stub the new query there); the path cases in
`test_embedding_shapes.py:81,97,137`; `test_text_embedding.py:184-201` (interception by name) gains
"no native node ever → Ollama". `scripts/docker-smoke.sh` gets an embed block after the chat block
(`:69-78`) asserting `x-fleet-backend: ollama`; `.github/workflows/image.yml:112` pulls
`nomic-embed-text`.

## 3. Placement is blind to evictions, and the warm bonus feeds thrashing

**Seen:** two nodes, two 15.7 GB models that cannot share one 24 GB card, 20 alternating requests,
fallback off: all 20 went to one node, swapping every time (7.6-8.0 s TTFT), the other node empty.
In the mixed test the router evicted a busy `qwen3-coder:30b` to load `qwen3.5:9b`, which another node
had loaded.

**Cause:** `scorer.py:283-296` `_score_thermal` gives a model unloaded in the last 30 minutes
**warm +30** (`:289-292`, from `node.model_unloaded_at`, set at `registry.py:71-75`) and an on-disk
model **cold +10**, so the node that just evicted a model is favoured for it. `_score_memory_fit`
(`:298-326`) measures against RAM + free GPU, so on discrete-GPU nodes every model fits everywhere.
Ollama evicts rather than spill a model that would fit after an eviction (`sched.go`, logged in the
run). Nothing scores what a load displaces.

**Build on:**
- `NodeState.gpu_memory_free_gb()` (`models/node.py:548-559`): exactly the memory Ollama loads into
  without evicting on a node with GPU memory; `memory.gpu_total_gb > 0` is the switch (0 on unified
  memory, `node.py:71-76`). Not `GpuDevice.kind`, which "decides nothing" (`:53`).
- `_model_fit_cost_gb` (`scorer.py:635-660`) for the cost; `LoadedModel.size_vram_gb`
  (`node.py:130-133`) for what each eviction frees.
- `serializers.hot_model_cap_for` (`:132`) and `distinct_resident_model_names` (`:257`): a load also
  evicts when Ollama's model-count cap is full, even with memory free (the `free_slots` arithmetic at
  `:186-193`).
- **Traffic, no new tracking:** `queue_depths` already passed to `score_request` (busy now) and
  `model_preloader.get_model_priority` / `_priority_cache` (`model_preloader.py:89,291`; recent
  traffic from `trace_store.py:1058`), already read synchronously at `routing.py:570-572`. Not
  `QueueManager.completed_count` (cumulative, unreachable from the scorer) and not
  `LoadedModel.requests_active` (never filled per model, `ollama_client.py:49-62`).
- `RoutingResult.scores_breakdown` (`models/request.py:146`) is already stored in the trace
  (`trace_store.py:324`): a new key reaches traces and logs without plumbing.

**Change:**
1. One method on `NodeState` beside `model_memory_available_gb`: memory a load can use **without
   evicting** (`gpu_memory_free_gb()` when `gpu_total_gb > 0`, else `memory.available_gb`), capped
   by the capacity ceiling. It also absorbs the ceiling cap that is written out three times today
   (`scorer.py:266-268`, `:313-315`, `routing.py:802-804`).
2. `_score_thermal`: warm only if the model fits in that figure and a model slot is free; otherwise
   it scores as cold.
3. `_score_memory_fit`: fit ratio against the no-eviction figure. `_eliminate` (`:262-274`) keeps
   RAM + GPU: it is the hard "can it run at all" gate.
4. An `eviction` signal in `score_request` (and the total at `:86`, the log at `:102-122`,
   `score_loaded_models`' sum at `:181`): which resident models must go, weighted by their queue
   depth and priority. **No new weight setting**: size it from `score_model_warm` and
   `score_model_cold` (`config.py:21-22`) so a node with room always beats a node that must evict a
   busy model by more than the warm-cold gap.
5. Callers that pick a node for a load use the same figure: `routing.py:777-810` `_pick_pull_node`
   (also sizes by weights instead of `_model_fit_cost_gb`, and leaks `model_size` across loop
   iterations) and `model_preloader.py:500-512` (picks most RAM + GPU, so it causes evictions too).

**Tests:** `tests/test_server/test_gpu_memory_override.py:62-100` `TestFitGate` already builds GPU
nodes with `size_vram_gb`: the alternation case goes there (node 1 has A resident, B recently
unloaded; node 2 empty; B goes to node 2), plus "a busy resident model is not evicted when another
node has room". `test_scorer.py` `test_warm_tier_scoring` (`:60`) and `test_memory_fit_scoring`
(`:187`) are unified-memory cases and must pass unchanged. Optionally give `conftest.make_heartbeat`
(`:133`) GPU arguments.

**Verify:** alternation test, fallback on and off: each model settles on its own node; every request
after its first load reaches its first token in under 1 s.

## 4. Substitution: keep same-category for "every load evicts", delete the rest

**Seen:** with defaults every request for `qwen2.5:14b-instruct-q8_0` was answered by `phi4:14b-q8_0`
instead of loading `qwen2.5` on the empty second node.

**Cause:** `routes/routing.py:539-667` `_try_vram_fallback`: when the best score is a cold load,
serve the best loaded model of the same category, failing that **any** loaded model. It never asks
whether a node could load the requested model without evicting.

**Decided:** load on a node with room instead of substituting; cross-category substitution removed.

**Change:**
1. At the cold-load decision (`routing.py:255-268`), if the cold winner's
   `scores_breakdown["eviction"] == 0` (item 3), return it. Results are sorted, so the winner is a
   node with room whenever one exists; no second fit check.
2. `_try_vram_fallback` keeps only the same-category block (`:557-593`). Move the `has_images` guard
   (`:618-639`) into it: that path checks nothing for images today and relies on categories.
3. **Delete `settings.vram_fallback`.** Strict mode stays per request (`parse_allow_fallback`,
   `:156-177`: `X-Fleet-No-Fallback: true` or `"fallback": false`); `:235-238` becomes
   `allow_fallback is not False`, and `X-Fleet-No-Fallback: false` (force fallback) goes with it.
4. `fallback_models` (the client's own list: `request.py:84,112`, `openai_compat.py:343`,
   `ollama_compat.py:85,162`, stripped at `streaming.py:1724`) is a separate feature and stays.
5. Side effect, intended: `ollama_compat.py:839` sends embeds through `score_with_fallbacks`, so an
   embed can fall back to a chat model today. Removing cross-category ends that.

**Remove:** `routing.py:595-665` (any-model pass, vision `risk_note`, the ERROR log) and the
`cross_category` / `fallback_category` parameters and fields (`:675-693`; nothing reads them,
`health_engine.py:784-819` reads only `requested_model`). `vram_fallback` in `config.py:46-47`,
`dashboard.py:1290,1401,4966`, `docs/configuration-reference.md:259`,
`docs/api-reference.md:86,1848,1872`, `docs/guides/agent-setup-guide.md:525`,
`docs/plans/vram-aware-model-fallback.md` (mark superseded), the docstring at `routing.py:546`.
Historical mentions (`CHANGELOG.md:816`, `docs/issues.md:2353`) stay. Changelog says plainly that
some requests now wait for a load where they used to get a different model.

**Tests:** `test_routing.py:338-455` `TestVramFallback` (`_cold_result` / `_hot_result`): add "cold
winner with eviction 0 → no substitution"; delete `test_vram_fallback_cross_category` (`:391-416`)
and `test_textonly_vision_model_request_may_still_fall_back` (`:550-557`); give `:541` a
same-category variant; drop `vram_fallback=` from `ServerSettings(...)` in ~6 tests and
`tests/test_server/test_routes.py:577,591-597`.

## 5. Benchmark harness

**Build on:** there is already one engine: `benchmark_runner.py:17-30` (dashboard) and
`scripts/benchmark.py:34` (CLI) both import discovery, workers, `warmup` and `build_report` from
`server/benchmark_engine.py`.

**Change:**
- **Warm every node directly** by moving the direct warm-up at `benchmark_runner.py:519-531` into
  `benchmark_engine` as a per-(node, model) warm over `/fleet/status` `nodes[].ollama_url`
  (`serializers.py:156`) × `models_loaded`, used by both. Fix two things on the way: send
  `keep_alive: -1` like the router (`streaming.py:1727`), not `"60m"`, and send `num_ctx` equal to
  the resident `context_length`, or Ollama reloads the model (`streaming.py:1551-1590`
  `_pin_resident_num_ctx`). Not `StreamingProxy.pre_warm` (`:1079`), which sends an empty prompt and
  never computes, so it would not cover item 8's first-compute stall.
- **Demand the exact model:** `send_request` (`benchmark_engine.py:437-457`) sends
  `X-Fleet-No-Fallback: true`, reads `X-Fleet-Served-Model` into a new `RequestResult.served_model`
  (`:129`; `X-Fleet-Node` is already read at `:320,362,473`) and fails on a mismatch. The stdlib-only
  `docs/experiments/model-alternation-benchmark.py` (`:56`) does the same with its own few lines.
- **`think`:** an option that sends `"think": false`, which already disables the router's
  `num_predict` inflation (`streaming.py:1916`).
- **Models on disk:** `scripts/benchmark.py:173-181` accepts `--models` from `models_available`.
- **Placement in results:** per-model `{node: count}` and mismatches go into `per_model_results` /
  `per_node_results`, JSON columns already (`trace_store.py:195-198`); no schema change.

**Remove / fix alongside:** the orchestration duplicated between `scripts/benchmark.py:224-298` and
`benchmark_runner.py:265-317` (spawn, drain, report) becomes one engine function both call;
`benchmark_runner.py:208-228` rebuilds `NodeInfo` by hand instead of `discover_fleet`;
`benchmark_runner.py:316` passes `targets`, defined only in the LLM branch (`:189`), so a run without
LLMs raises `NameError`; `save_benchmark_run` (`trace_store.py:1244-1281`) drops `report["workload"]`
and `mode` / `smart_pulled_models` (nest them in `fleet_snapshot`); the embed-name patterns are
repeated (`benchmark_engine.py:57`, `benchmark_runner.py:407`).

**Tests:** extend `tests/test_server/test_benchmark_workload.py` (`httpx_mock` pattern, `:70-104`);
add the first `BenchmarkRunner` tests, starting with the no-LLM run. Done already: Python 3.11
syntax (`af32336`).

## 6. Docs

- `docs/guides/docker.md`: embeddings through Ollama (true after item 2); `-e OLLAMA_NUM_PARALLEL` in
  the Linux node section.
- Linux configuration: herd reads `OLLAMA_NUM_PARALLEL` from the systemd unit; in a container pass it
  to herd-node.
- Configuration and API references: `vram_fallback` gone; substitution only within a category when
  every load would evict; strict mode per request.

## 7-8. Investigations

7. **Uneven split at low concurrency:** four nodes at 2 concurrent requests split 58/42 and read
   129.9 tok/s, below two nodes (152.9). One run; repeat, then read `scores_breakdown` in the traces.
8. **First request on a new runner:** ~39 s to the first token on each RTX 6000 the first time a
   loaded model computed. Likely CUDA JIT for compute capability 7.5, cached afterwards. Ollama's
   behaviour; check whether it recurs after unload and reload. Item 5's direct warm-up covers the
   benchmark either way.

## 9. Cold-load cost uses one fleet-wide context size

On the 48 GB card Ollama gave `qwen3.5:9b` a 131,072-token context and `qwen3-coder:30b` took 31 GB,
against 20 GB on 24 GB cards. `_expected_num_ctx` (`model_preloader.py:270-278`) returns the
override or `_observed_ctx`, keyed by model only (`:131,178`), so the last node to report wins and a
48 GB node's cost is estimated from a 24 GB node's context (or the reverse). Item 3's "fits without
evicting" would be wrong exactly on mixed-card herds.

**Change:** key `_observed_ctx` by (node, model), falling back to the model-level value; pass `node`
into `_expected_num_ctx` and its three callers (`scorer.py:658`, `model_preloader.py:510`,
`routes/fleet.py:143`). Fold `streaming.py:1058` `_resolved_num_ctx` into
`model_preloader.py:253` `_num_ctx_override`, which it mirrors. The learned KV cost per token
(`:119,187`) is per model and stays.

**Tests:** `test_model_preloader.py:620-744` (`_kv_node`, `test_observe_kv_cost_records_default_context`):
two nodes reporting different contexts for one model, each costed by its own.

## 10. Router overhead

**Seen:** 6.5 ms p50 on a Mac's loopback; 24.4 ms p50 / 27.7 ms p95 between two datacenter machines,
where the direct path crossed the same network, so nearly all of it is the router. Published Python
gateways add 2-7 ms p50 against a mock.

**Build on:**
- Timestamps already exist and are thrown away: `InferenceRequest.created_at` (`request.py:81`),
  `QueueEntry.enqueued_at` / `started_at` (`:123-124`, set at `queue_manager.py:400`). Scoring ≈
  `enqueued_at − created_at`, queue wait = `started_at − enqueued_at`. Trace `latency_ms` and TTFT
  start at the worker (`streaming.py:283,379-380`; retries `:556,752`), so they exclude both.
- `fleet_headers(extra=…)` (`fleet_headers.py:19-75`) is the one header builder; new headers also go
  in `cors.py:30-45` `EXPOSED_HEADERS`.
- `_request_meta` / `pop_request_meta` (`streaming.py:221`) already carries per-request facts to the
  response (`ollama_compat.py:48`).
- Columns are added with `try: ALTER TABLE … ADD COLUMN` (`trace_store.py:144-172`).
- `StreamingProxy._get_client` / `_invalidate_client` / `close` (`streaming.py:225-263, 2022`) pools
  one client per node.

**Change:**
1. One JSON `timings` column on `request_traces` (scoring, queue wait, upstream connect and first
   byte, total), added the existing way, written by `record_trace` (`:278-361`). Not
   `scores_breakdown`, whose readers sum every key (`scorer.py:105-118`, `fleet_headers.py:78`,
   `streaming.py:697`).
2. `X-Fleet-Overhead-Ms` through `fleet_headers(extra=…)`: total minus upstream for non-streaming
   responses; the part before upstream for streams (headers go out before the body,
   `openai_compat.py:485-497`); the full figure always in the trace. `scripts/router_overhead.py`
   reads it.
3. Extend `_get_client` to key on (node, port), keeping its URL-change check, and use it for the four
   per-request clients: vision embed (`streaming.py:1283`), speech-to-text (`:1313`), image
   (`:1396`), native text embed / rerank (`text_embedding_compat.py:230`), plus `/api/version`
   (`ollama_compat.py:193`). That folds the four copies of "host from `ollama_base_url`, default
   port" (`streaming.py` ~1278, 1307, 1391; `text_embedding_compat.py:209-212`) into one.
4. Then read the timings from a real run and fix what they show.

**Fix alongside:** `streaming.py:1873` fetches image URLs with a synchronous `httpx.get` inside
`_convert_messages_for_ollama` (`:1830`) on the request path, blocking the event loop for every
concurrent request: make it async through the pooled client. Responses that skip the canonical header
set (`anthropic_compat.py:316-320`, `ollama_compat.py:472,670,675,708`, `openai_compat.py:713`'s
non-standard `X-Fleet-Model`) move to `fleet_headers`.

**Tests:** `test_fleet_headers.py:13-55`, `test_trace_store.py:75-150`. Four fixtures monkeypatch
`httpx.AsyncClient` at module level and break once clients are pooled
(`test_native_text_proxy.py:40-53`, `test_embedding_shapes.py:76`, `test_client_compat.py:518-529`,
`test_vision_embedding_traces.py:52-62`): move them to the `_install_mock_ollama` pattern
(`test_client_compat.py:77-84`).

## 11. Run 2's method

Overhead the way gateway vendors measure it: a mock upstream with a fixed delay, direct vs through
herd, at fixed arrival rates (k6 `constant-arrival-rate` or vegeta) of 10 / 50 / 100 RPS, thousands of
samples, p50/p95/p99 and the rate where errors start; plus the real-model A/B with far more than 20
pairs, reading `X-Fleet-Overhead-Ms`. A prefix-sharing workload (multi-turn agent sessions with a long
shared system prompt), herd against plain round-robin on the same machines: published routing gains
(SGLang, llm-d) all come from cache reuse, which run 1's unique prompts could not show. One
`llama-bench` per GPU type for the card's own ceiling.

## Debt found by the audit (fix in release A unless noted)

| Where | What |
|---|---|
| `node/agent.py:725-756` `_restart_ollama` | Under systemd the router's `restart_ollama` command (`context_optimizer.py:245`) sets `os.environ` in herd-node (`:745-748`) and restarts nothing; with item 1, `_ollama_env` would then report a value Ollama never received. Only apply env overrides to an Ollama the node itself spawned; otherwise report that it cannot restart a service Ollama |
| `config.py:27` `score_role_affinity_max` | Never read (only displayed, `dashboard.py:1322`): delete |
| `health_engine.py` NUM_PARALLEL text ×3, `_NATIVE_TEXT_EMBED_MODELS` | Items 1 and 2 |
| `benchmark_runner.py:316` | `NameError` on a run without LLMs (item 5) |
| `trace_store.py:1244-1281` | Benchmark `workload` and `mode` silently dropped (item 5) |
| `ollama_compat.py:849-850` | 404 instead of 503 for a bare-name miss (item 2) |
| `streaming.py:1873` | Blocking image fetch on the event loop (item 10) |
| `routing.py:777-810` `_pick_pull_node` | Weights instead of fit cost; `model_size` leaks across iterations (item 3, release B) |
| `routes/fleet.py:57` | Documents a field `/fleet/limits` does not return |

## Order

1. **Release A:** debt table; items 1 (steps 1-3, 5), 2, 5, 6, 10 (steps 1-3); then item 1's warning
   (step 4, needs item 10's timings). Then read the timings and fix the 24 ms.
2. **Release B:** items 9, 3, 4, in that order (4 reads 3's `eviction` signal; 3's fit check needs 9's
   per-node context). One short design note with the eviction weights worked through on run 1's two
   cases before code.
3. **Run 2** (item 11) on the same hardware, stock node configuration (systemd unit only, no
   fastembed): one node scales with concurrency; embeddings succeed without the extra; the alternation
   test settles one model per node.

## Decisions

Decided by the maintainer, 2026-11-20:

1. **Substitution:** when a node can load the requested model without evicting, load it there.
   Same-category substitution stays only for "every load would evict". Cross-category substitution
   is **removed** (no setting, per the greenfield rule; this replaces "off by default behind a
   setting" from the first draft), and the `vram_fallback` setting goes with it; strict mode stays
   per request.
2. **Embeddings:** Ollama serves `nomic-embed-text` only while no node has ever served it natively,
   read from the trace store so it survives a restart.

Open: release A as 0.12.10, together with the VPN-address fix already on `main`?
