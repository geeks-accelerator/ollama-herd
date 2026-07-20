# Known Issues & Improvements

Identified via code review of the full codebase. Organized by priority.

**Status key:** `OPEN` — not yet addressed. `PARTIAL` — partially fixed. `FIXED` — resolved.

---

## Routing Safety

### Ollama watchdog cascade-restarted `ollama serve` and wiped pinned models `FIXED` (removed)

**File:** `src/fleet_manager/node/ollama_watchdog.py` (deleted 2026-04-23)
**Severity:** High

The node-side watchdog periodically sent a chat probe to Ollama to detect stuck runners. Its probe-model picker (`_pick_probe_model`) chose the **smallest currently-loaded model** as the probe target. When `nomic-embed-text` (an embedding-only model, ~274 MB) was hot — which is common — it was picked. `/api/chat` on an embed-only model returns HTTP 400 every time; the watchdog interpreted the 400 as "runner stuck," kicked runner processes via `pkill`, and the counters never reset on successful kicks. Result:

1. Every ~2 min, 2 consecutive 400s → KICK (kill runner processes).
2. Repeated 13 times over 13 min without resetting the counter.
3. Escalated to a full `ollama serve` restart (`launchctl kickstart -k`), wiping **all** hot models.
4. Subsequent preloader re-loads of pinned models timed out at 120s because the watchdog kept kicking runners mid-cold-load.
5. During the window, 20 `gemma3:27b` requests were silently routed to `gpt-oss:120b` via VRAM fallback — a cross-category substitution (vision → reasoning) that silently dropped image inputs.

**Observed:** 2026-04-23 21:50–23:10 UTC. User request for `claude-sonnet-4-5 → gemma3:27b` (vision path) got answered by `gpt-oss:120b` silently.

**Fix shipped:**
- `src/fleet_manager/node/ollama_watchdog.py` deleted entirely. The user's original fleet ran fine without it — the watchdog existed to smooth over intermittent Claude Code CLI issues that are now handled by other layers (admission control, streaming retry, context protection).
- `src/fleet_manager/node/agent.py` — all `_ensure_ollama_watchdog` / shutdown hooks removed.
- `src/fleet_manager/models/config.py` — 5 `ollama_watchdog_*` settings removed. Comment left in place describing why (so nobody re-adds it without the two fixes the original lacked: explicit probe-model allowlist, per-cause cooldowns).
- `src/fleet_manager/server/routes/routing.py` — cross-category VRAM fallback now logs at **ERROR** level (was INFO) with an explicit "QUALITY RISK" warning when vision → non-vision substitutions happen. Event record carries `cross_category` + `fallback_category` for dashboard filtering. Existing `X-Fleet-Fallback` response header continues to flag substitutions to clients.
- `tests/test_node/test_ollama_watchdog.py` deleted alongside the module.

If stuck-runner detection is ever needed again, re-add it with: (a) an **explicit allowlist** of chat-capable probe models, never size-based selection; (b) per-cause cooldowns so a guaranteed-failing probe can't escalate; (c) hard cap on serve-restart escalations per hour.

### Ollama native image models can evict LLMs from memory `PARTIAL`

**File:** `src/fleet_manager/server/routes/ollama_compat.py`
**Severity:** High

When an Ollama native image model (e.g., `x/z-image-turbo` at 12GB) is requested via `/api/generate`, Ollama may evict the resident LLM to make room. On a single-node fleet, this means ALL text inference fails with 500 errors until the LLM is reloaded.

**Observed:** 2026-03-30. After generating images with `x/z-image-turbo`, `gpt-oss:120b` was evicted. All DriftsBot text requests failed with 500 for several minutes.

**Proposed fixes (in order of complexity):**
1. **Prefer mflux over Ollama native** — when both mflux `z-image-turbo` and Ollama `x/z-image-turbo` are available, prefer mflux since it doesn't compete for Ollama VRAM
2. **Guard single-LLM nodes** — don't route Ollama native image requests to a node if it's the only node serving text LLM requests and the image model isn't already loaded
3. **Memory budget check** — before routing, verify that loading the image model won't push total VRAM past available memory (Ollama reports `size_vram` per model)
4. **Auto-unload after generation** — send `keep_alive: 0` after image generation completes to immediately free VRAM for the LLM

**Fix #1 implemented:** The router now prefers mflux over Ollama native when both are available. If a client requests `x/z-image-turbo` via `/api/generate` and mflux has `z-image-turbo` on any node, the router redirects to the mflux image server automatically. Ollama native is only used as a fallback when mflux isn't installed. This prevents LLM eviction because mflux runs as a separate subprocess outside Ollama's VRAM.

**Remaining:** Fixes #2 (guard single-LLM nodes) and #3 (memory budget check) are not yet implemented. These would protect against Ollama native image models on multi-node fleets where some nodes have mflux and others don't.

---

### Ollama watchdog can't escalate to `ollama serve` restart `OPEN`

**File:** `src/fleet_manager/node/ollama_watchdog.py`
**Severity:** High (root cause of multi-hour gpt-oss outages)

The watchdog detects stuck `/api/chat` and kills `ollama runner` processes via `pkill -9`. That recovers the case where `ollama serve` is healthy but a runner is wedged. It does **not** recover the more pernicious case where `ollama serve` itself has accumulated state corruption — `/api/tags` keeps answering, runners keep getting kicked, but each respawn wedges immediately under load.

**Observed:** 2026-04-22, ~5h `ollama serve` uptime under sustained load (Claude Code + dashboard briefing + concurrent `hf download`):

- 28 consecutive `gpt-oss:120b` requests in debug log, all `status=retried`, all `err=ReadError('')`
- Latencies climbing **monotonically** 50s → 130s → 190s → 250s → 310s → 370s → 452s → 458s → 512s → 572s
  - That growing-tail pattern means requests stack serially behind a stuck runner; each one waits longer than the last for a slot that never opens
- Watchdog log shows `KICKING stuck runner` firing on schedule (18:10:18) — kicks landing fine
- `ollama serve` log: repeated `"llama runner process no longer running" sys=9 string="signal: killed"` — runners die before serving anything
- `/api/tags` answers in 12ms throughout (so the watchdog's tags-probe stays green)
- `/api/chat` returns `HTTP 000` after 30s
- **Recovery only happened after manual `pkill -9 ollama serve` + relaunch** — runner kicks alone did nothing

**Why the current design is insufficient:**

1. **Treats one failure mode, not the whole space.** The watchdog assumes "runner stuck, serve healthy." It can't see the "serve healthy but every runner dies under load" mode that today's outage exhibited.
2. **No escalation.** After N kicks with no recovery, it should escalate to bouncing `ollama serve`. Today it kicked, watched the next probe still fail, kicked again on the next cooldown, and so on indefinitely.
3. **Cooldown vs probe-interval mismatch.** Probe every 60s, cooldown 120s — the watchdog is silent for 2 minutes after each kick while damage compounds. A growing-latency stack-up like today's was visible in the trace store within ~3 cycles, but the watchdog couldn't act on it.
4. **No load-shedding.** When the watchdog detects the system is in trouble, it does nothing to slow incoming traffic. The dashboard briefing kept firing `num_predict=4800` requests every ~60s (because each failure cleared the cache, triggering immediate retry on next pageview) — the watchdog couldn't see that load source, let alone throttle it.

**Proposed fixes (in order of complexity):**

1. **Add escalation path** — after 3 consecutive kicks where the next probe still fails within the cooldown window, restart `ollama serve` itself (`pkill -9 -f "ollama serve"` then `open -a Ollama` on macOS, `systemctl restart ollama` on Linux). Add a higher-level cooldown (e.g. 30 min) on serve restarts to prevent a flap loop. *This alone would have ended today's outage in ~5 minutes instead of multiple hours.*
2. **Add a third probe: per-cycle latency trend.** If the rolling p95 of `/api/chat` latency from the trace store grows monotonically over 3 cycles AND the absolute latency exceeds a threshold (e.g. 60s), treat that as a soft failure and trigger a kick BEFORE the request fully times out. Catches the stack-up pattern early.
3. **Failed briefings must update the cache.** `dashboard.py:_generate_briefing` should write a "last failure" record to the cache when the LLM call fails, so the next pageview/poll doesn't immediately re-trigger another `num_predict=4800` request. The endless 60s briefing-spam loop was a major load multiplier today.
4. **Load shedding via `/fleet/queue` 503**. When the watchdog has fired ≥1 kick in the last cycle, the router should return 503 Service Unavailable to non-critical traffic (everything except real user-facing requests) so health probes and briefings back off automatically. Hard to classify "critical" cleanly without explicit tags, but even a coarse "anything from `127.0.0.1` is internal → defer" rule would have helped.
5. **Separate watchdog from per-node agent.** Today's watchdog runs in the same process as the heartbeat/collector. If the node agent itself wedges, no watchdog. A small standalone supervisor (launchd plist on macOS, systemd unit on Linux) is a better long-term home — survives node-agent restarts, can kill `ollama serve` cleanly, can use a different binary so it doesn't share the failure mode.
6. **Stop using Ollama for what we don't need.** The briefing call could go to MLX (Qwen3-Coder-30B serves it in ~3s vs gpt-oss:120b's 50s+ when working). `nomic-embed-text` could move to an MLX-native embedding model. If Ollama's only tenant becomes "things users explicitly request via `/api/chat`," it's much harder to overload accidentally and the watchdog's blast radius shrinks proportionally.

**Recommendation:** Land #1 + #3 immediately (small surface, big impact). #2 next as additional signal. #4–#6 are larger architectural moves to tee up.

**Related:** Today's outage compounded with a `huggingface_hub` download running in parallel — disk-write saturation made runners crash even faster. Already noted in `docs/observations.md`. The watchdog has no awareness of disk I/O or other resource competition.

---

## External Dependencies

### Ollama ships a native MLX runner — our MLX subsystem's premise is stale `OPEN` (investigate first)

**Files:** `src/fleet_manager/node/mlx_supervisor.py`, `src/fleet_manager/server/mlx_proxy.py`, `scripts/setup-mlx.sh`, `CLAUDE.md`
**Severity:** Medium–High (strategic — a large subsystem may be redundant; **plus** load-bearing constants may be wrong)

Ollama now has a **first-party native MLX runner** ([ollama.com/blog/mlx](https://ollama.com/blog/mlx)). Verified on this fleet 2026-07-17: the running Ollama is **`0.24.0`** (not the `0.20.4` CLAUDE.md documents), and its binary contains **`github.com/ollama/ollama/x/mlxrunner`**, real MLX C-API bindings (`mlx_enable_compile`, `mlx_set_memory_limit`), `OLLAMA_MLX_MTP_*` + `OLLAMA_NEW_ENGINE` env knobs, a prefix-cache trie (`mlxrunner.trieNode`), and MTP speculative decoding — 6,925 `mlx` string hits in total.

**Two problems.** (1) **Stale, load-bearing facts:** the documented "Ollama 0.20.4 has a hardcoded 3-model hot cap" underpins `model_preload_max_count=3`, `OLLAMA_HOT_MODEL_CAP` / `free_slots`, the `OLLAMA_MAX_LOADED_MODELS=-1` gotcha, and the eviction/pin logic — if 0.24+ changed the cap, the herd is enforcing a limit that no longer exists. (2) **Expiring premise:** our whole `mlx_lm.server` stack (supervisor, proxy, `mlx:` prefix, the `--kv-bits` patch that breaks on every `mlx-lm` upgrade, and its recent bug tax) exists *because Ollama couldn't do MLX*.

**Not threatened:** the herd's core value is routing (scoring, queues, health, multi-node) — a faster Ollama helps it for free — and **distributed multi-Mac inference is still ours** (Ollama is single-host). The decisive unknown is **Ollama's MLX model coverage** (the preview accelerated only Qwen3.5-35B-A3B; `mlx_lm` runs arbitrary HF conversions).

**We are 8 versions behind:** latest is **v0.32.1**; `0.24.0` **predates stable MLX** (the 0.30 line). Full analysis + verified-vs-unverified split: [`issues/ollama-native-mlx-runner.md`](issues/ollama-native-mlx-runner.md). **Execution plan** (upgrade to v0.32.1 + the four measurements that decide the MLX subsystem's fate, install landmines, rollback): [`plans/ollama-0.32-upgrade-and-mlx-evaluation.md`](plans/ollama-0.32-upgrade-and-mlx-evaluation.md).

---

### GLM-4.7-Flash ~4× too slow on Ollama (glm4moelite MoE not exploited) `OPEN` (upstream)

**File:** none (upstream Ollama bug — herd serves/measures correctly)
**Severity:** Medium (affects model selection/benchmarking; no correctness impact)

`glm-4.7-flash` decodes at ~13.7 tok/s on the M3 Ultra via Ollama — the speed of the **dense** `gemma3:27b` — despite being a **30B-A3B MoE with 3B active params** that should match `qwen3-coder:30b-a3b` (~56.7 tok/s). Ollama's `glm4moelite` path doesn't exploit the sparsity and CPU-offloads the experts ([ollama/ollama#14045](https://github.com/ollama/ollama/issues/14045)). Compounded by interleaved thinking (~3,600 output tokens vs qwen's ~400) and a 202,752-token default context (51 s prefill). **Fix:** serve via MLX (verify `mlx-lm` supports `glm4_moe_lite` — [mlx-lm#806](https://github.com/ml-explore/mlx-lm/issues/806) — our pinned 0.31.3 may need an upgrade + re-patch); herd-side, cap `num_ctx` to cut the prefill. Full analysis: [`issues/glm-4.7-flash-ollama-glm4moelite-slow.md`](issues/glm-4.7-flash-ollama-glm4moelite-slow.md).

---

### DiffusionKit `argmaxtools` crashes on macOS 26+ `FIXED` (local patch)

**File:** `argmaxtools/test_utils.py` (installed dependency, not our code)
**Severity:** High (blocks all DiffusionKit image generation)

The `os_spec()` function in `argmaxtools.test_utils` parses `sw_vers` output expecting exactly 3 lines. macOS 26 added a `ProductVersionExtra` field (4th line), causing `IndexError: list index out of range`. This crashes `diffusionkit-cli` on any image generation attempt.

**Workaround applied:** Patched the installed `test_utils.py` to parse `sw_vers` output as a key-value dict instead of positional list. See [image generation guide](guides/image-generation.md) for the patch instructions.

**Upstream status:** No fix as of `argmaxtools` v0.1.23 (2026-03-30). The `argmaxtools` repo appears to be private — no way to submit a PR directly. Filed on DiffusionKit GitHub as the integration surface.

**Note:** This patch must be re-applied after any `uv tool upgrade diffusionkit` or `pip install --upgrade diffusionkit`.

---

### DiffusionKit SD3.5 Large — Python crash on cleanup `OPEN`

**File:** `diffusionkit/mlx/__init__.py` (installed dependency)
**Severity:** Low (image generates successfully, crash is post-generation)

SD3.5 Large (11.6GB peak memory) occasionally triggers a "Python quit unexpectedly" crash dialog on macOS after the image has been written to disk. The image is valid — the crash happens during post-generation telemetry/cleanup. SD3 Medium (3.5GB peak) does not exhibit this behavior.

**Workaround:** Use SD3 Medium for production workloads. SD3.5 Large works but may show the macOS crash dialog to users.

**Root cause:** Likely a memory-related segfault in the MLX/Metal cleanup path when using system Python 3.9. May resolve with a newer Python version or future DiffusionKit update.

---

## Performance (Will Bite at Scale)

### `available_gb` is the wrong ceiling for "can this model fit?" `OPEN` (needs a design decision)

**Files:** `src/fleet_manager/server/scorer.py`, `src/fleet_manager/server/model_preloader.py`
**Severity:** Medium (real request failures, narrow + self-healing window)

The scorer/preloader gate model loads on psutil's `available_gb`, which on macOS is **volatile** (sampled 17 GB → 445 GB on an idle 512 GB box; -97 GB in 3 s) because resident models are **wired** and wired isn't counted as available. Worse, it's the **wrong question**: Ollama evicts its own LRU model to make room, so resident-model memory *is* usable for a new model. After the 2026-07-17 reboot this produced `All 1 nodes eliminated for gpt-oss:120b` ×100+ and 30 s holding-queue timeouts on a machine with ~358 GB free.

**Narrow:** the scorer already skips the memory check for *resident* models ([`scorer.py:203`](../src/fleet_manager/server/scorer.py)), so it only bites at cold start / just after a restart. **Left open deliberately** — picking the wrong metric fails in the dangerous direction (over-reporting capacity is what produced the 290 GB thrash loop + kernel panic). Full analysis, four options, and a suggested instrument-first step: [`issues/available-gb-is-the-wrong-ceiling-for-model-fit.md`](issues/available-gb-is-the-wrong-ceiling-for-model-fit.md).

---

### `TraceStore` write-storm under WAL contention `FIXED` (0.6.2)

**Files:** `src/fleet_manager/server/trace_store.py`, `src/fleet_manager/server/latency_store.py`, `src/fleet_manager/server/health_engine.py`
**Severity:** High (observability outage, not request outage)
**Observed:** 2026-05-10 14:21 PDT → 2026-05-15 00:58 PDT (4.5 days)

A long-running read held off SQLite WAL checkpoints on `latency.db`. The WAL grew to 2.5 GB and writers couldn't acquire the lock within the 5-second `busy_timeout`. Result: ~40,000 background `record_trace` tasks failed with `database is locked` across May 10–15 (peak ~9,650/day). Requests themselves succeeded end-to-end (trace writes are fire-and-forget) but the dashboard's `reqs_24h` quietly dropped to 0 because it queries the same DB. The failure was missed by four consecutive soak checks because the grep pattern used to scan logs was wrong (no space after the JSON colon — see `docs/observations.md` 2026-05-15 for the full process post-mortem).

**Fix shipped in 0.6.2 — two rounds:**

Round 1 (2026-05-15, "longer timeout + retry"):
- `PRAGMA busy_timeout` 5s → 30s in both `TraceStore` and `LatencyStore`.
- `TraceStore.record_trace` retries on locked errors (200ms → 800ms → 2s, 3 attempts) before declaring the trace lost.
- `PRAGMA wal_autocheckpoint=100` in both stores bounds WAL growth even when a reader is slow.
- New `trace_store_write_failures` health check (WARNING at 1+, CRITICAL at 50+ failures in last 5 min) — makes the failure mode dashboard-visible instead of requiring operators to grep logs.
- Per-process JSONL log files (`herd.jsonl` + `herd-node.jsonl`) eliminate a cross-process daily-rotation race that left one log day growing to 131 MB while peers stayed at 6 MB.
- `CLAUDE.md` "Gotchas" entry documenting the correct JSONL grep pattern (`'"level": "ERROR"'` with space) so future scanners don't repeat the false-clean miss.

Round 2 (2026-05-16, "structural fix" — round 1 reduced amplitude but didn't eliminate the failure under sustained traffic; ~4,470 errors recurred in 12 hours of soak):
- **Dedicated `_read_db` connection per store** (`PRAGMA query_only=1` for defense-in-depth). aiosqlite serializes operations per-connection through a single background thread; with one connection serving both reads and writes, a slow dashboard read blocked queued writes for the read's duration. Worse, reader snapshots pinned the WAL checkpoint barrier on the writer's view of the same connection. Two connections = two threads, independent snapshots, no serialization.
- **Periodic `PRAGMA wal_checkpoint(PASSIVE)`** every 10s from a background task in `app.py` lifespan. Wall-clock-triggered checkpoints fire in the gaps between dashboard read snapshots; autocheckpoint alone is volume-triggered and can sit at 99 pages while readers accumulate snapshots that block the eventual checkpoint when the 100th write lands.
- Plan: `docs/plans/trace-store-read-connection-and-checkpoint.md`. Verified on the local fleet 2026-05-16 — a 30-concurrent-write + 120-dashboard-poll burst held the WAL at 410 KB peak vs 103 MB on the same workload pre-round-2.

Operator runbook for "database is locked" recovery: `docs/troubleshooting.md` § "Trace DB write failures."

---

### An idle MLX server gets externally SIGKILLed on a saturated box (likely OS memory pressure) `OPEN` (mitigated)

**File:** `src/fleet_manager/node/mlx_supervisor.py`
**Severity:** Low–Medium (churn + wasted VRAM/reloads; auto-recovers, no user-facing request failure)
**Observed:** 2026-07-16 — port 11440 (`mlx-community/Qwen3-Coder-Next-4bit`)

Over an 8 h benchmark window, port 11440 exited `rc=-9` (SIGKILL) **6×**, clustered in the load peak (06:24–06:43); the monitor caught each dead child and restarted it, and the port wasn't re-bindable for ~10 s (`_wait_port_free` "port still occupied … spawning anyway"), re-mmap'ing the 30B model each time.

**Corrected mechanism (an earlier draft of this issue was wrong — worth recording why).** The first diagnosis blamed a "false-positive health kill": that the runtime health poll (3 s timeout) marked the server unhealthy and the supervisor SIGKILLed it. **That is not how the supervisor works.** `_monitor` only restarts on an *actual* process exit (`rc = self._proc.poll(); if rc is None: continue` — L888-907); `poll_health`/`refresh_health` (L984, L1189) only *update the status string* for the dashboard — nothing kills or restarts a running-but-unhealthy server. So the `rc=-9` came from **outside** the supervisor entirely.

The signature points at **macOS memory pressure (jetsam / memorystatus)**: the kill was **selective** (only the idle 35 GB 11440 died; the actively-served 11441/11442 and the small supervisor parent all survived), clustered in load peaks, and left **zero** app-level markers (jetsam is silent to the victim — the 131 MB log has no `out of memory` / `Metal` / `allocate`; its 12k tracebacks are restart-race noise: `cannot schedule new futures after interpreter shutdown` from the dying process + `Address already in use` from the respawn racing the port). 11440 was **essentially idle** — 19,904 health pings vs **3** real inference requests (nothing routes to Qwen3-Coder-Next; coding load went to Ollama `qwen3-coder:30b`), which makes it the lowest-priority, highest-footprint jetsam target. (`log show` for jetsam events was inconclusive without `sudo`, so "jetsam" is strong inference, not a captured kernel line.)

**There is no clean code fix** — a health-check debounce fixes nothing here (the health check doesn't cause the kill). The real lever is operational:
- **Don't keep an unused large model resident.** A model with zero routed traffic holds ~35 GB and becomes the jetsam target under pressure. **Mitigation applied 2026-07-16:** dropped Qwen3-Coder-Next from `FLEET_NODE_MLX_SERVERS`.
- If a genuinely-used MLX model is being jetsam'd, that's a real memory-headroom problem — surface it via the memory-pressure gate rather than absorbing repeated reloads.
- Minor hardening still worth doing: the `poll_health` comment "monitor will restart" (L1007) is misleading (the monitor does not restart on health status) and should be corrected so the next reader doesn't repeat this misdiagnosis.

---

### `/fleet/pin` reported "not on disk" for a resident, serving model `FIXED` (0.8.2)

**Files:** `src/fleet_manager/server/routes/fleet.py`, `src/fleet_manager/server/model_preloader.py`
**Severity:** Medium (factually false error; intermittent — depends on free memory at the instant of the call)
**Observed:** 2026-07-17 02:41 — reported by a client agent

`POST /fleet/pin {"model":"gpt-oss:120b","node_id":"bb"}` returned:

> `{"ok":false,"error":"'gpt-oss:120b' is not on disk on any online node — run 'ollama pull gpt-oss:120b' first."}`

…while gpt-oss:120b was **on disk, loaded (70.96 GB), and served 30/30 requests seconds later**. No restart, no traffic gap, and **not reproducible** on retry.

**Root cause — two compounding bugs.** The router log carried the real reason:

```
2026-07-17T02:41:01  Preloader: skipping gpt-oss:120b — need 72GB but only 49GB free on bb (fleet-pin)
```

1. **The error message conflated three causes.** `_load_model_on_best_node` returns a bare `False` for *not-on-disk*, *memory-gate refusal*, **and** *pre_warm error* — and `/fleet/pin` hardcoded the "not on disk … run `ollama pull`" message for all of them. The caller was told to pull a model that was already resident and serving.
2. **The memory gate ran against an already-resident model.** `_estimate_model_size("gpt-oss:120b")` = 72 GB, so the gate demands `72 × 1.2 = 86.4 GB` free. gpt-oss:120b was **already loaded**, and its own ~71 GB footprint is subtracted from the node's free memory — so the gate saw 49 GB free and refused to "load" a model that was **already in memory**. Pinning a hot model could fail *because it was hot*. (The preloader dodges this by checking `_model_is_loaded_anywhere` before calling the loader; the pin route called it directly.) The intermittency is explained by free memory fluctuating — the same call succeeded at 03:02 once memory recovered (`Preloader: loading gpt-oss:120b (~72GB) on bb (fleet-pin)`).

**Fix shipped in 0.8.2:**
- `_load_model_on_best_node` skips the memory gate when the model is **already resident** on the chosen node (reusing `_model_resident_on_node`); `pre_warm` still runs so `keep_alive=-1` is re-applied — the actual point of pinning a loaded model. Safe for the preloader, which never reaches that branch.
- `/fleet/pin` now checks on-disk explicitly (`_nodes_with_model_on_disk`) and reports each cause truthfully: genuine not-on-disk → `404` with the pull hint; on-disk-but-wouldn't-load → **`503`** naming insufficient free memory and pointing at `/fleet/status` + the router log's exact need-vs-free numbers.

---

### Failed-request traces get garbage-collected before they persist `FIXED` (0.8.2)

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Medium (observability — success rate reads higher than reality)
**Observed:** 2026-07-16

Over an 8 h window, **242** inbound OpenAI requests for `glm-4.7-flash:latest` produced **211** Ollama 503 `"maximum pending requests exceeded"` responses and **0** trace records — `glm` under no `original_model`, not even as a fallback — while 4,634 *completed* requests traced fine. The dashboard's "99.98 % success (4,669 requests)" was therefore computed over *traced* traffic only; the 211 GLM failures weren't in the denominator. (Context, not a herd bug: the client sent the *Ollama* model name to `/v1/chat/completions` instead of the resident `mlx:` model, so every request hit Ollama's saturated queue.)

**Corrected mechanism (a first draft of this issue said the error path "never calls `record_trace`" — that was wrong; the call is there).** The non-retryable branch *does* call `_record_trace(..., "failed")` (streaming.py L455). The real bug was in **`_create_logged_task`**: it did `asyncio.create_task(coro)` **without keeping a strong reference**. asyncio only holds a *weak* reference to a task, so a fire-and-forget task with no other reference can be GC'd mid-flight. Completed traces survived because the route keeps `await`-ing after recording (the loop runs the task); the **error path records then `raise`s on the very next line** with no further `await`, so the loop never ran the weakly-referenced trace task before the request tore down and GC collected it. Failed traces vanished; completed ones didn't — exactly the observed asymmetry.

**Fix shipped in 0.8.2 (two parts):**
- `_create_logged_task` now holds each task in a module-level `_background_tasks` set until its done-callback fires — the documented fix for the create_task weak-reference footgun. This makes *all* fire-and-forget writes (traces, latency records, client closes) reliable, not just the error path.
- The exhausted-retry branch (`_stream_with_retry`, `attempt > max_retries`) now records a terminal `"failed"` trace instead of leaving only per-attempt `"retried"` rows, so a request that burns every retry has a terminal outcome in the DB.

Complements the existing `trace_store_write_failures` health check: that catches "the write was attempted and failed"; this fixes "the write was scheduled and then GC'd before running."

---

### 1. `LatencyStore.get_percentile()` — Unbounded Memory Growth `FIXED`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** High

`get_percentile()` loaded ALL historical latency rows into memory every time a latency observation was recorded. For a high-traffic deployment with thousands of observations per `(node, model)` pair, this grew without bound.

**Fix:** Capped to the most recent 500 observations per `(node, model)` pair using a subquery with `ORDER BY timestamp DESC LIMIT 500`. Memory usage is now bounded regardless of history size.

---

### 2. `_refresh_cache()` — N+1 Query Pattern `FIXED`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** Medium

On startup, `_refresh_cache()` first queried all distinct `(node_id, model_name)` pairs, then issued a separate `get_percentile()` call for each pair. For a fleet with many node/model combinations, this meant dozens of sequential SQLite round-trips.

**Fix:** Replaced with a single SQL query using `ROW_NUMBER()` and `PERCENT_RANK()` window functions to compute all p75 values at once. Also caps to the latest 500 observations per pair. Startup is now one query regardless of fleet size.

---

### 3. `in_flight` List — O(n) Membership and Removal `FIXED`

**File:** `src/fleet_manager/server/queue_manager.py`
**Severity:** Low–Medium

The `in_flight` field on each queue was a `list`. Both `in` checks and `.remove()` were O(n). Under high concurrency with deep queues, this was a bottleneck.

**Fix:** Changed to `dict[str, QueueEntry]` keyed by `request_id`. All operations (`__contains__`, `pop`, `[]`) are now O(1). The reaper, `mark_completed`, `mark_failed`, and worker all use dict operations.

---

## Code Quality

### 4. `_request_tokens` Dict — Leaking Internal State `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Low

Route handlers in `openai_compat.py` and `ollama_compat.py` accessed the private `proxy._request_tokens` and `proxy._request_meta` dicts directly via `.pop()`. This broke encapsulation and coupled route logic to internal implementation details.

**Fix:** Added public methods `pop_token_counts(request_id)` and `pop_request_meta(request_id)` on `StreamingProxy`. All route handler access updated to use the public API.

---

### 5. `asyncio.ensure_future` — Deprecated API `FIXED`

**File:** `src/fleet_manager/common/discovery.py` (line ~65)
**Severity:** Low

`asyncio.ensure_future()` has been deprecated since Python 3.10 in favor of `asyncio.create_task()`. The project requires Python 3.11+, so this should be updated.

**Fix:** Replaced `asyncio.ensure_future(...)` with `asyncio.create_task(...)`.

---

### 6. Unused Dependencies and Imports `OPEN`

**Files:** `pyproject.toml`, `src/fleet_manager/server/app.py`
**Severity:** Low

- `sse-starlette` is listed in `pyproject.toml` but never imported in the source code.
- `pyyaml` is listed in `pyproject.toml` but never imported in the source code.
- `StaticFiles` is imported in `app.py` but never used.

**Fix:** Remove unused dependencies from `pyproject.toml` and the dead import from `app.py`.

---

### 7. `HeartbeatPayload.arch` — Hardcoded Default `OPEN`

**File:** `src/fleet_manager/models/` (HeartbeatPayload definition)
**Severity:** Low

The `arch` field defaults to `"apple_silicon"`, which is incorrect for non-Mac nodes (e.g., Linux/x86 or Linux/ARM).

**Fix:** Default to `platform.machine()` or similar runtime detection.

---

### 8. `event_stream()` Re-fetches State Every Tick `OPEN`

**File:** `src/fleet_manager/server/routes/dashboard.py`
**Severity:** Low

The SSE `event_stream()` function re-fetches `request.app.state` on every tick (every 2 seconds). The references should be captured once before the loop starts.

**Fix:** Capture `registry = request.app.state.registry` etc. before entering the `while True` loop.

---

### 9. Dashboard Inline HTML/CSS/JS — Growing Maintenance Burden `OPEN`

**File:** `src/fleet_manager/server/routes/dashboard.py`
**Severity:** Low (for now)

The dashboard is a large amount of inline HTML/CSS/JS in Python strings across 5 pages (Fleet Overview, Trends, Model Insights, Apps, Benchmarks). This is pragmatic for a single-file deployment but will become painful as more dashboard features are added (e.g., tag filtering on Trends/Models views).

**Fix:** When the dashboard grows further, extract to Jinja2 templates or a separate frontend build.

---

## Test Coverage Gaps

### 10. Untested Modules `PARTIAL`

**Severity:** Medium

The following modules still have zero test coverage:

- `server/rebalancer.py` — pre-warm trigger and queue move logic
- `common/discovery.py` — mDNS advertise and browse
- `common/system_metrics.py` — psutil metric collection
- `common/ollama_client.py` — Ollama HTTP client

Previously untested, now covered:
- ~~`node/agent.py`~~ — now has 6 tests in `tests/test_node/test_agent.py`

The rebalancer in particular has meaningful logic (deciding when to move pending requests, triggering pre-warm) that warrants unit tests.

---

### 11. `test_move_pending` — Tautological Assertion `OPEN`

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test asserts `moved >= 0`, which is always true for a non-negative integer. This assertion provides no verification that entries were actually moved.

**Fix:** Assert `moved >= 1` or verify the target queue received the expected entries.

---

### 12. `test_shutdown` — Vacuous Test `OPEN`

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test body is `pass  # No assertion needed`. It only verifies that no exception is raised, which provides minimal confidence.

**Fix:** Assert post-shutdown state — e.g., that worker tasks are cancelled, queues are empty, or new enqueues are rejected.

---

## Known Limitations

### 13. Meeting Detector False Positives on Dev Machines

**Severity:** Low

The macOS meeting detector (`node/meeting_detector.py`) detects active camera/microphone as "in meeting" and triggers a hard pause. Developers using webcam-based tools (video calls, streaming, screen sharing) during development will get false positives, causing the node to stop accepting work.

**Workaround:** Set `FLEET_NODE_ENABLE_CAPACITY_LEARNING=false` (the default) to disable meeting detection entirely. Tests use `@patch.object(MeetingDetector, "is_in_meeting", return_value=False)` to work around this.

---

### 14. Capacity Learning 7-Day Bootstrap Period

**Severity:** Low

The capacity learner requires 7 days of real observations to graduate from "bootstrapping" to "learned" mode. During the bootstrap period, the learner contributes less confidence to routing decisions. This cannot be validated in automated tests — it requires a week of real usage.

**Workaround:** Pre-seed the capacity learner JSON file with synthetic data if faster convergence is needed.

---

### 15. Tag Filtering Not Yet on Trends/Models Views

**Severity:** Low (feature gap)

The tagging system records tags on every trace and provides a dedicated Apps dashboard tab. However, the existing Trends and Model Insights views cannot yet be filtered by tag. Adding tag-based filtering to these views is a natural next step.

---

### 16. OLLAMA_NUM_PARALLEL Auto-Calculation Causes KV Cache Bloat and Model Thrashing `PARTIAL`

**Severity:** High

On high-memory machines (e.g., 512GB Mac Studio), Ollama's `auto` setting for `OLLAMA_NUM_PARALLEL` calculates a high slot count (e.g., 16). Each parallel slot pre-allocates KV cache for the full context window. With 16 slots and `default_num_ctx=262144`:

```
KV cache per model = 262144 ctx × 16 parallel = 4,194,304 KvSize → 384 GB
```

A single model consumes ~413 GB (17 GB weights + 384 GB KV cache + 12 GB compute), leaving no room for other models on a 464 GB VRAM machine. When a second model is requested, Ollama evicts the first — and vice versa — creating a thrashing loop that freezes the machine for 10-60 seconds per swap.

**Symptoms:**
- Models drop to 0 loaded at regular intervals (visible in herd dashboard and heartbeat data)
- Ollama logs show `"model requires more gpu memory than is currently available, evicting a model to make space"` repeatedly
- Machine freezes during model swaps (loading 88-151 GB models saturates memory bandwidth)
- `OLLAMA_KEEP_ALIVE=-1` alone does NOT fix this — eviction is space-based, not time-based

**Evidence:** Ollama server logs (`~/.ollama/logs/server-*.log`) showed eviction cascades at hourly intervals coinciding with bot-simulation model rotation. KV cache sizes confirmed via `load request` log entries showing `KvSize:4194304` with `Parallel:16`.

**Fix (user-side):** Set `OLLAMA_NUM_PARALLEL=2` (or 3-4). KV cache drops to ~20 GB per model, allowing 3-4 large models to coexist.

```bash
launchctl setenv OLLAMA_NUM_PARALLEL 2
# Restart Ollama
```

**Herd-side detection (implemented):** The health engine's `_check_kv_cache_bloat()` detects this by comparing VRAM used vs expected weight sizes. When overhead exceeds 50%, it reports CRITICAL severity with cross-platform fix instructions (macOS launchctl, Linux systemd, Windows env var). The model thrashing check (`_check_model_thrashing()`) catches the downstream symptom — frequent cold loads from eviction cascades. Both checks surface in the dashboard Health tab and `/dashboard/api/health` API.

**Remaining:** Could inject `num_ctx` overrides in proxied requests to cap context windows, but this risks changing model behavior. Current approach (detect + recommend) is safer.

---

### 21. Dynamic `num_ctx` Management Based on Actual Usage `PARTIAL`

**Severity:** Medium
**Files:** New module + `server/streaming.py`, `server/routes/dashboard.py` (settings)

Ollama allocates KV cache for the full `default_num_ctx` per model, even if most requests only use a fraction of it. A model with 131K default context uses ~67GB, but if 95% of requests only need 8K-16K context, the fleet is wasting 50+GB of memory per model on unused KV cache. This prevents loading additional models.

**Proposed approach — 3 phases:**

**Phase 1: Observe** — Track actual `num_ctx` usage per model from request traces.
- Log `prompt_eval_count` (prompt tokens) from every completed request
- Compute p50, p95, p99 of actual prompt sizes per model
- Surface in dashboard settings: "gpt-oss:120b: avg context 2K, p95 8K, p99 16K, allocated 131K"
- No behavior change — just visibility

**Phase 2: Recommend** — Use observed data to suggest optimal `num_ctx` per model.
- Dashboard shows: "Recommended: set num_ctx=32768 for gpt-oss:120b (covers p99 of your usage, saves ~50GB)"
- Health engine warns when allocated context >> actual usage by 4x+
- Settings page has a slider or input per model to set recommended `num_ctx`

**Phase 3: Auto-adjust** — Dynamically manage `num_ctx` via Ollama settings.
- Herd injects `num_ctx` in proxied requests based on learned optimal value
- If a request arrives that exceeds the current setting, Herd either:
  - a) Queues it and triggers an Ollama restart with higher `num_ctx` (slow but correct)
  - b) Passes it through with an explicit higher `num_ctx` (triggers model reload in Ollama)
  - c) Returns a warning header and serves at the current context limit
- Auto-restart Ollama if error rate spikes due to context truncation
- Settings toggle: `FLEET_DYNAMIC_NUM_CTX=true` (off by default)
- Settings page shows current vs recommended vs actual usage with toggle

**Key data already available:**
- `request_traces.prompt_tokens` in SQLite — has actual prompt sizes for every request
- Health engine already detects KV cache bloat (`_check_kv_cache_bloat()`)
- Context protection (`streaming.py`) already intercepts `num_ctx` in requests
- Dashboard settings page already has runtime toggles

**Why this matters:** On the 512GB Mac Studio, gpt-oss:120b with 131K context uses ~67GB. If actual usage is 16K context, it could use ~12GB — freeing 55GB for 2-3 additional models. This directly fixes the smart benchmark's inability to load multiple models.

---

### 17. Zombie In-Flight Queue Entries Block Concurrency Slots `FIXED`

**File:** `src/fleet_manager/server/queue_manager.py`
**Severity:** High

The queue worker adds entries to `in_flight` then hands an async generator to the route handler via a Future. If the client disconnects mid-stream or the generator is never fully consumed, `mark_completed`/`mark_failed` in the `_tracked_stream` finally block never executes. The entry stays in `in_flight` forever, permanently consuming a concurrency slot.

In production, 5 of 8 slots became zombied, causing the router to accept new connections but never process them (0 bytes returned after 2 minutes).

**Fix:** Added a background reaper task that runs every 60s and removes any in-flight entries older than 15 minutes (past the 10-minute Ollama read timeout). Reaped entries are marked as failed. The reaper starts automatically via `queue_mgr.start_reaper()` during app lifespan.

---

### 18. mDNS `NonUniqueNameException` Prevents Router Restart `FIXED`

**File:** `src/fleet_manager/common/discovery.py`
**Severity:** High

When the router crashes or is killed without clean shutdown, the zeroconf mDNS service registration persists in the network. On restart, `async_register_service()` raises `NonUniqueNameException` because the stale service name is still registered by the OS, causing the router to fail to start entirely.

**Fix:** Wrapped registration in try/except. On `NonUniqueNameException`, close the zeroconf instance, create a fresh one, and re-register with `allow_name_change=True`. This handles both stale registrations and concurrent instances gracefully.

---

### 19. Duplicate Queues from Unnormalized Model Names `FIXED`

**File:** `src/fleet_manager/models/request.py`
**Severity:** Medium

Ollama returns model names with explicit tags (e.g., `qwen3-coder:latest`) but client requests often omit the tag (e.g., `qwen3-coder`). This caused duplicate queues (`node:qwen3-coder` and `node:qwen3-coder:latest`), scoring mismatches, latency cache misses, and broken pre-warm tracking. Dashboard showed two separate queue cards for the same model with split stats (20 done vs 4520 done).

**Fix:** Added a Pydantic `model_validator` on `InferenceRequest` that appends `:latest` to model names (and fallback_models) that lack a tag. Normalization happens at construction time so all downstream code sees consistent names.

---

### 20. Client `num_ctx` Triggers Full Model Reload and Hang in Ollama `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Critical

When a client sends `num_ctx` in request options that differs from the loaded model's context window, Ollama's scheduler calls `needsReload()` and triggers a full model unload+reload. For large models (89GB `gpt-oss:120b`), this causes multi-minute hangs or complete deadlocks — 0 bytes returned. Reproduced: `num_ctx: 4096` on a model loaded at 32768 hangs indefinitely; without `num_ctx` works in 3 seconds. Confirmed directly against Ollama (bypassing Herd) — Ollama itself hangs.

Root causes compound: GPT-OSS minimum context override (Ollama bumps `num_ctx < 8192` to 8192), runner startup timeout exceeded during 89GB reload, and KV cache fill loop on small context values.

**Fix:** Added context-size protection (`FLEET_CONTEXT_PROTECTION=strip` by default) in `_build_ollama_body()`. Strips `num_ctx` when ≤ loaded context (prevents needless reload). When `num_ctx` > loaded context, searches fleet for a loaded model with sufficient context and more parameters, and auto-switches. Logged for operator visibility.

---

### 21. Stream Error Messages Are Empty Strings `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Medium

Failed request traces in the trace store have empty `error_message` fields. The `logger.error()` calls in `_stream_with_tracking` and `_stream_with_retry` format the exception with `{e}` but the exception objects sometimes stringify to empty strings (e.g., `httpx.RemoteProtocolError` with no message). This makes post-mortem debugging blind — you can see a request failed but not why.

**Fix:** Changed all `str(e)` to `f"{type(e).__name__}: {e}"` in stream error paths. Now error messages always include the exception class (e.g., `RemoteProtocolError:` instead of empty string). Applied in both `_stream_with_tracking` and `_stream_with_retry`.

---

### 22. Client Disconnects Recorded as "completed" `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** High

When a client disconnects mid-stream (HTTP timeout, connection drop), FastAPI sends `GeneratorExit` to the streaming generator. Both `_stream_with_tracking` and `_stream_with_retry` caught this but marked the request as **completed** — silently hiding failures from the dashboard and trace store.

**Observed:** 2026-04-01. Another agent reported "4 fetch failed — Ollama connection drops on large payloads" but the dashboard showed only 1 failed request out of 24,650. The disconnect failures were all recorded as successful completions.

**Fix:** `GeneratorExit` now records status `"client_disconnected"` and calls `mark_failed` instead of `mark_completed`. The trace store gets the real status so the dashboard accurately reflects failure rates.

---

### 23. Incomplete Streams (No done:true) Recorded as "completed" `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** High

If Ollama drops the TCP connection after sending partial data but without raising an exception, httpx's `aiter_lines()` stops iterating cleanly. The `finally` block saw `error_occurred = False` and marked it "completed" — even though the response was truncated and Ollama never sent the final `done: true` chunk.

**Fix:** After the stream loop completes without error, check if `_request_tokens` has an entry for this request (only populated when `done: true` is parsed in `stream_from_node`). If missing, record as `"incomplete"` and call `mark_failed`. This catches Ollama process deaths, OOM kills, and silent connection drops.

---

## Future Considerations

- **Extract dashboard frontend** — see issue #9 above
- **`event_stream()` optimization** — see issue #8 above
- **Tag filtering on Trends/Models** — see issue #15 above
- **`collector.py` catch-all** — silently returns empty metrics when Ollama is unreachable, which could mask bugs during development. Consider logging at `WARNING` level.

### #21 — Empty error messages on timeout failures `FIXED`
**File:** `server/streaming.py`
**Severity:** Low
**Problem:** httpx timeout exceptions have empty `str(e)`, so `f"{type(e).__name__}: {e}"` produces `ReadTimeout: ` with no details.
**Fix:** Use `repr(e)` as fallback when `str(e)` is empty: `f"{type(e).__name__}: {repr(e)}"`. Now captures the exception args (timeout value, URL, etc.) even when the string representation is empty.

---

### 22. Custom Date Range Selector for Dashboard Pages `FIXED`

**Files:** `src/fleet_manager/server/routes/dashboard.py`, `src/fleet_manager/server/trace_store.py`
**Severity:** Low (feature enhancement)

The Trends page has preset time buttons (24h, 48h, 72h, 7d) but no custom date/time range selector. The Model Insights and Apps pages have a `days` parameter but no time range UI at all.

**Proposed fix:**

1. **Shared date range component** — reusable across Trends, Model Insights, and Apps pages:
   - Preset buttons: 24h, 48h, 72h, 7d, 30d
   - Custom range: two datetime-local inputs (start, end)
   - All times in user's local timezone (JS `Date` handles this natively)
   - Component stores selection in URL params for shareability

2. **Backend changes:**
   - Add `start_ts` and `end_ts` query params to `/dashboard/api/trends`, `/dashboard/api/models`, `/dashboard/api/apps`
   - TraceStore queries already filter by timestamp — just expose the params
   - Timezone conversion: frontend sends UTC timestamps, backend uses them directly (traces are stored as Unix timestamps)

3. **Pages to update:**
   - Trends: replace current time buttons with shared component
   - Model Insights: add time range component (currently hardcoded to `days` param)
   - Apps: add time range component (currently hardcoded to `days` param)

---

## Model Management

### No priority/pinned model concept — restarts can evict primary models `FIXED`

**Severity:** High
**Discovered:** 2026-04-16 — during vision embedding testing, repeated fleet restarts (`pkill -9`) caused `gpt-oss:120b` (89GB, primary reasoning model) to be unloaded. VRAM fallback then routed requests to `gemma3:27b` (42GB), which loaded and consumed the memory `gpt-oss:120b` needed. Result: primary model evicted, replaced by a less capable one.

**Root cause:** No concept of "this model must always be loaded." VRAM fallback picks whatever is loaded without considering model importance. Ollama's `OLLAMA_KEEP_ALIVE=-1` keeps models loaded but can't prevent eviction when memory is consumed by other models loading first after a restart.

**Proposed fix:**
1. Add `FLEET_PINNED_MODELS` config — comma-separated list of models that must always be loaded (e.g., `gpt-oss:120b,nomic-embed-text`)
2. After node restart, load pinned models first before accepting other requests
3. VRAM fallback should never route to a non-pinned model if a pinned model exists for that category
4. Health check: WARNING if a pinned model is not loaded
5. Dashboard Settings: UI to manage pinned models

**Files:** `server/streaming.py` (VRAM fallback), `node/agent.py` (startup model loading), `models/config.py` (pinned models config), `server/health_engine.py` (health check)

### CoreML provider triggers macOS TCC dialog that freezes the node agent `FIXED`

**Severity:** Critical
**Discovered:** 2026-04-19 — after adding the vision embedding service, the node agent began freezing overnight. User reported a macOS permission dialog appearing asking Python for access. The dialog blocks the Python process indefinitely, causing heartbeats to stop, router marks node offline, all inference fails until someone dismisses the dialog.

**Pattern:** Consistent 120-130 errors/hour from midnight through 8 AM. Not random drops — a steady multi-hour outage until the user returns to the machine and dismisses the dialog. Happened twice in 5 days (April 14 and April 19).

**Root cause:** `CoreMLExecutionProvider` in `ONNXBackend.__init__` was enabled automatically on macOS. On first inference, CoreML compiles the ONNX model for the Neural Engine, which can trigger macOS TCC permission dialogs (Neural Engine access, Desktop folder access if cache scans adjacent paths). Once the dialog appears, the subprocess and the entire Python process block waiting for user interaction.

**Fix (commit d61d3cb+):** Default to CPU-only inference. CPU is fast enough on M-series chips (~60ms/image for DINOv2). Users can opt-in to CoreML via `FLEET_EMBEDDING_USE_COREML=true` with a warning in the logs.

**Files:** `src/fleet_manager/node/embedding_models.py`

---

### Queue concurrency ignores OLLAMA_NUM_PARALLEL — allows 8 in-flight but Ollama only runs 2 `OPEN`

**Severity:** Medium
**Discovered:** 2026-04-16 — dashboard always shows "1/8 in-flight" regardless of model or node. On a 512GB machine the concurrency formula always hits the `_MAX_CONCURRENCY=8` cap because headroom is massive (436GB / 2GB per slot = 218, clamped to 8).

**Root cause:** `compute_concurrency()` in `queue_manager.py` calculates slots from memory headroom divided by estimated KV cache cost (2GB), then clamps to `[1, 8]`. It has no knowledge of `OLLAMA_NUM_PARALLEL`, which controls how many requests Ollama actually processes simultaneously. With `OLLAMA_NUM_PARALLEL=2`, the queue allows 8 in-flight but Ollama queues anything beyond 2 internally, adding unnecessary latency.

**Impact:** On a 512GB machine with `OLLAMA_NUM_PARALLEL=2`:
- Queue reports 8 concurrency slots
- Ollama processes 2 at a time
- 6 requests sit in Ollama's internal queue, invisible to Herd's scoring
- Scoring engine thinks the node has capacity when it's actually backed up
- Wait time estimates are wrong

**Proposed fix:**
1. Node agent reads `OLLAMA_NUM_PARALLEL` from environment or Ollama's config and reports it in the heartbeat
2. `compute_concurrency()` uses `min(memory_slots, ollama_num_parallel)` instead of just memory slots
3. If `OLLAMA_NUM_PARALLEL` is not reported, fall back to current memory-based calculation
4. Dashboard shows actual concurrency (e.g., "1/2" not "1/8")

**Files:** `server/queue_manager.py` (compute_concurrency), `node/collector.py` (read OLLAMA_NUM_PARALLEL), `models/node.py` (add to heartbeat)

**Files:** `server/streaming.py` (VRAM fallback), `node/agent.py` (startup model loading), `models/config.py` (pinned models config), `server/health_engine.py` (health check)

---

### Ollama 3-model concurrent-load cap unconfigurable on macOS (upstream) `OPEN`

**Severity:** Medium
**Discovered:** 2026-04-22 — during Claude Code + ollama-herd setup on a 512GB M3 Ultra Mac Studio
**Upstream:** [ollama/ollama#7041](https://github.com/ollama/ollama/issues/7041), [#4855](https://github.com/ollama/ollama/issues/4855), [#5722](https://github.com/ollama/ollama/issues/5722), [#14953](https://github.com/ollama/ollama/issues/14953)

**Symptom:** Ollama 0.20.4 on macOS refuses to keep more than 3 models concurrently hot in VRAM, regardless of `OLLAMA_MAX_LOADED_MODELS` configuration. Loading a 4th model always evicts one of the existing three (LRU). Silently causes herd's VRAM fallback to fire and degrades Claude Code tool-use quality when mapped models get evicted.

**Root cause (partial):** From Ollama source (`envconfig/config.go`):

```go
MaxRunners = Uint("OLLAMA_MAX_LOADED_MODELS", 0)
```

- `Uint` parses the env value as **unsigned integer**
- `-1` cannot be parsed as unsigned → silently falls through to default `0`
- `0` resolves to `defaultModelsPerGPU = 3` in the scheduler

So setting `OLLAMA_MAX_LOADED_MODELS=-1` (a common "I want unlimited" pattern that propagated via our shell init files) was silently invalid. **But setting a positive integer doesn't fix it either** — see test table below.

**Test evidence (all on Ollama 0.20.4 / macOS 15.x / M3 Ultra 512GB):**

| Attempt | Process env (`ps eww`) | Cap behavior |
|---|---|---|
| `launchctl setenv OLLAMA_MAX_LOADED_MODELS 10` (confirmed via `launchctl getenv`) | shows `-1` | still 3 |
| Plist `EnvironmentVariables.OLLAMA_MAX_LOADED_MODELS=10` at `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` | regenerated by brew on restart, stripped | still 3 |
| `~/.zshrc` `export OLLAMA_MAX_LOADED_MODELS=10` | only affects new shells, not GUI Mac App | still 3 |
| Direct CLI: `OLLAMA_MAX_LOADED_MODELS=10 /Applications/Ollama.app/Contents/Resources/ollama serve` | process still shows `-1` | still 3 |
| Full kill (Mac App + all runners) + fixed launchctl + clean relaunch via `open -a Ollama` | process still shows `-1` | still 3 |
| Load 4 **distinct** models (different weight blobs) to rule out shared-blob conflict | — | 4th evicts LRU; memory not the constraint |

**Memory was never the issue.** During the 4-distinct-model test: 358 GB of RAM available, 149 GB hot, Ollama still refused the 4th load. The dashboard shows ~292 GB used / 512 GB — plenty of headroom.

**Impact on ollama-herd:**

- **VRAM fallback fires silently** — when a mapped model in `FLEET_ANTHROPIC_MODEL_MAP` isn't hot, requests fall back to nearest available model. Debugged via `x-fleet-fallback` response header. For Claude Code specifically, this means tool-heavy requests hit weaker models (e.g. gemma3:4b) that can't emit `tool_calls` cleanly, breaking the agent loop.
- **Typical Claude Code fleet needs 4+ hot models** — 1 for haiku, 1 for sonnet, 1 for opus (or pull 480B), 1 for vision, plus user's non-Claude-Code scripts. Currently forced to cap at 3 and accept reload cost on the rest.
- **Captures of this failure mode are invisible** until you check the trace DB — no health check currently surfaces it.

**Proposed workarounds:**

1. **Accept the 3-cap** (current behavior) — pick the 3 most critical models per node, accept reload cost on others
2. **Second Ollama instance on another port** — each daemon has its own 3-slot budget; register both as separate nodes or route-target in herd. Doubles capacity to 6 hot.
3. **`mlx-lm.server` for specific heavyweight models** — bypass Ollama entirely; no cap, plus potential 2× decode speed and native prefix caching. Moderate engineering.
4. **Wait for upstream fix** — the pattern of `Uint` + silent `-1` failure is reported across multiple open issues; unclear if anyone is working on it.

**Proposed fix in herd (already planned):** see `docs/plans/hot-fleet-health-checks.md` — adds six health checks that would surface this failure mode within one heartbeat interval instead of requiring trace DB archaeology. Specifically check #3 (`ollama_max_loaded_models_observed`) infers the effective cap from observed behavior rather than reading the unreliable env var.

**Files (herd-side mitigation only):** `server/health_engine.py`, `node/collector.py` (optional: report observed cap in heartbeat payload)

---

## OPEN — Codex `/v1/models` schema is an unbounded decode chain

**Severity:** low (cosmetic for the CLI; empties the Desktop model picker)
**Found:** 2026-07-18 driving a real `codex-cli 0.145.0-alpha.18`

Codex decodes `/v1/models` against its own **undocumented, strictly-typed**
schema and fails the *entire* decode on the first problem. Each field added
reveals exactly one more. We currently emit 20 fields discovered this way,
including two closed enums (`shell_type`, `visibility`) and a nested
`truncation_policy` struct. **Next known requirement:
`experimental_supported_tools`.**

Not converged, and treated as maintenance rather than a milestone. The CLI is
unaffected (`-m` bypasses the picker), but an undecodable payload leaves the
Desktop picker empty, which pushes Desktop onto its built-in `sol`/`luna`/`terra`
Lite slugs — a materially different code path.

**Discovery loop:** add the field → restart herd → run `codex exec` **three**
times → `grep -o 'missing field \`[a-z_]*\`'`. A *single* run after a restart
reports a false clean (the models refresh hasn't fired yet); so does a dead
server (no response, no decode error). Assert liveness in the same breath.
Wrong enum values are useful — the error names the valid variants.

**Files:** `server/routes/openai_compat.py` (`list_models`)

---

## OPEN — `write_stdin` round-trips but local models can't drive it

**Severity:** low
**Found:** 2026-07-18

The protocol path works — Codex accepts the call and returns output. But
`qwen3-coder:30b` could not drive an interactive session: it started `python3 -i`
via `exec_command`, sent input with `write_stdin`, received the echo rather than
the evaluated result, retried several times with different framings, and gave
up. In an earlier run it then *claimed* the session printed `42` — a
confabulation (`6*7` is derivable without executing anything).

No herd-side defect identified. Recorded so the next person doesn't re-derive
it, and as a caution: verify interactive-session claims against the tool blocks,
never the model's summary.

---

## OPEN — one `apply_patch` call bypassed the redirect, unreproduced

**Severity:** low
**Found:** 2026-07-18

During tool-coverage testing, `error=unsupported call: apply_patch` fired once
while the redirect fired 4 times in the same window — so a single call had a
shape `_patch_text_from_args` did not recognise. Not reproduced since.

The decline path is now audible (`no recognisable patch envelope (keys=…)`),
so a recurrence names the arg keys and is fixable in one pass instead of
requiring another debugging round.

**Files:** `server/responses_translator.py` (`_patch_text_from_args`)

---

## RESOLVED (not a bug) — `glm-4.7-flash` slowness is contention, not the model or its context

**Severity:** none for herd — the behaviour is a scheduling consequence, not a defect
**Found:** 2026-07-19 · **Root-caused:** 2026-07-19

**Symptom:** requests to `glm-4.7-flash:latest` took 500–640s, then failed at 1,204–5,205s.

**Answer:** decode throughput tracks concurrent fleet traffic almost monotonically. Measured across 22 production calls by counting requests that overlapped each one:

| overlapping non-glm requests | glm decode |
|---|---|
| 0 | **45.3 tok/s** |
| 2–9 | 34.8–35.9 |
| 15–22 | 22.7–32.0 |
| 27–34 | 10.0–17.7 |
| 38–66 | **6.4–7.8** |

A clean ~7× dose-response curve. These MoE models are memory-bandwidth-bound on Apple Silicon, and bandwidth is shared.

**Two wrong theories, and how they died** — worth recording because both were plausible and one nearly got "confirmed":

1. *Prefill regression* (this model has a **fixed** prefill issue on record, so it is the natural suspect). Killed by `time_to_first_token_ms`: prefill is a healthy 13–35s even on the failures; all degradation is decode.
2. *Residency at ctx=202752 / 63.2 GB, with the 32768 override never applied.* Killed by reproducing the **exact** production shape (14,322-token prompt → 3,365 generated) in isolation **at that same residency**: **105 seconds**, versus 500–640s in production. Same context, same model, 5–6× faster with an idle fleet.

   The proposed fix was to unload the model so it would cold-load at 32768. That would have appeared to work — the unload also drains the queued backlog — and the recovery would have been credited to the context change. A fix that works for the wrong reason is worse than no fix.

**A measurement error worth not repeating:** the "78.5 tok/s in isolation" figure that framed this whole investigation used a **40-token prompt**. At the real ~14K prompt it is **32 tok/s**; decode degrades with used context (79.9 → 40.2 → 31.9 as the prompt grows). Benchmark the workload's shape, not a convenient one.

**Why it escalates to failure:** an external caller polls this model about every 10 minutes. Under load a call takes 8–10 minutes, which exceeds the interval, so the next request stacks behind it — and each queued request slows the others further. Self-reinforcing. The four failures share one timestamp with decode times in a ~600s ladder (1,170 / 3,981 / 4,587 / 5,192), which is one pile-up, not four slow requests.

**Mitigations** (caller-side or scheduler-side, not model-side):
- Cap per-model concurrency so requests queue cleanly instead of degrading each other — see the bandwidth-aware concurrency issue below.
- Lengthen the poll interval past p99-under-load (~15 min) so calls cannot overlap.
- Reduce output length; 3,500–5,000 tokens at a 10-minute cadence is the real driver.

---

## OPEN — `compute_concurrency` sizes queues by memory capacity, and the bottleneck is neither capacity nor bandwidth

**Severity:** Medium — costs latency under load on every large-model queue; no correctness impact
**Found:** 2026-07-19, root-causing the `glm-4.7-flash` slowdown above
**Files:** `server/queue_manager.py` (`compute_concurrency`, `_compute_queue_concurrency`), `server/hardware_lookup.py`, `server/scorer.py`

### The mismatch

```python
# server/queue_manager.py
def compute_concurrency(available_memory_gb: float, model_size_gb: float) -> int:
    headroom = available_memory_gb - model_size_gb
    slots = int(headroom / _KV_CACHE_PER_REQUEST_GB)
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, slots))   # [1, 8]
```

Purely capacity-driven: "how many KV caches fit in RAM?" On a 512 GB M3 Ultra the headroom is always vast, so it returns the ceiling of **8** for every queue — confirmed live on both `bb:gpt-oss:120b` and `bb:qwen3-coder:30b`.

But nothing about this hardware is capacity-limited during inference. MoE decode on Apple Silicon is **memory-bandwidth-bound**, and bandwidth is shared across every concurrently-decoding model. Admitting 8 concurrent requests doesn't use idle capacity; it splits a fixed bandwidth budget 8 ways.

### Evidence

Measured on this fleet (see the glm entry above): decode throughput vs. overlapping requests — 45.3 tok/s at zero overlap, 6.4 tok/s at 38–66. A ~7× swing driven entirely by concurrency.

And from the 0.32.1 upgrade research, per-request vs aggregate on qwen3-coder:

| concurrent | tok/s per request | aggregate |
|---|---|---|
| 1 | 107.3 | 107 |
| 2 | 80.2 | 160 |
| 4 | 52.7 | 211 |

Aggregate genuinely rises, so this is a **latency/throughput trade, not free loss** — an earlier claim that concurrency bought "only ~10% aggregate" came from contention-polluted traces and was wrong. The point is that the trade is currently being made *blind*: nothing in the decision knows bandwidth exists.

### Why this is cheap to fix

The data is already present and already trusted elsewhere. `hardware_lookup.resolve_bandwidth()` returns 819 GB/s for this node's `Apple M3 Ultra`, it is populated on the live heartbeat (`node.hardware.memory_bandwidth_gbps`), and `scorer.py` already consumes it for signals 3/4/5. `compute_concurrency` is the one place that ignores it.

### Prototype sketch

Derive slots from bandwidth per in-flight stream rather than RAM per KV cache, keeping capacity as an upper bound:

```
bytes_per_token ≈ active_params × bytes_per_weight        # MoE: active experts, not total
streams_at_target ≈ (bandwidth_gbps × utilisation) / (bytes_per_token × target_tok_s)
slots = clamp(min(capacity_slots, streams_at_target), 1, 8)
```

Open questions the design has to answer:
- **What is the target?** A fleet tuned for interactive coding wants per-request latency; a batch benchmark wants aggregate. This probably needs to be a policy knob, not a constant — and per-model, since a compaction model and an interactive model want opposite answers.
- **Where does `active_params` come from?** `model_knowledge` has expert counts for some models; Ollama's `/api/show` exposes `expert_used_count` and `expert_count`. Prefer measured over declared, consistent with how KV cost per token is already learned from heartbeat data.
- **Does it need to be adaptive?** The honest version measures achieved tok/s per queue from the trace store (that data exists) and closes the loop, rather than predicting from a static table.
- **Interaction with `OLLAMA_NUM_PARALLEL`.** Ollama has its own admission limit (currently 4 on this fleet). Herd handing 8 workers to a backend that runs 4 means the extra just queue inside Ollama, where herd cannot see or reorder them. The two limits should be reconciled, and the node already reports its cap in the heartbeat.
- **Multi-model contention is the actual case.** The glm collapse was caused by *other models'* traffic, so a per-queue cap alone does not solve it. A node-level bandwidth budget shared across queues is the more correct model, and considerably more invasive.

### Controlled sweep (2026-07-19, glm-4.7-flash, idle fleet, ~1.5K prompt, 300 tok out)

| N | per-stream tok/s | aggregate tok/s |
|---|---|---|
| 1 | 74.7 | 63.0 |
| 2 | 56.4 | 91.6 |
| 3 | 43.7 | 125.8 |
| **4** | 35.5 | **137.5** |
| 6 | 42.6 | 127.8 |
| 8 | 35.3 | 137.8 |

**Aggregate saturates at N=4 and never improves.** Per-stream halves from 1→4. So slots 5-8 buy *nothing* in throughput and cost latency — herd's current ceiling of 8 is strictly worse than 4 on this hardware, under any policy.

**But the plateau is almost certainly not the bandwidth knee — it is `OLLAMA_NUM_PARALLEL=4`.** Ollama admits 4; requests 5-8 queue *inside* Ollama where herd cannot see, reorder or reject them. That fully explains why N=6 and N=8 match N=4 aggregate, and it makes the N=6 per-stream reading (42.6, higher than N=4's 35.5) measurement noise from uneven queue draining rather than signal.

Two consequences:

1. **The cheapest correct fix is not a bandwidth model at all** — it is to stop handing a backend more concurrent work than it will admit. The node already reports its cap in the heartbeat (`OllamaMetrics.max_loaded_models` exists; `num_parallel` should join it), and `hot_model_cap_for(node)` is the established pattern for consuming such a value. Capping queue concurrency at the backend's own parallelism converts invisible in-Ollama queueing into visible herd queueing, which is schedulable.
2. **The real bandwidth knee is still unmeasured.** Finding it requires sweeping `OLLAMA_NUM_PARALLEL` itself (1, 2, 4, 8) with an Ollama restart per step, and repeating per model class. Until then, any bandwidth formula would be fitted to a curve that is really an admission limit.

### ⚠️ Premise correction (2026-07-19, later) — it is NOT bandwidth-bound

This issue was opened as "bandwidth-aware concurrency." **That framing is wrong**, and the evidence needs no byte estimates:

- **gpt-oss-20b decodes at 116.08 t/s on M2 Ultra and 115.52 t/s on M3 Ultra** — flat, despite the Ultra's extra bandwidth ([llama.cpp #15396](https://github.com/ggml-org/llama.cpp/discussions/15396), maintainer's own numbers).
- **Qwen3-30B-A3B q4 hits 113.33 t/s on a 546 GB/s M4 Max**, versus our ~107 t/s on an **819 GB/s** M3 Ultra.

More bandwidth buys nothing at batch 1. Arithmetic on achieved traffic agrees: a single stream reaches only **~25%** of 819 GB/s on qwen3-coder and **~10–18%** on glm-4.7-flash.

**The likely real constraint is GPU occupancy.** At decode, `mul_mat_id` dispatches a grid of `n_expert_used × n_tokens` independent *small* mat-vecs — each too small to fill an 80-core GPU. That explains why the wider Ultra gains nothing over the Max. (Structural cost read from [`ggml-metal-ops.cpp`](https://github.com/ggml-org/llama.cpp/blob/master/ggml/src/ggml-metal/ggml-metal-ops.cpp); no maintainer asserts it is *the* measured bottleneck, and there is **no upstream issue tracking MoE decode on Metal** — a GitHub search for issues titled `metal`+`moe` returns zero.)

Also worth knowing: **`mul_mat_id` has no scattered row-gather at decode** — it does pointer arithmetic into a contiguous expert block, then a dense mat-vec. The "uncoalesced reads" hypothesis does not describe the implementation. And every merged Metal MoE optimisation PR (#12612, #13388, #15541) improves **prefill only**; none claims a decode win.

### The number that should drive `OLLAMA_NUM_PARALLEL`

From `llama-batched-bench` on M2 Ultra, gpt-oss-20b, npp=1024/ntg=32 ([#18308](https://github.com/ggml-org/llama.cpp/discussions/18308)):

| B | prefill t/s | decode aggregate | per-stream |
|---|---|---|---|
| 1 | 2361 | 128.8 | 128.8 |
| 4 | 2409 | 211.1 | 52.8 |
| 8 | 2420 | 245.7 | 30.7 |
| 24 | 2420 | 361.1 | 15.0 |

**Prefill throughput is dead flat at every batch size.** It is already compute-saturated at B=1. For agentic coding — high prompt:generation ratio — *total* throughput moves only **1.41×** across the entire B=1→32 sweep. Batching helps decode and does nothing for the phase that dominates our traffic.

Corroborating on Ollama specifically: with the default `num_parallel=1`, aggregate is flat from concurrency 1→8; setting 4 raises aggregate ~1.8× **but is slower at concurrency 1** (18.4 vs 21.7 t/s). Enabling slots costs single-stream latency.

### What the literature does (and does not) do

Two independent audits of ~20 SLO-aware serving systems (vLLM, SGLang, Dynamo, SCORPIO, SOLA, TaiChi, BucketServe, VoltanaLLM, CONCUR, SLOs-Serve, …):

- **Nobody targets p99 as a *control objective*.** Percentiles appear only as evaluation statistics. Every system uses per-request deadlines aggregated to a fraction-satisfied rate.
- **Nobody measures memory bandwidth as a live signal.** Several invoke memory-boundedness rhetorically, then substitute analytical proxies — token counts, KV lengths, batch sizes. Confirmed by full-text search on the three most likely candidates.
- **The dominant pattern is certainty-equivalence feedforward:** predict latency → solve a constraint → set the knob, with no corrective path for prediction error. `vLLM`'s `max_num_seqs` and `max_num_batched_tokens` are confirmed **static config**.
- The technique we want — sample completed-request latency, estimate safe concurrency, shed — **exists and is mature outside LLM serving**: Envoy's adaptive concurrency filter (gradient controller on p90 `sampleRTT`, 100ms windows), Netflix's gradient concurrency-limits. **But Envoy is structurally blind to streaming**: it measures whole-request RTT, where a long response and a slow response look identical. That is precisely why TPOT — not latency — has to be the controlled variable here. The pattern needs adapting, not copying.

**The one genuine feedback loop in production LLM serving is SGLang's, and it is worth studying.** `new_token_ratio` is a discount factor on each request's declared `max_new_tokens`, AIMD-shaped: on KV exhaustion the scheduler retracts requests and jumps the ratio up based on *observed* decode progress; on healthy steps it decays down over ~600 steps. The signal is **behavioural, not memory-physical** — it learns that most requests hit EOS well before `max_new_tokens`, which is the single most predictive admission signal anyone has shipped. The knob is a KV-consumption estimate rather than a concurrency cap, and the objective is avoiding retraction rather than any latency SLO, but the shape is right.

**Correction to the entry above:** Sarathi-Serve's token budget is **static**, from the paper's own text — *"We leverage Vidur, a LLM inference profiler and simulator to determine the token budget"* and *"dynamically varying the token budget… We leave this exploration for future work."* Concrete values in its eval: 2048 relaxed, 512 strict. It also does **no admission control** — it only composes batches. Chunked prefill remains a real and relevant lever for us; it is just not an adaptive one, and I previously implied otherwise.

**Everyone else sizes admission from static memory arithmetic.** vLLM's real defaults come from a hand-maintained lookup table keyed on `device_memory >= 70 GiB` — including an A100 carve-out that is empirical folklore encoded in source (`"Setting large max_num_batched_tokens for A100 reduces throughput, see PR #17885"`). TGI profiles once at startup, and on flash-attention models **ignores** `--max-batch-total-tokens` entirely. TensorRT-LLM's are build-time. Its `AutoTuner`, routinely cited as runtime batch tuning, is a **kernel tactic selector** — citing it for admission is a category error.

### Research findings (2026-07-19) — and they argue against leading with a cap

**MoE batching does not amortise like dense, which is the whole story.** Dense decode reads the weight set once per step regardless of batch, so batching is near-free throughput. With top-k-of-N routing, batching B streams activates the *union* of their experts — expected unique experts `N·(1-(1-k/N)^B)` — so weight bytes grow with B. A two-term model (dense bytes once + expert bytes by that fan-out) fitted on **only** our B=2 point predicted B=4 within **3.8%** (219 predicted vs 211 observed). A dense 30B would have gone 107→214→428; we got 211. Roughly half the dense benefit, and the tax is expert fan-out.

**Decode across llama.cpp slots IS genuinely batched.** Our own aggregate rising 107→160→211 proves it behaviourally — pure interleaving would have stayed flat at ~107. So concurrency is a real throughput win here, just a sharply diminishing one. (Source-level confirmation of a single `llama_decode` over a multi-slot batch: unverified.)

**Apple Silicon offers no hardware lever.** Verified from the macOS SDK headers: `MTLCommandQueue` exposes only `label` and `device`; the sole `priority` in Metal is `MTLIOPriority` on *asset-loading* queues. No MPS/MIG equivalent, no GPU compute QoS. Any budget must be enforced by admission control above the runtime — which makes the router the only possible enforcement point.

**A node-level bandwidth budget across model queues is novel in LLM serving, but has strong precedent in datacenter QoS.** Checked Clockwork, AlpaServe, MuxServe, Prism, ServerlessLLM, Salus — all budget capacity and/or compute time; none budget bandwidth. MuxServe is closest and partitions SMs, after measuring that decode latency is flat from 30%→100% SM allocation (i.e. it detects decode is bandwidth-bound, then budgets the resource that isn't the bottleneck). The template to copy is **Heracles** (ISCA'15), which had the same problem for DRAM — no hardware mechanism existed, so it measured aggregate bandwidth and throttled the co-runner's *concurrency*. That validates concurrency as the actuator even when bandwidth is the resource.

### The metric: TPOT, validated on our own traces

```
TPOT = (latency_ms - time_to_first_token_ms) / (completion_tokens - 1)
```

TTFT absorbs queue wait and prefill; the remainder is near-pure decode, so TPOT degrades if and only if decode is genuinely contended. Every column already exists. Measured across 10,686 completed traces, bucketed by node-wide overlapping requests:

| node-wide concurrency | n | median TPOT | implied tok/s |
|---|---|---|---|
| 1–2 | 4,330 | 19.6 ms | 51.1 |
| 3–5 | 4,483 | 31.0 ms | 32.2 |
| 6–10 | 1,625 | 44.8 ms | 22.3 |
| 11–25 | 234 | 36.3 ms | 27.6 |
| 26+ | 14 | 87.3 ms | 11.5 |

Monotonic apart from the 11–25 bucket (n=234 against 1,625 — likely sampling, not signal). **Control against p99 TPOT, not a memory number.**

### Counter-evidence: capping is probably not the highest-value lever

Three lines of published work address the same latency degradation *without* sacrificing throughput, and they should be evaluated before a cap ships:

- **Prefill stalls, not decode batching** — Sarathi-Serve (OSDI'24) attributes much of the damage to prefill iterations interrupting ongoing decodes, and fixes it with chunked prefill: 2.6× capacity for Mistral-7B on 1×A100 under tail-latency constraints. **Cheap diagnostic for us:** check whether inter-token latency spikes coincide with *new request arrivals* (long prompts landing mid-generation) rather than with steady queue depth. Must be measured per-backend — llama.cpp and `mlx_lm.server` schedule differently.
- **Head-of-line blocking** — FCFS is the default everywhere and causes HOL blocking; SJF-approximating schedulers recover latency at *zero* throughput cost (NeurIPS'24 learning-to-rank shows relative rank is predictable even though exact output length isn't). **Reordering costs nothing; capping costs throughput by construction.**
- **Fit the USL before picking a number** — `X(N) = γN / (1 + α(N−1) + βN(N−1))`, `N_max = √((1−α)/β)`. Fitting α (contention) and β (coherency) to fleet data gives a *derived* cap and, more importantly, distinguishes a plateau (α-dominated: a cap trades throughput for latency) from genuinely retrograde throughput (β>0: a cap recovers both). Our sweep plateaus rather than going retrograde — which weakens the case for capping as a throughput measure.

The strongest pro-capping number in the literature is Clockwork's (OSDI'20): concurrency bought ≤25% throughput while inflating tail latency **100×**, and its whole design is "execute one request at a time." Worth citing for the *mechanism* — but it is 2020-era fixed-shape DNN inference with no KV cache, and transferring the magnitude to autoregressive decode would be folklore.

### Measured on THIS fleet, 2026-07-19 — no published M3 Ultra multi-stream table exists

Unique random prompts per stream (prefix caching defeated — a first attempt with
near-identical prompts showed aggregate rising past the admission limit, which
was cache hits, not scaling). `decode t/s` is from Ollama's own `eval_duration`,
so it excludes prefill and queue wait. `OLLAMA_NUM_PARALLEL=4`.

**MoE — gpt-oss:120b** (128 experts, 4 active), our production model:

| N | decode t/s per stream | aggregate t/s |
|---|---|---|
| 1 | 36.2 | 28.9 |
| 2 | 26.2 | 39.1 |
| **4** | **27.1** | **60.7** |
| 8 | 28.9 | 58.2 ← retrograde |

**Dense — gemma3:27b**, same box, same session:

| N | decode t/s per stream | aggregate t/s |
|---|---|---|
| 1 | 21.3 | 7.1 |
| 2 | 13.6 | 16.1 |
| 4 | 7.4 | 18.1 |
| 8 | 6.6 | 18.3 |

**Four conclusions, and one of them refutes an earlier recommendation in this issue.**

1. **`NUM_PARALLEL=4` is a good setting; my suggestion to try 2 was wrong.** At N=4 both aggregate *and* per-stream beat N=2 (60.7 vs 39.1 aggregate; 27.1 vs 26.2 per stream). There is no latency argument for 2 here.
2. **N=8 is genuinely retrograde** (58.2 < 60.7) — throughput *decreases* with added load, which is `β > 0` in USL terms and the strongest possible justification for a cap. The cap shipped in `075348e` lands exactly on the peak.
3. **Per-stream decode is flat from N=2 onward** (26.2 / 27.1 / 28.9). The cost of going concurrent is a one-time ~25% hit at N=1→2, not a progressive collapse. The 7× degradation seen in production traces is therefore *not* decode contention within one model — it is cross-model contention plus queue wait, which is why per-request `tok/s` misleads and TPOT does not.
4. **Dense scales worse than MoE here, which is the opposite of the theory.** gemma3:27b flattens at ~18 t/s aggregate while per-stream collapses 21.3 → 6.6 (3.2×); gpt-oss holds per-stream flat and doubles aggregate. The expert-fan-out model predicted MoE should batch *worse*. It doesn't — at least not for these two models. Confounded by different model sizes and quantisations, so treat as a strong hint rather than a result, but it is direct evidence against the mechanism this issue previously leaned on.

### Do not start by writing code

Start by reproducing the curve deliberately: drive N concurrent streams at fixed N against one model on an idle fleet, record per-request and aggregate tok/s, and find the knee. The numbers above are observational — gathered from production traffic that happened to vary — not a controlled sweep. A controlled sweep is what tells you whether the knee is at 2, 3, or 4, and whether it moves with model size.

---

## RESOLVED (keep the draft model) — the MLX compactor's `--draft-model` disables batching, and that is the right trade here

**Severity:** Medium — every compaction request serialises; invisible from config
**Found:** 2026-07-19, verified locally against the installed mlx-lm
**Files:** `~/.fleet-manager/env` (`FLEET_NODE_MLX_SERVERS`), `CLAUDE.md` (MLX gotchas)

`mlx_lm/server.py:371` (v0.31.3, installed):

```python
is_batchable = draft_model is None
```

Unconditional. Our port-11441 compactor is configured with
`"draft_model":"mlx-community/Qwen3-1.7B-4bit"`, so **`is_batchable` is `False` for
every request** — continuous batching (default `--decode-concurrency 32`, added in
mlx-lm 0.28.4) never engages, and concurrent compaction requests serialise.

This is a genuine either/or that our docs present as a pure win. CLAUDE.md
describes the draft model as giving "~94 tok/s on M3 Ultra" for the compactor; it
does, **for one request at a time**. Maintainer-measured batching on comparable
hardware (M2 Ultra, Qwen 30B/3B 4-bit, mlx-lm PR #626) gives batch 1 → 89 t/s,
batch 2 → 141, batch 4 → 204. So the trade is roughly *94 t/s serialised* versus
*~204 t/s aggregate across 4 concurrent requests, at ~51 t/s each*.

Which is correct depends on whether compaction requests arrive concurrently. With
a single Claude Code session they do not, and speculative decoding wins. With
several sessions compacting at once, the draft model is actively harmful.

### Decision (2026-07-19): keep it — measured, not assumed

The trade only matters if compactions overlap. **They essentially never do:
84 of 2,813 mlx-routed requests on record ever overlapped another — 3%.**
Compaction is a serial workload by nature; one coding session compacts at a
time, spaced by whole conversations. The 97% case is precisely where
speculative decoding wins, so the current config is correct.

A live A/B across the two MLX servers is directionally consistent — the
batching-enabled server's aggregate keeps climbing with concurrency (33 → 93 →
101 t/s at N=1/2/4) while the draft-model server peaks at N=2 and then declines
(33 → 74 → 63.5) — but it is **confounded**: different models on each port
(Qwen3-Coder-30B vs GLM-4.7-Flash). It is corroboration, not proof. A decisive
test needs the same model with and without the flag, which costs a duplicate
~19GB resident and is not worth it given the overlap rate.

**Revisit if** the fleet ever serves several concurrent coding sessions — the
overlap rate is the trigger, and it is one query:
`SELECT COUNT(*) FROM request_traces WHERE model LIKE 'mlx:%'` with an overlap
join. At meaningful overlap the trade inverts and the draft model becomes a
liability.

Two related facts worth recording while here:
- Requests that set a `seed` are also non-batchable and force a batch drain.
- mlx-lm #965 (KV-cache cross-contamination between concurrent requests on M3
  Ultra at 16+ concurrency) was fixed in v0.31.2 — we run 0.31.3, so we have the
  fix. Worth knowing before raising concurrency anywhere near that range.
