# Routing Engine

How the router makes smart decisions about where to send every request.

## The Five-Stage Pipeline

Every incoming request passes through five stages:

```
Incoming Request
      |
Stage 1: Elimination    -- removes nodes that can't serve
      |
Stage 2: Scoring        -- ranks survivors across 7 signals
      |
Stage 3: Queue          -- highest score wins, request enters queue
      |
Stage 4: Pre-Warm       -- proactively loads model on backup node
      |
Stage 5: Rebalance      -- background process watching all queues
```

The goal of every decision is to minimize **total response time**:

```
total_time = queue_wait_time + cold_load_time + inference_time
```

A request routed to a node with the model already loaded and an empty queue beats a request routed to an idle node that needs to load the model first.

## Stage 1: Elimination

Before any scoring, the router removes nodes that can't serve the request. This is binary — pass or out.

| Condition | Outcome If False |
|-----------|-----------------|
| Node is online and heartbeating within 10s | Eliminated |
| Model is on disk on this node | Eliminated |
| Node has enough free memory to load the model | Eliminated |
| Node is not in hard-pause mode | Eliminated |

**Hard-pause triggers** (regardless of learned baseline):
- Camera or microphone active (meeting in progress)
- Memory pressure state is critical
- Availability score below 0.20
- Manual pause by operator

**If nothing survives:** The request enters a holding queue. The router retries elimination every 5 seconds as node states change. The request waits rather than errors.

## Stage 2: Scoring

Each surviving node gets scored across 7 weighted signals. Higher total wins.

### Signal 1: Model Thermal State (up to +50)

The most important signal. Cold-loading a 40GB model takes 15-30 seconds.

| State | Points |
|-------|--------|
| Model currently loaded in memory (hot) | +50 |
| Model on disk, loaded within last 30 min (likely OS-cached) | +30 |
| Model on disk, not recently used | +10 |
| Model not on disk (requires download) | +0 |

The "recently loaded" tier exists because macOS aggressively caches recently evicted memory pages — a model unloaded 20 minutes ago often reloads much faster.

### Signal 2: Memory Fit (up to +20)

How comfortably the model fits given current utilization and the node's dynamic memory ceiling.

```
fit_ratio = (ceiling_gb - ollama_used_gb) / model_size_gb

fit_ratio > 2.0     +20   comfortable headroom
fit_ratio 1.5-2.0   +15
fit_ratio 1.2-1.5   +8
fit_ratio 1.0-1.2   +3    tight -- risk of memory pressure
fit_ratio < 1.0     eliminated in Stage 1
```

The ceiling isn't total RAM — it's the adaptive ceiling from the capacity learner. A laptop in low-capacity mode might have a 20GB ceiling even with 128GB total.

### Signal 3: Queue Depth Penalty (up to -30)

A hot model on a saturated node is less attractive than a warm model on an empty node.

```
depth = in_flight + pending
penalty = min(30, depth x 6)
```

A queue of 5 subtracts the maximum 30 points. This naturally spreads load when multiple nodes can serve the same model.

### Signal 4: Estimated Wait Time Penalty (up to -25)

Queue depth alone is misleading. A queue of 3 on a fast 7B model completes in seconds. A queue of 3 on a slow 70B model takes minutes.

The router uses **p75 historical latency** per node per model:

```
est_wait = (in_flight + pending) x p75_latency_ms
penalty = min(25, est_wait_seconds / 10)
```

During the first 7 days before enough data is collected, the router uses a heuristic based on memory bandwidth and model size.

### Signal 5: Role Affinity (up to +15)

Large models belong on powerful machines. Small models should run on lighter hardware to preserve big-machine capacity.

```
Model > 30B parameters:
  Mac Studio    +15
  MacBook Pro   +5
  MacBook Air   +0

Model < 10B parameters:
  MacBook Air   +15   (preserve Mac Studio for large models)
  MacBook Pro   +8
  Mac Studio    +3
```

Without affinity, every small-model request would drift to the most powerful machine, crowding out the large models it's uniquely suited for.

### Signal 6: Availability Trend (up to +10)

Is this device freeing up or getting busier right now?

```
Rising availability    +10
Stable                 +5
Falling availability   +0
```

This prevents sending a long inference request to a machine whose owner just sat down to start working.

### Signal 7: Context Fit (up to +10)

Rewards nodes whose loaded context window comfortably handles the estimated request size. Penalizes nodes where the request might trigger a context resize (which causes a model reload).

## Stage 3: Queue and Execute

The highest-scoring node wins. The request enters that node's dedicated queue for that model. Each node+model pair has its own queue with dynamic concurrency calculated from available memory.

## Stage 4: Pre-Warm

After every routing decision, the router checks: is the winner's queue getting deep?

If the queue exceeds the pre-warm threshold (default: 3), the router loads the same model on the runner-up node by sending an empty generate request. By the time overflow requests arrive, the model is already hot.

A lock prevents duplicate pre-warm requests for the same model on the same node.

## Stage 5: Rebalance

A background process runs every 5 seconds:

1. Scans all queues for nodes with depth above the rebalance threshold
2. For each overloaded queue, checks if another node has the same model hot with spare capacity
3. Moves pending requests (not in-flight) to the better node
4. Caps movement at 3 requests per cycle to prevent oscillation

The rebalancer only moves requests to nodes where the model is already loaded — it never triggers cold loads.

## Fallback Chain

When the primary model has no viable node (all eliminated or all exhausted), the router tries fallback models in order:

1. Score all nodes for primary model → all eliminated
2. Wait in holding queue for up to 30 seconds, retrying every 2 seconds
3. If still nothing, try first fallback model → score all nodes
4. Continue through fallback list
5. If all models exhausted, return 503

Each fallback goes through the full elimination + scoring pipeline.

## Configuration

All scoring weights are tunable via environment variables:

| Variable | Default | Effect |
|----------|---------|--------|
| `FLEET_SCORE_MODEL_HOT` | 50 | Increase to prefer hot models more aggressively |
| `FLEET_SCORE_QUEUE_DEPTH_PENALTY_PER` | 6 | Decrease to tolerate deeper queues |
| `FLEET_SCORE_ROLE_AFFINITY_MAX` | 15 | Increase to enforce "big models on big machines" |
| `FLEET_PRE_WARM_THRESHOLD` | 3 | Lower to pre-warm earlier |
| `FLEET_REBALANCE_THRESHOLD` | 4 | Lower to rebalance more aggressively |

See the [full configuration reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) for all 44+ variables.

## Next Steps

- **[Adaptive Capacity](adaptive-capacity.md)** — How the capacity learner feeds into Signal 2 and Signal 6
- **[Deployment](deployment.md)** — Monitoring and tuning in production
- **[API Reference](api-reference.md)** — Response headers that show routing decisions
