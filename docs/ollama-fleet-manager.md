# Ollama Fleet Manager
### A distributed local LLM inference system for personal device fleets

---

## Vision

Most people running local LLMs via Ollama are leaving compute on the table. An older MacBook sitting on a shelf, a laptop that's on but idle, a Mac Studio that's powerful enough to run the heavy models — together these form a personal inference cluster that nobody is currently orchestrating.

**Ollama Fleet Manager** is a smart routing and orchestration layer that turns a collection of personal Apple Silicon devices into a unified, private, zero-cost LLM inference fleet. Point any AI agent framework, app, or script at a single endpoint and the system transparently distributes requests across your devices based on what models are loaded, what hardware is available, and how busy each machine is.

No cloud costs. No single-device bottleneck. No wasted hardware.

---

## The Problem This Solves

### Cloud costs are a barrier to capable AI agents
AI agent frameworks (CrewAI, AutoGPT, LangChain, custom pipelines) make a large number of LLM calls — planning, reflection, tool use, summarization, critique. A single agentic task can burn through dozens of API calls. Complex multi-agent pipelines can cost dollars per run. This forces users to either pay heavily or artificially throttle their agents to be less capable.

### Single-device local inference creates a new bottleneck
Running locally via Ollama solves the cost problem but introduces a new constraint: one device, one model loaded at a time. A multi-agent pipeline where three agents need to call different models simultaneously just queues up on one machine. Bigger, smarter models that would improve agent quality often can't fit alongside the other models the pipeline needs.

### Spare devices go unused
Many people have older laptops, secondary machines, or underutilized hardware that is perfectly capable of running quantized smaller models. A 2020 MacBook Air with 8GB of unified memory can comfortably run `phi3:mini` or a 4-bit quantized 7B model. Currently that compute sits idle.

---

## Architecture Overview

The system has four main layers:

```
Clients (agent frameworks, apps, scripts)
        ↓
Custom API (drop-in OpenAI + Ollama compatible)
        ↓
Router + Scoring Engine + Queue Manager
        ↓
Node Agents → Ollama instances on each device
```

### Components

#### 1. Custom API (Mac Studio)
- Exposes a unified endpoint that is drop-in compatible with both the **OpenAI API** and the **Ollama API**
- Any agent framework (LangChain, CrewAI, etc.) can point at it with zero code changes
- Accepts requests specifying a model, messages, and parameters
- Returns streamed responses transparently

#### 2. Smart Router
- Receives incoming requests and selects the optimal device+model queue
- Delegates scoring to the Scoring Engine
- Hands off to the Queue Manager once a target is selected

#### 3. Scoring Engine
- Ranks all candidate nodes for a given request using weighted signals:
  - **Model already loaded (hot)** → strong bonus — avoids cold start latency
  - **Model on disk but not loaded** → smaller bonus — load time penalty applies
  - **Active request count** → penalty scaled by in-flight requests
  - **Memory availability** → disqualifies nodes where model won't fit
  - **Memory pressure state** → deprioritizes nodes under macOS memory pressure
  - **CPU utilization** → secondary tiebreaker

#### 4. Queue Manager
- Maintains a queue per **device + model pair** (e.g. `macstudio:llama3:70b`, `laptop-a:mistral:7b`)
- Requests wait in their assigned queue until the device is ready
- Tracks in-flight requests separately from pending ones

#### 5. Queue Monitor + Rebalancer
- Watches queue depth across all device+model pairs on every heartbeat cycle
- When a queue exceeds a threshold, identifies overflow candidates (nodes with the model loaded or on disk with available memory)
- Moves **pending** (not yet in-flight) requests to the best alternative queue
- Can trigger **pre-warming**: proactively load a model on an idle node before moving requests over, rather than cold-starting on demand

#### 6. Node Agents (runs on each device)
- Lightweight background process on every machine in the fleet
- Polls local Ollama APIs and system metrics every 5 seconds
- Sends a heartbeat payload to the Registry on the Mac Studio

#### 7. Node Registry
- Maintains the live state of every node: hardware profile, loaded models, available models, current load
- Source of truth for the Scoring Engine
- Tracks online/offline/degraded status

---

## Node Agent — What It Collects

All devices in this fleet are Apple Silicon Macs, which means **unified memory** — there is no separate VRAM pool. The entire memory budget is shared between system, CPU, and GPU workloads. Ollama draws from this pool for model weights.

### Metrics collected per node

**CPU**
- Physical and logical core count (static, sent at registration)
- Overall utilization %
- Per-core utilization %

**Unified Memory**
- Total, used, available (GB)
- macOS memory pressure state: `normal` / `warn` / `critical`
- Wired memory (GB) — kernel + locked allocations
- Compressed memory (GB) — macOS compresses inactive pages before swapping

> Memory pressure is a more reliable signal than raw available GB on macOS. The OS begins compressing and swapping well before free memory hits zero, so a node under pressure should be deprioritized even if it appears to have headroom.

**Ollama `/api/ps`**
- Models currently loaded into memory
- Memory consumed per model (Ollama reports this directly)
- Active request count per model

**Ollama `/api/tags`**
- All models available on disk with their sizes

### Heartbeat payload

```json
{
  "node_id": "macbook-pro-m3",
  "arch": "apple_silicon",
  "timestamp": 1710000000,
  "cpu": {
    "cores_physical": 10,
    "cores_logical": 10,
    "utilization_pct": 28.4,
    "per_core_pct": [12.1, 44.2, 31.0, 18.5, 22.3, 41.1, 15.0, 29.8, 33.2, 27.6]
  },
  "memory": {
    "total_gb": 32,
    "used_gb": 18.2,
    "available_gb": 13.8,
    "pressure": "normal",
    "wired_gb": 4.1,
    "compressed_gb": 1.2
  },
  "ollama": {
    "models_loaded": [
      { "name": "mistral:7b", "size_gb": 4.1, "requests_active": 1 }
    ],
    "models_available": ["mistral:7b", "phi3:mini", "qwen:0.5b"],
    "requests_active": 1
  }
}
```

---

## Queue Architecture

The queue is per **device + model pair**, not just per device. This is the key design decision.

```
macstudio  : llama3:70b    [■■■□□]  depth: 3  (2 in-flight, 1 pending)
macstudio  : mistral:7b    [■□□□□]  depth: 1  (1 in-flight)
laptop-a   : mistral:7b    [■■□□□]  depth: 2  (1 in-flight, 1 pending)
laptop-b   : phi3:mini     [□□□□□]  depth: 0  (idle)
```

### Rebalancing rules
- Only **pending** requests (not yet in-flight) can be moved
- In-flight requests must complete on the node where they started
- Rebalancing triggers when queue depth exceeds a configurable threshold
- If no node has the model hot, rebalancing can trigger a **pre-warm** on the best candidate before moving requests
- Pre-warm is preferred over cold-start-on-first-request for better latency

### Device capability profiles
Each device is registered with an auto-detected capability profile:
- Total unified memory → determines which model sizes are viable
- Core count + generation → informs expected tokens/sec
- Models currently on disk → determines what can be loaded without a download

The router uses capability profiles to hard-exclude nodes that cannot fit a requested model, regardless of other signals.

---

## Device Placement Strategy

The system treats the fleet like a capacity grid, not just a list of machines. Model placement — which models to keep hot where — is a strategy driven by usage patterns and device capabilities.

| Device | Memory | Best for |
|---|---|---|
| Mac Studio (M2 Ultra, 192GB) | 192GB unified | Large models: llama3:70b, mixtral:8x7b |
| MacBook Pro (M3 Pro, 36GB) | 36GB unified | Mid models: llama3:8b, mistral:7b |
| MacBook Air (M1, 16GB) | 16GB unified | Small models: phi3:mini, qwen:0.5b |
| Older MacBook (M1, 8GB) | 8GB unified | Tiny/quantized: phi3:mini 4-bit, qwen:0.5b |

Natural hardware affinity falls out of the scoring system automatically — the Mac Studio wins bids for large models because it has the memory, laptops handle smaller concurrent tasks.

### Opportunistic device policy
Laptops come and go. The system is designed to treat nodes as **opportunistic**, not guaranteed:
- Nodes register on startup and heartbeat continuously
- If a heartbeat is missed, the node is marked degraded, then offline
- On graceful shutdown, the node signals a **drain** — in-flight requests finish, pending requests are redistributed before the node goes offline
- Older/weaker machines are never excluded — they are assigned models that fit their ceiling

---

## Enabling AI Agents

This is where the system becomes most powerful. AI agent frameworks make many LLM calls in parallel or in rapid succession — planning, tool use, reflection, critique, summarization. Today they are constrained to either cloud APIs (expensive) or a single local Ollama instance (bottlenecked).

With the fleet router acting as a drop-in API replacement, a multi-agent pipeline can look like:

```
Planner Agent    → llama3:70b  on Mac Studio      (reasoning)
Tool-Use Agent   → mistral:7b  on MacBook Pro     (fast, concurrent)
Summarizer Agent → phi3:mini   on MacBook Air     (lightweight, cheap)
Critic Agent     → llama3:8b   on Mac Studio      (parallel evaluation)
```

All running simultaneously. Zero cloud cost beyond electricity. The agent framework does not know or care that it's talking to multiple devices.

### API compatibility
The router exposes:
- **OpenAI-compatible API** — `/v1/chat/completions`, `/v1/models` — works with any framework that supports a custom base URL
- **Ollama-compatible API** — `/api/chat`, `/api/generate`, `/api/tags` — works as a drop-in Ollama replacement

---

## Dashboard

A real-time web dashboard provides full visibility into fleet state, queue health, and inference activity.

### Device panel
- One card per node showing: online status, CPU utilization with sparkline history, memory bar (used/total with pressure state), active inference count

### Queue cards
- One card per device+model pair
- Queue depth bar, in-flight count, avg wait time, avg inference time
- Model status indicator: 🔥 loaded / 💾 on disk / ❄️ unloaded

### Queue drill-down
- Click into any queue to see: pending requests (ID, wait time, prompt preview), in-flight requests with live token/sec and elapsed time, recent completions with latency

### Activity feed
- Live log of rebalancing events: "Moved 2 requests: macstudio:mistral:7b → laptop-a:mistral:7b"
- Pre-warm events: "Pre-warming laptop-b with llama3:8b"
- Node join/leave events

### Fleet health bar
- Total nodes online, total queued, total in-flight, overall memory pressure

### Idle suggestions
- "laptop-b has been idle for 20 min — pre-warm with mistral:7b to absorb overflow?"
- "3 requests queued — wake MacBook Air?"

---

## Roadmap

### Phase 1 — Foundation
- [ ] Node agent script (metrics collection + Ollama polling + heartbeat)
- [ ] Node Registry (heartbeat ingestion + state tracking)
- [ ] Basic router (model matching + node selection)
- [ ] Simple queue per device+model pair

### Phase 2 — Intelligence
- [ ] Scoring Engine (weighted multi-signal ranking)
- [ ] Queue Monitor + Rebalancer
- [ ] Pre-warm triggering
- [ ] Graceful drain on node shutdown

### Phase 3 — API Layer
- [ ] OpenAI-compatible API endpoint
- [ ] Ollama-compatible API endpoint
- [ ] Streaming response proxy

### Phase 4 — Dashboard
- [ ] Real-time fleet view
- [ ] Queue drill-down
- [ ] Activity feed
- [ ] Idle suggestions

### Phase 5 — Intelligence Layer
- [ ] Usage pattern tracking → proactive model placement
- [ ] Per-model avg tokens/sec benchmarking per device
- [ ] Automatic capability profile tuning

---

## Why This Doesn't Exist Yet

Tools like **llama.cpp** server, **LocalAI**, and **LiteLLM** solve parts of this problem but none address the full fleet management and intelligent routing layer for personal device fleets. Ollama itself is single-node by design. What's missing is:

- Node-aware routing that understands unified memory constraints
- Queue management with rebalancing across heterogeneous personal devices
- A dashboard designed for a human managing their own device fleet
- Opportunistic node handling (devices that come and go)
- First-class integration with agent frameworks via API compatibility

This project fills that gap.

---

*Ollama Fleet Manager — maximize your personal hardware for local AI.*
