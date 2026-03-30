---
name: local-transcription
description: Transcribe audio files locally using Qwen3-ASR across your device fleet. Fleet-routed speech-to-text with queue management, dashboard visibility, and automatic node selection. Supports WAV, MP3, M4A, FLAC. Use when the user wants to transcribe meetings, voice notes, podcasts, or any audio file without sending data to cloud APIs.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"microphone","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Local Transcription

You're helping someone transcribe audio files — meetings, voice memos, podcast episodes, phone recordings — without sending anything to the cloud. Every audio file stays on their devices. The fleet picks the best node to handle each transcription automatically.

## Why local transcription matters

Cloud transcription APIs charge per minute and send your audio to third-party servers. Meeting recordings contain sensitive business discussions. Voice notes contain personal thoughts. Podcast interviews contain unreleased content. None of that should leave your network.

This skill routes transcription requests across your fleet of devices. If one machine is busy transcribing a 3-hour recording, the next request goes to a different device. Queue management, health monitoring, and dashboard visibility — same infrastructure you'd get from a cloud API, running entirely on your hardware.

## Get started

```bash
pip install ollama-herd
herd                                    # start the router (port 11435)
herd-node                               # start on each device
uv tool install "mlx-qwen3-asr[serve]" --python 3.14  # install STT model
```

Enable transcription:

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"transcription": true}'
```

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Transcribe audio

### curl

```bash
curl -s http://localhost:11435/api/transcribe \
  -F "audio=@meeting-recording.wav" | python3 -m json.tool
```

### Python

```python
import httpx

def transcribe(audio_path):
    with open(audio_path, "rb") as f:
        resp = httpx.post(
            "http://localhost:11435/api/transcribe",
            files={"audio": (audio_path, f)},
            timeout=300.0,
        )
    resp.raise_for_status()
    result = resp.json()
    return result["text"]

text = transcribe("meeting.wav")
print(text)
```

### With timestamps

```python
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
        print(f"[{chunk['start']:.1f}s - {chunk['end']:.1f}s] {chunk['text']}")
    return result
```

### Response format

```json
{
  "text": "Hello, this is a test of the transcription system.",
  "language": "English",
  "chunks": [
    {
      "text": "Hello, this is a test of the transcription system.",
      "start": 0.0,
      "end": 3.2,
      "chunk_index": 0,
      "language": "English"
    }
  ]
}
```

### Supported audio formats

WAV, MP3, M4A, FLAC, MP4, OGG — any format FFmpeg supports. WAV files get a ~25% speed boost via native fast-path.

### Response headers

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Which device transcribed the audio |
| `X-Fleet-Model` | Transcription model used (qwen3-asr) |
| `X-Transcription-Time` | Processing time in milliseconds |

### Model

Qwen3-ASR — state-of-the-art open-source speech recognition in 2026. ~5% word error rate, runs natively on Apple Silicon via MLX. The 0.6B model uses ~1.2GB memory and transcribes at 0.08x real-time factor (a 10-minute recording transcribes in ~48 seconds).

## Also available on this fleet

The same router handles three other AI workloads. All endpoints are at `http://localhost:11435`:

### LLM inference

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss:120b","messages":[{"role":"user","content":"Hello"}]}'
```

Drop-in OpenAI SDK compatible. Point any client at `http://localhost:11435/v1`.

### Image generation

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a sunset","width":1024,"height":1024,"steps":4}'
```

Requires `FLEET_IMAGE_GENERATION=true`. Uses mflux (MLX-native Flux).

### Embeddings

```bash
curl http://localhost:11435/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"search query"}'
```

Routes through Ollama's embedding models automatically.

## Monitoring

```bash
# Transcription stats (last 24h)
curl -s http://localhost:11435/dashboard/api/transcription-stats | python3 -m json.tool

# Fleet health (includes STT activity and expansion recommendations)
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — transcription queues show with [STT] badge alongside LLM and image queues.

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — complete reference for all 4 model types with Python, JavaScript, and curl examples.

## Guardrails

- Never delete or modify audio files provided by the user.
- Never send audio data to external services — all transcription is local.
- Never delete or modify files in `~/.fleet-manager/`.
- If transcription fails, suggest checking node logs: `tail ~/.fleet-manager/logs/herd.jsonl`.
- If no STT models available, suggest installing: `uv tool install "mlx-qwen3-asr[serve]" --python 3.14`.
