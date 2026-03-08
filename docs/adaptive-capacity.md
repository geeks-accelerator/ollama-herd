# Adaptive Capacity Learning

How Ollama Herd learns when your devices have spare compute capacity and dynamically adjusts routing.

---

## Overview

The adaptive capacity system solves a core problem: your laptop isn't a server. It has an owner who uses it for meetings, coding, video editing, and web browsing. The capacity learner watches usage patterns over time, builds a behavioral model, and tells the router when each device has spare capacity for inference — without ever reading application names or invading privacy.

Three components work together:

- **Capacity Learner** — 168-slot behavioral model (7 days × 24 hours) that learns your weekly rhythm
- **Meeting Detector** — macOS-specific camera/microphone detection for hard-pause during calls
- **App Fingerprinter** — resource signature classifier that identifies workload intensity without reading app names

---

## Capacity Learner

### The Behavioral Model

The learner maintains 168 slots — one for each hour of the week (Monday 00:00 through Sunday 23:00). Each slot accumulates observations of CPU and memory usage at that time, weighted with exponential decay so recent behavior matters more than old patterns.

```
Monday 9am:  "Usually 65% CPU, 78% memory"  → low availability
Monday 2am:  "Usually 3% CPU, 45% memory"   → high availability
Saturday 3pm: "Usually 25% CPU, 55% memory" → moderate availability
```

After the 7-day bootstrap period, the learner combines three signals to compute a real-time availability score (0.0 to 1.0):

| Signal | Weight | Description |
|--------|--------|-------------|
| Historical baseline | 40% | What you usually do at this hour of the week |
| Current observed state | 40% | What's happening right now (CPU + memory) |
| CPU trend | 20% | Is activity rising or falling over the last 5 minutes |

When the slot has insufficient historical data, the current state gets more weight (up to 80%).

### Availability Score → Memory Ceiling

The availability score maps directly to how much memory the router is allowed to use for Ollama on this device:

| Score Range | Mode | Memory Ceiling | Behavior |
|-------------|------|----------------|----------|
| 0.80–1.00 | `full` | 80% of total RAM | Full participant in the fleet |
| 0.60–0.80 | `learned_high` | 50% of total (max 64GB) | Normal priority routing |
| 0.40–0.60 | `learned_medium` | 25% of total (max 32GB) | Low priority, small models only |
| 0.20–0.40 | `learned_low` | 12.5% of total (max 16GB) | Minimal, lightweight models only |
| 0.00–0.20 | `paused` | 0GB | No routing to this device |

### Hard Override Signals

These bypass the learned model entirely, regardless of what the historical baseline predicts:

| Signal | Effect | Details |
|--------|--------|---------|
| Camera or mic active | Hard pause (0GB) | Meeting detected — no inference |
| CPU >85% for 2+ minutes | Ceiling drops to 16GB | Sustained heavy workload |
| Manual override: `paused` | Hard pause (0GB) | User explicitly paused the node |
| Manual override: `full` | 80% ceiling | User explicitly set full capacity |

### Bootstrap Period

The first 7 days are observation-only. During bootstrap:

- The learner records all observations but doesn't contribute capacity
- The device does not participate in fleet routing
- Availability score is 0.0, mode is `bootstrap`
- After 7 days, the learner starts providing availability scores

### Learning Confidence

The system reports a confidence score (0.0 to 1.0) based on:

- **Days observed** (60% weight): reaches maximum at 30 days
- **Sample density** (40% weight): average observations per slot reaching 5+

This lets operators know how much to trust the learned predictions. A confidence of 0.8+ means the model has seen enough data to make reliable predictions.

### Exponential Decay

Observations use a 15-day half-life exponential decay. An observation from 15 days ago has half the weight of today's observation. This means:

- The model adapts to lifestyle changes (new job schedule, vacation)
- Seasonal patterns naturally fade
- The model stays responsive to recent behavior shifts

### Persistence

The learner saves state to disk every ~5 minutes at `~/.fleet-manager/capacity-learner-{node-id}.json`. On node restart:

- Learned state is restored from disk
- Unexpired manual overrides are reapplied
- The bootstrap period counts from the first-ever observation

On graceful shutdown (SIGTERM/SIGINT), the node agent saves capacity learner state before sending the drain signal.

---

## Meeting Detection

### How It Works

The `MeetingDetector` checks whether the camera or microphone is currently in use on macOS. If either is active, the node is hard-paused — no new inference requests are routed to it.

### Camera Detection (3 methods, in order)

1. **System log analysis** — Queries macOS unified logs for CoreMediaIO extension provider events in the last 5 seconds. If `startstream` appears without `stopstream`, the camera is active.

2. **CoreMediaIO library check** — Uses `lsof` to check for open file handles in `/Library/CoreMediaIO/`, which indicates active camera usage by any application.

3. **Process check** — Looks for `VDCAssistant` or `AppleCameraAssistant` processes, which macOS spawns when the camera is in use.

### Microphone Detection (2 methods, in order)

1. **IOAudioEngine state** — Queries `ioreg` for audio engine entries with active state (`IOAudioEngineState = 1`), which indicates an active audio input stream.

2. **Device file check** — Uses `lsof` to scan `/dev/` for audio input device handles.

### Platform Support

- **macOS**: Full support with multiple fallback detection methods
- **Linux/Windows**: Returns `False` gracefully — no meeting detection on non-Darwin systems

### Why It Matters

A video call uses sustained CPU, memory, and network bandwidth. Running a 40GB model inference during a Zoom call would make both the call and the inference terrible. Hard-pausing on meeting detection prevents this entirely.

---

## Application Fingerprinting

### Privacy-First Design

The fingerprinter never reads application names, window titles, or any user content. It observes only system-level resource consumption patterns — CPU, memory, network I/O, and disk I/O — and classifies the aggregate signature into a workload type.

### Resource Snapshots

Every heartbeat interval (typically 5 seconds), the fingerprinter collects:

| Metric | Source |
|--------|--------|
| CPU percent | `psutil.cpu_percent()` |
| Memory percent | `psutil.virtual_memory().percent` |
| Network bytes sent/recv (delta) | `psutil.net_io_counters()` |
| Disk I/O read/write (delta) | `psutil.disk_io_counters()` |

Snapshots are kept in a 2-minute sliding window (24 samples at 5-second intervals).

### Workload Classification

The classifier analyzes average resource usage across the window:

| Workload Type | Signature |
|---------------|-----------|
| `idle` | CPU <10%, memory <70% |
| `light` | CPU 10–35% or memory >70% |
| `moderate` | CPU 35–60% |
| `heavy` | CPU 60–85% |
| `intensive` | CPU >85%, or CPU >60% with >500KB/s sustained network |

The `intensive` classification with high network I/O catches video calls specifically — they have a distinctive signature of sustained CPU usage combined with high bidirectional network traffic.

### CPU Trend Signal

The fingerprinter computes a trend value (-1.0 to +1.0) over the last 5 minutes by comparing the first half and second half of recent snapshots:

```
trend = (second_half_avg_cpu - first_half_avg_cpu) / 50.0
clamped to [-1.0, +1.0]
```

- **Negative trend** (falling CPU) → user is wrapping up work → availability rising
- **Positive trend** (rising CPU) → user is starting work → availability falling
- **Zero** → stable state

This trend feeds into the capacity learner's availability score as the 20%-weighted trend signal.

### Dashboard Heatmap

The capacity learner exposes a `get_heatmap_data()` method that returns all 168 weekly slots with historical averages. This data powers a visual heatmap showing when the device is typically busy vs. available — useful for operators to understand fleet utilization patterns.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_ENABLE_CAPACITY_LEARNING` | `false` | Enable the adaptive capacity system |
| `FLEET_NODE_DATA_DIR` | `~/.fleet-manager` | Where learner state files are persisted |

The capacity learner is opt-in. Devices intended as dedicated inference servers (like a Mac Studio) should leave it disabled — they always run at full capacity.

### Enabling Capacity Learning

```bash
# On a laptop that's also used for daily work
FLEET_NODE_ENABLE_CAPACITY_LEARNING=true herd-node
```

On startup, the node agent creates the learner and begins the 7-day bootstrap period. No capacity is contributed during bootstrap — the device observes only.

---

## How It All Fits Together

```
Every 5 seconds (heartbeat interval):
  1. AppFingerprinter.collect_snapshot()     → records CPU, mem, net, disk
  2. MeetingDetector.is_in_meeting()         → checks camera + mic
  3. CapacityLearner.observe(cpu, memory)    → computes availability score
     ├── If meeting detected          → hard pause (0GB ceiling)
     ├── If sustained high CPU        → reduced ceiling (16GB)
     ├── If bootstrapping             → no capacity contributed
     └── Otherwise                    → learned score + ceiling
  4. CapacityInfo included in heartbeat      → sent to router
  5. Router's ScoringEngine uses ceiling     → respects dynamic capacity
```

The router never sends requests that would exceed the memory ceiling. If the MacBook's ceiling drops from 64GB to 0GB because a meeting started, the router immediately stops routing new requests to it and rebalances pending requests to other nodes.

---

## Heartbeat Payload

The capacity learner's output is included in the node agent's heartbeat as a `capacity` field:

```json
{
  "node_id": "macbook-pro-14",
  "capacity": {
    "mode": "learned_high",
    "ceiling_gb": 64.0,
    "availability_score": 0.723,
    "reason": "good_availability",
    "override_active": false,
    "learning_confidence": 0.85,
    "days_observed": 21
  }
}
```

The router reads `ceiling_gb` and uses it as the effective memory ceiling for scoring Signal 2 (Memory Fit) and for elimination in Stage 1.
