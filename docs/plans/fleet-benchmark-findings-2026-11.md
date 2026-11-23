# Fixes from the first fleet benchmark (2026-11-20)

**Created:** 2026-11-20 · **Revised:** 2026-11-20, after a code audit of `main` (file:line below are
`main` at `b156472`) · **Status:** ✅ **implemented on `main`, unreleased** (2026-11-20; suite 2334
passed). Open: run 2 (item 11), item 10 step 4 (fix what run 2's timings show), investigations 7-8,
and the release decision (end). Implementation notes are in "Implemented" just below.
**Source:** a benchmark of herd 0.12.9 with Ollama 0.40.16 on rented NVIDIA machines (four Quadro
RTX 6000 24 GB and one RTX A6000 48 GB, Ubuntu 22.04, Ollama as a systemd service, one private
network), using `scripts/benchmark.py` at `56993ff` with `--prompt-tokens 1250 --max-tokens 200`
and `docs/experiments/model-alternation-benchmark.py` (since folded into the engine:
`scripts/benchmark.py --sequence alternate`).

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
    **Re-measured on 0.12.10 (2026-11-20, `docs/experiments/router_overhead_results.json`):** 6.3 ms
    p50 over the network (was 23.8), header 6.0 ms; +6-7 ms through the mock up to 125 req/s, a knee
    at ~175 req/s, failures from 300 req/s, on the same Lambda vCPU. Three things learned: (a) run 2's
    "collapse at 50-80 req/s" was partly herd's own per-model queue cap (the mock node reported the
    systemd unit's 4 slots, and 4 slots at ~31 ms carry ~130 req/s), so the clean ceiling needs a node
    with a high `OLLAMA_NUM_PARALLEL`; (b) the event loop still spends ~4.5 ms of CPU per request on
    that vCPU (~2.4 ms on an M-series Mac), with three aiosqlite threads at 5-20 % each; (c) at
    overload the node's heartbeats fail too (17 `Heartbeat cycle failed` at 300+ req/s), because
    the router serves them on the same saturated loop. Open: profile the event loop per request
    (py-spy shows aiosqlite's C time but not which store), and keep heartbeats answerable under load.
    **Reproduced locally 2026-11-20** (`docs/experiments/router-scale.sh`,
    `router_scale_results.json`): 64 fake nodes on one Mac, 0.12.10 router; at 300 req/s heartbeats
    were fine, at 600 req/s (past the Mac's ceiling) they waited up to 6.2 s (p99 133 ms) and 2
    failed. The fix can be built and checked there, no cloud machines needed. Up to 256 nodes at
    50 req/s nothing failed and scoring took 0.2-1.5 ms, so the problem is request load, not
    fleet size.
13. **Shared-prompt sessions: first turn ~2x slower through herd.** 16 sessions over 4 nodes: herd's
    median TTFT 1.4-1.5 s against round-robin's 1.07 s, almost all of it on first turns (1.3 s
    against 0.66 s), although herd's own queue wait was 3 ms. Later turns favour herd (+26 % growth
    over four turns against +171 %), and p95 is 23 % better. Read Ollama's per-request
    prompt-eval counts and slot assignment for first turns placed by prefix affinity.
    **Still open (2026-11-22).** Related, not a fix: session and prefix affinity now fade by
    waiting against re-reading in seconds (`e42e3a9`), and prefill time is kept per request, which
    gives this item the per-request prompt-eval data it asks for. First turns not re-measured.

## From the website's benchmark review (2026-11-20)

The private plan `benchmark-page-feedback.md` (ollama-herd-private) triaged three reviews of the
published benchmark and audited this repo's tooling. Its OSS tooling items landed on `main`:
one `model_type()` classifier (`a3fc4db`), `X-Request-Id` + filtered `/dashboard/api/traces`
(`7111ee4`), one percentile function (`796fd4b`), per-request records / manifest / model events /
open-loop and seeded workloads / vision work in the benchmark engine (`4243946`, `aa652ef`),
`agent_version` in `/fleet/status` (`c688882`), fake herd-nodes and a fuller mock Ollama
(`3278ebd` and after). Items it hands to this plan:

14. **Error-class test matrix.** Community telemetry's errors (30 days to 2026-11-20, 0.7% of
    392,874 requests): `model_not_found` 55.5%, `bad_request` 30.1%, `context_too_long` 5.7%.
    Mostly client errors, so the question is whether herd's answer is correct and actionable.
    Map each matrix onto the existing tests first, add only gaps:
    - model not found: on no node / on disk not loaded / on one busy node / tag omitted or
      `latest` / alias differs between nodes / strict mode on and off / holder disconnects;
      the error names what *is* available; no wrong substitution in strict mode
      (existing: `test_backend_model_not_found.py`, 42 tests);
    - bad request: one parametrized matrix over the four protocols plus embed, vision, STT
      (malformed tools, missing fields, unsupported content blocks, oversized base64, invalid
      stream options) built on the existing compat fixtures, not a new suite;
    - context too long: prompts at 90/100/101/125% of a model's window, rejected before dispatch
      or sent to a node with a bigger window (existing: `test_context_protection_every_format.py`,
      `test_context_window_visibility.py`).
15. **Fixed (`ffa2f1f`): image generation and transcription wrote no trace row.** Since `7111ee4`
    they returned `X-Request-Id`, but `/dashboard/api/traces?request_id=` found nothing for them,
    and they were missing from the dashboard's usage and from community error counts. They are
    queue-dispatched, so they trace the way the queue path does (`StreamingProxy._record_trace`,
    from one helper, `_run_media_job`), not with `direct_proxy_tracer`: `completed`, `failed`,
    and `client_disconnected` when `first_chunk` cancels the job (a client that leaves while
    queued was already traced by `dispatched_stream`). The routes add the disconnect to the
    in-memory image/transcription events as well. No double count: the community rollup reads
    only `request_traces` (and `latency_observations`, which these jobs never wrote), and the
    per-type stats read only their own events. The trace's model is the queue entry's name,
    which `InferenceRequest` normalizes (`z-image-turbo:latest`), as the queue key already was.
16. **Fixed: dashboard benchmark runner can't select vision or embed-image work.** The runner
    accepted both types since `4243946`; the dialog now offers "Vision" and "Vision embeddings"
    (unchecked: both are opt-in). A vision-capable chat model (gemma3, llava) runs the text
    workload under "Chat" and the image workload only with "Vision" (`select_workloads`); before,
    `model_type()` saying `vision` made the dashboard's default `["llm"]` drop it and the CLI send
    it an image instead of the `--prompt-tokens` text.

Found by the local smoke test (`scripts/fake_fleet.py`, 8 fake nodes on one `scripts/mock_ollama.py`
with runtime faults, against a real router). All fixed on `main`, unreleased:

17. **Fixed (`c0dd89b`): `X-Fleet-*` named the first-choice node, not the one that served.** Every
    route built `X-Fleet-Node` / `X-Fleet-Retries` / `X-Fleet-Score` before reading the stream,
    while `_stream_with_retry` retries on other nodes producing the first chunk. `dispatched_stream`
    now returns after the first chunk (`first_chunk`, still cancelling if the client leaves), and
    routes build headers from the queue entry (`served_headers`). Re-run: 20 streaming `/api/chat`
    at a 50% 500 rate, headers agreed with `/dashboard/api/traces?request_id=` 20 of 20 (served
    after 0, 1 and 2 retries, and 502s after 2).
18. **Fixed (`c0dd89b`): a streaming request that failed before its first chunk was a 200.** All
    attempts failing (or a backend 4xx) returned 200 plus an error event. One mapping on every
    surface, streaming or not (`backend_failure_response`): the backend's 4xx, else 502, in the
    surface's error shape; the MLX passthroughs too (`first_mlx_chunk`). After the first chunk the
    stream still ends in-protocol. Streaming headers now arrive with the first token.
19. **Fixed (`c0dd89b`): Ollama's queue-full 503 came back 502 (non-streaming OpenAI, Anthropic,
    Responses) or 200 (streaming).** Now `429` + `Retry-After: 10` on all four surfaces, both modes
    (re-run: 8 of 8).
20. **Fixed (`8a777aa`): `/fleet/status` `nodes_online` counted only ONLINE** while routing sends to
    DEGRADED too. `nodes_online` is "routed to" in `/fleet/status`, `/fleet/queue` and the health
    vitals; `/fleet/status` adds `nodes_degraded`; the dashboard figure still warns on a degraded
    node. Re-run: 2 nodes killed, read 11 online / 2 degraded until they went offline (8 / 0).
21. **Fixed (`9a4b870`): a failed request's trace `retry_count` counted attempts.** Rows now read
    0, 1, 2 (retries before that attempt); `X-Fleet-Retries` is the serving attempt's value; the
    dashboard and health retry totals sum terminal rows only; the embed path follows the same rule.
    (That sum was right fleet-wide but credited retries to the serving node: replaced in item 26.)
22. **Not reproduced as reported (`e60f8d8`): "the router keeps a node's first backend URL".**
    `ollama_base_url` is rebuilt on every heartbeat; moving a fake node from one mock port to
    another moved `/fleet/status` and dispatch after one heartbeat. The stale thing was
    `HardwareProfile.ollama_host`, copied at registration and never read: removed.
23. **After a retry, the session stays pinned to the node that failed.** Session affinity records
    the first-choice node, so the next turn of that conversation tries the failed node first.
    Re-pin to the node that served (the queue entry knows it since `c0dd89b`).
24. **A retried request is counted twice in per-node queue stats**: failed on the first node,
    completed on the second. Count the attempt that served as the request; show the failed attempt
    as a retry, not a failure.

Found by an independent review of the fixes above (confirmed by experiment unless noted). Fixed
on `main` in `ffa2f1f`, unreleased:

25. **Fixed: with `FLEET_MAX_RETRIES=0` a client that left before the first chunk was traced
    `incomplete`.** `first_chunk` (from `c0dd89b`) cancels the stream, which arrives as
    `CancelledError`; `_stream_with_tracking` caught only `GeneratorExit`, so `finally` recorded
    `incomplete` and logged "was sent an error terminal". It now handles cancellation like
    `_stream_with_retry` (traced `client_disconnected`, re-raised); a test runs both paths through
    `first_chunk`.
26. **Fixed: per-node retry totals named the node that served, not the one that failed** (found by
    reading). Since `9a4b870` `_RETRIES_SUM` summed `retry_count` on terminal rows, which carry the
    serving node's id. A retry is now its `retried` row, on the node whose attempt failed, so the
    count is right per node and one per retry fleet-wide; the Ollama embed path, which retries a
    ReadTimeout on the same node and wrote no such row, now writes one. Fleet-wide request counts
    (`get_retry_stats_24h`, `get_overall_stats_24h`, `get_usage_overview`, the stream-reliability
    total) exclude `retried` rows, so `high_retry_rate` divides by requests: 4 retries in 12
    requests read 0.25 (under the 0.3 threshold) and now reads 0.33. Fallback totals count each
    request once. Per-node `request_count` still counts attempts, deliberately. Not changed:
    `get_error_rates_24h` still puts a node's `retried` rows in its denominator but not its
    failures, so a node whose attempts fail over reads a lower error rate than its attempts had.
27. **Fixed: an unhandled 500 had no `X-Request-Id`.** Starlette's `ServerErrorMiddleware` answers
    outside every `add_middleware` layer. `install_request_id_header` wraps the built middleware
    stack instead, which is safe because the layer only adds a header on `http.response.start`.

From run 4 on Lambda (2026-11-20, router at `dc5779c`; private notes in ollama-herd-private
`.bench-lambda-2026-11-20-run4`):

28. **Fixed (`4b020b5`, harness `3dd2230`): a load that had to evict a busy model never
    happened, and its requests starved.** Three nodes with `OLLAMA_NUM_PARALLEL=4` (two 24 GB cards, one 48 GB), four models,
    0.5 req/s open loop for 180 s, strict mode (the benchmark sends `X-Fleet-No-Fallback`):
    `qwen3-coder:30b` answered 2 of 27, 25 timed out at 300 s, and the whole run had 2 model loads.
    HAProxy least-connections answered all 27 (median TTFT ~50 s). **Not** "the router never takes
    the eviction": it did, every time (`Routing qwen3-coder:30b -> m1 (... evict=-40)`; with every
    option evicting and no other coding model, `routing.py:343` returns the cold result). Ollama
    could not carry it out:
    - Ollama evicts a busy runner by setting its `sessionDuration` to 0 and waiting on
      `unloadedCh` with no timeout (`processPending` in `server/sched.go`, on the scheduler's
      context, so a client giving up does not end it), and nothing else loads on that node
      meanwhile. Requests for a resident model skip that queue (`getRunner`'s fast path), and
      `useLoadedRunner` sets the runner's `sessionDuration` from the request's `keep_alive`.
    - herd sent `keep_alive: -1` on every request (`streaming.py:1855` and `:1865`,
      `anthropic_compat.py:292`, `ollama_compat.py:925`), so one request for the busy model during
      that wait re-arms it, it never unloads, and the node loads nothing more. Run 4's timeline
      fits: phi4 took m1 at about t=15 s; the coder requests sent to m1 from t=6 s, and a phi4
      request (t=8 s) that reached Ollama behind them, timed out at 300 s while later phi4
      requests were answered in 1 s, and m2 did the same from t=20 s (a qwen2.5 request at
      t=24 s among them); the benchmark saw two model loads in 180 s. HAProxy sends no
      `keep_alive`, so its evictions completed.
    - Even without the re-arm the wait was unbounded: the node is assigned at enqueue
      (`queue_manager.py:338`), nothing re-scores a queued request (only a failed attempt is,
      `streaming.py:785`), and herd kept dispatching the busy model hot (+50) to the node that had
      to drop it (`queue_manager.py:404`), so it went idle only by chance.
    - Stranding (`dc5779c`) was a charge of 50 against 40 for a busy model, and up to 55 points of
      queue and wait penalty outweigh it: harmless while evictions never completed, but once they
      do the coder was sent to the 48 GB card and evicted the 35 GB model.

    **Fix**, in the queue (where requests wait), the scorer and the request body:
    1. `keep_alive: -1` is left off while another model's request on that node is at Ollama and
       still waiting for its first token (`QueueManager.load_pending_on`, read in
       `_build_ollama_body`, the embed path too); the translator no longer adds its own. Decided
       from herd's queue, not residency: heartbeats lag a load by up to an interval, and a first
       version that sent it "only on loads" still wedged a node when a request herd took for a load
       hit a model that had just loaded. The runner keeps the `-1` it was loaded with.
    2. `QueueManager._must_wait`, before each dispatch. **A load waits its turn:** a request that
       must evict (`eviction < 0`, or routed hot and its model evicted since) on a node whose models
       are all busy stays in herd's queue until one goes idle (Ollama evicts an idle one at once),
       or for at most `_LOAD_WAIT_SECONDS` (30 s); requests for the same model join it and go with
       it. **Then it gets the room:** while it is at the node without output, new requests for that
       node's resident models are held, so the ones already running are the last. Held requests
       count in the queue depth. Nothing waits beside an idle model or for a load that fits.
    3. Scorer: requests for a model already queued or in flight on a node it is not resident on
       join that load (`joins_load` offsets the eviction, which stays in the breakdown for the
       queue) instead of each starting a load, and an eviction, elsewhere; a node whose load would
       strand a model is dropped from the candidates while any other can serve.

    **Local reproduction** (no GPUs): `scripts/mock_ollama.py` now schedules loads as `sched.go`
    does (one queue, a load waits for a busy model, a `keep_alive` re-arms it; `/_mock/state`
    shows the model loading) and `scripts/fake_fleet.py --follow-ps --gpu-gb G` reports each mock's
    `/api/ps` and its card memory as nvidia-smi would. Three mocks (`--max-loaded 1`, ~8 s loads
    and replies; the 32b only on the 48 GB node, ~80 s replies), three fake nodes, a real router,
    run 4's benchmark command (seed 7, 0.5 req/s, 180 s, 300 s timeout):

    | Model | before (`8eae448`) | after |
    |---|---|---|
    | qwen3-coder:30b | 27/27, 0.5 / 0.5 s | 27/27, 2.0 / 57.8 s |
    | phi4:14b-q8_0 | **2/23**, 12.3 / 12.3 s | 23/23, 32.3 / 94.8 s |
    | qwen2.5:14b-instruct-q8_0 | 22/23, 0.5 / 8.1 s | 23/23, 44.0 / 80.6 s |
    | qwen2.5:32b-instruct-q8_0 | 17/17, 70.1 / 166.1 s | 17/17, 70.1 / 166.2 s |

    Answered, TTFT p50 / p95. Before, the model that lost its card first starved, as on Lambda
    (here phi4, not the coder: the mock's timings order the first evictions differently), with
    two loads in the whole run and one qwen2.5 request lost too; after, all 90 answered (the
    slowest small-model first token 97 s, against a 300 s timeout) with 23 loads, and the 32b was
    never evicted. The price is visible: the two 24 GB cards now swap among three models (a load
    per card every 10-15 s), so the models that had a card to themselves got slower. Under strict
    mode that is the only way to serve all three; HAProxy least-connections on the real fleet
    swapped constantly too (medians 26-50 s) and lost 14 of 17 32b requests. What did not work on
    the way, for whoever tunes this next: the drain hold alone (at a 1/5 time scale) made evictions
    complete, so before stranding became a filter the coder evicted the 32b (1 of 17 answered) and
    the cards swapped constantly; deciding `keep_alive` from residency wedged a node again at full
    scale (a mock showed phi4 idle with a coder load stuck behind it; 20/27, 19/23, 16/23
    answered). Open: confirm on the Lambda fleet with run 4's command.

29. **Open: a node swaps two models per request instead of batching** (run 4, router `1e7cdb2`,
    0.5 req/s seed 7: 76/90 answered, m1 swapped coder <-> phi4 every 6-15 s). `_must_wait`
    measures the 30 s bound from each request's arrival, so requests held during a load are
    already past it when the new model answers and evict it at once; the node's next heartbeat
    still lists the evicted model, so new requests for it are routed there as if resident. A
    "switch turn" (bound counted from the later of arrival and the node's last switch; only the
    switched-to model counts as resident until a newer heartbeat) cut loads 26 -> 18 and small-model
    p95 from 86-130 s to 34-53 s at 0.5 req/s on the local mock, but doubled p95 at 0.25 req/s (a
    lone waiting request sits out a full turn). Needs a backlog condition (waiting model's held
    count against the resident model's arrivals) and repeat runs before it ships. Patch and local
    runs: ollama-herd-private `.bench-lambda-2026-11-20-run4/switch-turn.patch` (applies to `1e7cdb2`).
    **Measured again 2026-11-22, not shipped.** Ported onto `main` with the backlog condition (the
    turn counts from the switch only while the switched-to model has requests waiting), then an A/B
    on the local mock (two nodes at `--max-loaded 1`, 8 s loads, two models alternating, 150 s,
    seeds 7 and 11, before = `e42e3a9`):

    | rate, seed | loads before -> after | p95 a / b before | p95 a / b after |
    |---|---|---|---|
    | 0.25, 7 | 33 -> 29 | 19.6 / 18.5 s | 19.6 / 18.8 s |
    | 0.25, 11 | 27 -> 46 | 19.6 / 18.8 s | 15.9 / 15.9 s |
    | 0.5, 7 | 67 -> 61 | 186 / 173 s | 173 / 200 s |
    | 0.5, 11 | 59 -> 59 | 148 / 161 s | 169 / 178 s |

    All requests answered in every cell. The backlog condition did remove the p95 doubling at
    0.25, but the load count moved both ways and p95 at 0.5 got worse in three of four
    comparisons, so the turn was taken back out. What stays from the port is
    `QueueManager.progress`, the one hook both streaming loops call, which now counts streamed
    chunks for the dashboard. Next attempt should measure on a mock with item 28's timings and more
    repetitions per cell before reading a difference.
30. **Fixed on `main` (`dbed3d7`, unreleased): `joins_load` cancels the eviction charge for any queued depth, not only a load under
    way.** Queue depths now carry `loading` (`QueueDepths`), and `joins_load` applies only to a load under way. Not yet re-measured against run 4's churn. On run 4 that kept phi4 on m1 (score 0 + 10 cold, minus queue penalties) while evicting
    qwen2.5:14b on m2 cost 40 whenever one of its requests was in flight, so the churn never spread
    to the second card. Apply `joins_load` only while that model's load is at the node without
    output; the scorer needs a "load under way" input from the queue manager (it only sees
    combined depths today).
31. **Fixed on `main` (unreleased), from run 5 (2026-11-21, released 0.12.11): a model's first
    routing decision could strand it for the whole run.** Cause: `dispatch_embed` sent embeddings
    straight to Ollama, outside the queue manager, so the scorer saw `queue=0, wait=0` on a node
    with a backlog of them; now `QueueManager.direct` counts each in its node's depth while it runs
    (not as an in-flight entry, so it is never taken for a load).
    **Hardware check (2026-11-21, 2 x A10 24 GB + A100 40 GB, Ollama 0.40.17, vision workload,
    raw data in ollama-herd-private `.bench-lambda-2026-11-21-vchk/`):** forced stranding (nomic
    primed onto an A10 through the router, then loaded on the A100 directly): 0.12.11 sent 4 and 0
    embeddings to the idle A100 (10,248 and 12,742 in all), the fix 14,121 and 14,325 (24,606 and
    25,497). The first version (`4c3b7f1`) also reported embeddings in `get_queue_info`'s
    `in_flight`, which the rebalancer's pre-warm trigger reads: it warmed nomic onto an A10 serving
    the coder 43 times in three normal runs (0 on 0.12.11) and coding requests fell to a median of
    150 from 173. `b6ee0eb` reports them as `direct` instead: 0 such pre-warms. **Open:** with
    `b6ee0eb` coding requests were still 154, 150, 160 against 0.12.11's 162, 183, 173, with total
    chat throughput equal (mean 400 against 401 tok/s, old rate definition; the in-window correction
    lowers both by the same ~4) and image requests up 19%; the difference is
    all on m1 (router, harness and node on one machine; m2 identical at 76 coding requests every
    run). **Isolating check (same day, router and harness on a fourth A10 with no models, 0.12.11
    and `b6ee0eb` alternating, three runs each, raw data in `.bench-lambda-2026-11-21-vchk2/`):**
    the gap stayed (coding 147, 171, 181 against 146, 145, 148; total chat throughput 400.6 against
    400.4 tok/s; embeddings all on the A100 in every run), so it was not the shared machine. It
    follows image requests spilled onto m1: 14, 20 and 0 on 0.12.11 against 45, 46 and 38 with the
    fix, and m1's coder decode fell with them (73.8 tok/s at 0, about 50 to 57 at 38 to 46). See
    item 32 for the spill itself, which both versions do.
32. **Fixed on `main` (unreleased), verified on hardware, from the item 31 checks: image work
    spilled onto a card where the image model runs partly on the CPU.** Combined check (both fixes,
    `213cdb7`, against 0.12.11, alternating, three runs each, router and harness on their own
    machine, raw data in `.bench-lambda-2026-11-21-vchk3/`): image requests sent to the A10s
    45, 57, 44 → 2, 2, 2; chat throughput 333, 404, 402 → 500, 506, 503 tok/s (median +25%; in-window, corrected 2026-11-23, about
    329, 399, 398 → 496, 501, 499); first-token
    p95 648, 575, 577 → 71, 71, 75 ms; coding requests 146, 146, 150 → 174, 176, 176; image
    requests median 4,326 → 5,723; embeddings unchanged (all on the A100); no failures. The
    offloaded copy is still loaded (pre-warm put it there in every run); it just gets no work. `LoadedModel.offloaded`
    (`0 < size_vram < 90%` of size, already in heartbeats since 0.12.9) scores the model as cold. On an A10 next to qwen3-coder:30b (20 GB) moondream loads 59% CPU / 41%
    GPU (`ollama ps`), and its llama-server then burns about 24 cores beside the coder. The scorer
    still sends it image requests whenever its moondream queue is empty: score 88 (queue 0) beats
    the A100's 82 (one in flight), so image work leaves the card that serves it in about 70 ms for
    one where it takes seconds and slows the coder. Herd does not see CPU offload: heartbeats carry
    loaded models, not how much of each is on the GPU (Ollama's `/api/ps` has `size_vram`). Fix
    direction: report each loaded model's GPU share from the node and score a partly offloaded
    model as cold (or not a candidate while a fully resident one can serve), then re-run the
    vision check: coding requests should match 0.12.11 or beat it. Vision test on two A10 24 GB (m1, m2) and one A100 40 GB (m5),
    each run starting from a reset that loads nomic-embed-text and moondream on m5 and the coder on
    m1 and m2, `--skip-warmup`. In repetition 1 the router's view of m5 did not yet list nomic (the
    reset had just loaded it; heartbeats are every 5 s), so the first embeddings went to m1 cold
    (score 53). After that every one of 360 nomic routings went to m1 (affinity, then the model
    "on its way" there), where the coder, moondream and nomic do not fit a 22.5 GiB A10 together:
    each embedding waited about 15 s and 50 were answered in 180 s. In repetitions 2 and 3 the first
    embedding went to m5 and 10,270 and 10,306 were answered at about 70 ms. Nothing re-scored m1
    against m5 once m5's heartbeat listed nomic hot and idle. Related to item 30. Data: run 5
    `b8_three_r1` (OSS `docs/experiments/fleet_run5_results.json`, vision), router log in
    ollama-herd-private `.bench-lambda-2026-11-21-run5/journals/m1-router.log.gz`.

### Run 6 (2026-11-21/22): two Apple M4 Macs on AWS EC2

Two `mac-m4.metal` Dedicated Hosts (Apple M4, 24 GB, 120 GB/s; the M4 Pro first choice had no
capacity in any of 11 zones), router, HAProxy and harness on a c7i.xlarge in the same subnet,
Ollama 0.40.13 as a LaunchDaemon, `qwen2.5:7b` (`qwen3-coder:30b` does not fit an M4's GPU),
about 1,250 prompt and 200 completion tokens per request. Manifest and results:
`docs/experiments/fleet_run6_manifest.json`, `fleet_run6_results.json`. Figures to quote are the
cold-cache arms (item 35).

33. **Fixed on `main` (`6dbf115`), found in run 6: herd held every small machine to one request
    at a time.** `QueueManager._compute_queue_concurrency` took `(available_gb - model size) / 2 GB`,
    but `available_gb` is read after Ollama loaded the model and the KV for all its slots: on a
    24 GB M4 (8.47 GB free, 5.22 GB model) that is 1 against Ollama's 4. Per-request decode stayed
    at 22.4 tok/s at every concurrency while first-token time grew linearly (5.3 s at 1, 83.8 s at
    8), and herd's own traces showed `queue_ms` 27 s against `backend_ms` 9 s. A resident Ollama
    model now gets `decode_parallelism_for`. One Mac at concurrency 8 went from 18-20 to 28
    requests per 120 s, and two Macs scale 2.0x at saturation. Invisible on the 512 GB Mac Studio.
34. **Fixed on `main` (`87e3bf0`), found in run 6: a full Mac refused a model it could make room
    for.** The "can it run here at all" gate (`scorer._eliminate`, `_pick_pull_node`) read memory
    free now, which on unified memory already excludes the loaded models. s1 with `qwen2.5:7b` and
    `qwen2.5:14b` idle (3.0 GB free) answered a cold `gemma3:12b` with 30 s of `All 2 nodes
    eliminated` and a 503 "try again shortly". Now the gate adds what evicting frees; the eviction
    stays a score. Re-run on the fix: `qwen2.5:14b` into a Mac holding `qwen2.5:7b` + `gemma3:12b`
    (3.5 GB free) answered 200 in 17.7 s. **Side effect, recorded in item 39:** it also let a
    second load of a model go to a node that must evict, where the old gate's refusal had happened
    to force a better layout.
35. **Fixed in the harness (`541f0db`, unreleased): Ollama's prompt cache carried between back-to-back runs that reuse a seed.** `scripts/benchmark.py --run-seed` derives each cell's seed (`cell_seed`); `--seed` keeps its meaning. Runs 2-5's scaling cells remain possibly warm.
    llama.cpp keeps up to 8 GiB of earlier prompts in host RAM, and the harness's prompts repeat
    by seed, so a cell run straight after another with the same seed starts warm: `scaling`
    cells at concurrency 8 ran after concurrency 4 with the same seed (18-29 of 56-60 requests
    with a first token under 1 s), and a run with seed 13 twenty minutes after another got 71
    requests against 36-40 cold. The same order was used in runs 2-5, whose scaling cells may be
    inflated the same way. Run 6 restarted Ollama before every cell from the saturation arm on.
    Fix: restart Ollama between cells in the method, or a harness seed that varies by cell.
36. **Fixed in the summarizer (in-window figure), from run 6: `tokens_per_sec` counts requests that
    finish after the window.** At concurrency 32 on 4 slots the slowest first tokens arrived at
    151 s in a 120 s cell, and their tokens were divided by 120 s, so the figure kept rising past
    saturation (two Macs: 66.7 at 16, 93.3 at 32, at the same 4 slots). `fleet-summarize.py` now
    also reports `chat_tokens_per_sec_in_window` / `tokens_per_sec_in_window`, from requests that
    finished inside the load phase: 40.0 and 40.0 for those two cells. The old figure stays, so
    runs 2-5 remain comparable; compare across concurrencies with the in-window one.
37. **An M4 saturates at 4 parallel requests.** In-window chat throughput, cold cache, three
    repetitions each: two Macs with 4 slots 40.0 tok/s at concurrency 16 and 32; with
    `OLLAMA_NUM_PARALLEL=8` (`-c 32768 -np 8`, herd following with concurrency 8 on its own) 40.0
    at 8 and 36.7 at 16 and 32. One Mac with 8 slots: 18.3, so two Macs are 2.2x one. More slots
    only slow each request. 4 is the right setting for this class of machine at this workload.
38. **Cold-cache head-to-head on identical Macs: a tie.** Herd (`6dbf115`) 32, 32, 32 requests
    against HAProxy roundrobin 32, 32, 32 (40.0 tok/s in-window each), 0 failures, Ollama restarted before
    every run, each pair on one seed. The earlier gap (0.12.12: 56.7 against 75.0) was item 33 plus
    item 35's cache order. One model on identical machines is round-robin's best case.
39. **Fixed on `main` (`923b7c4`, `e9d0054`, unreleased): a pre-warm that must evict blocked every load on its Mac.** Pre-warms count as in-flight loads and are not sent when they would wait for a busy model; fit is judged against Metal's working set. Seed 13 on the M4s still to be repeated. Mixed models (seed 13,
    router `87e3bf0`): the rebalancer pre-warmed `gemma3:12b` onto s1; Ollama had to evict the busy
    `qwen2.5:7b`, which it does by waiting for it to go idle while loading nothing else; herd kept
    sending it work, since its load hold only covers loads in its own queues; Ollama's scheduler on
    s1 logged nothing for 7 minutes and 11 of 27 requests timed out at 300 s. Herd's "fits without
    evicting" judged by available RAM, Ollama by Metal's working set (17.8 of 24 GiB) and KV x
    parallel. Evidence and fix directions in `docs/issues.md`.
40. **Session affinity: +43% on multi-turn traffic.** Four-turn sessions over a 1,500-token system
    prompt, 8 at once, cold cache: herd 80, 80, 80 requests in 180 s against 51, 56, 64 sent
    round-robin to each Ollama, 0 failures. Keeping a conversation on the Mac holding its prefix
    skips its re-prefill; round-robin pays it on most turns.
41. **Failure handling holds on Macs.** 8 concurrent for 240 s, Ollama stopped on s2 at about 60 s,
    herd-node killed 90 s later, three repetitions: 4 failures each, exactly the 4 streams in flight
    on s2 (they cannot be retried after the first chunk); the next requests got `ConnectError` and
    were retried on s1; the node kill cost nothing.
42. **First sighting of critical memory pressure on a real fleet.** During the mixed-models arm
    macOS reported critical pressure on s2, and herd logged `Eliminated s2: critical memory pressure
    and phi4-mini:latest is not resident (cold load withheld)` while still serving its resident
    models, as the 2026-10-02 rule intends. The deliberate pressure test on s1 never got past
    `normal` (3 GB free).
43. **Fixed on `main`, found in run 6:** the dashboard's embedding cards showed the fleet total on
    every node (`82fdf26`); the harness could not see herd's native text embeddings and silently
    ran the embeddings arm without `nomic-embed-text` (`6a588a8`), warmed them at Ollama and
    dropped them (`1445755`), and now fails instead of shrinking when a requested model is missing
    (`006de5a`); vision embeddings retry once on a dropped kept-alive connection (`eeff6d1`, 3 of
    ~23,000 in the full-load demo); `herd-node pull-vision` exists and `/api/pull` names it instead
    of sending vision models to Ollama (`b2c5d35`); a backend stream that ends with no output is
    retried on another node (`2b9ff02`, not in any measured router).
44. **Mixed models on two 24 GB Macs (cold start, open loop 0.15 req/s, four models, three seeds):**
    Herd completed every request on `6dbf115` (27/27 each, first token p50 11.0 s, p95 46.2 s,
    3 loads) and on `87e3bf0` in two seeds of three (p50 11.1 s, p95 44.5 s, 4 loads; seed 13 lost
    11 requests to item 39). HAProxy leastconn completed all with p50 48.4 s and p95 176.9 s and 16
    loads; roundrobin lost 2 and 9 requests to 300 s timeouts (median 25/27), p50 36.4 s, 11
    loads. In-window chat throughput: Herd 20.0 / 18.9, leastconn 14.4, roundrobin 16.7 tok/s.
    Placement is where herd earns its keep on Macs: 3-4 loads against 11-16, a quarter of the
    first-token time, and no timeouts except the pre-warm stall.
45. **Rate sweep, one model, two Macs, cold cache (two repetitions):** identical up to 0.2 req/s
    (first token about 5.4 s either way). Past capacity (~0.27 req/s): at 0.3 Herd p50 23.6 s
    against 26.6 s and p95 52.5 s against 47.6 s; at 0.4 p50 39.9 s against 61.1 s and p95 127.9 s
    against 115.7 s. Same in-window throughput (40.0 and 42.2 tok/s) and no failures on either.
    Herd queues the excess in order, so most requests wait less and the last ones wait longer.
46. **Native embeddings on Apple, with `nomic-embed-text` this time:** beside `qwen2.5:7b`, 4
    workers per model gave 5,852 requests per 120 s (median of three; e.g. 1,328 text embeddings at
    365 ms p50 and 4,513 vision at 105 ms), 16 workers 6,232; 0 failures in all six. Embedding
    throughput is near its ceiling at 4 workers; more workers mostly add chat.
47. **Soak: 4 hours, 1,728 requests, 0 failures** (chat on `qwen2.5:7b` and `qwen2.5:14b`,
    `nomic-embed-text`, DINOv2, 0.12 req/s), herd-node's own memory peak flat at 0.39-0.40 GB on
    both Macs throughout. **But on a 24 GB Mac Ollama's defaults run into swap:** each
    `llama-server` keeps up to 8 GiB of prompt cache (`--cache-ram`), which filled within about an
    hour (26% hit rate on unique prompts); swap reached 14.5 GB on s2 and 11.8 GB on s1, both Macs
    sat at `warn` pressure and dipped into critical several times, and `qwen2.5:7b` latency rose
    from 14.3 to about 17 s (first token 5.3 to 7.4 s) before levelling off. Nothing failed. For
    small Macs set `LLAMA_ARG_CACHE_RAM` (CLAUDE.md, prompt cache gotcha); the EC2 bootstrap
    should too. Two false starts before it, both recorded: the harness dropped
    `nomic-embed-text` (fixed), and the startup preloader loaded a third model onto a busy Mac,
    which swapped and answered with empty streams (open, `docs/issues.md`).

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
111.7 tok/s on one machine to 220.6 on two and 434.9 on four, with 0 failures. (Run 1's figures
use the old rate definition, which counted work finished after the window; its per-request data
was not kept, so they are not corrected. Run 2's in-window figures are 106.7 → 213.3 → 426.7, see
`docs/experiments/legacy_rate_corrections.json`.) But getting there took
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
  `docs/experiments/model-alternation-benchmark.py` (`:56`) did the same with its own few lines (it
  has since been folded into the engine as `--sequence alternate`).
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
