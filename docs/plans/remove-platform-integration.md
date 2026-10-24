# Remove the gotomy.ai platform integration

**Status**: ✅ **IMPLEMENTED on `main` 2026-10-24 (steps 1–6), unreleased — ships in `0.12.0`.**
Audited against the codebase the same day; see § *What the audit changed* and
§ *What implementation changed* at the end.
**Target release**: `0.12.0` — removing endpoints, settings and a dependency is a
breaking change even though nothing that works is lost.
**Supersedes**: [`platform-connection-ux.md`](platform-connection-ux.md),
[`platform-local-usage-telemetry.md`](platform-local-usage-telemetry.md),
[`platform-p2p-capability-advertisement.md`](platform-p2p-capability-advertisement.md).

**Constraints this plan is held to** (CLAUDE.md § Design Principles): greenfield,
so **no `FLEET_*` on/off flags and no second code path**; **reuse before build**,
extracting a shared helper rather than copying one. Every step below names the
existing code it reuses.

---

## Why

Every platform feature in herd talks to a coordination service that was never
deployed. `https://gotomy.ai` answers `308 → https://ollamaherd.com/` (checked
2026-10-24), and there is no service behind it. So `herd --cloud`, the node's
platform connection, the signed platform heartbeat and the **account** usage
telemetry cannot work for any user. Keeping them means maintaining roughly 2,000
lines, six test modules and a Rust-extension dependency for features that do
nothing.

**Not affected:** the **anonymous community telemetry** that the router sends to
`ollamaherd.com/api/v1/telemetry`, which *is* deployed. Its payload stays byte for
byte, pinned by a test written before anything moves. `ollamaherd.com/telemetry`
is the contract.

## The one exception: removed flags stay accepted, and warn

A removed Typer option makes the process exit with `No such option`. Under the
launchd and systemd keep-alive agents that becomes a crash loop: an upgrade that
looks like an outage. None of these flags is documented in the README, the skills,
the guides or the example plists (only `--telemetry-local-summary` appears, once,
in `docs/api-reference.md`), and the platform never worked, so very few people pass
them. But "very few" isn't zero, and tolerance costs a few lines.

| Command | Flags kept as hidden no-ops |
|---------|-----------------------------|
| `herd` | `--cloud`, `--token`, `--cloud-url` |
| `herd-node` | `--platform-token` (and its alias `--operator-token`), `--platform-url`, `--telemetry-local-summary`, `--telemetry-include-tags` |

**Implementation, kept to one place:**
- **One small module, `cli/removed_flags.py`,** holds the removed-flag names, a
  single warning function, and the `0.14.0` removal version. Both CLIs declare the
  options with `hidden=True` and call that function if any was passed. Removing
  the exception later means deleting one module and the option lines, with nothing
  left to hunt down.
- **Typer has no built-in mechanism for this.** Typer `0.27.3` doesn't expose
  Click's `deprecated` option parameter (checked), so a hand-written warning is
  necessary. Typer's `ignore_unknown_options` is rejected: it silently swallows
  *every* unknown flag, including typos like `--prot 11435`.
- **Track the removal in `docs/issues.md`** as an OPEN entry ("remove the hidden
  platform flags in 0.14.0"), the same mechanism every other scheduled task uses.
  Also add one line to CLAUDE.md's greenfield principle naming this as its
  recorded exception.

**No environment-variable warning.** The earlier draft added a startup warning
for leftover `FLEET_NODE_PLATFORM_*` variables. The audit drops it. Those variables
are already ignored (verified: both `ServerSettings()` and `NodeSettings()`
construct cleanly with them set, because `common/env_file.py` loads the env file
into `os.environ` rather than through pydantic's dotenv support, so unknown keys
never meet a strict model). A leftover variable is inert. Warning about it would
be a second shim protecting against nothing, and more debt to remove later. The
changelog tells operators they can delete them.

`~/.fleet-manager/platform.json` may exist on disk. Leave it alone and don't read
it; the changelog says it's safe to delete.

## Inventory

### Delete (source)

| Path | Lines | Only importers today |
|------|------:|----------------------|
| `src/fleet_manager/cloud/` (`__init__.py`, `connector.py`) | 235 | `cli/server_cli.py` |
| `node/platform_connection.py` | 396 | agent, `routes/platform.py`, `platform_client.py` |
| `node/platform_client.py` | 144 | `platform_heartbeat.py` |
| `node/platform_heartbeat.py` | 274 | agent |
| `node/telemetry_scheduler.py` | 179 | agent (the **account** scheduler) |
| `node/daily_rollup.py` | 323 | `telemetry_scheduler`, plus three helpers borrowed by `anonymous_rollup` (see below) |
| `node/device_info.py` | 211 | `platform_connection` only |
| `node/benchmark_estimate.py` | 118 | `platform_connection` only |
| `server/routes/platform.py` | 157 | `app.py` (`include_router`) |
| `node/anonymous_telemetry.py` | — | **none in production**, only its own test |

`node/anonymous_telemetry.py` was "out of scope" in the first draft, to keep this
change away from the anonymous pipeline. The audit brings it in. It is the node-side
sender from before the router took over sending: nothing imports it, and it carries
private copies of `community_telemetry`'s scheduling helpers (`_load_last_sent_day`,
`_seconds_until_next_run`, an inline "yesterday"). It also calls
`build_anonymous_rollup`, whose signature changes below. The worry that justified
leaving it, touching the live pipeline, is answered by the byte-identical payload
test. Delete it, and `tests/test_node/test_anonymous_telemetry.py`, in their own
commit.

### Edit (source)

- **`server/app.py`**: drop the `platform` import and `include_router` (~lines 331, 344).
- **`models/config.py`** (`NodeSettings`, ~402–414): remove `platform_url`,
  `platform_token`, `platform_node_id`, `platform_connected_at`,
  `telemetry_local_summary`, `telemetry_include_tags`. The `SecretStr` and
  `datetime` imports are used only by these fields, so they go too (ruff's `F401`
  will confirm). All six are on `NodeSettings`; `ServerSettings` has none.
- **`node/agent.py`**: remove `_ensure_platform_connection`,
  `_ensure_telemetry_scheduler`, `_ensure_platform_heartbeat`, their calls in
  `start()` (~227–241), `_telemetry_task` / `_platform_heartbeat_task` and their
  cancellation (~906). Leave the `sys.platform == "win32"` branches alone; they
  match "platform" but are the Windows guards.
- **`cli/server_cli.py`**: the `--cloud` branch (~111–131) and the
  `CLOUD_FLEET_TOKEN` read are replaced by the hidden options and one call to
  `cli/removed_flags`.
- **`cli/node_cli.py`**: same for the platform and account-telemetry flags
  (~35–66, 104–105, 120–125, 138–140). Remove them from `settings_kwargs` too;
  passing a removed field into `NodeSettings` would fail.
- **`server/routes/dashboard.py:5770`**: delete the stale `// Platform connection
  card` comment. The card itself left in `8991298`; the routes outlived it.
- **`pyproject.toml`**: drop **`cryptography`** (imported only by
  `platform_connection` and `platform_heartbeat`) and **`websockets`** (only
  `cloud/connector.py`). Re-grep `src` and `tests` first.
  - **What the install loses:** `cryptography` and the `cffi` and `pycparser`
    packages under it. That's three Homebrew resources, and a Rust-extension
    package with a steady stream of advisories.
  - **What stays: `websockets`.** It still arrives through `uvicorn[standard]`
    (`uv tree --invert --package websockets`). It leaves the direct dependency
    list because herd no longer imports it, but it remains in the install and the
    formula.
  - **`depends_on "rust" => :build` stays,** for `pydantic-core` and `tiktoken`.

### The three helpers the anonymous pipeline borrows: each has a better home

`anonymous_rollup.py` imports `_categorize_error`, `_percentile` and
`_yesterday_utc_bounds` from `daily_rollup.py`. The first draft moved all three
into `anonymous_rollup.py`. The audit found that none of them should go there:

- **`_categorize_error` → `common/errors.py`, as public `categorize_error`.** That
  module's own docstring already describes the pair: exceptions reach "the
  anonymous community telemetry error histogram" via `_categorize_error`, and
  `describe_exception` exists so its output stays categorisable. Today the two
  halves of that contract live in different layers. Putting them together makes
  the "one definition of raw error text" rule structural. Add it to `__all__`, and
  repoint `anonymous_rollup.py`, `tests/test_server/test_error_descriptions.py`
  (7 references) and `tests/test_server/test_upstream_incomplete.py` (4).
- **`_percentile` → one shared `percentile`, replacing two copies.**
  `server/benchmark_engine.py:499` has its own linear-interpolation `percentile`.
  The two differ only in the empty case (`0.0` against `None`) and in the order of
  the floating-point operations. Extract one into `common/` and point both callers
  at it.
  - **The anonymous version's arithmetic is canonical,** since it feeds the
    published payload. The byte-identical test enforces that; benchmark output is
    rounded to 0.1, so last-bit differences cannot show there.
  - **Keep `None` for empty input.** At benchmark's one unguarded call site,
    `latency_p*`, add an explicit empty guard rather than silently returning `0.0`.
- **`_yesterday_utc_bounds` → deleted.** It is dead in production. The only live
  sender, `community_telemetry.send_once`, computes "yesterday" itself and passes
  `day=` all the way down. `build_anonymous_rollup`'s `day=None` branch, the
  helper's only caller, is reached only from tests.
  - **Make `day` required** in `build_anonymous_rollup` and `build_herd_rollup`,
    and delete the `None` branches and the helper.
  - That leaves exactly one "yesterday" computation, in `send_once`, where today
    there are four: the helper, two inline copies in `community_telemetry`
    (~173, ~200) and one in the dead `anonymous_telemetry.py`.
  - The tests that relied on the default pass an explicit `day`.

**Keep the helper tests; move them with the code.** `tests/test_node/test_daily_rollup.py`
has 15 tests with 32 references to the three helpers. The `_categorize_error`
tests move beside `test_error_descriptions.py`, and the `_percentile` tests go
with the new shared helper. Delete only the tests of the account payload builder
and of `_yesterday_utc_bounds`. Deleting the file wholesale would silently drop
coverage of the error categoriser the community payload depends on.

### Delete (tests)

- **Whole files:** `tests/test_node/test_platform_client.py`,
  `test_platform_connection.py`, `test_platform_heartbeat.py`,
  `test_telemetry_scheduler.py`, `test_device_info.py`, `test_anonymous_telemetry.py`
  (with its module), and `tests/test_server/test_platform_routes.py`.
- **Partly:** the account-payload part of `test_daily_rollup.py` (above).
- **Then grep for stragglers:** search `tests/` for `platform_`, `cloud`,
  `telemetry_local_summary`, `telemetry_include_tags` and `/api/platform`. The
  `sys.platform` patches in the Windows tests aren't stragglers.

**`CLOUD_FLEET_TOKEN` comes out of `tests/conftest.py` and
`tests/test_hermetic_env.py`.** It was cleared because `herd --cloud` read it as a
real credential. After this change nothing reads it, so no test can leak it, and
clearing it would be a defence of nothing. The `FLEET_*` prefix rule that does the
real work is untouched.

### Docs

- **`docs/api-reference.md`**: delete § Platform Connection (~1870) with its three
  endpoints, and § Daily Usage Telemetry (opt-in) (~1971).
- **`docs/configuration-reference.md`**: delete § Platform Connection (~456). Leave
  § Anonymous community telemetry (~500) unchanged.
- **The three platform plans**: set **Status** to *Superseded by
  `remove-platform-integration.md` (2026-10-24): the platform was never deployed.*
  Don't delete them; they hold the reasoning.
- **CLAUDE.md**:
  - Drop the module-table rows for `platform_connection`, `platform_client`,
    `platform_heartbeat`, `telemetry_scheduler`, `daily_rollup`, `device_info` and
    `benchmark_estimate`.
  - Change the `anonymous_rollup` row, which says it "shares `_categorize_error`
    with `daily_rollup`", and add `categorize_error` to the `common/errors.py` row.
  - Rewrite the **"TWO telemetry pipelines"** gotcha to describe one pipeline, with
    a sentence of history.
  - Keep the settings-boundary and wire-contract gotchas; they're about the
    surviving pipeline.
  - Name the hidden flags as the greenfield rule's one recorded exception.
- **`CHANGELOG.md`**: a **Removed** section listing:
  - the endpoints, settings and flags, noting the flags are accepted and ignored
    with a warning until 0.14.0;
  - `cryptography` and `websockets` as direct dependencies;
  - leftover `FLEET_NODE_PLATFORM_*` variables and `platform.json` as safe to delete.

  Add one line saying anonymous community telemetry and its opt-out are unchanged.
- At the end, grep `docs/`, `skills/`, `README.md` and `AGENTS.md` for `gotomy` and
  `platform connection`. As of today only the files above mention them.

## Verification

1. **The community payload is byte-identical.** Before anything moves, extend
   `tests/test_node/test_anonymous_rollup.py`, which already builds payloads from
   `tmp_path` stores:
   - Seed traces covering several models, errors of every category and enough
     latencies for each percentile.
   - Call **`community_telemetry.build_herd_rollup`** with an **explicit `day=`**
     and a fixed `install_id`. That's the full payload the router sends, devices
     included, and passing `day` makes it deterministic with no clock to freeze.
   - Assert the canonical JSON (`sort_keys=True`) equals a committed string.

   Every later step must keep it green unchanged.
2. **The contract tests stay green, untouched:**
   `tests/test_models/test_telemetry_env_contract.py` (the
   `FLEET_TELEMETRY` / `FLEET_NODE_TELEMETRY` opt-out) and
   `tests/test_node/test_telemetry_openapi_contract.py`.
3. **Upgrade safety, as tests.** Follow the existing CLI test pattern in
   `tests/test_models/test_cli_version.py` (`typer.testing.CliRunner`, parametrized
   over `node_cli` and `server_cli`), with the server and agent start stubbed:
   - Every removed flag parses, the process starts, and the one warning is logged.
   - A typo like `--prot` still fails, proving the tolerance is per flag.
   - Both settings classes construct with the removed environment variables set.
4. **On the live fleet:** put a removed flag in a copy of the start command,
   kickstart, and confirm the process comes up with exactly the one warning. After
   the next daily send, confirm the router log has `community telemetry sent for
   <day>`. It's logged at DEBUG from `fleet_manager.server.community_telemetry`; it
   was last seen at `2026-10-24T00:07:52Z` for `2026-10-23`. The dashboard has no
   telemetry preview to check instead; the first draft wrongly said it did.
5. **Packaging:** a clean `pip install` of the built wheel into a fresh Python 3.12
   venv imports and starts, and `cryptography` is absent from it. A dev venv can't
   prove that, since it may hold the package anyway. Regenerate the Homebrew
   resources: `cryptography`, `cffi` and `pycparser` drop out, `websockets` stays.
6. `uv run pytest`, `ruff`, and `./scripts/health.sh` 6/6. Refresh the test count
   in all four locations. It will **drop**, by roughly the deleted modules' tests;
   that's expected, not a regression.

## Order

Each commit leaves the suite green:

1. The byte-identical payload test (verification 1), **before anything moves**.
2. `categorize_error` → `common/errors.py`; one shared `percentile`; `day` made
   required, with `_yesterday_utc_bounds` and the inline "yesterday" copies removed;
   helper tests moved. The payload test must pass unchanged.
3. Delete `node/anonymous_telemetry.py` and its test.
4. Delete the platform modules, routes, settings and wiring; add `cli/removed_flags.py`,
   the hidden options and their tests.
5. Drop the two direct dependencies, regenerate the lock, and run the clean-wheel
   check.
6. Docs, CLAUDE.md, the `docs/issues.md` entry for 0.14.0, the changelog; mark the
   three plans superseded.
7. Deploy locally, run verification 4, and release `0.12.0` on the normal
   checklist. The formula changes shape this time, so step 12's regeneration and
   audit matter more than usual.

## What the audit changed

| First draft | After the audit | Why |
|-------------|-----------------|-----|
| Move the three helpers into `anonymous_rollup.py` | `categorize_error` → `common/errors.py`; `percentile` shared with `benchmark_engine`; `_yesterday_utc_bounds` deleted | Existing homes and a duplicate, and one dead helper |
| `anonymous_telemetry.py` out of scope | Deleted, in its own commit | No production importer; carries copies of `community_telemetry`'s helpers; its "risk" is covered by the payload test |
| Startup warning for leftover env vars | Dropped | The variables are already inert; the warning would be a second shim guarding nothing |
| Hidden flags handled in each CLI | One `cli/removed_flags.py`; removal tracked in `docs/issues.md` | One place to delete in 0.14.0; uses the existing tracking mechanism |
| Payload test with a frozen clock and `install_id` | `build_herd_rollup(day=...)` with a fixed `install_id` | Existing parameters make it deterministic; covers the whole sent payload |
| Keep clearing `CLOUD_FLEET_TOKEN` in tests | Remove it | Nothing reads it after this change |
| Verify the send via a dashboard telemetry preview | Verify via the DEBUG send log line | No such preview exists |
| Homebrew loses `cryptography` and `websockets` | Loses `cryptography`, `cffi`, `pycparser`; `websockets` stays via `uvicorn[standard]` | Measured with `uv tree --invert` |
| New CLI test setup | Follow `tests/test_models/test_cli_version.py` | Existing `CliRunner` pattern |

## What implementation changed

| Plan said | Done instead | Why |
|-----------|--------------|-----|
| New module `cli/removed_flags.py` | `removed_option` in `cli/__init__.py`, beside `version_callback` | That module already *is* "shared helpers for the `herd` and `herd-node` entry points". Same single place to delete in 0.14.0. |
| Each CLI checks the removed flags and warns | One Typer option `callback=` with `expose_value=False` | Mirrors `version_callback`'s eager-callback pattern. The values never reach `start()`, so neither CLI has any removal logic. |
| Hidden options keep their `envvar=` | `envvar=` dropped | With it, a leftover `FLEET_NODE_PLATFORM_TOKEN` would set off the warning. Dropping it is what makes leftover variables inert. |
| Guard benchmark's one unguarded `percentile` call | No change needed | All six benchmark call sites already guarded with `if latencies else None`. |
| `day` required | `day` required **and keyword-only** | It follows a defaulted parameter; every caller already passed it by name. |
| — | `scripts/health.sh` test floor lowered 1945 → 1923, with the reason in a comment | The floor is a ratchet against tests silently disappearing; this deletion was deliberate. |

**Measured:** the golden payload test passed unchanged at every step (it was
mutation-checked first: changing the percentile arithmetic or renaming one error
category both fail it). The flag tests were mutation-checked too: echoing the token
value fails 3, and unhiding and un-wiring one flag fails 2. A clean `pip install`
of the wheel into a fresh Python 3.12 venv contains neither `cryptography` nor
`cffi`, and `create_app`, the node agent and community telemetry import there.
Test count 2024 → 1931, passing both with the operator's 19 `FLEET_*` variables
present and with them stripped.

**On the live fleet (2026-10-24):**
- An isolated `herd --cloud --token … --cloud-url …` (private data dir, private mDNS
  type, empty env file) started, printed three warnings, served `/fleet/status`, and
  the token appeared nowhere in its output or logs.
- The live router and node restarted onto this code with zero errors and warnings.
  `/api/platform/status` now returns 404, and tool calls through gpt-oss and the
  MLX coder, `/v1/messages`, `/v1/responses` and `/api/embed` all work.

**Homebrew:** resolving the sdist's closure with brew's one-day cooldown gives 42
packages, down from 45. `cffi`, `cryptography` and `pycparser` drop out, nothing is
added, and `websockets` stays.

