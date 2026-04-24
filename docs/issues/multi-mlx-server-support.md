# Support multiple mlx_lm.server processes on different ports

**Status:** ✅ Resolved 2026-04-24
**Severity:** Medium (feature gap, not a bug)
**Filed:** 2026-04-23
**Shipped:** 2026-04-24 (see `CHANGELOG.md` → Unreleased → "Multi-MLX-server support")

The design below was implemented essentially as-proposed.  Summary of what
landed:

- `FLEET_NODE_MLX_SERVERS` JSON list (back-compat with the legacy single-server
  env vars when unset).
- `MlxSupervisorSet` (new) manages N `MlxSupervisor` children with parallel
  start/stop, per-child memory gate, per-child status reporting.
- Heartbeat payload gained `mlx_servers: list[MlxServerInfo]` + `mlx_bind_host`.
- Router registry: `resolve_mlx_url(model)` + `all_mlx_urls()`.
- `MlxProxy` takes an optional `url_resolver` callable; per-URL httpx client
  cache isolates slow/fast backends.
- `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` covers multi-node aggregation (previously
  omitted from the design but needed for the MacBook+Studio case).
- Memory-pressure gate added as `FLEET_NODE_MLX_MEMORY_HEADROOM_GB`.
- Dashboard: per-node MLX server table with colour-coded status + time-since-healthy.
- Two health checks: `mlx_memory_blocked`, `mlx_server_down`.
- 45 new tests, 929 total passing.
- Live on Mac Studio: Next-4bit @ 11440 + 30B-A3B @ 11441 (dedicated compactor).

Keeping this file as a historical record — close via the CHANGELOG entry.

---


## Problem (historical, for reference)

(This section is preserved as the pre-implementation snapshot.  See the
status block above for what actually shipped.)

Today the MLX integration assumes **one** `mlx_lm.server` process per node, on a single URL (`FLEET_NODE_MLX_URL` / `FLEET_MLX_URL`). The supervisor (`node/mlx_supervisor.py`) spawns one process with one `--model` flag; the proxy (`server/mlx_proxy.py`) forwards all `mlx:`-prefixed models to that one URL and trusts that whichever model is loaded is the right one.

`mlx_lm.server` itself is **single-model-per-process** — its `--model` flag is a startup choice, not a runtime swap. To serve N MLX models concurrently we need N `mlx_lm.server` processes on N ports.

Concrete case that surfaced this: 2026-04-23. User wanted to A/B test `mlx-community/Qwen3-Coder-Next-4bit` (80B MoE, 44.8 GB) alongside the currently-running `mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit` (201 GB). The Mac Studio had ~70 GB headroom — enough to fit both weight sets — but the fleet could only route to one at a time because of the single-URL assumption. We ended up doing a destructive swap (shut down the 480B to load the 80B) rather than a side-by-side comparison.

## Why this matters long-term

- **A/B testing** — any time we evaluate a new MLX model, we currently have to sacrifice an existing one. High friction, discourages experimentation.
- **Model specialization** — different Claude tiers could route to different MLX models (e.g. `claude-haiku-*` → small MoE for speed, `claude-opus-*` → 480B for quality) without destructive cycling.
- **Warm-load preservation** — MLX prompt caches are per-process. Killing a process to swap models wipes the prompt cache, losing the 10-100× warm-turn speedup on the old model.
- **Concurrent serving** — with enough headroom, we could have e.g. Qwen3-Coder-Next + a vision MLX model running in parallel on different ports.

## Proposed design

### Configuration shape

Replace the single `FLEET_NODE_MLX_URL` / `FLEET_NODE_MLX_AUTO_START_MODEL` pair with a JSON-valued env var describing a set of MLX server processes:

```bash
FLEET_NODE_MLX_SERVERS='[
  {"model":"mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit","port":11440,"kv_bits":8},
  {"model":"mlx-community/Qwen3-Coder-Next-4bit","port":11441,"kv_bits":8}
]'
```

Back-compat: if the old env vars are set and the new one isn't, synthesize a single-entry list. Old config keeps working.

### Supervisor changes (`node/mlx_supervisor.py`)

- `MlxSupervisor` becomes `MlxSupervisorSet` — manages a list of child processes keyed by `(model, port)`.
- Each child gets its own log file, health check, restart policy.
- Startup: spawn all configured processes in parallel; don't block agent startup on any one of them.
- Shutdown: SIGTERM all, wait up to N seconds, SIGKILL stragglers.

### Router-side proxy changes (`server/mlx_proxy.py`)

- `MlxProxy.__init__` takes a **mapping from model id → base URL** instead of a single base URL.
- Admission control (`_acquire_slot`) already keys by model — good. Keep the semaphore-per-model design; just add a lookup step to pick the URL.
- Node heartbeat already merges `mlx:` loaded models from the node's `mlx_client`. Extend `MlxClient` to poll all URLs and return a `{model: url}` map so the router proxy knows where each MLX model lives.
- Dashboard / `/fleet/status` exposes per-URL health (green/red/loading) so operators can see at a glance which MLX processes are up.

### Node client changes (`node/mlx_client.py`)

- Poll every configured MLX URL's `/v1/models` independently.
- Report the union back in heartbeats, keyed with `mlx:` prefix + source port metadata so the router can route correctly.

### Failure modes to handle

- **One of N fails to start** → agent continues; failed process gets retried on a backoff; the others keep serving. Health endpoint surfaces the failure clearly.
- **Memory pressure** → supervisor should NOT auto-start all servers if system memory is tight. Add a startup-time check: sum of expected model sizes + 20% headroom vs `psutil.virtual_memory().available`. Log and skip if it wouldn't fit.
- **Shared weights on disk** — multiple processes may reference the same model file; HF cache de-dupes naturally, no special handling.

## Alternatives considered

1. **Swap-on-demand**: spawn a new `mlx_lm.server` when a request for a new model arrives, kill it when idle. Rejected: cold-load times on large models (30–120 s) are way too slow for this to feel interactive.

2. **Single process, hot-swap model via API**: upstream `mlx_lm.server` doesn't support this today. Possible future upstream contribution, but the single-model-per-process constraint is baked deep into MLX's prompt-cache and model-loading infra.

3. **Use Ollama for one MLX model via its MLX backend**: Ollama's MLX backend is a dylib in preview, missing on most installs, and only covers a subset of models (not qwen3-coder). Not a viable path today.

## Rough effort estimate

- Supervisor set + config shape: ~half a day
- Proxy multi-URL routing: ~half a day
- Tests (supervisor spawning, proxy routing, failure modes): ~half a day
- Dashboard surfacing: ~2–4 hours
- Doc updates: ~2 hours

**Total: 1.5–2 days of focused work.** Non-trivial but not massive. Worth doing the next time we have a clear need to run 2+ MLX models concurrently.

## Related

- `src/fleet_manager/node/mlx_supervisor.py` — current single-process supervisor
- `src/fleet_manager/server/mlx_proxy.py` — current single-URL proxy
- `src/fleet_manager/node/mlx_client.py` — current single-URL polling client
- `docs/plans/mlx-backend-for-large-models.md` — architecture doc (predates this gap)
- `docs/guides/mlx-setup.md` — operator-facing setup, would need updating alongside this

## Source conversation

Raised 2026-04-23 during an evaluation of Qwen3-Coder-Next as a possible replacement for the 480B. User observation: *"nonetheless at some point need to update MLX integration to support multiple models on different ports instead of just one so we can eventually use more than one mlx model at once."*
