# Platform Connection UX

**Status**: Proposed
**Date**: April 2026
**Coordinates with**: private platform repo (uses existing endpoints, no platform-side changes)
**Related plans**: [platform-local-usage-telemetry.md](./platform-local-usage-telemetry.md) (depends on this), [platform-p2p-capability-advertisement.md](./platform-p2p-capability-advertisement.md) (depends on this)

---

## What this is

A user-facing flow for opting a node into the coordination platform at
`gotomy.ai`. Runs in the OSS dashboard's Settings tab
(`http://localhost:8001/settings`) so users don't have to SSH into a
headless mac-mini and edit YAML.

**This plan is the prerequisite for any other platform-connected
feature.** Telemetry (a separate plan) and future P2P capability
advertisement both need a node to already be connected. Shipping
telemetry without this plan means only power users who edit CLI flags
will ever turn it on — the friction kills adoption.

## The friction we're fixing

Today, connecting a node to the platform would require:

1. Install `herd-node`, runs locally, works fine
2. Visit `ollamaherd.com`, sign up, navigate to `gotomy.ai/web/`
3. Create an operator token, copy it
4. **SSH into the node**, edit `~/.fleet-manager/config.yaml` or pass CLI flags
5. Restart the agent, read logs to verify

Step 4 is where 80% of non-developer users drop off.

The new flow collapses steps 4-5 into a paste-and-click on a dashboard
the user already has open.

## End-to-end user story

1. User has `herd-node` running locally, with the OSS dashboard open at
   `localhost:8001`.
2. User signs up at `gotomy.ai`, generates an operator
   token (shown once), copies it.
3. User switches to the local OSS dashboard's **Settings** tab.
4. New **Platform connection** card shows *"Not connected"* with an
   input field and a *Connect* button.
5. User pastes the `herd_…` token, clicks Connect.
6. Dashboard calls `POST /api/platform/connect` on the **local** OSS
   server. Behind the scenes:
   - Server validates the token by calling
     `GET https://gotomy.ai/api/auth/me`
   - Generates (or loads) an Ed25519 keypair for this node
   - Runs a quick benchmark if we don't have a fresh one
   - Calls `POST https://gotomy.ai/api/nodes/register`
     with the benchmark + public key
   - Persists: operator token, platform-issued node UUID, platform URL
     to `~/.fleet-manager/`
7. Card now shows *"Connected as devuser — node mac-studio-1"* with
   feature toggles: *"Share local usage telemetry"*, *"Offer capacity
   to the network"* (disabled until P2P ships).
8. Disconnect is one click; state goes back to *"Not connected"* and
   all platform-dependent scheduler tasks halt.

## Requirements

### OSS dashboard — new Settings-tab card

One card on the existing Settings page. Three visual states, selected
by a single enum in a GET `/api/platform/status` response:

#### State: `not_connected`

```
┌─ Platform connection ────────────────────────────────────┐
│                                                          │
│  Not connected.                                          │
│                                                          │
│  Connect this node to gotomy.ai to earn    │
│  credits for serving peers, see historical usage on the  │
│  dashboard, and participate in the network.              │
│                                                          │
│  You keep your fleet private by default — nothing leaves │
│  this machine until you explicitly enable a feature.     │
│                                                          │
│  Get an operator token at gotomy.ai/web/   │
│                                                          │
│  ┌──────────────────────────────────────────┐ [Connect] │
│  │ herd_…                                   │            │
│  └──────────────────────────────────────────┘            │
│                                                          │
│  Platform URL: https://gotomy.ai   [edit]  │
└──────────────────────────────────────────────────────────┘
```

#### State: `connecting`

```
┌─ Platform connection ────────────────────────────────────┐
│                                                          │
│  Connecting…                                             │
│                                                          │
│  ✓  Token validated                                      │
│  ⟳  Benchmarking (this may take ~30 seconds)             │
│     Registering node…                                    │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Progress lines update as the backend reaches each step.

#### State: `connected`

```
┌─ Platform connection ────────────────────────────────────┐
│                                                          │
│  ✓ Connected as devuser (2026-04-20)                    │
│    Node: mac-studio-1  ·  class: premium  ·  92 tok/s   │
│    Platform: gotomy.ai      [Disconnect]  │
│                                                          │
│  ┌─ Features ───────────────────────────────────────┐   │
│  │  ☐ Share local usage telemetry                   │   │
│  │     Daily aggregate per model — no prompts.      │   │
│  │                                                  │   │
│  │  ☐ Offer capacity to the network   (coming)      │   │
│  │     Serve inference for peers, earn credits.     │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

Each feature toggle is a separate setting. Connection state is a
prerequisite, not a feature itself — toggling features is independent.

### OSS server — three new routes

Mounted under `src/fleet_manager/server/routes/platform.py`. Localhost
only (same trust model as the rest of the OSS dashboard — anyone with
LAN access to the dashboard can modify these; not-our-problem for v1,
same as every other Settings toggle).

| Route | Purpose |
|---|---|
| `GET /api/platform/status` | Return the current connection state + identity + feature toggles |
| `POST /api/platform/connect` | Validate token → bench if needed → register node → persist |
| `POST /api/platform/disconnect` | Revoke local state, stop platform-dependent tasks. Does **not** delete data on the platform — user does that via the platform dashboard. |

#### `GET /api/platform/status`

```json
{
  "state": "connected",                              // "not_connected" | "connecting" | "connected" | "error"
  "platform_url": "https://gotomy.ai",
  "connected": {
    "user_email": "devuser@example.com",             // from GET /api/auth/me
    "user_display_name": "devuser",
    "node_id": "3723887e-...",                       // platform-issued UUID
    "node_name": "mac-studio-1",
    "throughput_class": "premium",
    "tokens_per_sec": 92.0,
    "connected_at": "2026-04-20T17:55:00Z"
  },
  "features": {
    "telemetry_local_summary": false,
    "p2p_serve": false                               // disabled in UI until P2P ships
  },
  "error": null                                      // populated only when state == "error"
}
```

#### `POST /api/platform/connect`

Request:
```json
{
  "operator_token": "herd_2a9860...",
  "platform_url": "https://gotomy.ai"  // optional; defaults to production
}
```

Handler (pseudocode):

```python
async def connect(body):
    # 1. Validate by hitting the platform's /api/auth/me
    async with httpx.AsyncClient() as h:
        r = await h.get(f"{body.platform_url}/api/auth/me",
                        headers={"Authorization": f"Bearer {body.operator_token}"})
    if r.status_code != 200:
        return err("Invalid operator token — see gotomy.ai/web/")

    # 2. Ensure we have an Ed25519 keypair (generate if missing)
    keypair = await ed25519_keypair.load_or_generate()

    # 3. Ensure we have a recent benchmark (<24h old); else run one
    benchmark = await benchmark_store.latest_or_run(max_age_hours=24)

    # 4. Register the node
    async with httpx.AsyncClient() as h:
        r = await h.post(f"{body.platform_url}/api/nodes/register",
                         headers={"Authorization": f"Bearer {body.operator_token}"},
                         json={
                             "name": settings.node_id or socket.gethostname(),
                             "public_key": keypair.public_b64,
                             "benchmark": benchmark.to_platform_payload(),
                             "region": settings.region,  # optional
                         })
    if r.status_code == 409:
        # Existing registration with same pubkey. Load the existing node_id.
        existing_node_id = r.json()["details"]["existing_node_id"][0]
        node_id = existing_node_id
    elif r.status_code == 201:
        node_id = r.json()["data"]["id"]
    else:
        return err(f"Registration failed: {r.status_code} {r.text}")

    # 5. Persist everything atomically
    await node_settings.update(
        platform_url=body.platform_url,
        platform_operator_token=body.operator_token,
        platform_node_id=node_id,
        platform_connected_at=datetime.now(timezone.utc),
    )
    return ok({"node_id": node_id})
```

#### `POST /api/platform/disconnect`

Request body: empty.

Handler:

```python
async def disconnect():
    # 1. Stop any running platform-dependent background tasks
    #    (telemetry scheduler, future P2P advertiser)
    await agent.stop_platform_tasks()

    # 2. Clear persisted state
    await node_settings.update(
        platform_url=None,
        platform_operator_token=None,
        platform_node_id=None,
        platform_connected_at=None,
    )

    # 3. Note — DO NOT call platform's deregister endpoint.
    #    The user might reconnect later from the same machine; the
    #    platform-side node record should remain until the user
    #    deletes it from the platform dashboard.
    return ok({"state": "not_connected"})
```

The comment on step 3 is deliberate: disconnecting from the node side
is distinct from deleting the node from the platform side. The user's
earnings history, ledger entries, and registration survive a local
disconnect. To fully delete, they visit the platform dashboard and
hit "Deregister node."

### NodeSettings additions

Add four fields in `src/fleet_manager/models/config.py`:

```python
class NodeSettings(BaseSettings):
    # ... existing fields ...

    # Platform connection (all None when disconnected)
    platform_url: str | None = None
    platform_operator_token: SecretStr | None = None     # 0600 file, never logged
    platform_node_id: str | None = None
    platform_connected_at: datetime | None = None
```

Use `pydantic.SecretStr` for the token so it doesn't accidentally
leak into repr() / str() / logs. On serialize, always redact.

Persist as a single JSON file at `~/.fleet-manager/platform.json`
with mode `0600` at write time. Keep the operator token out of the
main `config.yaml` so it doesn't show up in config dumps.

### CLI parity — unchanged

The existing CLI flags keep working unchanged. This plan adds the
dashboard as a *second* path to the same settings, not a replacement:

```bash
# These still work exactly as today
herd-node --platform-token herd_xxx --platform-url https://gotomy.ai
```

Mapping:

| Dashboard action | Equivalent CLI | Env var |
|---|---|---|
| Paste token + Connect | `--platform-token herd_xxx` | `FLEET_NODE_PLATFORM_TOKEN` |
| Change platform URL | `--platform-url https://...` | `FLEET_NODE_PLATFORM_URL` |
| Disconnect | Not set / unset env var | — |
| Enable telemetry feature | `--telemetry-local-summary` | `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY` |

All three paths (dashboard, CLI, env var) write to the same
`NodeSettings`. Last-writer-wins during agent startup, normal Pydantic
resolution rules apply.

## Design Decisions

### Benchmark-on-connect, with fallback

If we have a recent benchmark (<24h old), reuse it. If not, run a
quick synthetic bench as part of the Connect flow — takes ~30 seconds
on most hardware. User sees the progress bar in the `connecting`
state.

Skipping the bench entirely and registering with a placeholder would
misclassify the node's throughput class on the platform side, which
matters for earning-rate calculations. Worth the 30 seconds.

### Disconnect is local-only

Doesn't deregister the node from the platform. Reasons:

1. The node UUID + earnings history should survive a reconnect from
   the same machine.
2. Full deregistration is a more destructive operation that deserves
   a separate confirm flow, on the platform dashboard where full
   context is visible.
3. The "disconnect" semantics users expect from a toggle are "stop
   syncing", not "erase everything I've ever sent."

Surface this distinction in the UI:

> Disconnecting stops your node from communicating with the platform.
> Your earnings and node history remain on the platform. To fully
> remove your node, visit gotomy.ai/web/ and deregister it there.

### OSS dashboard is unauthenticated localhost — trust model unchanged

Anyone with LAN access to `localhost:8001` can already change every
other Settings toggle. Connecting / disconnecting the platform is no
different in principle. We document the risk ("don't expose the OSS
dashboard beyond your LAN") and move on.

v2 could add a cookie-based auth layer, but that's a separate plan.

### Error states surface to the UI, not just logs

Every failure path returns a structured error to the dashboard so the
user sees a useful message:

| Failure | UI message |
|---|---|
| Token format invalid | *"Tokens start with `herd_` — check what you pasted."* |
| Token rejected by platform | *"That token isn't valid. Generate a new one at gotomy.ai/web/"* |
| Platform unreachable | *"Can't reach gotomy.ai. Check your internet connection."* |
| Ed25519 already registered to another user | *"This machine's key is already registered to a different account. Run `herd-node rotate-keys` to reset."* |
| Benchmark failed | *"Benchmark didn't complete. Try running `herd-node benchmark` manually, then reconnect."* |

## Files

### Create

| File | Purpose |
|---|---|
| `src/fleet_manager/server/routes/platform.py` | Three new OSS-server routes (`status`, `connect`, `disconnect`) |
| `src/fleet_manager/node/platform_connection.py` | Pure-logic module called by the routes. Token validation, keypair load/generate, benchmark-or-run, registration, persistence. Unit-testable without the HTTP layer. |
| `src/fleet_manager/server/static/settings_platform_card.html` (or wherever the Settings tab lives) | The new card markup + JS. Follows the existing OSS dashboard's vanilla-JS pattern. |
| `tests/test_node/test_platform_connection.py` | Unit tests covering validation, error paths, persistence. |
| `tests/test_server/test_platform_routes.py` | Integration tests covering the three routes, mocking the upstream platform with `pytest_httpx`. |

### Modify

| File | Change |
|---|---|
| `src/fleet_manager/models/config.py` | Add four `platform_*` fields to `NodeSettings`. |
| `src/fleet_manager/server/routes/__init__.py` | Mount the new `platform` router. |
| `src/fleet_manager/cli/node_cli.py` | Add `--platform-token` and `--platform-url` options (env var parity as per the existing convention). |
| `src/fleet_manager/node/agent.py` | On startup, if platform is connected, also spawn the platform-dependent tasks (telemetry scheduler, future P2P advertiser). |
| OSS dashboard Settings-tab page | Add the card. Wire it to the three new routes. |
| `docs/api-reference.md` | Document the three new OSS-side routes + the connection flow. |
| `CHANGELOG.md` | Entry under `[Unreleased]` → `Added`. |

## Testing

### Unit — `test_platform_connection.py`

- `validate_token()` with a mocked 200 from `/api/auth/me` → returns
  user info.
- `validate_token()` with a mocked 401 → raises `InvalidTokenError`.
- `load_or_generate_keypair()` on a fresh machine → creates file at
  `~/.fleet-manager/node_key.ed25519` with mode 0600.
- `load_or_generate_keypair()` with existing file → loads it, doesn't
  regenerate.
- `persist_connection_state()` writes `platform.json` with mode 0600
  and never includes plaintext token in any *other* file (grep the
  config dump output).

### Integration — `test_platform_routes.py`

Using `pytest_httpx` to stub `gotomy.ai`:

- `POST /api/platform/connect` happy path → 200, status endpoint
  reflects `connected`, `~/.fleet-manager/platform.json` exists.
- Connect with a bad token (platform returns 401) → route returns
  400 with the "Invalid token" message, nothing persisted.
- Connect when platform returns 5xx → route returns 502 with
  "platform unreachable" message.
- `POST /api/platform/disconnect` from a connected state → `platform.json`
  cleared, subsequent `GET /api/platform/status` shows `not_connected`.
- Disconnect when not connected → no-op success (idempotent).

### Manual / smoke

Run `herd-node serve`, open the dashboard, paste a real token against
a local Supabase stack, confirm the Settings card transitions through
all three visual states and that `~/.fleet-manager/platform.json` has
the expected contents.

## Privacy & Security

### Token storage

The operator token is a bearer credential. Store it in
`~/.fleet-manager/platform.json` with mode `0600` (user-only readable).
Same trust model as `~/.ssh/*` — if a user's home directory is
compromised, they have bigger problems.

macOS Keychain integration is nice-to-have for v2; a 0600 file matches
where `latency.db` and capacity-learner state already live.

### What the OSS server sends to the platform during Connect

Exactly:
- The operator token (in `Authorization` header)
- The node's Ed25519 public key
- The benchmark result (throughput, model used, hardware summary —
  same data the `POST /api/nodes/register` endpoint documents)
- A name (default = hostname; user can edit before clicking Connect
  — *nice-to-have for v1, required for v1.1*)
- Region string (optional, from config)

**Not sent:** anything else. The OSS server does not transmit the
contents of `latency.db`, the trace store, or any request history
as part of Connect.

### What the platform sees afterward

Once connected, only the features the user explicitly enables
transmit data. Telemetry (a separate plan) sends daily aggregates
only. P2P capability advertisement (a separate plan) sends per-model
ceilings on the heartbeat. Each feature has its own opt-in; Connect
alone is dormant until the user turns one on.

Surface this clearly in the `connected` state's description — users
should understand that connecting isn't the same as sharing data.

### Audit log on the node

Every Connect / Disconnect action logs a single line at INFO level:

```
platform-connect: connected to https://gotomy.ai as devuser (node_id=3723...)
platform-connect: disconnected
```

No tokens in logs. Errors log at WARNING with the platform's error
message but not the token.

## Platform-side notes (informational, no work required)

All endpoints this plan depends on **already exist** on the platform:

- `GET /api/auth/me` — token validation + identity lookup
- `POST /api/nodes/register` — node registration, returns UUID

No private-repo changes required to ship v1 of this plan.

**Nice-to-have** (platform team's roadmap, not blocking):

- A `GET /api/nodes/by-public-key` lookup that would let the OSS
  side check "is this Ed25519 key already registered to me?" before
  attempting to register. The current 409-on-duplicate handling is
  sufficient; this would just be cleaner UX.
- A dashboard banner on the platform side when a node hasn't
  heartbeat'd in >24h — helps users notice stale connections from
  retired machines.

## Out of scope

- Supabase OAuth redirect flow *inside* the OSS dashboard. We
  deliberately don't try to embed "Sign in with GitHub" in the
  localhost dashboard — the user signs up on
  `gotomy.ai/web/` using their real browser, then
  pastes a token into the local dashboard. Keeps the OAuth flow
  where it already works, no iframe security headaches.
- Cookie-based auth on the OSS dashboard (v2 concern).
- macOS Keychain for token storage (v2 nice-to-have).
- Multi-platform connections (connecting one node to two platforms
  simultaneously). One at a time; switch by disconnect → connect.
- Automatic reconnection if the token is revoked server-side
  mid-session. Detected on next API call; user gets a warning banner.

## Timeline

| Day | Work |
|---|---|
| 0 | Review |
| 1 | `platform_connection.py` module + unit tests (no HTTP yet) |
| 2 | `platform.py` routes + `pytest_httpx` integration tests |
| 3 | `NodeSettings` additions + CLI flag + env var parity |
| 4 | Settings-tab card UI + end-to-end manual test against the local Supabase stack |
| 5 | Docs + CHANGELOG entry |

**~4 engineer-days.** Most of the complexity is in the error-state UX;
the happy path is thin glue over endpoints that already work.

## Ordering relative to the other platform plans

This plan is the **prerequisite**. Without it:

- The telemetry plan ([platform-local-usage-telemetry.md](./platform-local-usage-telemetry.md))
  has no way to get an operator token + node UUID onto the node
  except "edit YAML," which kills adoption.
- The P2P capability plan
  ([platform-p2p-capability-advertisement.md](./platform-p2p-capability-advertisement.md))
  has the same problem plus is many months out regardless.

**Ship this first.** Then ship telemetry on top of it, using the
persisted `platform_operator_token` + `platform_node_id` fields this
plan puts in place.
