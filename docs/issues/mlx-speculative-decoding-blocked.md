# Speculative decoding blocked by upstream mlx-lm bug

**Status:** Blocked on upstream (tracking [ml-explore/mlx-lm#1081](https://github.com/ml-explore/mlx-lm/issues/1081))
**Severity:** Medium (missed perf win, not a correctness issue)
**Filed:** 2026-04-24

## What we tried

`mlx_lm.server` exposes `--draft-model` and `--num-draft-tokens` for speculative decoding — a smaller model proposes tokens the main model verifies, yielding 10–30% throughput on workloads with high draft acceptance. Expected to be a user-visible win on Qwen3-Coder-Next (80B MoE / 3B active) with a 1.7B draft.

We implemented the full server-side support:
- `FLEET_NODE_MLX_DRAFT_MODEL` + `FLEET_NODE_MLX_NUM_DRAFT_TOKENS` settings in `ServerSettings`
- Wired through `node/mlx_supervisor.py::_build_cmd()` — appends `--draft-model <repo> --num-draft-tokens N` when configured
- Downloaded `mlx-community/Qwen3-1.7B-4bit` as the draft (shares Qwen3 tokenizer with the main)
- Verified the command line launches with the expected flags

Every request then failed with:

```
ValueError: Speculative decoding requires a trimmable prompt cache
(got {'ArraysCache'}).
```

at `mlx_lm/generate.py::speculative_generate_step` line 531.

## Root cause (upstream)

[ml-explore/mlx-lm#1081](https://github.com/ml-explore/mlx-lm/issues/1081), open since March 2026:

> `ArraysCache.is_trimmable()` returns True but `trim()` method doesn't exist.

`ArraysCache` is the default prompt cache type in mlx-lm 0.31.3. Speculative decoding requires a trimmable cache so rejected draft tokens can be rolled back. The type check passes the wrong answer, and the `trim()` call then explodes.

Confirmed reproduction matrix:
| Flags | Result |
|---|---|
| `--kv-bits 8` + `--draft-model` | FAIL (ArraysCache) |
| `--kv-bits 0` + `--draft-model` | FAIL (still ArraysCache — this is the default cache type) |
| Absolute-minimal flags: `--model X --port 11440 --draft-model Y` | FAIL (same) |
| `--kv-bits 8` alone (no draft) | ✅ works — our current prod config |
| `--draft-model` alone on vanilla mlx-lm (without our kv-bits patch) | FAIL (same ArraysCache) |

The bug is in upstream mlx-lm's cache-type initialization, independent of our `--kv-bits` server.py patch.

## What's in the repo now

- **Infrastructure is shipped and ready**: settings, supervisor flag plumbing, draft-model weights cached on disk (`~/.cache/huggingface/hub/models--mlx-community--Qwen3-1.7B-4bit`, ~940 MB). Tests verify the supervisor constructs the correct command line.
- **Runtime is disabled**: `FLEET_NODE_MLX_DRAFT_MODEL` is NOT set in `~/.fleet-manager/env`. Supervisor omits the flags. Standard single-model behavior.
- **`FLEET_NODE_MLX_KV_BITS=8` is re-enabled** — we temporarily disabled it while debugging the cache conflict, but since speculative is blocked regardless, kv-bits stays on (matches our observed-best config).

## How to unblock

1. **Watch upstream**: subscribe to [#1081](https://github.com/ml-explore/mlx-lm/issues/1081). Check mlx-lm releases for a fix.
2. **When upstream ships a fix**:
   - `./scripts/setup-mlx.sh` needs updating with the new pinned version (currently `0.31.3`); re-test that our `--kv-bits` patch still applies cleanly against the new server.py.
   - Add `FLEET_NODE_MLX_DRAFT_MODEL=mlx-community/Qwen3-1.7B-4bit` + `FLEET_NODE_MLX_NUM_DRAFT_TOKENS=4` to `~/.fleet-manager/env`.
   - Restart node.
   - Benchmark with `scripts/benchmark-performance.py` before/after to confirm real throughput win on this fleet's workload. If acceptance rate < 50% or steady-state latency doesn't improve, `--num-draft-tokens 2` or `--draft-model mlx-community/Qwen3-4B-Instruct-4bit` as alternatives.

## Alternative paths considered

- **Patch mlx-lm locally to fix ArraysCache.trim()**: out of scope. We already maintain the kv-bits patch; adding a second local fork patch increases setup-mlx.sh complexity + coupling to specific upstream versions. If a year passes without upstream fixing it, reconsider.
- **Use llama.cpp instead of mlx-lm for this model**: llama.cpp has better speculative-decoding maturity but we're not set up for it and Qwen3-Coder-Next MoE routing may not be as well-tuned on llama.cpp. Non-trivial pivot.
- **Wait for a larger Qwen3-Coder variant with same-family draft**: doesn't help if the upstream cache bug still applies.

## Related

- [`docs/plans/claude-code-performance-improvements.md`](../plans/claude-code-performance-improvements.md) — the plan that proposed this
- [`docs/guides/mlx-setup.md`](../guides/mlx-setup.md) — setup flow + patch management
- [`docs/experiments/mlx-lm-server-kv-bits.patch`](../experiments/mlx-lm-server-kv-bits.patch) — our existing patch, for reference
