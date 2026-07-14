# Local LLM Model Selection for Large Unified-Memory Hardware (2026)

**Created**: 2026-06-27  
**Status**: Evidence-based — web research + benchmark data from public leaderboards  
**Hardware focus**: Apple Silicon with large unified memory (128GB–512GB), primarily M3/M4 Ultra class  
**Ollama version**: v0.30.8 (June 12, 2026)

---

## Why this matters

Most LLM selection guides are written for 24GB consumer GPUs. The math is completely different on high-memory Apple Silicon:

- A 512GB M3 Ultra can run models that require 8× H100s on NVIDIA infrastructure
- Memory bandwidth (819 GB/s on M3 Ultra) is the inference bottleneck, not parameter count
- MoE models benefit disproportionately from this: a 400B-total / 17B-active model runs at roughly the speed of a 17B dense model

This document covers the 2026 model landscape specifically for fleets with ≥128GB unified memory, where the model selection space is substantially larger than typical consumer setups.

---

## The critical architecture shift: Mixture-of-Experts

Every major 2026 open-weight flagship is a Mixture-of-Experts (MoE) model. Understanding MoE is prerequisite to making good hardware-matched decisions.

### How MoE works

A MoE model has a large pool of "expert" sub-networks. For each token, a learned router activates only a small subset (e.g., 8 out of 128). The result: a 400B-total-parameter model might activate only 17B parameters per forward pass.

On memory-bandwidth-bound hardware (all Apple Silicon), **inference throughput scales with active parameters, not total parameters.** Two models with the same active param count run at the same speed regardless of their total sizes.

### The consequence for model selection

| Model | Total params | Active params | Relative speed |
|-------|-------------|---------------|----------------|
| Dense 120B (e.g., gpt-oss:120b) | 120B | 120B | 1× |
| Llama 4 Scout | 109B | **17B** | ~7× faster |
| Llama 4 Maverick | 400B | **17B** | ~7× faster |
| Nemotron-3-Super 120B | 120B | **12B** | ~10× faster |
| Qwen3-Next 80B | 80B | **3B** | ~40× faster |
| Qwen3-Coder 480B | 480B | **35B** | ~3.4× faster |

A 400B MoE model can be simultaneously larger in parameter count AND faster at inference than a 120B dense model.

---

## 2026 benchmark landscape

### What benchmarks actually discriminate in 2026

MMLU and HumanEval have saturated — nearly all 2026 frontier models score 88%+ and 90%+ respectively. The benchmarks that still separate models are:

| Benchmark | What it tests | Why it matters |
|-----------|--------------|----------------|
| **GPQA Diamond** | PhD-level science questions | Real reasoning depth |
| **SWE-Bench Verified** | Real software engineering tasks | Agentic coding capability |
| **LiveCodeBench** | Competitive programming, contamination-resistant | Coding without data leakage |
| **AIME 2025** | Competition math | Structured multi-step reasoning |
| **Humanity's Last Exam** | Cross-domain expert questions | Breadth of expertise |
| **τ²-bench / BFCL V4** | Tool-use / function-calling | Agentic reliability |

### Baseline: models that were strong as of early 2026

| Model | MMLU | GPQA Diamond | AIME 2025 | LiveCodeBench | SWE-Verified |
|-------|------|--------------|-----------|---------------|--------------|
| DeepSeek V3 671B (2024) | 88.5 | 59.1 | ~39% | 37.6 | — |
| Qwen3 235B-A22B (2025) | 87.8 | 47.5 / 92.4†| 85.7% | 70.7 | ~76.4 |

†Qwen3 235B GPQA: 47.5% in non-thinking mode, 92.4% in thinking mode.

### 2026 open-weight models: where each lands relative to the 2025 baselines

| Model | MMLU | GPQA Diamond | AIME 2025 | LiveCodeBench | SWE-Verified | vs. DeepSeek V3 |
|-------|------|--------------|-----------|---------------|--------------|-----------------|
| **Kimi K2.6** | 92.0 | 87.6 | 99.1%† | 89.6 | 80.2 | **clearly beats** |
| **GLM-5.1** | **96** | **94** | 98% | — | 77.8 | **clearly beats** |
| **GLM-5.2** | ~96 | ~94 | — | — | — | **clearly beats** |
| **Qwen3.5 397B** | 91.0 | 88.4 | 91% | — | — | **beats** |
| **DeepSeek V4 Pro** | — | — | — | 93.5 | 80.6 | **clearly beats on coding** |
| Llama 4 Maverick | 85.5 | — | — | — | — | slightly below |
| Llama 4 Scout | 79.6 | — | — | — | — | below |

†Kimi K2 Thinking mode.

**Key insight**: Llama 4 Maverick, despite being released in 2026, scores *below* DeepSeek V3 on MMLU. It is faster and multimodal, but not a quality upgrade for reasoning tasks. For "better insights," the actual upgrades are Kimi K2.6, GLM-5.x, and Qwen3.5 397B.

---

## What's available on Ollama (June 2026)

Ollama v0.30.8 now supports 135,000+ GGUF models from Hugging Face in addition to its curated library. The MLX backend switched to be the default on Apple Silicon in v0.19, roughly doubling decode throughput on qualifying hardware. **Update Ollama before pulling new models.**

### Locally runnable — fits in ≥128GB unified memory

| Model | `ollama pull` | Size (Q4) | Active params | Best for |
|-------|--------------|-----------|---------------|----------|
| **Llama 4 Scout** | `llama4:scout` | 67 GB | 17B | Fast general inference, 10M ctx, multimodal |
| **Qwen3-Next 80B** | `qwen3-next:80b` | 50 GB | 3B | Very high throughput, same arch as Qwen3-Coder-Next |
| **Nemotron-3-Super 120B** | `nemotron-3-super:120b` | 87 GB | 12B | Multi-agent workflows, speed-optimized |
| **Devstral 2 123B** | `devstral-2:123b` | 75 GB | dense | Coding agent tasks (Mistral) |
| **Mistral Medium 3.5** | `mistral-medium-3.5:128b` | 80 GB | dense | Multimodal, instruction following (June 2026) |
| **Command A** | `command-a:111b` | 67 GB | dense | Enterprise instruction following |
| **Qwen3 235B** | `qwen3:235b-a22b` | 142 GB | 22B | Strong reasoning + thinking mode |
| **Qwen3-Coder 30B** | `qwen3-coder:30b` | 19 GB | 3B | Fast coding, already widely used |

### Locally runnable — requires ≥256GB unified memory

| Model | `ollama pull` | Size (Q4) | Active params | Notes |
|-------|--------------|-----------|---------------|-------|
| **Llama 4 Maverick** | `llama4:maverick` | 245 GB | 17B | 400B total, 128 experts, multimodal |
| **Qwen3.5 397B** | `qwen3.5` | ~214 GB | 17B | Beats Qwen3 235B and DeepSeek V3 on reasoning |
| **Qwen3-Coder 480B** | `qwen3-coder:480b` | 290 GB | 35B | Frontier coding capability locally |

### Locally runnable — requires ~450–512GB unified memory

| Model | `ollama pull` | Size (Q4) | Notes |
|-------|--------------|-----------|-------|
| **DeepSeek V3 671B** | `deepseek-v3:671b-q4_K_M` | 404 GB | 2024 model; still beats Llama 4 Maverick on benchmarks |
| **Cogito 2.1 671B** | `cogito-2.1:671b-q4_K_M` | 404 GB | May 2026 hybrid reasoning, MIT license; same size as DeepSeek V3 but newer |

### Too large to run locally — available as cloud-routed Ollama tags

These pull in seconds and route requests to managed inference. Zero local VRAM. Ideal for low-volume high-quality tasks (deep reasoning, analysis) where latency is acceptable.

| Model | `ollama pull` | Benchmark highlights |
|-------|--------------|---------------------|
| **GLM-5.2** | `glm-5.2:cloud` | MMLU 96, GPQA 94 — best open-weight on knowledge benchmarks |
| **Kimi K2.6** | `kimi-k2.6:cloud` | MMLU 92, GPQA 87.6, SWE 80.2 — best overall open-weight |
| **DeepSeek V4 Pro** | `deepseek-v4-pro:cloud` | LiveCodeBench 93.5, SWE 80.6 — best open-weight for code |
| **GLM-5.1** | `glm-5.1:cloud` | Same tier as GLM-5.2; April 2026 release |
| **Kimi K2.7 Code** | `kimi-k2.7-code:cloud` | Coding-focused Kimi variant |
| **MiniMax M3** | `minimax-m3:cloud` | 1M ctx, multimodal, SWE-Bench Pro 59.0 |
| **Nemotron-3-Ultra** | `nemotron-3-ultra:cloud` | 550B/55B active, long-context agent workflows |
| **DeepSeek V4 Flash** | `deepseek-v4-flash:cloud` | 284B/13B active; local GGUF not yet in Ollama mainline |

---

## Decision framework by use case

### High-throughput short-context inference (<2K tokens in, <500 out)

Speed is the constraint. MoE with small active params wins.

**Best options:**
1. `qwen3-next:80b` (50GB, 3B active) — highest throughput in class; ~100+ tok/s on M3 Ultra
2. `llama4:scout` (67GB, 17B active) — ~50 tok/s, 10M ctx window for edge cases
3. `nemotron-3-super:120b` (87GB, 12B active) — strong instruction following, ~80 tok/s

### High-quality reasoning / analysis (low volume, quality-first)

Quality is the constraint. Use cloud tags for the true frontier.

**Best options:**
1. `glm-5.2:cloud` — highest open-weight MMLU/GPQA scores; zero VRAM cost
2. `kimi-k2.6:cloud` — comprehensive strong performance, best SWE scores
3. `qwen3.5` locally (214GB, 17B active) — beats DeepSeek V3 and Qwen3 235B on every reasoning benchmark; no cloud dependency

### Agentic coding (tool use, code generation, repo-level tasks)

Tool-call reliability matters more than benchmark scores. Function calling + SWE-Bench Verified are the signals.

**Best options:**
1. `deepseek-v4-pro:cloud` — SWE 80.6, LiveCodeBench 93.5
2. `qwen3-coder:480b` (290GB, 35B active) — frontier coding locally if you have the memory
3. `qwen3-coder:30b` (19GB, 3B active) — fast, widely battle-tested for agentic code use
4. `devstral-2:123b` (75GB) — Mistral's dedicated coding agent

### Balanced general inference (medium volume, quality + speed)

**Best options:**
1. `llama4:scout` (67GB) — strong instruction following, multimodal, fast
2. `mistral-medium-3.5:128b` (80GB) — June 2026, multimodal, excellent instruction following
3. `qwen3:235b-a22b` (142GB, 22B active) — best quality of the 128GB-tier models, thinking mode available

---

## Hardware-specific sizing guide

### 128GB M4 Max / M3 Max

Practical usable memory after OS overhead: ~100–110GB

| Fits comfortably | Tight but works | Requires offloading |
|-----------------|-----------------|---------------------|
| llama4:scout (67GB) | qwen3:235b (142GB at q3) | llama4:maverick |
| nemotron-3-super:120b (87GB) | | |
| devstral-2:123b at q4 (75GB) | | |
| mistral-medium-3.5:128b (80GB) | | |

### 512GB M3/M4 Ultra

Practical usable memory: ~450–470GB after OS + ~50GB model overhead

Everything in the "locally runnable" sections above fits. Key tradeoffs at this tier:

- Running `llama4:maverick` (245GB) + `qwen3:235b` (142GB) simultaneously = 387GB — fits with room for OS
- Running `cogito-2.1:671b` (404GB) leaves ~50–60GB for other hot models — tight
- `qwen3-coder:480b` (290GB) + `llama4:scout` (67GB) = 357GB — fits with room

With `OLLAMA_KEEP_ALIVE=-1` (models pinned hot), plan which models need to coexist. The 671B-class models (DeepSeek V3, Cogito 2.1) effectively occupy the machine solo when hot.

---

## Quantization guidance

| Quant | Quality vs fp16 | Use when |
|-------|----------------|----------|
| Q8_0 | ~99% | You have the memory and want near-lossless; 1.75× larger than Q4 |
| Q4_K_M | ~97% | Default recommendation; best quality/size tradeoff |
| Q4_K_S | ~95% | Save ~5% over Q4_K_M when memory is tight |
| IQ4_XS | ~96% | 2026 format; slightly better than Q4_K_M at same size |
| Q2_K | ~85% | Last resort for 2-bit local runs of very large models (e.g., GLM-5 locally) |

For 2026 MoE models specifically: the routing mechanism is sensitive to quantization — Q4_K_M or better is strongly recommended. At Q2 or below, expert selection quality degrades and output coherence suffers more than raw benchmark numbers suggest.

---

## Cloud vs. local tradeoff

Ollama's cloud-routed tags (`:cloud` suffix) route requests to managed inference endpoints. For a self-hosted fleet, this means:

**Pros of cloud tags:**
- Immediate access to models too large to run locally (GLM-5.2 is 466GB on Ollama; Kimi K2.6 is ~1TB full precision)
- No VRAM allocated locally; the full local hardware stays available for other requests
- Quality ceiling is higher than any single local machine can achieve

**Cons of cloud tags:**
- Latency depends on network and endpoint load
- Requires internet connectivity; no airgapped operation
- Cost per token if the provider isn't free tier

**Practical split:** Use cloud tags for low-volume, quality-sensitive tasks (deep analysis, complex reasoning). Use local models for high-volume, latency-sensitive tasks. The herd router can split by model name, so mixing cloud and local in your model map is straightforward.

---

## Ollama infrastructure note

Ollama v0.19 (March 2026) switched to MLX as the default inference backend on Apple Silicon, replacing llama.cpp for most model classes. Observed effects:

- Decode speed roughly doubled on tested models vs. the prior llama.cpp backend
- Prompt processing (prefill) speed improved ~40%
- Memory usage is slightly lower at the same quantization level
- Affects all models served through Ollama, including models already downloaded

v0.30.8 (June 2026) extended GGUF hardware support further and added native support for new quantization formats (IQ4_XS, GGUF-V3). Updating before pulling new models is worth doing regardless of other changes.

```bash
brew upgrade ollama
```

---

## Sources

- [Ollama Library — newest additions](https://ollama.com/library?sort=newest)
- [Ollama Blog — MLX backend announcement](https://ollama.com/blog/mlx)
- [Ollama Blog — Nemotron-3-Ultra](https://ollama.com/blog/nemotron-3-ultra)
- [LLM Leaderboard 2026 — llm-stats.com](https://llm-stats.com/)
- [LLM Leaderboard — artificialanalysis.ai](https://artificialanalysis.ai/leaderboards/models)
- [BenchLM — best open source LLM 2026](https://benchlm.ai/blog/posts/best-open-source-llm)
- [Llama 4 Maverick benchmarks — Artificial Analysis](https://x.com/ArtificialAnlys/status/1908890796415414430)
- [Llama 4 Scout & Maverick M3 Ultra benchmarks — hardware-corner.net](https://www.hardware-corner.net/llama-4-on-mac-m3-ultra-speed/)
- [Kimi K2.6 local inference hardware guide](https://runaihome.com/blog/kimi-k2-local-inference-hardware-guide-2026/)
- [GLM-5.2 hardware requirements](https://www.compute-market.com/blog/glm-5-2-local-hardware-guide-2026)
- [DeepSeek V3 vs Qwen3 235B comparison — llm-stats.com](https://llm-stats.com/models/compare/deepseek-v3-vs-qwen3-235b-a22b)
- [New LLM Releases April 2026 — fazm.ai](https://fazm.ai/blog/new-llm-releases-april-2026)
- [AI Updates June 2026 — llm-stats.com](https://llm-stats.com/llm-updates)
- [Best local LLMs for Mac 2026 — insiderllm.com](https://insiderllm.com/guides/best-local-llms-mac-2026/)
- [Ollama June 2026 Update — promptquorum.com](https://www.promptquorum.com/local-llms/top-open-source-models-ollama)
- [Cohere Command A+ announcement](https://cohere.com/blog/command-a-plus)
- [Meta Llama 4 launch — VentureBeat](https://venturebeat.com/ai/metas-answer-to-deepseek-is-here-llama-4-launches-with-long-context-scout-and-maverick-models-and-2t-parameter-behemoth-on-the-way)
