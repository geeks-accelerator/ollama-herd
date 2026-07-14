# Apple Distributed MLX and JACCL: Multi-Mac LLM Clusters (2026)

**Created**: 2026-07-13  
**Status**: Evidence-based — WWDC26 session notes, Apple developer docs, community benchmarks  
**Scope**: JACCL, MLX distributed inference, EXO framework, Thunderbolt 5 cluster setup  
**Relevant hardware**: Apple Silicon M3/M4 Pro and later (Thunderbolt 5 required)

---

## What was announced

At WWDC26, Apple announced production-ready distributed inference and training in MLX via two sessions:
- [Explore distributed inference and training with MLX](https://developer.apple.com/videos/play/wwdc2026/233/) (session 233)
- [Run local agentic AI on the Mac using MLX](https://developer.apple.com/videos/play/wwdc2026/232/) (session 232)

The key enabler is **JACCL** — Apple's open-source collective communication library that uses RDMA over Thunderbolt 5. RDMA ships in macOS 26.2. This turns a Thunderbolt cable between two Macs into a 50–60 Gbps link with sub-50µs latency — an order of magnitude lower than TCP-based alternatives.

The WWDC demo ran Kimi K2.6 (1 trillion parameters) at 28+ tokens/sec across four M3 Ultras. Single M3 Ultra can't load the model at all at INT8; the cluster makes it possible.

---

## Technology stack

```
App / OpenAI API
       ↓
  MLX LM server (mlx_lm.server)
       ↓
  MLX distributed (mx.distributed)
       ↓
  JACCL backend (collective comms)
       ↓
  RDMA over Thunderbolt 5 (macOS 26.2+)
       ↓
  Physical Thunderbolt 5 cable
```

### JACCL

Open-source library ([github.com/apple/jaccl](https://github.com/apple/jaccl)). Provides collective communication primitives: all-reduce, all-sum, point-to-point. MLX feeds it tensors; it handles RDMA transport automatically.

Supports two topologies:
- **Mesh**: every node connects directly to every other node (lowest latency, requires N×(N-1)/2 cables)
- **Ring**: each node connects to two neighbors (fewer cables, slightly higher latency per hop)

JACCL auto-selects the optimal topology based on message size.

### MLX backends (all four available)

| Backend | Transport | Latency | When to use |
|---------|-----------|---------|-------------|
| JACCL | RDMA / Thunderbolt 5 | sub-50µs | Mac cluster, macOS 26.2+ |
| Ring | TCP sockets | ~1ms | LAN without RDMA, always available |
| MPI | TCP + infiniband | ~1ms | Linux HPC clusters |
| NCCL | NVLink / PCIe | µs-range | CUDA environments |

For Apple Silicon cluster inference, JACCL is the target. Ring works today as a fallback.

---

## Hardware requirements

**Required:**
- Thunderbolt 5 on every node (not Thunderbolt 4 — RDMA requires TB5)
  - M3 Pro and later: TB5 ports (most configs)
  - M3 Ultra: TB5 (6 ports)
  - M4 Max: TB5 (3 ports)
  - M4 Ultra: TB5
- Active Thunderbolt 5 cable between each pair of nodes (for mesh topology)
- macOS 26.2 or later on all nodes
- RDMA enabled on each node (requires physical access — cannot be done via SSH)

**Topology cable count:**
| Nodes | Cables (mesh) |
|-------|--------------|
| 2 | 1 |
| 3 | 3 |
| 4 | 6 |
| 6 | 15 |

Practical limit is 4–6 nodes — cable count and port count become the constraint.

---

## Setup walkthrough

### 1. Enable RDMA (per machine, requires physical access)

```bash
# Boot into macOS Recovery, open Terminal, then:
rdma_ctl enable
# Reboot normally

# Verify after reboot:
ibv_devices
# Should list rdma_en5, rdma_en4, etc. (one per TB5 port used)
```

This cannot be done remotely. Someone must be physically at each machine.

### 2. Create hostfile

```json
[
  {
    "ssh": "mac-studio",
    "ips": ["192.168.1.10"],
    "rdma": [null, "rdma_en5", "rdma_en4"]
  },
  {
    "ssh": "macbook-pro",
    "ips": ["192.168.1.11"],
    "rdma": ["rdma_en5", null, "rdma_en4"]
  }
]
```

Or use auto-configuration:
```bash
mlx.distributed_config \
    --hosts mac-studio,macbook-pro \
    --output "cluster.json" \
    --env MLX_METAL_FAST_SYNCH=1 \
    --auto-setup \
    --backend jaccl
```

### 3. Launch distributed inference

```bash
# Distributed server across cluster
MLX_METAL_FAST_SYNCH=1 mlx.launch \
    --hostfile cluster.json \
    -- /path/to/mlx_lm.server \
    --model "moonshotai/Kimi-K2.6" \
    --host 0.0.0.0 \
    --port 8080
```

**`MLX_METAL_FAST_SYNCH=1` is critical.** Without it inference runs 5–6× slower due to CPU/GPU sync overhead.

### 4. Python API (for integration)

```python
import mlx.core as mx
from mlx_lm import stream_generate
from mlx_lm.utils import sharded_load

group = mx.distributed.init(strict=True, backend="jaccl")
tensor_group, pipeline_group = group, None

model, tokenizer = sharded_load("moonshotai/Kimi-K2.6", pipeline_group, tensor_group)
for response in stream_generate(model, tokenizer, prompt, max_tokens=1024):
    if group.rank() == 0:
        print(response.text, end="", flush=True)
```

---

## Parallelism strategies

### Tensor parallelism (default — use for inference speed)

Splits each model layer **by width** across all nodes. Every node processes the same token simultaneously, exchanging partial results via all-reduce at each layer boundary.

- **Speedup**: ~linear with node count up to 4 nodes (3× on 4-node cluster)
- **Communication**: frequent (every layer), requires low-latency JACCL
- **Memory**: each node holds a fraction of every layer — enables models larger than any single machine
- **Best for**: maximizing tokens/sec on models that fit the cluster

### Pipeline parallelism (use for memory expansion only)

Splits model **by depth** — node 0 holds early layers, node N holds late layers. Data flows sequentially through nodes.

- **Speedup**: none — tokens still sequential, idle wait between stages
- **Communication**: only at layer boundaries (low frequency)
- **Memory**: access to sum of all nodes' memory
- **Best for**: loading a model the cluster can hold but that adds no speed value — effectively just memory pooling

For inference serving, **tensor parallelism is almost always what you want**.

---

## Performance numbers

From Apple WWDC26 demo (4× M3 Ultra, JACCL, tensor parallelism):

| Configuration | Model | Tokens/sec |
|--------------|-------|-----------|
| 1× M3 Ultra | Qwen 3.6 27B | baseline |
| 4× M3 Ultra | Qwen 3.6 27B | ~3× baseline |
| 4× M3 Ultra | Kimi K2.6 (1T) | 28–32 tok/s |
| 1× M3 Ultra | Kimi K2.6 (1T) | does not fit |

From distributed fine-tuning (data-parallel LoRA):

| Configuration | Model | Tokens/sec |
|--------------|-------|-----------|
| 1× M3 Ultra | Qwen 3.5 9B | ~180 |
| 4× M3 Ultra | Qwen 3.5 9B | ~600 (3.3×) |

**Two-node scaling estimate**: Community results suggest ~1.5–1.8× speedup for 2-node tensor parallelism (diminishing returns kick in; communication overhead is proportionally larger with fewer nodes). The WWDC 3× number is for 4 nodes.

---

## What models become runnable at each cluster size

All sizes are approximate Q4 quantization (4-bit), which is the sweet spot for quality vs memory.

### Single M3 Ultra (512GB)

Already fits: Kimi K2.6 Q4 (~500GB), most MoE models up to ~400B params

### 2-node M3 Ultra + M4 Max (512 + 128 = 640GB)

Newly runnable: models in the 512–640GB Q4 range. The real gain is higher-precision quantization on models that already fit the Mac Studio:
- Kimi K2.6 Q5/Q5_K_M (~625GB) — noticeably better quality than Q4
- GLM-5.1 if it exceeds 512GB at Q4

Also: distributed fine-tuning becomes possible (data-parallel LoRA across both machines).

### 2-node M3 Ultra + M3 Ultra (512 + 512 = 1TB)

Newly runnable: Kimi K2.6 at INT8 (~1TB), any model up to 1TB. This is where the cluster starts unlocking qualitatively different models.

### 4-node M3 Ultra × 4 (512 × 4 = 2TB)

The WWDC demo configuration. Runs anything available today. Opens the door to future 2T+ parameter models.

---

## EXO: the simpler alternative

[EXO](https://github.com/exo-explore/exo) is an open-source framework that auto-discovers Macs on a LAN and distributes a model across them — no Thunderbolt required, no RDMA setup, no macOS 26.2 dependency.

**EXO exposes compatible APIs:**
- OpenAI Chat Completions (`/v1/chat/completions`)
- Ollama API (`/api/chat`)
- Claude Messages API (`/v1/messages`)
- OpenAI Responses API (`/v1/responses`)

**EXO tradeoffs vs JACCL:**

| | EXO (TCP/WiFi) | JACCL (RDMA/TB5) |
|--|----------------|-----------------|
| Setup | `pip install exo`, auto-discover | macOS Recovery, cables, hostfile |
| Transport | LAN (GigE/Wi-Fi) | Thunderbolt 5 RDMA |
| Latency | ~1–10ms | <50µs |
| Bandwidth | 1–10 Gbps | 50–60 Gbps |
| Inference speedup | Minimal on TCP (pipeline-only) | 3× on 4-node (tensor) |
| Memory pooling | Yes | Yes |
| macOS version | Any | 26.2+ |
| Hardware req | Any Mac | Thunderbolt 5 + cable |

EXO is the right choice for: running a model that doesn't fit any single machine, using LAN nodes that aren't cable-adjacent, or getting started without Recovery-mode setup.

JACCL is the right choice for: maximizing tokens/sec across a tightly-coupled cluster, achieving actual inference speedup (not just memory expansion).

---

## Fleet-specific analysis: Mac Studio M3 Ultra (512GB) + MacBook Pro M4 Max (128GB)

### Is this cluster viable for JACCL?

**Hardware**: Both machines have Thunderbolt 5 — yes, one TB5 cable between them is all that's needed for a 2-node mesh.

**macOS 26.2**: Required on both machines. Check: `sw_vers -productVersion`.

**RDMA setup**: Must be done at each machine physically (Recovery mode). MacBook Pro requires someone at the keyboard.

**Asymmetric memory (512GB + 128GB)**: Tensor parallelism splits layers evenly across nodes regardless of memory capacity. The 128GB node must hold its 50% of each layer. This constrains the maximum model size to `2 × 128GB = 256GB` in tensor parallel mode — **smaller than the Mac Studio alone** for most models.

The correct strategy for this asymmetric pair:
- **Pipeline parallelism**: assign ~80% of layers to the Mac Studio, ~20% to the MacBook. No speedup, but access to 640GB combined.
- **EXO over LAN**: simpler to set up, handles asymmetric memory naturally with pipeline.
- **Tensor parallelism**: only worth trying on small models where both machines can hold their shard (models up to ~256GB in total size).

### Practical recommendation

For the 512+128 configuration specifically:

1. **Near-term** (now, no new hardware): continue running everything on the Mac Studio. It handles all current models at Q4. Use the MacBook Pro only for different Ollama model instances (existing herd node behavior).

2. **Medium-term** (if macOS 26.2 is stable): set up EXO across both machines via LAN. No cable needed, no Recovery mode. Exposes /v1/chat/completions compatible API. Test with a model in the 400–500GB Q4 range.

3. **If a second M3 Ultra is added** (512+512=1TB): JACCL with 1 cable becomes compelling. Kimi K2.6 at INT8 (full precision, 1TB) runs at 28+ tok/s via tensor parallelism. This is the sweet spot.

4. **For fine-tuning** (data-parallel LoRA): the asymmetric pair works fine — each machine trains on its own batch, gradients synced. MacBook Pro 128GB is a reasonable co-trainer.

---

## Integration with ollama-herd

Several integration paths, in order of complexity:

### Option 1: Transparent (no changes needed)

Run `mlx_lm.server` on the cluster head node (Mac Studio) and register it as an MLX backend in `FLEET_NODE_MLX_SERVERS` as usual. The distributed setup is invisible to the herd — it just sees a faster/larger-capacity `mlx_lm.server` endpoint.

```bash
# On Mac Studio (head node):
MLX_METAL_FAST_SYNCH=1 mlx.launch \
    --hostfile cluster.json \
    -- /path/to/mlx_lm.server \
    --model "moonshotai/Kimi-K2.6" \
    --host 0.0.0.0 \
    --port 11440
```

Then in herd config:
```
FLEET_NODE_MLX_SERVERS=[{"model":"mlx:Kimi-K2.6","port":11440}]
```

### Option 2: EXO as additional herd node

Run EXO cluster exposing its Ollama-compatible API on a port, then configure the herd to route specific large models to it. EXO's Ollama API at `http://cluster-head:11434` looks identical to a regular Ollama endpoint.

### Option 3: Future native support

Track MLX distributed inference support in `MlxSupervisor` — when launching an `mlx_lm.server`, optionally wrap it in `mlx.launch --hostfile ...`. This would let herd manage distributed server lifecycle automatically. Not implemented; would require hostfile management and multi-node health checking.

---

## Current status (July 2026)

| Component | Status |
|-----------|--------|
| JACCL | Open-source, ships with macOS 26.2 |
| macOS 26.2 | Released (point release on macOS 26) — check availability |
| MLX 0.32.0 | Ships JACCL backend support |
| EXO | Active development, v0.x, API-compatible |
| Ollama MLX backend | Ships in Ollama v0.19+, stable in 0.30.8 |
| Ring backend (fallback) | Available today, no RDMA required |

The Ring backend (TCP) works right now with zero setup changes — performance is lower than JACCL but enables immediate two-node memory pooling over LAN. Useful for running models that don't fit any single machine.

---

## Key gotchas

- **RDMA must be enabled in Recovery** — cannot be done via SSH or sudo. Requires physical access to each machine. Plan for this before scheduling a cluster setup.
- **`MLX_METAL_FAST_SYNCH=1` is mandatory** — missing it causes 5–6× slower inference. Set it in the launch environment, not just the shell.
- **Thunderbolt 5 ≠ Thunderbolt 4** — RDMA only works over TB5. M2 machines are excluded.
- **Tensor parallelism is bottlenecked by the smallest node** — in a 512+128 cluster, tensor-parallel model capacity is 2×128=256GB, not 640GB. Use pipeline parallelism for memory expansion.
- **Fully-connected mesh for JACCL** — for 2 nodes, this is just 1 cable; for 4 nodes it's 6 cables. Plan the port usage: M3 Ultra has 6 TB5 ports, a 4-node mesh uses 3 per node.
- **EXO and mlx_lm.server are not the same process** — EXO uses its own model loading, not mlx_lm.server. Can't mix them behind the same endpoint.
- **macOS 26.2 point release** — verify both machines are on 26.2+. The base macOS 26.0 does not include RDMA support.

---

## Sources

- [WWDC26 session 233 — Explore distributed inference and training with MLX](https://developer.apple.com/videos/play/wwdc2026/233/)
- [WWDC26 session 232 — Run local agentic AI on the Mac using MLX](https://developer.apple.com/videos/play/wwdc2026/232/)
- [MLX distributed documentation (0.32.0)](https://ml-explore.github.io/mlx/build/html/usage/distributed.html)
- [alexziskind1/mlx-jaccl-cluster — community cluster setup guide](https://github.com/alexziskind1/mlx-jaccl-cluster)
- [MLX GitHub discussion #3481 — RDMA file transfer over TB5 with JACCL](https://github.com/ml-explore/mlx/discussions/3481)
- [byteiota — MLX JACCL distributed training explainer](https://byteiota.com/mlx-jaccl-thunderbolt-distributed-training/)
- [byteiota — MLX distributed training with JACCL multi-Mac clusters explained](https://byteiota.com/mlx-distributed-training-with-jaccl-multi-mac-llm-clusters-explained/)
- [Medium — $10K sovereign AI cluster with Apple Silicon](https://medium.com/@michael.hannecke/the-10k-sovereign-ai-cluster-how-smbs-run-100b-models-on-apple-silicon-hardware-b90a94020f4d)
- [EXO cluster documentation](https://noqta.tn/en/blog/exo-distributed-ai-cluster-apple-silicon-local-llm-2026)
- [Hacker News — macOS 26.2 enables fast AI clusters with RDMA over Thunderbolt](https://news.ycombinator.com/item?id=46248644)
- [Apple TN3205 — Low-latency communication with RDMA over Thunderbolt](https://blog.massapi.com/posts/2026-03-18-1623-tn3205-low-latency-communication-with-rdma-over-thunderbolt/)
