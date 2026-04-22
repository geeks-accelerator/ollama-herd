# Platform Telemetry: Local-Usage Daily Summary

**Status**: Proposed
**Date**: April 2026
**Coordinates with**: [private platform repo — Phase 2 of dashboard visualizations plan]

---

## What this is (and what it isn't)

This plan adds an **opt-in** path for `herd-node` to send **daily aggregates**
of local inference usage to the coordination platform at
`gotomy.ai`. The platform's dashboard needs this data to
visualize "local usage alongside what you served for peers" on its
Contribution tab.

**What we send:** daily rollups per-model of request counts, token
totals (prompt + completion), and min/avg/max latency.

**What we never send:** prompt text, completion text, per-request
records, timestamps below day granularity, **tags**, anything that
could classify *what* someone was asking about.

Tags are deliberately excluded. Even though the user chose them, values
like `project:internal-audit` can reveal intent, and keeping the wire
shape "model counts only" makes the privacy story trivial to verify by
eye.

This is symmetric with the existing `latency_observations` schema on our
side — token counts and timing only, no content. We're extending the
same privacy guarantee outward to the platform.

## Why now

Phase 1 of the platform dashboard shipped already (private repo). Users
who sign in see their P2P earnings (requests their node served for
peers), but the Contribution chart has a placeholder banner saying
*"Local usage isn't visible yet. When `herd-node` v0.6 ships with the
opt-in telemetry flag, you'll see a second colored series here."*

Phase 2 on the platform side needs us to ship the emitter. Their side
of the contract is the `POST /api/telemetry/local-summary` endpoint;
ours is everything that produces the payload and sends it.

## What the platform expects

### HTTP contract

```
POST https://gotomy.ai/api/telemetry/local-summary
Authorization: Bearer herd_<hex>         # same operator token as P2P auth
Content-Type: application/json

{
  "day": "2026-04-20",                   # ISO date, UTC, always yesterday (never today)
  "node_id": "bb9a4c8f-...",             # the platform-issued node UUID (see below)
  "entries": [
    {
      "model": "llama3.2:3b",
      "local_requests": 42,              # requests served from this node for our own traffic
      "local_prompt_tokens": 8400,
      "local_completion_tokens": 12003,
      "p2p_served_requests": 0,          # requests served on behalf of a platform peer
      "p2p_served_tokens": 0,            # (always 0 until P2P routing ships — see "Forward compatibility")
      "min_latency_ms": 98.2,
      "avg_latency_ms": 245.3,
      "max_latency_ms": 1423.7
    }
  ]
}
```

### Response

Platform returns the standard HATEOAS envelope with `next_steps`.
`200 OK` on success. `401` if the operator token is invalid. `409` if a
summary for `(user, node, day)` was already ingested — idempotent retries
should be safe and silently succeed, so we should detect 409 and treat
it as success locally.

### Rate limit

Platform limits telemetry to **1 call per node per day**. We only need
to call it once a day per node after midnight UTC for the previous day.

### Forward compatibility

`p2p_served_requests` and `p2p_served_tokens` stay at `0` until OSS
implements P2P routing (out of scope here). Shipping both fields now
means when P2P lands, the platform dashboard's "three-color stacked
area" (local / served / consumed) just works — no second migration.

## Design Decisions

### Opt-in, default off

Telemetry is valuable only if users trust it. Default **off**, one flag
to enable: `--telemetry-local-summary`. Adding it to config also works
(same flag name).

The dashboard already shows a clear banner explaining what the platform
ingests vs never ingests when telemetry is disabled. The flag docs must
repeat that promise.

### Daily rollup on the node, not per-request streaming

Per-request streaming would let the platform build richer views, but:

1. Every request becomes an external network hop — we'd be adding
   reliability risk to the happy path.
2. It's overkill for the viz we're powering (stacked area over time).
3. It breaks the privacy story: "we send aggregates" is a simple claim
   that's verifiable by eye; "we send aggregates derived from a stream
   of per-request events" is not.

So: one daily aggregate, computed from existing `latency_observations` +
`request_traces` tables, sent once per day per node.

### Authentication reuses the operator token

The node already has an operator token (stored under `~/.fleet-manager/`
alongside the existing state files — `latency.db`, capacity-learner
state, logs; same thing we use for heartbeats to the platform once
P2P ships). No new auth mechanism.
If the user hasn't registered their node with the platform (via
`herd-node register --platform-token ...`), telemetry is a no-op — no
error, no retry loop, just a debug log line.

### Node ID: platform's UUID, not our local hostname

The platform issues a UUID when a node registers (via
`POST /api/nodes/register`). We store it locally alongside the operator
token. Telemetry payloads reference that UUID, not our `node_id`
hostname-based identifier — the platform's dashboards key on the UUID.

If we haven't registered yet, telemetry is a no-op (same as above).

## Implementation

### Files to create

| File | Purpose |
|------|---------|
| `src/fleet_manager/node/platform_client.py` | Thin httpx wrapper around `gotomy.ai/api/*`. Used here + future P2P heartbeat work. Single `AsyncClient` with 10s timeout, exponential retry on 5xx / network error up to 3 tries. |
| `src/fleet_manager/node/daily_rollup.py` | Aggregates yesterday's rows from `latency_observations` + `request_traces` into the telemetry payload shape. |
| `src/fleet_manager/node/telemetry_scheduler.py` | Fires once per day at ~00:05 UTC; calls `daily_rollup.build()` then `platform_client.post_local_summary()`. Handles 409 (idempotent) and errors. |
| `tests/test_node/test_daily_rollup.py` | Unit tests over a seeded SQLite db. |
| `tests/test_node/test_platform_client.py` | `pytest-httpx` tests covering success / 401 / 409 / 5xx-retry paths. |

### Files to modify

| File | Change |
|------|--------|
| `src/fleet_manager/cli/node_cli.py` | Add one Typer option: `--telemetry-local-summary / --no-telemetry-local-summary` (default `False`, env: `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY`). When on, the agent spawns `telemetry_scheduler` as an asyncio task alongside the heartbeat loop. |
| `src/fleet_manager/node/agent.py` | Wire the scheduler task; share the existing `httpx.AsyncClient` with `platform_client` rather than creating a new one. On every scheduled build, log the payload at DEBUG level so users running `--log-level debug` can see exactly what's being sent. |
| `src/fleet_manager/models/config.py` | Add one `NodeSettings` field (`telemetry_local_summary: bool`) following the existing Pydantic `NodeSettings` pattern. Also add fields to persist the platform-issued node UUID + operator token so the scheduler knows whether it can run. |
| `src/fleet_manager/__init__.py` | **Prerequisite fix:** sync `__version__` with pyproject (currently desynced — `__init__.py` says `0.3.0`, pyproject says `0.5.2`). Drop `__version__` down to a single source of truth by importing from pyproject metadata, or pin both to the same value manually. |
| `CHANGELOG.md` | Add an entry under `[Unreleased]` → `Added` describing the telemetry feature. **The version bump that cuts `[Unreleased]` → `[x.y.z]` isn't driven by this feature alone** — there's substantial other unreleased work (vision embeddings, priority preloader, SSE watchdog) that belongs in the same release. Don't bump the version number as part of this plan; that happens at release-cut time. |
| `docs/api-reference.md` | Document the new flag + the telemetry privacy promise. |
| `docs/website/skills.md` (if listing CLI flags) | Add `--telemetry-local-summary`. |

### Aggregation SQL (read-only, one query)

One SELECT over `latency_observations`. No join, no second query, no
percentile helper dependency — `MIN` / `AVG` / `MAX` are all you need.

```sql
-- Columns we read: model_name, prompt_tokens, completion_tokens,
-- latency_ms, timestamp. Nothing else.
SELECT
  model_name                AS model,
  COUNT(*)                  AS local_requests,
  SUM(prompt_tokens)        AS local_prompt_tokens,
  SUM(completion_tokens)    AS local_completion_tokens,
  MIN(latency_ms)           AS min_latency_ms,
  AVG(latency_ms)           AS avg_latency_ms,
  MAX(latency_ms)           AS max_latency_ms
FROM latency_observations
WHERE timestamp BETWEEN :start_of_yesterday_utc AND :end_of_yesterday_utc
GROUP BY model_name;
```

The `request_traces` table is not touched by this feature.

### Scheduler timing

Run once per day at 00:05 UTC + **`random.uniform(0, 600)` seconds of
jitter, generated fresh each day**. This produces smooth load at the
platform (not predictable by hashing node UUIDs) while keeping each
node's emit time roughly consistent day-to-day when measured over
long periods.

### Always POST; let 409 be the idempotency guard

No local state file. No GET-before-POST handshake. Every scheduled
fire (and once at agent startup) just POSTs the summary and accepts
either `200` (ingested) or `409` (already ingested for this day) as
success.

This is the simplest possible correctness story:

- No `~/.fleet-manager/telemetry_state.json` to maintain.
- No SIGTERM ordering concerns — there's nothing to write.
- Crash recovery is trivial: restart, POST, platform dedupes.
- One platform endpoint to build (POST), not two.

On agent startup, fire immediately rather than waiting for the next
00:05 UTC window. Users who run the node only during business hours
still get yesterday's telemetry sent. If today's already been sent,
409 returns quickly and we log a debug line.

### Payload size — one cap

**Max 50 entries per payload.** If a user somehow has more than 50
distinct models in one day (unlikely — typical fleet runs 2–8 models),
truncate to the top 50 by `local_requests` and log a warning.

Without tags each entry is ~100 bytes, so 50 entries = ~5 KB max.
No separate byte-size cap needed.

Enforced in `daily_rollup.build()` before the payload reaches
`platform_client.post_local_summary()`. One test asserts the cap
rejects oversized synthetic input cleanly.

## Privacy & Security

### The promise we're keeping (in code + docs)

The telemetry emitter's **only** read is:

- `latency_observations`: `model_name`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `timestamp`

It **never reads**:

- Prompt body / completion body (the router deliberately doesn't store these in OSS already)
- `request_traces` at all — no tags, no request IDs, no anything from that table
- Request ID, user IP, request source, `fallback_used`, `excluded_nodes`,
  `scores_breakdown`, `error_message` — none of these leave the node

Enforce this structurally: `daily_rollup.build()` should only query the
whitelisted columns from `latency_observations`. A test asserts that
the built payload dict's keys match exactly the expected set — prevents
future contributors from casually adding a field.

### Why no tags

Even though users explicitly set tags (`agent:claude-code`,
`project:internal-compliance-audit`), those values reveal intent:
which projects, which codebases, which internal tools. Keeping the
wire shape "model counts only" makes the privacy story trivial to
verify by eye. If a future iteration wants per-tag dashboards, it'll
be a separate feature with its own opt-in.

### Inspecting what will be sent

No bespoke dry-run flag. Users who want to see the exact payload
before committing run:

```
herd-node --log-level debug --telemetry-local-summary
```

The agent logs the full payload at DEBUG level on every build, then
sends. This is the Unix way — inspection uses general-purpose tooling
rather than a feature-specific knob.

### Environment-variable parity

The flag has a matching env var, following the existing convention
(e.g. `FLEET_NODE_ENABLE_CAPACITY_LEARNING=true`):

| Flag | Env var |
|---|---|
| `--telemetry-local-summary` | `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY` |

Matters for homelab and containerized deployments where editing
CLI arguments is awkward.

### Retention policy is a prerequisite

**Not shipping until the platform team publishes a concrete retention
number.** A CLI `--help` that says "contact the platform team to
understand how long your data is kept" is not a trust-first feature.

Target answer: `local_usage_rollups` retained **90 days rolling**,
documented in `/docs/methodology`. CLI help and opt-in copy reference
the number directly:

```
--telemetry-local-summary  Send anonymous daily rollups to the
                           platform. Platform retains rows for 90
                           days. See https://gotomy.ai/docs/methodology
```

If the platform team needs different numbers, agree before we ship.

### Disabling

`--no-telemetry-local-summary` stops the scheduler immediately. Users
who want to scrub previously-ingested data contact the platform
operators via the address on `/docs/methodology`. (Platform-side
delete-my-telemetry endpoint is their roadmap item, not ours.)

### Log what we sent

The agent writes one debug log line per daily emit:
```
telemetry: sent daily summary for 2026-04-20 — 4 entries, 42 total requests, accepted
```
No payload bodies in logs. Users can `--log-level debug` to see this;
default `INFO` stays quiet.

## Testing

### Unit — `test_daily_rollup.py`

- Seed a temp SQLite with 24 hours of `latency_observations` spanning
  2 days. Assert the build returns one entry per model with correct
  sums, counts, and min/avg/max latency.
- Edge: day with no requests → `entries: []`.
- Edge: single-sample model → `min == avg == max`.
- **Privacy invariant:** assert the payload dict's keys are *exactly*
  the whitelisted set — no leaking fields. Also assert that the code
  path never opens or queries `request_traces` (can be enforced by
  monkey-patching the trace store to raise on any call).
- **Cap test:** synthesize 100 models → assert payload truncated to
  50 entries (sorted by `local_requests` desc), truncation logged
  at WARNING.

### Integration — `test_platform_client.py`

Use `pytest_httpx` to stub `gotomy.ai`:

- **`200`** → one POST made; logged at INFO. Next scheduler fire
  today also POSTs and gets `409` (the idempotency test below).
- **`409`** → treated as success; logged at DEBUG.
- **`401`** → log a clear "operator token rejected, check registration"
  warning; don't retry.
- **`503`** → three retries with exponential backoff, then give up and
  log; the next day's scheduler run will try again.
- **Network error** → same as `503`.

### Integration — CLI smoke

With `--telemetry-local-summary` flag on and a mocked platform
responding `200`, run the agent for 1 second (or trigger the scheduler
manually via the test harness) and assert one POST happened and the
payload was also logged at DEBUG level.

With flag off, assert zero POSTs.

With `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY=true` env var and no CLI
flag, assert behavior is identical to passing the CLI flag
(parity test).

## Release coordination

The platform's `POST /api/telemetry/local-summary` endpoint is
non-existent as of this plan. The platform team needs to:

1. Ship the endpoint + the `local_usage_rollups` Postgres table.
2. Light up the three-color Contribution chart to show the new data.

Ship our end of the wire on the same day they ship theirs. Suggested
version label: `herd-node 0.6.0 "Telemetry"`.

Backwards compatibility: older `herd-node` versions ignore the flag
(it doesn't exist for them) and just don't send telemetry. Nothing
breaks.

## Coordination with the platform team

### Prerequisites (must be agreed before we ship)

1. **`POST /api/telemetry/local-summary`** — the ingest endpoint
   itself. Returns the standard HATEOAS envelope, `200` on success,
   `409` on duplicate `(user, node, day)` (idempotent — we treat as
   success), `401` on invalid operator token.
2. **Retention policy.** Platform team publishes a concrete number —
   recommended **90 days rolling**, documented on `/docs/methodology`.
   Required because the CLI `--help` text quotes it verbatim.
3. **Timezone of the `day` field.** We're assuming UTC. If the platform
   would rather key on user-local timezone, we'd need the user's
   timezone in the payload (which reveals approximate location).
   **Recommend: stay UTC;** dashboard displays in the user's local
   time at render-time.

### Nice to have (platform-side, not blocking)

1. **"Last telemetry received" display on the dashboard Settings tab.**
   The platform already stores every ingested payload, so it can
   render "last we received from `mac-studio-1`: 4 entries, 42
   requests, 2026-04-19" using its own `local_usage_rollups` table —
   no extra API call from the node needed. Closes the loop for users
   who want visual confirmation that telemetry landed.

## Out of scope

- P2P request routing (a separate future plan; tracked under
  `multimodal-routing-roadmap.md`).
- **Per-node per-model capability advertisement** (e.g. "this node
  can accept llama-3.1:8b requests up to 32K tokens"). This is a
  routing concern, not a usage-telemetry concern — it needs real-time
  state on every heartbeat, not a once-a-day aggregate. Belongs in
  the P2P routing plan. Flagged here because it's an easy thing to
  confuse with telemetry; they're different data paths.
- Real-time streaming telemetry (see "Design Decisions").
- Platform-side ingestion code (their repo).
- Dashboard UI changes (their repo).
- User-initiated "delete my telemetry" endpoint (platform handles this
  through their own support flow).

## Timeline

| Day | Work |
|---|---|
| 0 | Plan review, align with platform team on prerequisites |
| 1 | `platform_client.py` (POST only) + `daily_rollup.py` + unit tests |
| 2 | `telemetry_scheduler.py` + wiring in `agent.py` + one CLI flag |
| 3 | Integration tests, docs, CHANGELOG `[Unreleased]` entry. Version number is set at release-cut time, bundled with other unreleased work. |
| 4 | Platform team ships POST endpoint; coordinated release |

**~2 engineer-days** — one CLI flag, one endpoint, one SQL query,
one payload cap, no state file, no dry-run flag, no `agent_version`,
no percentile helper.
