# GLM-4.7-Flash is ~4× too slow on Ollama (glm4moelite MoE not exploited)

**Status:** `OPEN` (upstream Ollama bug — not a herd defect)
**Severity:** Medium — affects model selection / benchmarking; no correctness impact
**Discovered:** 2026-07-15, during a Tier-0 model benchmark on the local fleet
**Upstream:** [ollama/ollama#14045 — "glm-4.7-flash is slow and uses a lot of cpu"](https://github.com/ollama/ollama/issues/14045)

---

## Summary

`glm-4.7-flash` served via Ollama on the M3 Ultra runs at **~13.7 tok/s decode** and takes **~284 s/request** on a structured-extraction workload — vs `qwen3-coder:30b` at ~11.9 s. This is **not** simply "it's a slow thinking model." GLM-4.7-Flash is architecturally a **30B-A3B MoE with only 3B active params** ([zai-org/GLM-4.7-Flash](https://huggingface.co/zai-org/GLM-4.7-Flash): 64 experts, 4 routed + 1 shared active, 47 layers), so it *should* decode as fast as `qwen3-coder:30b-a3b` (also 3B active), which does **56.7 tok/s** on the same box, same Ollama. Instead Ollama serves it at the speed of the **dense** `gemma3:27b`. The 4× deficit is an Ollama backend bug: it doesn't exploit the MoE sparsity and CPU-offloads the experts (see #14045 — users report 20× regressions and high CPU despite GPU allocation).

**herd is serving and measuring correctly** — nothing in the routing/streaming path is at fault. This is logged so we don't re-diagnose it and so the fix path is recorded.

## Evidence (local trace DB, 2026-07-15, `latency.db`)

| Model | Active params | Decode tok/s | Prefill tok/s (TTFT) | Avg out tokens |
|---|---|---|---|---|
| `qwen3-coder:30b` | 3B (MoE) | **56.7** | 1381 | ~400 |
| `gpt-oss:120b` | ~5B (MoE) | 50.9 | 572 | ~250 |
| `gemma3:27b` | 27B (**dense**) | 10.8 | 130 | ~220 |
| **`glm-4.7-flash`** | **3B (MoE)** | **13.7** | 228 (51 s avg) | **~3,600** |

glm matches the **dense** 27B, not the **3B-active** MoE it actually is → Ollama is running it densely.

## Root cause — three compounding factors

1. **(Dominant, per-token) Ollama's `glm4moelite` path is buggy.** It CPU-offloads the experts / doesn't use the sparsity → 13.7 tok/s instead of the ~50 tok/s the 3B-active architecture predicts. Upstream: [#14045](https://github.com/ollama/ollama/issues/14045). herd can't fix another engine's kernels.
2. **(Token count) Interleaved thinking.** GLM-4.7-Flash reasons by default (`ollama show` → `Capabilities: thinking`), emitting ~3,600 tokens for a task where `qwen3-coder:30b` emits ~400 — 9× more generation. Real, but a token-count cost, not a per-token-speed cost.
3. **(Prefill + variance) 202,752 default context + contention.** The model defaults to a **198 K** context window → giant KV allocation → **51 s average TTFT** for 13 K prompts (qwen: 4.8 s), and it feeds the CPU-offload. Sharing the box with concurrent `gpt-oss:120b` traffic produced wild variance (one request hit a **175 s TTFT**; decode swung 10→42 tok/s purely on prompt size — 7.4 K prompt → 42.6 tok/s, 13 K → 10.6).

Wall-clock ≈ [9× tokens] × [4× slower/token from the Ollama bug] × [51 s prefill + contention].

## Fix path

- **Real fix (decode): serve glm via MLX, not Ollama.** MLX exploits the 3B-active sparsity → expected ~50–100 tok/s. MLX weights exist ([lmstudio-community/GLM-4.7-Flash-MLX-*bit](https://huggingface.co/lmstudio-community/GLM-4.7-Flash-MLX-4bit), [mlx-community/glm-4.7-flash-abliterated-8bit](https://huggingface.co/mlx-community/glm-4.7-flash-abliterated-8bit)). **Caveat — verify first:** stock `mlx-lm` has an open issue for `glm4_moe_lite` support ([ml-explore/mlx-lm#806](https://github.com/ml-explore/mlx-lm/issues/806)); users hit `Model type glm4_moe_lite not supported`. Our `mlx_lm.server` is pinned to **0.31.3** — confirm it loads glm4_moe_lite before relying on this. If not, `uv tool upgrade mlx-lm` (which **wipes the `--kv-bits` patch** → re-run `scripts/setup-mlx.sh`). Then add glm as a `FLEET_NODE_MLX_SERVERS` entry.
- **herd-side mitigation (prefill only, either backend): cap `num_ctx`.** Set glm's context to ~16–32 K via `num_ctx_overrides` / `FLEET_DYNAMIC_NUM_CTX` (the same context-waste lever we apply to gpt-oss:120b — see [#16](../issues.md) and [#21](../issues.md)). Kills most of the 51 s prefill and shrinks the KV cache feeding the CPU-offload. Does **not** fix the 13.7 tok/s decode.
- **Even on MLX, thinking still costs.** MLX removes the 4× per-token deficit but not the ~3,600 thinking tokens; expect ~60–90 s/request, not ~284 s. For a raw-speed-vs-quality split, run with thinking disabled separately.

## Not a herd action item

There is no herd code change that fixes the decode speed — it's an upstream Ollama inference bug. Track #14045; the herd-native lever is `num_ctx` capping, and the deployment fix is the MLX backend (pending the mlx-lm glm4_moe_lite check).
