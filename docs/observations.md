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

*Add new observations above this line. Date them. Link evidence. Extract the transferable insight.*
