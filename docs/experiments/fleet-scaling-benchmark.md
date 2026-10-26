# Concurrent agents on 1, 2 and 4 machines

**Status:** procedure only. Not run yet; needs up to four fleet machines.

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
