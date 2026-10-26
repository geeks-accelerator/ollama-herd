# Two models, strictly alternating: what a swap costs

**Status:** harness ready (`model-alternation-benchmark.py`), smoke-tested 2026-10-26. The real runs
(arms 1 to 5 below) are not done yet; they need two fleet machines and llama-swap installed.

## Question

"I use two models that don't fit in memory together. What does each request cost, and does a
smarter swapper fix it, or does placement?" Three ways to run more models than fit on one machine
(Ollama, llama-swap, llama.cpp router mode) all have to unload one model to load the other. Two
machines behind a router keep both loaded. This measures the difference instead of asserting it.

## Method

- **Models:** A and B, each about 55 to 60% of one machine's GPU memory at the chosen context, so
  they cannot both stay loaded on one machine. Same GGUF files on every arm.
- **Load:** 20 sequential requests, strictly A, B, A, B. Fixed short prompt, 128 output tokens,
  temperature 0, streaming. One client, no concurrency: this isolates the swap.
- **Recorded per request:** time to first token, total time, completion tokens, and on Herd the
  `X-Fleet-Node` header (which machine served it).
- **Reported:** per model, the first request's TTFT (a cold load everywhere) and the median and max
  TTFT after it. On a swapping arm every request after the first is cold; on a placement arm none
  are.

## Arms

| # | Setup | `--url` |
|---|---|---|
| 1 | One machine, Ollama defaults | `http://localhost:11434/v1/chat/completions` |
| 2 | One machine, llama-swap + llama-server, one model at a time | `http://localhost:8080/v1/chat/completions` |
| 3 | One machine, llama.cpp router mode, `--models-max 1` (then the default 4, to see what happens when the count cap allows more than memory holds) | `http://localhost:8080/v1/chat/completions` |
| 4 | Two machines, A on one and B on the other, behind Ollama Herd (Ollama nodes) | `http://<router>:11435/v1/chat/completions` |
| 5 | Two machines, same placement, llama-swap with B as a peer | `http://<llama-swap>:8080/v1/chat/completions` |

```bash
python3 docs/experiments/model-alternation-benchmark.py \
    --url http://localhost:11434/v1/chat/completions \
    --models deepseek-r1:70b qwen2.5:72b --requests 20 \
    --label "arm 1: Ollama 0.40.x, M3 Ultra 512GB, OLLAMA_MAX_LOADED_MODELS=1" \
    --out docs/experiments/model_alternation_arm1.json
```

Record every version in `--label` (Ollama, llama-swap, llama.cpp build, Herd) and the hardware.
Results go next to this file as `model_alternation_arm<N>.json`; the site's charts read them from
a release tag, so commit them before the release that publishes them.

## Expected, and what would change the conclusion

Arms 1 to 3 should all pay a cold load per swap, differing only by engine load speed and the OS
page cache. Arms 4 and 5 should stay warm, and should match each other: if arm 5 equals arm 4, the
write-up says so. The honest conclusion is "the fix is placement, not a better swapper", which
claims nothing about llama-swap that it was not built for. If arm 2 or 3 stays warm, the models
were too small to force a swap: pick bigger ones and rerun.

## Smoke test (2026-10-26, M4 Max 128 GB, Ollama 0.40.3)

`llama3.2:3b` and `qwen3:8b`, 8 requests. Not the experiment (both models are small and stay in the
page cache), only proof the harness separates the two cases:

| Setting | TTFT after the first request |
|---|---|
| `OLLAMA_MAX_LOADED_MODELS=1` (forced swap) | 1.2 to 1.6 s, every request |
| `OLLAMA_MAX_LOADED_MODELS=2` (both resident) | 15 ms (`llama3.2:3b`), 44 ms (`qwen3:8b`) |
