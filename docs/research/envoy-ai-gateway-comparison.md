# Envoy AI Gateway: Enterprise Cousin or Competitor?

## How we found it

Someone at a Seattle AI event (April 2025) recommended [Envoy AI Gateway](https://aigateway.envoyproxy.io). The pitch: an open-source AI gateway built on top of Envoy Proxy, the CNCF graduated project that powers Istio and AWS App Mesh. Co-developed by Tetrate and Bloomberg, donated to the CNCF community.

The natural question: is this the same thing as Ollama Herd? Should we be worried? Should we be integrating?

Short answer: they solve fundamentally different problems at fundamentally different scales. Think enterprise cloud API gateway vs local fleet router.

## What Envoy AI Gateway does

Envoy AI Gateway extends [Envoy Gateway](https://gateway.envoyproxy.io/) (a Kubernetes-native API gateway built on Envoy Proxy) with AI-specific capabilities. It sits in front of **cloud LLM APIs** and gives enterprise teams a single endpoint that handles:

- **Multi-provider routing** — one endpoint for OpenAI, Anthropic, Bedrock, Vertex AI, and 12+ other providers
- **Credential injection** — developers use a single internal auth token; the gateway injects per-provider API keys at the edge
- **Token-based rate limiting** — policies based on token consumption, not just request-per-second
- **Cross-backend failover** — if Provider A fails, traffic routes to Provider B automatically
- **Model name virtualization** — expose abstract model names that map to specific backends
- **A/B testing** — weight-based routing between providers or model versions
- **MCP support** — dedicated MCPRoute CRD for Model Context Protocol routing (added v0.4)
- **OpenTelemetry** — distributed tracing following GenAI semantic conventions

### Architecture

Two-tier gateway pattern:

- **Tier 1** — external-facing entry point. Handles client auth, global rate limiting, routing to cloud providers or internal model-serving clusters
- **Tier 2** — deployed inside self-hosted model-serving clusters (alongside KServe/vLLM). Handles internal traffic routing with an Endpoint Picker Provider (EPP) that selects endpoints based on KV-cache usage, queued requests, and LoRA adapter info

### Supported providers

OpenAI, Azure OpenAI, Anthropic, AWS Bedrock, Google Vertex AI, Google Gemini, Groq, Grok, Together AI, Cohere, Mistral, DeepSeek, DeepInfra, SambaNova, Hunyuan, Tencent LLM Knowledge Engine, and more.

Self-hosted models (vLLM, Ollama) work via OpenAI-compatible API format, though Ollama is not a first-class provider.

### Deployment

**Kubernetes is mandatory.** No Docker-only, no bare-metal, no laptop setup. Installation requires:

1. Kubernetes Gateway API CRDs
2. Gateway API Inference Extension CRDs
3. Envoy Gateway via Helm chart
4. Envoy AI Gateway via Helm chart
5. Extension manager configuration

You need familiarity with Kubernetes Gateway API, Envoy's xDS configuration model, Helm, and CRD-based configuration.

### Maturity

- **Version:** v0.5.0 (pre-1.0, released January 2026)
- **GitHub:** ~1,500 stars, 211 forks
- **Contributors:** Tetrate, Bloomberg, WSO2, Red Hat, Google
- **Adopters:** Bloomberg, Nutanix, Tencent Cloud, Tetrate
- **Assessment:** Ranked 4th of 5 in an independent comparison. Missing semantic caching, budget management, virtual key hierarchies vs more mature alternatives

## How it compares to Ollama Herd

| Dimension | Envoy AI Gateway | Ollama Herd |
|-----------|-----------------|-------------|
| **Core problem** | Govern cloud LLM API calls across enterprise teams | Route inference across local Ollama devices |
| **Deployment** | Kubernetes + Helm + CRDs | `uv run herd` (two commands, zero config) |
| **Infrastructure** | K8s cluster required | Any machine with Python |
| **Provider focus** | 16+ cloud APIs | Ollama instances on LAN |
| **Routing intelligence** | Weight-based, failover, A/B | 7-signal scoring (thermal, memory, queue, wait, affinity, availability, context fit) |
| **Hardware awareness** | None | Thermal state, memory pressure, CPU utilization, disk space, model loading state |
| **Device intelligence** | None | Capacity learning, meeting detection, dynamic context optimization |
| **Auth model** | CEL policies, credential injection, cross-namespace isolation | Trusted LAN, no auth needed |
| **Rate limiting** | Token-aware, policy-based | Per node:model queue with dynamic concurrency |
| **Observability** | OpenTelemetry + GenAI conventions | JSONL + SQLite + live dashboard + Fleet Intelligence |
| **Scale target** | Enterprise multi-cluster, multi-team | 1-5 machines, home/office fleet |
| **Operational overhead** | High (Envoy xDS, Gateway API, Helm, CRDs) | Near-zero (mDNS, SQLite, HTTP) |
| **Language** | Go (90.6%) | Python (async, FastAPI) |

### Where Envoy AI Gateway is stronger

- **Multi-provider abstraction** — single endpoint for 16+ cloud APIs with automatic credential rotation
- **Enterprise governance** — CEL authorization policies, cross-namespace isolation, TLS automation
- **Ecosystem** — inherits battle-tested Envoy Proxy capabilities (the same proxy handling billions of requests at Google, Lyft, Bloomberg)
- **MCP routing** — dedicated CRD for Model Context Protocol, ahead of the curve for agent tooling
- **Token-based rate limiting** — prevents cost surprises from expensive completions

### Where Ollama Herd is stronger

- **Hardware-aware routing** — knows about thermal state, memory pressure, model loading, device availability. Envoy AI Gateway routes blind to hardware
- **Zero-config setup** — two commands vs multi-step Kubernetes installation
- **Device intelligence** — capacity learning, meeting detection, dynamic context optimization. No equivalent in Envoy AI Gateway
- **Observability for operators** — live dashboard with health checks, Fleet Intelligence briefings, real-time SSE updates. Envoy relies on external OpenTelemetry pipelines
- **Local economics** — designed for the fleet agent use case where cloud API costs don't scale (see `local-fleet-economics.md`)

### Where they overlap

- Both proxy inference requests to backend model servers
- Both support OpenAI-compatible API format
- Both handle failover and retry logic
- Both are open-source and pre-1.0
- Both have streaming support

## What this means for Ollama Herd

### No competitive threat

Envoy AI Gateway doesn't do hardware-aware local routing. It can technically route to Ollama (via OpenAI-compat backend), but with zero awareness of memory pressure, thermal state, model loading, or device availability. It would round-robin or weight-route — dumb routing compared to 7-signal scoring.

Their target user is an enterprise K8s team managing cloud API spend. Our target user is someone running a few machines on their desk who wants smart routing with zero ops.

### Complementary in hybrid setups

An agent fleet that calls both cloud APIs and local models could use both:

- **Envoy AI Gateway** as the front door: handles "should this go to OpenAI or local?"
- **Ollama Herd** as the local backend: handles "which local machine handles this request?"

This is the hybrid architecture that makes sense for cost-sensitive agent fleets: expensive/complex requests go to cloud (Claude, GPT-4), routine inference stays local (120B open-source models).

### MCP routing is worth watching

Envoy AI Gateway added Model Context Protocol routing in v0.4 with dedicated MCPRoute CRDs. As AI agents increasingly use MCP for tool discovery and execution, gateway-level MCP routing becomes important. Worth tracking for potential integration or feature parity.

### Validates the space

Bloomberg and Tetrate building an AI gateway under CNCF governance validates that AI traffic management is a real problem. The fact that their solution requires Kubernetes and enterprise infrastructure validates Herd's niche: the same problem solved at personal/small-team scale with zero operational overhead.

## Key takeaway

If someone says "use Envoy AI Gateway instead of Herd" — they're solving a different problem. If they say "you should know about it" — they're right. It's the enterprise cousin of what we're doing, and the hybrid integration pattern (Envoy for cloud, Herd for local) is genuinely interesting.

## Sources

- [Envoy AI Gateway](https://aigateway.envoyproxy.io) — project homepage
- [GitHub: envoyproxy/ai-gateway](https://github.com/envoyproxy/ai-gateway) — source code
- [Reference Architecture](https://aigateway.envoyproxy.io/blog/envoy-ai-gateway-reference-architecture/) — two-tier gateway pattern
- [Tetrate blog: Concept to Reality](https://tetrate.io/blog/envoy-ai-gateway-concept-to-reality) — origin story
- [AI Gateway In Depth](https://jimmysong.io/blog/ai-gateway-in-depth/) — technical deep dive
- [Top Open Source AI Gateways (2026)](https://www.getmaxim.ai/articles/top-open-source-ai-gateways-for-enterprises-in-2026/) — independent comparison
- [kgateway Ollama docs](https://kgateway.dev/docs/envoy/2.0.x/ai/ollama/) — Ollama backend configuration
