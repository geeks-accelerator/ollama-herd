# Hot-Fleet Health Checks

**Status**: Proposed
**Created**: 2026-04-22
**Motivation**: During Claude Code integration setup we spent ~2 hours debugging silent model eviction → silent VRAM fallback → "model produces garbage" user experience. Every symptom was visible in the trace DB and `ollama ps`, but no health check surfaced it. Adding six low-complexity checks would have caught every issue in under a minute.

---

## Background — the silent-failure chain we hit

The failure mode that motivated this plan:

1. User maps `claude-sonnet-4-5 → qwen3-coder:30b` in `FLEET_ANTHROPIC_MODEL_MAP`
2. Ollama's 3-model memory-budget heuristic evicts `qwen3-coder:30b` when a 4th model loads
3. Next Claude Code request hits `/v1/messages`, can't find `qwen3-coder:30b` hot, falls back to nearest available (e.g. `gemma3:4b`)
4. `gemma3:4b` receives Claude Code's tool-heavy prompt, can't emit clean `tool_calls` blocks, produces JSON-as-text
5. User sees broken agentic loop, "Claude Code is garbage tonight"
6. Debugging path: trace DB query → find `x-fleet-fallback: gemma3:4b` header → realize the mapped model isn't hot → re-pre-warm → watch it get evicted again

**All six steps are observable from data the router already collects.** The dashboard just doesn't surface them as health issues yet.

---

## Integration point

`src/fleet_manager/server/health_engine.py` runs 18 named checks today. Each check is a pure function returning a `HealthCheckResult(name, status, message, detail, severity)`. The dashboard at `/dashboard/api/health` aggregates them.

This plan adds **6 new checks** to that framework. Each is ~30 lines of code + tests. Ship as a single commit: `add hot-fleet health checks`.

---

## Check specifications

### 1. `mapped_models_hot` — WARN

**What**: For each distinct model in `FLEET_ANTHROPIC_MODEL_MAP`, verify it's currently hot on at least one fleet node.

**Data source**: `settings.anthropic_model_map.values()` vs. heartbeats' `loaded_models` field per node.

**Trigger**:
- OK when every mapped target is loaded on ≥1 online node
- WARN when any mapped model is not hot — next request will pay cold-load penalty (~15-30s)

**Example message**:
```
Mapped model 'qwen3-coder:30b' is not currently hot on any node. Claude Code requests routed to
claude-sonnet-4-5 will cold-load on next call (~30s delay).
```

**Skip condition**: when `FLEET_ANTHROPIC_MODEL_MAP` is empty (no Anthropic route configured).

### 2. `model_eviction_churn` — WARN

**What**: Detect repeated load→evict→load cycles for the same model in the last hour, signaling memory-budget pressure.

**Data source**: heartbeat history in the trace DB (heartbeats already store `loaded_models` snapshots per node, timestamped).

**Trigger**:
- Query: count transitions where a model was present in heartbeat T-1, absent in T, present in T+1
- WARN at ≥3 churn cycles in 1 hour for any single `(node, model)` pair

**Example message**:
```
qwen3-coder:30b has been evicted and reloaded 4× on Neons-Mac-Studio in the last hour.
Memory budget pressure; some mapped model cannot stay hot alongside others.
```

**Implementation note**: heartbeats are the natural 60-sec sampling interval — cheap to scan.

### 3. `ollama_max_loaded_models_observed` — INFO/WARN

**What**: Infer the *effective* concurrent-model cap from observed behavior, not from reading `OLLAMA_MAX_LOADED_MODELS` env (which is unreliable — see open issues section).

**Data source**: herd-node queries `GET /api/ps` at heartbeat time. Track the maximum number of concurrently-loaded models seen over a rolling window. When we request a load and an eviction happens, record that as the observed cap.

**Trigger**:
- INFO when observed cap ≥ distinct mapped models (everyone fits)
- WARN when observed cap < distinct mapped models (eviction churn inevitable)

**Example message**:
```
Observed hot-model cap: 3 on Neons-Mac-Studio (despite 358GB RAM free). 5 distinct models
are mapped. Expect eviction churn. See ollama/ollama#7041 for upstream status.
```

**Implementation note**: DO NOT read `OLLAMA_MAX_LOADED_MODELS` env value — it reports `-1` regardless of actual configuration on Ollama 0.20.4 macOS. Trust observed behavior only. Details in the open-issues section below.

### 4. `fallback_rate` — WARN

**What**: Percentage of recent `/v1/messages` (and `/v1/chat/completions`) requests where `original_model != model` (fallback was used).

**Data source**: `request_traces` table, `timestamp > now-10min`, compare `original_model` and `model` columns.

**Trigger**:
- OK when fallback rate < 5%
- WARN at ≥ 10% in last 10 min
- ERROR at ≥ 30%

**Example message**:
```
23% of requests in the last 10 minutes hit VRAM fallback (5/22). Mapped models are not staying hot.
Check mapped_models_hot.
```

**Implementation note**: This is the most user-visible check — it captures the exact symptom.

### 5. `context_overallocation` — INFO

**What**: For each hot model, compare its allocated `num_ctx` to the p99 of actual `prompt_tokens` observed in traces. If allocation >> usage, memory is wasted on empty KV cache.

**Data source**: `ollama ps` output (CONTEXT column) + `request_traces.prompt_tokens` p99 over 7 days.

**Trigger**:
- INFO when allocated > 3× p99 → "shrinking num_ctx would free memory"
- Never WARN (this isn't a correctness issue, just efficiency)

**Example message**:
```
qwen3-coder:30b allocated 131072 tokens of context but p99 actual usage is 8,400 tokens.
Setting num_ctx=16384 would free ~24GB KV cache. See FLEET_DYNAMIC_NUM_CTX.
```

**Implementation note**: This check complements `FLEET_DYNAMIC_NUM_CTX` — tells the user whether enabling it would help.

### 6. `cold_load_frequency` — INFO/WARN

**What**: Count first-request-after-load events per model in last hour. Cold loads on 70B+ models add 20-30s latency; if they happen frequently, churn is user-visible.

**Data source**: Identify cold loads by detecting unusually-high latency_ms on the first trace after a gap-in-heartbeat where the model wasn't present.

**Trigger**:
- OK at 0–1 cold load per hour per model
- WARN at ≥3/hour for any model ≥30GB

**Example message**:
```
gpt-oss:120b cold-loaded 4× in the last hour on Neons-Mac-Studio. Each load takes ~25s.
Users are feeling this as slow first requests in Claude Code sessions.
```

---

## Dashboard integration

Existing `/dashboard/api/health` returns `{name, status, message, detail, severity}` per check. All 6 new checks slot in with no schema changes.

Suggested grouping on the dashboard UI: **"Hot Fleet"** as a new section between the existing "Memory" and "Streaming" groups. Or add a dedicated **Claude Code** section (if we want to pull mapped-model status out for that audience).

Relevant existing checks that pair well:
- `memory_pressure` (existing) + `mapped_models_hot` (new) → covers "will my next request be fast?"
- `timeout_rate` (existing) + `fallback_rate` (new) → covers "is the fleet producing useful output?"

---

## Test strategy

Each check gets a unit test in `tests/test_server/test_health_engine.py`:

- Happy-path fixture: model map + all models hot
- Sad-path fixture: mapped model missing from heartbeat's `loaded_models`
- Churn-path fixture: injected heartbeat history showing eviction pattern
- Mock clock / fixture data to avoid real SQL in unit tests (use the existing `TraceStore` test helpers)

Integration test: end-to-end `/dashboard/api/health` response includes the new check names.

**Non-test verification**: re-run the scenario we hit tonight (pre-warm qwen3-coder:30b, load a 4th model to trigger eviction, hit `/v1/messages` with a `claude-sonnet-4-5` prompt). The health endpoint should flip to WARN/ERROR on `mapped_models_hot` and `fallback_rate` within one heartbeat interval.

---

## Implementation order

1. **`mapped_models_hot`** — simplest, highest signal, 30 lines.
2. **`fallback_rate`** — captures the user-visible symptom, ~40 lines.
3. **`model_eviction_churn`** — requires heartbeat-history query, ~60 lines.
4. **`ollama_max_loaded_models_config`** — requires adding one field to heartbeat payload, ~40 lines.
5. **`context_overallocation`** — pure efficiency hint, ~40 lines.
6. **`cold_load_frequency`** — most complex detection logic, ~60 lines.

Total: ~270 lines + tests. One focused commit, one focused PR.

---

## Open issues from tonight's debugging

### macOS memory accounting caveat

Any check that reports "free memory" **must use `psutil.virtual_memory().available`** or the equivalent macOS accounting, not just raw `vm_stat` "Pages free". On Apple Silicon with aggressive caching:

- `Pages free` alone: typically under 1 GB (macOS intentionally keeps idle pages low)
- `Pages free + inactive + speculative`: the real available number, usually hundreds of GB

Debugging tonight showed the dashboard correctly reports 292GB used / 512GB total (= 220GB available), while a naive `vm_stat | awk '/Pages free/'` sum reported 0.6GB free. The naive reading gave a completely misleading picture of memory pressure and should never be used in health-check logic.

### Ollama 3-model cap is HARDCODED on macOS — confirmed 2026-04-22

**Root cause partially understood**. From Ollama's own source (`envconfig/config.go`):

```go
MaxRunners = Uint("OLLAMA_MAX_LOADED_MODELS", 0)
```

- `Uint` parses as unsigned integer
- `-1` **cannot be parsed as unsigned** → reverts to default `0`
- `0` resolves to `defaultModelsPerGPU = 3` in the scheduler

So the string `-1` in env has always been silently invalid. Set a positive integer like `10` to actually raise the cap… except that doesn't work either, see below.

**Comprehensive test of whether the cap can be raised — performed 2026-04-22 on Ollama 0.20.4 / macOS 15.x / M3 Ultra 512GB:**

| Attempt | Result |
|---|---|
| `launchctl setenv OLLAMA_MAX_LOADED_MODELS 10` (confirmed via `launchctl getenv`) | Process env shows `-1`, cap still 3 |
| Plist `EnvironmentVariables.OLLAMA_MAX_LOADED_MODELS=10` at `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` | brew services regenerates plist on restart, value wiped |
| `~/.zshrc` `export OLLAMA_MAX_LOADED_MODELS=10` | Inherited by new shells only; Mac App GUI launch doesn't see it |
| Direct CLI: `OLLAMA_MAX_LOADED_MODELS=10 /Applications/Ollama.app/Contents/Resources/ollama serve` | Process env still shows `-1` |
| Full kill (Mac App + all runners) then clean relaunch with `open -a Ollama` after fixing launchctl + shell rc | Process env still `-1`, cap still 3 |
| Load 4 **distinct** models (different weight blobs) to rule out shared-blob conflict | 4th load evicts LRU — cap is on model count, not weights |

**Memory was never the constraint.** During all tests: 358 GB available RAM, 149 GB hot, Ollama still refused to load a 4th model.

**Conclusion:** Ollama 0.20.4 on macOS / Apple Silicon has an effective hard cap at 3 concurrently-loaded models that cannot be raised by any env-based configuration we tried. The env var path (`OLLAMA_MAX_LOADED_MODELS`) appears to be either:
- Ignored entirely on this build
- Overwritten by an internal default during startup (we see `-1` in `ps eww` after setting any other value)

**Implication for check #3 (`ollama_max_loaded_models_config`):** Cannot infer the effective cap from `OLLAMA_MAX_LOADED_MODELS` env alone — the reported value is unreliable. The check should instead:
1. Query `/api/ps` for the node
2. Count currently-loaded models
3. If the count = some stable maximum (e.g. 3) while more models are mapped AND available memory > sum of model sizes, WARN "probable Ollama hardcoded cap, see upstream issues"
4. Consider the cap an *observed* value, not a configured one

**Related upstream GitHub issues** (track these for eventual fix):
- [ollama/ollama#7041 — Variable OLLAMA_MAX_LOADED_MODELS is being ignored](https://github.com/ollama/ollama/issues/7041)
- [ollama/ollama#4855 — Environment variable OLLAMA_MAX_LOADED_MODELS does not seem to work](https://github.com/ollama/ollama/issues/4855)
- [ollama/ollama#5722 — OLLAMA_NUM_PARALLEL also ignored in some setups](https://github.com/ollama/ollama/issues/5722)
- [ollama/ollama#14953 — iGPU: cap concurrent models (scheduler rework)](https://github.com/ollama/ollama/issues/14953)

Our test conditions (Apple Silicon, 358GB free RAM, 4 distinct weight blobs, env-var-ignored on Ollama 0.20.4) are a cleaner reproducer than anything in those threads — worth a comment on #7041 or a fresh issue when someone has a moment.

### Plist regeneration by brew services

`brew services restart ollama` regenerates the plist from Homebrew's template, stripping any manual `EnvironmentVariables` edits. A `brew services list`-aware solution needs to either:
- Put envs in `launchctl setenv` + shell profile (fragile — shell rc files can clobber)
- Use a wrapper script as the `ProgramArguments` that sources env from a file
- Install Ollama via `.app` installer (Mac App) and rely on it reading from `launchctl`

The Mac App approach worked best for env picking-up of *other* vars (OLLAMA_FLASH_ATTENTION, OLLAMA_KV_CACHE_TYPE, OLLAMA_KEEP_ALIVE, OLLAMA_NUM_PARALLEL all propagated correctly), but the `-1` mystery above persists regardless of install method.

### Workarounds for the 3-cap (none implemented, noted for future)

1. **Accept the cap** — pick the 3 most important models per node
2. **Run a 2nd Ollama instance on a different port** — each daemon has its own 3-slot budget; register both as separate nodes or route-target in herd
3. **Switch specific models to `mlx-lm.server` directly** — bypass Ollama entirely; no cap, more engineering
4. **Upstream fix** — wait for Ollama to fix envconfig parsing or expose a reliable cap override

---

## Non-goals

- **Not** a replacement for Ollama's own memory management. This check framework surfaces what's happening; it doesn't try to *control* what stays hot.
- **Not** a routing-policy change. All checks are observability-only.
- **Not** a cross-node optimizer. Checks run per-node independently and aggregate for the dashboard.

---

## Success criteria

A hackathon-day user who configures `FLEET_ANTHROPIC_MODEL_MAP` and hits silent fallback can:

1. Open `http://<fleet-ip>:11435/dashboard/api/health`
2. See a clear WARN on `mapped_models_hot` with model name and node
3. Resolve via the hint (pre-warm, adjust map, raise OLLAMA_MAX_LOADED_MODELS)
4. Re-check health → green

End-to-end debug time: **under 60 seconds**, vs. the 2 hours it took tonight.
