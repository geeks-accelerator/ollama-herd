# MLX-LM + Q8 KV Cache — Hackathon Experiment

**Date**: 2026-04-22
**Hardware**: Mac Studio M3 Ultra 512GB (Apple Silicon)
**Goal**: Determine whether a patched `mlx_lm.server` with Q8 KV cache quantization can match or beat Ollama's tuned llama.cpp backend on a realistic Claude Code workload.

**Upstream status**: Two open PRs already implement this exact patch — [ml-explore/mlx-lm#934](https://github.com/ml-explore/mlx-lm/pull/934) (lichengzhe, approved by contributor, not merged) and [ml-explore/mlx-lm#1073](https://github.com/ml-explore/mlx-lm/pull/1073) (deceptech-packet-ninja, more complete — handles `BatchQuantizedKVCache` edge case, closes [#1043](https://github.com/ml-explore/mlx-lm/issues/1043)). We opted not to submit a duplicate PR and instead [commented on #1073 with this benchmark data](https://github.com/ml-explore/mlx-lm/pull/1073#issuecomment-4299866597) to push the merge. Once either PR lands upstream, our local patch (`mlx-lm-server-kv-bits.patch`) becomes unnecessary — just upgrade `mlx_lm` to the release that includes it.

## Motivation

Earlier testing showed Ollama (llama.cpp engine with `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`) was faster than raw `mlx_lm.server` on `qwen3-coder:30b`:

- Ollama median TTFT: **306ms**
- Raw MLX median TTFT: **422ms** (30% slower)

But MLX supports the same tuning — `mlx_lm.generate` CLI has `--kv-bits` / `--kv-group-size` / `--quantized-kv-start`, and flash attention is automatic in MLX's Metal kernels. `mlx_lm.server` just didn't expose the flags.

## Patch

Added three CLI args to `mlx_lm/server.py` and forwarded them to the `stream_generate` call. ~40 lines total. Saved as [`mlx-lm-server-kv-bits.patch`](./mlx-lm-server-kv-bits.patch).

```
--kv-bits 8
--kv-group-size 64
--quantized-kv-start 0
```

**Enhanced with two safeguards learned from upstream PRs after initial benchmark:**

1. `choices=[4, 8]` constraint on `--kv-bits` (from [PR #934](https://github.com/ml-explore/mlx-lm/pull/934)) — MLX only supports 4-bit and 8-bit quantization. Without this, `--kv-bits 3` is accepted by argparse and fails at runtime with a cryptic error.

2. `_is_batchable` returns `False` when `kv_bits` is set (from [PR #1073](https://github.com/ml-explore/mlx-lm/pull/1073)) — `BatchQuantizedKVCache` does not exist yet in MLX, so continuous batching must be disabled when KV quantization is active. Without this guard, running with `--decode-concurrency > 1 --kv-bits 8` crashes. Our initial benchmark was sequential and didn't hit it; production use would.

Both are verified in the local patch and reflected in [`mlx-lm-server-kv-bits.patch`](./mlx-lm-server-kv-bits.patch).

## Methodology

Simulated 25-turn Claude Code session. Each turn appends ~500 tokens of "tool result" (fake Python file content) plus a new user question. Measured TTFT (time-to-first-token) on streaming responses.

Three configs tested on identical hardware, identical model (`qwen3-coder-30b-a3b` 4-bit), identical context (262144):

1. **MLX default (f16 KV)** — out-of-the-box `mlx_lm.server`
2. **MLX + Q8 KV (patched)** — our patched `mlx_lm.server --kv-bits 8`
3. **Ollama** — `qwen3-coder:30b` via Ollama 0.20.4 with `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0`

## Results

| Config | Median TTFT | Mean TTFT | Max TTFT |
|---|---|---|---|
| MLX default (f16 KV) | 422ms | 517ms | 1250ms |
| **MLX + Q8 KV (patched)** | **320ms** | **328ms** | 539ms |
| **Ollama (llama.cpp + FA + Q8)** | **306ms** | **326ms** | 509ms |

**MLX+Q8 closes the gap to ~4% of Ollama — within measurement noise.** Neither shows TTFT growth across 25 turns (both have working prefix caching).

### Per-turn data (first/last/median excerpts)

| Turn | MLX+Q8 | Ollama |
|---|---|---|
| 1 | 539 | 281 |
| 5 | 279 | 265 |
| 10 | 296 | 292 |
| 15 | 330 | 306 |
| 20 | 373 | 336 |
| 25 | 382 | 340 |

Both configurations stay flat through 50-message conversations. **MLX+Q8 max latency (539ms) is tighter than Ollama's (509ms + one outlier at 509ms on turn 23).**

## Insight

**The KV cache quantization was the missing piece.** Adding `--kv-bits 8` closed a 100ms gap. Flash attention was already enabled automatically in MLX's Metal kernels.

The performance characteristics are essentially equivalent. The choice between Ollama and patched-MLX comes down to architectural concerns, not speed:

| Dimension | Winner |
|---|---|
| Median TTFT | Ollama by 14ms (noise) |
| Max TTFT | MLX+Q8 by 30ms (noise) |
| **3-model concurrent cap** | **MLX+Q8** (no cap — independent process per model) |
| **MAX_LOADED_MODELS env reliability** | **MLX+Q8** (Ollama's env is silently ignored) |
| **Operational maturity** | **Ollama** (battle-tested, mature tooling) |
| **Tool-use / Anthropic route** | **Ollama** (more model coverage, more stable) |
| **Model registry / CLI UX** | **Ollama** (`ollama pull`, `ollama list`) |

## Recommendation

**For ollama-herd's Claude Code integration**: stay on Ollama. Speed is equivalent, operational surface is better.

**Consider patched MLX as a second-tier backend** when:
1. You need more than 3 models hot simultaneously on a single node (MLX bypasses the cap)
2. You want to experiment with speculative decoding, vision models, or other MLX-first features
3. Building a `herd-mlx-node` as an alternative node implementation makes sense long-term

## Next steps (post-hackathon)

1. **Submit the patch upstream** to `ml-explore/mlx-lm`. It's ~30 lines, pure plumbing, closes a legitimate gap between the CLI and server.
2. **Implement `herd-mlx-node`** that can coexist with `herd-node` for heterogeneous fleets.
3. **Expand this benchmark** with: tool-calling workloads (not just text completion), mixed model sizes, concurrent multi-session stress.

## Files

- Patch: [`mlx-lm-server-kv-bits.patch`](./mlx-lm-server-kv-bits.patch)
- Benchmark scripts: `/tmp/mlx-experiment/benchmark.py` (HTTP-based, used for this experiment)
- Raw results: `/tmp/mlx-experiment/mlx_q8_server_results.json`, `/tmp/mlx-experiment/ollama_results.json`
