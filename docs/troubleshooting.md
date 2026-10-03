# Troubleshooting

Common issues and solutions when running Ollama Herd.

Topic-specific guides:
- **MLX backend** — see [MLX Setup Guide](guides/mlx-setup.md) for install / version / env troubleshooting (`mlx_lm.server: error: unrecognized arguments: --kv-bits`, 120s health-check timeouts, silently-unloaded models after `uv tool upgrade mlx-lm`).
- **`mlx_lm.server orphan(s) found on port N: PIDs [...]. Killing them...`** in the herd-node log on startup — the previous herd-node session was killed without also killing its `mlx_lm.server` children, so they were reparented to launchd and kept holding the port. The supervisor (from 0.6.1+) detects this on every `start()` call and SIGKILLs the orphans before its own `Popen`. The single WARNING line is informational — fleet recovers cleanly within seconds. To avoid the warning entirely, use `pkill -9 -f "bin/herd|mlx_lm.server"` (the canonical restart recipe in `CLAUDE.md` § Local deployment) instead of `pkill -f "bin/herd"`. If you see this warning AND the supervisor logs `Cannot kill orphan mlx_lm.server PID N (permission denied)`, the orphan was started under a different user or with elevated privileges — kill it manually with `sudo kill -9 N` and the supervisor will retry on the next heartbeat. Background: 2026-04-27 observation in `docs/observations.md`.
- **Dashboard shows MLX server status `quarantined`** (or `mlx_server_quarantined` health-check recommendation appears) — the supervisor saw 5+ crashes within a 5-minute window and switched to a 10-minute restart interval to stop burning CPU on a persistent failure. Most common cause is the upstream mlx-lm bug filed as [ml-explore/mlx-lm#1208](https://github.com/ml-explore/mlx-lm/issues/1208) — `_generate` → `load_default` → `snapshot_download` → `thread_map` race that puts `mlx_lm.server` into a stuck state where every chat-completion crashes the process. Investigation steps: (1) `tail -200 ~/.fleet-manager/logs/mlx-server-<port>.log` and look for `RuntimeError: cannot schedule new futures after interpreter shutdown` — that's the upstream bug, restart the node to clear; (2) if the trace is different, it's a fresh failure mode worth investigating before restarting. Quarantine clears automatically once a restart stays up for 5 minutes without crashing. Tunable constants: `_QUARANTINE_FAILURE_COUNT` / `_QUARANTINE_WINDOW_S` / `_QUARANTINE_RESTART_INTERVAL` in `src/fleet_manager/node/mlx_supervisor.py`.
- **Claude Code integration** — see [Claude Code Integration](guides/claude-code-integration.md) for routing, auth, and model-map issues.
- **`FLEET_*` env vars ignored after restart** — both `herd` and `herd-node` auto-load `~/.fleet-manager/env` at startup (see `docs/examples/fleet-env.example` for the template). If vars are still missing, verify the file exists and that the CLIs were started by a version that includes `common/env_file.py`. Shell env always wins, so a stale `export` in your shell profile can mask changes to the file.
- **Vision embedding chips disappeared from the dashboard** (or `/embed` calls return HTTP 500) — the herd-node venv is missing `onnxruntime`, which lives in the optional `embedding` dependency group. Run `uv sync --extra embedding` (or `uv sync --all-extras`, recommended) on the node, then restart `herd-node`. From 0.6.1 onward, the collector probes for `onnxruntime` on every heartbeat and stops advertising vision embedding models when the backend isn't loadable — so the chips disappearing IS the diagnostic signal. A `vision_backend_missing` health check fires WARNING with the same fix command as soon as the asymmetry is detected (weights cached + backend missing). Pre-0.6.1 behavior was to advertise the chips and 500 every `/embed` call, which produced silent failures in agentic dedup loops. **Why this keeps happening**: the project's local-deploy snippet used to be `uv sync` (without `--extra embedding`), and `uv sync` without explicit extras is destructive — it removes any package not in core deps + the requested extras. Every routine restart silently stripped `onnxruntime`. The snippet is now `uv sync --all-extras`. If you're using a custom deploy script, audit it for the same trap.
- **nomic-embed-text requests timing out (ReadTimeout)** — embed requests are queuing behind LLM inference inside Ollama. `OLLAMA_NUM_PARALLEL` limits concurrent inference slots; when both are occupied by a 120B model, embeds can wait minutes and time out — even with plentiful CPU and RAM (software queue starvation, not hardware). **Fix:** enable the native fastembed text embedding server: `uv sync --extra embedding && pkill -f herd-node && uv run herd-node &>/dev/null & disown`. The server starts on port 11439 and intercepts `nomic-embed-text` before it reaches Ollama. The `text_embedding_ollama_bypass` health check (WARNING) fires when this situation is detected — its fix text walks you through the same steps. First request after enabling will take ~30s to download the 130 MB model; subsequent requests are <10ms. See `docs/observations.md` 2026-06-01 for the original incident.
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

## herd isn't running at all (and nothing told you)

Health checks cover a *degraded* fleet well and an *absent* one not at all — if the
router is not running, there is nothing to report it. On the reference fleet this
caused three unattended outages in a month, one of them **21 hours**, each time a
reboot with no manual restart.

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:11435/fleet/status   # 000 = down
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT datetime(MAX(timestamp),'unixepoch','localtime') FROM request_traces"  # last traffic
uptime                                                                   # compare to the above
```

Last-traffic matching machine uptime means herd never came back after a reboot.
**Install the launchd agents** (`docs/examples/launchd/`) so this cannot recur, then
use `launchctl`, not `pkill`, to manage them — `KeepAlive` respawns within 30s, so a
pkill-then-start sequence races itself:

```bash
launchctl list | grep ollama-herd                                          # 3rd col = last exit code
launchctl kickstart -k gui/$UID/com.geeksaccelerator.ollama-herd.router    # restart
launchctl bootout   gui/$UID/com.geeksaccelerator.ollama-herd.node         # really stop
pkill -9 -f mlx_lm.server   # MLX children use start_new_session; reap separately
```

## A model is resident at the wrong context (KV memory 4x what you configured)

Symptom: `FLEET_NUM_CTX_OVERRIDES` says 32768, but the model is running at 131072 —
and the router *refuses to correct it*, logging:

```
Dynamic num_ctx: override num_ctx=32768 for gemma3:27b cannot apply
  -- already resident at 32768. Shrinking would force an unload/reload...
```

while the backend says otherwise. The router is trusting a cached value the backend
has since contradicted, so it never self-heals. Confirm from the launch args, which
are authoritative (**not** `ollama ps` — it reported `CONTEXT 131072` for models at
both 131,072/slot and 32,768/slot):

```bash
ps -Ao args | grep llama-server | grep -oE '\-c [0-9]+ \-np [0-9]+'
# per-slot = -c / -np
```

**Workaround:** `ollama stop <model>`, then send one request through the router
(`:11435`), which reloads it with the override applied. Verify with the args again.

This is worth fixing rather than living with: an oversized resident model is exactly
what made Ollama predict 341.7 GiB for a 16 GB model, try to evict an unevictable
`KEEP_ALIVE=-1` model, and then **hang forever instead of erroring** — every
subsequent model load timed out. Tracked in `docs/issues.md`.

## A model load hangs forever instead of failing

If `/api/generate` for a not-yet-loaded model never returns and the request never
even appears in Ollama's GIN log, the scheduler is stuck. Look for:

```bash
grep -a "predicted\|evicting" ~/.ollama/logs/server.log | tail -3
```

`predicted to exceed available memory, evicting` with an absurd `predicted=` for a
small model means the context math went wrong (`predicted_num_ctx` = context ×
`OLLAMA_NUM_PARALLEL`). It then tries to evict, cannot if the resident model is
`KEEP_ALIVE=-1` (`expires_at` far in the future via `/api/ps`), and hangs. Restart
Ollama to clear it, and fix the context so the prediction is sane — see
`docs/configuration-reference.md` § Ollama environment.

## Is herd itself leaking memory?

**Do not start from system memory — it cannot answer this.** On a large box that
number is dominated by Ollama's resident weights (~91 GB here) plus any
`mlx_lm.server` children (17 GB each), so a 20 GB leak inside `herd-node` does not
move it enough to notice. In the 2026-10-02 incident the system series read 205–246
GB over seven days with no climb, and the hours before the incident were among the
lowest in the window. Two devices hit the same bug and neither produced a usable
history from it.

**Read herd's own numbers instead** (since 0.9.7):

```bash
# current, per node, with children broken out
curl -s localhost:11435/fleet/status | python3 -c "
import json,sys
for n in json.load(sys.stdin)['nodes']:
    pm = n.get('process_memory') or {}
    print(n['node_id'], 'agent', pm.get('footprint_gb'), 'peak', pm.get('peak_gb'))
    for c in pm.get('children', []):
        print('   ', c['role'], c['footprint_gb'], 'peak', c['peak_gb'])"

# the trend — the heartbeat summary line is the only durable history, and it
# rotates daily, so scan the rotations too
grep -ho 'self=[0-9.]*GB (peak [0-9.]*GB)' ~/.fleet-manager/logs/herd-node.jsonl*
```

**Three things to get right, each of which has already cost real time:**

1. **Read `peak`, not current.** ONNX Runtime keeps the high-water mark of the
   largest run a process ever does, so one oversized request raises the floor
   permanently and current usage afterwards reads innocent. A healthy result is a
   *flat peak*, not a low current. A flat current proves nothing.
2. **Use footprint, not RSS.** `phys_footprint` is what the kernel charges the
   process; RSS is the metric that hid the original 28 GB. Confirm by hand with
   `footprint -p <pid>`, not `ps -o rss`. Note `psutil.memory_full_info()` raises
   `AccessDenied` on macOS without root, which is why herd shells out.
3. **Compare the agent against itself, never the total.** The total on this fleet
   is ~37 GB and almost all of it is legitimate MLX model weights — it hides the
   agent's own 3.7 GB as thoroughly as system memory hid the 28 GB. The embedding
   servers are asyncio tasks *inside* the agent, not subprocesses, so their ONNX
   arenas are charged to the agent's own figure; `mlx` and `transcription` children
   are separate processes holding weights and are excluded from the health check
   for that reason.

**What normal looks like here:** agent at ~0.1 GB cold, settling near **3.7 GB**
once nomic and the reranker have each served a long input, then flat. Measured over
9 h: peak rose once to 3.80 GB and held exactly flat for 10 h while current drifted
down 3.53 → 3.47 GB. That is the designed ceiling (bounded, not released — see the
open `_slots` eviction issue), not a leak.

**When it is a leak:** the `herd_process_memory` check fires WARNING above 8 GB and
CRITICAL above 16 GB for the agent's own process. 8 GB is roughly double the
designed ceiling and is also the figure from the incident, which passed "more than 8
GB within minutes" on its way to 28 GB. Restart to reclaim the high-water mark
(`launchctl kickstart -k gui/$UID/com.geeksaccelerator.ollama-herd.node`), then find
what raised it — oversized embedding inputs are the known cause, and `prompt_tokens`
in `request_traces` now records real tokenizer counts rather than word counts, so a
long unspaced input no longer records as `1`.

**Known gap:** the router probes itself for the health check but that figure is not
persisted, so it has no history; and nothing writes any of these numbers to
`request_traces` or a time series. The heartbeat log line is the only durable record
and it rotates daily — good for a day's curve, not a week's.

---

## Fleet-wide throughput dropped and nothing in the dashboard explains it

**Check this first — before tuning anything, and before suspecting an Ollama or
llama.cpp upgrade.** herd's scheduler assumes it is the *only* client of its
backends. Every signal it scores — queue depth, free slots, session affinity,
context fit — is derived from what herd itself dispatched. If another process on
the box talks to Ollama directly (`http://localhost:11434`), that traffic is not
merely unmeasured: it silently invalidates the arithmetic. `QueueManager` caps a
queue's concurrency to match llama-server's `-np`, so a co-tenant filling the same
slots means herd's "concurrency 2" is really occupancy 3–4 at the backend.

The symptom is distinctive: **herd's own numbers all look healthy** — no errors,
no retries, no fallbacks, memory fine — while throughput is plainly down.

**Check the dashboard first — this is now automated.** Since 0.9.7 the
`backend_bypass_clients` health check names the culprit directly:

```bash
curl -s localhost:11435/dashboard/api/health | python3 -c "
import json,sys
for r in json.load(sys.stdin)['recommendations']:
    if r['check_id'] == 'backend_bypass_clients':
        for c in r['data']['clients']:
            print(c['node_id'], c['pid'], c['process'], c['connections'], c['cmdline'])"
```

Each node probes who holds established connections to its Ollama and reports any
process that is not herd, a child herd spawned, or Ollama itself. It fires within
about a minute of a foreign client appearing and clears about a minute after it
leaves. **A silent check is not proof of absence:** an empty list also means "the
probe could not run" (no `lsof`, or Windows, where it is unimplemented), because
the check only ever fires on a positive sighting — a blind node is a missed
detection, never a false alarm. So if throughput is down and this card is absent,
still run the manual reconciliation below.

**Reconcile backend-side work against router-side dispatch:**

```bash
# what the backend actually did
grep -c "new prompt, n_ctx_slot" ~/.ollama/logs/server.log

# what herd dispatched
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT date(timestamp,'unixepoch'), COUNT(*) FROM request_traces GROUP BY 1"
```

**Only a gap in one direction matters.** `backend > router` means work herd never
dispatched — that is the co-tenant signal. `router > backend` is benign and common:
the backend line is attributed by the nearest preceding `[GIN]` timestamp (approximate),
and the log rotates. (This section used to add that a fully-cached prompt may not
emit a `new prompt` line; on 0.34.4 it does — see the correction further down.)
Treat this as a **rough tripwire, not an audit** — a sustained `backend > router`
excess of tens of percent is the alarm; small gaps either way are noise. When it
does trip, confirm with `lsof` below before concluding anything.

**Identify the client.** herd and herd-node connect over IPv6 (`::1`); most other
tools use IPv4 (`127.0.0.1`), so they separate at a glance:

```bash
lsof -nP -iTCP:11434 -sTCP:ESTABLISHED
```

Do **not** trust the process name — `ps` shows whatever `process.title` a Node
daemon set, and its `cwd` may be `/`. Resolve the real identity from its file
descriptors:

```bash
lsof -p <pid> | awk '$4=="txt"{print $NF}'    # actual executable
lsof -p <pid> | grep -E '\.json$'             # config file it opened
```

A globally-installed CLI can share a name with an unrelated project folder — check
the binary path, not the name.

**Two other tells of a bypassing client:**

- `ollama ps` shows a model at a larger `CONTEXT` than `FLEET_NUM_CTX_OVERRIDES`
  specifies. A request that skips the router also skips `num_ctx` resolution, so
  Ollama applies its own default.
- Oversized prompts appear in the backend log that never appear in herd's traces
  **at all**:
  ```bash
  grep -oE "task\.n_tokens = [0-9]+" ~/.ollama/logs/server.log | sort -t= -k2 -n | tail
  ```
  A bypassing client's request is absent from `request_traces` entirely — that
  absence is the tell. **Correction (2026-10-02):** this section previously said
  `prompt_tokens` counts cache misses rather than full context, so a long-context
  request "looks small in traces". That is wrong. `prompt_eval_count` is the full
  prompt length — verified on Ollama 0.34.4 by resending an identical prompt:
  `prompt_eval_count=4074` both times while `prompt_eval_duration` fell
  2.235 s → 0.021 s, so the prefix cache was hit and the count did not move.
  Likewise, a fully-cached prompt **does** emit a `new prompt` line on 0.34.4,
  which makes the reconciliation above tighter than described (674 traces against
  679 backend lines over the same 2.4 h window).

**Fix:** point the other client at the router (`:11435`) instead of the backend
(`:11434`). herd is Ollama-API compatible, so this is usually a one-line change to
an `OLLAMA_BASE_URL` / `OLLAMA_HOST` setting. Watch for tools that reach your fleet
by *fallback* rather than by configuration — a cloud provider losing its API key
can silently redirect an entire workload onto local hardware.

**Measure with p25, not the mean.** This class of problem hits the tail far harder
than the typical request. In the 2026-08-23 incident the median fell 6% while p25
fell 44% — a mean-or-median dashboard hid it almost completely. Full case study in
`docs/observations.md` (2026-08-23).

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

   - **`OLLAMA_NUM_PARALLEL` too high** — on high-memory machines, Ollama auto-calculates a high parallel slot count (e.g., 16). Each slot pre-allocates KV cache for the full context window. With 16 slots × 262K context, a **single model consumes 384 GB of KV cache** on top of its weights — leaving no room for other models even on a 512GB machine. Fix: `OLLAMA_NUM_PARALLEL=4` (or 2–6 depending on workload). This drops KV cache to a manageable level while still allowing embed requests to get slots alongside LLM inference. With `FLEET_DYNAMIC_NUM_CTX=true`, context windows shrink to actual usage (p99), further reducing per-slot KV cache. The Health dashboard detects excessive KV cache as "KV cache bloat." Note: if you're running the native fastembed text embedding server (port 11439), embed requests no longer compete for Ollama slots, so you can be more generous with `OLLAMA_NUM_PARALLEL` for LLM concurrency.

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

## Trace DB write failures / "database is locked" / dashboard shows reqs_24h=0

**Symptom:** Health endpoint emits `trace_store_write_failures` (WARNING at 1+, CRITICAL at 50+ failures in the last 5 min). The dashboard's `reqs_24h` shows 0 despite the router clearly serving traffic. `~/.fleet-manager/logs/herd.jsonl` has lines like:

```
{"level": "ERROR", "logger": "fleet_manager.server.streaming",
 "msg": "Background task 'trace-record-abc12345' failed: database is locked", ...}
```

**What's happening:** SQLite WAL mode allows concurrent readers and one writer. When a long-running reader holds an old WAL snapshot open, the WAL can't checkpoint and grows unboundedly. Eventually the 30-second `busy_timeout` + 3-retry backoff in `TraceStore.record_trace` (≈90s cumulative patience) is exhausted, and the background trace task gives up. Requests themselves still succeed (trace writes are fire-and-forget) but observability evaporates because the dashboard queries the same DB that's not getting writes. Full incident post-mortem: 2026-05-15 entry in `docs/observations.md`.

**Diagnosis:**

```bash
# 1. WAL size — should be <100 MB under normal operation
ls -lh ~/.fleet-manager/latency.db*

# 2. Last successful trace write — should be within the last minute under load
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT datetime(timestamp,'unixepoch','localtime'), model, status \
   FROM request_traces ORDER BY timestamp DESC LIMIT 1"

# 3. Disk space on the data dir
df -h ~/.fleet-manager

# 4. Failure count surfaced by health check (immediate signal)
curl -s http://localhost:11435/dashboard/api/health | \
  python3 -c "import sys,json; d=json.load(sys.stdin); \
  print([r for r in d['recommendations'] if r['check_id']=='trace_store_write_failures'])"
```

**Resolution:**

```bash
# 1. Stop all writers (also kills MLX children — start_new_session=True orphans them otherwise)
pkill -9 -f "bin/herd|mlx_lm.server"
sleep 3

# 2. Drain the WAL back into the main DB.  Returns "0|0|0" on success;
#    non-zero means another process is still holding the lock.
sqlite3 ~/.fleet-manager/latency.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 3. Verify WAL is now empty (latency.db-wal should be 0 bytes)
ls -lh ~/.fleet-manager/latency.db*

# 4. Restart
uv run herd &>/dev/null & disown
sleep 4
uv run herd-node &>/dev/null & disown

# 5. Confirm traces resume — send a probe, then look in the DB
curl -X POST http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"<a-loaded-model>","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT datetime(timestamp,'unixepoch','localtime'), model, status \
   FROM request_traces ORDER BY timestamp DESC LIMIT 1"
```

**Prevention** (already in 0.6.2+, listed innermost to outermost):

- `PRAGMA busy_timeout=30000` on every connection — writers wait up to 30s for the lock before failing
- `record_trace` retries 3 times on locked errors with exponential backoff (200ms → 800ms → 2s)
- `PRAGMA wal_autocheckpoint=100` — autocheckpoints when 100 WAL pages have been written (volume-triggered)
- **Dedicated `_read_db` connection per store** with `PRAGMA query_only=1` — dashboard reads run on a separate aiosqlite thread so they don't serialize behind writes; reader snapshots don't pin the writer's view of the WAL barrier
- **Periodic `PRAGMA wal_checkpoint(PASSIVE)`** every 10 seconds from a background task in `app.py` lifespan — wall-clock-triggered, fires in the gaps between dashboard read snapshots so the WAL drains even under bursty traffic
- `trace_store_write_failures` health check (WARNING at 1+ failures in 5 min, CRITICAL at 50+) — makes the failure mode dashboard-visible instead of requiring operators to grep logs

The two structural layers (`_read_db` connection + periodic checkpoint) were added 2026-05-16 after the first round of fixes (busy_timeout + retry + autocheckpoint, shipped 2026-05-15) didn't fully eliminate the failure under sustained traffic. See `docs/plans/trace-store-read-connection-and-checkpoint.md` for the analysis and `docs/observations.md` 2026-05-16 for the lesson on why "longer timeout + retry" doesn't fix structural contention.

**If it recurs**: there's likely a slow query path in the codebase that needs an index, or a reader that holds a transaction open across an `await`. Capture `EXPLAIN QUERY PLAN` for recently-added queries and check trace_store / latency_store for long-running read transactions that span an `await`.

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
