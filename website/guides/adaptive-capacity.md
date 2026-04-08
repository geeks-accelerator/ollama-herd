# Adaptive Capacity

How your fleet learns when each device has spare compute — and adjusts routing automatically.

## The Problem

Your laptop isn't a server. It has an owner who uses it for meetings, coding, video editing, and browsing. Routing inference to a machine during a video call makes both the call and the inference terrible.

The adaptive capacity system watches usage patterns over time, builds a behavioral model, and tells the router when each device has spare capacity — without reading application names or invading privacy.

## Three Components

| Component | What It Does | Platform |
|-----------|-------------|----------|
| **Capacity Learner** | Builds a weekly behavioral model of each device | All platforms |
| **Meeting Detector** | Detects active cameras/microphones for hard-pause | macOS only |
| **App Fingerprinter** | Classifies workload intensity from system metrics | All platforms |

## Capacity Learner

### The Weekly Model

The learner maintains **168 slots** — one for each hour of the week (Monday 00:00 through Sunday 23:00). Each slot accumulates observations of CPU and memory usage, weighted with exponential decay so recent behavior matters more.

```
Monday 9am:   65% CPU, 78% memory  --> low availability
Monday 2am:    3% CPU, 45% memory  --> high availability
Saturday 3pm: 25% CPU, 55% memory  --> moderate availability
```

### Availability Score

After a 7-day bootstrap period, the learner combines three signals into a real-time score (0.0 to 1.0):

| Signal | Weight | What |
|--------|--------|------|
| Historical baseline | 40% | What you usually do at this hour of the week |
| Current observed state | 40% | What's happening right now |
| CPU trend | 20% | Is activity rising or falling over the last 5 minutes |

### Score to Memory Ceiling

The availability score maps to how much memory the router can use on this device:

| Score | Mode | Memory Ceiling | What It Means |
|-------|------|---------------|---------------|
| 0.80-1.00 | Full | 80% of total RAM | Full fleet participant |
| 0.60-0.80 | Learned high | 50% (max 64GB) | Normal priority |
| 0.40-0.60 | Learned medium | 25% (max 32GB) | Small models only |
| 0.20-0.40 | Learned low | 12.5% (max 16GB) | Minimal, lightweight only |
| 0.00-0.20 | Paused | 0GB | No routing |

The router reads `ceiling_gb` from each heartbeat and uses it for scoring (memory fit) and elimination (can the model fit?).

### Bootstrap Period

The first 7 days are observation-only:
- The learner records patterns but doesn't contribute capacity
- The device doesn't participate in fleet routing
- After 7 days, scores activate and the device joins the fleet

### Exponential Decay

Observations use a **15-day half-life**. An observation from 15 days ago has half the weight of today's. This means:
- The model adapts to schedule changes (new job, vacation)
- Seasonal patterns naturally fade
- The model stays responsive to recent shifts

### Persistence

State saves to disk every ~5 minutes at `~/.fleet-manager/capacity-learner-{node-id}.json`. On restart, learned state is restored. The bootstrap countdown continues from the first-ever observation.

## Meeting Detection (macOS)

When a camera or microphone is active, the node is **hard-paused** — availability score drops to 0.0, memory ceiling drops to 0GB, no requests route to it.

### How It Detects

**Camera** (three methods, tried in order):
1. macOS unified logs for CoreMediaIO extension events
2. `lsof` check for open handles in `/Library/CoreMediaIO/`
3. Process check for `VDCAssistant` or `AppleCameraAssistant`

**Microphone** (two methods):
1. `ioreg` query for IOAudioEngine active state
2. `lsof` scan for audio input device handles

The node resumes automatically when the meeting ends. On Linux and Windows, meeting detection returns `false` gracefully — no pause, no error.

### Why It Matters

A video call uses sustained CPU, memory, and network bandwidth. Running a 40GB model during a Zoom call degrades both. Hard-pausing on meeting detection is the difference between a system you leave running and one you constantly babysit.

## App Fingerprinting

### Privacy-First Design

The fingerprinter **never reads application names, window titles, or user content**. It observes only system-level resource consumption:

| Metric | Source |
|--------|--------|
| CPU percent | `psutil.cpu_percent()` |
| Memory percent | `psutil.virtual_memory().percent` |
| Network bytes (delta) | `psutil.net_io_counters()` |
| Disk I/O (delta) | `psutil.disk_io_counters()` |

Snapshots are kept in a 2-minute sliding window (24 samples at 5-second intervals).

### Workload Classification

| Workload | Signature |
|----------|-----------|
| **Idle** | CPU <10%, memory <70% |
| **Light** | CPU 10-35% or memory >70% |
| **Moderate** | CPU 35-60% |
| **Heavy** | CPU 60-85% |
| **Intensive** | CPU >85%, or CPU >60% with >500KB/s sustained network |

The "intensive with high network" pattern catches video calls specifically — they have a distinctive signature of sustained CPU plus high bidirectional network traffic.

### CPU Trend

The fingerprinter computes a trend (-1.0 to +1.0) by comparing the first and second half of recent snapshots:

- **Negative** (falling CPU) — user wrapping up work, availability rising
- **Positive** (rising CPU) — user starting work, availability falling
- **Zero** — stable

This trend feeds into the capacity learner as the 20%-weighted trend signal.

## How It Fits Together

Every 5 seconds (heartbeat interval):

```
1. App Fingerprinter collects snapshot     -- CPU, mem, net, disk
2. Meeting Detector checks camera + mic    -- boolean
3. Capacity Learner computes availability  -- 0.0 to 1.0
   |-- If meeting detected         --> hard pause (0GB ceiling)
   |-- If sustained high CPU       --> reduced ceiling (16GB)
   |-- If bootstrapping            --> no capacity contributed
   |-- Otherwise                   --> learned score + ceiling
4. Capacity info included in heartbeat     -- sent to router
5. Router's scoring engine uses ceiling    -- respects dynamic capacity
```

The router never sends requests that would exceed the memory ceiling. If a MacBook's ceiling drops from 64GB to 0GB because a meeting started, the router immediately stops routing there and rebalances pending requests.

## Enabling Capacity Learning

Capacity learning is **opt-in**. Dedicated servers should leave it disabled — they always run at full capacity.

```bash
# On a laptop that's also used for daily work
FLEET_NODE_ENABLE_CAPACITY_LEARNING=true herd-node
```

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_ENABLE_CAPACITY_LEARNING` | `false` | Enable the adaptive system |
| `FLEET_NODE_DATA_DIR` | `~/.fleet-manager` | Where learner state is persisted |

## Next Steps

- **[Routing Engine](routing-engine.md)** — How capacity feeds into scoring signals 2 and 6
- **[Deployment](deployment.md)** — Which devices should enable capacity learning
- **[Core Concepts](concepts.md)** — Overview of all fleet mechanics
