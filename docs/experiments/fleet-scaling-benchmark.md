# Concurrent agents on 1, 2 and 4 machines

**Status:** run 2026-11-20 on rented NVIDIA machines; results below and in `fleet_scaling_results.json`.

## Question

"If I run several agents at once, what does a second (or fourth) machine buy me?" This measures
**throughput under concurrency**, not the speed of one request: Ollama Herd routes whole requests,
so a single request is no faster on four machines than on one. Any chart from this says so in its
title.

## Harness

`scripts/benchmark.py`, unchanged: concurrent streaming load against the router for a fixed
duration, per-node saturation, results saved to the router and shown on the dashboard's
Benchmarks tab (which doubles as footage for the demo video). Every arm runs through Herd, so the
client and the measurement are identical; the baseline is Herd with one node.

## Method

1. Same model on every node (for example `qwen3-coder:30b`), loaded and warm. Same Ollama version,
   same `OLLAMA_NUM_PARALLEL` on every node; record both.
2. For N = 1, 2 and 4 nodes, and concurrency C = 1, 2, 4 and 8:
   ```bash
   python scripts/benchmark.py --url http://<router>:11435 --models qwen3-coder:30b \
       --concurrency <C> --duration 120
   ```
3. Measure the router's own overhead once: the same single request sent to one node's Ollama
   directly and through the router, 20 times each. State it next to the results.
4. Export the runs from the router (`GET /dashboard/api/benchmarks`) to
   `docs/experiments/fleet_scaling_results.json`, with hardware and versions.

## Report

Aggregate tokens per second and per-stream TTFT p50 and p95, per (N, C). The baseline arm (one
node, `OLLAMA_NUM_PARALLEL` tuned) is reported as prominently as the others, including the cases
where a second machine adds nothing (C = 1).

## Results (2026-11-20)

Four NVIDIA Quadro RTX 6000 24 GB (Lambda Cloud, one private network), Ollama 0.40.16 with
`OLLAMA_NUM_PARALLEL=4` set only in Ollama's systemd unit, herd `main` at `c0891f5` (released in
0.12.10), `qwen3-coder:30b`. Each request ~1,260 prompt tokens and 200 completion tokens
(`--prompt-tokens 1250 --max-tokens 200`, the average request on ollamaherd.com/stats), 120 s per
arm, every node warmed directly first. **0 failed requests in every arm.**

Aggregate completion tokens per second (TTFT p50):

| Concurrent requests | 1 machine | 2 machines | 4 machines |
|---|---|---|---|
| 1 | 66.7 (556 ms) | 76.7 (568 ms) | 76.7 (559 ms) |
| 2 | 96.7 (869 ms) | 153.3 (566 ms) | 153.3 (568 ms) |
| 4 | 106.7 (1,425 ms) | 160.0 (882 ms) | 308.3 (566 ms) |
| 8 | 106.7 (8,429 ms) | 213.3 (1,434 ms) | 316.7 (881 ms) |
| 16 | | | 426.7 (1,414 ms) |

**Corrected 2026-11-23 to in-window figures** (requests that finished inside the 120 s window).
First published as 67.2/97.2/111.6/111.0, 77.8/154.9/165.7/223.0 and 77.8/155.1/308.4/326.7/437.1,
which divided every completed request, those that finished in the drain included, by the elapsed
time. This run kept no per-request records, so the figures are `(answered - C) x 200 / 120`: every
request ran to its 200 tokens, and each of the C workers had exactly one request in the drain, as
in every run 6 cell and every later arm that kept its records. Run 4 and 5 and the 0.12.12 check
are recomputed the same way in `legacy_rate_corrections.json` (`legacy-rate-corrections.py`).

- **One request is no faster on more machines** (C = 1: 67-77 tok/s everywhere). The one-machine
  C = 1 arm read 66.7 against 76.7 on the others, on the same machine (`m1`): one of its 41
  requests took 12.2 s (latency p99) against ~2.6 s everywhere else, with a normal TTFT, so one
  slow generation in Ollama, not routing. (Corrected 2026-11-20; an earlier note blamed a
  different card.)
- **Saturated, throughput is near-linear in machines:** 106.7 → 213.3 → 426.7 tok/s (2.00x, 4.00x).
  One machine is flat from 4 to 8 concurrent requests (Ollama's 4 slots); past that, requests wait.
- **Requests per machine is what matters,** not total concurrency: at 8 concurrent, four machines
  each have only 2 in flight, so 316.7 tok/s, not 4x.
- **Even placement once every machine had work:** with at least as many concurrent requests as
  machines, every arm split requests within 1-2 of evenly. With fewer (2 machines at C = 1, 4 at
  C = 1 and C = 2) the spare machines served nothing: one request runs on one machine.
- **Router overhead,** measured separately on 0.12.10 (2026-11-20, `router_overhead_results.json`):
  **6.3 ms p50, 7.1 ms p95** per request over the private network (200 alternating pairs, direct vs
  through herd; `X-Fleet-Overhead-Ms` reported 6.0 ms). On 0.12.9 it was 23.8 ms. Through a 20 ms mock
  upstream the router adds 6-7 ms up to 125 req/s, 11 ms at 150, and queues from about 175 req/s
  (p99 104 ms); at 200 req/s it adds ~200 ms and from 300 req/s requests fail. That is one router
  process on one 14-vCPU AMD EPYC-Rome cloud machine, with its event loop at ~80 % of a core at 175/s.

## Run 6: two Apple M4 Macs on AWS EC2 (2026-11-21/22)

Two `mac-m4.metal` Dedicated Hosts (Apple M4, 10-core GPU, 24 GB, 120 GB/s; $1.23/h each with a
24-hour minimum) in us-east-2a, macOS 26.7.3, Ollama 0.40.13 as a LaunchDaemon with
`OLLAMA_NUM_PARALLEL=4` and `OLLAMA_CONTEXT_LENGTH=4096`; router, HAProxy 2.8 and the harness on a
c7i.xlarge in the same subnet. `qwen2.5:7b` (5.6 GB, 100% GPU), ~1,280 prompt and 200 completion
tokens per request. Scaled down from the planned six-host fleet by decision (the 24-hour minimum
makes every host a full day's charge), and to the M4 because the M4 Pro had no capacity in any of
11 zones. Manifest: `fleet_run6_manifest.json`; summary: `fleet_run6_results.json`; findings:
`docs/plans/fleet-benchmark-findings-2026-11.md` items 33-47.

**Read these numbers with three cautions.** (1) Throughput is **in-window** chat tokens per second
(`*_in_window`: requests that finished inside the 120 s window); the older `tokens_per_sec` also
counts requests still running when the window closed and keeps rising past saturation. (2) Only
arms run with Ollama restarted first (empty prompt cache) are quoted below; earlier arms reused
seeds back to back and inherited each other's cache, worth up to 2x (finding 35). (3) The router
changed during the run, as fixes landed: 0.12.12 for the first arms, then `6dbf115` (queue
concurrency), `87e3bf0` (load gate), `82fdf26`, `b2c5d35`; each arm's router is in the manifest.

**What two M4s do (cold cache, three repetitions, 0 failed requests in every cell):**

| | 1 Mac | 2 Macs |
|---|---|---|
| saturated, 8 slots per Mac | 18.3 tok/s | 40.0 tok/s (**2.2x**) |
| 4 slots per Mac, concurrency 16 / 32 | | 40.0 / 40.0 |
| 8 slots per Mac, concurrency 16 / 32 | | 36.7 / 36.7 |

An M4 saturates at 4 parallel requests; 8 slots only slow each one (finding 37). Before
`6dbf115`, herd held each 24 GB Mac to one request at a time (finding 33).

**Herd against HAProxy on the same two Macs:**

| Test | Herd | HAProxy |
|---|---|---|
| one model, 8 concurrent, cold cache, 3 pairs | 32 requests, 40.0 tok/s each run | 32, 40.0 (roundrobin) |
| 4-turn sessions over a 1,500-token prompt, 8 at once | **80** requests (median), first token p50 2.1 s | 56 (round-robin to each Ollama), 4.1 s |
| four models at random, open loop 0.15 req/s | 27/27, first token p50 11 s, p95 45 s, **3-4 loads** | leastconn 27/27, p50 48 s, p95 177 s, 16 loads; roundrobin 25/27 (300 s timeouts), p50 36 s, 11 loads |
| one model, open loop 0.1 / 0.2 req/s | first token 5.3 / 5.5 s | 5.5 / 5.7 s |
| one model, open loop 0.4 req/s (past capacity) | p50 39.9 s, p95 127.9 s | p50 61.1 s, p95 115.7 s |

Identical machines and one model is round-robin's best case, and there herd ties it. Herd pulls
ahead where placement matters: conversations kept on the Mac holding their prefix (+43%), and
mixed models served with a quarter of the loads and a quarter of the first-token time. One seed of
the mixed test on `87e3bf0` lost 11 of 27 requests to a pre-warm that blocked a Mac's loads
(finding 39, open).

**Embeddings, failure, soak.** Native `nomic-embed-text` and DINOv2 beside chat: about 5,850
requests per 120 s at 4 workers per model, 0 failures (finding 46). Ollama stopped and herd-node
killed on one Mac mid-run: only the 4 streams in flight there failed, every time (finding 41).
Soak: 4 hours, 1,728 requests, 0 failures, herd-node's own memory flat; Ollama's 8 GiB-per-model
prompt cache pushed both 24 GB Macs into swap and latency up about 20% (finding 47). Router
overhead over the network: **3.4 ms p50** (200 alternating pairs; `X-Fleet-Overhead-Ms` 3.0 ms).

**Recorded side by side (2026-11-22, `main` at the time, router on s1).** Two measurements made
while filming, each a single take, so read them as demonstrations rather than repeated cells:

| Test | 1 Mac | 2 Macs |
|---|---|---|
| gpt-oss 20B and gemma3 12B alternating, 8 requests one at a time (they cannot share one M4's 17.8 GB working set) | median first token 21.0 s, 233.7 s total (a swap of 20-44 s on every switch) | 554 ms, 39.0 s (one model warm on each Mac) |
| 96 requests of qwen2.5 1.5B, 300 tokens each, sent at once | 101.6 s (94.6 s in an earlier take) | 58.1 s: 1.6-1.75x, 0 failed |

The recordings, with the rest of run 6's footage, are on the Geeks in the Woods YouTube channel in the
[Ollama Herd playlist](https://www.youtube.com/playlist?list=PLB5PhrWfq6Hk); every wall page in them is a
demo page built on herd's API, not part of herd.

Cold-load times are from EBS, not a Mac's internal SSD, and are not comparable with a Mac on a
desk; throughput once loaded is.
