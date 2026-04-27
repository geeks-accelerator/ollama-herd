# Speculative decoding blocked by upstream mlx-lm bug

**Status:** 🟡 PARTIALLY UNBLOCKED — works on standard transformer MoE (Qwen3-Coder-30B-A3B-Instruct), still blocked on hybrid linear-attn MoE (Qwen3-Coder-Next).  Tracking [ml-explore/mlx-lm#1081](https://github.com/ml-explore/mlx-lm/issues/1081) for the latter.
**Severity:** Medium (missed perf win on the main coding model; secondary models OK)
**Filed:** 2026-04-24
**Last update:** 2026-04-27

## 2026-04-27 update — partial unblock, model-specific

Re-tested on the same pinned `mlx-lm==0.31.3` we ship in `scripts/setup-mlx.sh`.  No code changes on our side, no version bump — same wheels.  The bug is **architecture-specific**, not version-specific: the cache type mlx-lm builds depends on the model's attention layout, and only some layouts produce a trimmable cache.

| Main model | Draft | mlx-lm 0.31.3 result |
|---|---|---|
| `Qwen3-Coder-30B-A3B-Instruct-4bit` (standard MoE, full attention) | `Qwen3-1.7B-4bit` | ✅ Works.  ~94 tok/s on M3 Ultra (300-tok benchmark, no error). |
| `Qwen3-Coder-Next-4bit` (Qwen3-Next: hybrid linear-attn + MoE) | `Qwen3-1.7B-4bit` | ❌ Fails with `ValueError: Speculative decoding requires a trimmable prompt cache (got {'ArraysCache'})` at `mlx_lm/generate.py::speculative_generate_step:531`.  Same exact flags + draft as the row above. |

**Why the difference**: Qwen3-Next uses Mamba/SSM-style linear attention layers, which mlx-lm represents with `ArraysCache` instead of `KVCache`.  `ArraysCache.is_trimmable()` returns False (correct invariant — you can't trim what isn't a sliding KV).  Speculative decoding requires trimmability so rejected draft tokens can roll back, so mlx-lm refuses to run.  This isn't a bug in `is_trimmable()` (as the upstream issue title suggests) — it's a fundamental mismatch between speculative decoding's rollback requirement and linear-attn's non-trimmable state.  Fixing it upstream means either supporting cache snapshot/restore for `ArraysCache`, or building a trimmable variant for hybrid models.  Neither is a quick patch.

Earlier (2026-04-24) test runs on the 30B-A3B model also failed — most likely because we were testing on Coder-Next first (where it always fails), saw `ArraysCache`, and assumed it applied to the whole family.  The matrix in the original report below conflated "mlx-lm pre-loads ArraysCache" with "every MoE on mlx-lm uses ArraysCache", which isn't true.

### What's deployed

- **Port 11440 (`Qwen3-Coder-Next-4bit`, main coding model):** no draft.  Standard generation.
- **Port 11441 (`Qwen3-Coder-30B-A3B-Instruct-4bit`, dedicated context compactor):** `--draft-model mlx-community/Qwen3-1.7B-4bit --num-draft-tokens 4`.  Spec decoding active.
- Config in `~/.fleet-manager/env` + `~/.zshrc` `FLEET_NODE_MLX_SERVERS`.

The compactor handles every Claude Code request's pre-summarization pass, so the perf win lands on the hottest path even though the main model can't use it.

### To revisit

1. Watch upstream [#1081](https://github.com/ml-explore/mlx-lm/issues/1081) for hybrid-cache trimmability or snapshot/restore support.
2. When a Qwen3-Coder-Next variant ships with a non-hybrid attention layout (or a non-Next 80B-class coder model lands), retest spec decoding on the main coding port.
3. If we ever swap the main model away from Qwen3-Next architecture (e.g. to a standard MoE coder), enable the draft on 11440 in the same call.

### Alternative paths (still not pursued)

- **Patch mlx-lm locally for ArraysCache snapshot/restore**: out of scope.  Maintaining one patch (`--kv-bits`) is already a recurring tax (`./scripts/setup-mlx.sh` wipe on every `uv tool upgrade mlx-lm`); a second patch with deeper algorithmic implications increases break risk.
- **Run the main model on llama.cpp**: llama.cpp has mature spec decoding but Qwen3-Next MoE routing isn't a first-class llama.cpp citizen, and our whole MLX integration would need a parallel llama.cpp path.

---

## Original report (2026-04-24) — kept for history; partial fix above supersedes the "always blocked" claim

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
