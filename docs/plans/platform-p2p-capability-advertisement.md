# Platform Telemetry: P2P Capability Advertisement

**Status**: Proposed (design questions)
**Date**: April 2026
**Coordinates with**: private platform repo — P2P routing (future phase)
**Related plans**: [platform-local-usage-telemetry.md](./platform-local-usage-telemetry.md),
[multimodal-routing-roadmap.md](./multimodal-routing-roadmap.md)

---

## What this is

The platform is moving toward **peer-to-peer request routing** —
consumers ask the platform to serve a request, the platform picks a
node on the network that can handle it, the node serves the response.
Local-fleet routing (what OSS does today) is already solved. This plan
is about the **node → platform** side of P2P: how the platform knows
what each participating node can serve, so it can pick the right one
when a peer asks.

**This is not the same as the daily-usage telemetry plan.** That plan
is about *what happened yesterday* (dashboards). This plan is about
*what the node can do right now* (routing decisions).

| | Usage telemetry | Capability advertisement |
|---|---|---|
| Purpose | Dashboards, personal analytics | P2P routing decisions |
| Cadence | Once per day | Every heartbeat (~5s) |
| Opt-in? | Yes — default off | No — you can't participate in P2P without advertising |
| Transport | `POST /api/telemetry/local-summary` | Heartbeat payload extension |
| Freshness | Historical aggregates | Near-real-time |

**Status:** *Design questions only* — this plan is not ready to
implement. It captures the questions we need to answer before P2P
routing can ship. Intended as a reference for whoever picks up P2P
routing work next.

## The core problem

If a peer requests inference with 32K tokens of input but the serving
node has `num_ctx=8192` configured for that model, the request fails
or gets silently truncated. For routing to work, the platform needs to
know each node's current per-model ceiling for every routing-relevant
constraint (context length, available VRAM, loaded-vs-cold, expected
latency, service availability).

## Proposed shape

Extend the existing heartbeat payload with a `capabilities` array.
The heartbeat already carries `OllamaMetrics`, `CapacityMetrics`,
`ImageMetrics`, etc. — capabilities is the routing-relevant projection
of all of that.

```json
"capabilities": {
  "models": [
    {
      "model": "llama-3.1:8b",
      "max_context_tokens": 32768,
      "loaded": true,
      "estimated_tokens_per_sec": 95.0,
      "warm_since_seconds": 320
    },
    {
      "model": "llama-3.1:70b",
      "max_context_tokens": 8192,
      "loaded": false,
      "estimated_tokens_per_sec": 22.0,
      "warm_since_seconds": null
    }
  ],
  "services": {
    "stt": { "available": true, "model": "qwen3-asr" },
    "image_generation": { "available": false },
    "vision_embedding": { "available": true, "model": "clip-vit-l-14" }
  },
  "current_load": {
    "queue_depth": 2,
    "vram_fraction_used": 0.62
  }
}
```

The platform side consumes this into a routing table keyed on
`(user_id, node_id)`, freshness-bounded by heartbeat age. Routing
decisions filter on it.

## Design questions we need to answer

### 1. What does `max_context_tokens` actually mean?

There are at least three numbers that could go here:

- **Model-native max** — what the weights support (e.g. llama-3.1 = 128K).
- **`num_ctx` configured on this node** — what the operator has set in
  the node's Ollama config. Typically much lower than native to save
  VRAM (e.g. 8K–32K).
- **Effective right-now ceiling** — accounting for VRAM pressure from
  other loaded models. Often lower still.

OSS already has a "dynamic `num_ctx`" feature that picks a per-request
context length based on VRAM available at request time. The advertised
`max_context_tokens` should probably be *"what I could accept if you
told me to load this model right now and served a request"* — i.e. the
effective ceiling, computed continuously.

**Open question:** how do we compute it without actually trying a load?
An oracle function `estimate_max_context(model, vram_free)` exists
implicitly in the dynamic-num-ctx code — we'd lift it to a public
capability function.

**Risk:** the estimate is wrong. Node advertises 32K, platform routes
a 24K request, node's VRAM got claimed by local work in the meantime,
load fails. Need a reject-and-reroute path (see Q3).

### 2. Model ACL — which models do I offer to the network?

A user running `llama-3.1:405b` on their mac-studio-ultra doesn't
necessarily want strangers tying it up. But they do want the dashboard
to show earnings from `llama-3.1:8b` serving.

Options:

- **All-or-nothing.** One flag: `--p2p-serve / --no-p2p-serve`. If on,
  everything you have is offered. Simplest, least flexible.
- **Allowlist.** Config lists which models to offer. Requires UI for
  managing it (CLI file edit, dashboard toggle, or both).
- **Blocklist.** Advertise everything by default, opt-out specific
  models. Privacy-hostile default — starts by sharing max.
- **Tiered.** Small models (<10B) shared by default; large models
  (≥30B) require explicit opt-in. Balances simplicity with protecting
  scarce resources.

**Recommendation:** tiered default with per-model override. Matches how
people intuitively think about compute cost (running a 7B model is
"cheap", running a 70B model is "expensive").

**Open question:** do we tier by model size (parameters), class
(small/medium/large/xl like the platform's `credit_rates`), or
benchmarked throughput? Parameters is the most intuitive user-facing
number.

### 3. Capability lag and the reject-and-reroute path

Even with 5-second heartbeats, the platform's view of node capability
is always slightly stale. Race:

1. Platform sees: node X has 32K context available for llama-3.1:8b
2. User on node X fires a big local job that eats VRAM
3. Heartbeat hasn't fired yet
4. Platform routes a peer's 24K-token request to node X
5. Node X receives the request → VRAM check fails → ???

Options for step 5:

- **Silent truncate.** Node serves the request at a smaller context,
  answer is wrong. Terrible — failure is invisible.
- **Reject with reroute hint.** Node returns `409 InsufficientCapacity`
  with its current effective ceiling. Platform re-routes to another
  node. Consumer sees ~1 RTT of extra latency, no failed request.
- **Reject hard.** Node returns an error. Consumer's client retries.
  Simpler but worse UX.

**Recommendation:** reject-with-reroute. Platform should handle it
transparently — the consumer shouldn't know.

**Open question:** how many reroutes before we give up? And do we
penalize the rejecting node's quality score for the "bad advertisement"?
Probably yes but lightly (capability estimates are inherently noisy).

### 4. Service availability as a first-class field

OSS already tracks image / STT / vision-embedding service availability
in the heartbeat. For P2P, the platform needs that same signal. Should
it be a subset of `capabilities` (as shown above) or a separate
top-level key?

**Recommendation:** nest under `capabilities.services`. Keeps all
"what can I serve for peers?" data in one place, which is what the
routing engine wants.

### 5. Pricing and its relationship to capability

Separate concern worth flagging: the platform has a `credit_rates`
table that sets earning per Mtok per model class. Today it's
platform-operator-set. In the future, nodes might want to express
"I'll only serve llama-3.1:70b at 1.5× base rate" (offering premium
capacity at a premium price).

**Out of scope for this plan** — pricing is a separate economic
concern. But note: if we allow per-node pricing, it lives on the same
heartbeat as capability advertisement.

### 6. Privacy implications

Unlike usage telemetry (aggregated, opt-in), capability advertisement
is **inherently identifying at the node level** — it tells the platform
which models you have loaded, your hardware (via `tokens_per_sec`
bench), your region. Users consent to this by enabling P2P at all;
opting out means "don't participate."

But: **the data must only be used for routing and your own dashboard.**
It must never be:

- Aggregated into network-wide "models most users run" stats without
  explicit opt-in.
- Exposed to other users via a leaderboard or "nodes near you" view
  (see the rejected features list in the platform dashboard plan).
- Sold or shared.

Write these rules into the platform's `/docs/methodology` and the
privacy policy at the same time capability advertisement ships.

### 7. Where does this data live on the platform?

Routing-speed lookups need to be fast. Proposed:

- A `node_capabilities` table keyed on `(user_id, node_id, model)`.
- Updated via `UPSERT` on every heartbeat.
- Stale rows aged out by a background job (any row older than 2×
  heartbeat interval is dropped from routing consideration).
- In-memory cache (Redis or Postgres `LISTEN/NOTIFY`) for the
  routing hot path.

**Open question:** is this a Postgres table at all, or should it live
in a faster store? Routing decisions happen on every consumer request,
which could be very high volume. Starting in Postgres, moving hot data
to Redis if latency becomes a problem, is the pragmatic path.

## What this plan is NOT designing

Still open and deferred to a proper P2P routing plan:

- **Request routing algorithm** — how the platform picks *which* node
  among the capable set (7-signal scoring? weighted random? round-robin
  by latency tier?).
- **Signed capability claims** — do nodes cryptographically sign their
  capability payload, or is operator-token auth enough?
- **Fraud detection** — nodes claiming capabilities they don't have to
  maximize earnings. OSS already has a quality score concept; extend
  it.
- **Cross-region routing** — latency-aware, region-preferring routing
  logic.
- **Timeout semantics** — how long does the platform wait before
  rerouting? How does the consumer's overall timeout interact?
- **Streaming response routing** — if a node fails mid-stream, can we
  recover?
- **Failure modes when the platform is unreachable** — we probably
  want the local fleet to keep working on local traffic even if the
  platform is down. Capability advertisement fails silently; local
  routing continues.

## Implementation sequencing (when we get there)

Rough sketch — each row is a discrete PR:

1. Lift `estimate_max_context()` from dynamic-num-ctx code into a
   public capability function. Test against current behavior.
2. Build `capabilities` dict in `collector.py` alongside the existing
   metrics. Start by populating from what we already track; leave
   gaps as `null`.
3. Add `capabilities` field to `HeartbeatPayload` (Pydantic model).
4. Platform side (private repo) consumes and stores it in
   `node_capabilities` table. One query: "find nodes that can serve
   model X with ≥ Y context" → returns list, freshness-filtered.
5. Add model ACL config field + CLI flag. Wire into capability
   builder so allowlist is applied at advertise time, not at request
   time.
6. Reject-and-reroute path: `409 InsufficientCapacity` response from
   `node/routes/*` with current capability snapshot. Platform
   side handles reroute.
7. Signed capability claims (maybe — depends on the auth model we
   settle on for P2P overall).

## Status

This plan is **design questions only**. Before any of this gets built,
we need concrete answers — especially for Q1 (what `max_context_tokens`
means), Q2 (model ACL default), and Q3 (reroute semantics). The
recommendations in each question are starting points, not commitments.

When P2P routing becomes active work, fork this plan into a proper
implementation plan with a timeline.

## Out of scope

- The P2P request flow itself (consumer → platform → node → consumer).
  That's a separate design problem that uses this capability data as
  input.
- Usage telemetry (see
  [platform-local-usage-telemetry.md](./platform-local-usage-telemetry.md)).
- Economic pricing (Q5 noted but deferred).
- UI for capability management in the dashboard — comes once we know
  what fields we're displaying.
