# Ollama Herd: Agent Setup Guide

One fleet endpoint for all your AI workloads — LLM inference, image generation, speech-to-text, and embeddings. This guide tells you everything you need to configure your agent to use Ollama Herd instead of calling Ollama, mflux, or cloud APIs directly.

## Fleet endpoint

```
http://localhost:11435
```

This is the only URL your agent needs. All four model types route through it. The router handles node selection, load balancing, failover, and monitoring automatically.

## What's available

| Capability | Endpoint | What it does |
|------------|----------|-------------|
| **LLM inference** | `/v1/chat/completions` or `/api/chat` | Text generation, reasoning, code, chat |
| **Image generation** | `/api/generate-image` | Generate images from text prompts |
| **Speech-to-text** | `/api/transcribe` | Transcribe audio files to text |
| **Embeddings** | `/api/embeddings` | Convert text to vectors for search/RAG |
| **Model list** | `/api/tags` or `/v1/models` | See all available models |
| **Fleet status** | `/fleet/status` | Node health, queue depths, loaded models |

## Prerequisites

Ollama Herd must be running on the network. If it's not, install and start it:

```bash
pip install ollama-herd
herd              # start the router (port 11435)
herd-node         # start on each device with GPUs
```

Verify it's running:

```bash
curl -s http://localhost:11435/api/tags | head -5
```

---

## 1. LLM Inference

Drop-in replacement for OpenAI and Ollama APIs. Point your existing client at `http://localhost:11435` instead of `http://localhost:11434` (Ollama) or `https://api.openai.com`.

### OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:11435/v1",
    api_key="not-needed",  # Herd doesn't require auth
)

response = client.chat.completions.create(
    model="gpt-oss:120b",       # any model loaded on any node
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
for chunk in response:
    content = chunk.choices[0].delta.content or ""
    print(content, end="")
```

### Ollama format (curl)

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "gpt-oss:120b",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}'
```

### Ollama format (Python httpx)

```python
import httpx

resp = httpx.post("http://localhost:11435/api/chat", json={
    "model": "gpt-oss:120b",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": False,
}, timeout=120.0)
result = resp.json()
print(result["message"]["content"])
```

### OpenAI format (curl)

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss:120b",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

### With request tags (for analytics)

Tag your requests so you can see per-app analytics in the dashboard:

```python
response = client.chat.completions.create(
    model="gpt-oss:120b",
    messages=[{"role": "user", "content": "Hello"}],
    extra_body={"metadata": {"tags": ["my-agent", "reasoning"]}},
)
```

Or via header (when you can't modify the body):

```bash
curl -H "X-Herd-Tags: my-agent, production" \
  http://localhost:11435/api/chat -d '{"model":"gpt-oss:120b","messages":[...]}'
```

### List available LLM models

```bash
curl -s http://localhost:11435/api/tags | python3 -c "
import json, sys
for m in json.load(sys.stdin)['models']:
    print(f'  {m[\"name\"]}')"
```

### Response headers

Every response includes fleet metadata:

| Header | Example | Description |
|--------|---------|-------------|
| `X-Fleet-Node` | `Neons-Mac-Studio` | Which device handled the request |
| `X-Fleet-Score` | `85` | Routing score (higher = better fit) |

---

## 2. Image Generation

Generate images using mflux (MLX-native Flux models) on any node with the model installed.

### Check if enabled

```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'image_generation: {d[\"config\"][\"toggles\"][\"image_generation\"]}')"
```

If disabled, enable it:

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"image_generation": true}'
```

### Generate an image (curl)

```bash
curl -o output.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-image-turbo",
    "prompt": "a robot painting a sunset on a beach",
    "width": 1024,
    "height": 1024,
    "steps": 4
  }'
```

### Generate an image (Python)

```python
import httpx

def generate_image(prompt, width=1024, height=1024, output_path="output.png"):
    resp = httpx.post(
        "http://localhost:11435/api/generate-image",
        json={
            "model": "z-image-turbo",
            "prompt": prompt,
            "width": width,
            "height": height,
            "steps": 4,
            "quantize": 8,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)

    node = resp.headers.get("X-Fleet-Node", "unknown")
    time_ms = resp.headers.get("X-Generation-Time", "?")
    return {"node": node, "time_ms": time_ms, "path": output_path}
```

### Generate an image (JavaScript)

```javascript
async function generateImage(prompt, width = 1024, height = 1024) {
  const resp = await fetch("http://localhost:11435/api/generate-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "z-image-turbo",
      prompt,
      width,
      height,
      steps: 4,
    }),
  });
  if (!resp.ok) throw new Error((await resp.json()).error);
  return Buffer.from(await resp.arrayBuffer());
}
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `model` | (required) | `z-image-turbo`, `flux-dev`, or `flux-schnell` |
| `prompt` | (required) | Text description of the image |
| `width` | `1024` | Image width in pixels |
| `height` | `1024` | Image height in pixels |
| `steps` | `4` | Inference steps (4 is optimal for z-image-turbo) |
| `seed` | random | Integer seed for reproducible output |
| `negative_prompt` | `""` | What to avoid |
| `quantize` | `8` | Quantization level (3-8) |

### Response

- **200 OK**: Raw PNG bytes. `Content-Type: image/png`
- **404**: Model not available on any node
- **502**: Generation failed (check node logs)
- **503**: Image generation is disabled

### Performance

| Resolution | Time (M3 Ultra) | File size |
|-----------|-----------------|-----------|
| 512x512 | ~7s | 100-400 KB |
| 1024x1024 | ~18s | 1-2 MB |

---

## 3. Speech-to-Text (Transcription)

Transcribe audio files using Qwen3-ASR on any node with mlx-qwen3-asr installed.

### Check if enabled

```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'transcription: {d[\"config\"][\"toggles\"][\"transcription\"]}')"
```

If disabled, enable it:

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"transcription": true}'
```

### Transcribe audio (curl)

```bash
curl -s http://localhost:11435/api/transcribe \
  -F "audio=@recording.wav" | python3 -m json.tool
```

### Transcribe audio (Python)

```python
import httpx

def transcribe(audio_path):
    with open(audio_path, "rb") as f:
        resp = httpx.post(
            "http://localhost:11435/api/transcribe",
            files={"audio": (audio_path, f)},
            timeout=300.0,  # long audio may take minutes
        )
    resp.raise_for_status()
    result = resp.json()
    return result["text"]

# Usage
text = transcribe("meeting-recording.wav")
print(text)
```

### Transcribe with segments (Python)

```python
import httpx

def transcribe_with_timestamps(audio_path):
    with open(audio_path, "rb") as f:
        resp = httpx.post(
            "http://localhost:11435/api/transcribe",
            files={"audio": (audio_path, f)},
            timeout=300.0,
        )
    resp.raise_for_status()
    result = resp.json()

    for chunk in result.get("chunks", []):
        start = chunk["start"]
        end = chunk["end"]
        text = chunk["text"]
        print(f"[{start:.1f}s - {end:.1f}s] {text}")

    return result

# Usage
result = transcribe_with_timestamps("podcast-episode.mp3")
```

### Transcribe audio (JavaScript)

```javascript
async function transcribe(audioPath) {
  const fs = require("fs");
  const FormData = require("form-data");

  const form = new FormData();
  form.append("audio", fs.createReadStream(audioPath));

  const resp = await fetch("http://localhost:11435/api/transcribe", {
    method: "POST",
    body: form,
  });
  if (!resp.ok) throw new Error((await resp.json()).error);
  return await resp.json();
}
```

### Supported audio formats

WAV, MP3, M4A, FLAC, MP4, OGG — any format FFmpeg supports. WAV files get a ~25% speed boost via native fast-path.

### Response format

```json
{
  "text": "Full transcription text...",
  "language": "English",
  "chunks": [
    {
      "text": "Hello, this is a test.",
      "start": 0.0,
      "end": 2.5,
      "chunk_index": 0,
      "language": "English"
    }
  ]
}
```

### Response headers

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Which device transcribed the audio |
| `X-Fleet-Model` | Which model was used |
| `X-Transcription-Time` | Processing time in milliseconds |

### Errors

| Status | Meaning |
|--------|---------|
| `200` | Success — body is JSON with text and chunks |
| `404` | No transcription models available on any node |
| `502` | Transcription failed on the selected node |
| `503` | Transcription is disabled |

---

## 4. Embeddings

Convert text to vectors for semantic search, RAG, and similarity. Routes through Ollama's embedding models.

### Generate embeddings (curl)

```bash
curl http://localhost:11435/api/embeddings -d '{
  "model": "nomic-embed-text",
  "prompt": "The fleet manages all inference"
}'
```

### Generate embeddings (Python)

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

# Usage
vector = embed("search query here")
print(f"Dimensions: {len(vector)}")
```

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.embeddings.create(
    model="nomic-embed-text",
    input="The fleet manages all inference",
)
vector = response.data[0].embedding
```

### Available embedding models

Check what's loaded:

```bash
curl -s http://localhost:11435/api/tags | python3 -c "
import json, sys
for m in json.load(sys.stdin)['models']:
    if 'embed' in m['name'].lower():
        print(f'  {m[\"name\"]}')"
```

Common embedding models: `nomic-embed-text`, `mxbai-embed-large`, `all-minilm`.

---

## Monitoring your agent's usage

### Tag your requests

Add `metadata.tags` to every request so you can track per-agent analytics:

```python
# Every LLM call from your agent should include tags
response = client.chat.completions.create(
    model="gpt-oss:120b",
    messages=messages,
    extra_body={"metadata": {"tags": ["my-agent-name", "task-type"]}},
)
```

### View your agent's stats

Open `http://localhost:11435/dashboard` → **Apps** tab to see per-tag analytics: request count, latency, tokens, error rate.

Or query via API:

```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

### Check fleet health

```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Health score: {d[\"vitals\"][\"health_score\"]}/100')
for r in d['recommendations']:
    print(f'  [{r[\"severity\"]}] {r[\"title\"]}')"
```

---

## Configuration reference

### Environment variables to check

| Variable | Default | What it controls |
|----------|---------|-----------------|
| `FLEET_IMAGE_GENERATION` | `false` | Enable `/api/generate-image` |
| `FLEET_TRANSCRIPTION` | `false` | Enable `/api/transcribe` |
| `FLEET_AUTO_PULL` | `true` | Auto-download missing LLM models |
| `FLEET_VRAM_FALLBACK` | `true` | Route to loaded model if requested model is cold |
| `FLEET_CONTEXT_PROTECTION` | `strip` | Prevent num_ctx from reloading models |

### Toggle features at runtime

```bash
# Enable everything
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"image_generation": true, "transcription": true}'

# Check current settings
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool
```

---

## Error handling patterns

### Connection refused

The router isn't running. Start it:

```bash
herd          # or: uv run herd
herd-node     # on each device
```

### 404: model not found

The model isn't loaded on any node. Check available models:

```bash
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

### 503: feature disabled

Enable the feature via settings API:

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"image_generation": true, "transcription": true}'
```

### 502: node failed

The selected node couldn't process the request. Check node logs:

```bash
tail ~/.fleet-manager/logs/herd.jsonl | grep error
```

### Timeouts

Large requests (long audio, big images) may take minutes. Set appropriate timeouts:

```python
# LLM: 120s default is fine for most requests
# Image: 120s for 1024x1024
# Transcription: 300s for long audio files
```

---

## Quick start checklist

1. Verify router is running: `curl http://localhost:11435/api/tags`
2. Enable features you need: `curl -X POST .../dashboard/api/settings -d '{"image_generation":true,"transcription":true}'`
3. Check available models: `curl http://localhost:11435/api/tags`
4. Tag your requests with `metadata.tags` for analytics
5. Point your OpenAI SDK at `http://localhost:11435/v1`
6. Monitor at `http://localhost:11435/dashboard`
