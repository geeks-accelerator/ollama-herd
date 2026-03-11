# Why Distribute Local Inference

Most conversations about distributed AI focus on scaling to hundreds of GPUs in data centers. Ollama Herd solves a different problem: you have 2-5 machines on your desk or your network, each capable of running local models, and you want them to work together intelligently.

This doc covers the real benefits — the ones you discover after running a fleet for a week, not the ones on the marketing page.

## It's Not About Context Window Expansion

A common misconception: distributing inference across machines gives you a bigger context window. It doesn't.

Context window is a property of the **model**, not the fleet. If you're running Llama 3 with a 128K context window, you get 128K whether it runs on your MacBook or your Mac Studio. Ollama Herd routes each request to **one node** for full inference — it doesn't split context across machines.

What distribution actually gives you is fundamentally different and, in practice, more valuable for daily use.

## Concurrent Throughput Without Queuing

The most immediate benefit: multiple requests running simultaneously across different machines instead of queuing on one.

A single Mac Studio running a 70B model can handle one inference at a time (realistically). A second request waits. A third request waits longer. With agents, coding assistants, and background tasks all hitting the same Ollama instance, you're constantly bottlenecked.

With a fleet, request #1 goes to the Mac Studio, request #2 goes to the MacBook Pro, request #3 goes to the Mac Mini. All three run in parallel. Throughput scales linearly with machines.

This matters most when you're running AI-assisted development tools (Aider, Continue, Cline) alongside agent frameworks (CrewAI, LangChain) alongside ad-hoc chat. Each tool thinks it has a dedicated Ollama instance. The fleet makes that true.

## Model Mix Without Memory Thrashing

This is the benefit that's hard to appreciate until you've suffered without it.

On a single machine with 64GB RAM, you can comfortably keep one 70B model loaded. Need a coding model? Ollama unloads the 70B, loads the coding model. Need the 70B back? Unload, reload. Each swap takes 30-90 seconds depending on the model and your storage speed.

This is **model thrashing** — the local AI equivalent of disk thrashing in the pre-SSD era. It's the silent killer of local AI productivity.

With a fleet, different models live on different machines:

- Mac Studio (192GB): Llama 3 70B + DeepSeek Coder 33B — both loaded simultaneously
- MacBook Pro (36GB): Llama 3 8B for quick tasks + Nomic Embed for RAG
- Mac Mini (32GB): Mistral 7B + Phi-3 for lightweight agent loops

No model ever gets unloaded to make room for another. Every request hits a hot model. The router knows which models are loaded where and scores accordingly — a model already in GPU memory gets +50 points in the scoring engine.

The fleet doesn't just prevent thrashing. It makes thrashing architecturally impossible because each model has a permanent home.

## Hardware-Appropriate Routing

Not all machines are equal, and not all requests need the biggest model.

A quick "summarize this in one sentence" doesn't need a 70B model on your most powerful machine. A complex multi-step reasoning task does. Ollama Herd's scoring engine considers **role affinity** — it naturally routes large models to large machines and small models to small machines.

This means your Mac Studio handles the heavy lifting while your older MacBook contributes meaningfully with smaller models. Every machine in the fleet pulls its weight at the right level.

## Thermal and Resource-Aware Routing

Machines have physical constraints that change throughout the day.

A MacBook running inference heats up. Fan noise increases. Battery drains. Sustained GPU load triggers thermal throttling, which slows inference — sometimes dramatically. The scoring engine penalizes thermally stressed nodes, routing requests to cooler machines.

More subtly: the **capacity learner** builds a 168-slot weekly behavioral model (one slot per hour of the week). It learns that your MacBook is busy Tuesday mornings, your Mac Mini is idle on weekends, and your Mac Studio is always available. Routing decisions reflect these patterns automatically.

## Meeting Detection and Workload Awareness

Your MacBook is in a Zoom call. Inference requests would compete for CPU, RAM, and thermal headroom — degrading both the call quality and the inference speed.

Ollama Herd detects active cameras and microphones on macOS and **hard pauses** the node. No inference routes there until the meeting ends. This isn't a nice-to-have — it's the difference between a system you leave running and one you constantly babysit.

Beyond meetings, **application fingerprinting** classifies your current workload (idle/light/moderate/heavy/intensive) using CPU, memory, and network patterns — without reading app names or window titles. A heavy workload dynamically reduces the node's memory ceiling, which reduces its scoring, which routes requests elsewhere.

The fleet adapts to your work patterns. You don't adapt to the fleet.

## Fault Tolerance You Don't Think About

A single Ollama instance is a single point of failure. Process crashes, machine sleeps, network hiccup — your AI tools get errors.

With a fleet:
- Node goes offline → router stops routing to it within one missed heartbeat (5 seconds)
- Inference fails mid-request → auto-retry on next-best node (before first chunk is sent)
- Model unavailable everywhere → auto-pull to the best available node
- All nodes saturated → holding queue with FIFO ordering, drains as capacity frees

Clients never need retry logic. The fleet handles it.

## Pre-Warming and Proactive Loading

When one node's queue depth hits a threshold, the router **pre-warms** the same model on the runner-up node. By the time the next request arrives, the model is already loaded and hot.

The background **rebalancer** runs every 5 seconds, moving queued requests from overloaded nodes to nodes with spare capacity — but only to nodes where the model is already hot (avoiding cold-load cascading).

These mechanics are invisible to clients. Requests just get faster as the fleet learns demand patterns.

## The Fleet Gets Smarter Over Time

This is the compounding benefit.

- **Latency tables** (SQLite) track per-node, per-model response times. The scoring engine uses p75 historical latency to estimate wait times. A node that's consistently slow for a particular model gradually gets fewer requests for that model.
- **Capacity learner** refines its 168-slot weekly model with every heartbeat. After 30 days, it knows your usage patterns better than you do.
- **Trace store** records every request with full scoring breakdowns. You can query why any routing decision was made: `SELECT scores_breakdown FROM request_traces WHERE request_id = '...'`.

None of this state is ephemeral. It persists across restarts in SQLite and JSON files. A fleet that's been running for a month makes meaningfully better routing decisions than one running for a day.

## What This Means in Practice

The pitch isn't "run AI on multiple computers." The pitch is:

- **Zero wait time**: requests run on the first available machine, not in a queue
- **Zero model swapping**: every model stays loaded on its home machine
- **Zero babysitting**: meetings, heavy workloads, and thermal limits handled automatically
- **Zero client changes**: point your existing tools at one URL and the fleet figures it out
- **Compounding intelligence**: the longer it runs, the better the routing decisions

You stop thinking about which machine to use. You stop waiting for models to load. You stop killing inference when you need to jump on a call. The fleet just works, and it gets better at working every day.

That's the real benefit of distributing local inference. Not a bigger context window — a smarter, faster, more resilient way to use the hardware you already own.
