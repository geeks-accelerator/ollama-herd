# Device-Aware Scoring

## Why

The scorer treats a 512 GB M3 Ultra Mac Studio and a 128 GB M3 MacBook as
essentially equivalent for any model over 20 GB — both hit the same `≥128 GB`
tier of Signal 5 (role affinity) and tie at 100/100.  On a real fleet that
means the MacBook keeps winning 50%+ of Claude Code traffic despite being
**~4× slower at prompt eval** because its ~300 GB/s memory bandwidth doesn't
come close to the Studio's ~800 GB/s.

For prompt-eval-bound workloads (long Claude Code contexts, big system
prompts, multi-tool conversations) this is the dominant bottleneck — and it's
currently invisible to routing.

The observed scoring breakdown from production confirms it:

```
Lucass-MacBook-Pro-2    total=100.0  thermal=50  mem=20  queue=-0  affinity=15  ctx=15  wait=0
Neons-Mac-Studio        total=100.0  thermal=50  mem=20  queue=-0  affinity=15  ctx=15  wait=0
```

Whichever node tiebreaks first wins every request.

## Goals

1. **Device capability as a first-class signal** — chip model and memory
   bandwidth flow from collector → heartbeat → scorer without operator
   intervention.
2. **Cold-start routing is correct** — day-one routing to Studio is right
   even before the latency store has trace data.
3. **Proportional load distribution under pressure** — when Studio is
   saturated, the MacBook picks up its *fair share* (≈ capacity ratio),
   not an arbitrary cutoff.
4. **No regressions on existing fleets** — nodes with unknown chips fall
   back to today's behavior.  No operator config required to stay working.

## Non-goals

- Per-turn model switching within a Claude Code conversation (separate
  discussion — KV cache prefix reuse makes this a loss in most cases).
- Splitting a single request across multiple nodes (tensor parallelism).
  That's llama.cpp/MLX territory, not router territory.
- Full GPU telemetry on Linux/NVIDIA.  Apple Silicon is the primary
  target; Linux gets a reasonable heuristic but not precise numbers.

## Design

### Part 1 — Chip detection in collector

**macOS:**
```
sysctl -n machdep.cpu.brand_string
→ "Apple M3 Ultra" | "Apple M3 Max" | "Apple M3 Pro" | "Apple M3"
```

**Linux:**
```
/proc/cpuinfo  + nvidia-smi (if present) for discrete GPU
→ "Intel Xeon + NVIDIA A100" (free-form string, best-effort)
```

**Windows:**
```
wmic cpu get name  →  best effort string
```

Any unrecognized string is passed through verbatim to the heartbeat so we
can see what operators have out there.

### Part 2 — Bandwidth lookup

A small hardcoded table in `server/hardware_lookup.py`:

```python
# Unified-memory chips (Apple Silicon) — memory bandwidth in GB/s
APPLE_SILICON_BANDWIDTH = {
    "apple m3 ultra":   819,
    "apple m3 max":     400,     # 16-core variant; 10-core is 300
    "apple m3 pro":     150,     # 12-core; 10-core is 120
    "apple m3":         100,
    "apple m2 ultra":   800,
    "apple m2 max":     400,
    "apple m2 pro":     200,
    "apple m2":         100,
    "apple m1 ultra":   800,
    "apple m1 max":     400,
    "apple m1 pro":     200,
    "apple m1":         68,
}
```

Lookup is lowercase + normalized ("Apple M3 Ultra" → "apple m3 ultra").
Unknown chip → return `None` → scorer falls back to memory_total_gb heuristic.

### Part 3 — NodeState extensions

Add to `HardwareProfile` (already has `chip: str = ""`):

```python
memory_bandwidth_gbps: float = 0.0   # 0 means unknown
```

Populated by collector on startup (chip detection is once-per-boot; no
point re-running every heartbeat).  Sent in the heartbeat payload like
any other hardware field.

### Part 4 — Signal 5 (role affinity) becomes bandwidth-aware

Current:
```python
if node_mem >= 128: return 15.0
elif node_mem >= 32: return 5.0
```

Proposed: use bandwidth as the primary signal, memory as fallback when
bandwidth is unknown:

```python
def _score_role_affinity(self, node, model):
    model_size = self._estimate_model_size(model, node)
    bw = node.hardware.memory_bandwidth_gbps

    if bw > 0:
        # Known bandwidth — scale bonus across a sensible range
        # 100 GB/s (MacBook Air M2)  → 5
        # 300 GB/s (MacBook Pro M3)  → 13
        # 400 GB/s (M3 Max)          → 17
        # 800 GB/s (Studio Ultra)    → 25
        bw_bonus = min(25.0, 5.0 + (bw / 40.0))
    else:
        # Unknown bandwidth — fall back to memory tiers (today's behaviour)
        bw_bonus = 15.0 if node.hardware.memory_total_gb >= 128 else 5.0

    # Scale bonus by model size appetite:
    # Big models benefit disproportionately from bandwidth
    # Small models don't need it — a smaller faster node can still be ideal
    if model_size > self._s.score_role_large_threshold_gb:
        return bw_bonus
    elif model_size < self._s.score_role_small_threshold_gb:
        # Small model — prefer smaller nodes that won't hog big ones
        return max(3.0, 15.0 - bw_bonus * 0.5)
    return bw_bonus * 0.6  # Mid-size: partial bandwidth credit
```

**Result on your fleet for a 20 GB model:**

| Node | bw (GB/s) | Current | Proposed |
|------|-----------|---------|----------|
| Neons-Mac-Studio (M3 Ultra) | 800 | 15 | 25 |
| Lucass-MacBook-Pro-2 (M3 Max) | 400 | 15 | 15 |

Studio wins by +10 points → gets all traffic until its queue fills up.

### Part 5 — Signal 3 (queue depth) becomes capacity-normalized

Current:
```python
penalty = -6.0 * depth, capped at -30
```

Proposed: normalize penalty by a node's relative capacity.  A queue of 4
on a node 4× faster than the fleet baseline should cost the same as a
queue of 1 on a baseline node.

```python
def _score_queue_depth(self, node, depth):
    if depth == 0:
        return 0.0
    # Relative capacity: this node's bandwidth / fleet median
    relative = node.hardware.memory_bandwidth_gbps / self._fleet_median_bandwidth()
    relative = max(0.25, min(4.0, relative))  # clamp to sane range
    effective_depth = depth / relative
    return -min(30.0, effective_depth * 6.0)
```

**Result:** Studio with 4 queued gets treated as if queue=1 → penalty -6
instead of -24.  So routing doesn't prematurely flip to MacBook until
Studio is genuinely backed up.

### Part 6 — Signal 4 (wait time) cold-start fallback

Currently the heuristic when `latency_store` has no data is a generic
"estimate from model size".  Replace with a bandwidth-aware estimate:

```python
def _estimate_eval_speed(self, node, model):
    """Return estimated tokens/sec for prompt eval on this node+model."""
    # Empirical rule of thumb: prompt-eval throughput on Apple Silicon
    # is roughly memory_bandwidth_gbps × 1.2 for Q4 models, bounded by
    # model size.  Actual numbers vary but the ratio holds for routing.
    bw = node.hardware.memory_bandwidth_gbps or 100.0
    model_size_gb = self._estimate_model_size(model, node)
    # Bigger models eat more bandwidth per token
    return max(50.0, bw * 1.2 / max(1.0, model_size_gb / 10))
```

When the latency store does have data, it wins.  This only helps on
cold fleets.

### Part 7 — Config knobs

New settings in `ServerSettings`:

```python
# Bandwidth-aware scoring — set to false to keep pure memory tiers
bandwidth_aware_scoring: bool = True

# Queue penalty normalization.  When true, a queue of N on a 4x faster
# node is treated as N/4 for penalty calculation.  Essentially enables
# proportional load distribution.
queue_penalty_bandwidth_normalize: bool = True
```

Both default on; both can be turned off by ops if they cause unexpected
behaviour.

## Expected steady-state distribution

For a fleet of Studio (800 GB/s) + MacBook (400 GB/s), under sustained load:

```
expected_share[Studio]  = 800 / (800 + 400) = 67%
expected_share[MacBook] = 400 / (800 + 400) = 33%
```

For Studio (800) + MacBook (300) + MacBook Air (100):
```
Studio  = 800 / 1200 = 67%
Pro     = 300 / 1200 = 25%
Air     = 100 / 1200 = 8%
```

This is what "Studio is 4× faster → 75/25 split" cashes out to — except the
real ratio depends on actual bandwidth numbers, not a rule of thumb.

## Testing

- Unit tests for `hardware_lookup.resolve_bandwidth()` — known chips,
  unknown chips, case variations
- Unit tests for new Signal 3/4/5 — synthesize nodes at varying bandwidths
  and confirm scoring flips at the right queue depths
- Integration: add a test that simulates 100 sequential requests against
  a 2-node fleet and asserts the distribution approaches the expected
  ratio within ±5%
- Keep existing tests green — the fallback path must preserve today's
  behaviour when chip is unknown

## Rollout

1. Land detection + lookup first (heartbeat carries bandwidth, but
   scoring still uses memory tiers).  No behaviour change, just data.
2. Verify via dashboard that both nodes report correct chip + bandwidth.
3. Flip `bandwidth_aware_scoring` on in a follow-up commit.
4. Observe trace distribution — expect Studio's share of qwen3-coder
   traffic to climb from ~50% → ~70%.

## Related work landed in parallel

Nothing in the October 2026 MLX / stress-test additions conflicts with this
plan — the overlaps are:

- **MLX queue info** — `/fleet/queue` now merges Ollama queues with MLX
  synthetic queues (`mlx_proxy.get_queue_info()` keyed by the same
  `node_id:model` shape).  Signal 3's normalization operates on those keys
  unchanged, so MLX traffic automatically benefits from bandwidth-aware
  load balancing without extra work.
- **MLX on the same hardware** — `memory_bandwidth_gbps` is a node-level
  attribute.  When a node runs both Ollama and MLX, both backends share
  the same value.  No per-backend bandwidth plumbing needed.
- **`claude-code-stress-test.py`** (added in `docs/experiments/`) supersedes
  the older `scripts/test-claude-code-requests.py` from the debug-logging
  work — the newer one extracts real request shapes from the JSONL log.
  After this plan lands we should delete the duplicate.
- **`claude-code-ollama-ecosystem-2026.md`** explicitly cites MoE active
  parameters vs M-series bandwidth as the primary latency constraint —
  independent confirmation that bandwidth belongs in the scoring layer.

## Files to modify

| File | Change |
|------|--------|
| `src/fleet_manager/server/hardware_lookup.py` | NEW — chip→bandwidth table |
| `src/fleet_manager/node/collector.py` | Detect chip string + populate bandwidth |
| `src/fleet_manager/models/node.py` | Add `memory_bandwidth_gbps` to HardwareProfile |
| `src/fleet_manager/server/scorer.py` | Bandwidth-aware Signals 3, 4, 5 |
| `src/fleet_manager/models/config.py` | New `bandwidth_aware_scoring`, `queue_penalty_bandwidth_normalize` |
| `tests/test_server/test_scorer.py` | Regression tests + new bandwidth tests |
| `tests/test_server/test_hardware_lookup.py` | NEW |
| `docs/configuration-reference.md` | Document new knobs |
