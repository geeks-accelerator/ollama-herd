# Competitive Landscape — Local LLM Inference Routing
### March 2026

---

## Market Context

The local LLM inference space has exploded since 2024. Ollama alone grew 46% in three months and now has more GitHub stars than PyTorch. The shift to local inference is driven by three forces: data privacy regulations, cloud API costs at agent scale, and consumer hardware that's now powerful enough to run 70B+ parameter models.

Within this space, a gap exists: most tools focus on running models on a single machine, but many users now have multiple capable devices. The question is no longer "can I run AI locally?" but "how do I make all my devices work together?"

Ollama Herd operates at this intersection — fleet orchestration for local LLM inference across multiple devices on a LAN.

---

## Direct Competitors

### Tier 1: Significant Projects

| Project | Stars | Language | Focus | Last Active |
|---------|-------|----------|-------|-------------|
| **exo** | ~42K | Python | Model sharding across devices | Active |
| **LiteLLM** | ~38K | Python | Cloud API gateway + proxy | Active |
| **GPUStack** | ~5K | Python | GPU cluster management | Active |
| **Bifrost** | ~2.8K | Go | Adaptive LLM load balancer | Active |
| **Olla** | ~170 | Go | LLM proxy with failover | Active |

### Tier 2: Niche / Emerging

| Project | Stars | Language | Focus |
|---------|-------|----------|-------|
| **SOLLOL** | ~4 | Python | Context-aware Ollama routing |
| **OLOL** | ~23 | Python | Ollama inference cluster |
| **Hive** | ~17 | — | Ollama task queue |
| **OllamaFlow** | ~17 | — | Backend-labeled Ollama routing |
| **ollama_proxy_server** | ~200+ | Python | Multi-instance proxy with API keys |
| **ollama_load_balancer** | ~50+ | Rust | Parallel request dispatch |

---

## Detailed Competitor Analysis

### exo (~42K stars)
**What it does:** Splits a single large model across multiple devices using tensor parallelism. If one machine can't fit a 405B model, exo distributes layers across several machines so they collectively run it.

**Relationship to Herd:** Complementary, not competitive. exo answers "how do I run a model too big for one machine?" Herd answers "how do I route many requests to many models across many machines?" An exo cluster could register as a single Herd node.

**Key differentiator:** exo makes one big model from many devices. Herd makes many devices serve many models intelligently.

**Limitations:** No request routing or load balancing. No queue management. No health-based scoring. Single-model focus. Doesn't use Ollama as its inference backend.

---

### LiteLLM (~38K stars)
**What it does:** AI gateway / proxy server that provides a unified OpenAI-compatible interface to 100+ LLM providers (OpenAI, Anthropic, Bedrock, Azure, etc.). Includes cost tracking, rate limiting, guardrails, and load balancing across cloud providers.

**Relationship to Herd:** Different layer. LiteLLM routes between cloud providers. Herd routes between local devices. They can work together — Herd sits between LiteLLM and local Ollama instances, giving LiteLLM a single "local" endpoint backed by an intelligent fleet.

**Key differentiator:** LiteLLM is provider-agnostic cloud gateway. Herd is device-aware local orchestration. LiteLLM has no concept of thermal state, memory pressure, device health, or mDNS discovery.

**Strengths:** Massive adoption, enterprise features (SSO, audit logging, JWT auth), 100+ provider integrations, mature ecosystem.

**Limitations:** No local device awareness. No per-device health scoring. No queue management per device+model pair. No auto-discovery.

---

### GPUStack (~5K stars)
**What it does:** GPU cluster manager for AI model deployment. Manages GPU resources across environments (on-prem, Kubernetes, cloud). Auto-configures inference engines (vLLM, SGLang, TensorRT-LLM). Supports all GPU vendors (NVIDIA, Apple, AMD, Intel).

**Relationship to Herd:** The most polished alternative for heterogeneous device fleets, but targets a different user. GPUStack is infrastructure-oriented — it manages GPU resources and inference engine selection. Herd is user-oriented — it makes your personal devices work together with zero config.

**Key differentiator:** GPUStack has a full web UI and supports multiple inference engines. Herd is simpler (two commands, zero config) and Ollama-native.

**Strengths:** Multi-engine support, multi-vendor GPU support, polished dashboard, enterprise positioning.

**Limitations:** More complex setup than Herd. Not Ollama-native. Targets GPU cluster operators, not personal device fleet owners. No mDNS auto-discovery.

---

### Bifrost (~2.8K stars)
**What it does:** Adaptive load balancer for LLM APIs. Routes requests based on latency, error rates, and capacity. Written in Go for high performance.

**Relationship to Herd:** Similar load balancing concept but targets cloud API endpoints, not local devices. No device health awareness.

**Key differentiator:** High-performance Go implementation with adaptive routing algorithms. But no device metrics, no mDNS, no Ollama integration, no dashboard.

---

### Olla (~170 stars)
**What it does:** High-performance lightweight proxy and load balancer for LLM infrastructure. Multiple strategies (priority-based, round-robin, least-connections). Automatic failover, rate limiting, connection pooling. Anthropic API support.

**Relationship to Herd:** Infrastructure proxy — production-quality reverse proxy with circuit breakers. More "Nginx for LLMs" than "fleet manager."

**Strengths:** Production-oriented features (connection pooling, circuit breakers, rate limiting). Written in Go.

**Limitations:** No mDNS discovery. No per-device health metrics. No dashboard designed for personal fleet management. No queue management. No capacity learning.

---

### SOLLOL (~4 stars)
**What it does:** Context-aware Ollama routing with priority queues, auto-discovery, and a dashboard. Part of a larger ecosystem (NERVA, FlockParser).

**Relationship to Herd:** Closest functional competitor. Similar feature set on paper — scoring, queues, discovery, dashboard.

**Limitations:** 4 stars. Tightly coupled to a larger ecosystem. Not a standalone polished product. Limited community.

---

## Adjacent Projects (Not Competitors)

| Project | Stars | What it Does | Relationship to Herd |
|---------|-------|-------------|---------------------|
| **Open WebUI** | ~126K | Chat interface for LLMs | Users can point it at Herd instead of a single Ollama. Open WebUI's built-in multi-instance support uses random selection — Herd adds intelligent routing. |
| **vLLM** | ~72K | High-performance serving engine | GPU cluster inference. Different scale and complexity. Not consumer-oriented. |
| **Petals** | ~10K | BitTorrent-style distributed inference | Volunteer-based model sharding across the internet. Different model (public volunteer network vs. personal LAN fleet). |
| **Distributed Llama** | ~2.9K | C++ model parallelism | Like exo but in C++. Splits one model across devices. Complementary. |
| **llm-d** | — | Kubernetes-native distributed inference | Red Hat project. Enterprise-scale, k8s-native. Different universe from personal fleet management. |
| **LocalAI** | ~30K+ | Local OpenAI-compatible API | Offers federated mode for distributed inference. More complex setup. Not Ollama-native. |

---

## Feature Comparison Matrix

| Feature | Ollama Herd | exo | LiteLLM | GPUStack | Olla | SOLLOL |
|---------|:-----------:|:---:|:-------:|:--------:|:----:|:------:|
| Multi-signal scoring | 7 signals | No | Provider-level | Engine selection | Priority | Context-aware |
| Per node:model queues | Yes | No | Rate limiting | Yes | No | Priority queues |
| mDNS auto-discovery | Yes | Yes | No | Yes | No | Yes |
| Real-time dashboard | SSE + 5 tabs | Limited | Admin panel | Full web UI | No | Yes |
| Ollama native | Yes | No | Via config | No | Via config | Yes |
| OpenAI API compat | Yes | No | Yes | Yes | Yes | Yes |
| Model fallbacks | Yes | No | Yes | No | No | No |
| Auto-retry on failure | Yes | No | Yes | No | Yes | No |
| Capacity learning | 168-slot model | No | No | No | No | No |
| Meeting detection | Yes (macOS) | No | No | No | No | No |
| Auto-pull missing models | Yes | No | No | Yes | No | No |
| Request tagging/analytics | Yes | No | Yes | No | No | No |
| Zero config setup | 2 commands | Yes | Config file | Install + config | Config file | Partial |
| Target user | Personal fleet | Model sharding | Cloud gateway | GPU clusters | Infrastructure | Ecosystem |

---

## Where Ollama Herd Wins

### Against exo
Not competition — complementary. exo splits one model across devices. Herd routes many requests to many models across many devices. Both can coexist: an exo cluster registers as a single Herd node.

### Against LiteLLM
Different layer. LiteLLM is a cloud API gateway. Herd is a local device orchestrator. Herd has no cloud provider integrations; LiteLLM has no device health awareness. They work together naturally.

### Against GPUStack
GPUStack is more polished but more complex. It targets GPU cluster operators who want multi-engine support and enterprise features. Herd targets individuals and small teams who want zero-config fleet management with the Ollama they already use. The "two commands, zero config" setup is Herd's moat against GPUStack's complexity.

### Against Olla / Bifrost
Infrastructure-oriented proxies. They're "Nginx for LLMs" — great at connection management and failover, but blind to device health, thermal state, memory pressure, and usage patterns. Herd knows your devices intimately.

### Against SOLLOL
Similar feature set on paper, but SOLLOL has 4 stars and is coupled to a larger ecosystem. Herd is standalone, well-documented, and designed for independent adoption.

---

## The Unique Position

No other project combines all of these:

1. **7-signal intelligent scoring** with learned latency data and context-fit awareness
2. **Per node:model queue management** with dynamic concurrency and rebalancing
3. **mDNS zero-config auto-discovery** — truly zero config
4. **Model fallbacks + auto-retry + auto-pull** — resilience at every layer
5. **Adaptive capacity learning** — 168-slot behavioral model of each device's weekly rhythm
6. **Meeting detection + app fingerprinting** — respects that laptops aren't servers
7. **Real-time dashboard** with 5 tabs (fleet, trends, models, apps, benchmarks)
8. **Request tagging** for per-application analytics
9. **Both OpenAI and Ollama API formats** — drop-in for any client
10. **Designed for personal Apple Silicon fleets** — not a scaled-down enterprise tool

---

## Market Gaps Herd Can Exploit

1. **Open WebUI's 126K users** have no intelligent routing. One URL change upgrades them from random selection to 7-signal scoring.

2. **Agent framework users** (OpenClaw 250K+, LangChain 108K, CrewAI 28K) need local fleet routing but don't want infrastructure complexity. One `base_url` change connects them to Herd.

3. **r/LocalLLaMA power users** frequently ask about multi-machine setups. There's no well-known, polished answer. Herd is that answer.

4. **Apple Silicon upgraders** — people who bought a new Mac but still have the old one. They want to use both. Herd makes this trivial.

---

*The competitive landscape favors Herd: the alternatives are either complementary (exo), operating at a different layer (LiteLLM), more complex (GPUStack), or too niche to matter (SOLLOL, Hive). The market gap — zero-config intelligent routing for personal device fleets — is Herd's to own.*
