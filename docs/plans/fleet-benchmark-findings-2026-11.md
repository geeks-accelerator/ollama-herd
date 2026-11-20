# Fixes from the first fleet benchmark (2026-11-20)

**Created:** 2026-11-20 · **Status:** 📋 planned, nothing built. Release A (items 1, 2, 5, 6) is
small and independent; release B (items 3, 4) changes routing behaviour and needs the decisions at
the end first.
**Source:** a benchmark of herd 0.12.9 with Ollama 0.40.16 on rented NVIDIA machines (four Quadro
RTX 6000 24 GB and one RTX A6000 48 GB, Ubuntu 22.04, Ollama as a systemd service, one private
network), using `scripts/benchmark.py` at `56993ff` with `--prompt-tokens 1250 --max-tokens 200`
and `docs/experiments/model-alternation-benchmark.py`. Code references are to the `v0.12.9` tag.

## What the run showed

Scaling is real once the router is allowed to use each machine: `qwen3-coder:30b` went from
111.7 tok/s on one machine to 220.6 on two and 434.9 on four, with 0 failures and the router adding
24 ms per request. But getting there took a workaround, and three of the four tests hit a bug:

| # | Problem | Who it hits | Size | Release |
|---|---|---|---|---|
| 1 | Router serialises every node whose Ollama has `OLLAMA_NUM_PARALLEL` set in a systemd unit | Linux users who raised parallelism the documented way | S | A |
| 2 | `/api/embed` (and `/v1/embeddings`, `/api/embeddings`) for `nomic-embed-text` 503s when no node has fastembed, even with the model loaded in Ollama | Every pip/Homebrew install without the `embedding` extra, and every container | S | A |
| 3 | Placement ignores what a cold load evicts, and the warm bonus rewards the node that just evicted | Anyone with more models than one node holds | M | B |
| 4 | `vram_fallback` (on by default) answers a request for a model that is not loaded with *any* loaded model, across categories, even when another node could load it into free memory | Every multi-model herd | M, behaviour change | B |
| 5 | Benchmark harness: warms one node, requires models to be loaded, cannot demand the exact model | Our own measurements | S | A |
| 6 | Docs say things the code does not do | Readers of the Docker and Linux guides | S | A |

Items 7-9 below are investigations, not fixes.

---

## 1. `OLLAMA_NUM_PARALLEL` is invisible on Linux when Ollama is a systemd service

**Seen:** one node, `OLLAMA_NUM_PARALLEL=4` in `/etc/systemd/system/ollama.service.d/override.conf`
(the method Ollama's Linux docs give). Throughput was flat at 77 tok/s from 1 to 8 concurrent
requests and time to first token grew by one whole request per step (18.6 s at 8). `/fleet/queue`
showed the node's queue at `"concurrency": 1`, 7 pending, 1 in flight; Ollama's own slots were idle
between requests. Passing `OLLAMA_NUM_PARALLEL=4` to `herd-node` as well fixed it: 96 → 112 tok/s
at 2 → 4, time to first token 0.9 s at 2.

**Cause:** `node/collector.py` `_ollama_env()` reads the variable from herd-node's own environment,
falling back to `launchctl getenv` on macOS only. A systemd service's environment is not inherited
by anything else, so the node reports `num_parallel: 0`; `server/serializers.py`
`decode_parallelism_for()` then uses `OLLAMA_DEFAULT_NUM_PARALLEL = 1` and
`queue_manager._compute_queue_concurrency()` caps the node at 1. Ollama's default really is 1, so
**an unconfigured install is unaffected**; only a raised value goes unseen.

**Fix:**
1. On Linux, when the variable is not in herd-node's environment, read the unit's:
   `systemctl show ollama.service -p Environment --value` (works unprivileged; confirmed on Ubuntu
   22.04 during the run) and parse `OLLAMA_NUM_PARALLEL` (and `OLLAMA_MAX_LOADED_MODELS`, which goes
   through the same `_ollama_env`). Cache it like the launchctl path, with a timeout, failures read
   as "unset". Only for the default unit name; a user with a renamed unit sets the variable on
   herd-node, as today.
2. Investigate a second, more direct source: the running runner's command line
   (`/proc/<pid>/cmdline` is world-readable, unlike `environ`). If Ollama 0.40's llama.cpp runner
   carries the slot count (`--parallel N` or equivalent), it is the value Ollama actually applied
   per model, which also covers families Ollama forces to 1. Use it if present, systemd otherwise.
   Check what the runner command line looks like before building on it.
3. A health check in `health_engine.py`: a Linux node reporting 0
   while a loaded model's queue has had pending requests for a sustained period says "herd assumes
   Ollama serves one request at a time on <node>; if you raised OLLAMA_NUM_PARALLEL, set it for
   herd-node too".
4. Containers: a node in a container sees neither systemd nor host processes. Document that it must
   be given `-e OLLAMA_NUM_PARALLEL=<same value>` (item 6).

**Tests:** collector reads the value from a faked `systemctl show` output; env wins over systemd;
`systemctl` missing, failing or timing out reads as unset; non-Linux never calls it. If item 2
lands, a fake `/proc` tree with a runner command line.

**Verify:** on a Linux VM (the Lima setup from the container plan), Ollama as a systemd service with
`OLLAMA_NUM_PARALLEL=4`, herd-node started without it: `/fleet/status` shows `num_parallel: 4` and
`/fleet/queue` shows concurrency 4.

## 2. `nomic-embed-text` embeddings fail without fastembed, even when Ollama has the model

**Seen:** 1,055 of 1,055 embedding requests returned 503 "No node is running the native text
embedding server for 'nomic-embed-text:latest'. Install fastembed…", with `nomic-embed-text:latest`
loaded in Ollama on one node. After `uv tool install 'ollama-herd[embedding]'` on the nodes, about
17,000 embedding requests succeeded with 0 failures.

**Cause:** `routes/ollama_compat.py` `dispatch_embed()` sends any model for which
`node/text_embedding_models.is_text_embedding_model()` is true to `embed_text()`, which only knows
the native fastembed server. That list is exactly `nomic-embed-text` and `nomic-embed-text:latest`,
the most-used embedding model. When no online node serves it natively there is no fallback.
`/v1/embeddings` and `/api/embeddings` both go through `dispatch_embed`, so all three endpoints fail
the same way. The published container image installs base dependencies only, so **every container
herd** that uses `nomic-embed-text` gets 503s, while `docs/guides/docker.md` says "embedding models
are served through Ollama".

**Fix:** in `dispatch_embed`, take the native path only when at least one online node serves it
natively; otherwise fall through to the existing Ollama embed path, which scores nodes that have the
model like any other. Keep the native path's "restarting, retry in 10 s" answer for a node that
serves natively and is briefly offline (the 2026-10-04 incident in `_no_native_server`); fall back
to Ollama only when no node has ever served it natively *and* some node has it in Ollama. If neither
serves it, keep the 503, but say both options ("`ollama pull nomic-embed-text` on a node, or install
the `embedding` extra").

Check before building: the two backends' vectors differ (fastembed runs a quantised ONNX export of
nomic v1.5; Ollama runs its GGUF). A herd that has both, with the native server going up and down,
would mix vectors from two models in one index. Decided (see Decisions): the safe
one is "prefer native whenever any node has it, Ollama only when none does", plus the existing
`X-Fleet-Backend` header so a client can tell.

**Tests:** no native node and the model in Ollama → served by Ollama with `X-Fleet-Backend: ollama`;
a native node online → native, as today; native node offline but known → the retryable 503, as
today; in neither → 503 naming both fixes. The same three for `/v1/embeddings`.

**Verify:** the container smoke test (`scripts/docker-smoke.sh`) gains an embed request for
`nomic-embed-text` through the router.

## 3. Placement is blind to evictions, and the warm bonus feeds thrashing

**Seen (model-alternation test):** two nodes, two 15.7 GB models that cannot share one 24 GB card,
20 alternating requests with fallback off. The router sent all 20 to the same node, which swapped
models on every request (7.6-8.0 s to the first token each time, 193 s in all), while the other
node sat empty. In the mixed-workload test the router loaded `qwen3.5:9b` onto a node that was
serving `qwen3-coder:30b`, evicting it, although another node had `qwen3.5:9b` loaded; coder
throughput then fell to 48 tok/s.

**Cause, from `server/scorer.py`:**
- `_score_thermal()`: a model unloaded in the last 30 minutes scores **warm (+30)** on that node; a
  model merely on disk scores **cold (+10)**. After the first swap, node 1 has the evicted model
  "warm", so node 1 wins the next request for it, evicts the other model, which becomes "warm"
  there in turn. The signal meant "page cache is hot" ends up meaning "this node just threw it out".
- `_score_memory_fit()` and `_eliminate()` compare the model's cost with
  `model_memory_available_gb()` = RAM available + free GPU memory. On a discrete-GPU node with
  52 GB of free RAM, every model "fits" with a ratio above 2 on every node, so the signal cannot
  tell an empty card from a full one. Ollama does not spill a model that would fit after an
  eviction into RAM; it evicts (`sched.go`: "model predicted to exceed available memory,
  evicting", logged during the run).
- Nothing scores what a cold load would displace.

**Fix (design first, in this plan's follow-up):**
1. **Eviction cost.** For a cold load on a node, estimate whether it fits in the memory Ollama
   will actually use without evicting: free GPU memory on a node with a discrete GPU (from the
   0.12.9 detection), available RAM on unified-memory nodes. If it does not fit, the load evicts
   something: subtract a penalty that grows with the evicted models' recent traffic (the queue
   manager already tracks per-node-model completions). An idle resident model costs little to
   evict; a busy one costs a lot.
2. **Warm only counts if it would still fit.** Keep the +30 for a recently unloaded model only when
   loading it back would not evict; otherwise it is a cold load with an eviction, scored as such.
3. **Prefer free memory for cold loads.** Among nodes that can cold-load, one with room scores above
   one that must evict, by more than the warm-versus-cold gap.
4. Add the eviction estimate to `scores_breakdown` in the trace, so a placement can be explained.

**Tests:** the alternation case as a scorer test (two nodes, one with model A resident and B
recently unloaded, one empty; a request for B goes to the empty node); a busy resident model is not
evicted when another node has room; on a unified-memory node behaviour is unchanged; the existing
scorer suite still passes.

**Verify:** rerun the alternation test (arm 4, fallback off): each model should settle on its own
node after its first load, and every later request should reach its first token in under 1 s.

## 4. `vram_fallback` substitutes any loaded model, even when the requested one could load

**Seen:** with defaults, every request for `qwen2.5:14b-instruct-q8_0` was answered by
`phi4:14b-q8_0`, which was loaded, instead of loading `qwen2.5` onto the empty second node. The
benchmark read 66 ms to first token for the wrong model. The trace store records it
(`original_model` ≠ `model`) and `X-Fleet-Fallback: true` is set, but the client got a different
model from the one it named.

**Cause:** `routes/routing.py` `_try_vram_fallback()`: when the best score is a cold load, serve the
best loaded model of the same category, and failing that **any loaded model** (cross-category,
logged at ERROR), blocked only for image requests and by the usage-priority check. It never asks
whether some node could load the requested model without evicting anything. The design (March 2026,
`docs/plans/vram-aware-model-fallback.md`) was for a 512 GB machine where a cold load meant a
10-minute swap of a 90 GB model; on a herd with an empty node, the same rule refuses to use it.

**Fix (decided, see Decisions):**
1. When some node can cold-load the requested model **without evicting** (item 3's estimate), load
   it there; do not substitute. Substitution stays for the case it was designed for: every load
   would evict.
2. Cross-category substitution off by default (a setting to turn it back on). Answering a coding
   request with a reasoning model, or a general model with a coder, is a quality risk the design
   already logs as an error.
3. No change to `X-Fleet-No-Fallback` / `"fallback": false`, which already give strict mode per
   request.

**Tests:** empty node available → cold load there, no fallback; every node full → same-category
fallback as today; only cross-category available → loads (or queues) by default, substitutes with
the setting on; image requests unchanged.

## 5. Benchmark harness

- **Warm every node directly.** `warmup()` sends one request per model through the router, which
  warms one node. The first request on a fresh runner took about 39 s on these Turing cards (item 8),
  so the first 4-node arm read 211.8 tok/s with a 29 s p99, and 272.5 tok/s with a 1.1 s p99 once
  every node was warm. Warm each node's Ollama directly (the router's `/fleet/status` gives each
  node's Ollama URL), per model it has loaded.
- **Ask for the exact model.** Send `X-Fleet-No-Fallback: true` on every benchmark request, and
  count a response whose `X-Fleet-Served-Model` differs from the request as a failure. Do the same in
  `docs/experiments/model-alternation-benchmark.py`. A benchmark that silently measures another model
  is worse than one that fails.
- **Models that are not loaded.** `--models` currently drops any model not loaded on some node, so a
  one-machine mixed workload (two models that do not fit together) cannot run. Accept models that are
  on disk, and report the cold loads.
- **Thinking models.** The router raises a thinking model's `num_predict` to at least 1,024
  (`thinking_min_predict`), so `--max-tokens 200` gave 1,024-token replies from `qwen3.5:9b`. Add
  `--think false` (sends `"think": false`) and print the effective cap beside the results.
- **Per-node outcome.** Record each response's `X-Fleet-Node` and `X-Fleet-Served-Model` in the
  saved run, so placement is in the results and not only in the trace store.
- Done already: Python 3.11 syntax (`af32336`).

## 6. Docs

- `docs/guides/docker.md`: the "Serve native text embeddings" bullet says embedding models are served
  through Ollama. True after item 2; until then, say `nomic-embed-text` needs the `embedding` extra.
  Add `-e OLLAMA_NUM_PARALLEL` to the Linux node section (item 1).
- The Linux install / configuration guide: if you raise `OLLAMA_NUM_PARALLEL`, herd reads it from
  the systemd unit (after item 1); in a container, pass it to herd-node too.
- `vram_fallback`: document what it does in the configuration reference, in the terms of item 4's
  decision, with the per-request off switch.

## Investigations (no fix decided)

7. **Uneven split at low concurrency.** Four nodes at 2 concurrent requests split 58/42 between two
   nodes and read 129.9 tok/s, below two nodes at the same concurrency (152.9). One run; repeat
   before treating it as real. If it holds, read the score breakdowns in the traces.
8. **First request on a new runner.** About 39 s to the first token on each RTX 6000 the first time a
   loaded model computed, then normal. Likely CUDA compiling kernels for compute capability 7.5 that
   the build does not ship precompiled, and cached afterwards. Ollama's behaviour, not herd's; worth
   knowing because herd's scorer treats a just-loaded model as hot. Check whether it recurs after an
   unload and reload (cache) before saying anything about it.
9. **Ollama 0.40 sizes context by VRAM.** On the 48 GB card Ollama asked for 131,072 tokens of
   context for `qwen3.5:9b` and `qwen3-coder:30b` took 31 GB, against 20 GB on the 24 GB cards.
   Check that `_model_fit_cost_gb()` (which uses measured context where it has it) estimates the
   larger figure on a large card, or item 3's "fits without evicting" will be wrong exactly there.

## Order

1. **Release A:** items 1, 2, 5, 6. Each is local, with tests; none changes routing for a herd that
   is not hitting the bug.
2. **Design note for items 3 and 4** (scoring changes, numbers on a whiteboard first), then build,
   then release B.
3. **Benchmark run 2** on the same hardware once release B is out, with stock configuration on the
   nodes (no herd-node workaround, no fastembed), to show the fixes: one node scales with
   concurrency when `OLLAMA_NUM_PARALLEL` is in the systemd unit only; embeddings succeed without
   the extra; the alternation test settles one model per node with fallback on and off.

## Decisions

Decided 2026-11-20 by the maintainer:

1. **Item 4: change the default.** When some node can load the requested model without evicting,
   load it there instead of substituting. Cross-category substitution is **off by default**, behind a
   setting. Same-category substitution stays for the case it was built for: every load would evict.
   The changelog says so plainly, since some requests will now wait for a load where they used to get
   a different model.
2. **Item 2: fall back to Ollama only when no node has ever served `nomic-embed-text` through
   fastembed.** Once any node has served it natively, keep the native path (and its retryable 503
   while that node is away), so one index never mixes vectors from the two backends. "Has ever
   served" needs to survive a router restart: persist it (the trace store already records
   `X-Fleet-Backend` per request, or a small flag), not only in memory.

Open:

3. Release A as 0.12.10, together with the VPN-address fix already on `main`?
