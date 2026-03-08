# Fleet Manager — Routing Decision Engine
### How the router makes smart decisions about where to queue requests

---

## Overview

The routing engine is the brain of Fleet Manager. Every incoming request passes through a five-stage pipeline that eliminates unsuitable candidates, scores the survivors across six weighted signals, selects a winner, and then triggers background processes to keep the fleet ahead of future demand.

The goal of every routing decision is to minimize **total response time**:

```
total_time = queue_wait_time + cold_load_time + inference_time
```

A request routed to a node with the model already loaded and an empty queue will always beat a request routed to an idle node that needs to load the model first. The scoring system encodes this reality precisely.

---

## The Five Stages

```
Incoming Request
      ↓
Stage 1: Hard Elimination    → removes nodes that physically cannot serve
      ↓
Stage 2: Scoring             → ranks survivors across 6 weighted signals
      ↓
Stage 3: Final Decision      → highest score wins, request enters queue
      ↓
Stage 4: Pre-Warm Trigger    → proactively loads model on runner-up if needed
      ↓
Stage 5: Rebalancer          → continuous background process watching all queues
```

---

## Stage 1 — Hard Elimination

Before any scoring, the router eliminates candidates that simply cannot serve the request. This pass is binary — a node either passes or is out.

```
Condition                                          Outcome if false
─────────────────────────────────────────────────────────────────────
Node is online and heartbeating within 10s       → eliminated
Model is on disk on this node                    → eliminated
Node has enough free memory to load the model    → eliminated
Node is not in hard-pause mode                   → eliminated
```

**Hard-pause triggers (regardless of learned baseline):**
- Camera or microphone currently active (meeting in progress)
- macOS memory pressure state is `critical`
- MacBook availability score is below 0.20
- Node has not heartbeated within the last 10 seconds

**What happens if nothing survives elimination:**
The request enters a **holding queue** rather than failing. The router retries elimination every 5 seconds as node states change. This matters for edge cases like a single-device fleet where the one available node is momentarily at capacity — the request waits rather than errors.

---

## Stage 2 — Scoring

Each surviving candidate receives a score composed of six weighted signals. Higher total score wins. The signals are designed to be additive and independent — each captures a distinct dimension of routing quality.

---

### Signal 1 — Model Thermal State
**Weight: 0 to +50 points**

The single most important signal. Cold-loading a 40GB model takes 15–30 seconds. The scoring strongly rewards nodes where the model is already in memory.

```
Model currently loaded in memory (hot)           +50
Model on disk, loaded within last 30 minutes     +30  (likely OS-cached)
Model on disk, not recently used                 +10
Model not on disk (requires download)             +0  (and alert user)
```

The "recently loaded" tier exists because macOS aggressively caches recently evicted memory pages. A model unloaded 20 minutes ago often reloads significantly faster than one that's been cold for hours.

---

### Signal 2 — Effective Memory Fit
**Weight: 0 to +20 points**

Not raw available memory — but how comfortably the model fits given current utilization and the node's dynamic memory ceiling. A tight fit risks pushing the node into memory pressure, degrading the entire machine.

```
fit_ratio = (node.ceiling_gb - node.ollama_used_gb) / model.size_gb

fit_ratio > 2.0      +20   (model fits with comfortable headroom)
fit_ratio 1.5–2.0    +15
fit_ratio 1.2–1.5    +8
fit_ratio 1.0–1.2    +3    (tight — risk of triggering memory pressure)
fit_ratio < 1.0      eliminated in Stage 1
```

`node.ceiling_gb` is the adaptive ceiling, not total RAM. For the work MacBook in low-capacity mode, this ceiling may be 20GB even though the machine has 128GB total — the scoring respects whatever capacity mode the node is currently in.

---

### Signal 3 — Queue Depth Penalty
**Weight: 0 to −30 points**

A hot model on a saturated node is less attractive than a warm model on an empty node. This penalty ensures load balancing across nodes that can both serve a request.

```
depth = queue.in_flight_count + queue.pending_count
penalty = min(30, depth × 6)
```

A queue of depth 5 subtracts the maximum 30 points, making it unattractive even if the model is hot. This naturally spreads load across the fleet when multiple nodes can serve the same model.

---

### Signal 4 — Estimated Wait Time Penalty
**Weight: 0 to −25 points**

Queue depth alone is misleading. A queue of 3 requests on `phi4:14b` completes in under a minute. A queue of 3 requests on `deepseek-r1:671b` could take 20 minutes. The router uses observed per-model, per-node inference latency to estimate actual wait time.

```
avg_ms    = historical_latency_table[model][node].p75
est_wait  = (in_flight_count + pending_count) × avg_ms
penalty   = min(25, est_wait_seconds / 10)
```

The p75 latency (75th percentile) is used as the planning estimate — pessimistic enough to avoid over-routing to a busy node, optimistic enough not to under-utilize capacity.

**Cold start bootstrap:** In the first 7 days before sufficient latency data is collected, the router falls back to a heuristic:

```
estimated_tokens_per_second = node.memory_bandwidth_gb_s / model.size_gb × 0.85
heuristic_ms_per_request    = expected_output_tokens / estimated_tokens_per_second × 1000
```

---

### Signal 5 — Node Role Affinity
**Weight: 0 to +15 points**

The fleet has natural structural roles. Large models belong on the Mac Studio. Small fast models should run on the old MacBook to preserve the Mac Studio's capacity for what it's uniquely suited for. Affinity scores encode this without hard-wiring it — the scoring system can be overridden by the other signals when circumstances warrant.

```
Model size > 30B parameters:
  Mac Studio              +15
  New MacBook             +5
  Old MacBook             +0  (eliminated in Stage 1 anyway — won't fit)

Model size < 10B parameters:
  Old MacBook             +15  (preserve Mac Studio for large models)
  New MacBook             +8
  Mac Studio              +3

Embedding request (any model):
  Node with model hot     +15  (embeddings must never block large model capacity)
  Any other node          +0
```

The small-model affinity toward the old MacBook is particularly important. Without it, every `qwen2.5:7b` request would drift toward the Mac Studio (highest score on other signals), starving the old MacBook of work and eventually crowding the Mac Studio's large model capacity.

---

### Signal 6 — Availability Trend
**Weight: 0 to +10 points**
*Applies to work MacBook only*

The MacBook's availability score can change rapidly. A rising score means the machine is freeing up — safe to route new work. A falling score means the owner is actively starting work — avoid adding long-running requests that will still be running when the machine is needed.

```
MacBook availability trend (last 5 minutes):
  Rising  (+)             +10
  Stable  (±)             +5
  Falling (−)             +0
```

This prevents the pathological case where the router sends a 3-minute inference request to the MacBook at the exact moment the owner sits down to start a video call.

---

## Stage 3 — Final Decision

```
total_score = signal_1 + signal_2 − signal_3 − signal_4 + signal_5 + signal_6
```

The highest-scoring candidate wins. The request is atomically added to that node's model queue.

### Example Scored Decision

**Request:** `llama3.3:70b`, standard prompt, normal priority

```
┌─────────────────────────────────────────────┬────────┬────────┐
│ Signal                                      │ Studio │ MBP    │
├─────────────────────────────────────────────┼────────┼────────┤
│ S1  Model hot (Studio) / cold on disk (MBP) │  +50   │  +10   │
│ S2  Memory fit ratio 3.1 / 1.8              │  +20   │  +15   │
│ S3  Queue depth 4 / 0                       │  -24   │   0    │
│ S4  Est. wait 48s / 0s                      │  -12   │   0    │
│ S5  Role affinity (large model)             │  +15   │   +5   │
│ S6  Availability trend (MBP only)           │   —    │  +10   │
├─────────────────────────────────────────────┼────────┼────────┤
│ Total Score                                 │   49   │   40   │
└─────────────────────────────────────────────┴────────┴────────┘

→ Route to Mac Studio (49 > 40)
→ Trigger pre-warm: macbook-new:llama3.3:70b
  (Studio queue growing, MacBook available, model fits comfortably)
```

The Mac Studio wins despite its busy queue because the model is already hot and it has the role affinity advantage. But the gap is only 9 points — small enough that the pre-warm trigger fires to close that gap for the next request.

---

### Score Dynamics Over Time

As the Mac Studio queue grows, the routing decision will eventually flip:

```
Studio queue depth 0  → Score: 73  (wins easily)
Studio queue depth 3  → Score: 55  (still wins)
Studio queue depth 5  → Score: 43  (wins by 3 — marginal)
Studio queue depth 6  → Score: 37  (loses — route to MacBook)
```

By the time the router is sending requests to the MacBook, the pre-warm triggered 2–3 requests earlier means the model is already loaded. The handoff is seamless.

---

## Stage 4 — Pre-Warm Trigger

After every routing decision, the router evaluates whether to proactively load the model on the runner-up node.

```
if winner.queue_depth >= PRE_WARM_THRESHOLD (default: 3):
  if runner_up.model_is_cold:
    if runner_up.availability_score >= 0.60:
      if runner_up.model_fits_comfortably:
        if not pre_warm_lock[runner_up.node][model]:
          → send pre_warm signal to runner_up
          → set pre_warm_lock[runner_up.node][model] = true
          → lock expires when model reports as loaded
```

**Why the pre-warm lock matters:**
If 10 requests arrive in a burst and all of them trigger pre-warm evaluation, only the first should send a load signal. The lock prevents the node from receiving duplicate load requests for the same model while it's already loading.

**Pre-warm and the MacBook:**
Pre-warm signals respect the availability ceiling. If the MacBook is in low-capacity mode (ceiling 20GB) and `llama3.3:70b` is 40GB, the pre-warm is suppressed even if the Mac Studio queue is deep. The router will not ask the MacBook to load something it can't currently hold.

---

## Stage 5 — Rebalancing (Continuous Background Process)

The rebalancer runs every 5 seconds, independent of incoming requests. It watches all queues and moves pending requests when imbalances develop.

```
for each queue:
  if queue.pending_depth > REBALANCE_THRESHOLD (default: 4):

    candidates = all_nodes
      .filter(can_serve_model(queue.model))
      .filter(model_is_hot OR model_fits_comfortably)
      .filter(availability_score > 0.40)
      .filter(not_in_hard_pause)
      .sort_by(score)

    if candidates.any():
      requests_to_move = min(queue.pending_depth / 2, 3)
      move requests_to_move pending requests → best candidate queue
```

**Critical invariant:** Only pending requests are moved. In-flight requests complete on the node where they started. A request that has begun generating tokens will never be interrupted or re-routed.

**The move is atomic:** A request is removed from the source queue and added to the destination queue as a single operation before either node is notified. There is no state where the request exists in both queues or neither.

---

## The Latency Table

The router maintains a per-node, per-model latency table that grows richer over time:

```
latency_table[node_id][model_name] = {
  p50_ms:        4200,
  p75_ms:        6800,    ← used for planning
  p95_ms:        14200,
  p99_ms:        31000,
  sample_count:  847,
  last_updated:  1710000000
}
```

This table is updated after every completed request. It's persisted to disk so the router retains learned latency data across restarts.

Over time this table enables increasingly accurate wait-time estimation, which makes the scoring increasingly precise. A fleet that's been running for a month makes dramatically better routing decisions than one that's been running for a day.

---

## Hard Edge Cases and How They're Handled

### Rapid MacBook availability drop
The MacBook availability score can drop from 0.75 to 0.10 in under 30 seconds when a meeting starts. Any pending requests in the MacBook's queues need to move immediately — not on the next rebalancer cycle.

The node agent emits a **capacity-change event** (distinct from the regular heartbeat) whenever the availability score changes by more than 0.20 in a single interval. The router handles this event synchronously, immediately running rebalancing on all affected queues.

### Cold-load surge
If 10 requests arrive simultaneously for a model that's cold on all nodes, the router must not send 10 simultaneous load signals to the same node. The pre-warm lock handles this: only the first routing decision triggers the load signal. Subsequent requests queue behind the first, and by the time they're processed, the model is hot.

### Model not available anywhere
If the requested model isn't on disk on any node, the router does not silently fail or hang. It returns an explicit error response indicating which nodes were checked and that the model isn't available, along with the `ollama pull` command needed to make it available.

### All nodes saturated
If every candidate node has a queue depth above the rebalance threshold and no node has spare capacity, new requests enter the **holding queue** with a timestamp. The router surfaces this state in the dashboard as a fleet saturation warning. When any node's queue clears, the holding queue drains in FIFO order.

### Latency table missing (first week)
Before sufficient latency data exists for a node+model pair, Signal 4 uses the heuristic estimate. The router logs which estimates are heuristic vs data-driven and displays this in the dashboard so the operator knows when the scoring is fully calibrated.

---

## Scoring Configuration

All thresholds and weights are configurable in `fleet-manager.yaml`. The defaults are tuned for a fleet of 2–5 Apple Silicon devices. Larger fleets or specific workloads may benefit from tuning.

```yaml
scoring:
  pre_warm_threshold: 3          # queue depth that triggers pre-warm
  rebalance_threshold: 4         # queue depth that triggers rebalancing
  max_rebalance_per_cycle: 3     # max requests moved per rebalancer run
  availability_floor: 0.20       # below this, MacBook is hard-paused
  pre_warm_min_availability: 0.60 # below this, suppress pre-warm signals
  latency_percentile: 75         # which percentile to use for wait estimation

weights:
  model_thermal:    50           # max points for Signal 1
  memory_fit:       20           # max points for Signal 2
  queue_depth:      30           # max penalty for Signal 3
  wait_time:        25           # max penalty for Signal 4
  role_affinity:    15           # max points for Signal 5
  availability_trend: 10         # max points for Signal 6
```

---

## Summary

The routing engine is designed around one core insight: **the most important factor in response time is whether the model is already loaded**. Everything else — queue depth, memory headroom, role affinity, availability trends — is a refinement on top of that foundation.

The five-stage pipeline ensures that:
1. Only physically capable nodes are considered
2. The best candidate is selected based on a rich, data-driven score
3. The pre-warm system keeps the fleet one step ahead of demand
4. The rebalancer continuously corrects imbalances without interrupting in-flight work
5. The latency table gets more accurate over time, making every subsequent decision better than the last

The result is a routing system that feels intelligent rather than mechanical — one that learns the fleet's behavior, anticipates demand, and routes requests to where they'll be served fastest, while never hijacking a device its owner is actively using.
