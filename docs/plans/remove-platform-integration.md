# Remove the gotomy.ai platform integration

**Status**: 📋 **PROPOSED — 2026-10-24.** Inventory verified against the tree on
that date (importers traced, not inferred from names).
**Target release**: `0.12.0` — removing endpoints, settings and a dependency is a
breaking change even though nothing that works is lost.
**Supersedes**: [`platform-connection-ux.md`](platform-connection-ux.md),
[`platform-local-usage-telemetry.md`](platform-local-usage-telemetry.md),
[`platform-p2p-capability-advertisement.md`](platform-p2p-capability-advertisement.md).

---

## Why

Every platform feature in herd talks to a coordination service that was never
deployed. `https://gotomy.ai` answers `308 → https://ollamaherd.com/` (checked
2026-10-24), and there is no service behind it. So `herd --cloud`, the node's
platform connection, the signed platform heartbeat and the **account** usage
telemetry cannot work for any user today. Keeping them means maintaining roughly
2,000 lines of code, five test modules and two dependencies for features that do
nothing.

CLAUDE.md's greenfield rule applies: delete the feature, no flag. **One deliberate
exception, below:** the command-line flags stay accepted, so nobody's start command
breaks.

**Not affected:** the **anonymous community telemetry** pipeline. It is a separate
pipeline: the router sends it, keyed by a random `install_id`, to
`ollamaherd.com/api/v1/telemetry`, which *is* deployed. It stays, byte for byte.
`ollamaherd.com/telemetry` is the contract.

## The exception: removed flags stay accepted, and warn

A removed Typer option makes the process exit with `No such option`. Under the
launchd and systemd keep-alive agents that becomes a crash loop: an upgrade that
looks like an outage, with no explanation. None of these flags is documented in the
README, the skills, the guides or the example plists (only
`--telemetry-local-summary` appears, once, in `docs/api-reference.md`), and the
platform never worked, so very few people pass them. But "very few" isn't zero, and
the cost of tolerance is a few lines.

So each removed flag becomes a **hidden, accepted, ignored** option that logs one
WARNING saying it was removed, why, and to drop it from the start command:

| Command | Flags kept as hidden no-ops |
|---------|-----------------------------|
| `herd` | `--cloud`, `--token`, `--cloud-url` |
| `herd-node` | `--platform-token` (and its alias `--operator-token`), `--platform-url`, `--telemetry-local-summary`, `--telemetry-include-tags` |

- `hidden=True`, so `--help` no longer shows them to new users.
- Nothing reads their values beyond deciding whether to warn.
- **Rejected alternative:** Typer's `ignore_unknown_options`. It would also keep
  old commands working, but it silently swallows *every* unknown flag, including
  typos like `--prot 11435`.
- **Removal version: `0.14.0`.** Write it in the warning text and in CLAUDE.md, so
  the exception neither gets "fixed" early nor kept forever.

The environment variables need no shim; it was verified that they are already
ignored. With `FLEET_NODE_PLATFORM_TOKEN`, `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY`
and made-up `FLEET_*` / `FLEET_NODE_*` names set, both `ServerSettings()` and
`NodeSettings()` construct cleanly. herd loads `~/.fleet-manager/env` into
`os.environ` itself (`common/env_file.py`), not through pydantic's dotenv support,
so unknown keys never reach a strict model. For symmetry, `herd-node` logs the
same kind of one-line WARNING at startup when it sees any
`FLEET_NODE_PLATFORM_*`, `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY` or
`FLEET_NODE_TELEMETRY_INCLUDE_TAGS`, so operators know to clean their env file.

`~/.fleet-manager/platform.json` may exist on disk (written `0600` by a past
connect). **Leave it alone and don't read it**; the changelog tells operators they
may delete it.

## Inventory

### Delete (source)

| Path | Lines | Only importers today |
|------|------:|----------------------|
| `src/fleet_manager/cloud/` (`__init__.py`, `connector.py`) | 235 | `cli/server_cli.py` |
| `node/platform_connection.py` | 396 | agent, `routes/platform.py`, `platform_client.py` |
| `node/platform_client.py` | 144 | `platform_heartbeat.py` |
| `node/platform_heartbeat.py` | 274 | agent |
| `node/telemetry_scheduler.py` | 179 | agent (the **account** scheduler) |
| `node/daily_rollup.py` | 323 | `telemetry_scheduler`, `anonymous_rollup` (helpers only — see below) |
| `node/device_info.py` | 211 | `platform_connection` only |
| `node/benchmark_estimate.py` | 118 | `platform_connection` only |
| `server/routes/platform.py` | 157 | `app.py` (`include_router`) |

`device_info.py` and `benchmark_estimate.py` weren't in the original scan; they
become dead code the moment `platform_connection` goes. The community payload's
`devices[]` is built from heartbeats, not from either module.

### Edit (source)

- **`server/app.py`**: drop the `platform` import and `app.include_router(platform.router)` (~lines 331, 344).
- **`models/config.py`** (`NodeSettings`, ~lines 402–414): remove `platform_url`,
  `platform_token`, `platform_node_id`, `platform_connected_at`,
  `telemetry_local_summary`, `telemetry_include_tags`, and the `SecretStr` /
  `datetime` imports if nothing else uses them. All six fields are on `NodeSettings`;
  `ServerSettings` has none.
- **`node/agent.py`**: remove `_ensure_platform_connection`,
  `_ensure_telemetry_scheduler`, `_ensure_platform_heartbeat`, their calls in
  `start()` (~227–241), `_platform_heartbeat_task` / `_telemetry_task` and their
  cancellation in shutdown (~906). Add the env-var warning described above. Leave
  the `sys.platform == "win32"` branches untouched; they match "platform" but are
  the Windows guards.
- **`cli/server_cli.py`**: replace the `--cloud` branch (~111–131) and the
  `CLOUD_FLEET_TOKEN` read with the hidden-option warning.
- **`cli/node_cli.py`**: same for the platform and account-telemetry flags
  (~35–66, 104–105, 120–125, 138–140). Remove `platform_*` / `telemetry_*` from
  `settings_kwargs`; passing a removed field into `NodeSettings` would fail.
- **`server/routes/dashboard.py:5770`**: delete the stale `// Platform connection
  card` comment header. The card's UI was already removed in `8991298`; the
  routes outlived it.
- **`pyproject.toml`**: drop **`cryptography`** (used only by `platform_connection`
  and `platform_heartbeat`, for Ed25519) and **`websockets`** (only
  `cloud/connector.py`). Re-grep before removing, including tests. `cryptography`
  is a Rust-extension package with a steady stream of advisories that the Homebrew
  formula builds from source. Losing it shrinks the formula and the audit surface.
  (`depends_on "rust" => :build` likely stays for `pydantic-core` and `tiktoken`;
  regenerate the formula's resources and let them say.)

### Move, don't delete: the anonymous pipeline's helpers

`node/anonymous_rollup.py` imports `_categorize_error`, `_percentile` and
`_yesterday_utc_bounds` from `daily_rollup.py`. Move all three into
`anonymous_rollup.py`, the only remaining production caller, **before** deleting
`daily_rollup.py`. CLAUDE.md requires the raw-error categorisation to keep exactly
one definition, and the module table already says `anonymous_rollup` "shares
`_categorize_error`", which becomes "owns".

- **Repoint test imports:** `tests/test_server/test_error_descriptions.py` (7
  references) and `tests/test_server/test_upstream_incomplete.py` (4) import
  `_categorize_error` from `daily_rollup` for reasons unrelated to the platform.
- **Keep the helper tests.** `tests/test_node/test_daily_rollup.py` has 15 tests
  with 32 references to the three helpers. Move those tests to
  `tests/test_node/test_anonymous_rollup.py` (or a new
  `test_rollup_helpers.py`), and delete only the tests that exercise the account
  payload builder. Deleting the file wholesale would silently drop coverage of
  the error categoriser the community payload depends on.

### Delete (tests)

`tests/test_node/test_platform_client.py`, `test_platform_connection.py`,
`test_platform_heartbeat.py`, `test_telemetry_scheduler.py`, `test_device_info.py`,
`tests/test_server/test_platform_routes.py`, and the account-payload part of
`test_daily_rollup.py` (above). Then grep `tests/` for `platform_`, `cloud`,
`telemetry_local_summary`, `telemetry_include_tags` and `/api/platform` to catch
stragglers. The `sys.platform` patches in the Windows tests are not stragglers.

`tests/conftest.py` keeps clearing `CLOUD_FLEET_TOKEN`. Nothing reads it any more,
but an operator's shell may still export a real credential, and a test still has
no business seeing it. Update the comment to say that. `tests/test_hermetic_env.py`
stays as is.

### Docs

- **`docs/api-reference.md`**: delete § Platform Connection (~1870) and its three
  endpoints, plus § Daily Usage Telemetry (opt-in) (~1971), the account pipeline.
- **`docs/configuration-reference.md`**: delete § Platform Connection (~456).
  Keep § Anonymous community telemetry (~500) unchanged.
- **The three platform plans**: set **Status** to *Superseded by
  `remove-platform-integration.md` (2026-10-24): the platform was never deployed.*
  Don't delete them; they hold the reasoning.
- **CLAUDE.md**:
  - Remove the module-table rows for `platform_connection`, `platform_client`,
    `platform_heartbeat`, `telemetry_scheduler`, `daily_rollup`, `device_info` and
    `benchmark_estimate`, and change `anonymous_rollup`'s "Shares" to "Owns".
  - Rewrite the **"TWO telemetry pipelines"** gotcha. There is now one, and the
    useful history is a single sentence: an account pipeline existed, and its
    removal moved the shared helpers.
  - Keep the "Moving telemetry code across the settings boundary" and "wire
    contract" gotchas; they're about the surviving pipeline.
  - Record the hidden-flag **exception to the greenfield rule**, with its reasoning
    and the `0.14.0` removal version.
- **`CHANGELOG.md`**: a **Removed** section naming the endpoints, settings, flags
  (accepted and ignored with a warning until 0.14.0), the two dependencies, and
  `platform.json` as safe to delete. A **one-line** reassurance that anonymous
  community telemetry and its opt-out are unchanged.
- Re-grep `docs/`, `skills/`, `README.md` and `AGENTS.md` for `gotomy` and
  `platform connection` at the end. As of today only the files above mention them.

## Verification

1. **Community payload is byte-identical.** Before any change, build the anonymous
   payload from a fixed fixture with a **frozen clock and a fixed `install_id`**
   (the payload covers "yesterday", so two runs on different days differ even with
   identical code). Serialise it canonically (`json.dumps(..., sort_keys=True)`)
   and save the bytes. Rebuild after the change and compare. Commit this as a test,
   so the helper move is pinned and not just checked once.
2. **The telemetry contract tests stay green, untouched:**
   `tests/test_models/test_telemetry_env_contract.py` (the
   `FLEET_TELEMETRY` / `FLEET_NODE_TELEMETRY` opt-out) and
   `tests/test_node/test_telemetry_openapi_contract.py`.
3. **Upgrade safety, as tests:**
   - Both settings classes construct with the removed env vars set.
   - `herd` and `herd-node` parse every removed flag, start, and log the warning
     (use Typer's `CliRunner` with the server and agent start stubbed).
   - A typo like `--prot` still fails, which proves the shim is per-flag and not a
     blanket `ignore_unknown_options`.
4. **On the live fleet:** add a removed flag to a copy of the start command,
   kickstart, and confirm the process comes up with exactly the one warning.
   Confirm the next anonymous telemetry send succeeds; it's daily, so check the
   send log line or the `/dashboard` telemetry preview, which renders the same
   payload.
5. **Packaging:** a clean `pip install` of the built wheel imports and starts
   without `cryptography` or `websockets` installed. That proves nothing still
   imports them, which a dev venv can't, since it may have them anyway. Regenerate
   the Homebrew resources and confirm both drop out of the formula.
6. `uv run pytest`, `ruff`, and `./scripts/health.sh` 6/6. Refresh the test count
   in all four locations (it will **drop**, by roughly the size of the deleted
   modules; that's expected, not a regression).

## Order

One branch of work, committed in this order so each step is green:

1. Payload fixture test (verification 1), committed **before** anything moves.
2. Move the three helpers and their tests; repoint imports; delete nothing yet.
   The payload test must pass unchanged.
3. Delete the platform modules, routes, settings and wiring; add the hidden flags
   and the env-var warning, with their tests.
4. Drop the dependencies; regenerate the lock; run the clean-wheel check.
5. Docs, CLAUDE.md, changelog; mark the three plans superseded.
6. Deploy locally and run verification 4. Release as `0.12.0` on the normal
   checklist. The Homebrew formula **will** change shape this time, so step 12's
   regeneration and audit matter more than usual.

## Out of scope, noted

`node/anonymous_telemetry.py`, the node-side anonymous sender from before the
router took over sending, has no production importer, only its own test. It looks
like leftover code, but it's in the pipeline this plan promises not to touch.
Confirm it separately, in its own change, after this one ships.
