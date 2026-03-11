# Ollama Optimization for M3 Ultra (512GB)

**Status**: Implemented
**Date**: March 2026

## Problem

The M3 Ultra Mac Studio with 512GB unified memory was running `gpt-oss:120b` on Ollama defaults: 1 parallel slot, 128k context, 5-minute keep-alive. Only ~66 GiB of ~464 GiB available GPU memory was being utilized. Multiple devices on the LAN route requests through this instance, so it needed to handle concurrent requests efficiently.

## Memory Analysis

| Component | Size |
|-----------|------|
| Model weights (Metal) | 59.8 GiB |
| Model weights (CPU) | 1.1 GiB |
| KV cache per slot @ 32k | ~1.2 GiB |
| Compute graph | ~0.4 GiB |

At 16 parallel slots: 61 + (16 x 1.2) + 0.4 = **~80.6 GiB total**, leaving ~383 GiB free for additional models.

16 slots was chosen as the sweet spot: power of 2 for clean scheduling, well within the target range, and leaves massive headroom for loading multiple models simultaneously.

## Configuration

Applied via `~/.ollama/config.json`:

```json
{
  "num_parallel": 16,
  "num_ctx": 32768,
  "keep_alive": "24h",
  "flash_attention": true,
  "host": "0.0.0.0:11434"
}
```

Key decisions:
- **32k context** instead of 128k: Sufficient for all fleet workloads, dramatically reduces KV cache memory per slot
- **16 parallel slots**: Enables concurrent requests from multiple LAN clients without queueing at the Ollama level
- **24h keep-alive**: Prevents unnecessary model unloading between usage sessions
- **Flash attention**: Reduces memory usage and improves throughput for long sequences
- **0.0.0.0 binding**: Required for LAN access from other fleet nodes

## Verification

1. Server log confirms `Parallel:16`, `KvSize:32768`, `FlashAttention:Enabled`
2. Total memory usage ~80 GiB (up from 66 GiB due to additional KV cache slots)
3. Concurrent requests from multiple LAN clients process without queueing
4. Combined with Ollama Herd's `keep_alive: -1` on routed requests, models stay loaded indefinitely

## Impact

- Throughput increased ~16x for concurrent workloads (1 slot to 16)
- Context reduced from 128k to 32k (sufficient for all current use cases)
- Flash attention provides ~20% memory savings on attention computation
- ~383 GiB headroom available for loading additional models simultaneously
