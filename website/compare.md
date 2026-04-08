# How Ollama Herd Compares

The local AI inference space has dozens of tools. Most solve different problems. Here's an honest look at where Ollama Herd fits — and when you should use something else.

## Quick Comparison

| Feature | Single Ollama | DIY Scripts | exo | LiteLLM | GPUStack | **Ollama Herd** |
|---------|:------------:|:-----------:|:---:|:-------:|:--------:|:--------------:|
| Multi-device routing | No | Manual | No (splits models) | Cloud providers | Yes | **Yes** |
| Zero-config setup | Yes | No | Yes | Config file | Install + config | **2 commands** |
| mDNS auto-discovery | No | No | Yes | No | Yes | **Yes** |
| Thermal-aware routing | No | No | No | No | No | **Yes** |
| Memory pressure detection | No | No | No | No | No | **Yes** |
| Meeting detection | No | No | No | No | No | **Yes (macOS)** |
| Capacity learning | No | No | No | No | No | **168-slot model** |
| Per node:model queues | No | No | No | Rate limiting | Yes | **Yes** |
| Multi-signal scoring | No | No | No | Provider-level | Engine selection | **7 signals** |
| Model fallbacks | No | No | No | Yes | No | **Yes** |
| Auto-retry on failure | No | No | No | Yes | No | **Yes** |
| Auto-pull missing models | No | No | No | No | Yes | **Yes** |
| Real-time dashboard | No | No | Limited | Admin panel | Web UI | **SSE + 8 tabs** |
| Request tagging/analytics | No | No | No | Yes | No | **Yes** |
| OpenAI API compatible | No | Fragile | No | Yes | Yes | **Yes** |
| Ollama API compatible | Yes | Partial | No | Via config | No | **Yes** |
| Multimodal (images + STT) | No | No | No | No | No | **Yes** |
| Target user | Single machine | Tinkerers | Model sharding | Cloud gateway | GPU clusters | **Personal fleet** |

## Detailed Comparisons

### vs. Single Ollama

Running one Ollama instance is the starting point. It works great — until you have more than one machine or more than one concurrent user.

**When single Ollama is enough:**
- You have one machine
- You run one model at a time
- You don't mind waiting in a queue

**When you need Herd:**
- You have 2+ machines and want them to work together
- Multiple tools hit Ollama simultaneously (agents, coding assistant, chat)
- You're tired of model thrashing (loading/unloading models to free memory)
- Your MacBook fans spin up during inference and you want requests routed elsewhere

### vs. exo

exo splits a single large model across multiple devices using tensor parallelism. If one machine can't fit a 405B model, exo distributes the layers so they collectively run it.

**exo and Herd solve different problems.** exo answers "how do I run a model too big for one machine?" Herd answers "how do I route many requests to many models across many machines?"

They're complementary — an exo cluster can register as a single Herd node.

**Choose exo when:** You need to run one model that's too large for any single device.

**Choose Herd when:** You have multiple devices that can each run their own models and you want intelligent routing across all of them.

### vs. LiteLLM

LiteLLM is a cloud API gateway that provides a unified OpenAI-compatible interface to 100+ LLM providers (OpenAI, Anthropic, Bedrock, Azure, etc.).

**Different layer entirely.** LiteLLM routes between cloud providers. Herd routes between local devices. LiteLLM has no concept of thermal state, memory pressure, device health, or mDNS discovery.

They work together naturally — Herd sits between LiteLLM and your local Ollama instances, giving LiteLLM a single "local" endpoint backed by an intelligent fleet.

**Choose LiteLLM when:** You need to route between cloud providers or want a unified API across OpenAI/Anthropic/etc.

**Choose Herd when:** You want your local devices to work together. Use both if you want local + cloud with intelligent routing at each layer.

### vs. GPUStack

GPUStack is a GPU cluster manager for AI model deployment. It manages GPU resources across environments (on-prem, Kubernetes, cloud), auto-configures inference engines (vLLM, SGLang, TensorRT-LLM), and supports all GPU vendors.

**GPUStack is more polished but more complex.** It targets GPU cluster operators who want multi-engine support and enterprise features. Herd targets individuals and small teams who want zero-config fleet management with the Ollama they already use.

**Choose GPUStack when:** You're managing a GPU cluster with mixed vendors and need multi-engine support.

**Choose Herd when:** You have a few personal devices running Ollama and want them to work together in 60 seconds.

### vs. DIY Scripts

Many people write their own routing scripts — round-robin across Ollama instances, manually checking which node has capacity, or just SSH-ing into whichever machine seems free.

**DIY works until it doesn't.** You'll spend more time maintaining the scripts than using them. No thermal awareness, no capacity learning, no auto-retry, no dashboard, no meeting detection. Every edge case becomes your problem.

**Choose DIY when:** You have very specific routing logic that no tool supports.

**Choose Herd when:** You want routing that handles the edge cases you haven't thought of yet.

## What Makes Herd Unique

No other project combines all of these:

1. **7-signal intelligent scoring** with learned latency data
2. **Per node:model queue management** with dynamic concurrency
3. **mDNS zero-config discovery** — truly two commands
4. **Adaptive capacity learning** — learns your weekly usage patterns
5. **Meeting detection + app fingerprinting** — respects that laptops aren't servers
6. **Multimodal routing** — LLM, embeddings, image gen, and speech-to-text
7. **Both OpenAI and Ollama API formats** — drop-in for any client
8. **Real-time dashboard** with fleet overview, trends, health, and analytics

The market is fragmenting into three niches: model splitting (exo), cloud API gateways (LiteLLM), and local fleet routing. Herd owns the local fleet routing niche — purpose-built for people with multiple devices who want one smart endpoint.
