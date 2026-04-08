# Quickstart

From zero to routed inference in 60 seconds.

## Prerequisites

- Two or more machines on the same local network
- [Ollama](https://ollama.com) installed on each machine
- At least one model pulled (e.g., `ollama pull llama3.2:3b`)
- Python 3.10+ on the router machine

## Step 1: Install Ollama Herd

On the machine you want as the router (typically your most powerful device):

```bash
pip install ollama-herd
```

## Step 2: Start the Router

```bash
herd
```

The router starts on port 11435. You'll see:

```
Ollama Herd ready on port 11435
```

## Step 3: Start Node Agents

On each device running Ollama (including the router machine if it also runs Ollama):

```bash
pip install ollama-herd
herd-node
```

The node discovers the router automatically via mDNS:

```
Discovered router at 10.0.0.100:11435
Heartbeat sent: 2 models loaded, 128GB available
```

> **Can't use mDNS?** Connect directly: `herd-node --router-url http://10.0.0.100:11435`

## Step 4: Verify the Fleet

Check that nodes are online:

```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

You should see your nodes listed with their models, memory, and status.

Or open the dashboard in your browser:

```
http://localhost:11435/dashboard
```

## Step 5: Send Your First Request

**OpenAI format:**

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.2:3b",
    "messages": [{"role": "user", "content": "Hello from the fleet!"}],
    "stream": false
  }'
```

**Ollama format:**

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "llama3.2:3b",
  "messages": [{"role": "user", "content": "Hello from the fleet!"}],
  "stream": false
}'
```

The router scores all available nodes and routes the request to the best one. Check the response headers to see which node handled it:

```bash
curl -v http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Which node am I on?"}], "stream": false}' \
  2>&1 | grep X-Fleet
```

```
< X-Fleet-Node: mac-studio-ultra
< X-Fleet-Score: 85
```

## Step 6: Use with Your Tools

Point any OpenAI-compatible tool at the router — no code changes needed:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.2:3b",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

Replace `localhost` with the router's LAN IP if connecting from another machine.

## What Just Happened

1. Your node agents discovered the router via mDNS
2. Each node sends heartbeats every 5 seconds with system state (CPU, memory, thermal, loaded models)
3. Your request hit the router, which scored all nodes on 7 signals
4. The highest-scoring node received the request through its dedicated queue
5. The response streamed back through the router to your client

## Next Steps

- **[Core Concepts](concepts.md)** — Understand the mental model behind scoring, queues, and capacity
- **[Integrations](integrations.md)** — Connect Open WebUI, LangChain, CrewAI, and other tools
- **[Deployment](deployment.md)** — Production setup, monitoring, and tuning
- **Dashboard** — Open `http://localhost:11435/dashboard` to see your fleet in real time

## Upgrading

```bash
pip install --upgrade ollama-herd
```

Restart the router and node agents after upgrading. See [CHANGELOG](https://github.com/geeks-accelerator/ollama-herd/blob/main/CHANGELOG.md) for what's new.
