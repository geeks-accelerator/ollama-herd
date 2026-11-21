# Run 6: a fleet of Apple machines on AWS EC2 Mac

**Status:** PLAN (2026-11-21). Nothing allocated yet.

Runs 2-5 measured herd on rented NVIDIA machines (Lambda Cloud). Herd's home is Apple Silicon,
and the only Apple fleet it has been measured on is this repo's own two Macs. This run repeats the
run 5 tests on a mixed fleet of Apple machines, and adds the questions only Apple hardware can
answer: unified memory instead of a GPU card, memory bandwidth as the decode limit, and MLX.

Same harness (`scripts/benchmark.py`), same manifest format (`docs/experiments/fleet_run5_manifest.json`),
same summarizer (`docs/experiments/fleet-summarize.py`), three repetitions per arm, so the results
sit next to run 5's on the benchmark page.

## Questions

1. **Does scaling hold on Macs?** Run 2 showed 111 → 223 → 437 tok/s on 1, 2 and 4 identical
   NVIDIA cards. Same test on identical Macs, and Herd against HAProxy on identical machines (run
   5's `identical_machines`).
2. **Mixed Apple fleet placement.** Four chip classes with 24 to 256 GB of unified memory and 120
   to 819 GB/s of bandwidth. Does placement (eviction charge, stranding, partial-offload rules from
   0.12.10-0.12.12) behave on unified memory, where `size_vram == size` and there is no
   `gpu_total_gb`? Herd against HAProxy leastconn and roundrobin, run 5's open-loop method.
3. **One big Mac or two medium ones, at the same price?** One M3 Ultra ($12.50/h) against two M4
   Max ($6.25/h each): the Apple version of run 5's `one_large_vs_two_small`, but at exactly equal
   cost, which run 5 could not arrange.
4. **Is the scorer's bandwidth awareness worth anything?** Signals 3-5 use
   `memory_bandwidth_gbps` (`server/hardware_lookup.py`). Same model on an M4 Max (546 GB/s) and an
   M4 Pro (273 GB/s): does herd give the faster machine the larger share, and does that beat an
   even split?
5. **A model only one machine can hold.** A ~140 GB model fits only the M3 Ultra. Under mixed
   traffic it must never be evicted for a model another Mac could take (0.12.11's stranding rule,
   so far proven only on a 48 GB card).
6. **Memory pressure on a small Mac.** On the 24 GB M4, does `kern.memorystatus_vm_pressure_level`
   reach the router, and does critical pressure withhold cold loads while resident models keep
   serving (the 2026-10-02 rule, never seen firing on a real fleet)?
7. **MLX.** Ollama 0.40 runs safetensors models on its MLX runner, which decodes one request at a
   time (`decode_parallelism_for` = 1). Does herd queue that correctly beside llama.cpp models?
   And an `mlx_lm.server` node (`FLEET_NODE_MLX_SERVERS`) in the same fleet.
8. **The usual controls:** router overhead across the network, failure handling (Ollama stopped,
   node killed), embeddings (native fastembed and DINOv2 on Apple), and a long soak.

Not in scope: Thunderbolt-linked distributed MLX (AWS connects hosts over 10 Gbps Ethernet only;
that needs a Bookingmac-style Mac mini cluster, a separate run).

## Fleet

All in one region and one availability zone, one VPC subnet, 10 Gbps between hosts.

| Name | Instance type | Chip | Memory | Bandwidth | On-demand | Role |
|---|---|---|---|---|---|---|
| u1 | `mac-m3ultra.metal` | M3 Ultra, 60-core GPU | 256 GB | 819 GB/s | $12.50/h | the big machine; holds the Ultra-only model |
| x1, x2 | `mac-m4max.metal` | M4 Max, 40-core GPU | 128 GB | 546 GB/s | $6.25/h each | identical pair; MLX node (x2) |
| p1, p2 | `mac-m4pro.metal` | M4 Pro, 20-core GPU | 48 GB | 273 GB/s | ~$1.97/h each | identical pair |
| s1 | `mac-m4.metal` | M4, 10-core GPU | 24 GB | 120 GB/s | ~$1.23/h | small machine; memory-pressure test |
| r1 | `c7i.2xlarge` (Linux) | | | | ~$0.36/h | router, HAProxy, benchmark client |

The router runs on its own Linux instance so that no Mac pays for routing or for the load
generator, and so the router overhead figure is comparable to run 2's (also measured off-node).

**Cost.** Every Mac is a Dedicated Host with a **24-hour minimum** (Apple's macOS licence). Six
hosts: 12.50 + 12.50 + 3.94 + 1.23 = **$30.17/h, $724 for the 24-hour block**, plus the router,
EBS and S3 (under $25). Budget **$760**. The M4 Pro and M4 rates come from a third-party price
tracker; confirm both on the AWS Dedicated Host pricing page before allocating.

Cheaper fallback ($480): drop x2 and p2. That loses the identical pairs (question 1 at N = 2 and
the equal-price arm of question 3), so do it only if quota or capacity forces it.

## Before the clock starts

The 24 hours begin when a host is allocated, so everything that can be done without hosts is done
first.

1. **Quotas.** EC2 Mac Dedicated Host quotas are per instance family and region and are usually 0
   or 1 on an account that has never used them. File Service Quotas increases for
   `mac-m3ultra`, `mac-m4max` (2), `mac-m4pro` (2) and `mac-m4` in the chosen region, a week ahead.
   Check which availability zone has M3 Ultra capacity (`aws ec2 describe-instance-type-offerings
   --location-type availability-zone --filters Name=instance-type,Values=mac-m3ultra.metal`) and
   pick that zone for every host.
2. **Model store in S3.** On a Linux instance, `ollama pull` every model below, then upload
   `~/.ollama/models` to an S3 bucket in the same region, with a gateway VPC endpoint so the hosts
   fetch over the AWS network for free. Pulling ~400 GB from the Ollama registry six times would
   eat hours of the block.
3. **Bootstrap script** (`scripts/aws-mac-bootstrap.sh`, written 2026-11-21, not yet run on an
   EC2 Mac), run as EC2 user data or over SSH with `NODE_ID`, `ROUTER_URL`, `HERD_VERSION` and
   `MODELS_S3` set. It exits non-zero on any failure and writes `/var/db/herd-bootstrap.done`
   only once the router lists the node online at the expected agent version. It does:
   - Ollama from the GitHub release's `Ollama-darwin.zip`, pinned version, CLI only
     (`ollama serve` under a LaunchDaemon run as `ec2-user`: an EC2 Mac boots with nobody
     logged in, so a launchd agent would never start). Not Homebrew: the formula is stale (CLAUDE.md, Ollama
     notes). Environment on every Mac: `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_KEEP_ALIVE=-1`,
     `OLLAMA_MAX_LOADED_MODELS=10`, `OLLAMA_CONTEXT_LENGTH=32768`.
   - `uv tool install "ollama-herd[embedding]==X.Y.Z"` from PyPI (the released version, not `main`).
   - Models synced from S3 to local disk: the 2 TB instance store on the M4 and M4 Pro hosts,
     a gp3 EBS volume (1 TB, 1,000 MB/s) on the others.
   - `herd-node --router-url http://<r1>:11435` with `FLEET_NODE_NODE_ID` set to the short name.
     **mDNS does not cross a VPC** (no multicast), so every node is pointed at the router
     explicitly, as on Lambda.
   - Telemetry off everywhere (`FLEET_TELEMETRY=false`, `FLEET_NODE_TELEMETRY=false`).
4. **Rehearse the bootstrap** on a Mac you own (or one `mac-m4.metal` the week before, $30) until
   it brings a node to `online` with no hands. A broken bootstrap found on the day costs the
   whole fleet's hourly rate.
5. **Release first.** Publish the current unreleased fixes (load-hold cap, pinning herd's own
   loads) as a release and run that from PyPI, so the mixed-fleet tests measure them.
6. **Write the run 6 manifest** (`docs/experiments/fleet_run6_manifest.json`) with every section,
   file pattern and seed before the run, the way run 5's was. The summarizer reads it.

## Models

| Model | Loaded size | Fits | Used in |
|---|---|---|---|
| `qwen3-coder:30b` | ~19 GB | all but s1 at full context | 1, 3, 4, soak |
| `qwen2.5:14b-instruct-q8_0` | ~16 GB | all | 2, 3 |
| `phi4:14b-q8_0` | ~16 GB | all | 2 |
| `gemma3:27b` | ~17 GB | all but s1 | 2, vision |
| `gpt-oss:120b` | ~65 GB | u1, x1, x2 | 2, 3 |
| `qwen3:235b-a22b` (Q4_K_M) | ~142 GB | **u1 only** | 5 |
| a safetensors model on Ollama's MLX runner (pick one that `ollama ps` shows as `RUNNER mlx`) | ~20 GB | x1 | 7 |
| `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` via `mlx_lm.server` | ~17 GB | x2 | 7 |
| `nomic-embed-text`, `dinov2-vit-s14` | small | all | 8 |

Workload as run 5: about 1,250 prompt and 200 completion tokens per chat request
(`--prompt-tokens 1250 --max-tokens 200`).

## Schedule (one 24-hour block)

Hours are from allocation. Every test arm is three repetitions unless noted, every repetition
writes `--output` (every request is kept in it), and every arm's
start and end are logged on r1.

| Hours | Step |
|---|---|
| 0:00-1:30 | Allocate all hosts at once, launch instances (macOS boot plus bootstrap takes 20-60 min on EC2 Mac), sync models, start nodes |
| 1:30-2:00 | **Sanity gate.** `/fleet/status` shows six nodes online with the right `memory_bandwidth_gbps`, `num_parallel` 4 and agent version; `ollama ps` on each Mac shows `100% GPU` for a loaded model (Metal works); one chat, one embed, one DINOv2 embed through r1. Stop here and fix if any fails |
| 2:00-2:30 | **Router overhead** (question 8): 200 alternating pairs, direct to x1's Ollama vs through r1, as `router_overhead_results.json` |
| 2:30-4:00 | **Scaling on identical Macs** (question 1): `qwen3-coder:30b` on x1 alone, then x1 + x2; concurrency 1, 2, 4, 8; 120 s each. Repeat on p1 / p1 + p2. Then Herd vs HAProxy roundrobin on x1 + x2 at concurrency 8, alternating front doors |
| 4:00-5:30 | **Mixed Apple fleet** (question 2): all six Macs, models chosen at random per request (`--sequence random`, seeds 7, 11, 13), open loop at 0.25 and 0.5 req/s for 180 s, timeout 300 s, documented start state. Herd vs HAProxy leastconn vs HAProxy roundrobin, same seeds |
| 5:30-6:30 | **Same price, one big vs two medium** (question 3): u1 alone behind one router, x1 + x2 behind another; `qwen3-coder:30b` + `gpt-oss:120b` + `nomic-embed-text`, 4 workers each, 180 s |
| 6:30-7:15 | **Bandwidth** (question 4): `qwen3-coder:30b` on x1 + p1 only, concurrency 2, 4, 8; record each node's request share and per-node decode rate. Compare against HAProxy roundrobin (an even split) |
| 7:15-8:00 | **Ultra-only model** (question 5): `qwen3:235b-a22b` resident on u1, open-loop mixed traffic of the other models at 0.5 req/s for 300 s. Pass: it is never evicted (`model_events` in the report) |
| 8:00-8:30 | **Memory pressure** (question 6): on s1, load `gemma3:27b` then request `qwen3-coder:30b` there with `X-Fleet-No-Fallback`; record the pressure level in heartbeats and what the router does. One repetition |
| 8:30-9:30 | **MLX** (question 7): mixed traffic over the MLX-runner model on x1, the `mlx_lm.server` model on x2 and a llama.cpp model; check herd's queue concurrency is 1 for the MLX-runner model and that nothing queues inside Ollama |
| 9:30-10:15 | **Embeddings** (question 8): `nomic-embed-text` (native fastembed and Ollama) and DINOv2 at concurrency 4 and 16 alongside chat traffic |
| 10:15-11:00 | **Failure** (question 8): run 5's script: 8 concurrent for 240 s; stop Ollama on p2 at ~60 s, `kill -9` herd-node and stop Ollama on s1 90 s later; kill times logged to the millisecond |
| 11:00-23:00 | **Soak**: realistic mixed traffic from r1 (all chat models weighted toward `qwen3-coder:30b`, embeddings, occasional vision) at an average of 0.3 req/s. Pass: zero ERROR lines, completion rate above 99%, `process_memory` peak flat on every node |
| 23:00-24:00 | Export everything (below) before the block ends. Release the hosts only after the 24 hours (they cannot be released earlier) |

Slack: the schedule leaves about two hours inside the soak window for re-running any arm that
failed for a reason outside herd (an S3 sync that stalled, a host that would not boot). Cut the
soak, not a test arm.

## What to record

The manifest's `run` block, as run 5: date, region and zone, instance types with prices, macOS
version, Ollama version and environment, herd version, harness commit, workload, telemetry off.
Per section: files, router, seeds, start state, events with timestamps.

Exported from r1 and the nodes into `docs/experiments/`:
- `fleet_run6_manifest.json`, `fleet_run6_results.json` (the summarizer's output);
- every `--output` report (per-request records, `model_events`);
- `/dashboard/api/traces?since=<run start>` and both JSONL logs from every machine;
- `ollama ps` and the `llama-server` launch arguments (`-c`, `-np`) on each Mac at the start and
  end of each section.

**Caveat to print beside every cold-load number:** models load from EBS or the instance store, not
from an internal Mac SSD, so cold-load times are not comparable with a Mac on a desk. Throughput
once loaded is.

## Report

Into `docs/experiments/fleet-scaling-benchmark.md` (a run 6 section) and the findings plan, in the
same shape as run 5: a table per question, the HAProxy arms reported as prominently as Herd's,
including any arm where Herd does not win. Then charts for the benchmark page.

## Abort and stop rules

- Sanity gate fails after an hour of fixing: stop, export what exists, keep the hosts for the
  soak only if the router and at least four nodes are healthy.
- M3 Ultra capacity unavailable in every zone: run without u1 (drop questions 3 and 5), saving
  $300; do not wait for it.
- Any arm with failures herd caused: keep the data, note it, move on, and write it up as a finding
  rather than re-running until it passes.

## Open questions before booking

- Region: `us-east-1` or `us-west-2` (M4 generation launched in both); pick whichever has M3 Ultra
  capacity in one zone.
- Savings Plans give up to 44% off, but only with a 1- or 3-year commitment, so on-demand is right
  for a single run.
- Whether to add a second M3 Ultra for a scaling arm at the top end ($300 more).
