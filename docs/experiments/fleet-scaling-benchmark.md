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
| 1 | 67.2 (556 ms) | 77.8 (568 ms) | 77.8 (559 ms) |
| 2 | 97.2 (869 ms) | 154.9 (566 ms) | 155.1 (568 ms) |
| 4 | 111.6 (1,425 ms) | 165.7 (882 ms) | 308.4 (566 ms) |
| 8 | 111.0 (8,429 ms) | 223.0 (1,434 ms) | 326.7 (881 ms) |
| 16 | | | 437.1 (1,414 ms) |

- **One request is no faster on more machines** (C = 1: 67-78 tok/s everywhere). The one-machine
  C = 1 arm read 67.2 against 77-78 on the others, on the same machine (`m1`): one of its 41
  requests took 12.2 s (latency p99) against ~2.6 s everywhere else, with a normal TTFT, so one
  slow generation in Ollama, not routing. (Corrected 2026-11-20; an earlier note blamed a
  different card.)
- **Saturated, throughput is near-linear in machines:** 111.6 → 223.0 → 437.1 tok/s (2.00x, 3.92x).
  One machine is flat from 4 to 8 concurrent requests (Ollama's 4 slots); past that, requests wait.
- **Requests per machine is what matters,** not total concurrency: at 8 concurrent, four machines
  each have only 2 in flight, so 326.7 tok/s, not 4x.
- **Even placement once every machine had work:** with at least as many concurrent requests as
  machines, every arm split requests within 1-2 of evenly. With fewer (2 machines at C = 1, 4 at
  C = 1 and C = 2) the spare machines served nothing: one request runs on one machine.
- **Router overhead,** measured separately on 0.12.10 (2026-11-20, `router_overhead_results.json`):
  **6.3 ms p50, 7.1 ms p95** per request over the private network (200 alternating pairs, direct vs
  through herd; `X-Fleet-Overhead-Ms` reported 6.0 ms). On 0.12.9 it was 23.8 ms. Through a 20 ms mock
  upstream the router adds 6-7 ms up to 125 req/s, 11 ms at 150, and queues from about 175 req/s
  (p99 104 ms); at 200 req/s it adds ~200 ms and from 300 req/s requests fail. That is one router
  process on one 14-vCPU AMD EPYC-Rome cloud machine, with its event loop at ~80 % of a core at 175/s.
