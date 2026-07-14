# Testing Distributed MLX Inference

How to validate the distributed MLX backend (`mlx.launch`-wrapped
`mlx_lm.server` across multiple Macs). The feature adds `backend` / `hosts` /
`hostfile` / `pipeline` keys to `FLEET_NODE_MLX_SERVERS` — see
[`docs/plans/distributed-mlx-inference.md`](../plans/distributed-mlx-inference.md)
for the design and
[`docs/research/apple-distributed-mlx-jaccl-2026.md`](../research/apple-distributed-mlx-jaccl-2026.md)
for background on JACCL and topology trade-offs.

**Platform**: macOS on Apple Silicon. Distributed inference is an Apple-only
capability; the core routing path is unaffected on other platforms.

---

## Why three tiers

The distributed feature has two separable parts:

1. **The plumbing** — wrapping `mlx_lm.server` in `mlx.launch`, sharding the
   model across ranks, rank-0 serving HTTP and broadcasting to peers, and the
   herd supervisor spawning/monitoring all of that.
2. **The transport** — how ranks actually talk to each other: TCP (`ring`) or
   RDMA over Thunderbolt 5 (`jaccl`).

You do **not** need exotic hardware to test the plumbing. Testing splits into
three tiers of increasing hardware requirement, each proving strictly more than
the last:

| Tier | Backend | Hardware needed | Proves |
|------|---------|-----------------|--------|
| **1** | `ring`, all ranks on one machine | one Mac | plumbing: launch wrapper, sharding, rank-0 HTTP, herd integration |
| **2** | `ring` over LAN | two Macs on a network | real cross-machine distribution + memory pooling |
| **3** | `jaccl` over Thunderbolt 5 | two TB5 Macs + cable + RDMA | the RDMA fast path + real speedup |

Start at Tier 1. Most regressions surface there, with zero setup.

---

## Prerequisites (all tiers)

- `mlx-lm` installed so `mlx_lm.server` is on `PATH` (the herd resolves it via
  the shared binary-discovery helper).
- `mlx` installed so `mlx.launch` is on `PATH`. Confirm the launcher advertises
  the backend you intend to use:
  ```bash
  mlx.launch --help | grep -A1 backend
  # → --backend {ring,mpi,nccl,jaccl}
  ```
- A small model for smoke tests — pick something that loads in seconds so a
  failed run fails fast. Any small instruct model from the `mlx-community`
  Hugging Face org works; avoid large weights until the plumbing is proven.

---

## Tier 1 — single machine, `ring`, two ranks

Proves the entire code path except the physical transport. No second machine,
no Thunderbolt, no RDMA, no `mlx.launch` SSH (all ranks are local).

### Bare `mlx.launch` first (isolate the launcher from the herd)

Run the distributed server by hand so any failure is clearly in `mlx.launch` /
`mlx_lm.server`, not the herd:

```bash
MLX_METAL_FAST_SYNCH=1 mlx.launch \
  --backend ring -n 2 \
  -- \
  mlx_lm.server --model <small-model> --host 127.0.0.1 --port 8080 --pipeline
```

`-n 2` launches two local ranks over the ring backend. Only rank 0 binds the
HTTP server; rank 1 participates in the pipeline. Verify it serves:

```bash
curl -s localhost:8080/v1/models
curl -s localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<small-model>","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
```

A 200 from `/v1/models` and a completion from `/v1/chat/completions` proves the
sharded server works. If this fails, the problem is upstream of the herd.

### Then through the herd

Point a `FLEET_NODE_MLX_SERVERS` entry at the ring backend and let the
supervisor build the `mlx.launch` command:

```bash
export FLEET_NODE_MLX_ENABLED=true
export FLEET_NODE_MLX_SERVERS='[{"model":"<small-model>","port":11440,"backend":"ring","hosts":"127.0.0.1","pipeline":true}]'
# start the node agent
```

Note: with all ranks local, the operator-supplied `hosts` peer list is minimal;
the node prepends its own address automatically. What this validates:

- The supervisor resolves `mlx.launch`, builds the wrapped command, injects
  `MLX_METAL_FAST_SYNCH=1` via `--env`, and appends `--` before the inner
  server command.
- Rank-0 health polling works (the supervisor dials loopback regardless of the
  bind host).
- The heartbeat advertises the model with an `mlx:` prefix and marks the server
  `distributed` with a node count.

Confirm via the dashboard — the "Node Models" card shows the server with a
`ring · N nodes` chip — or the health endpoint:

```bash
curl -s localhost:11435/dashboard/api/health | grep -i mlx
```

**What Tier 1 does NOT prove:** the network transport, cross-machine memory
pooling, or any speedup. It's a plumbing test.

---

## Tier 2 — two machines, `ring` over LAN

Proves real cross-machine distribution and memory pooling. Works over ordinary
Ethernet or Wi-Fi — no Thunderbolt, no RDMA, no macOS-Recovery step.

### Setup

On both machines:

- Same `mlx_lm.server` / `mlx.launch` install, **at the same absolute path** —
  `mlx.launch` execs the inner command at an identical path on every host.
- **Passwordless SSH** from the machine that launches (rank 0) to the other:
  `ssh <peer>` must succeed with no prompt, host key already trusted.
- The model available on both (same Hugging Face cache path, or pre-pulled).

### Run

Configure one entry on the launching node. List only the **peer** IPs in
`hosts`; the node prepends its own:

```bash
export FLEET_NODE_MLX_ENABLED=true
export FLEET_NODE_MLX_SERVERS='[{"model":"<model>","port":11440,"backend":"ring","hosts":"<peer-ip>","pipeline":true}]'
# expose rank-0's HTTP to the LAN so a router on another host can reach it:
export FLEET_NODE_MLX_BIND_HOST=0.0.0.0
```

`pipeline: true` is the practical mode for ring — the TCP hop is too slow for
tensor parallelism's per-layer all-reduce. Pipeline splits the model by depth,
so the two machines' memory pools combine: you can now load a model larger than
either machine alone (with no speedup — pipeline is for capacity, not latency).

### Verify

- Rank 0's `/v1/models` and `/v1/chat/completions` respond (dial the launching
  node's LAN IP).
- A model that exceeds a single machine's memory loads and serves a completion —
  the headline proof of memory pooling.
- The herd dashboard shows the server `distributed`, `ring · 2 nodes`, healthy.

**What Tier 2 does NOT prove:** the JACCL/RDMA fast path, or the ~3× tensor
speedup (that needs Tier 3 on symmetric nodes).

---

## Tier 3 — two machines, `jaccl` over Thunderbolt 5

Proves the RDMA-over-Thunderbolt transport and real speedup. This is the tier
with hard hardware requirements.

### Prerequisites specific to JACCL

- **macOS 26.2 or later** on every node (RDMA over Thunderbolt ships here).
  Check: `sw_vers -productVersion`.
- **Thunderbolt 5** on every node. Thunderbolt 4 does not support RDMA. Check
  the bus speed (~120 Gb/s links) in `system_profiler SPThunderboltDataType`.
- **A Thunderbolt 5 cable between each pair of nodes.** JACCL wants a
  fully-connected topology — for 2 nodes that's one cable; for N nodes it's
  N·(N-1)/2 cables, which caps practical clusters at 4–6.
- **RDMA enabled on each node.** This can't be done from a running session — it
  requires macOS Recovery:
  ```
  # boot into Recovery, open Terminal:
  rdma_ctl enable
  # reboot
  ```
  Verify after reboot (from a normal session):
  ```bash
  rdma_ctl status     # → enabled
  ibv_devices         # lists one RDMA device per active TB link
  ```
  An empty `ibv_devices` list or `rdma_ctl status → disabled` means RDMA isn't
  active yet — do the Recovery step, and make sure a Thunderbolt cable is
  physically connecting the machines.

### Hostfile

JACCL uses a hostfile (not a comma-separated `hosts` string) describing the SSH
hostnames plus the RDMA device matrix for the mesh. Generate it with the MLX
tooling rather than hand-writing it:

```bash
mlx.distributed_config \
  --hosts <host-a>,<host-b> \
  --backend jaccl \
  --auto-setup \
  --output cluster.json
```

### Run

```bash
export FLEET_NODE_MLX_ENABLED=true
export FLEET_NODE_MLX_SERVERS='[{"model":"<model>","port":11440,"backend":"jaccl","hostfile":"/abs/path/cluster.json"}]'
export FLEET_NODE_MLX_BIND_HOST=0.0.0.0
```

Tensor parallelism is the default (omit `pipeline`) — with sub-50 µs RDMA
latency, the per-layer all-reduce is cheap enough to give a real throughput
speedup, unlike ring.

### Verify

- Bare `mlx.launch --backend jaccl --hostfile cluster.json -- mlx_lm.server …`
  serves before wiring the herd — same isolation principle as Tier 1.
- Compare tokens/sec against a single-node run of the same model to measure the
  speedup.
- The herd dashboard shows the server `jaccl · N nodes`, healthy.

---

## Symmetric vs asymmetric clusters

Tensor parallelism (the mode that gives speedup) splits every layer evenly, so
it's bottlenecked by the **smallest** node — an asymmetric pair caps the
tensor-parallel model size at `2 × (smaller node's memory)`, which can be
*smaller* than the larger node alone. Rules of thumb:

- **Symmetric nodes** (same memory) → tensor parallelism → speedup. Tier 3 with
  `jaccl`, no `pipeline`.
- **Asymmetric nodes** (different memory) → pipeline parallelism → memory
  expansion, no speedup. Use `pipeline: true` on `ring` or `jaccl`.

See the research doc for the full analysis.

---

## Troubleshooting

- **`mlx.launch: command not found`** — install/upgrade `mlx`; the herd
  preflights this and reports `mlx.launch not found (distributed backend)` in
  the server's heartbeat status.
- **Hangs at startup, no HTTP** — inter-rank connection failing. For `ring`
  check LAN reachability + passwordless SSH; for `jaccl` check `ibv_devices` is
  non-empty and a cable connects every pair.
- **5–6× slower than expected** — `MLX_METAL_FAST_SYNCH=1` didn't reach the
  remote ranks. The herd passes it via `mlx.launch --env` automatically; if
  running `mlx.launch` by hand, add `--env MLX_METAL_FAST_SYNCH=1`.
- **`mlx.launch` can't find the script on a peer** — the inner `mlx_lm.server`
  must exist at the **same absolute path** on every host. Install it the same
  way everywhere.
- **Health shows the server but it never goes healthy** — rank 0 binds the
  configured host; the herd always health-polls loopback, so a `0.0.0.0` bind is
  fine locally. If a remote router can't reach it, confirm
  `FLEET_NODE_MLX_BIND_HOST=0.0.0.0`.
- **RDMA won't enable remotely** — by design. `rdma_ctl enable` only works in
  macOS Recovery, which needs physical or screen-share access to each machine.

---

## What each tier is worth

- **Tier 1** catches ~every code regression in the distributed path and needs no
  setup — run it in CI-adjacent smoke tests and before any release touching the
  MLX supervisor.
- **Tier 2** is the first test of genuine cross-machine behavior and is
  achievable on any two networked Macs — the practical bar for "distributed
  works."
- **Tier 3** is the only tier that exercises RDMA and produces speedup numbers,
  and it's gated on Thunderbolt-5 hardware + the Recovery-mode RDMA step. Treat
  it as hardware-lab validation, not routine testing.
