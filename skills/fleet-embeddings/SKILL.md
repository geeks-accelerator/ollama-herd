---
name: fleet-embeddings
description: Generate embeddings across your device fleet for RAG, semantic search, and similarity matching. Fleet-routed via Ollama embedding models with automatic load balancing. Batch embed thousands of documents across nodes instead of bottlenecking on one machine. Use when the user needs to create embeddings, build a knowledge base, or set up semantic search.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"search","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Fleet Embeddings

You're helping someone generate embeddings — converting text into vectors for semantic search, RAG pipelines, duplicate detection, or recommendation systems. Instead of hitting one Ollama instance, the fleet distributes embedding requests across all available nodes automatically.

## Why fleet embeddings matter

Building a RAG knowledge base means embedding thousands of document chunks. On a single machine, embedding 10,000 chunks takes significant time and blocks LLM inference. With fleet routing, embedding requests spread across nodes — the machine that's least busy handles each batch, and LLM inference continues uninterrupted on other nodes.

Same Ollama embedding models you already know. Same API. Just faster because the fleet parallelizes it.

## Get started

```bash
pip install ollama-herd
herd                        # start the router (port 11435)
herd-node                   # start on each device
ollama pull nomic-embed-text  # pull an embedding model
```

No feature toggle needed — embeddings route through Ollama automatically.

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Generate embeddings

### Ollama format (curl)

```bash
curl http://localhost:11435/api/embeddings -d '{
  "model": "nomic-embed-text",
  "prompt": "The fleet manages all inference routing"
}'
```

### OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.embeddings.create(
    model="nomic-embed-text",
    input="The fleet manages all inference routing",
)
vector = response.data[0].embedding
print(f"Dimensions: {len(vector)}")
```

### Python (httpx)

```python
import httpx

def embed(text, model="nomic-embed-text"):
    resp = httpx.post(
        "http://localhost:11435/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]

vector = embed("search query here")
```

### Batch embedding for RAG

```python
import httpx

def embed_batch(texts, model="nomic-embed-text"):
    """Embed a list of texts. Fleet distributes across nodes."""
    vectors = []
    for text in texts:
        resp = httpx.post(
            "http://localhost:11435/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        vectors.append(resp.json()["embedding"])
    return vectors

# Embed document chunks for RAG
chunks = [
    "Introduction to fleet management...",
    "The scoring engine uses 7 signals...",
    "Context protection prevents model reloads...",
]
vectors = embed_batch(chunks)
print(f"Embedded {len(vectors)} chunks, {len(vectors[0])} dimensions each")
```

### Available embedding models

Check what's available:

```bash
curl -s http://localhost:11435/api/tags | python3 -c "
import json, sys
for m in json.load(sys.stdin)['models']:
    if 'embed' in m['name'].lower() or 'nomic' in m['name'].lower():
        print(f'  {m[\"name\"]}')"
```

Common models: `nomic-embed-text`, `mxbai-embed-large`, `all-minilm`, `snowflake-arctic-embed`.

Pull a model if needed:

```bash
curl -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "nomic-embed-text", "node_id": "your-node-id"}'
```

### Usage analytics

Tag embedding requests to track per-project usage:

```python
resp = httpx.post(
    "http://localhost:11435/api/embeddings",
    json={
        "model": "nomic-embed-text",
        "prompt": text,
        "metadata": {"tags": ["my-rag-pipeline", "indexing"]},
    },
)
```

## Also available on this fleet

### LLM inference

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss:120b","messages":[{"role":"user","content":"Hello"}]}'
```

Drop-in OpenAI SDK compatible. 7-signal scoring routes to the optimal node.

### Image generation

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a sunset","width":1024,"height":1024,"steps":4}'
```

Requires `FLEET_IMAGE_GENERATION=true`. Uses mflux (MLX-native Flux).

### Speech-to-text

```bash
curl -s http://localhost:11435/api/transcribe \
  -F "audio=@recording.wav" | python3 -m json.tool
```

Requires `FLEET_TRANSCRIPTION=true`. Uses Qwen3-ASR.

## Monitoring

```bash
# Fleet health and model recommendations
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Per-app usage (see which projects use the most tokens)
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — embedding requests flow through the same queues as LLM requests.

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — complete reference for all 4 model types.

[Request Tagging Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/request-tagging-analytics.md) — tag requests for per-project analytics.

## Guardrails

- Never delete or modify files in `~/.fleet-manager/`.
- Never pull or delete models without user confirmation.
- If embedding model not available, suggest: `ollama pull nomic-embed-text`.
- If router not running, suggest: `herd` or `uv run herd`.
