# Integrations

Connect Ollama Herd to your existing tools. In most cases, you change one URL and everything works.

## The Pattern

Almost every AI tool supports either the OpenAI API or the Ollama API. Ollama Herd speaks both. The integration is always the same:

1. Find where the tool configures its LLM endpoint
2. Change the URL to your Herd router (`http://router-ip:11435`)
3. Done — the tool thinks it's talking to one Ollama or one OpenAI endpoint, but the fleet handles routing

Replace `router-ip` with your router's LAN IP address. If the tool runs on the same machine as the router, use `localhost`.

## OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://router-ip:11435/v1",
    api_key="not-needed",
)

response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Hello from the fleet!"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

Works for chat completions, embeddings, and image generation.

## OpenAI SDK (Node.js)

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://router-ip:11435/v1",
  apiKey: "not-needed",
});

const stream = await client.chat.completions.create({
  model: "llama3.3:70b",
  messages: [{ role: "user", content: "Hello from the fleet!" }],
  stream: true,
});

for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content || "");
}
```

## Open WebUI

Open WebUI supports multiple Ollama connections. Point it at Herd instead of a single Ollama:

1. Go to **Settings > Connections**
2. Set Ollama URL to `http://router-ip:11435`
3. Save

Open WebUI's built-in multi-instance support uses random selection. With Herd, you get 7-signal intelligent routing instead.

## LangChain

```python
from langchain_ollama import ChatOllama

llm = ChatOllama(
    base_url="http://router-ip:11435",
    model="llama3.3:70b",
)

response = llm.invoke("Hello from LangChain!")
print(response.content)
```

Or with the OpenAI-compatible interface:

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://router-ip:11435/v1",
    api_key="not-needed",
    model="llama3.3:70b",
)
```

## CrewAI

```python
from crewai import Agent, LLM

llm = LLM(
    model="ollama/llama3.3:70b",
    base_url="http://router-ip:11435",
)

agent = Agent(
    role="Researcher",
    goal="Find information",
    llm=llm,
)
```

CrewAI agents fire many concurrent requests. With a fleet, each agent's requests fan out across devices instead of queuing on one.

## OpenClaw (Claude Code)

Edit `~/.config/openclaw/openclaw.json5`:

```json5
{
  "models": {
    "providers": {
      "ollama": {
        "baseUrl": "http://router-ip:11435",
        "apiKey": "ollama-local",
        "api": "ollama",
        "models": [
          {
            "id": "llama3.3:70b",
            "name": "Llama 3.3 70B",
            "reasoning": false,
            "input": ["text"],
            "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
          }
        ]
      }
    }
  }
}
```

OpenClaw makes 5-20+ LLM calls per task. The fleet prevents these from queuing on a single machine.

## Aider

```bash
aider --model ollama/llama3.3:70b --ollama-api-base http://router-ip:11435
```

Or set the environment variable:

```bash
export OLLAMA_API_BASE=http://router-ip:11435
aider --model ollama/llama3.3:70b
```

## Continue.dev

In your Continue configuration (`.continue/config.json`):

```json
{
  "models": [
    {
      "title": "Llama 3.3 70B (Fleet)",
      "provider": "ollama",
      "model": "llama3.3:70b",
      "apiBase": "http://router-ip:11435"
    }
  ]
}
```

## LlamaIndex

```python
from llama_index.llms.ollama import Ollama

llm = Ollama(
    base_url="http://router-ip:11435",
    model="llama3.3:70b",
)

response = llm.complete("Hello from LlamaIndex!")
```

## LiteLLM

LiteLLM is a cloud API gateway. Herd sits between LiteLLM and your local Ollama fleet:

```python
import litellm

response = litellm.completion(
    model="ollama/llama3.3:70b",
    messages=[{"role": "user", "content": "Hello!"}],
    api_base="http://router-ip:11435",
)
```

This gives you local + cloud in one stack: LiteLLM routes between providers, Herd routes between local devices.

## curl

**OpenAI format:**

```bash
curl http://router-ip:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.3:70b",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

**Ollama format:**

```bash
curl http://router-ip:11435/api/chat -d '{
  "model": "llama3.3:70b",
  "messages": [{"role": "user", "content": "Hello!"}],
  "stream": false
}'
```

**Embeddings:**

```bash
curl http://router-ip:11435/api/embed -d '{
  "model": "nomic-embed-text",
  "input": "Hello from the fleet!"
}'
```

**Image generation:**

```bash
curl http://router-ip:11435/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-image-turbo",
    "prompt": "a cat sitting on a laptop",
    "size": "1024x1024"
  }'
```

## Request Tagging

Any integration can add tags for per-app analytics. Use the `X-Herd-Tags` header or the `metadata.tags` field:

```python
# OpenAI SDK with tags
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Hello!"}],
    extra_headers={"X-Herd-Tags": "my-app,production"},
)
```

```bash
# curl with tags
curl http://router-ip:11435/api/chat \
  -H "X-Herd-Tags: my-app, production" \
  -d '{"model": "llama3.3:70b", "messages": [{"role": "user", "content": "Hello!"}]}'
```

The dashboard's **Apps** tab breaks down usage, latency, and error rates per tag.

## Next Steps

- **[Quickstart](quickstart.md)** — Get the fleet running first
- **[Deployment](deployment.md)** — Production setup and monitoring
- **[API Reference](api-reference.md)** — Full endpoint documentation
