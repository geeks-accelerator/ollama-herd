# A mixed workload on mixed hardware

**Status:** run 2026-11-20; results below and in `mixed_workload_results.json`.

## Question

What real herds run (ollamaherd.com/stats): a coding model, a small chat model and embeddings in use
at once, on machines of different sizes. What does one machine doing everything cost, against the
same load spread over a small mixed fleet?

## Method

- **Models:** `qwen3-coder:30b`, `qwen3.5:9b`, `nomic-embed-text`, four concurrent workers each,
  180 s, chat requests ~1,260 prompt / 200 completion tokens (`scripts/benchmark.py
  --prompt-tokens 1250 --max-tokens 200`). `qwen3.5:9b` thinks, and the router raises a capped
  reply to its thinking minimum, so its replies ran to ~1,024 tokens.
- **D2, three machines:** two NVIDIA Quadro RTX 6000 24 GB and one RTX A6000 48 GB, starting with
  the coder on the 24 GB cards and the chat and embedding models on the 48 GB one; herd decides
  from there.
- **D1, one machine:** one RTX 6000 24 GB with all three models, the coder loaded.
- Ollama 0.40.16, `OLLAMA_NUM_PARALLEL=4` in the systemd unit; no fastembed installed, so
  embeddings go through Ollama. Router at herd `main` `c786c64` (released in 0.12.10).

## Results (2026-11-20)

| | coder tok/s (TTFT p50) | qwen3.5:9b | embeddings in 180 s | failed |
|---|---|---|---|---|
| D2: three machines | **144.8** (936 ms) | 13 requests, TTFT 40 s | 11,325 at 64 ms | 0 |
| D1: one machine | 93.3 (1,746 ms) | no token in 180 s | none answered | 12 |

- On one 24 GB machine the three models cannot stay loaded together (`qwen3-coder:30b` alone takes
  ~20 GB): Ollama evicts to load the others, and the chat model and embeddings got nothing.
- On three machines each model stays where it is loaded: the coder on the 24 GB cards, the chat
  and embedding models on the 48 GB card. Every request succeeded.
- `qwen3.5:9b` is slow in both: four 1,024-token thinking replies at once on one card that also
  answers ~60 embeddings a second. It is the thinking budget, not placement.
- Two earlier attempts the same day are not results: one where the harness's own warm-up loaded
  models in the wrong places, and one before 0.12.10's fix for pre-warming that evicted busy models.
