# MLX vs Ollama — Adoption Landscape & Strategic Fit (mid-2026)

**Created**: 2026-07-15
**Status**: Evidence-based — web research (Jul 2026) cross-referenced against first-party benchmark data from this fleet's 512GB M3 Ultra Mac Studio
**Related**:
- [`mlx-lm-stability-and-concurrency.md`](./mlx-lm-stability-and-concurrency.md) — MLX server operational reality (crash loops, concurrency limits)
- [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md) — long-context prefill failure mode that shapes backend routing
- [`../experiments/mlx-lm-q8kv-benchmark.md`](../experiments/mlx-lm-q8kv-benchmark.md) — the head-to-head TTFT experiment this doc cites
- [`../plans/mlx-backend-for-large-models.md`](../plans/mlx-backend-for-large-models.md) — implementation plan for the MLX backend

---

## TL;DR

1. **Ollama dwarfs MLX in adoption** by an order of magnitude (~176k vs ~27k GitHub stars, ~8.9M monthly developers vs no comparable install base). Ollama is the universal local-inference connector; MLX is an Apple-scoped, research/power-user framework with strong first-party endorsement.
2. **The "MLX is faster" claim is real but routinely overstated.** Normalized against *raw* llama.cpp, MLX's edge is ~1.4–1.8× (biggest on MoE). On **our own M3 Ultra**, tuned Ollama (Flash Attention + Q8 KV) and patched MLX+Q8 are **within measurement noise** — 306ms vs 320ms median TTFT, 43.5 vs 42.4 tok/s decode.
3. **The debate is partly settled by integration**: Ollama 0.19 (Mar 2026) adopted MLX as its *own* Apple Silicon backend for >32GB machines. The "easy runtime" is now also the "fast runtime."
4. **For ollama-herd**, a *direct* mlx-lm backend is still worth it — but the moat is narrower than the hype suggests. The real wins are escaping Ollama's Go-wrapper overhead, quant/KV control (NVFP4, q8 KV), vision/audio via mlx-vlm/mlx-audio, and hedging on Apple's M5+ Neural Accelerator roadmap. Position MLX as the **throughput/power-user tier**, not a replacement.
5. **Routing implication**: MLX's prefill/TTFT weakness at long context (30K+) is a liability for Claude Code's exact workload. The scorer should be **backend-aware** — long-prompt jobs to llama.cpp-backed nodes, short-prompt/high-throughput jobs to MLX.

---

## 1. Adoption by the numbers

| Metric | Apple MLX | Ollama |
|---|---|---|
| GitHub stars | ~27,000 (`ml-explore/mlx`) + 6.3k (`mlx-lm`) | **~176,000** (`ollama/ollama`) |
| Forks | ~2,000 (mlx) + 866 (mlx-lm) | ~17,000 |
| Latest release | v0.31.2 / v0.31.3 (Apr 22 2026) | frequent (multiple/month) |
| Release cadence | ~every 2 weeks (~73 releases) | multiple/month |
| Backing | small Apple core team (~4 eng + community) | **$65M Series B** (Jul 9 2026, Theory Ventures; $88M total) |
| User base | no comparable figure; proxied by HF hub | **~8.9M monthly devs, 85% of Fortune 500, ~1M installs/week** |
| Team size | Apple ML Research | 14 employees |

Sources: [TechCrunch, Jul 9 2026](https://techcrunch.com/2026/07/09/popular-open-source-ai-developer-tool-ollama-raises-65m-grows-to-nearly-9m-users/); [TechFundingNews](https://techfundingnews.com/14-employees-8-9m-developers-ollama-raises-65m-to-become-ais-platform-layer/); GitHub repo pages (Jun–Jul 2026 snapshots).

**Data-quality flag**: MLX star counts vary by source snapshot (~25.7k–27.5k); ~27k is current. Contributor counts for the MLX repos didn't render reliably on GitHub and aren't independently confirmed.

---

## 2. Ecosystem & integrations

**MLX** anchors a real but Apple-scoped stack:
- `mlx-lm`, `mlx-vlm`, `mlx-embeddings`, `mlx-audio`, `mlx-whisper`
- **mlx-community HF org: ~4,800 pre-converted models** ([HF docs](https://huggingface.co/docs/hub/mlx))
- LM Studio ships an MLX backend (Apple spotlighted it on M5, Mar 2026)
- **WWDC 2026: `MLXLanguageModel`** — any mlx-community model drops into the Foundation Models Swift API. A significant first-party endorsement.

**Ollama** is the de-facto integration target for local inference: Open WebUI, LangChain (`langchain-ollama`), LlamaIndex, Continue, Aider, Haystack, CrewAI, AutoGen, Dify, Tabby — most "run locally" guides default to it ([Cohorte](https://cohorte.co/blog/ollama-advanced-use-cases-and-integrations)). Its OpenAI-compatible API made it the universal local connector.

**Verdict**: breadth of downstream adoption isn't close. MLX is deeper on Apple-native/Swift and research/fine-tuning; Ollama is far broader for application integration.

---

## 3. Performance reality

The "MLX is faster than Ollama/llama.cpp on Mac" claim is **real but often overstated**, and the framing shifted in 2026.

### Third-party evidence

- **Most rigorous source**: [arXiv:2511.05502](https://arxiv.org/abs/2511.05502) (Oct 2025, M2 Ultra 192GB, Qwen-2.5, prompts to 100K) — MLX had highest sustained generation throughput; MLC-LLM lowest TTFT at moderate prompts; llama.cpp best for lightweight single-stream; Ollama best ergonomics.
- **The inflated 3× figure**: field benchmarks showing MLX ~130 tok/s vs Ollama ~43 tok/s on MoE are misleading — Ollama's Go wrapper eats ~50% of raw llama.cpp throughput. **Normalized against raw llama.cpp, MLX's real edge is ~1.4–1.8×** (biggest on MoE, ~1.4–1.6× dense) ([yage.ai, Mar 2026](https://yage.ai/share/mlx-apple-silicon-en-20260331.html)).
- **Where MLX loses — prefill/TTFT**: MLX does full prefill before emitting tokens, so TTFT rises linearly with input. At **30K+ context, llama.cpp + Flash Attention is ~50% faster**. On short-prompt chat, an M1 Max saw MLX 13 tok/s effective vs GGUF 20 tok/s (94% of MLX time in prefill).
- **Where MLX wins**: memory (7–13% less than GGUF), and increasingly **Apple's M5 Neural Accelerators**, which target MLX compute graphs (Qwen3-14B-4bit: TTFT 4.06× faster M5-vs-M4).
- **q8 KV cache**: both frameworks support Q8_0 KV quantization (halves KV memory, perplexity increase <0.1), but it requires Flash Attention and can cost ~5–10% tok/s on Metal ([Contra Collective, Jun 2026](https://contracollective.com/blog/kv-cache-quantization-q8-vs-q4-m5-max-mlx-2026)).

### First-party evidence — this fleet's M3 Ultra

Our own head-to-head ([`mlx-lm-q8kv-benchmark.md`](../experiments/mlx-lm-q8kv-benchmark.md)), a simulated 25-turn Claude Code session on `qwen3-coder-30b-a3b` 4-bit, identical context (262144):

| Config | Median TTFT | Mean TTFT | Max TTFT | Median decode |
|---|---|---|---|---|
| MLX default (f16 KV) | 422 ms | 517 ms | 1250 ms | — |
| **MLX + Q8 KV (patched)** | **320 ms** | 328 ms | 539 ms | 42.4 tok/s |
| **Ollama (llama.cpp + FA + Q8)** | **306 ms** | 326 ms | 509 ms | 43.5 tok/s |

**Key finding: on our actual hardware, with both backends tuned, MLX and Ollama are dead even** — 306 vs 320ms TTFT (~4%, within noise), 43.5 vs 42.4 tok/s decode. The MLX advantage only appears against *untuned* Ollama or *untuned* MLX (default f16 KV is 30% slower). Neither showed TTFT growth across 25 turns — both have working prefix caching.

This directly contradicts the popular framing. **Tuning matters more than backend choice** on this hardware and workload.

---

## 4. Sentiment & momentum

MLX is gaining momentum (WWDC push, M5 hardware, growing HF hub) but remains **niche vs Ollama's mainstream position**. The consensus 2026 pattern: *"most Mac users start with Ollama and switch to MLX when they need speed"*, with MLX suited to power users running 70B+ on Mac Studio.

The pivotal event: **Ollama 0.19 (Mar 30 2026) adopted MLX as its own Apple Silicon backend** (>32GB unified memory required), posting +93% decode / +57% prefill on M5 Max plus NVFP4 support ([Ollama blog](https://ollama.com/blog/mlx)). Commentary now frames the debate as "settled by integration" — the easy-mode runtime got fast, undercutting the main reason to run MLX directly.

---

## 5. Structural differences that shape adoption

| Axis | Ollama | MLX |
|---|---|---|
| Platform | Mac / Linux / Windows / NVIDIA / AMD | **Apple-only** |
| Metal proximity | llama.cpp Metal path (CUDA-pattern abstraction loss) | **closer to unified memory & future silicon** |
| UX | one-command pull/run | more code-forward (Python/Swift); `mlx-lm` ships a server |
| Models | turnkey registry | needs conversion, but ~4,800-model mlx-community hub covers most |
| Release stability | steady | ~2-week cadence → **real API churn; pin versions** |

---

## 5b. Why new architectures land faster on MLX (despite Ollama's bigger community)

This is the counterintuitive one, and it's directly load-bearing for herd's backend strategy. Ollama's ~9M developers do **not** translate into faster support for brand-new model architectures (e.g. GLM's `glm4_moe_lite`, which is 4× too slow on Ollama — see [`../issues/glm-4.7-flash-ollama-glm4moelite-slow.md`](../issues/glm-4.7-flash-ollama-glm4moelite-slow.md)). Nor is it a clean "MLX works, Ollama broken" — stock `mlx-lm` has its own open issue for that arch ([mlx-lm#806](https://github.com/ml-explore/mlx-lm/issues/806)). The real difference is **iteration speed and the barrier to a correct implementation**:

1. **Ollama's community is downstream, not upstream.** Users build apps/integrations, not inference kernels. Model-execution code lives in **llama.cpp/GGML** (a small circle of C++/Metal experts) and Ollama's own Go engine (14-person company). User volume doesn't add kernel engineers.
2. **Adding an architecture is far cheaper in MLX** — often a single Python file (~200–400 lines) vs new C++/Metal kernels + GGUF conversion + quantization compat in llama.cpp.
3. **MoE is the crux.** Correct sparse-expert-gather is one of the hardest things to get right in a low-level kernel; MLX's array framework expresses it more naturally, so novel MoE variants tend to work there sooner. The GLM bug is exactly this — Ollama runs it *densely* (all ~30B params) instead of gathering the ~3B active.
4. **Vendors ship MLX weights first.** Zhipu, Alibaba, DeepSeek increasingly bless MLX conversions on day one because porting is trivial; GGUF support waits on llama.cpp maintainers.

**Implication**: MLX's decisive advantage for herd is **model availability**, not raw speed — getting the newest architectures running correctly before Ollama does. "Lands first" ≠ "lands working," though: verify each new arch loads in the pinned `mlx-lm` before relying on it.

## 6. Strategic implications for ollama-herd

Adding an MLX backend is **sound but with a narrower moat than it appears**, precisely because Ollama itself now runs on MLX (>32GB machines). The marginal win from a *direct* mlx-lm backend:

1. **Escaping Ollama's Go-wrapper overhead** (~50% throughput loss vs raw llama.cpp) — the biggest concrete gain, strongest on MoE models and Mac Studio 70B+ fleets, exactly herd's target hardware. *(Caveat: our own benchmark shows tuned Ollama already recovers most of this on the 30B — validate per-model before assuming the gain.)*
2. **Quantization/KV control** — NVFP4, q8 KV — that let nodes hold larger models / longer contexts.
3. **Vision & audio** via mlx-vlm / mlx-audio, plus direct access to the mlx-community hub — useful given herd's existing VISION/embedding ambitions.
4. **Hardware hedge**: MLX is the framework Apple optimizes M5+ Neural Accelerators for; betting on it tracks Apple's roadmap.

### Caveats to design around

- **Prefill/TTFT weakness at long context**: herd's scorer should route long-prompt/large-context jobs to llama.cpp-backed nodes and short-prompt/high-throughput jobs to MLX. This is not hypothetical — it's the same 30K-token cliff documented in [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md).
- **MLX release churn**: pin `mlx-lm` versions; the ~2-week cadence breaks APIs. See [`mlx-lm-stability-and-concurrency.md`](./mlx-lm-stability-and-concurrency.md).
- **Concurrency limits**: `BatchQuantizedKVCache` doesn't exist yet — KV quantization forces sequential decode (continuous batching must be disabled). Factor into per-node concurrency scoring.
- **Apple-only**: a non-issue for an Apple Silicon fleet, but caps portability if herd ever targets NVIDIA nodes.

### Net recommendation

MLX as a **second, performance-tier backend is a smart bet** — position it as the throughput/power-user path alongside Ollama's compatibility path, not a wholesale replacement. But **let first-party benchmarks, not hype, drive per-model backend selection**: on our M3 Ultra, tuned Ollama matched patched MLX on the 30B. The decisive advantages are the *long tail* — larger models, tighter quantization, Apple hardware roadmap, and multimodal — not raw 30B chat speed.

---

## Sources

- [TechCrunch — Ollama raises $65M (Jul 9 2026)](https://techcrunch.com/2026/07/09/popular-open-source-ai-developer-tool-ollama-raises-65m-grows-to-nearly-9m-users/)
- [TechFundingNews — Ollama funding & metrics](https://techfundingnews.com/14-employees-8-9m-developers-ollama-raises-65m-to-become-ais-platform-layer/)
- [arXiv:2511.05502 — comparative inference study (Oct 2025)](https://arxiv.org/abs/2511.05502)
- [yage.ai — MLX vs llama.cpp normalized benchmarks (Mar 2026)](https://yage.ai/share/mlx-apple-silicon-en-20260331.html)
- [Ollama blog — MLX backend adoption (Mar 30 2026)](https://ollama.com/blog/mlx)
- [Contra Collective — Q8 vs Q4 KV cache on M5 Max MLX (Jun 2026)](https://contracollective.com/blog/kv-cache-quantization-q8-vs-q4-m5-max-mlx-2026)
- [Hugging Face — MLX hub docs (~4,800 models)](https://huggingface.co/docs/hub/mlx)
- [Cohorte — Ollama integrations survey](https://cohorte.co/blog/ollama-advanced-use-cases-and-integrations)
- First-party: [`docs/experiments/mlx-lm-q8kv-benchmark.md`](../experiments/mlx-lm-q8kv-benchmark.md), `ollama_results.json`, `mlx_q8_server_results.json`

## Data-quality notes

- MLX star counts vary by source (~25.7k–27.5k); ~27k current.
- The 3× MLX field figure is misleading unless normalized against *raw* llama.cpp (real: 1.4–1.8×).
- The arXiv study is the most methodologically solid but is Oct 2025 / M2 Ultra only.
- Several benchmark numbers come from individual blogs (M4/M5 tests) — treat as directional, not definitive.
- Our first-party benchmark is a single 25-turn run on one model (`qwen3-coder:30b`); broaden across models before generalizing the "tuned parity" conclusion.
