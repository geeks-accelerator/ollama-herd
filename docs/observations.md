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
