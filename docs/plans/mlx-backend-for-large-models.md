# MLX Backend for Large Models — Breaking the 3-Model Cap

**Status**: Proposed
**Created**: 2026-04-22
**Related**:
- `docs/issues.md` — "Ollama 3-model concurrent-load cap unconfigurable on macOS"
- `docs/plans/hot-fleet-health-checks.md` — detection for silent eviction
- `docs/experiments/mlx-lm-q8kv-benchmark.md` — benchmark that validated parity
- `docs/experiments/mlx-lm-server-kv-bits.patch` — the upstream-pending patch we depend on (applied by the setup script)
- `scripts/setup-mlx.sh` — idempotent installer: pins `mlx-lm==0.31.3`, applies the patch, verifies flags are live. Must be re-run after any `uv tool upgrade mlx-lm`.
- `docs/guides/mlx-setup.md` — operator-facing setup + troubleshooting guide

---

## Motivation

Ollama 0.20.4 on macOS caps concurrent hot models at 3, regardless of `OLLAMA_MAX_LOADED_MODELS` configuration or available RAM. Confirmed hardcoded via exhaustive testing on 2026-04-22 (see `docs/issues.md`). On a 512GB M3 Ultra with 358GB free, Ollama still refused to load a 4th model.

For a realistic Claude Code fleet, we want ≥4 hot models simultaneously:

| Role | Model | Size hot |
|---|---|---|
| Claude Code daily driver | `qwen3-coder:30b-agent` (40K ctx) | ~26 GB |
| Claude Code opus tier | `qwen3-coder:480b-a35b-q4_K_M` | ~285 GB |
| Vision (auto-routed) | `gemma3:27b` | ~42 GB |
| Non-Claude-Code scripts | `gpt-oss:120b` | ~76 GB |

That's 4 models. Total hot would be ~430 GB on 512 GB hardware. Not memory-constrained — **cap-constrained**. Currently one of these gets evicted on every 4th-model load, silently triggering `x-fleet-fallback` routing and degrading Claude Code tool use.

### Why MLX as the escape hatch

We benchmarked [`mlx_lm.server` with `--kv-bits 8`](../experiments/mlx-lm-q8kv-benchmark.md) on identical hardware against Ollama's tuned llama.cpp backend. Result: **320ms median TTFT for MLX+Q8 vs 306ms for Ollama** — within measurement noise on a 25-turn Claude Code workload. Both have working prefix caching.

**MLX's advantage is architectural, not raw speed:**

- Each `mlx_lm.server` is an independent process — its own memory budget, its own scheduler
- No 3-model cap (that's an Ollama internal heuristic)
- `MAX_LOADED_MODELS`-equivalent is process-per-model; no silent env-var parsing failures
- Speculative decoding, vision-first models (mlx-vlm), and draft-model support are first-class

**MLX's tradeoffs we accept:**

- Separate weight files (MLX 4-bit/8-bit formats, not GGUF)
- MLX-community model coverage is narrower than Ollama's catalog
- Ops surface is less mature (no `mlx list`, no unified model registry)
- Stock `mlx_lm.server` lacks `--kv-bits` — we need [our patch](../experiments/mlx-lm-server-kv-bits.patch) or for [upstream PR #1073](https://github.com/ml-explore/mlx-lm/pull/1073) to land

---

## Design: multi-backend herd-node

### Architecture

**Single logical node, two backend processes.** Today `herd-node` talks to one Ollama instance at `localhost:11434`. After this change, `herd-node` knows about two backends:

```
┌─────────────────────────────────────────────────────┐
│ Neons-Mac-Studio (single herd-node process)         │
│                                                      │
│  ┌──────────────────┐      ┌──────────────────┐     │
│  │  Ollama          │      │  mlx_lm.server   │     │
│  │  :11434          │      │  :11440          │     │
│  │                  │      │                  │     │
│  │  qwen3-coder:30b │      │  Qwen3-Coder     │     │
│  │  gpt-oss:120b    │      │    480B (MLX)    │     │
│  │  gemma3:27b      │      │                  │     │
│  └──────────────────┘      └──────────────────┘     │
│           │                         │                │
│           └────────┬────────────────┘                │
│                    │                                 │
│             ┌──────▼──────┐                          │
│             │  herd-node  │  (merges model lists,    │
│             │             │   routes per request)    │
│             └──────┬──────┘                          │
└────────────────────┼─────────────────────────────────┘
                     │
              heartbeat to router
                     │
              ┌──────▼──────┐
              │ herd router │  (unchanged — sees one node
              │   :11435    │   with 4 loaded models)
              └─────────────┘
```

Router-side code is unchanged. From the router's perspective, Neons-Mac-Studio has N loaded models; which backend serves them is an internal detail of the node.

### Node-side changes

**New module: `src/fleet_manager/node/backends/`**

```
backends/
├── __init__.py          # abstract Backend interface + registry
├── ollama_backend.py    # refactored from existing collector logic
└── mlx_backend.py       # NEW — talks to mlx_lm.server
```

**`Backend` protocol:**

```python
class Backend(Protocol):
    name: str                                    # "ollama", "mlx", ...
    base_url: str                                # e.g. http://localhost:11440
    def is_healthy(self) -> bool: ...
    def list_models(self) -> list[ModelInfo]: ...
    def list_loaded(self) -> list[LoadedModel]: ...
    def chat_completions_url(self) -> str: ...   # /v1/chat/completions
    def anthropic_messages_url(self) -> str | None: ...  # /v1/messages if supported
    def pull(self, model: str) -> PullStatus: ...
    def stop(self, model: str) -> None: ...
```

**Changes to `collector.py`:**

- Aggregate `list_models()` and `list_loaded()` across all enabled backends
- Tag each model with its serving backend (e.g., `gpt-oss:120b@ollama`, `Qwen3-Coder-480B-A35B-4bit@mlx`)
- Report unified list in heartbeat

**Changes to `agent.py`:**

- Read backend config from env (see Configuration section)
- Instantiate enabled backends on startup
- For request forwarding: look up which backend owns the requested model, forward there
- Optional: auto-start `mlx_lm.server` as subprocess when `FLEET_MLX_AUTO_START=true`

### Router-side changes

**Minimal.** The router already handles heterogeneous node capabilities. What's needed:

1. **Routing preference by backend** (optional, post-MVP):
   - `FLEET_ROUTE_LARGE_MODELS_TO_MLX=true` — prefer MLX-served copies when the same model exists on both backends (rare, but future-proofing)
2. **Heartbeat payload extension** — `loaded_models` entries gain an optional `backend` field. Router doesn't care about the value; it's only for dashboard display.
3. **Dashboard** (nice-to-have) — show per-backend breakdown in the Nodes-detail panel.

### Model identity

**Use `<registry>/<name>` prefix conventions:**

- `qwen3-coder:30b` → resolved to Ollama (default, no prefix)
- `mlx:Qwen3-Coder-480B-A35B-4bit` → explicitly MLX
- `mlx:mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` → full HF path

This lets the Anthropic model map reference MLX models cleanly:

```json
{
  "claude-sonnet-4-5": "qwen3-coder:30b",
  "claude-opus-4-7": "mlx:Qwen3-Coder-480B-A35B-4bit"
}
```

### Configuration

New env vars (all opt-in, default-off):

| Var | Default | Purpose |
|---|---|---|
| `FLEET_MLX_ENABLED` | `false` | Enable MLX backend in herd-node |
| `FLEET_MLX_HOST` | `http://localhost:11440` | mlx_lm.server URL |
| `FLEET_MLX_MODELS` | `[]` | JSON list of model IDs served by MLX (advisory; node also queries the server) |
| `FLEET_MLX_AUTO_START` | `false` | If true, herd-node starts `mlx_lm.server` as a subprocess |
| `FLEET_MLX_AUTO_START_MODEL` | `""` | Model to pass to `--model` when auto-starting |
| `FLEET_MLX_KV_BITS` | `8` | KV cache quantization bits (requires patched mlx_lm.server — see Open Questions) |
| `FLEET_MLX_CACHE_DIR` | `~/.cache/huggingface` | MLX model download location |

---

## Implementation phases

### Phase 1 — Prove routing works (1 day)

**Goal**: Route `claude-opus-4-7` requests through the existing Anthropic-compat route to a manually-started `mlx_lm.server`.

Steps:
1. Start `mlx_lm.server --model <path> --port 11440 --kv-bits 8` manually (already done in benchmark)
2. Add a minimal proxy path in `anthropic_compat.py`: if the mapped model starts with `mlx:`, forward to `$FLEET_MLX_HOST/v1/chat/completions` instead of the node's Ollama
3. Test that a Claude Code session routes correctly through MLX for opus and through Ollama for sonnet

**Success criterion**: `curl http://localhost:11435/v1/messages` with an opus model ID returns a response from the MLX-served model with clean `tool_use` blocks.

**No refactor yet** — hardcode the MLX backend URL for this phase. Just prove the path.

### Phase 2 — Backend abstraction (2 days)

**Goal**: Refactor existing Ollama-coupled code behind a `Backend` interface.

Steps:
1. Create `src/fleet_manager/node/backends/__init__.py` with the `Backend` protocol
2. Extract existing Ollama logic from `collector.py` into `backends/ollama_backend.py`
3. Implement `backends/mlx_backend.py` with `/v1/chat/completions`, `/v1/models` polling, health check
4. Registry in `backends/__init__.py` keyed by name
5. Update `collector.py` to iterate over enabled backends and merge results
6. Update request-proxy code to look up backend per model and forward

**Tests** (added alongside):
- `tests/test_node/test_backends.py` — unit tests for each backend with mocked HTTP responses
- Existing Ollama-path tests keep passing (regression guard)

**Success criterion**: `uv run pytest tests/test_node/` green, `ollama ps` + `curl localhost:11440/v1/models` both show up in the heartbeat's `loaded_models`.

### Phase 3 — Auto-start mlx_lm.server (1 day)

**Goal**: Single-command herd-node startup launches both backends.

Steps:
1. Add `FLEET_MLX_AUTO_START` env handling in `agent.py`
2. On node startup, if enabled, `subprocess.Popen(["mlx_lm.server", ...])` with configured flags
3. Graceful shutdown: kill subprocess on herd-node exit (handle SIGTERM, SIGINT)
4. Restart on crash with exponential backoff (reuse the existing `platform_connection` backoff pattern)
5. Log to `~/.fleet-manager/logs/mlx-server.jsonl` with rotation

**Success criterion**: `FLEET_MLX_ENABLED=true FLEET_MLX_AUTO_START=true uv run herd-node` brings up both backends, Ctrl+C shuts both down cleanly.

### Phase 4 — Model management UX (2 days)

**Goal**: `herd` CLI can pull/list/remove MLX models alongside Ollama's.

Steps:
1. `herd model pull mlx:<name>` — wraps `huggingface_hub.snapshot_download`
2. `herd model list` — unified list with `backend` column
3. `herd model remove mlx:<name>` — removes from HF cache
4. Dashboard shows per-backend breakdown in Nodes-detail

**Success criterion**: a user reading [`docs/guides/claude-code-integration.md`](../guides/claude-code-integration.md) can pull an MLX model and start using it without knowing MLX internals.

### Phase 5 — Health checks + observability (1 day)

**Goal**: Hot-fleet health checks (see `docs/plans/hot-fleet-health-checks.md`) are backend-aware.

Steps:
1. `mapped_models_hot` considers both backends
2. `model_eviction_churn` doesn't trigger for MLX (no eviction; OOM crashes are different)
3. `fallback_rate` still accurate (fallback can go across backends)
4. New check: `mlx_subprocess_healthy` — WARN if MLX auto-start is on but subprocess keeps restarting

**Success criterion**: `/dashboard/api/health` surfaces backend-specific issues without false positives.

---

## Testing strategy

### Unit tests

- `tests/test_node/test_backends.py` — mock each backend's HTTP, verify adapter correctness
- `tests/test_node/test_ollama_backend.py` — existing tests, refactored to use the new interface
- `tests/test_node/test_mlx_backend.py` — NEW, tests against responses fixtured from real `mlx_lm.server`
- `tests/test_server/test_routing.py` — verify router-side handles mixed-backend heartbeats cleanly

### Integration tests

- `tests/integration/test_mlx_subprocess.py` — spin up real `mlx_lm.server` with a tiny model (`mlx-community/Qwen2.5-0.5B-Instruct-4bit`, ~500 MB), register with herd, run one request end-to-end
- `tests/integration/test_mixed_fleet.py` — Ollama + MLX in same node, route requests to both, verify correct backend serves each

### Regression guards

- All existing 607 tests stay green
- Smoke test: `curl /v1/messages` with an Anthropic model name still routes to Ollama by default (MLX is opt-in)

---

## Concrete fleet target (post-implementation)

With multi-backend enabled on Neons-Mac-Studio:

```
┌────────────────────────── Ollama (:11434) ──────────────────────────┐
│ qwen3-coder:30b-agent    26 GB  @ 40K ctx   → Claude Code daily     │
│ gpt-oss:120b             76 GB  @ 131K ctx  → other scripts          │
│ gemma3:27b               42 GB  @ 131K ctx  → vision (auto-routed)  │
└──────────────────────────────────────────────────────────────────────┘
┌─────────────────────────── MLX (:11440) ────────────────────────────┐
│ Qwen3-Coder-480B-A35B-4bit   285 GB  @ 256K   → claude-opus-4-7     │
└──────────────────────────────────────────────────────────────────────┘

Total hot: ~430 GB / 512 GB
Memory remaining: ~80 GB
4 models concurrently loaded (Ollama 3-cap worked around via MLX)
```

All routes work correctly:
- `claude-sonnet-4-5` → Ollama → qwen3-coder:30b-agent
- `claude-opus-4-7` → MLX → Qwen3-Coder-480B
- Image content → Ollama → gemma3:27b (auto-routed)
- Non-Claude-Code `/api/chat` to `gpt-oss:120b` → Ollama

---

## Open questions

### The patched mlx_lm.server dependency

Phase 1+ require `--kv-bits` on `mlx_lm.server`. Resolved 2026-04-23:

1. ~~**Wait for upstream**~~ — [PR #1073](https://github.com/ml-explore/mlx-lm/pull/1073) still hasn't landed.
2. **Ship with local patch** ✅ — `scripts/setup-mlx.sh` pins `mlx-lm==0.31.3` and applies the patch via embedded Python (more robust than `patch -p1` against the patch file's hunk format). Idempotent; re-run after any upgrade. `mlx_supervisor` preflights for `--kv-bits` support and fails fast with a remediation hint if the patch got wiped.
3. ~~**Accept f16 KV for now**~~ — rejected; the 30% throughput cost on opus-tier is worse than the operational toil of re-running a script after upgrades.

Leaning toward option 3 for the initial ship — it removes the upstream-dependency block and we can upgrade to Q8 KV when upstream lands.

### Model coverage

Not every Ollama model has a pre-built MLX variant on HuggingFace. Initial MLX model whitelist:

- `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` — verified today
- `mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit` — needs existence check
- `mlx-community/Llama-3.3-70B-Instruct-4bit` — standard
- `mlx-community/DeepSeek-V3-*` — depends on release status

Nothing for gpt-oss MLX yet. Fine — gpt-oss stays on Ollama.

### Subprocess lifecycle edge cases

- What if `mlx_lm.server` crashes mid-request? → herd-node marks backend unhealthy, falls back to Ollama-served alternative if available
- What if it hangs? → health-check timeout triggers kill + restart
- What about the long cold-load time (~20-30s for 280 GB model)? → accept cold-start penalty on first opus request after node restart; warm the model on boot via a dummy completion

### Speculative decoding

`mlx_lm.server` has `--draft-model` for speculative decoding. Worth adding to FLEET_MLX_DRAFT_MODEL env var. Post-MVP; nice-to-have that Ollama doesn't offer.

### Vision models

MLX has a separate project — `mlx-vlm` — for vision. Different server binary, different API shape. Out of scope for this plan. Vision stays on Ollama (`gemma3:27b`) which handles it well enough.

---

## Non-goals

- **Not** replacing Ollama. Ollama stays the default backend; MLX is the escape hatch for large-model cases and cap-busting.
- **Not** unifying the two model registries. They're different formats; user picks the backend per model.
- **Not** routing the same model across both backends simultaneously. A given model lives on one backend at a time.
- **Not** cross-process KV cache sharing. Each backend's cache is isolated.
- **Not** mlx-vlm integration. Vision stays on Ollama.

---

## Success criteria

The plan is done when all of these are true:

1. ✅ `FLEET_MLX_ENABLED=true` + `FLEET_MLX_AUTO_START=true` on a Mac Studio brings up 4+ hot models
2. ✅ `/dashboard/api/health` shows no fallback warnings while all 4 are serving traffic
3. ✅ Trace DB shows no `original_model != model` rows on Claude Code sessions for at least 24 hours of active use
4. ✅ Swapping `claude-opus-4-7` mapping between `qwen3-coder:30b-agent` (Ollama) and `mlx:Qwen3-Coder-480B-A35B-4bit` (MLX) works with a single env-var change and `herd` restart
5. ✅ `uv run pytest` green (new tests added, no regressions)
6. ✅ Existing users who don't set `FLEET_MLX_ENABLED` see zero behavior change
7. ✅ Docs updated: `docs/configuration-reference.md`, `docs/operations-guide.md`, `docs/guides/claude-code-integration.md`, `CLAUDE.md` (Architecture section)

---

## Timeline estimate

| Phase | Effort | Can ship independently? |
|---|---|---|
| 1 — Prove routing | 1 day | Yes — demonstrates the path works |
| 2 — Backend abstraction | 2 days | Yes — makes the code maintainable without adding features |
| 3 — Auto-start subprocess | 1 day | Yes — operational win |
| 4 — Model management UX | 2 days | Yes — adoption gate |
| 5 — Health checks | 1 day | Yes — operational polish |
| **Total** | **~1 week** | Phased rollout possible |

Each phase is independently valuable. If we stop after Phase 2, we have a working manual setup. If we stop after Phase 3, users can ship it. Phases 4–5 are adoption and polish.

---

## Out-of-scope future work

- **`herd-mlx-node` as separate binary** — if we ever want physical separation (e.g., run MLX on one machine, Ollama on another). Not needed today; multi-backend-single-node covers the single-machine case.
- **vLLM backend** — Linux GPU servers would benefit from vLLM's prefix caching + continuous batching. Different platform, different backend class. Similar plumbing pattern to MLX backend but for Linux.
- **Backend-affinity routing** — route requests to nodes where the target model is already hot across the fleet. Requires multi-node coordination; plan doc exists as `docs/plans/vram-aware-model-fallback.md`.
- **Cross-backend load balancing** — same model on two backends for redundancy. Rare; deferred.

---

## Related docs

- `docs/issues.md` — Ollama 3-cap OPEN issue (primary driver for this plan)
- `docs/plans/hot-fleet-health-checks.md` — observability plan that this work needs to stay in sync with
- `docs/experiments/mlx-lm-q8kv-benchmark.md` — benchmark that validated MLX is competitive
- `docs/experiments/mlx-lm-server-kv-bits.patch` — the upstream-pending patch
- `docs/observations.md` — "Ollama's llama.cpp engine with FA + Q8 KV beats raw mlx-lm" observation (with corrective update on our patched version matching)
- `docs/guides/claude-code-integration.md` — user-facing docs to update in Phase 4
- `CLAUDE.md` — architecture section needs a new "Backend abstraction" bullet
