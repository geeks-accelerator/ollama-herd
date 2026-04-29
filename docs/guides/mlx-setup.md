# MLX Backend Setup (Apple Silicon)

The MLX backend lets a single node serve models that don't fit alongside
Ollama's hardcoded 3-model hot cap on macOS — most notably the
Qwen3-Coder-480B-A35B-Instruct-4bit (≈260 GB) that won't co-exist with
gpt-oss:120b + gemma3:27b in Ollama.  MLX runs as an independent
`mlx_lm.server` subprocess managed by the node agent; the router routes
`mlx:`-prefixed models to it.

**Platform support**: macOS on Apple Silicon (arm64) only.  On Linux,
Windows, or Intel Macs the node agent skips MLX init and the core
routing still works against Ollama.

## One-time install

```bash
./scripts/setup-mlx.sh
```

This:

1. Installs `mlx-lm==0.31.3` via `uv tool` (the version the ollama-herd
   patch is verified against).
2. Applies the KV-cache-quantization patch documented in
   `docs/experiments/mlx-lm-server-kv-bits.patch` — exposes
   `--kv-bits`, `--kv-group-size`, `--quantized-kv-start` which the
   node's `mlx_supervisor` passes to match Ollama's `OLLAMA_KV_CACHE_TYPE=q8_0`
   tuning.
3. Verifies the flags are live on `mlx_lm.server --help`.

**The script is idempotent.** Safe to re-run; a no-op if everything's
already in place.

### Re-run after any mlx-lm upgrade

`uv tool upgrade mlx-lm` or any fresh `uv tool install mlx-lm` **wipes
the patch** — `mlx_lm.server` will start failing with
`unrecognized arguments: --kv-bits 8 --kv-group-size 64` and the node
agent will log "mlx_lm.server failed to become healthy within 120s".

Remedy: re-run `./scripts/setup-mlx.sh`.  If upstream has moved past
`0.31.3`, the script will re-pin to that known-good version.

## Required environment variables

MLX is opt-in, gated behind env flags.  These must be in your shell
profile (`~/.zshrc` on macOS) AND in `launchctl setenv` if you want
GUI-launched Ollama to see the same env — see the
[macOS env gotcha](../../CLAUDE.md) in CLAUDE.md.

### Router (``herd``) — fleet-wide

```bash
export FLEET_MLX_ENABLED=true
export FLEET_MLX_URL=http://127.0.0.1:11440   # where mlx_lm.server listens on the router's node
```

### Node (``herd-node``) — the machine that hosts mlx_lm.server

```bash
export FLEET_NODE_MLX_ENABLED=true
export FLEET_NODE_MLX_URL=http://127.0.0.1:11440
export FLEET_NODE_MLX_AUTO_START=true
export FLEET_NODE_MLX_AUTO_START_MODEL=mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit
export FLEET_NODE_MLX_KV_BITS=8                     # matches Ollama's q8_0
export FLEET_NODE_MLX_PROMPT_CACHE_BYTES=17179869184  # 16 GB
```

### Optional: route Anthropic requests to the MLX model

```bash
export FLEET_ANTHROPIC_MODEL_MAP='{"default":"mlx:mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit","claude-sonnet-4-5":"mlx:mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit"}'
```

## Multi-server setup — main model + dedicated compactor

You can run multiple `mlx_lm.server` processes on one node (or across
multiple nodes) when you have the RAM headroom.  The canonical use case
is **pairing a main coding model with a smaller model dedicated to
context compaction**, so summarization never competes for the main
model's process or triggers an Ollama eviction.

Canonical recipe on a 512 GB Mac Studio — Qwen3-Coder-Next-4bit (≈42 GB)
for coding + Qwen3-Coder-30B-A3B-Instruct-4bit (≈16 GB) for compaction:

```bash
# Multi-server MLX: main + compactor on dedicated ports.
# Note: speculative decoding (draft_model) is enabled ONLY on 11441.
# The main coding model on 11440 (Qwen3-Coder-Next-4bit) is a hybrid
# linear-attn architecture that builds a non-trimmable ArraysCache, so
# spec decoding still hits mlx-lm#1081 there.  The 30B-A3B-Instruct
# compactor uses standard transformer attention and works fine.
# See docs/issues/mlx-speculative-decoding-blocked.md for the full
# per-architecture compatibility matrix.
export FLEET_NODE_MLX_SERVERS='[
  {"model":"mlx-community/Qwen3-Coder-Next-4bit","port":11440,"kv_bits":8},
  {"model":"mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit","port":11441,"kv_bits":8,"draft_model":"mlx-community/Qwen3-1.7B-4bit","num_draft_tokens":4}
]'
# 0.0.0.0 = LAN-reachable (required for multi-node); 127.0.0.1 = local-only
export FLEET_NODE_MLX_BIND_HOST=0.0.0.0
# Startup gate: refuse to spawn a server if (estimated_weights + headroom) > available RAM
export FLEET_NODE_MLX_MEMORY_HEADROOM_GB=10.0

# Route context compaction to the smaller 30B instead of the default gpt-oss:120b.
# This is also the model with spec decoding live, so every Claude Code
# summarization pass benefits — verified 2026-04-27 at ~94 tok/s on M3 Ultra.
export FLEET_CONTEXT_COMPACTION_ENABLED=true
export FLEET_CONTEXT_COMPACTION_MODEL=mlx:mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit
```

When `FLEET_NODE_MLX_SERVERS` is set, the legacy single-server fields
(`FLEET_NODE_MLX_AUTO_START_MODEL`, `FLEET_NODE_MLX_URL`,
`FLEET_NODE_MLX_KV_BITS`) are ignored.  Keep them defined so reverting
to a single-server config only requires unsetting `FLEET_NODE_MLX_SERVERS`.

**Speculative decoding** uses two same-tokenizer-family models — a
small "draft" model proposes tokens that the main model verifies and
either accepts or rejects.  Acceptance gives a multi-token speedup per
forward pass; rejection costs the draft's compute but doesn't break
correctness.  The draft must share the main's tokenizer family (both
Qwen3 here, so `mlx-community/Qwen3-1.7B-4bit` works as draft for
either Qwen3-Coder variant).  **Architecture compatibility matters more
than family**: standard transformer MoEs (`Qwen3-Coder-30B-A3B-Instruct`)
work; hybrid linear-attn MoEs (`Qwen3-Coder-Next`) don't, because their
state cache can't be rolled back when a draft token is rejected.

**Why this layout wins on a 3-model-capped Ollama host**: the Ollama
0.20.4 macOS build has a hardcoded 3-model hot cap.  If the compactor
ran as an Ollama model (e.g. `gpt-oss:120b`), it would occupy one of
those slots — and any `ollama run` in a terminal could silently evict
a mapped model that Claude Code depends on.  Running the compactor in
its own MLX process sidesteps the cap entirely, at a cost of ≈16 GB
RAM per node that hosts it.

Verify both are healthy:
```bash
curl -s http://localhost:11440/v1/models | jq -r '.data[].id'
curl -s http://localhost:11441/v1/models | jq -r '.data[].id'
curl -s http://localhost:11435/fleet/status | jq '.nodes[].mlx_servers'
```

The dashboard renders a per-server health table inside each node card
showing port/model/status/size/time-since-healthy — useful when one
server crashes and you need to see which one without tailing logs.

**Memory-gate behavior**: if a server would overflow available RAM at
start time, the supervisor skips it with `memory_blocked` status and
emits a WARNING health-check recommendation.  No crash-loop, no OOM.
Free some memory (drop a pinned Ollama model) and restart the node to
retry, or lower `FLEET_NODE_MLX_MEMORY_HEADROOM_GB` if the check is
being too conservative for your workload.

## Verify it worked

After `source ~/.zshrc` and restarting `herd-node`:

```bash
# 1. mlx_lm.server process is running
ps aux | grep mlx_lm.server | grep -v grep

# 2. mlx_lm.server /v1/models lists your auto-start model
curl -sS http://127.0.0.1:11440/v1/models | jq .

# 3. The node collector reports MLX state
grep -i "MLX state" ~/.fleet-manager/logs/herd.jsonl | tail -3

# 4. The router's fleet status includes the MLX model
curl -sS http://localhost:11435/fleet/status | jq '.nodes[0].mlx'
```

## Troubleshooting

- **`mlx_lm.server: error: unrecognized arguments: --kv-bits 8`** →
  the patch was wiped.  Re-run `./scripts/setup-mlx.sh`.
- **`mlx_lm.server failed to become healthy within 120s`** → usually
  a cold-load timeout on huge models.  The 480B takes ≈90–180s to
  load on a fresh start.  Increase the health-check timeout in
  `mlx_supervisor` or pre-warm manually once:
  `mlx_lm.server --model <repo> --port 11440 --kv-bits 8` and keep
  it running; the node agent will detect it instead of spawning.
- **`mlx_lm.server binary not found`** → `./scripts/setup-mlx.sh` wasn't
  run, or its install went to a path not on `$PATH`.  Make sure
  `~/.local/bin` is in `$PATH` (uv tool's default shim directory).
- **mlx-lm got uninstalled unexpectedly** → an earlier ollama-herd
  troubleshooting step (e.g. to mitigate memory-pressure crashes on
  very large models) may have removed it.  Re-run the setup script.
  Consider bumping `context_compaction_model` or other curator
  settings to avoid curator+MLX memory contention rather than
  uninstalling.

## Related

- `docs/plans/mlx-backend-for-large-models.md` — architecture + benchmarks
- `docs/experiments/mlx-lm-server-kv-bits.patch` — the patch rationale
- `docs/experiments/mlx-lm-q8kv-benchmark.md` — MLX+Q8 vs Ollama comparison
- `CLAUDE.md` — macOS env gotchas (`launchctl setenv` vs `~/.zshrc`)
