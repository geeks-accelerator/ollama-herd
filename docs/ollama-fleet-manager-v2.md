# Ollama Fleet Manager
### Turn your personal Apple Silicon devices into a private, zero-cost AI inference fleet

---

## The Vision

Most people running local LLMs via Ollama are leaving compute on the table. A Mac Studio that could be running the largest open source models. A new MacBook with capacity to spare in the evenings. An older laptop gathering dust that can still run fast small models. Together these form a personal inference cluster that nobody is currently orchestrating intelligently.

**Ollama Fleet Manager** is a smart routing and orchestration layer that turns a collection of personal Apple Silicon devices into a unified, private, zero-cost LLM inference fleet. Point any AI agent framework, app, or script at a single endpoint and the system transparently distributes requests across your devices — routing each request to the best available device based on what models are loaded, what hardware is available, how busy each machine is, and critically, whether the device's owner is actually using it for work.

No cloud costs. No single-device bottleneck. No wasted hardware. No Ollama requests killing your MacBook while you're on a Zoom call.

---

## The Problem This Solves

### Cloud costs are a barrier to capable AI agents

AI agent frameworks (CrewAI, AutoGen, LangChain, custom pipelines) make a large number of LLM calls — planning, reflection, tool use, summarization, critique. A single agentic task can burn through dozens of API calls. Complex multi-agent pipelines can cost dollars per run. This forces users to either pay heavily or artificially throttle their agents to be less capable.

### Single-device local inference creates a new bottleneck

Running locally via Ollama solves the cost problem but introduces a new constraint: one device, one model loaded at a time. A multi-agent pipeline where three agents need to call different models simultaneously just queues up on one machine. Bigger, smarter models that would improve agent quality often can't fit alongside other models the pipeline needs.

### Spare devices go unused

Many people have older laptops, secondary machines, or underutilized hardware that is perfectly capable of running quantized smaller models. A 2020 MacBook with 16GB of unified memory can comfortably run phi4 or a 4-bit quantized 7B model at useful speeds. Currently that compute sits idle.

### Your work machine gets hijacked

Without intelligence about device context, any local routing system will happily consume 100% of your new MacBook's memory and CPU for Ollama requests while you're trying to compile code, edit video, or join a meeting. Fleet Manager treats device context as a first-class signal — not an afterthought.

---

## Architecture Overview

```
Clients (agent frameworks, apps, scripts, OpenWebUI)
                        ↓
         Custom API — OpenAI + Ollama compatible
                        ↓
    Router → Scorer → Queue Manager → Rebalancer
                        ↓
    Node Agents on each device → Ollama instances
```

### Core Components

**Custom API (runs on Mac Studio)**
Exposes a unified endpoint compatible with both the OpenAI API (`/v1/chat/completions`) and the Ollama API (`/api/chat`, `/api/generate`). Any agent framework or app can point at it with a single env var change. No code modifications required.

**Smart Router**
Receives each request and selects the optimal device+model queue using the Scoring Engine. Handles streaming response proxying transparently.

**Scoring Engine**
Ranks all candidate nodes for a given request:
- Model currently loaded in memory → strong bonus (no cold-start latency)
- Model on disk but not loaded → smaller bonus (load time penalty)
- Node in low-capacity mode → heavy penalty or disqualification
- Memory pressure state → deprioritizes nodes under macOS memory compression
- Active request count → penalty scaled by in-flight load
- Device role (anchor vs overflow vs fast-tier) → structural routing preference

**Queue Manager**
Maintains a queue per device+model pair — not just per device. `macstudio:llama3.3:70b` and `macbook-old:phi4:14b` are independent queues with independent depths.

```
macstudio    : deepseek-r1:671b   [■■■□□]  depth: 3
macstudio    : llama3.3:70b       [■□□□□]  depth: 1
macbook-new  : qwen2.5:32b        [□□□□□]  depth: 0  (low-cap mode)
macbook-old  : phi4:14b           [■■□□□]  depth: 2
macbook-old  : qwen2.5:7b         [■□□□□]  depth: 1
```

**Queue Monitor + Rebalancer**
Watches queue depth every heartbeat cycle. When a queue exceeds threshold, identifies overflow candidates and moves pending (not yet in-flight) requests to better queues. Can trigger pre-warm: proactively loading a model on an idle node before moving requests over, avoiding cold-start on the first request.

**Node Agents**
Lightweight background process on every device. Polls Ollama APIs and system metrics every 5 seconds, sends a heartbeat to the Registry. On the work MacBook, the agent also runs the adaptive capacity learner.

**Node Registry**
Maintains live state of every node: hardware profile, loaded models, available models, current load, and current capacity mode. Source of truth for the Scoring Engine.

---

## Your Specific Fleet

### 🖥️ Mac Studio 2023 — 512GB Unified Memory
**Role: Heavy lifter and fleet anchor**

The Mac Studio is the undisputed anchor of this fleet. At 512GB of unified memory it can run the largest open source models that exist, keep several loaded simultaneously, and still have hundreds of gigabytes to spare. This machine runs 24/7 as the router and the primary inference engine.

**Recommended models to keep loaded:**

| Model | Size (Q4) | Purpose |
|---|---|---|
| `deepseek-r1:671b` | ~370GB | Frontier reasoning — the flagship |
| `llama3.1:405b` | ~230GB | Meta's best general model |
| `llama3.3:70b` | ~40GB | Workhorse — keep hot always |
| `mixtral:8x22b` | ~65GB | Fast mixture-of-experts for agent pipelines |
| `deepseek-coder-v2` | ~15GB | Dedicated coding model |
| `nomic-embed-text` | ~300MB | Embeddings — tiny, keep hot always |

The Mac Studio can simultaneously hold the 70B workhorse, a coding model, and the embedding model in memory while still having capacity for burst requests on the larger models. Practically speaking: the 671B and 405B models share the deep-end of memory on demand while the 70B stays permanently loaded as the fast response tier for this device.

**Router configuration:** Maximum priority. Never throttled. All requests welcome. Pre-warm idle models based on historical usage patterns.

---

### 💻 New MacBook — 128GB Unified Memory
**Role: Overflow node with adaptive capacity**

This is where Fleet Manager does something no existing tool does: treats a primary work machine as an intelligent, self-aware fleet participant that learns when it's safe to contribute capacity and when it needs to protect the owner's work.

**Recommended models when available:**

| Model | Size (Q4_K_M) | Purpose |
|---|---|---|
| `llama3.3:70b` | ~40GB | Overflow from Mac Studio during busy periods |
| `qwen2.5:32b` | ~20GB | Strong reasoning, lighter footprint |
| `mistral-small:22b` | ~14GB | Fast, high-quality, low resource footprint |
| `phi4:14b` | ~9GB | Efficient backup for small tasks |

**Adaptive Capacity Learning — not static config**

Rather than a YAML file with hardcoded work hours, the node agent on this MacBook continuously learns your actual usage patterns. It observes the device over time and builds a probabilistic model of when you're working versus when the machine has genuine spare capacity.

What the agent tracks:

```
CPU utilization history          → rolling 30-day baseline by hour/day
Memory pressure state history    → when does pressure typically spike
Active application fingerprints  → which apps are running and how demanding
Input activity                   → keyboard/mouse activity as a work proxy
Meeting detection                → camera/microphone in use (Zoom, FaceTime, Meet)
Network I/O patterns             → video call bandwidth signatures
Thermal state                    → is the machine running hot from your work
```

How the learning works:

The agent builds a rolling usage model with a 30-day window. For each hour of the week (7 days × 24 hours = 168 slots), it maintains a learned distribution of observed system load. This isn't a rigid schedule — it's a probabilistic map of your behavior.

```
Monday 10am:  historically 87% CPU busy, 92% memory pressure → inferred: deep work
Monday 7pm:   historically 12% CPU, normal memory → inferred: available
Saturday 2pm: historically 34% CPU, normal memory → inferred: light use, available
```

The agent then computes a real-time **availability score** that combines:
- The historical baseline for this hour (what you usually do at this time)
- The current observed state (what's actually happening right now)
- A trend signal (is activity rising or falling in the last 10 minutes)

This score maps to a dynamic memory ceiling the router respects:

```
Availability 0–20%  → Ollama paused entirely
Availability 20–40% → 16GB ceiling, lowest priority
Availability 40–60% → 32GB ceiling, low priority
Availability 60–80% → 64GB ceiling, normal priority
Availability 80–100% → 100GB ceiling, full participant
```

**Immediate override signals** (regardless of learned baseline):
- Camera or microphone active → hard pause (you're in a meeting)
- Memory pressure hits `warn` → drain queue, pause new requests
- CPU above 85% sustained 2+ minutes → drop to 8GB ceiling
- Thermal throttling detected → pause entirely

**Cold-start bootstrap:**
On first install, the agent runs in observation-only mode for 7 days, collecting behavioral data before contributing any capacity. This means the system is safe to install immediately on a work machine — it will learn before it acts.

**User visibility:**
The dashboard shows the MacBook's current inferred mode with a simple status and the historical pattern heat map so you can see exactly what it's learned. You can nudge it manually when the model is wrong ("I'm on vacation this week — full capacity") without needing to touch config files.

---

### 💻 Old MacBook — 16GB Unified Memory
**Role: Fast-response tier for small models**

This machine is more capable than it sounds for the right tasks. 16GB of unified memory comfortably runs 7B models at good quality and smaller models at impressive speed. It should be the fleet's dedicated fast-response layer — high throughput for lightweight tasks that don't need heavy reasoning.

**Recommended models:**

| Model | Size (Q4_K_M) | Purpose |
|---|---|---|
| `phi4:14b` | ~9GB | Microsoft's surprisingly capable small model |
| `qwen2.5:7b` | ~5GB | Excellent quality-to-size, keep hot |
| `llama3.2:3b` | ~2GB | Ultra-fast classification and routing tasks |
| `gemma3:4b` | ~3GB | Google's efficient small model |
| `nomic-embed-text` | ~300MB | Embeddings everywhere |

Keep `qwen2.5:7b` and `nomic-embed-text` permanently hot. Use `phi4:14b` as the primary model when only one is needed at a time (it won't fit alongside the 7B comfortably in 16GB). The 3B models are ideal for agent sub-tasks: summarizing, classifying, routing decisions, tool call formatting — tasks where latency matters more than raw capability.

**Router configuration:** Always available. Never throttled. First choice for fast small-model requests, overflow destination for embeddings from any device.

---

## Fleet Summary

| Device | Memory | Role | Ollama Budget | Priority |
|---|---|---|---|---|
| Mac Studio 2023 | 512GB | Anchor + router | ~450GB | Highest — always |
| New MacBook | 128GB | Adaptive overflow | 0–100GB (learned) | Dynamic |
| Old MacBook | 16GB | Fast-response tier | ~13GB | Always on |

**Natural request routing:**

- Request for `deepseek-r1:671b` → Mac Studio only
- Request for `llama3.3:70b` → Mac Studio first, MacBook overflow if busy and available
- Request for `qwen2.5:7b` → Old MacBook first (keep it free for this)
- Request for embeddings → whichever device has `nomic-embed-text` hot with lowest queue
- Agent pipeline with 3 parallel calls → fan out across all three devices simultaneously

---

## Queue Architecture

The queue is per device+model pair. This is the key design decision — it allows the system to reason precisely about load, make intelligent rebalancing decisions, and give operators (you) clear visibility into exactly where bottlenecks are forming.

**Rebalancing rules:**
- Only pending requests (not yet in-flight) can be moved between queues
- In-flight requests complete where they started — no mid-stream interruptions
- Rebalancing triggers when queue depth exceeds a configurable threshold
- If no node has the model hot, pre-warm the best candidate before moving requests over
- The MacBook in low-capacity mode is never a rebalancing target regardless of queue depth

**Pre-warm strategy:**
When the Mac Studio's `llama3.3:70b` queue depth grows and the MacBook's availability score is above 60%, the system proactively loads `llama3.3:70b` on the MacBook before the queue backs up further. This eliminates the cold-start latency on the first overflow request.

---

## Adaptive Capacity Learning — Technical Design

The auto-learning system on the work MacBook is built around a few core ideas:

**Rolling behavioral model**

The agent maintains a 168-slot (7×24 hours) rolling histogram of system state. Each slot stores a distribution of observed CPU, memory pressure, and application fingerprints over the last 30 days. Older observations are weighted less (exponential decay) so the model adapts to lifestyle changes — a new job with different hours, a vacation, a project crunch period.

**Application fingerprinting**

Rather than reading application names (privacy concern), the agent observes resource consumption signatures. Video call applications have a distinctive pattern: sustained camera/microphone usage, specific network I/O bandwidth, elevated CPU baseline. The agent learns to recognize these patterns without needing to know which specific app is running.

**Confidence-weighted decisions**

Early in learning (first 7–30 days), the model has low confidence and defaults to conservative behavior — smaller memory ceilings, slower capacity increases. As more data accumulates, confidence rises and the model becomes more aggressive about offering capacity during historically-idle periods.

**Anomaly handling**

If current behavior is significantly different from the learned baseline (you're working on a Saturday afternoon when you're usually free), the real-time observed state takes priority over the historical model. The system is conservative by default: when uncertain, it offers less capacity, not more.

**Privacy**

All learning happens locally on the device. No behavioral data leaves the machine. The node heartbeat to the router sends only the current availability score and memory ceiling — not raw usage data or application information.

---

## Node Agent — What It Collects

All devices are Apple Silicon, meaning unified memory — no separate VRAM pool. The GPU, CPU, and system all draw from the same memory budget.

**Per device, every 5 seconds:**

```json
{
  "node_id": "macstudio",
  "arch": "apple_silicon",
  "timestamp": 1710000000,
  "cpu": {
    "cores_physical": 24,
    "utilization_pct": 34.2,
    "per_core_pct": [...]
  },
  "memory": {
    "total_gb": 512,
    "used_gb": 180.4,
    "available_gb": 331.6,
    "pressure": "normal",
    "wired_gb": 12.1,
    "compressed_gb": 0
  },
  "capacity": {
    "mode": "full",
    "ceiling_gb": 450,
    "availability_score": 1.0
  },
  "ollama": {
    "models_loaded": [
      { "name": "llama3.3:70b", "size_gb": 40.2, "requests_active": 1 },
      { "name": "nomic-embed-text", "size_gb": 0.3, "requests_active": 0 }
    ],
    "models_available": ["deepseek-r1:671b", "llama3.1:405b", "mixtral:8x22b", "deepseek-coder-v2"],
    "requests_active": 1,
    "requests_queued": 2
  }
}
```

The MacBook's heartbeat additionally includes:

```json
{
  "capacity": {
    "mode": "learned_low",
    "ceiling_gb": 20,
    "availability_score": 0.31,
    "reason": "historically_busy_slot",
    "override_active": false,
    "learning_confidence": 0.84,
    "days_observed": 23
  }
}
```

---

## AI Agent Framework Integration

This is the system's highest-value use case. Agent frameworks make many LLM calls in parallel or rapid succession. Today they're constrained to cloud APIs (expensive) or a single Ollama instance (bottlenecked). Fleet Manager removes both constraints.

**What a multi-agent pipeline looks like on this fleet:**

```
Planner Agent     → deepseek-r1:671b  on Mac Studio    (deep reasoning)
Tool-Use Agent    → llama3.3:70b      on Mac Studio    (fast execution)
Summarizer Agent  → qwen2.5:7b        on Old MacBook   (lightweight)
Critic Agent      → qwen2.5:32b       on New MacBook   (if available)
Embedder          → nomic-embed-text  on Old MacBook   (always fast)
```

All running simultaneously. Zero cloud cost. The agent framework doesn't know or care that it's talking to multiple devices.

**Integration is one line:**

```python
# Before — expensive
client = OpenAI(api_key="sk-...")

# After — free, private, fleet-distributed
client = OpenAI(base_url="http://macstudio.local:8080/v1", api_key="none")
```

Works with: LangChain, LangGraph, CrewAI, AutoGen, LlamaIndex, or any OpenAI-compatible client. No other changes needed.

---

## Real-Time Dashboard

The dashboard is a core product feature — not an afterthought. It's the visual artifact that makes the system understandable and shareable.

**Fleet overview bar**
Total nodes online, total queued requests, total in-flight, global memory pressure. Immediately tells you if the fleet is healthy.

**Device cards (one per node)**
- CPU sparkline (last 5 minutes)
- Memory bar with pressure state color (green/yellow/red)
- Current capacity mode and ceiling
- MacBook: adaptive capacity heat map showing the learned weekly pattern, current availability score, and what's overriding it if anything

**Queue cards (one per device+model pair)**
- Queue depth bar
- In-flight request count with elapsed time
- Avg wait time + avg tokens/sec
- Model status: 🔥 loaded / 💾 on disk / ❄️ not available

**Queue drill-down**
Click any queue to see pending requests (ID, wait time, prompt preview), in-flight requests with live token/sec counter, and recent completions with latency.

**Rebalancing activity feed**
Live log:
```
14:23:01  Moved 2 requests: macstudio:llama3.3:70b → macbook-new:llama3.3:70b
14:22:45  Pre-warming macbook-new with llama3.3:70b (macstudio queue depth: 6)
14:21:30  macbook-new capacity mode: learned_low → learned_high (availability: 0.82)
14:18:00  macbook-old:phi4:14b queue cleared — idle
```

**MacBook learning panel**
A dedicated section showing:
- The learned weekly heat map (which hours are typically available)
- Current inferred mode and why
- Confidence score and days observed
- Manual override controls ("I'm on vacation — full capacity this week")

---

## Competitive Landscape

### exo (41,000 GitHub stars)
Connects Apple Silicon devices and uses tensor/pipeline parallelism to split a single large model across all of them. Goal: run models too big for any one device.

Fleet Manager is complementary, not competitive. exo splits one huge model across devices. Fleet Manager routes many different requests to many different models across many devices. A Fleet Manager node could itself be an exo cluster.

### OLOL
Python/gRPC load balancer for Ollama. Static server configuration, basic round-robin or model-aware routing. No queue management, no rebalancing, no utilization awareness, no concept of a work machine with dynamic capacity.

### SOLLOL
Orchestration and observability layer for distributed Ollama. Early stage. No queue architecture, no rebalancing, no adaptive device context.

### Hive
HiveCore + HiveNode architecture where nodes connect outbound-only (no port forwarding required). Interesting architectural concept worth borrowing. No scoring, no queue management, no adaptive capacity.

### Olla
High-performance proxy and load balancer with circuit breakers, connection pooling, and broad API compatibility. Production infrastructure tool. No per-device utilization tracking, no queue depth management, no work-machine awareness, no personal fleet UX.

### The Gap Fleet Manager Fills

| Capability | exo | OLOL | Olla | Fleet Manager |
|---|---|---|---|---|
| Multi-device coordination | ✅ | ✅ | ✅ | ✅ |
| Apple Silicon / unified memory aware | ✅ | ❌ | ❌ | ✅ |
| Per-device utilization tracking | ❌ | ❌ | Partial | ✅ |
| Queue per device+model pair | ❌ | ❌ | ❌ | ✅ |
| Queue rebalancing | ❌ | ❌ | ❌ | ✅ |
| Pre-warm idle devices | ❌ | ❌ | ❌ | ✅ |
| Work machine adaptive capacity | ❌ | ❌ | ❌ | ✅ |
| Auto-learned usage patterns | ❌ | ❌ | ❌ | ✅ |
| Graceful drain on departure | ❌ | ❌ | ❌ | ✅ |
| Real-time ops dashboard | Basic | ❌ | ❌ | ✅ |
| OpenAI API compatible | ✅ | ✅ | ✅ | ✅ |
| Ollama API compatible | ✅ | ✅ | ✅ | ✅ |

---

## Viral Growth Strategy

### The Hook

The projects that grow virally in this space have an instantly communicable premise:
- Ollama: *"Run LLMs locally with one command"*
- OpenWebUI: *"ChatGPT interface for your local Ollama"*
- exo: *"Run frontier models on your everyday devices"*

Fleet Manager's hook candidates:

> *"Your spare MacBook is wasting compute. Fleet Manager fixes that."*

> *"Turn all your Apple Silicon devices into one local AI cluster — automatically."*

> *"Run AI agents locally across your whole device fleet. Zero cost. Zero config."*

The work-machine angle is uniquely resonant: *"Fleet Manager knows when you're working and gets out of the way."* No competitor can say this.

### The Demo

The viral moment needs a 60-second screen recording showing:
1. Three devices auto-discovered — zero configuration
2. A multi-agent pipeline fires — requests fan out to all three devices simultaneously
3. A meeting starts — the MacBook's availability score drops to zero in real time on the dashboard
4. Requests automatically reroute to Mac Studio and old MacBook without interruption
5. Meeting ends — MacBook rejoins the fleet automatically
6. Total cloud cost: $0

Post simultaneously to r/LocalLLaMA, Hacker News (Show HN), and X/Twitter with the demo video.

### Zero-to-Running in Under 60 Seconds

```bash
# On each device
pip install fleet-node && fleet-node start

# On Mac Studio (router + dashboard)
pip install fleet-manager && fleet-manager start
# → Dashboard at http://localhost:8080
# → API at http://localhost:8080/v1
```

No config files on first run. Auto-discovery via mDNS. The router finds the nodes. The dashboard opens. That's the first-run experience — a product feature, not an afterthought.

### Distribution Channels

**OpenWebUI compatibility** — 45,000+ users can point OpenWebUI at Fleet Manager's endpoint with a single URL change. No other modifications. This is the single largest existing distribution channel in the local AI ecosystem.

**Agent framework integration guides** — step-by-step docs for CrewAI, LangChain, AutoGen, LlamaIndex. Each one is a separate community with its own forums, Discord, and YouTube ecosystem. Being the "local fleet backend" for each framework creates multiple independent distribution paths.

**r/LocalLLaMA** — the most important community in local AI. The "spare laptop" framing and the work-machine awareness feature are immediately relatable to this audience.

**Apple Silicon communities** — r/macmini, MacRumors, Apple subreddits. This audience doesn't identify as AI enthusiasts but owns exactly the hardware Fleet Manager is built for. The framing here is hardware efficiency, not AI capability.

**Hacker News Show HN** — lead with the engineering problem and the adaptive learning system. HN rewards intellectual honesty about tradeoffs and novel technical approaches. The behavioral modeling on the work machine is genuinely interesting to this audience.

### Community Hooks

**Hardware profile registry** — community-contributed optimal model configurations for specific Mac hardware combos. "Best setup for Mac Studio 192GB + MacBook Pro 36GB" as a shareable config. Gives every contributor's audience a reason to discover the project.

**Agent recipes** — shareable multi-agent pipeline configurations that work with Fleet Manager. A CrewAI recipe that uses the Mac Studio for planning and the old MacBook for fast tool calls is a natural viral artifact for the agent builder community.

**The name** — "Ollama Fleet Manager" is descriptive but not sticky. Consider: **Herder** (herding your device fleet), **Corral** (Ollama-adjacent animal metaphor), **Pack** (collective, distributed), **Shoal** (coordinated movement). The name should feel like it belongs alongside Ollama and OpenWebUI.

---

## Roadmap

### Phase 1 — Foundation
- [ ] Node agent: metrics collection, Ollama polling, heartbeat
- [ ] Node Registry: heartbeat ingestion, state tracking, online/offline detection
- [ ] Basic router: model matching, node selection, request forwarding
- [ ] Simple per-device+model queues

### Phase 2 — Intelligence
- [ ] Scoring engine: weighted multi-signal ranking
- [ ] Queue monitor + rebalancer
- [ ] Pre-warm triggering
- [ ] Graceful drain on node shutdown

### Phase 3 — Adaptive Capacity
- [ ] Behavioral observation agent (7-day bootstrap mode)
- [ ] Rolling usage model with exponential decay
- [ ] Availability score computation and dynamic ceiling
- [ ] Hard override signals (meeting detection, memory pressure)
- [ ] Manual override controls

### Phase 4 — API Layer
- [ ] OpenAI-compatible endpoint (`/v1/chat/completions`, `/v1/models`)
- [ ] Ollama-compatible endpoint (`/api/chat`, `/api/generate`, `/api/tags`)
- [ ] Streaming response proxy
- [ ] OpenWebUI compatibility validation

### Phase 5 — Dashboard
- [ ] Real-time fleet view with device cards
- [ ] Queue drill-down
- [ ] Rebalancing activity feed
- [ ] MacBook learning panel with heat map visualization
- [ ] Manual override controls

### Phase 6 — Ecosystem
- [ ] Agent framework integration guides (CrewAI, LangChain, AutoGen)
- [ ] Hardware profile registry
- [ ] Auto-discovered model recommendations per device spec
- [ ] exo cluster as Fleet Manager node support

---

## Why This Matters Beyond the Technical

The deeper thesis here is that most people have dramatically underestimated the compute they already own. A 2023 Mac Studio with 512GB of unified memory is a genuinely extraordinary inference machine — it can run models that cost hundreds of thousands of dollars in GPU hardware just two years ago. A fleet of three Apple Silicon devices, intelligently coordinated, can support sophisticated multi-agent AI workflows that would cost hundreds of dollars a month in cloud API fees.

Fleet Manager makes that compute accessible. Not by adding hardware — by adding the intelligence layer that the hardware has always deserved. And critically, it does so in a way that respects the fact that these devices belong to people who have other things to do with them.

The system that knows not to hijack your MacBook during a meeting is the system people will actually keep running.

---

*Ollama Fleet Manager — maximize what you already own.*
