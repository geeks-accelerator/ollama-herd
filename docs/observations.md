# Observations

Patterns, insights, and learnings extracted from operating Ollama Herd. Each observation is a small unit of knowledge that compounds over time. The trace store, capacity learner, and JSONL logs are the raw data — this file is the extracted signal.

**Format:** Each observation has a date, a short title, the raw evidence, and the extracted insight. Observations accumulate — they're never deleted, only superseded by newer ones that refine the understanding.

---

## How to add observations

Query the trace store for patterns:

```bash
# Most common failure modes
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT error_message, COUNT(*) as n FROM request_traces WHERE status='failed' GROUP BY error_message ORDER BY n DESC LIMIT 10"

# Models that trigger the most fallbacks
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT original_model, model, COUNT(*) as n FROM request_traces WHERE fallback_used=1 GROUP BY original_model, model ORDER BY n DESC"

# Slowest node/model combinations
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT node_id, model, AVG(latency_ms) as avg_ms, COUNT(*) as n FROM request_traces WHERE status='completed' GROUP BY node_id, model HAVING n > 10 ORDER BY avg_ms DESC LIMIT 10"

# Retry frequency by node
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT node_id, SUM(retry_count) as retries, COUNT(*) as total FROM request_traces GROUP BY node_id ORDER BY retries DESC"

# Per-tag usage (which projects/processes use the most tokens)
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT j.value as tag, COUNT(*) as requests, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) as tokens FROM request_traces, json_each(tags) j WHERE tags IS NOT NULL GROUP BY j.value ORDER BY tokens DESC"

# Time-to-first-token distribution
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT node_id, model, AVG(time_to_first_token_ms) as avg_ttft, MIN(time_to_first_token_ms) as min_ttft, MAX(time_to_first_token_ms) as max_ttft FROM request_traces WHERE time_to_first_token_ms IS NOT NULL GROUP BY node_id, model"

# Hourly request patterns (when is the fleet busiest)
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT CAST((timestamp % 86400) / 3600 AS INTEGER) as hour, COUNT(*) as requests FROM request_traces GROUP BY hour ORDER BY hour"

# Cold model loads (TTFT > 40s = model was loaded from disk, not hot in memory)
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT model, node_id, COUNT(*) as cold_loads, ROUND(AVG(time_to_first_token_ms)/1000,1) as avg_load_sec FROM request_traces WHERE time_to_first_token_ms > 40000 GROUP BY model, node_id ORDER BY cold_loads DESC"

# Model thrashing detection (alternating cold loads = models evicting each other)
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT datetime(timestamp, 'unixepoch', 'localtime') as time, model, ROUND(time_to_first_token_ms/1000,1) as ttft_sec FROM request_traces WHERE time_to_first_token_ms > 40000 ORDER BY timestamp DESC LIMIT 20"
```

Check capacity learner state:

```bash
# View learned capacity patterns
cat ~/.fleet-manager/capacity_state.json | python3 -m json.tool

# Check current slot utilization
ls -la ~/.fleet-manager/logs/
```

Read JSONL logs:

```bash
# Recent errors
grep '"level":"ERROR"' ~/.fleet-manager/logs/*.jsonl | tail -20

# LAN proxy activity
grep 'lan_proxy' ~/.fleet-manager/logs/*.jsonl | tail -20

# Heartbeat failures
grep 'heartbeat.*fail' ~/.fleet-manager/logs/*.jsonl | tail -20
```

When you see a pattern, add it below with the date and evidence.

---

## Observations

### 2026-07-17 — Ollama 0.30.2 changed `prompt_eval_count` to include cached prefix tokens

**Evidence:** Ollama 0.30.2 release note: *"The llama.cpp backend now includes cached prompt tokens in token accounting, improving usage reporting for requests with prompt cache hits."* We read `prompt_eval_count` directly at `streaming.py` (the `done:true` branch) and feed it to traces → `context_optimizer`'s `total_p99` → dynamic num_ctx sizing. Before 0.30.2, on a prefix-cache hit Ollama reported only the *newly-evaluated* tokens, so a cache hit made a large prompt look small — **undercounting** true prompt size. After 0.30.2 it reports the full logical prompt.

**Insight:** This is a net **accuracy win** for us, not a bug — dynamic num_ctx and the KV-sizing work (see `docs/issues/model-sizing-ignores-kv-cache.md`) now run on truthful prompt sizes. Two things follow. (1) During the 7-day p99 window rollover, stats briefly blend pre-0.30.2 (undercounted) and post-0.30.2 (accurate) traces; self-correcting, no action. (2) `prompt_eval_count` is now unambiguously *logical prompt size incl. cached*, **not** "tokens actually computed" — so it must never be used to infer prefix-cache effectiveness. That exact confusion produced the false "zero prefix-cache reuse" report (`docs/issues/ollama-native-mlx-runner.md` Q3: the number stays flat whether caching works or not). Verified 0.32.1 tok/s estimation is unaffected — `benchmark_estimate.py` uses `completion_tokens / latency`, never `prompt_eval_count`.

**Action taken:** Comment at the `prompt_eval_count` read in `streaming.py` recording the semantics. No code change — the value was already the right one for context sizing. Part of the post-0.32.1 upgrade audit (`docs/plans/post-0.32-enhancements.md`).

---

### 2026-07-17 — On Ollama 0.32.1, an over-context single message truncates (HTTP 200), it does not error

**Evidence:** The 0.30.9 release note said *"Ollama will now return an error if a single message is larger than the current context window."* Probed directly against the installed 0.32.1: loaded `gemma3:4b` with `options.num_ctx=512` (Ollama clamped it up to its **2048 floor**) and POSTed `/api/chat` a ~5000-token single message. Result: **HTTP 200**, `prompt_eval_count: 1027` — i.e. the input was **truncated to fit** the window, not rejected. Repeated with `num_ctx=256` and a larger message: same, 200 + clamped count.

**Insight:** Whatever 0.30.9 introduced, it does **not** manifest for the `/api/chat` path we use on 0.32.1 — the long-standing silent-truncation behavior persists. So `check_context_overflow`'s "input may be truncated by Ollama" warning is **still accurate**, and there is no new error class to classify or retry-guard. This is why the enhancement plan front-loaded an empirical probe before writing any code: the changelog predicted a behavior change that the running binary doesn't exhibit on our path. (Untested edge: a message exceeding the model's *maximum* context, where no truncation is possible — not worth a 131K-token probe against live inference.)

**Action taken:** None in code — the predicted gap doesn't exist. Recorded so nobody re-implements a guard for a non-occurring error. Part of the post-0.32.1 upgrade audit.

---

### 2026-04-22 — Four-env-var combo makes 30B models at 131K ctx viable on 128 GB Macs

**Evidence:** qwen3-coder:30b-agent at 131K ctx on an M4 Max 128 GB MacBook was failing every real Claude Code request with SIGKILL'd llama runners (`sys=9 string="signal: killed"`, `Post /completion: EOF`) during generation. Latencies climbed 19s → 281s before Jetsam killed the subprocess. Model fit at rest (31 GB ollama ps footprint) but KV growth during actual generation pushed system memory from ~90 GB in use to 127+ GB. Setting one knob at a time:

- `OLLAMA_NUM_PARALLEL=1` alone — model footprint dropped at-rest, but KV still grew during generation because of f16 KV cache
- Adding `OLLAMA_KV_CACHE_TYPE=q8_0` + `OLLAMA_FLASH_ATTENTION=1` — halved remaining KV footprint; 25 GB model, 14 GB free headroom during sustained load
- `OLLAMA_KEEP_ALIVE=-1` — necessary so Herd controls lifecycle instead of Ollama's 5-minute idle timer evicting mid-session

Stress test result after all four: 6/6 of the `big_agentic` pattern (55 messages, 27 tools, 32K max_tokens, streaming) passed with p50 ≈ 1s latency. Pre-fix: 0%.

**Insight:** `OLLAMA_NUM_PARALLEL` defaults to 4 on macOS regardless of memory. At 131K ctx that's ~60 GB of pre-allocated KV buffer on a 128 GB machine — invisible at rest, fatal the moment real generation starts. The "the model fits!" gut check is wrong here; what matters is `weights + parallel_slots × ctx_length × kv_bytes_per_token`. For memory-tight Apple Silicon fleets running large-context MoE models, the four-env-var combo is the difference between "toy" and "production Claude Code backend."

**Action taken:** Documented in `docs/troubleshooting.md` ("Ollama llama runner killed by OS") and `docs/operations-guide.md` ("Memory Tuning for Memory-Tight Nodes") with observed before/after numbers. Health engine's "KV cache bloat" detector already surfaces this class of issue on the dashboard. Complements the Ollama watchdog's new escalation path that restarts `ollama serve` after 3 failed kicks.

---

### 2026-04-22 — Role affinity ties 100/100 between same-tier nodes, MacBook hogs Claude Code traffic

**Evidence:** Production traces showed every qwen3-coder:30b-agent request landing on Lucass-MacBook-Pro-2 with a perfect 100/100 score breakdown (`thermal=50, mem=20, queue=0, wait=0, affinity=15, ctx=15`). The Mac Studio M3 Ultra (800 GB/s memory bandwidth, 4× faster at prompt eval) was scoring identically 100/100 — both sat in the same `≥128 GB` role-affinity tier. With no tiebreaker, whichever node was listed first won every request. The MacBook (M4 Max 546 GB/s) then choked on real Claude Code prompt sizes while the Studio sat idle.

**Insight:** Memory-size tiers fail to distinguish nodes that all clear the "big" bar — a MacBook Pro 128 GB and a Mac Studio 512 GB cluster into the same bucket, but their prompt-eval throughput differs by 3–4×. On Apple Silicon, *memory bandwidth* is the right discriminator because prompt eval is memory-bandwidth-bound. Adding chip detection + a chip→bandwidth lookup table flows the real capability into scoring; sub-dividing Signal 5 into a continuous bandwidth-proportional bonus (+25 max at 800 GB/s) breaks the tie. Capacity-normalizing Signal 3 so a queue of N on a faster node counts as N/(relative_speed) produces roughly proportional load distribution under pressure — for Studio+MacBook at 800+400 GB/s, that's a 67/33 split rather than 100/0 or 50/50.

**Action taken:** Shipped `server/hardware_lookup.py` (chip→bandwidth table for M1–M4 + common discrete GPUs), extended `HardwareProfile` with `chip` + `memory_bandwidth_gbps`, rewrote Signals 3/4/5 to be bandwidth-aware with memory-tier fallback for unknown chips. Two new env vars (default on). Plan doc: `docs/plans/device-aware-scoring.md`.

---

### 2025-03-08 — Registry localhost rewrite was masking node reachability

**Evidence:** Nodes running Ollama bound only to localhost (127.0.0.1:11434) were being registered with their LAN IP in heartbeats, but the router was sometimes building Ollama URLs using `request_ip == payload.lan_ip` to determine locality — which gave false positives when the router happened to be on the same machine.

**Insight:** The `is_local` check in `_build_ollama_url()` should only compare the registry's own identity, not the request source IP. This was fixed by removing the `request_ip` comparison. The deeper pattern: locality detection in distributed systems should never rely on request metadata — it should be self-knowledge only.

**Action taken:** Fixed `registry.py`, added LAN proxy (`ollama_proxy.py`) so nodes automatically bridge localhost-bound Ollama to LAN.

---

### 2025-03-08 — LAN proxy eliminates the #1 setup friction point

**Evidence:** The most common troubleshooting issue in docs was "Ollama not bound to all interfaces." Every new user hit this. The fix was manual (`OLLAMA_HOST=0.0.0.0 ollama serve`) and easy to forget.

**Insight:** If the most common support issue has an automatable fix, automate it. The node agent now detects localhost-only Ollama and starts a TCP reverse proxy on 0.0.0.0:11435 automatically. Zero user intervention. The troubleshooting doc now says "this is handled automatically" instead of "run this command."

**Pattern:** Any setup step that >50% of users hit should be automated, not documented. Documentation is an apology for bad defaults.

---

### 2025-03-08 — Benchmark data needs persistence, not just console output

**Evidence:** Early benchmarks used `scripts/benchmark.py` which printed results to stdout. Results were lost between sessions. No way to track fleet performance over time or compare before/after a change.

**Insight:** Benchmarks are observations about fleet health. They belong in the trace store alongside request traces — same SQLite DB, same query patterns, same dashboard. Added `benchmark_runs` table and a Benchmarks dashboard tab. Now you can see performance trends across runs.

**Pattern:** If you're generating data that informs decisions, persist it. Console output is ephemeral. SQLite is permanent and queryable.

---

### 2026-03-08 — OLLAMA_KEEP_ALIVE=16s caused catastrophic model thrashing on 512GB machine

**Evidence:** Mac Studio with 512 GB RAM, only using 118 GB. Two models (`gpt-oss:120b` at 83 GB and `qwen3.5:122b` at 87 GB) were alternating cold loads every 1-2 minutes — 50-190 second TTFT on every swap. Trace data showed 58 cold loads (TTFT >40s) in a single day. Root cause: `OLLAMA_KEEP_ALIVE` was set to `16` (seconds). Both models would easily fit simultaneously in memory with 337 GB to spare.

```sql
-- Query that exposed the pattern
SELECT model, COUNT(*) as cold_loads,
       ROUND(AVG(time_to_first_token_ms)/1000, 1) as avg_load_sec
FROM request_traces
WHERE timestamp > strftime('%s', 'now') - 86400
  AND time_to_first_token_ms > 40000
GROUP BY model ORDER BY cold_loads DESC;
```

**Insight:** Ollama's defaults prioritize memory conservation over performance. On high-memory machines, this is exactly backwards — the cost of unloading a model (50-190s cold load) vastly exceeds the cost of keeping it loaded (82-87 GB of memory you're not using anyway). Fix: `OLLAMA_KEEP_ALIVE=-1` (never unload). Both models now stay hot at "Forever" with TTFT dropping from 50-190s to 0.5-3s.

**Pattern:** Default configs are tuned for the average user, not your hardware. When operating distributed systems, always audit the knobs that control resource lifecycle — keepalive timeouts, connection pools, cache eviction. The default is almost never right for your specific deployment. This is the same class of issue as TCP keepalive defaults causing spurious disconnects or database connection pool sizes limiting throughput.

---

### 2026-03-08 — OLLAMA_NUM_PARALLEL=16 caused 384 GB KV cache per model, triggering eviction thrashing

**Evidence:** Mac Studio with 512 GB unified memory, `OLLAMA_KEEP_ALIVE=-1` (confirmed working), yet models still dropped to 0 loaded approximately every hour. Ollama server logs (`~/.ollama/logs/server-3.log`) showed repeated `"model requires more gpu memory than is currently available, evicting a model to make space"` at regular intervals (12:00, 13:00, 14:00 PDT).

Root cause: `OLLAMA_NUM_PARALLEL` was set to `16`. On the 512 GB machine with `default_num_ctx=262144`, Ollama pre-allocates KV cache for all parallel slots:

```
KV cache = num_ctx × num_parallel × per-token-size
262144 × 16 = 4,194,304 KvSize → 384 GB KV cache per model
```

A single 49-layer model (17 GB weights + 384 GB KV cache + 12 GB compute = ~413 GB) consumed nearly all 464 GB of available VRAM. When a second model was requested, Ollama had to evict the first — and vice versa — creating a thrashing loop despite `KEEP_ALIVE=-1`.

Fix: `OLLAMA_NUM_PARALLEL=2`. KV cache drops from 384 GB to ~20 GB per model, allowing 3-4 large models to coexist simultaneously.

**Insight:** `KEEP_ALIVE` controls *when* models unload (time-based eviction). `NUM_PARALLEL` controls *how much memory* each model claims (space-based eviction). Fixing one without the other still causes thrashing — just via a different mechanism. For multi-model fleets, both must be tuned: `KEEP_ALIVE=-1` to prevent time-based eviction, and `NUM_PARALLEL=2-4` to prevent space-based eviction. The auto-calculated `NUM_PARALLEL` optimizes for single-model throughput, not multi-model coexistence.

**Pattern:** When debugging resource contention, distinguish between time-triggered and space-triggered eviction. They have identical symptoms (models unloading unexpectedly) but completely different root causes and fixes. Logs are the differentiator: "idle timeout" vs "not enough memory" tell you which knob to turn.

---

### 2026-03-22 — Zombie in-flight entries silently starved queue concurrency

**Evidence:** Dashboard showed `gpt-oss:120b` queue with 5/8 in-flight, 0 pending, but Ollama reported 0 active requests. External client reported accepting connection but receiving 0 bytes after 2 minutes. The 5 in-flight entries were from requests where clients disconnected mid-stream — the async generator's `finally` block (which calls `mark_completed`) never ran because the generator was abandoned, not consumed or closed.

**Insight:** In async generator-based streaming architectures, handing a generator to a consumer via a Future creates a lifecycle gap: the producer (queue worker) marks the entry as in-flight, but cleanup depends on the consumer fully consuming or explicitly closing the generator. If neither happens (client disconnect, timeout, error in the route handler), the entry is orphaned. The fix is a reaper — a background task that enforces a maximum in-flight duration. This is the same pattern as TCP keepalive probes: when you can't trust the cleanup path, add a heartbeat/timeout that catches the failure case.

**Pattern:** Any system that tracks "in-progress" state and relies on the happy path for cleanup needs a reaper. Database connection pools have idle timeouts. HTTP servers have request timeouts. Queue managers need in-flight timeouts. If the cleanup is in a `finally` block that might not execute, you need a belt-and-suspenders reaper.

---

### 2026-03-22 — Model name normalization split queue state across two keys

**Evidence:** Dashboard showed two queue cards: `Neons-Mac-Studio:qwen3-coder` (8 concurrency, 20 done) and `Neons-Mac-Studio:qwen3-coder:latest` (1 concurrency, 4520 done). Same model, two identities. The `:latest` variant had 4520 completions at 1 concurrency while the untagged variant had 8 concurrency slots but only 20 completions — the scoring engine and queue manager were treating them as independent models.

**Insight:** Ollama's tag system means `qwen3-coder` and `qwen3-coder:latest` are the same model, but string equality says they're different. Every system that uses model names as keys (queues, latency cache, scoring, pre-warm tracking) was silently creating duplicate entries. The fix is to normalize at the boundary — add `:latest` to any model name without a tag at `InferenceRequest` construction time, before any downstream code sees it.

**Pattern:** When external systems have implicit defaults (Ollama's `:latest` tag, Docker's `:latest` tag, npm's `@latest` dist-tag), normalize them to explicit form at your system boundary. Don't let implicit defaults leak into your key space — it creates phantom duplicates that are invisible until you check the data.

---

### 2026-03-23 — Client `num_ctx` triggers catastrophic Ollama model reloads

**Evidence:** External script sending `num_ctx: 4096` to `gpt-oss:120b` (loaded at 32768 context) caused 0 bytes returned and indefinite hang. Same request without `num_ctx` completed in 3 seconds. Confirmed directly against Ollama on port 11434 (bypassing Herd) — same hang. Trace DB showed 5 requests with 300-600s latencies and null token counts, confirming Ollama never started generating.

Research revealed the mechanism: Ollama's scheduler calls `needsReload()` when `num_ctx` differs from loaded context, triggering a full unload+reload of the 89GB model. Compounding factors: GPT-OSS minimum context override (4096 → 8192, but still ≠ 32768), runner startup timeout exceeded during reload, and potential KV cache fill loop on small context values. Related Ollama issues: #9749, #11711, #3583, #13461.

**Insight:** When proxying to a backend service (Ollama, databases, APIs), the proxy must understand which client parameters trigger expensive internal operations in the backend. `num_ctx` looks like an innocent optimization hint but it's actually a destructive reconfiguration command. The router is in the best position to protect against this — it knows what context the model is already loaded with and can strip unnecessary resize requests. This is the same pattern as a database connection pool that normalizes `SET` commands to prevent clients from reconfiguring shared connections.

**Pattern:** Proxy layers should be aware of which pass-through parameters have side effects in the backend. Not all client parameters are equal — some are query parameters (affect this request only), and some are configuration parameters (affect the backend's state for all future requests). A parameter that looks like a per-request hint (`num_ctx`) can actually be a global state mutation (model reload). The proxy should classify parameters and strip or normalize the dangerous ones. When the same backend serves multiple clients, one client's "optimization" can be another client's outage.

---

### 2026-03-23 — Context-based model upgrade as an alternative to cold loading

**Evidence:** After implementing context protection to strip `num_ctx ≤ loaded context`, the question arose: what if the client genuinely needs more context than the loaded model has? Rather than letting Ollama attempt a slow resize or failing with a warning, the router can search for a loaded model with sufficient context AND more parameters (larger `size_gb`). If node has `small-model:7b` at 32k and `big-model:70b` at 128k, a request for `small-model:7b` with `num_ctx: 65536` auto-switches to `big-model:70b` — already warm, no load time.

**Insight:** The router's fleet-wide view of loaded models enables optimizations that no single Ollama instance can make. Ollama only knows about its own loaded models and would try to reload the requested model with a larger context. The router knows about ALL loaded models across ALL nodes and can find a better-fit model that's already warm. This transforms a potentially catastrophic operation (reload 89GB model) into a zero-cost operation (use an already-loaded model).

**Pattern:** When a system has multiple backends with overlapping capabilities, the router/proxy layer can perform capability-based substitution: instead of forcing the requested backend to adapt (expensive), find an already-adapted backend that meets the requirement (free). This is the same principle as CDN edge selection, database read replica routing, and microservice version routing — match the request to the capability, don't force the capability to match the request.

---

### 2026-03-23 — In-memory event lists as a lightweight observability layer

**Evidence:** Added health visibility for context protection and zombie reaper — two features that only logged events but had no dashboard presence. Rather than adding columns to the trace store (schema migration, async complexity), used module-level event lists capped at 100-200 entries with `get_*_events(hours)` getters. The health engine imports and aggregates them into `Recommendation` cards. Same pattern already proven by VRAM fallback tracking in `routing.py`.

**Insight:** Not every operational signal needs database persistence. For "what's happening right now" visibility, in-memory event lists with time-windowed getters are sufficient. They're zero-latency (no async/await), zero-schema (no migrations), and self-cleaning (capped lists). The health engine treats all data sources uniformly — it doesn't care if the data comes from SQLite traces, registry state, or in-memory event lists. This separation of storage concern from health analysis concern keeps the system simple.

**Pattern:** When adding observability for a new subsystem, ask: does this need to survive a restart? If no (it's diagnostic, not historical), use an in-memory event list. If yes (it's a metric for trending), add it to the trace store. The health engine's check methods abstract over this distinction — each check knows where to get its data, and the `analyze()` method just collects recommendations. This is the Strategy pattern applied to health monitoring.

---

## 2026-07-17: Two false alarms from one bug — the log timestamps are UTC, the fleet isn't

**Evidence:** Scanning `herd.jsonl` for errors twice produced confidently wrong answers in the same session. First: `0 ERRORs in 8h` on a fleet that had logged 399 — the parser used `timestamp`/`message`, but `JSONLFormatter` writes `ts`/`level`/`logger`/`msg`, so every record silently failed the filter. Second, after fixing the field names: `218 warnings in the last 3 minutes` — which nearly blocked a green-light to resume production work. The real number was **0**. Cause: `ts` is ISO-8601 *with* a `+00:00` offset (`2026-07-17T10:22:21.109982+00:00`) while the box runs UTC-8. `time.mktime(time.strptime(ts[:19], ...))` treats that UTC string as **local**, yielding epochs **8 hours in the future** — so `ts >= now - 180` matched **every line ever written**. The tell was visible and ignored: a "last 3 min" query returning 218 rows from a log with ~91 lines in the window.

**Insight:** This is the same failure family as the 2026-05-15 grep-pattern miss (`'"level":"ERROR"'` without the space), and it fails in *both* directions — false-clean and false-alarm. A false-clean hides an incident for days; a false-alarm burns trust and blocks real work. Both come from asserting on a log's shape without verifying it. **Parse with `datetime.fromisoformat(ts).timestamp()`** (it honours the offset), use the real field names, and **always print the total line count alongside a windowed query** — if "last 3 minutes" returns thousands of rows, the clock math is wrong, not the fleet. Cheap invariant, catches the whole class.

---

## 2026-07-17: I kernel-panicked the box by ignoring a number I had already printed

**Evidence:** A `watchdog timeout: no checkins from watchdogd in 90 seconds` panic took down the M3 Ultra at 02:11 during Ollama upgrade testing. Not memory exhaustion — the machine was *starved* for 90 straight seconds. Cause was testing method: a `/fleet/pin` test on a 290 GB model fell through to a real load; curl gave up after 20 s, I checked the pin store, saw it hadn't persisted, and moved on — but the **load kept running server-side for minutes**, persisted the pin on completion (it reappeared after reboot), and the sustained memory/IO starved `watchdogd`. The warning sign was in my own output, unread: **a 19 GB model reporting a 394-second load time**. On a healthy box that load is seconds. I ran three more heavy tests after seeing it.

**Insight:** Three compounding lessons. (1) **A slow load is a stop signal, not a curiosity** — 394 s for 19 GB means the box is already thrashing, and everything after it is stacking onto a machine in distress. (2) **A client timeout does not cancel server-side work** — curl returning doesn't mean the 290 GB load stopped; it kept going and *persisted state*. Never infer "it didn't happen" from "my request gave up." (3) **The plan said "read-only first, do not disturb the soak"** and I wrote that sentence myself before ignoring it. Memory-heavy measurements belong on an idle fleet, one at a time, with `memory_pressure` + `vm.swapusage` checked between each. Following your own plan is the cheap version of this lesson; the expensive version is a kernel panic and a paused workday.

---

## 2026-07-17: Ollama 0.24 → 0.32.1 — the fix was llama.cpp, not the MLX we upgraded for

**Evidence:** We upgraded chasing Ollama's native MLX runner (real: `x/mlxrunner`, MLX C-API bindings, 6,925 `mlx` strings in the binary). Results on the same M3 Ultra: **glm-4.7-flash 13.7 → 77.8 tok/s (5.7×)**, gpt-oss:120b 50.9 → 74.5, prefix cache 568 ms → 38 ms cold-vs-warm (29×/token). But **MLX never ran**: 0 `mlx` mentions in a 5 MB server log, `ggml_metal_init` + `llama_model_loader` throughout, `OLLAMA_NEW_ENGINE` unset. The glm fix is visible upstream in llama.cpp: `handle_glm4moelite: detected Ollama-format glm4moelite GGUF; translating to deepseek2 (MLA conventions)` — it stopped CPU-offloading the experts. Meanwhile our own `mlx_lm.server` serves the same model at **59 tok/s**.

**Insight:** **Ollama's llama.cpp path is now 32% faster than our MLX subsystem — with Ollama's MLX still switched off.** Two years of local-inference strategy assumed "Ollama is the slow, convenient path; MLX is the fast path you bolt on." That inverted while we weren't looking, because we were 8 versions behind and documenting `0.20.4` while running `0.24.0`. Every piece of guidance built on the old premise ("use `mlx:` for GLM", "Ollama can't prefix-cache", "macOS hardcodes a 3-model cap") turned out false on 0.32.1. **A dependency's weaknesses are not permanent, and a documented constant can outlive its truth silently.** Pin the version *and* re-verify the beliefs attached to it — and put the dependency's version in the soak checks so the drift can't hide.

---

## 2026-04-02: shutil.which() blind spots in tool-installed binaries

**Evidence:** Image generation stopped working after every Herd restart. The node agent couldn't find `mflux-generate-z-image-turbo` even though `uv tool list` confirmed it was installed. Root cause: `uv tool install` puts binaries in `~/.local/bin/` via symlinks, but when `uv run herd-node` launches the Python process, `~/.local/bin` isn't in `$PATH`. `shutil.which()` only checks `$PATH`. The fleet status showed `image=none, port=none` — zero image capabilities reported despite mflux being fully functional if called with the full path.

**Insight:** Any system that discovers external tool capabilities via `shutil.which()` or `subprocess` is vulnerable to PATH blindness. The fix isn't to manipulate PATH (fragile, platform-specific) — it's to check known installation directories explicitly. We added `_which_extended()` that checks `~/.local/bin`, `/opt/homebrew/bin`, and `/usr/local/bin` as fallbacks. This pattern applies to any agent/collector that needs to discover installed CLI tools.

---

## 2026-04-02: Silent success is worse than loud failure

**Evidence:** Two bugs masked failures as successes for weeks: (1) Client disconnects (`GeneratorExit`) were caught and marked "completed" — 0 failures in the dashboard while the other agent reported 4 fetch failures. (2) Streams ending without Ollama's `done: true` (process crash, TCP drop) also marked "completed". The dashboard showed 24,650 completed, 1 failed. The real failure count was hidden.

**Insight:** In distributed systems, the most dangerous bugs aren't the ones that crash — they're the ones that silently succeed. A streaming proxy must distinguish between "stream completed normally (got done:true)" and "stream ended without error but without completion signal." The `GeneratorExit` exception in Python async generators is especially treacherous — it's the correct way for consumers to signal "I'm done" but it looks identical to "I crashed/timed out." Always check for a positive completion signal (`done: true`, final chunk, etc.) rather than assuming "no error = success."

---

## 2026-04-02: Thinking models break the num_predict contract

**Evidence:** Agent reported empty responses from `gpt-oss:120b` with `num_predict=200`. All 200 tokens went to chain-of-thought reasoning (`message.thinking`), leaving 0 for visible output. Ollama returned `done_reason: "length"` and empty `message.content`. From the client's perspective: successful completion, no error, no content. The fix was router-level: auto-detect thinking models and inflate `num_predict` by 4× (200 → 1024) before forwarding to Ollama.

**Insight:** Thinking models fundamentally change the token budget contract. `num_predict` no longer means "max output tokens" — it means "max thinking + output tokens." This is a breaking semantic change that no client-side code expects. The router is the ideal place to fix it because: (1) it sees all requests, (2) it knows which models are thinking models via the catalog, (3) it can inflate transparently without client changes. The pattern: when an upstream system changes semantics, the proxy layer should absorb the translation. Same principle as context protection.

---

## 2026-04-02: launchctl setenv is a lie (on macOS)

**Evidence:** Set `OLLAMA_NUM_PARALLEL=2` via `launchctl setenv` and confirmed it worked (`launchctl getenv` returned 2). Ollama restarted fine with the new value. But `~/.zshrc` contained `launchctl setenv OLLAMA_NUM_PARALLEL 16`. The next time any terminal opened, the zshrc re-ran and silently overwrote the value back to 16. Ollama kept running with 2 (already loaded), but the next Ollama restart would pick up 16 again. Another agent caught it.

**Insight:** `launchctl setenv` is session-scoped and overridden by shell profile scripts. For persistent macOS environment changes, you must update both `launchctl setenv` (immediate) AND the shell profile (`~/.zshrc`, `~/.bash_profile`). The KV cache bloat health check we added detects the symptom (VRAM > expected weights) but can't detect the env var revert itself. Defense in depth: fix the config file, apply the runtime change, and add monitoring for the downstream effect.

---

---

## 2026-04-08: Context window utilization is shockingly low

**Evidence:** `GET /dashboard/api/context-usage` on a fleet with 67K+ requests over 7 days showed gpt-oss:120b allocated at 131,072 context but actual total token usage (prompt + completion) was: p50=1,100, p95=4,120, p99=5,409, max=34,721. That's 4.1% utilization at p99. The model was using ~120GB VRAM (67GB weights + ~50GB KV cache) when it could have used ~70GB at 16K context — wasting 50GB that prevented other models from loading.

**Insight:** Default context windows are set for the model's maximum capability, not actual usage. In practice, 99% of requests use <5% of the allocated context. This KV cache waste is invisible without measuring actual token distributions. The fix is dynamic num_ctx management: measure p99 of total tokens (prompt + completion, not just prompt), add 50% headroom, round to next power of 2. Critical: use p99 of TOTAL tokens not just prompt — setting 8K based on prompt p99 caused output truncation because completion tokens need context space too. The 24h rolling max prevents quiet periods from under-sizing.

---

## 2026-04-08: Ollama evicts by VRAM pressure, not by model priority

**Evidence:** Smart benchmark tried to load codestral:22b alongside gpt-oss:120b (at 131K context). Despite 390GB "available" RAM, Ollama evicted gpt-oss to make room for codestral. The router didn't cause this — Ollama's internal memory manager decided what to evict. The smallest model (llama3.2:1b at 2GB) survived because it loaded last. After reducing gpt-oss to 16K context (freeing ~50GB KV cache), all three models coexisted.

**Insight:** Ollama's eviction is space-based, not priority-based. It doesn't know which model is "more important" — it just evicts whatever frees enough memory. `OLLAMA_KEEP_ALIVE=-1` prevents time-based eviction but not space-based eviction. The only way to prevent eviction is to ensure total VRAM fits: model weights + KV cache (context × parallel slots × overhead) for ALL loaded models must be < available unified memory. On Apple Silicon, "available" RAM reported by the OS includes memory that Ollama considers used for KV cache. The dynamic num_ctx feature directly addresses this by shrinking KV cache to actual needs.

---

## 2026-04-22: Ollama's llama.cpp engine with FA + Q8 KV beats raw mlx-lm on M3 Ultra (and has working prompt cache)

**Evidence:** Ran a head-to-head 25-turn multi-turn benchmark comparing `mlx_lm.server` (mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit) against Ollama's qwen3-coder:30b on identical M3 Ultra 512GB hardware. Both at 262144 native context. Simulated a Claude Code session where each turn extends the prior conversation by ~500 tokens.

Results:
- **Ollama median TTFT: 306ms** (steady state 315ms across turns 5-25)
- **MLX-lm median TTFT: 422ms** (steady state 488ms)
- Ollama max: 509ms | MLX max: 1,250ms
- Both stay FLAT through 50-message conversations — **prefix caching works on both**
- Growth from turn 1 → turn 25: MLX 0.75×, Ollama 1.21× (neither grows significantly)

Critical context for the comparison: **Ollama is NOT running MLX on this machine.** The ollama.log shows `WARN MLX dynamic library not available error="failed to load MLX dynamic library"` — the Mac App build can't find the MLX dylib. Ollama is using its native engine (`--ollama-engine` flag on runner process confirms llama.cpp-derived backend) with `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`. So the real comparison was raw-mlx vs a tuned llama.cpp, not mlx-vs-mlx.

**Insight:** Two important corrections to earlier assumptions about ollama-herd performance.

**(1) "Ollama has no prompt caching" was wrong.** For sequential requests sharing a common prefix (the exact shape of a Claude Code multi-turn session), Ollama's native engine caches the KV state and reuses it. TTFT stays flat across 50-message conversations. The 8-second TTFT we saw earlier in different contexts (e.g. laptop M5 Pro trace, initial large-context loads) was not "no prefix cache" — it was (a) cold load after context reallocation, or (b) underpowered hardware (M5 Pro's 20 GPU cores vs M3 Ultra's 80), or (c) cache miss from a different session shape.

**(2) Raw mlx-lm is NOT a faster alternative** for this model on this hardware. The llama.cpp tuning (flash attention + q8 KV cache + years of Apple Silicon metal-kernel optimization) outperforms raw mlx-lm.server. mlx-lm may still be worth using for a different reason — bypassing Ollama's 3-model cap (see previous observation) — but not for single-model speed. If we had Ollama's MLX backend working (the dylib is missing on this install), the comparison might flip, but only for Qwen3.5 models; qwen3-coder isn't covered by Ollama's MLX preview as of April 2026.

**Corollary lesson: don't re-allocate `num_ctx` between requests.** Every time `pre-warm with num_ctx=X` then `pre-warm with num_ctx=Y` runs, the KV cache resets and the prompt cache drops. To keep prefix caching working, pick a context size and stick with it. The clean way to lock it: create a Modelfile variant (e.g. `FROM qwen3-coder:30b\nPARAMETER num_ctx 40960` → `ollama create qwen3-coder:30b-agent -f ...`) and map Claude tiers at the variant, so the specific context size is part of the model identity and Ollama never reallocates it out from under you. Implemented 2026-04-22 as `qwen3-coder:30b-agent` at 40960 ctx.

**Trap if you try to match Ollama's `OLLAMA_KV_CACHE_TYPE=q8_0` on mlx-lm.server:** the server binary doesn't expose `--kv-bits`; that flag only exists on `mlx_lm.generate` CLI and the library API. And the library API itself doesn't do automatic prefix-matching — in our test, calling `stream_generate` with a shared `prompt_cache` object across 8 turns showed linear TTFT growth (452ms → 1320ms), because the library requires explicit cache-trim logic the server handles for you. So "mlx-lm + FA + Q8 KV + prompt cache" is not a one-flag combination today — it needs either a mlx-lm.server patch (~1 day, upstream-able) or a custom serving wrapper (~1 week). With defaults (no Q8 KV), mlx-lm.server still stays within 30% of Ollama's llama.cpp+FA+Q8KV perf. Worth doing for the 3-model-cap bypass, not for speed.

**Update (same day, hackathon patch landed):** Patched `mlx_lm/server.py` (~30 lines) to expose `--kv-bits` / `--kv-group-size` / `--quantized-kv-start` and forward them to `stream_generate`. With `--kv-bits 8` (matching `OLLAMA_KV_CACHE_TYPE=q8_0`), MLX's server median TTFT dropped from 422ms → **320ms**, essentially tied with Ollama's 306ms. The 25-turn benchmark shows both staying flat across 50-message conversations; max latency: MLX+Q8 539ms vs Ollama 509ms. Within measurement noise. KV quantization was the entire gap. Flash attention is already automatic in MLX's Metal kernels, no toggle needed. Patch saved at `docs/experiments/mlx-lm-server-kv-bits.patch`, full writeup + raw data at `docs/experiments/mlx-lm-q8kv-benchmark.md`. With this patch, MLX becomes a legitimate alternative backend for ollama-herd — not for speed (tied), but for architectural properties Ollama doesn't have on this hardware: no 3-model cap, reliable env var semantics, independent-process isolation per model.

**Upstream contribution lesson:** before writing a PR, grep existing open PRs for the same change. When we went to submit the patch upstream to `ml-explore/mlx-lm`, we found two open PRs already doing exactly this — [#934](https://github.com/ml-explore/mlx-lm/pull/934) (Feb 2026, approved by a contributor, sitting stale) and [#1073](https://github.com/ml-explore/mlx-lm/pull/1073) (Mar 2026, more complete — handles the `BatchQuantizedKVCache` NYI edge case). The right move wasn't a third PR; it was [commenting on #1073 with our independent benchmark data](https://github.com/ml-explore/mlx-lm/pull/1073#issuecomment-4299866597) to add merge-pressure signal. One-minute action, much higher leverage than a duplicate PR.

**Corollary: read all the competing implementations before shipping your own.** Once we'd decided not to open a duplicate PR, we diffed the two upstream PRs against our local patch to see what we missed. Two real safeguards came back: (a) PR #934's `choices=[4, 8]` argparse constraint prevents silent runtime failures from invalid `--kv-bits` values, and (b) PR #1073's `_is_batchable` guard prevents a crash when `--decode-concurrency > 1 --kv-bits N` is combined (because `BatchQuantizedKVCache` doesn't exist yet). Our 25-turn benchmark was sequential so we never hit the batching bug — but we would have shipped it to anyone running concurrent decoding. The differences between competing PRs are where the subtle correctness concerns live; that's the part worth reading carefully. In general, "write my version first, then look at others" loses every time to "look at others first, then write mine informed by what they got right."

---

## 2026-04-22: Ollama 3-model cap on macOS is hardcoded, not env-configurable

**Evidence:** On Ollama 0.20.4 / M3 Ultra 512GB, attempted to raise the concurrent-model cap via every standard path — `launchctl setenv OLLAMA_MAX_LOADED_MODELS 10`, plist `EnvironmentVariables`, `~/.zshrc` export, direct command-line env prefix, full kill + clean relaunch. In every case the process env reported `-1` (not what we set) and the cap remained at 3 models. Loaded 4 distinct-weight-blob models to rule out shared-blob conflict — the 4th evicted LRU even with 358 GB RAM available. Dashboard confirmed 292 GB used / 512 GB total, so memory was never the constraint. Root cause traced to Ollama source: `MaxRunners = Uint("OLLAMA_MAX_LOADED_MODELS", 0)` — `Uint` is unsigned, `-1` fails parse and falls through to default 0 (= `defaultModelsPerGPU = 3`). But setting positive integers didn't raise the cap either. Related upstream: `ollama/ollama#7041`, `#4855`, `#5722`, `#14953`.

**Insight:** Don't trust `OLLAMA_MAX_LOADED_MODELS` as a configurable knob on macOS. The effective cap must be inferred from observed behavior, not read from env. For ollama-herd: when `FLEET_ANTHROPIC_MODEL_MAP` references >3 distinct models on a single node, some will be evicted silently, and the router's VRAM-fallback logic will send Claude Code's tool-heavy requests to weaker models that can't emit `tool_calls` blocks. The user-visible symptom is "Claude Code started returning plain text instead of using tools." The only reliable detection today is the `x-fleet-fallback` response header or `original_model != model` in the trace DB. Workarounds: (1) limit map to ≤3 distinct models per node, (2) run a second Ollama daemon on another port (each gets its own 3-slot budget), (3) use `mlx-lm.server` directly for specific models, (4) wait for upstream fix. Also: Ollama env var semantics differ per variable — `OLLAMA_KEEP_ALIVE=-1` IS valid ("keep forever") while `OLLAMA_MAX_LOADED_MODELS=-1` is silently invalid. Don't assume `-1` means "unlimited" across Ollama envs. See `docs/issues.md` and `docs/plans/hot-fleet-health-checks.md`.

---

*Add new observations above this line. Date them. Link evidence. Extract the transferable insight.*

---

## 2026-04-23 — `uv tool install mlx-lm` silently wipes the `--kv-bits` patch

**Evidence**: During an operational fix on this machine, `mlx-lm` was reinstalled (via `uv tool install mlx-lm` pulling the latest 0.31.3). The node supervisor immediately started failing with:

```
mlx_lm.server: error: unrecognized arguments: --kv-bits 8 --kv-group-size 64
```

Supervisor logged "mlx_lm.server failed to become healthy within 120s" and gave up. The Qwen3-Coder-480B stopped running despite weights still being on disk and `FLEET_NODE_MLX_AUTO_START=true` set. Root cause: upstream `mlx_lm.server` doesn't expose KV-quantization flags — we depend on a local patch (`docs/experiments/mlx-lm-server-kv-bits.patch`). Any reinstall/upgrade overwrites the patched `server.py`.

**Insight**: The patch is a brittle external dependency. We've made the regression **loud** (supervisor errors are ERROR-level in the JSONL log) but not **prevented**. Fix shipped alongside this observation:

- `scripts/setup-mlx.sh` — idempotent installer that pins `mlx-lm==0.31.3`, applies all three patch hunks via Python (more robust than `patch -p1` against this patch file's formatting), and verifies `--kv-bits` is exposed after. Must be re-run after any `uv tool upgrade mlx-lm`.
- `docs/guides/mlx-setup.md` — canonical setup reference with env block + troubleshooting for this exact failure mode.
- CLAUDE.md updated with explicit "re-run after upgrade" warning.

Until upstream lands [PR #1073](https://github.com/ml-explore/mlx-lm/pull/1073) or equivalent, this stays as operational toil. A future improvement would be a startup-time check in `mlx_supervisor` that probes `mlx_lm.server --help` for `--kv-bits` before launching; if missing, log a fatal hint pointing at `./scripts/setup-mlx.sh` and skip auto-start rather than timing out at 120s.

---

## 2026-05-16 — "Longer timeout + retry" doesn't fix structural contention

**Evidence**: After shipping the 2026-05-15 trace_store fixes (busy_timeout 5s → 30s, retry-on-locked with exponential backoff, `wal_autocheckpoint=100`, new health check) and verifying the post-restart 30-minute soak was clean, the local fleet ran fine for ~11 hours.  Then between 06:48 → 19:00 UTC on 2026-05-16, **~4,470 background `record_trace` tasks failed with `database is locked` again** — same error message, same logger, same code path.  The new health check fired exactly as designed (WARNING, 38 failures in last 5 min), so this time we saw it inside an hour instead of four days.  But seeing it doesn't fix it.

Failure pattern was identical to the original incident, just lower amplitude:

| Metric | Original (2026-05-10) | Round 1 fix (2026-05-16) |
|---|---|---|
| Errors/day | ~9,650 | ~9,000 (peak in single hour: 440) |
| WAL peak | 2.5 GB over 4.5 days | 103 MB over 12 hours |
| Visibility | None (4 days hidden) | Health check WARNING within minutes |
| Symptom | Dashboard `reqs_24h=0` for days | Dashboard `reqs_24h` stuck at yesterday's count |

The retry layer + 30s busy_timeout was absorbing roughly two-thirds of the contention, but every transient stall longer than ~90s (busy_timeout × retry budget) still leaked through as a permanent failure.  The WAL was growing, just more slowly.

**Root cause analysis showed the contention was structural, not transient.** `TraceStore` opened a single `aiosqlite.Connection` shared between `record_trace` (write) and every dashboard analytics method (`get_recent_traces`, `get_overall_stats_24h`, etc.).  Two consequences:

1. aiosqlite serializes operations per-connection through a single background thread.  A 200ms dashboard query blocked all queued writes on that connection for 200ms.
2. Even with WAL mode (where readers don't *block* writers in the usual sense), the reader's transaction held a snapshot point — the checkpointer could only advance to the oldest reader's snapshot.  With dashboard polling every 15-30s across multiple endpoints, snapshots were continuously open, and checkpoints never caught up.

The first round of fixes treated symptoms — "wait longer for the lock," "retry the locked write," "shrink the autocheckpoint threshold."  All useful, none addressed the actual mechanism: a single connection being shared between two access patterns that should never have been sharing.

**The structural fix** (round 2, same day):

- Open `_db` and `_read_db` as separate `aiosqlite.Connection` instances per store.  Route every analytics/scoring read through `_read_db`; keep writes on `_db`.  `_read_db` gets `PRAGMA query_only=1` as defense-in-depth.  Two connections = two threads = no serialization between read and write, and independent snapshots that don't pin each other's WAL view.
- Add a periodic `PRAGMA wal_checkpoint(PASSIVE)` task in `app.py`'s lifespan, firing every 10s on each store's writer.  Wall-clock-triggered rather than volume-triggered, so it fires in the gaps between dashboard read snapshots regardless of write volume.

Verified the same day under live load: a 30-concurrent-chat + 120-dashboard-poll burst held the WAL at 410 KB peak vs 103 MB on the same workload before the change.  Health check stayed silent.

**Insight**: When a fix that hardens the safety net ("wait longer, retry more") reduces incident amplitude without eliminating it, the contention is structural — you're sharing something you shouldn't be sharing.  The fix isn't a bigger hammer, it's separating the shared thing.

This generalizes: a "wait + retry" pattern is the right tool for transient stochastic contention (network hiccups, brief lock collisions between independent writers).  It's the wrong tool for sustained structural contention (the same two access patterns competing for the same resource on every request).  When the metrics show your retry budget being exhausted at a steady rate rather than only on spikes, the access pattern is broken, not the load.

Plan: `docs/plans/trace-store-read-connection-and-checkpoint.md`.  Note in the plan that Part B (split `trace_store` onto its own `traces.db`) was intentionally deferred — the C+A combination addresses the actual mechanism, and shipping with smaller scope + real verification beats shipping a bigger change on speculation.  If a future workload shows the failure mode return despite C+A, B is the next step.

---

## 2026-05-15 — A wrong grep pattern hid a 4-day production incident from every soak check

**Evidence**: A "spotless 44h soak" report on Apr 29 said the local fleet had 0 ERROR and 0 WARNING in `herd.jsonl` since the latest restart.  Same conclusion at the day-after check (also Apr 29) and the week-after check (May 4) — all reported the fleet as clean.  On May 15, while doing a routine log review, parsing the file with Python instead of grep revealed: **39,792 ERROR lines on May 10 alone**, and 1,196 WARNING.  Spotted because the May 10 log file was 131 MB while every other day's file was ~6 MB — a 22× outlier I'd somehow not noticed before because I was only looking at error counts, not file sizes.

The root cause of the false-clean reports: the JSONL formatter writes `{"ts": "...", "level": "ERROR", ...}` — JSON with a default space after each colon.  Every soak check used `grep -c '"level":"ERROR"' herd.jsonl` (no space after the colon), which matches zero lines.  The same grep ran across ~14 days of rotated logs and dutifully returned 0/0 for every single file, producing perfectly defensible-looking "no errors" reports while the actual incident accumulated underneath.

The actual incident, once visible: starting 2026-05-10 14:21 PDT, **a long-running read transaction prevented SQLite WAL checkpoint** on `latency.db`.  The WAL grew to 2.5 GB over the next ~4.5 days while `record_trace` background tasks failed with `database is locked` after the 5-second `busy_timeout` expired — roughly 9,000 trace-record failures per day.  Requests themselves still succeeded end-to-end (`record_trace` is fire-and-forget; an exception only fails the trace, not the response), but observability quietly evaporated: the dashboard's `reqs_24h` showed 0 because it queries the same DB that wasn't getting writes, which I'd been reading as "fleet is idle, no traffic to soak" rather than the true reading "router is serving but its observability layer is broken."

Adjacent fact pattern hiding in the same blob: at UTC midnight on May 10/11, the `TimedRotatingFileHandler` in `herd-node` renamed `herd.jsonl` → `herd.jsonl.2026-05-10`, but the *router's* handler (a separate `TimedRotatingFileHandler` in a separate process writing to the same file) still had a file descriptor pointing at the renamed inode and happily wrote to it for the next five days.  That's how one rotated file ended up 131 MB while same-week peers stayed at the normal ~6 MB.  Two-process daily rotation against a single log file is a well-known footgun in `logging.handlers` and we walked straight into it.

**Insight**: Two distinct kinds of lesson here, and both are reusable.

The technical lesson: any observability layer that runs *in band* with the thing being observed will go dark in exactly the failure mode where observability matters most.  `record_trace` writes to the same SQLite file that `LatencyStore` writes to, with separate connections in the same process — they share WAL contention.  Whenever the WAL can't checkpoint (because a reader holds an old snapshot), writers stall, and the *first* thing to stall is the layer that's supposed to tell you something's wrong.  The fixes are layered: (1) longer `busy_timeout` (5s → 30s) absorbs short contention without dropping traces; (2) retry-on-locked with exponential backoff in `record_trace` covers longer stalls without the caller knowing; (3) `wal_autocheckpoint=100` bounds WAL growth so a slow reader can't *cause* the contention; and (4) a new `trace_store_write_failures` health check makes the previously-invisible failure mode dashboard-visible.  All four matter — none of them on their own would have caught the May 10 scenario, because every one of them was the safety net for a layer below it.

The process lesson — and this is the one I owe the user a real apology for: **never trust a count without validating the pattern that produced it.**  Four separate soak reports across two weeks (44h, day-after, week-after, "how's it going") all confidently asserted "0 errors" because a single bad grep returned 0/0 consistently.  The grep was wrong on day one and stayed wrong for fourteen days; once the wrong pattern was in my muscle memory it kept being reused.  The CLAUDE.md gotcha added in this commit pins down the correct pattern (`'"level": "ERROR"'` with the space), but the deeper rule is to **validate any clean-looking count with at least one other tool** — `wc -l` per file, file-size sanity check, a Python JSON parse, an explicit known-bad-line test — before reporting it.  Cheap counters across many files that all return the same uniform number are a smell, not a comfort.  Generalizable beyond this incident: when an automated check reports a uniformly clean result across many independent inputs over a long timeframe, the most likely explanation is that the check is broken, not that everything is fine.

Filed: `trace_store_write_failures` health check, retry-on-locked in `record_trace`, busy_timeout bump, per-process log files, and a CLAUDE.md "Gotchas" entry warning future scanners to use the right grep pattern.  All shipped in 0.6.2.

---

## 2026-04-27 — "Blocked on upstream" is architecture-specific, not version-specific

**Evidence**: `docs/issues/mlx-speculative-decoding-blocked.md` had been marked "blocked on mlx-lm#1081" for three days, with a reproduction matrix claiming `--draft-model` failed with `ValueError: Speculative decoding requires a trimmable prompt cache (got {'ArraysCache'})` on every flag combination we'd tried. Re-tested today on the **same pinned mlx-lm 0.31.3 we'd been on the whole time** — no version bump, no patch, no env change. Spawned a one-off `mlx_lm.server` on port 11442 with `--model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --draft-model mlx-community/Qwen3-1.7B-4bit --num-draft-tokens 4`. It worked: HTTP 200, ~94 tok/s on M3 Ultra, no `ArraysCache` error. Then tried the same flags against `Qwen3-Coder-Next-4bit` — failed exactly as the matrix said.

The original matrix had been built by testing on Qwen3-Coder-Next first, seeing `ArraysCache`, and concluding "every MoE on mlx-lm 0.31.3 produces ArraysCache." That was the wrong generalization. Qwen3-Next (the architecture, not just the model) is a hybrid attention design with Mamba/SSM-style linear-attn layers. mlx-lm represents that state with `ArraysCache`, which is **correctly** non-trimmable — you can't trim what isn't a sliding KV. Standard transformer MoEs (like 30B-A3B-Instruct, despite both being "Qwen3 family") use full attention, build a proper `KVCache`, and spec decoding rolls back rejections fine. The bug everyone had been calling "ArraysCache.is_trimmable() returns True but trim() doesn't exist" is misnamed: `is_trimmable()` returning False is the correct invariant; the real gap is that mlx-lm's spec-decode path requires rollback support that linear-attn caches don't and probably can't provide.

**Insight**: When something's "blocked on upstream," re-test on the actual hot path before assuming the block is total. Two failure modes look identical from the outside: "the bug applies to everything in this family" vs "the bug applies to one architecture in this family." The first one means wait. The second one means deploy on the subset that works while you wait for the rest. Three days of "no spec decoding" turned into "spec decoding live on the dedicated context compactor (which is where every Claude Code summarization pass lands)" because I tested the second-most-likely model after the first failed, instead of marking the family broken and moving on. Architecture matters more than version in MLX failure mode taxonomy — the same wheel can have completely different behavior on two models that differ in attention design but share a tokenizer family. Generalization: when blocked on a library, vary the inputs (model architecture, dtype, cache type, batch size) before assuming the block is total. Filed update to the issue doc replacing the "always fails" matrix with a per-architecture table; marked the issue 🟡 PARTIALLY UNBLOCKED rather than ✅ FIXED to reflect what's actually true. Shipped as the headline `Added` entry in 0.6.1.

---

## 2026-04-27 — Orphan mlx_lm.server processes survive their parent's death and pretend the supervisor is broken

**Evidence**: Audited the local fleet after 17 hours of "stable" operation post the quarantine guard ship. The supervisor logged 204 `mlx_lm.server(port=N) exited unexpectedly (rc=1); QUARANTINED — restarting in 600s` warnings, one every 10 minutes per port for 17 hours straight. Looked like the upstream mlx-lm bug had recurred and quarantine was steadily containing it. Drilled in:

- The mlx-server log on port 11440 had **zero crashes** in 17 hours — only `GET /v1/models 200` health-check responses.
- `ps aux | grep mlx_lm.server` showed PIDs 91773 (port 11440) and 91778 (port 11441) **both alive 17 hours, parent PID = 1 (launchd)**.
- `lsof -i :11440 -i :11441` confirmed those orphan processes were holding the ports.
- Yet the supervisor's herd.jsonl had 668 "restarted successfully" log lines — meaning it WAS spawning new processes successfully (they got past the initial health check).

The reconciliation: my supervisor uses `subprocess.Popen(start_new_session=True)`, which `setsid()`s in the child. When the child's parent (herd-node) dies, the child stays alive and gets reparented to launchd — it does NOT receive SIGHUP. Earlier in the previous session, I'd run `pkill -9 -f "bin/herd-node"` to restart the node without touching mlx_lm.server. The original mlx_lm.server children were orphaned. The new herd-node started its own supervisor, which spawned its own mlx_lm.server processes — but those failed to bind because the orphans held the ports, exited rc=1 in ~2 seconds, and the supervisor's quarantine logic kicked in. **For 17 hours, our supervisor was crash-looping against itself while the orphan it didn't know about did the actual serving.**

The dashboard showed `mlx_server_quarantined` (CRITICAL), the supervisor's view said "broken," and reality said "fine, just not under our supervision." 3 chat completions completed successfully during the window — all served by the orphans, never by our supervisor's failed spawns. The "successful restart" log lines were the orphan returning 200 on /v1/models during the brief health-check window before the doomed bind attempt.

**Insight**: `start_new_session=True` is correct for "child should survive a transient parent restart" but creates a recovery footgun for "parent restart that should also wipe children." A supervisor that owns a port-bound subprocess **must check for orphans on its port before spawning** — both because it can't bind otherwise AND because the orphan's behavior diverges from what the supervisor thinks is happening. Implemented `find_orphan_mlx_pids_on_port` (psutil-based, identity-strict — only kills processes whose cmdline mentions `mlx_lm.server` AND whose net_connections show binding to our port; an unrelated process on the same port is left alone). `MlxSupervisor.start()` now SIGKILLs those orphans before its own `Popen` and logs a loud WARNING explaining what was found.

Generalizable beyond this incident: any "supervisor manages a subprocess that binds a known port" pattern needs the orphan-check at spawn time, not just trust in the subprocess module's reaping. The contract `Popen` advertises ("when this object goes away, so does the child") is a lie under `start_new_session=True`. Document the limitation if you use it; better, scan for orphans at startup so a botched previous teardown can't silently break the next session.

Also: the operator restart recipe in `CLAUDE.md` was the tertiary cause — `pkill -f "bin/herd"` doesn't catch `mlx_lm.server`. Updated to `pkill -9 -f "bin/herd|mlx_lm.server"` so the manual path doesn't reproduce the same trap. Both fixes shipped together — the supervisor self-heals if a future operator forgets the right pkill, and the right pkill is now in the docs anyway.

---

## 2026-04-26 — A crashing supervisor with no upper bound is its own outage

**Evidence**: Local fleet ran the multi-MLX setup for 28 hours uninterrupted. Around 17:00 PDT on 2026-04-26 something — most likely a Claude Code session resuming with a 100K+ token prompt — pushed `mlx_lm.server v0.31.3` into a state where every chat-completion request crashed the process with `RuntimeError: cannot schedule new futures after interpreter shutdown` from inside `huggingface_hub.snapshot_download`'s `thread_map`. The supervisor's `_monitor` task did exactly what it was designed to do — restart the process after each crash with exponential backoff up to 60 s — and proceeded to do that **420 times over 2.5 hours** before I noticed. Each cycle:

- Spawned a fresh `mlx_lm.server` (~3-5 GB RAM allocation across the 42 GB model + tokenizer + cache structures)
- Loaded the model successfully (visible in `mlx-server-11440.log` as a few seconds of startup)
- Accepted exactly one HTTP POST `/v1/chat/completions` (logged as 200 in mlx-lm's access log)
- Crashed during `_generate → load_default → snapshot_download → thread_map → ThreadPoolExecutor.submit` with the threadpool shutdown error
- Was restarted by the supervisor 60 s later
- Repeat

Zero successful traces in the trace DB during the entire window — every request that came in saw the process die before its response stream completed, so the router's wall-clock timeout fired and the trace was never finalized as completed-or-failed. The dashboard kept showing "MLX server healthy" because the supervisor's `poll_health` saw GET `/v1/models` returning 200 between crashes — and it was, because there's a brief window after each restart where the server is alive but no chat-completion has been attempted yet.

**Insight**: A supervisor that never gives up on restarting is correct for transient failures and catastrophic for persistent upstream bugs. The same code that makes "transient crash → automatic recovery" graceful makes "persistent code path that crashes on every request → 2.5 hours of wasted CPU and log noise." The fix is a quarantine guard: track crash timestamps in a rolling window; if more than `_QUARANTINE_FAILURE_COUNT` happen within `_QUARANTINE_WINDOW_S`, switch to a much slower restart cadence (10 min) and emit a CRITICAL health-check recommendation. Quarantine clears automatically when a restart stays up for the full window — so genuinely transient bursts still recover automatically; only persistent failures get throttled. This is now in `mlx_supervisor.py::_record_crash_and_check_quarantine`. The dashboard surfaces it via the new `mlx_server_quarantined` health check.

Generalizable: any auto-restart loop needs a quarantine state. "Restart on failure" is not a complete strategy without a "stop trying so hard" partner. A health check that says "yeah we know this is broken, we're not going to keep slamming it" is more useful than continuing to flap with no signal that the operator should intervene. Underlying mlx-lm bug filed upstream as [ml-explore/mlx-lm#1208](https://github.com/ml-explore/mlx-lm/issues/1208) — local copy of the issue body in `docs/upstream-issues/mlx-lm-load-default-crashloop.md`.

---

## 2026-04-25 — Profile before optimizing: the cost was where I least expected it

**Evidence**: User asked whether dashboard polling needed caching to reduce model-perf impact. My first instinct said yes for `_detect_image_models()` and `_detect_transcription_models()` — they ran every 5s on the heartbeat and "felt" like binary-scanning could be slow. Profiled the static binary detection: **0.03 ms**. Already fast, would have been wasted effort to cache. Re-profiled with the static-vs-live parts split: the actual 16-17ms cost was `psutil.process_iter()` scanning ~500 processes for `mflux`/`diffusionkit`/`qwen3-asr` names (the live "currently generating?" check), NOT the binary detection.

Even bigger: `/dashboard/api/health` clocked **450 ms per call**. At the dashboard's 15s poll cadence, that's ~3% continuous CPU per active dashboard tab. Most of it is trace DB aggregation over 24h windows for the 18 health checks. This was nowhere on my radar before profiling because the endpoint "doesn't feel expensive" — it's just JSON.

End result: cached three things at 30s TTL, saving ~370 ms/min of agent CPU and ~1.4 sec/min of router CPU per active dashboard tab. The thing I would have cached on instinct (binary detection) wouldn't have helped. The thing I never would have looked at (the health endpoint) was the biggest win by 25×.

**Insight**: "feels slow" is a useless heuristic; "measurable" is the only one that pays. Two operating rules captured by this experience:

1. **Always split a function before benchmarking it.** The 16ms `_detect_image_models()` cost was mis-attributed for ~30 minutes because I profiled the function as a whole. As soon as I split static-vs-live, the answer was obvious. If a hot function does multiple things, time each one separately even if they look like they belong together — your hypothesis about which part is slow is going to be wrong about half the time.
2. **Profile endpoints by request, not just by call.** Anything the dashboard polls at a fixed interval becomes a continuous load proportional to `(per-call cost / poll interval)`. A 450ms endpoint at 15s polling is 3% CPU; the same endpoint at 60s polling is 0.75%. Either the endpoint OR the polling cadence is a knob — both are valid optimizations and both should be considered before assuming the implementation needs caching.

Generalizable beyond this incident: the dashboard polling cadence (15s for `/dashboard/api/health`) interacts multiplicatively with cache TTL choice. A 15s TTL with a 15s polling interval gives effectively 0% hit rate due to timing alignment — was actually the user's first instinct ("cache for 15 seconds") and would have been a no-op savings. 30s TTL gives a guaranteed 50% hit rate. Caching configuration must be calibrated against the consumer's polling rhythm or it's purely cosmetic. See `_HEALTH_CACHE_TTL_S` in `dashboard.py` and `_ttl_cache` in `collector.py` for how these chose their values.

---

## 2026-04-25 — A bumped Homebrew tap is *described*, not *tested*

**Evidence**: 0.6.0 shipped to PyPI + Homebrew on 2026-04-24. The Homebrew tap had been live for three releases (0.5.0 → 0.5.1 → 0.5.2 → 0.6.0), each release bumped via `url` + `sha256` edits to `Formula/ollama-herd.rb`. 24 hours after 0.6.0 went live, ran the *first ever* end-to-end install: `brew install ollama-herd` failed at the first Rust-extension dep (`pydantic-core`) with `error: can't find Rust compiler`. Inspection showed the same failure mode would have occurred in 0.5.x — neither the maintainer nor any user had ever actually executed the install path that the marketing site advertised.

Two bugs were lurking in the formula that no version-bump workflow would have caught:

1. **Missing `depends_on "rust" => :build`.** Homebrew runs `pip install --no-binary :all:` for source-build reproducibility. `pydantic-core`, `cryptography`, and `tiktoken` are Rust-extension Python packages — they need `maturin` to build, which itself needs Rust. Without a Rust toolchain available at build time, every install fails before producing a working venv.

2. **Six `pyproject.toml` deps with no matching `resource` block** (`cryptography`, `cffi`, `pycparser`, `tiktoken`, `regex`, `websockets`). Homebrew's `virtualenv_install_with_resources` doesn't transparently pull missing deps from PyPI — they have to be declared as resources or the install errors out. The formula listed pure-Python deps but skipped the Rust-extension chain entirely, presumably because someone generated the resource list when those deps were not yet in `pyproject.toml`.

There was also a latent third bug: pydantic-core was pinned to 2.45.0 while the formula's pydantic was 2.12.5, which requires `pydantic-core==2.41.5` exactly. Pydantic raises `SystemError` at import time when the versions disagree. So even if the Rust + missing-resources problems were fixed, the install would have produced a broken venv that crashes on first import.

**Insight**: A formula update workflow that touches only `url` + `sha256` carries no signal about whether the install actually works. The maintainer's confidence in the tap was a complete artifact of "the file looks plausible." The fix is procedural: **the release checklist now includes a non-negotiable step that runs `brew uninstall && brew untap && brew tap && brew install ollama-herd` against a fresh-clone of the published formula, plus an import-sanity check inside the produced venv.** Documented in `CLAUDE.md` → "Release checklist" step 13 and as a Gotcha entry. Generalizable principle: any distribution surface that touches transitive dependency resolution (Homebrew formulae, Conda recipes, Docker base-image Pythons, AUR PKGBUILDs) needs to be installed end-to-end on a clean environment as a release gate, not just version-bumped. "Bumped successfully" and "installs successfully" are independent claims; treating them as the same thing is how you get tap-shaped silence in support channels for months.

Bonus operational note: PyPI exposes no uninstall metric, and neither does Homebrew. The closest signals available for catching a botched release are (1) abrupt download dropoff vs the prior baseline and (2) GitHub issue volume. Both are soft and lagging — they will not catch a "the install just doesn't work" problem until someone files an issue, which most users won't bother to do (they'll just leave). The end-to-end install test is the *only* reliable signal.

---

## 2026-04-24 — Long Claude Code sessions on Next-4bit cluster at ~240s; `FLEET_MLX_WALL_CLOCK_TIMEOUT_S=300` is right at the edge

**Evidence**: Post-deploy traffic sample on `mlx:mlx-community/Qwen3-Coder-Next-4bit` with a 2167-message Claude Code CLI session, after Layer 1 clearing reduced the prompt from 103K → 62K tokens:

| Request | Status | Latency |
|---------|--------|---------|
| 11:07:12 | ✅ | 240s |
| 11:11:14 | ✅ | 241s |
| 11:15:16 | ✅ | 242s |
| 11:20:30 | ❌ | 300.5s (timeout) |

Four consecutive turns on the same session cluster within a 2-second band (240-242s), then one edge case fell 0.5s past the 300.0s `FLEET_MLX_WALL_CLOCK_TIMEOUT_S` and got killed. The 30B compactor on port 11441 was not concurrently active — this is pure single-model-long-context generation time, not multi-MLX contention.

**Insight**: On 80B-class MoE models serving long tool-heavy sessions, per-turn latency is remarkably consistent (~1-2% stddev over consecutive turns) because the dominant cost is prefill + KV cache evaluation, both of which scale deterministically with effective token count. **When a workload's p95 latency is ≥80% of the wall-clock budget, the timeout becomes a coin flip** — any momentary slowdown (OS scheduling jitter, memory-bandwidth contention from another process, a KV cache eviction that forces a recomputation) pushes a turn over the edge.

Rule of thumb: set `FLEET_MLX_WALL_CLOCK_TIMEOUT_S` to **at least 2×** your observed p95 turn latency for the worst-case legitimate workload. For our Studio+Next-4bit+2K-message-session pattern that means 600s, not 300s. The default stays 300s for small fleets and short sessions; operators running prolonged agentic workflows should bump it.

Transferable principle beyond MLX: any timeout set to the p95 of observed latency will fire on every adverse-tail request. Set timeouts to catch wedged behavior (orders of magnitude above p95), not to bound best-case behavior.

---

## 2026-04-24 — Dedicated compactor MLX server beats shared Ollama curator on a 3-model-capped host

**Evidence**: Before this, context compaction defaulted to `gpt-oss:120b`
via Ollama.  On a macOS host capped at 3 concurrent Ollama models, that
curator permanently occupied one of the three slots — which in practice
meant that opening a terminal and running `ollama run gemma3:27b` could
silently evict the mapped Claude Code MLX fallback.  The `x-fleet-fallback`
signal caught some of these, but the steady-state pressure was real: every
compaction invocation competed for the same slots the main coding model
depended on, and Ollama's LRU eviction gave us no say in what got dropped.

After shipping multi-MLX-server support, we moved the compactor to a
dedicated `mlx_lm.server` subprocess on port 11441 running
`mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` (≈16 GB, 80B MoE w/
3B active) alongside the main `Qwen3-Coder-Next-4bit` on 11440 (≈42 GB).
Both stay hot continuously.  Compaction now has its own process, its own
prompt cache, its own admission control — and zero pressure on the Ollama
3-slot budget.

**Insight**: the "compactor uses shared Ollama" design made sense when
MLX was single-process-per-node — a dedicated MLX curator would have
forced destructive A/B swaps.  Once multi-MLX was viable, moving the
compactor there resolved three adjacent problems at once:
  1. Ollama eviction no longer threatens the MLX fallback chain.
  2. The compactor's prompt cache persists across invocations (MLX's
     process keeps it warm; Ollama's doesn't, by default).
  3. The main coding model's process never blocks on compactor work —
     they're now fully independent slots.

Generalizable principle: when a background workload (compaction,
embedding, classification) doesn't need the full quality of the main
model, give it its own dedicated process.  The RAM cost is a one-time
static allocation; the slot-contention avoidance compounds over every
turn.  On our 512 GB Mac Studio, 16 GB for a dedicated compactor buys
freedom from Ollama's 3-model cap indefinitely.

---

## 2026-04-24 — Model swaps drift `launchctl`/`~/.zshrc` out of sync, supervisor loads the wrong model silently

**Evidence**: Restarted fleet after shipping P1–P4 Claude Code enhancements. `~/.fleet-manager/env` correctly had `FLEET_NODE_MLX_AUTO_START_MODEL=mlx-community/Qwen3-Coder-Next-4bit` (matches the 2026-04-23 swap away from the 480B). Supervisor logs showed `mlx_lm.server exited unexpectedly (rc=1); restarting in 1.0s` in a tight loop — exponential backoff climbing through 1s/2s/4s/8s/16s/32s. Root cause: both `launchctl getenv FLEET_NODE_MLX_AUTO_START_MODEL` AND `~/.zshrc` still exported the old 480B model. Shell env + launchctl env both win over the env file (`env_file.py:load_env_file()` only sets keys *not already in env*), so the supervisor got the stale 480B string, tried to bind port 11440, collided with a leftover `mlx_lm.server` process from a prior session, exited with rc=1, restart loop.

**Insight**: The 2026-04-02 launchctl observation captured "launchctl setenv is a lie" — this is its sibling failure mode one year later. Whenever a model name, env var, or config value lives in *three* places (`~/.fleet-manager/env`, `~/.zshrc`, `launchctl setenv`), any partial update creates a silent divergence where the supervisor loads a model the operator thought they deprecated. Symptom was *not* "MLX won't start" but "MLX is crash-looping" — the supervisor correctly reported the failure, but the root cause was two directories away from the logs. Defense: when doing a model swap, a single command pattern must update all three locations atomically. A future hardening would be a `scripts/set-mlx-model.sh <model>` helper that writes to the env file, updates `launchctl setenv`, rewrites the `~/.zshrc` export line, and prints a diff — one-shot, no partial states. Also: the supervisor should log the *actual* model name it's launching on every spawn (currently it logs the intended `self.model` but not what ended up on the command line after all env resolution), so operators can catch mismatches from the first log line.

## 2026-06-01 — Embed ReadTimeout storm: software queue starvation, not hardware pressure

**Evidence**: 202 `Embed failed on bb: ReadTimeout` errors in `herd.jsonl` between 00:00–01:33 UTC. Machine metrics at the time: 14% CPU utilization, 221 GB used of 512 GB RAM. Zero hardware pressure. All errors were `nomic-embed-text` via Ollama. The storm stopped when the machine was shut down.

**Root cause**: `OLLAMA_NUM_PARALLEL=2` means Ollama processes at most 2 concurrent inference requests. With `gpt-oss:120b` (71 GB) and `gemma3:27b` (42 GB) both loaded and receiving traffic, both slots were occupied for long periods (120B model can take minutes per complex response). `nomic-embed-text` embed requests queued in Ollama's internal scheduler and eventually timed out on the client side — not from herd's 600s read timeout, but from the client's own patience limit.

**Silent failure dimension**: errors were invisible to the dashboard because the embed route in `ollama_compat.py` had no `record_trace()` calls. The `overall_error_rate_pct` in the health vitals showed 0% throughout. No alert fired. The incident sat undetected until a manual log scan with the correct grep pattern (`'"level": "ERROR"'` with the space that `JSONLFormatter` actually emits).

**Fixes shipped**:
1. **Trace recording**: all embed outcomes (success + failure) now write to the trace DB. `embed_error_rate` health check (WARNING ≥5/h, CRITICAL ≥25/h) surfaces storms within the hour.
2. **Retry on ReadTimeout**: embed route retries twice (1s + 3s backoff) before failing. Brief queue-full conditions clear in seconds.
3. **Native fastembed server** (port 11439): `nomic-embed-text` requests are intercepted before reaching Ollama entirely. fastembed uses ONNX Runtime on CPU — independent of Ollama's queue. The model (130 MB, int8) downloads automatically on first request; subsequent inference is ~4ms.
4. **OLLAMA_NUM_PARALLEL 2→4**: more slots reduce the probability that embed requests wait behind LLM inference if they ever do reach Ollama (e.g., for models not in the native registry).
5. **Health checks**: `text_embedding_ollama_bypass` (WARNING) fires when nomic-embed-text is in Ollama without a native server; `nomic_loaded_in_ollama` (INFO) fires when the native server is up but nomic is still occupying an Ollama slot.

**Insight**: "no failed requests" and "invisible failed requests" look identical on the dashboard. Software queue starvation is orthogonal to hardware utilization — a machine at 14% CPU with 291 GB free can still starve requests through application-layer concurrency limits. The grep pattern `'"level": "ERROR"'` (with space) is required; `'"level":"ERROR"'` (no space) matches zero lines in our JSONLFormatter output and produces a falsely clean result.

---

## 2026-07-11 — GitHub page looked abandoned for 3 months despite active PyPI publishing

**Evidence**: Routine adoption review found the GitHub releases page showing v0.5.2 (April 14, 2026) as the latest release. PyPI had 0.6.0, 0.6.1, 0.6.2, and 0.7.0 — all with full CHANGELOG entries, proper version bumps, and Homebrew tap updates. The gap was 13 weeks. GitHub traffic data confirmed the downstream effect: 17 total page views in the last 14 days, 14 unique visitors. No external forks, no external issues, no community discussion found on Reddit, HN, or similar venues. Any new visitor discovering the project on GitHub would see a repo that apparently hadn't shipped anything since April and has 3 fewer starred versions than PyPI.

**Root cause**: The release checklist covered every distribution surface (PyPI, Homebrew formula, local deploy verification) but never included `git tag` or `gh release create`. This wasn't caught for 13 weeks because all the quality gates were green — tests passed, PyPI accepted the build, Homebrew installed successfully — and none of those gates touch the GitHub releases page. The CLAUDE.md checklist is the only instruction set all agents (human and AI) follow when releasing; anything missing from the checklist simply doesn't happen.

**Fix**: Backfilled v0.6.0–v0.7.0 as GitHub releases (2026-07-11) with full CHANGELOG bodies as release notes. Added step 12 to the release checklist: `git tag -a vX.Y.Z HEAD`, `git push origin vX.Y.Z`, `gh release create` with notes extracted from CHANGELOG. The step is placed between PyPI verification and the Homebrew tap bump, with a note explaining why it matters for discoverability.

**Insight**: The GitHub releases page is the public face of the project's activity — it's what a prospective user, contributor, or integration partner sees when evaluating whether a project is alive. A project can be shipping high-quality work continuously while appearing completely dormant to the outside world if that work isn't surfaced through the release mechanism the platform provides. Checklist-driven release processes have a blind spot: they only enforce what's listed. Any distribution surface not represented by a numbered step will eventually fall behind when someone follows the checklist under time pressure. The fix is to represent every surface explicitly — not to rely on the releaser remembering.

---

## 2026-07-13 — Apple JACCL distributed MLX: asymmetric fleet (512GB + 128GB) has limited tensor-parallel benefit

**Evidence**: WWDC26 session 233 ("Explore distributed inference and training with MLX") + community benchmarks. Apple demonstrated 3× inference speedup on 4× M3 Ultra (512GB each) running Kimi K2.6 (1T param) via JACCL tensor parallelism. Setup: RDMA over Thunderbolt 5, macOS 26.2+, MLX 0.32.0, `MLX_METAL_FAST_SYNCH=1`.

**Key technical constraint**: Tensor parallelism splits model layers evenly across all nodes. The bottleneck is the **smallest node** — with 512GB + 128GB nodes, the effective tensor-parallel capacity is `2 × 128GB = 256GB`, smaller than the Mac Studio alone. Memory savings only come from pipeline parallelism (no inference speedup) or from adding a second identically-sized node.

**Fleet-specific takeaways**:
1. **Immediate value**: The Mac Studio alone already runs all current flagship models at Q4 (Kimi K2.6 ~500GB, Qwen3.5 ~214GB, etc.). The 128GB MacBook Pro adds asymmetric memory that tensor-parallelism can't efficiently use.
2. **EXO over LAN (pipeline)**: For loading models in the 512–640GB range that exceed Mac Studio capacity, EXO's pipeline parallelism over Ethernet works without Recovery mode or cables. The trade-off: zero inference speedup, just memory expansion.
3. **JACCL becomes compelling** with a second M3 Ultra (512+512=1TB): Kimi K2.6 INT8 (~1TB) runs at 28+ tok/s with tensor parallelism and 1 TB5 cable.
4. **RDMA setup gotcha**: enabling RDMA requires physical access (macOS Recovery, `rdma_ctl enable`). Cannot be done remotely. Must be done at each machine independently before the first cluster launch.
5. **`MLX_METAL_FAST_SYNCH=1` is mandatory**: without it, inference is 5–6× slower on JACCL clusters. Set in `mlx.launch` environment, not just the shell.

**Integration path with herd**: Transparent. Run `mlx_lm.server` behind `mlx.launch --hostfile cluster.json` and expose it on the normal port. The herd `MlxSupervisor` registers it as any other MLX server. Distributed cluster management is external to the herd.

**Insight**: Distributed ML inference speedup (vs memory pooling) requires symmetric node memory for tensor parallelism. Asymmetric pairs are better served by pipeline parallelism for memory expansion, or by EXO which handles asymmetry more naturally. The 3× WWDC number applies to 4× identical-capacity nodes — the real-world number for a 2-node asymmetric pair via tensor parallelism is closer to 1.5–1.8× on models small enough to fit the smaller node's shard.

Full research doc: `docs/research/apple-distributed-mlx-jaccl-2026.md`

---

## 2026-07-13 — MLX supervisor polled `http://0.0.0.0` for health when bind host was `0.0.0.0`

**Evidence**: While auditing the codebase ahead of the distributed-MLX work, found that `MlxSupervisor.base_url` was built as `f"http://{self.host}:{self.port}"` and used for *both* the `mlx_lm.server --host` bind flag and every local health poll / warmup request. `self.host` comes from `FLEET_NODE_MLX_BIND_HOST` — and `registry.resolve_mlx_url`'s own docstring instructs operators to set `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` for multi-node MLX aggregation. So any multi-node deploy was polling `http://0.0.0.0:<port>/v1/models` for health. On macOS a client connect to `0.0.0.0` often falls through to loopback, so it "worked" by accident — but it's not a valid connect target and is unreliable/wrong across platforms. A latent bug hiding behind platform leniency, never surfaced because the local fleet historically bound loopback.

**Root cause**: One field (`self.host`) served two distinct roles — the address to *bind* (must be `0.0.0.0` for LAN/distributed) and the address to *dial for health* (must be a concrete loopback). They only coincide when binding loopback.

**Fix**: Added `MlxSupervisor.health_host` — returns `127.0.0.1` when `self.host` is a wildcard bind (`""`, `0.0.0.0`, `::`, `*`), else the concrete host. `base_url` now uses `health_host`; the `--host` flag still carries the real bind. Rank-0 is always local to the node, so loopback is always correct for polling. Startup log now shows `(bind 0.0.0.0, health-poll 127.0.0.1)` when they differ, so operators can see the distinction. Regression tests lock both the wildcard-fallback and concrete-host cases. Fixed as groundwork for `docs/plans/distributed-mlx-inference.md` (distributed mode *requires* a `0.0.0.0` bind, which would have made this bug mandatory).

**Insight**: When one variable encodes two roles that usually have the same value, the divergent case is a latent bug that waits for the configuration that splits them. "Bind address" and "health-probe address" look identical until you expose a service on the LAN. Grep for fields used in both a server's bind path and a client's connect path — they should almost always be separate. Adjacent cleanup shipped the same day: node MLX config consolidated to the single `FLEET_NODE_MLX_SERVERS` surface (legacy `mlx_auto_start_model` / poll-only `MlxClient` paths removed), and binary discovery unified into `common/binaries.py::which_extended` (was three near-duplicate copies across collector, image server, and the MLX supervisor).

---

## 2026-07-13 — MLX "crash burst" was an external kill, not model instability; restart path hardened against EADDRINUSE

**Evidence**: A 24h soak surfaced 5 `mlx_lm.server(port=11440) exited unexpectedly (rc=-9)` WARNINGs on the Qwen3-Coder-Next server. Initial read was "the known-finicky hybrid-attn model." Investigation disproved that:
- All 5 crashes clustered in a single 15-minute window (17:14–17:29), then 20h+ stable — a burst, not chronic.
- The model's own log showed the process was **idle** at every kill (0 chat completions in flight, only 5s health polls), with **no traceback, no OOM, no Metal error** — just a clean restart sequence each time.
- Heartbeat 30s before the first kill: `mem=187/512GB (normal)`, cpu 8% — ~325 GB free. No macOS jetsam events. Rules out memory pressure / OOM.
- The **other** MLX server (:11441, same box, same binary, larger model) had **zero** restarts in the window. Rules out a box-wide `pkill -f mlx_lm.server`, a redeploy, or a global jetsam sweep.
- The long-running supervisor logged "exited unexpectedly" (its monitor *detected* the exit), not an orphan-kill/terminate — so the supervisor didn't cause it.

**Conclusion**: an external, **targeted** `SIGKILL` of the idle port-11440 process (manual `kill -9` / `pkill -f Qwen3-Coder-Next` / a test harness / a model hot-swap) during a development day on the MLX code — not a production stability problem. The killer doesn't write to our logs, so it can't be named from them, but every *systemic* cause is excluded by evidence. Auto-recovery worked flawlessly on all 5 (restart within ~3s, never tripped quarantine).

**Secondary finding + fix**: several restarts logged `OSError: [Errno 48] Address already in use`. After a SIGKILL, TIME_WAIT entries left by the 5s health-check connections can briefly hold the listening port; the supervisor's 1s-later re-`bind()` (no `SO_REUSEADDR` — we don't control `mlx_lm.server`'s socket options) then hits EADDRINUSE, fails, and burns an extra restart cycle. Added `port_is_bindable(host, port)` (a plain no-`SO_REUSEADDR` bind probe mirroring what the child will attempt) + `MlxSupervisor._wait_port_free()`, called before the respawn `Popen` in `_monitor`. It polls (0.25s cadence, 10s cap, honors the stop event) until the port is actually bindable, then spawns — so a crash no longer races the kernel's port release. On timeout it spawns anyway and lets the health check surface a genuine failure.

**Insight**: "process exited unexpectedly (rc=-9)" reads as instability, but `SIGKILL` + *idle* + *no traceback* + *no memory pressure* + *one process while its sibling survives* is the signature of an **external, targeted kill**, not a self-inflicted crash. Before blaming a flaky model, check: was it doing anything, did it log a fault, was the box under pressure, and did peer processes on the same host survive? The blast radius (one process vs. all) is the fastest discriminator between "targeted kill" and "environmental sweep." Separately: any supervised subprocess that binds a fixed port needs a port-release wait on restart, because SIGKILL + short-lived client connections = TIME_WAIT that outlives the process.

---

## 2026-07-15 — A global fallback setting masked a broken request path (and faked a passing test)

**Evidence**: During the 0.8.1 restart verification, a request for `mlx:mlx-community/Qwen3-Coder-Next-4bit` on `/v1/chat/completions` returned HTTP 500 (`Ollama returned 400: "invalid model name"`). The *same* request had "passed" during the 0.8.0 verification ("MLX reply: ok"). Git confirmed `openai_compat.py` has **never** routed `mlx:` prefixes to the MLX proxy (zero `mlx` references at the 0.8.0 tag) — `/v1/chat/completions` always sent them to Ollama. The only thing that changed between the two tests: `FLEET_VRAM_FALLBACK` was ON (default) during the 0.8.0 test and OFF during 0.8.1 (set for a benchmark).

**Root cause**: With VRAM-aware fallback ON, the unservable `mlx:` request (invalid to Ollama) was silently substituted with a *loaded* model (gpt-oss) and returned 200. The 0.8.0 "MLX inference verified" was a **false positive** — it never touched the MLX server; it was a fallback. Turning the global flag off (for strict benchmarking) unmasked the real behavior: the request path was broken all along.

**Insight**: A resilience feature that substitutes on failure will **mask** a broken path *and* fake a green test — a request "succeeds" while doing something entirely different from what was asked. Two lessons: (1) when verifying a specific backend, assert on `X-Fleet-Served-Model` / `x-fleet-fallback: false`, never just on a 200 + plausible output — that's exactly the visibility gap 0.8.1's canonical headers close. (2) Run capability tests with fallback OFF (now a per-request `X-Fleet-No-Fallback: true`) so a substitution can't paper over a real failure. Corollary for operators: `mlx:` models are reached via `/v1/messages` (model-map) or the MLX proxy, not by passing a literal `mlx:` name to `/v1/chat/completions`.

---

## 2026-07-18 — A tok/s number measured under load is not a tok/s number

**Evidence**: Investigating "qwen is slow during the benchmark", `qwen3-coder:30b` measured **24.8 tok/s** and `gpt-oss:120b` **26.3**, while idle models on the same box measured 78.8 (`glm-4.7-flash`) and 80.3 (`gemma3:4b`). That correlation looked like a context-size story — the slow models were loaded at 262144/131072 ctx, the fast ones smaller. It wasn't. The two "slow" models were the only ones the benchmark was driving. Re-measured in isolation, `qwen3-coder:30b` does **108.4 tok/s** — the *fastest* of the three MoE models, not the slowest. `gpt-oss:120b` does 75.1, matching the 74.5 already recorded in CLAUDE.md.

Trace data isolated the real curve: 1 concurrent request → 56.3 tok/s/req, 2 → 31.0, 3 → 29.5 (and cleanly, 1 → 107.3, 2 → 80.2, 4 → 52.7). Aggregate still rises (107 → 160 → 211); per-request latency is what collapses.

**Insight**: Contention produces *plausible* numbers, not obviously broken ones — 24.8 tok/s looks like a config problem, and a tidy correlation (big context = slow) was available to explain it. Two guards: (1) **the contradiction is the tell** — CLAUDE.md already recorded gpt-oss at 74.5 and the fresh measurement said 26.3; reconciling that gap immediately, instead of reporting around it, would have caught this before a research task was launched on a false premise. (2) Before attributing a throughput number to configuration, check whether the model was *serving traffic* while you measured it — `SELECT model, COUNT(*) FROM request_traces WHERE timestamp > strftime('%s','now')-600 GROUP BY model` takes two seconds and invalidates most of the interesting-sounding hypotheses.

**Corollary, measured and source-verified**: allocated KV cache has **zero** effect on decode speed. A randomised, reload-verified A/B on the M3 Ultra gave median 108.1 tok/s at `-c=131072` and 108.1 at `-c=1048576` — 8× the allocation, identical throughput. Attention runs on a view sized by *used* KV (`n_kv`), not the allocation. Oversized context costs **memory, not speed**; `FLEET_DYNAMIC_NUM_CTX` should be justified on memory-reclamation grounds only. What does drive decode: used context (22 tok → 107.7, 18.7K → 75.0, 58K → 46.1) and concurrency.

---

## 2026-07-18 — A config knob that silently does nothing looks exactly like one that works

**Evidence**: `FLEET_NUM_CTX_OVERRIDES={"qwen3-coder:30b": 32768}` with `FLEET_DYNAMIC_NUM_CTX=true` logged **393 injections and 393 strips in 9 hours** — a 100% defeat rate. `_apply_context_protection` injected the override, then the strip branch immediately below removed it because the model was already resident at 262144 and shrinking would force an unload/reload (the multi-minute hang that method exists to prevent). Both lines logged at INFO, so the feature looked busy while doing nothing, and the `context_waste` health check kept recommending a value the system structurally could not apply.

**Insight**: The logic was correct — the override's real job is setting the context a *cold* load comes up with, and refusing to thrash a resident model is right. The defect was **narrative**: two INFO lines that read as "working" when composed they mean "no-op". Fixed by checking the loaded context *before* injecting and, when the override can't apply, saying so once per model at WARNING with the reason and the remedy. Generalisation: when a feature's effect can be cancelled by a later stage in the same function, the cancelling path needs to be as loud as the acting path — otherwise the logs actively argue against the bug.

---

## 2026-07-18 — Codex tells the model to narrate before acting; local models take that as permission to stop

**Evidence**: A Codex Desktop session ended mid-task after emitting *"Let me check for documentation files in the project root and docs directory:"* — 15 tokens, no tool call. Herd was blameless: tools reached the model every turn (`tools=3 custom=['exec']`), the stream completed, no errors. Codex's system prompt instructs *"Before making tool calls, send a brief preamble to the user explaining what you're about to do."* Frontier models satisfy that by emitting preamble **and** tool call in one response; `qwen3-coder:30b` emits the preamble and ends the turn — and a text-only response means "turn complete" in the Responses protocol. Measured on the real captured tool schema, 15 trials each: **15/15** turns called a tool with no preamble instruction, **7/15** with it, **15/15** with a counter-instruction appended.

**Insight**: Client system prompts are tuned for the models the client ships with, and a compatibility shim inherits prompts that assume behaviours local models don't have. The failure is invisible from the server side — every metric says success, because the request *did* succeed; it's the agentic loop that ended early. When a client-side agent "just stops", check whether its own prompt asks for something the local model will satisfy *instead of*, rather than *in addition to*, the action.
