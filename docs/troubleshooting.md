# Troubleshooting

Common issues and solutions when running Ollama Herd.

---

## "Model not found on any node"

**Symptom:** `404` response with `"model(s) 'llama3.3:70b' not found on any node"`.

**Cause:** The router doesn't talk to Ollama directly — it only knows about nodes that have sent heartbeats via `herd-node`. Without a running node agent, the router has zero nodes and cannot find any models.

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
   ifconfig | grep "inet " | grep -v 127.0.0.1

   # They should share the same subnet (e.g., both 10.0.0.x or 192.168.1.x)
   ```

2. **Firewall blocking the port** — macOS may prompt to allow incoming connections when `herd` first starts. Check System Settings → Network → Firewall.

3. **Ollama not bound to all interfaces** — Ollama defaults to `localhost:11434`. The node agent handles this automatically by starting a TCP reverse proxy on the LAN IP that forwards to localhost. If the proxy can't start (e.g., port conflict), you can manually bind Ollama to all interfaces:

   ```bash
   OLLAMA_HOST=0.0.0.0 ollama serve
   ```

### Connection refused

**Symptom:** `ConnectionRefusedError` — the connection is actively rejected.

**Cause:** The port is not open. Either the service isn't running or it's listening on a different port/interface.

**Fix:** Verify the service is listening:

```bash
# Check if herd is listening
lsof -i :11435

# Check if Ollama is listening
lsof -i :11434
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

The restart uses `shutil.which("ollama")` to find the binary and `start_new_session=True` so Ollama survives if the agent is later terminated.

---

## Meeting Detector False Positives

**Symptom:** Node stops accepting work even though you're not in a meeting.

**Cause:** The macOS meeting detector checks for active camera/microphone. Any app using the camera or mic (video calls, streaming apps, screen recording, some browsers) triggers the "in meeting" state, which causes a hard pause.

**Fix:** If this is a development machine where the camera is often active:

```bash
# Disable capacity learning entirely (meeting detection is part of it)
FLEET_NODE_ENABLE_CAPACITY_LEARNING=false herd-node
```

Meeting detection is disabled by default — it only activates when `FLEET_NODE_ENABLE_CAPACITY_LEARNING=true` is set.

---

## High Latency or Slow Responses

**Possible causes:**

1. **Cold model loading (most common)** — if the model isn't loaded in memory ("hot"), Ollama needs to load it first. This can take 10-190+ seconds depending on model size. The dashboard shows model thermal state (hot/warm/cold).

   **The #1 fix:** Check your `OLLAMA_KEEP_ALIVE` setting. The default is `5m` — Ollama unloads models after just 5 minutes of idle. On machines with lots of memory, set it to never unload:

   ```bash
   # macOS (GUI Ollama app)
   launchctl setenv OLLAMA_KEEP_ALIVE "-1"
   # Then restart Ollama (⌘Q and reopen)

   # Linux / terminal
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

2. **Model thrashing** — if two or more models alternate requests on the same node and keep-alive is short, they evict each other in a loop. Symptoms: every request has 50-190s TTFT, `ollama ps` only ever shows one model loaded despite having memory for several. Fix: `OLLAMA_KEEP_ALIVE=-1` and `OLLAMA_MAX_LOADED_MODELS=-1`.

3. **Queue congestion** — check the dashboard for queue depths. If one node has a deep queue, the rebalancer should redistribute, but you may want to add more nodes.

4. **Memory pressure** — if a node is under memory pressure, the scoring engine penalizes it. Check the dashboard for memory metrics.

5. **KV cache contention** — concurrent requests share KV cache memory. Dynamic concurrency is calculated as `(available_memory - model_size) / 2GB`, clamped to 1-8. Large models with limited headroom may only allow 1-2 concurrent requests.

---

## Debug Checklist

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
