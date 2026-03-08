# OpenClaw + Ollama Herd Integration Guide
### Run OpenClaw agents across your entire Apple Silicon device fleet

---

## Why This Integration Matters

OpenClaw is the largest open-source AI agent (247k+ GitHub stars). It runs a ReAct loop that makes 5-20+ LLM calls per task — reasoning, tool execution, observation, iteration. Each call hits your configured LLM provider.

The problem: OpenClaw's Ollama provider supports only a **single `baseUrl`**. One Ollama instance. One device. If that device is busy, your agent waits. If you have three Macs on your desk, two of them sit idle while the agent queues on one.

**Ollama Herd fixes this.** Point OpenClaw at Herd's endpoint and every LLM call is intelligently routed across your entire device fleet — scored by model availability, memory pressure, queue depth, and estimated latency. Your spare MacBook starts pulling its weight.

```
OpenClaw Agent (ReAct loop)
        |
        v
  Ollama Herd Router (:8080)
     /     |      \
    v      v       v
Mac Studio  MacBook Pro  MacBook Air
(70B models) (32B overflow) (7B fast tier)
```

---

## Quick Start (5 minutes)

### 1. Install and start Ollama Herd

On your router machine (e.g., Mac Studio):

```bash
git clone https://github.com/geeks-accelerator/ollama-herd.git
cd ollama-herd
uv sync
uv run herd
```

On each additional device:

```bash
git clone https://github.com/geeks-accelerator/ollama-herd.git
cd ollama-herd
uv sync
uv run herd-node
```

Nodes auto-discover the router via mDNS. No configuration needed.

### 2. Verify the fleet is running

Open `http://your-router:8080` in a browser. You should see the dashboard with your nodes listed under **Herd Nodes**.

Or check from the terminal:

```bash
curl http://localhost:8080/fleet/status | python3 -m json.tool
```

### 3. Configure OpenClaw

Edit `~/.config/openclaw/openclaw.json5`:

```json5
{
  "models": {
    "providers": {
      "ollama": {
        // Point at Herd router instead of local Ollama
        "baseUrl": "http://your-router:8080",
        "apiKey": "ollama-local",
        "api": "ollama",
        "models": [
          {
            "id": "llama3.3:70b",
            "name": "Llama 3.3 70B",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
            "contextWindow": 131072,
            "maxTokens": 1310720
          },
          {
            "id": "qwen2.5-coder:14b",
            "name": "Qwen 2.5 Coder 14B",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
            "contextWindow": 32768,
            "maxTokens": 327680
          },
          {
            "id": "qwen2.5:7b",
            "name": "Qwen 2.5 7B",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
            "contextWindow": 32768,
            "maxTokens": 327680
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "ollama/llama3.3:70b",
        "fallbacks": ["ollama/qwen2.5-coder:14b", "ollama/qwen2.5:7b"]
      }
    }
  }
}
```

### 4. Set the active model

```bash
openclaw models set ollama/llama3.3:70b
```

### 5. Use OpenClaw normally

```bash
openclaw
```

Every LLM call OpenClaw makes now goes through Herd. The router scores all available nodes and routes to the best one. Your agent doesn't know or care that it's talking to multiple devices.

---

## Configuration Deep Dive

### Native Ollama API vs OpenAI-Compatible Mode

OpenClaw supports two API formats for Ollama. **Use the native Ollama API** — it's the only mode that reliably supports tool calling with streaming.

| | Native Ollama (`api: "ollama"`) | OpenAI Compat (`api: "openai-completions"`) |
|---|---|---|
| Endpoint | `/api/chat` | `/v1/chat/completions` |
| Tool calling + streaming | Works | Broken (tools silently dropped) |
| Herd support | Full | Full |
| `baseUrl` format | `http://router:8080` | `http://router:8080/v1` |
| Recommended | **Yes** | Only if you need OpenAI format specifically |

### Native Ollama API (recommended)

```json5
{
  "models": {
    "providers": {
      "ollama": {
        "baseUrl": "http://your-router:8080",   // NO /v1 suffix
        "apiKey": "ollama-local",
        "api": "ollama"
      }
    }
  }
}
```

Herd receives requests on `/api/chat`, scores all nodes, routes to the best one, and streams NDJSON back. Tool calls work correctly.

### OpenAI-Compatible Mode (alternative)

```json5
{
  "models": {
    "providers": {
      "ollama": {
        "baseUrl": "http://your-router:8080/v1",   // WITH /v1 suffix
        "apiKey": "ollama-local",
        "api": "openai-completions",
        "injectNumCtxForOpenAICompat": false
      }
    }
  }
}
```

Set `injectNumCtxForOpenAICompat` to `false` — Herd's proxy does not forward the `options` field to Ollama, so injecting `num_ctx` has no effect and may cause warnings.

### Using Herd + Cloud Fallback

Configure Herd as local fleet with a cloud provider as fallback for when all devices are busy:

```json5
{
  "models": {
    "providers": {
      "ollama": {
        "baseUrl": "http://your-router:8080",
        "apiKey": "ollama-local",
        "api": "ollama",
        "models": [
          {
            "id": "llama3.3:70b",
            "name": "Llama 3.3 70B (Local Fleet)",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
            "contextWindow": 131072,
            "maxTokens": 1310720
          }
        ]
      },
      "anthropic": {
        "apiKey": "sk-ant-...",
        "models": [
          {
            "id": "claude-sonnet-4-20250514",
            "name": "Claude Sonnet (Cloud Fallback)"
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "ollama/llama3.3:70b",
        "fallbacks": ["anthropic/claude-sonnet-4-20250514"]
      }
    }
  }
}
```

OpenClaw tries your local fleet first. If Herd returns an error (all nodes saturated, model unavailable), it falls back to Claude in the cloud.

### Environment Variable Override

You can also configure via environment variables:

```bash
export OLLAMA_API_KEY=ollama-local
export OLLAMA_BASE_URL=http://your-router:8080
```

Note: For reliable configuration, the explicit JSON5 config is preferred over environment variables. Earlier OpenClaw versions had bugs where env vars didn't properly override provider config.

---

## Model Recommendations

### For OpenClaw agent tasks

| Model | Size | Best For | Recommended Device |
|---|---|---|---|
| `llama3.3:70b` | ~40GB | General reasoning, planning, complex tool use | Mac Studio / high-memory device |
| `qwen2.5-coder:14b` | ~9GB | Code generation, code review, debugging | Any device with 16GB+ |
| `qwen2.5:32b` | ~20GB | Strong reasoning with lower resource footprint | MacBook Pro / 32GB+ device |
| `deepseek-r1:14b` | ~9GB | Deep reasoning tasks (set `reasoning: true`) | Any device with 16GB+ |
| `qwen2.5:7b` | ~5GB | Fast lightweight tasks, summarization | Old MacBook / 8GB+ device |
| `phi4:14b` | ~9GB | Efficient general purpose | Any device with 16GB+ |

### Key guidance

- **14B+ parameter models** for reliable tool calling. Smaller models may hallucinate tool calls.
- **Set `reasoning: false`** unless the model explicitly supports reasoning parameters. Many Ollama models return HTTP 400 when `reasoning: true`.
- **Context window matters.** OpenClaw recommends 64k+ token context. Set `contextWindow` accurately in your model config.

### Example fleet model distribution

```
Mac Studio (512GB):   llama3.3:70b (always hot), deepseek-r1:671b (on demand)
MacBook Pro (128GB):  qwen2.5:32b, llama3.3:70b (overflow from Studio)
MacBook Air (16GB):   qwen2.5:7b (always hot), phi4:14b (when not alongside 7b)
```

Herd's scoring engine handles routing automatically — the Mac Studio wins bids for 70B requests when its queue is short, the MacBook Pro picks up overflow, and the Air handles all the lightweight calls.

---

## How Herd Helps OpenClaw Specifically

### 1. Multi-call agentic workloads

OpenClaw's ReAct loop generates sequential LLM calls: reason, act, observe, repeat. A complex task might make 15+ round-trips. Each call is independently routed to the best available node:

```
Call 1 (planning)     → Mac Studio (llama3.3:70b, hot, queue empty)
Call 2 (tool call)    → Mac Studio (still hot, queue depth 1)
Call 3 (observation)  → Mac Studio (queue depth 2, still best)
Call 4 (next action)  → MacBook Pro (Studio queue depth 4, Pro just loaded 70b via pre-warm)
Call 5 (summary)      → MacBook Air (qwen2.5:7b, fast lightweight task)
```

### 2. Multiple OpenClaw sessions sharing a fleet

Multiple family members or team members using OpenClaw through different messaging platforms (WhatsApp, Telegram, Slack) — all hitting the same Herd router. The queue manager prevents sessions from starving each other:

```
Alice's WhatsApp agent  → queued to Mac Studio
Bob's Discord agent     → queued to MacBook Pro (Studio busy with Alice)
Eve's Telegram agent    → queued to MacBook Air (lightweight model)
```

### 3. Model fallbacks

Herd's model fallback feature maps directly to OpenClaw's `fallbacks` config. If `llama3.3:70b` has no available node, Herd tries the fallback models before returning an error. OpenClaw's own fallback chain adds another layer:

```
Request for llama3.3:70b
  → Herd tries all nodes for llama3.3:70b... all busy
  → Herd tries fallback qwen2.5:32b on MacBook Pro... available!
  → Response streamed back to OpenClaw
```

### 4. Auto-retry transparency

Herd retries failed requests on alternative nodes before the first response chunk is sent. OpenClaw never sees the failure — the stream starts from the successful retry node. This matters for long-running agent tasks where a mid-pipeline failure would force the entire ReAct loop to restart.

### 5. Observability for agent debugging

Herd's dashboard and trace store provide visibility that OpenClaw itself doesn't have:

- **Which node handled each call** — see if your agent is being routed efficiently
- **Latency per call** — identify which LLM calls in the ReAct loop are bottlenecks
- **Token usage** — track how many tokens your agent pipeline consumes per task
- **Queue depth over time** — see if your fleet is sized appropriately for your agent workload

Visit `http://your-router:8080` to see the dashboard. Check traces at `/dashboard` under the **Model Insights** tab.

---

## Troubleshooting

### OpenClaw can't connect to Herd

```bash
# Verify Herd is running and reachable
curl http://your-router:8080/api/tags

# Should return: {"models": [...]}
# If connection refused: check that Herd is running (uv run herd)
# If empty models: check that nodes are connected (dashboard → Herd Nodes)
```

### Tool calls not working

Make sure you're using native Ollama API mode:
```json5
"api": "ollama"   // NOT "openai-completions"
```

And that `baseUrl` does NOT have `/v1` suffix:
```json5
"baseUrl": "http://router:8080"   // NOT "http://router:8080/v1"
```

### Model not found

Herd routes to nodes that have the model. If no node has it:

```bash
# Check what models are available across the fleet
curl http://your-router:8080/api/tags

# Pull the model on a specific node
ssh your-node "ollama pull llama3.3:70b"
```

### Context window defaulting to 4096

If using OpenAI-compatible mode, Ollama defaults to 4096 context unless `num_ctx` is explicitly sent. In native Ollama API mode, the model's full context is used automatically.

### Sub-agents falling back to cloud

Known OpenClaw issue (GitHub #7211): sub-agent inference may not pick up local Ollama models. Workaround: explicitly set the model in your agent config rather than relying on auto-selection.

---

## What's Next

- **Adaptive capacity learning** (coming soon) — Herd will learn when your MacBook is being used for work and automatically reduce its availability, then ramp back up when you're away. Your agent pipeline won't interfere with your Zoom calls.
- **Per-session routing hints** — allow OpenClaw skills to suggest preferred models or device tiers via request headers.
- **OpenClaw skill for fleet management** — a ClawHub skill that lets you manage your Herd fleet from within OpenClaw itself (check node status, pull models, view traces).

---

*Point OpenClaw at Herd. Let your whole fleet work for your agents.*
