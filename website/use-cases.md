# Use Cases

Ollama Herd turns multiple devices running Ollama into one smart endpoint. Here's how different people use it.

## Solo Developer with 2+ Machines

**The pain:** You have a Mac Studio for heavy work and a MacBook for portability. When you're running Aider or Continue.dev on the MacBook, it heats up, fans spin, and inference slows down. Meanwhile the Mac Studio sits idle. You keep SSH-ing between machines or manually switching base URLs.

**With Herd:** Point all your tools at `http://router-ip:11435`. The Mac Studio handles the heavy models (70B+), the MacBook handles quick tasks (7B-14B). When the MacBook is in a Zoom call, requests automatically route to the Mac Studio. When you're at your desk with both machines free, they share the load.

**Example setup:**
- Mac Studio (192GB): Llama 3.3 70B + DeepSeek Coder 33B — always loaded
- MacBook Pro (36GB): Qwen 2.5 7B + Nomic Embed — for lightweight tasks and RAG
- Tools: Aider, Continue.dev, Open WebUI — all pointed at one URL

## Agent-Heavy Workflows

**The pain:** You're running CrewAI crews, LangChain chains, or OpenClaw agents that fire dozens of concurrent LLM requests. A single Ollama instance queues them all sequentially. A 5-agent crew that should take 2 minutes takes 10 because every request waits in line.

**With Herd:** Concurrent requests fan out across your fleet. Agent #1 goes to the Mac Studio, agent #2 goes to the MacBook, agent #3 goes to the Mac Mini. Throughput scales linearly with machines. Auto-retry means agent failures don't crash the crew — the router re-routes to the next best node.

**Example setup:**
- 3 devices: Mac Studio + MacBook Pro + Mac Mini
- Models: One large reasoning model (70B), one fast agent model (7B-14B), one embedding model
- Framework: CrewAI / LangChain / OpenClaw — all using OpenAI SDK with `base_url` pointed at Herd

## Small Team / Office

**The pain:** Your team has 4-5 Macs. Everyone runs Ollama locally, but nobody's machine is powerful enough for the big models. People share a "team Mac Studio" by manually coordinating who's using it. No visibility into who's queued where.

**With Herd:** One router, all machines as nodes. Everyone points their tools at the same URL. The router handles contention — no manual coordination. The dashboard shows who's using what, queue depths, and per-app analytics (via request tagging). The Mac Studio handles the big models, personal laptops handle lightweight tasks.

**Example setup:**
- Router on the team Mac Studio
- 4 MacBooks as nodes (each running `herd-node`)
- Per-app tagging: each developer's tools tagged for analytics
- Dashboard on a shared monitor or bookmarked URL

## Home Lab Enthusiast

**The pain:** You've accumulated hardware — a Mac Mini, an older MacBook, maybe a Linux box with an NVIDIA GPU. You want a unified local AI setup but every tool assumes a single machine. Managing multiple Ollama instances manually is tedious.

**With Herd:** Every device joins the fleet automatically via mDNS. Mix and match platforms — macOS, Linux, Windows. The router knows each device's capabilities and routes accordingly. NVIDIA GPU boxes handle what they're good at, Apple Silicon handles the rest. Image generation routes to the Mac with mflux installed. Embeddings route to whichever node has the model loaded.

**Example setup:**
- Mac Mini M2 (24GB): Small models + embeddings
- Linux box with RTX 4090: Large models with CUDA acceleration
- Old MacBook (16GB): Lightweight agent tasks when it's not being used
- All discovered automatically, no config files

## Multimodal AI Pipeline

**The pain:** You need LLM inference, embeddings for RAG, image generation, and speech-to-text. Each service runs on a different port, different machine, different API. Your application code is full of conditional routing logic.

**With Herd:** One endpoint handles all four model types. The router knows which nodes can serve which modality and routes accordingly. Your app talks to one URL for everything.

**Example setup:**
- LLM: `POST /v1/chat/completions` or `POST /api/chat` — routed to best available node
- Embeddings: `POST /api/embed` — routed to node with embedding model loaded
- Image gen: `POST /api/generate-image` — routed to Apple Silicon node with mflux
- Speech-to-text: `POST /api/transcribe` — routed to node with MLX and Qwen3-ASR
- All through `http://router-ip:11435`

## "Is This For Me?"

**Herd is a great fit if:**
- You have 2 or more devices that can run Ollama
- You run AI tools concurrently (agents, coding assistants, chat)
- You want zero-config setup (no Docker, no Kubernetes, no YAML)
- You care about privacy and want everything local
- You're tired of model thrashing on a single machine

**Herd is probably overkill if:**
- You have exactly one machine and no plans to add more
- You run one model at a time with no concurrency needs
- You're happy with single-machine Ollama performance

**Getting started takes 60 seconds:**
```bash
pip install ollama-herd
herd                    # on your router machine
herd-node               # on each device
```
