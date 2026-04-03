---
name: local-transcription
description: Local speech-to-text transcription with Qwen ASR — transcription routed across your Apple Silicon fleet. Transcribe meetings, voice notes, podcasts with local speech-to-text. Works like Whisper but runs locally via MLX. Fleet-routed transcription with queue management and dashboard visibility. 语音转文字 | transcripción de voz
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"microphone","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# Local Speech-to-Text Transcription

You're helping someone use speech-to-text transcription on audio files — meetings, voice memos, podcast episodes, phone recordings — without sending anything to the cloud. Every audio file stays on their devices. The fleet picks the best node to handle each speech-to-text transcription automatically.

## Why local speech-to-text transcription matters

Cloud speech-to-text transcription APIs charge per minute and send your audio to third-party servers. Meeting recordings contain sensitive business discussions. Voice notes contain personal thoughts. Podcast interviews contain unreleased content. None of that should leave your network. Local transcription keeps it private.

This skill routes speech-to-text transcription requests across your fleet of devices. If one machine is busy with a 3-hour transcription, the next speech-to-text request goes to a different device. Transcription queue management, health monitoring, and dashboard visibility — same infrastructure you'd get from a cloud speech-to-text API, running entirely on your hardware.

## Get started with speech-to-text transcription

```bash
pip install ollama-herd
herd                                    # start the transcription router (port 11435)
herd-node                               # start on each transcription device
uv tool install "mlx-qwen3-asr[serve]" --python 3.14  # install speech-to-text model
```

Enable speech-to-text transcription:

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"transcription": true}'
```

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Transcribe audio with speech-to-text

### curl — basic transcription

```bash
# Speech-to-text transcription of a meeting recording
curl -s http://localhost:11435/api/transcribe \
  -F "audio=@meeting-recording.wav" | python3 -m json.tool
```

### Python — speech-to-text transcription

```python
import httpx

def speech_to_text_transcription(audio_path):
    """Run speech-to-text transcription on an audio file."""
    with open(audio_path, "rb") as f:
        transcription_resp = httpx.post(
            "http://localhost:11435/api/transcribe",
            files={"audio": (audio_path, f)},
            timeout=300.0,
        )
    transcription_resp.raise_for_status()
    transcription_result = transcription_resp.json()
    return transcription_result["text"]

# Run speech-to-text transcription
transcription_text = speech_to_text_transcription("meeting.wav")
print(transcription_text)
```

### Speech-to-text transcription with timestamps

```python
def transcription_with_timestamps(audio_path):
    """Speech-to-text transcription returning timestamped chunks."""
    with open(audio_path, "rb") as f:
        transcription_resp = httpx.post(
            "http://localhost:11435/api/transcribe",
            files={"audio": (audio_path, f)},
            timeout=300.0,
        )
    transcription_resp.raise_for_status()
    transcription_result = transcription_resp.json()
    for transcription_chunk in transcription_result.get("chunks", []):
        print(f"[{transcription_chunk['start']:.1f}s - {transcription_chunk['end']:.1f}s] {transcription_chunk['text']}")
    return transcription_result
```

### Transcription response format

```json
{
  "transcription_text": "Hello, this is a test of the speech-to-text transcription system.",
  "language": "English",
  "transcription_chunks": [
    {
      "text": "Hello, this is a test of the speech-to-text transcription system.",
      "start": 0.0,
      "end": 3.2,
      "chunk_index": 0,
      "language": "English"
    }
  ]
}
```

### Supported audio formats for transcription

WAV, MP3, M4A, FLAC, MP4, OGG — any format FFmpeg supports. WAV files get a ~25% transcription speed boost via native fast-path.

### Speech-to-text transcription response headers

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Which device performed the speech-to-text transcription |
| `X-Fleet-Model` | Transcription model used (qwen3-asr) |
| `X-Transcription-Time` | Transcription processing time in milliseconds |

### Speech-to-text transcription model

Qwen3-ASR — state-of-the-art open-source speech-to-text transcription in 2026. ~5% word error rate, runs natively on Apple Silicon via MLX. The 0.6B transcription model uses ~1.2GB memory and transcribes at 0.08x real-time factor (a 10-minute recording completes transcription in ~48 seconds).

## Also available on this fleet

The same router handles three other AI workloads alongside speech-to-text transcription. All endpoints are at `http://localhost:11435`:

### LLM inference

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss:120b","messages":[{"role":"user","content":"Hello"}]}'
```

### Image generation

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a sunset","width":1024,"height":1024,"steps":4}'
```

### Embeddings

```bash
curl http://localhost:11435/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"search query"}'
```

## Monitoring speech-to-text transcription

```bash
# Transcription stats (last 24h)
curl -s http://localhost:11435/dashboard/api/transcription-stats | python3 -m json.tool

# Fleet health (includes speech-to-text transcription activity)
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — speech-to-text transcription queues show with [STT] badge alongside LLM and image queues.

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — complete reference for all 4 model types including speech-to-text transcription with Python, JavaScript, and curl examples.

## Guardrails

- Never delete or modify audio files provided by the user for transcription.
- Never send audio data to external services — all speech-to-text transcription is local.
- Never delete or modify files in `~/.fleet-manager/`.
- If transcription fails, suggest checking node logs: `tail ~/.fleet-manager/logs/herd.jsonl`.
- If no speech-to-text models available, suggest installing: `uv tool install "mlx-qwen3-asr[serve]" --python 3.14`.
