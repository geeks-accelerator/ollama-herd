# Troubleshooting

Common issues and solutions when running Ollama Herd.

Topic-specific guides:
- **MLX backend** — see [MLX Setup Guide](guides/mlx-setup.md) for install / patch / env troubleshooting (`mlx_lm.server: error: unrecognized arguments: --kv-bits`, 120s health-check timeouts, silently-unloaded models after `uv tool upgrade mlx-lm`).
- **Claude Code integration** — see [Claude Code Integration](guides/claude-code-integration.md) for routing, auth, and model-map issues.
- **`FLEET_*` env vars ignored after restart** — both `herd` and `herd-node` auto-load `~/.fleet-manager/env` at startup (see `docs/examples/fleet-env.example` for the template). If vars are still missing, verify the file exists and that the CLIs were started by a version that includes `common/env_file.py`. Shell env always wins, so a stale `export` in your shell profile can mask changes to the file.
- **Vision embedding chips disappeared from the dashboard** (or `/embed` calls return HTTP 500) — the herd-node venv is missing `onnxruntime`, which lives in the optional `embedding` dependency group. Run `uv sync --extra embedding` (or `uv sync --all-extras`, recommended) on the node, then restart `herd-node`. From 0.6.1 onward, the collector probes for `onnxruntime` on every heartbeat and stops advertising vision embedding models when the backend isn't loadable — so the chips disappearing IS the diagnostic signal. A `vision_backend_missing` health check fires WARNING with the same fix command as soon as the asymmetry is detected (weights cached + backend missing). Pre-0.6.1 behavior was to advertise the chips and 500 every `/embed` call, which produced silent failures in agentic dedup loops. **Why this keeps happening**: the project's local-deploy snippet used to be `uv sync` (without `--extra embedding`), and `uv sync` without explicit extras is destructive — it removes any package not in core deps + the requested extras. Every routine restart silently stripped `onnxruntime`. The snippet is now `uv sync --all-extras`. If you're using a custom deploy script, audit it for the same trap.
- **Claude Code CLI quality collapses / tool-call loops around 30K tokens** with local Qwen3-Coder variants — known upstream parser bug ([llama.cpp#20164](https://github.com/ggml-org/llama.cpp/issues/20164)) triggered by tools with multiple optional parameters. Claude Code has 27 tools, most with optional params, so it hits this hard. Mitigations already shipped: `FLEET_ANTHROPIC_TOOL_SCHEMA_FIXUP=inject` (default) promotes known-safe optional params to required-with-default on the outbound schema. If the symptom persists after that, the research doc `docs/research/why-claude-code-degrades-at-30k.md` walks through swapping to Qwen3-Coder-Next (80B MoE / 3B active) which was specifically trained for agentic tool use and runs in ~45 GB vs the 480B's ~200 GB.
- **Claude Code session feels stuck after 1+ hour / prompt over 100K tokens** — hosted Claude handles this by dropping stale tool_result bodies; we do the same via `server/context_management.py`. Three layers of defense, fail-open from cheap to expensive:
  - **Layer 1** — mechanical clearing fires at `FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_TRIGGER_TOKENS` (default 100K) and keeps `FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_KEEP_RECENT` recent results verbatim (default 3). Grep for `tool-result clearing: N→M tokens`.
  - **Layer 2** — LLM-based compactor summarises what Layer 1 left behind. When the post-clearing prompt is still > `FLEET_CONTEXT_COMPACTION_FORCE_TRIGGER_TOKENS` (default 150K), the route passes `force_all=True` to bypass per-strategy bloat gates and summarise everything. Grep for `compaction: N→M tokens`.
  - **Hard cap** — if the prompt STILL exceeds `FLEET_ANTHROPIC_MAX_PROMPT_TOKENS` (default 180K), the request is refused pre-inference with HTTP 413 + a `"run /compact and resubmit"` message. Claude Code CLI surfaces this to the user.
  - **Wall-clock timeout** — independently, any MLX request that exceeds `FLEET_MLX_WALL_CLOCK_TIMEOUT_S` (default 300s) gets its slot released and returns 413 with the same hint. Catches the case where `mlx_lm.server` keeps emitting tokens slowly but never stops (wedged-request syndrome).
  - **If all layers are firing and you're still stuck**: run `/compact` in the Claude Code CLI. Last-resort: restart the Claude Code session (Ctrl+C → new session) — cheap because our prompt cache stays warm across Claude Code restarts.

---

## "Model not found on any node"

**Symptom:** `404` response with `"model(s) 'llama3.3:70b' not found on any node"`.

**Cause:** The model doesn't exist on any fleet node, and auto-pull either failed, timed out, or is disabled.

**Note:** If `FLEET_AUTO_PULL=true` (default), the router will attempt to pull the model onto the best available node before returning 404. Check the router logs for `Auto-pulling` messages. A 404 after auto-pull means the pull failed (network issue, timeout, or no node has enough memory).

**Fix:** Make sure `herd-node` is running on at least one machine with Ollama:

```bash
# On the machine running Ollama
herd-node

# Or with explicit router URL (skips mDNS discovery)
herd-node --router-url http://router-ip:11435
```

Verify the node has registered:

```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

You should see at least one node in the `nodes` array with `"status": "online"`.

---

## LAN Connectivity Issues

### Timeout (no connection)

**Symptom:** `ConnectTimeout(TimeoutError())` — requests hang and then time out. The connection is never established.

**Common causes:**

1. **Different networks** — the most common cause. If one machine is on Wi-Fi and another is on phone tethering (mobile hotspot), they're on completely different networks and can't see each other. Verify both machines are on the same LAN:

   ```bash
   # On each machine, check the IP
   # macOS / Linux
   ifconfig | grep "inet " | grep -v 127.0.0.1
   # Windows (PowerShell)
   # Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' }

   # They should share the same subnet (e.g., both 10.0.0.x or 192.168.1.x)
   ```

2. **Firewall blocking the port** — your OS firewall may be blocking incoming connections:
   - **macOS:** System Settings → Network → Firewall (macOS prompts to allow on first launch)
   - **Linux:** `sudo ufw allow 11435/tcp` (if using ufw) or `sudo firewall-cmd --add-port=11435/tcp --permanent`
   - **Windows:** `netsh advfirewall firewall add rule name="Ollama Herd" dir=in action=allow protocol=tcp localport=11435`

3. **Ollama not bound to all interfaces** — Ollama defaults to `localhost:11434`. The node agent handles this automatically by starting a TCP reverse proxy on the LAN IP that forwards to localhost. If the proxy can't start (e.g., port conflict), you can manually bind Ollama to all interfaces:

   ```bash
   OLLAMA_HOST=0.0.0.0 ollama serve
   ```

### Connection refused

**Symptom:** `ConnectionRefusedError` — the connection is actively rejected.

**Cause:** The port is not open. Either the service isn't running or it's listening on a different port/interface.

**Fix:** Verify the service is listening:

```bash
# macOS / Linux
lsof -i :11435    # herd
lsof -i :11434    # Ollama

# Windows (PowerShell)
# netstat -ano | findstr :11435
# netstat -ano | findstr :11434
```

### Timeout vs. Refused — What's the Difference?

| Behavior | Meaning |
|----------|---------|
| **Timeout** | Packets aren't arriving at all — network/routing issue |
| **Refused** | Packets arrive but port is closed — service not running |

Timeout usually means a network-level problem (wrong network, firewall, routing). Refused usually means the service just isn't running on that port.

---

## mDNS Discovery Not Working

**Symptom:** `herd-node` can't find the router automatically.

**Possible causes:**

1. **mDNS blocked by network** — some enterprise/hotel Wi-Fi networks block multicast traffic. Use explicit connection instead:

   ```bash
   herd-node --router-url http://router-ip:11435
   ```

2. **Firewall blocking mDNS** — mDNS uses UDP port 5353. Ensure it's not blocked.

3. **Different subnets** — mDNS only works within the same broadcast domain (subnet). Machines on different VLANs won't discover each other.

---

## Node Shows "Degraded" or "Offline"

**Symptom:** A node appears as `degraded` or `offline` in the dashboard even though it's running.

**Cause:** The router marks nodes based on heartbeat timing:

| Condition | Status |
|-----------|--------|
| Last heartbeat < `FLEET_HEARTBEAT_TIMEOUT` (15s) | `online` |
| Last heartbeat > timeout but < `FLEET_HEARTBEAT_OFFLINE` (30s) | `degraded` |
| Last heartbeat > offline threshold | `offline` |

**Fix:**
- Check that `herd-node` is still running on the machine
- Check network connectivity between the node and router
- Look at `herd-node` logs for connection errors
- If the node is frequently flapping, increase `FLEET_HEARTBEAT_TIMEOUT`

---

## Ollama Auto-Restart Behavior

**Symptom:** Ollama seems to restart unexpectedly.

**Explanation:** The node agent monitors Ollama health. After 3 consecutive health check failures, it automatically restarts Ollama using `ollama serve`. This is by design — it handles cases where Ollama crashes or is killed externally.

**Timeline:**
1. Ollama becomes unreachable
2. Next 3 heartbeats (every 5 seconds) fail health checks → 15 seconds
3. Agent runs `ollama serve` as a detached process
4. Waits up to 30 seconds for Ollama to become healthy
5. If it doesn't start, the agent exits with an error

The restart uses `shutil.which("ollama")` to find the binary and detaches the process (`start_new_session` on Unix, `CREATE_NEW_PROCESS_GROUP` on Windows) so Ollama survives if the agent is later terminated.

---

## Meeting Detector False Positives

**Symptom:** Node stops accepting work even though you're not in a meeting.

**Cause:** The meeting detector checks for active camera/microphone. Any app using the camera or mic (video calls, streaming apps, screen recording, some browsers) triggers the "in meeting" state, which causes a hard pause.

> **Platform note:** Meeting detection is **macOS only**. On Linux and Windows, the detector is automatically disabled and nodes always report as available. This is a graceful degradation — no configuration needed.

**Fix:** If this is a development machine where the camera is often active:

```bash
# Disable capacity learning entirely (meeting detection is part of it)
FLEET_NODE_ENABLE_CAPACITY_LEARNING=false herd-node
```

Meeting detection is disabled by default — it only activates when `FLEET_NODE_ENABLE_CAPACITY_LEARNING=true` is set.

---

## Context Window Exceeded

**Symptom:** Response includes header `X-Fleet-Context-Overflow: estimated_tokens=5000; context_length=4096`.

**Cause:** The estimated input tokens exceed the model's context window on the winning node. Ollama will truncate the input, potentially losing important context.

**Fix:**
- Use a model with a larger context window (e.g., models with 32K or 128K context)
- Split large inputs into smaller requests
- Increase `FLEET_SCORE_CONTEXT_FIT_MAX` to more aggressively route long inputs to nodes with larger context windows

---

## Auto-Pull Timeout or Failure

**Symptom:** Logs show `Auto-pull timed out` or `Auto-pull failed` and a 404 is returned.

**Possible causes:**

1. **Network issue** — the selected node can't reach the model registry (registry.ollama.ai)
2. **Model too large** — the default timeout is 300s (5 min); large models need more time
3. **No suitable node** — no node has enough available memory to fit the model

**Fix:**
- Verify the node has internet access: `curl -I https://registry.ollama.ai`
- Increase timeout for large models: `FLEET_AUTO_PULL_TIMEOUT=900` (15 min)
- Check available memory on nodes: `curl http://localhost:11435/fleet/status`
- Manually pull on a specific node: `ollama pull <model>` on the target machine
- Disable auto-pull: `FLEET_AUTO_PULL=false`

---

## High Latency or Slow Responses

**Possible causes:**

1. **Cold model loading (most common)** — if the model isn't loaded in memory ("hot"), Ollama needs to load it first. This can take 10-190+ seconds depending on model size. The dashboard shows model thermal state (hot/warm/cold).

   **The #1 fix:** Check your `OLLAMA_KEEP_ALIVE` setting. The default is `5m` — Ollama unloads models after just 5 minutes of idle. On machines with lots of memory, set it to never unload:

   ```bash
   # macOS (GUI Ollama app)
   launchctl setenv OLLAMA_KEEP_ALIVE "-1"
   # Then restart Ollama (⌘Q and reopen)

   # Linux (systemd)
   sudo systemctl edit ollama
   # Add: Environment="OLLAMA_KEEP_ALIVE=-1"
   sudo systemctl restart ollama

   # Windows (PowerShell)
   [System.Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "User")
   # Restart Ollama from the system tray

   # Any platform (terminal session)
   export OLLAMA_KEEP_ALIVE=-1
   ```

   **How to tell if this is your problem:** Run `ollama ps` — if the "Until" column shows a timestamp instead of "Forever", models are being evicted. Also check your traces for high TTFT:

   ```bash
   # Find cold loads (TTFT > 40 seconds) in the last 24 hours
   sqlite3 ~/.fleet-manager/latency.db "
     SELECT model, COUNT(*) as cold_loads,
            ROUND(AVG(time_to_first_token_ms)/1000, 1) as avg_load_sec
     FROM request_traces
     WHERE timestamp > strftime('%s', 'now') - 86400
       AND time_to_first_token_ms > 40000
     GROUP BY model ORDER BY cold_loads DESC;
   "
   ```

   See [Optimize Ollama for your hardware](../README.md#optimize-ollama-for-your-hardware) in the README for the full tuning guide.

2. **Model thrashing** — two or more models alternate requests on the same node, evicting each other in a loop. Every request has 50-190s TTFT, and `ollama ps` only ever shows one model loaded despite having memory for several. Two common causes:

   - **Short keep-alive** — `OLLAMA_KEEP_ALIVE` defaults to `5m`, so idle models get evicted. Fix: `OLLAMA_KEEP_ALIVE=-1` and `OLLAMA_MAX_LOADED_MODELS=-1`.

   - **`OLLAMA_NUM_PARALLEL` too high** — on high-memory machines, Ollama auto-calculates a high parallel slot count (e.g., 16). Each slot pre-allocates KV cache for the full context window. With 16 slots × 262K context, a **single model consumes 384 GB of KV cache** on top of its weights — leaving no room for other models even on a 512GB machine. Fix: `OLLAMA_NUM_PARALLEL=2` (or 3–4). This drops KV cache to ~20 GB per model, allowing multiple models to coexist. The Health dashboard detects this as "KV cache bloat."

   - **`launchctl setenv` gets overridden by shell profile** — if `~/.zshrc` or `~/.bash_profile` contains `launchctl setenv OLLAMA_NUM_PARALLEL 16`, every new terminal session resets the value. You must update BOTH the shell profile file AND run `launchctl setenv` for immediate effect. Verify with `launchctl getenv OLLAMA_NUM_PARALLEL`. The Ollama process only reads the value at startup, so you also need to restart Ollama after changing it.

3. **Queue congestion** — check the dashboard for queue depths. If one node has a deep queue, the rebalancer should redistribute, but you may want to add more nodes.

4. **Memory pressure** — if a node is under memory pressure, the scoring engine penalizes it. Check the dashboard for memory metrics.

5. **KV cache contention** — concurrent requests share KV cache memory. Dynamic concurrency is calculated as `(available_memory - model_size) / 2GB`, clamped to 1-8. Large models with limited headroom may only allow 1-2 concurrent requests.

---

## Ollama llama runner killed by OS (SIGKILL / Jetsam) on memory-tight nodes

**Symptom:** Requests to a large-context model on a memory-constrained node (128 GB MacBook running qwen3-coder:30b-agent at 131K ctx is the canonical case) randomly fail with 500s from Ollama after seconds to minutes of generation. Ollama server log shows:

```
llama runner process no longer running sys=9 string="signal: killed"
post predict error="Post http://127.0.0.1:PORT/completion: EOF"
```

**Cause:** macOS Jetsam (or Linux OOM killer) terminates the llama runner subprocess when system memory gets tight. The model itself fits at rest, but KV cache growth during actual generation — especially with `OLLAMA_NUM_PARALLEL > 1` pre-allocating slots × ctx_length of buffer — tips memory over the OOM threshold mid-request. Other apps (browsers, Claude Code CLI, etc.) compete for the same memory.

**Fix — the reliable four-env-var combination for memory-tight Apple Silicon fleets:**

```bash
# ~/.zshrc  (persistence across sessions)
export OLLAMA_NUM_PARALLEL=1          # 1 KV slot instead of 4 — ~4× less buffer
export OLLAMA_KV_CACHE_TYPE=q8_0      # 8-bit KV cache — halves remaining KV memory
export OLLAMA_FLASH_ATTENTION=1       # required for q8_0 KV to work correctly
export OLLAMA_KEEP_ALIVE=-1           # keep hot; router manages lifecycle
```

```bash
# launchctl — required on macOS because GUI-launched Ollama.app reads from launchd env
launchctl setenv OLLAMA_NUM_PARALLEL 1
launchctl setenv OLLAMA_KV_CACHE_TYPE q8_0
launchctl setenv OLLAMA_FLASH_ATTENTION 1
launchctl setenv OLLAMA_KEEP_ALIVE -1
```

**Observed impact** on an M4 Max 128GB MacBook running qwen3-coder:30b-agent at 131K ctx:

| Metric | Before | After |
|--------|--------|-------|
| Model footprint in VRAM | 31 GB | 25 GB |
| Free memory under sustained Claude Code load | ~500 MB | 14 GB |
| Success rate on 55-message tool-using prompts | ~0% (Jetsam kills) | 100% |
| p50 latency on big_agentic pattern | timeout / retry | ~1 s |

If kills persist, the next layer of defense was previously the Ollama watchdog's automatic `ollama serve` restart. That watchdog was removed on 2026-04-23 after it caused more harm than good (see `docs/issues.md` → "Ollama watchdog cascade-restarted `ollama serve` and wiped pinned models"). If you're hitting repeated runner crashes today, restart `ollama serve` manually and re-check the four env vars above — a restart loop usually means one of them isn't actually set in the environment Ollama launched from.

---

## Debug Checklist

## Requests hang with 0 bytes returned when using `num_ctx`

**Symptom:** Client sends a request with `options.num_ctx` set. The router accepts the connection but returns 0 bytes after minutes, eventually timing out. Streaming requests to the same model (without `num_ctx`) work fine.

**Cause:** When `num_ctx` differs from the model's loaded context window, Ollama unloads and reloads the entire model. For large models (89GB+), this takes minutes and often deadlocks — the runner startup timeout expires and the request hangs indefinitely.

**Fix:** Context protection is enabled by default (`FLEET_CONTEXT_PROTECTION=strip`). The router automatically strips `num_ctx` when it's ≤ the loaded context, and auto-upgrades to a bigger loaded model when more context is needed. If you see this issue, check that context protection hasn't been disabled:

```bash
# Verify context protection is active
curl -s http://localhost:11435/dashboard/api/settings | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"context_protection: {d['config']['context_protection']['context_protection']}\")
"

# Check logs for context protection activity
grep "Context protection" ~/.fleet-manager/logs/herd.jsonl | tail -5
```

If the client genuinely needs a larger context than any loaded model provides, you'll need to load a model with a larger context window.

---

## Quick debugging checklist

When something isn't working:

```bash
# 1. Check if router is running and accessible
curl http://localhost:11435/fleet/status

# 2. Check if nodes are registered
curl -s http://localhost:11435/fleet/status | python3 -c "
import sys, json
d = json.load(sys.stdin)
for n in d.get('nodes', []):
    print(f\"{n['node_id']}: {n['status']} ({len(n.get('ollama', {}).get('models_available', []))} models)\")
"

# 3. Check recent traces for errors
curl -s http://localhost:11435/dashboard/api/traces?limit=5

# 4. Check router logs
tail -20 ~/.fleet-manager/logs/herd.jsonl | python3 -m json.tool

# 5. Test a simple request
curl http://localhost:11435/v1/chat/completions -d '{
  "model": "llama3.2:3b",
  "messages": [{"role": "user", "content": "Hi"}]
}'
```
