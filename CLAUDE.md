# CLAUDE.md

## Build & Run

```bash
uv sync                          # install core deps
uv sync --extra embedding        # + vision embeddings (DINOv2 / SigLIP / CLIP) — needs onnxruntime
uv run herd                      # start router on :11435
uv run herd-node                 # start node agent (auto-discovers router via mDNS)
uv run herd-node --router-url http://localhost:11435  # explicit router URL
```

Without `--extra embedding`, the vision embedding server starts but `onnxruntime` isn't importable, so the collector probes for it on every heartbeat and refuses to advertise the models — DINOv2/SigLIP/CLIP chips disappear from the node card entirely. A `vision_backend_missing` health check fires WARNING with the exact `uv sync --extra embedding` fix command. (The previous behavior was to advertise the chips and 500 every `/embed` call, which produced silent failures in agentic dedup loops — see commit `9ff8a54` and the 2026-04-25 observation in `docs/observations.md`.)

## Test

```bash
uv sync --extra dev              # install test deps (first time only)
uv run pytest                    # run all 969 tests (~40s)
uv run pytest tests/test_server/ # run server tests only
uv run pytest tests/test_models/ # run model tests only
uv run ruff check src/           # lint
uv run ruff format src/          # format
```

## Release to PyPI

**IMPORTANT: Never publish without running locally first.** AI agents: do NOT publish unless the user explicitly says "publish."

### Release checklist

**Pre-publish (locally):**
1. Bump version in `pyproject.toml`
2. Update `CHANGELOG.md` (Keep a Changelog format) — rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, leave a fresh empty `[Unreleased]` stub above it
3. `uv run pytest` — 0 failures
4. `uv run ruff check src/` — clean
5. Commit + push the version bump
6. Deploy locally — restart `herd` + `herd-node`, verify: `/fleet/status`, `/api/embed`, `/dashboard/api/health`, `/fleet/queue`
7. Soak several hours on the local fleet — `grep '"level":"ERROR"' ~/.fleet-manager/logs/herd.jsonl` should stay clean

**Publish:**

8. `rm -rf dist/ && uv build` — produces wheel + sdist
9. Capture the sdist sha256 (needed for the Homebrew bump): `shasum -a 256 dist/ollama_herd-X.Y.Z.tar.gz`
10. `uv publish --username __token__ --password "$(python3 -c "import configparser; c=configparser.ConfigParser(); c.read('$HOME/.pypirc'); print(c['pypi']['password'])")"`
11. Wait for PyPI cache to update (~1 min) — verify: `curl -s https://pypi.org/pypi/ollama-herd/json | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])"` returns the new version

**Bump Homebrew tap (separate repo):**

12. Edit `geeks-accelerator/homebrew-ollama-herd/Formula/ollama-herd.rb`:
    - Update main `url` + `sha256` (use the values from step 9, plus the new sdist URL from `https://pypi.org/pypi/ollama-herd/X.Y.Z/json`)
    - For each new dep added in this release: add a `resource "<name>" do ... end` block (alphabetically). Get URL/sha from `https://pypi.org/pypi/<dep>/json`
    - For any dep that bumped in this release: update its existing resource block
    - **If the new release adds a Rust-extension dep** (cryptography, pydantic-core, tiktoken, etc.): ensure `depends_on "rust" => :build` is present
13. **End-to-end install test (this step is non-negotiable — see "Brew tap testing" gotcha below):**
    ```bash
    brew uninstall ollama-herd  # if previously installed
    brew untap geeks-accelerator/ollama-herd  # forces a fresh tap clone
    brew tap geeks-accelerator/ollama-herd
    brew install ollama-herd  # ~5 min for a Python-virtualenv formula
    /opt/homebrew/Cellar/ollama-herd/X.Y.Z/libexec/bin/python -c "import fleet_manager; from fleet_manager.server.app import create_app; print('ok')"
    /opt/homebrew/bin/herd --help
    ```
    All must succeed. If pip fails on a Rust-extension dep with "can't find Rust compiler" → add `depends_on "rust" => :build`. If pydantic complains about pydantic-core version → bump both together.
14. Commit + push the formula
15. One more `brew uninstall && brew untap && brew tap && brew install ollama-herd` against the pushed-to-GitHub formula to confirm a real fresh-user install works

**Post-publish soak verification:**

16. **Day-after check** (24h after `uv publish`):
    ```bash
    # PyPI download + version sanity
    curl -s https://pypistats.org/api/packages/ollama-herd/recent | python3 -m json.tool
    curl -s https://pypi.org/pypi/ollama-herd/json | python3 -c "import json,sys; print('latest:', json.load(sys.stdin)['info']['version'])"
    # No new GitHub issues from real users?
    gh issue list --repo geeks-accelerator/ollama-herd --state open --limit 20
    # Local fleet still healthy?
    grep '"level":"ERROR"' ~/.fleet-manager/logs/herd.jsonl | tail
    sqlite3 ~/.fleet-manager/latency.db \
      "SELECT status, COUNT(*) FROM request_traces WHERE timestamp > (strftime('%s','now') - 86400) GROUP BY status"
    ```
    Healthy: downloads >0, no new issues, ERROR count flat, success rate >99%.

17. **Week-after check** (7 days after `uv publish`):
    ```bash
    curl -s https://pypistats.org/api/packages/ollama-herd/recent | python3 -m json.tool
    gh issue list --repo geeks-accelerator/ollama-herd --state open --limit 20
    gh issue list --repo geeks-accelerator/homebrew-ollama-herd --state open --limit 10
    ```
    Bad signals (act on these): sudden download dropoff to ~0 (might mean PyPI yanked the release or the page is broken); spike of new issues mentioning the version; tap repo issue about install failure.

**There is no "uninstalls" metric** — neither PyPI nor Homebrew tracks them. The closest signals are (1) a download trend that drops faster than usual after the spike, and (2) GitHub issue volume. Treat both as soft signals, not alarms.

**Package:** `ollama-herd` on [PyPI](https://pypi.org/project/ollama-herd/) | **Build:** hatchling | **Version:** `pyproject.toml`
**Homebrew tap:** `geeks-accelerator/homebrew-ollama-herd` (separate repo — formula bump is its own commit, no PyPI republish needed for tap-only fixes)

### Local deployment

```bash
# Kill EVERYTHING herd-related, including any mlx_lm.server children that
# would otherwise survive the parent's death and orphan onto launchd.
pkill -9 -f "bin/herd|mlx_lm.server" && sleep 3
uv sync --all-extras && uv run herd &>/dev/null & disown
sleep 3 && uv run herd-node &>/dev/null & disown
```

**`pkill -9 -f "bin/herd|mlx_lm.server"` matters as much as `--all-extras`.** `MlxSupervisor` spawns mlx_lm.server with `start_new_session=True` so the children survive a parent crash. If you only `pkill bin/herd`, the mlx_lm.server processes get reparented to launchd and keep holding ports 11440 + 11441. The next supervisor startup tries to bind, fails, and logs "QUARANTINED" forever against an orphan that's actually fine (see `docs/observations.md` 2026-04-27). The supervisor now detects + SIGKILLs orphans automatically at start time, but the cleaner restart recipe avoids the warning entirely. (`-9` because some MLX shutdown paths hang on SIGTERM — see commit `9ff8a54`.)

**`--all-extras` is non-negotiable here.** Plain `uv sync` is destructive — it removes any package not in core deps + currently-requested extras. The `embedding` extras (`onnxruntime`, `Pillow`, `numpy`, `huggingface-hub`) are optional in `pyproject.toml`, so a bare `uv sync` strips them every restart, and the next vision-embedding request 500s. A health check (`vision_backend_missing`) now catches this regression server-side, but `--all-extras` in the deploy snippet is the actual fix — it makes the local fleet keep every optional capability resident across restarts. Total cost: ~250 MB of additional packages in `.venv/`. See `docs/observations.md` (entry: 2026-04-25 — "uv sync without --extra embedding strips vision embedding deps").

Both entry points auto-load `~/.fleet-manager/env` at startup (see `src/fleet_manager/common/env_file.py`), so `FLEET_*` vars work even when launched from non-interactive shells (Bash subshells, nohup, launchd). Shell env still wins if set. Template: `docs/examples/fleet-env.example` — copy to `~/.fleet-manager/env` on a fresh machine.

### Gotchas

- **`launchctl setenv` is overridden by `~/.zshrc`** — update both shell profile AND launchctl for macOS env vars. On Linux: `sudo systemctl edit ollama`. On Windows: `[System.Environment]::SetEnvironmentVariable()`
- **`shutil.which()` can't find `uv tool` binaries** — `_which_extended()` in `collector.py` handles platform-aware fallback paths
- **Thinking models eat `num_predict` budgets** — router auto-inflates by 4× for known thinking models. Add new ones to `is_thinking_model()` in `model_knowledge.py`
- **Default context windows waste KV cache** — gpt-oss:120b allocates 131K ctx but p99 usage is ~5K tokens. Enable `FLEET_DYNAMIC_NUM_CTX=true` to auto-optimize. See `docs/plans/dynamic-num-ctx.md`
- **Ollama `OLLAMA_MAX_LOADED_MODELS=-1` is silently invalid** — parsed as unsigned int, `-1` fails, falls through to default `0` = 3-model cap. Use a positive integer (but see next point — may be ignored anyway on macOS 2026). `OLLAMA_KEEP_ALIVE=-1` IS valid (means "keep forever"). Ollama env var semantics differ per variable; don't assume `-1` means unlimited.
- **Ollama 0.20.4 macOS has a hardcoded 3-model hot cap** — no env configuration we've found will raise it. When a mapped model gets evicted, silent VRAM fallback fires and Claude Code tool use breaks. Surfaced via `x-fleet-fallback` response header and fallback_rate in trace DB. See `docs/issues.md` and `docs/plans/hot-fleet-health-checks.md`.
- **Use `mlx:` prefix in `FLEET_ANTHROPIC_MODEL_MAP` to bypass the 3-model cap** — any mapped value starting with `mlx:` routes through an independent `mlx_lm.server` subprocess instead of Ollama. Setup: run `./scripts/setup-mlx.sh` (installs pinned mlx-lm 0.31.3 + applies the `--kv-bits` patch the supervisor requires), then set `FLEET_MLX_ENABLED=true` on the router and `FLEET_NODE_MLX_ENABLED=true` + `FLEET_NODE_MLX_AUTO_START=true` on the node. **Re-run the script after any `uv tool upgrade mlx-lm`** — upgrades wipe the patch and supervisor auto-start fails with `unrecognized arguments: --kv-bits 8`. See `docs/guides/mlx-setup.md`.
- **Run multiple MLX models concurrently via `FLEET_NODE_MLX_SERVERS`** — JSON list of `{model, port, kv_bits}` entries, one `mlx_lm.server` subprocess per entry. Memory-pressure gate (`FLEET_NODE_MLX_MEMORY_HEADROOM_GB`) refuses to start a server that wouldn't fit; surfaces skip reason in heartbeat as `memory_blocked` status. Set `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` for multi-node LAN aggregation. Dashboard renders per-server health table inside each node card. Canonical use case: dedicate a smaller MLX model (e.g. `Qwen3-Coder-30B-A3B-Instruct-4bit`) to context compaction via `FLEET_CONTEXT_COMPACTION_MODEL=mlx:...` so summarization has its own prompt cache and doesn't compete for the main model's slot. See `docs/guides/mlx-setup.md` § "Multi-server setup".
- **Brew tap testing — a tap that's only ever been bumped (version + sha256) has been *described*, not *tested*.** Homebrew runs `pip install --no-binary :all:` which forces source builds for every resource. Any Rust-extension Python dep (`pydantic-core`, `cryptography`, `tiktoken`) needs `depends_on "rust" => :build` in the formula or the install fails at "can't find Rust compiler" while bootstrapping `maturin`. Any `pyproject.toml` dep that isn't listed as a `resource` block also breaks the install (Homebrew's `virtualenv_install_with_resources` doesn't transparently pull from PyPI for missing deps). The 0.5.x formula was broken throughout for both reasons; nobody noticed because nobody actually ran `brew install`. **Step 13 of the release checklist is non-negotiable** — uninstall + untap + retap + install + import sanity check, every time, before considering a release done. See the 0.6.0-formula-fix observation in `docs/observations.md`.

## Architecture

Single Python package (`fleet_manager`), cross-platform (macOS, Linux, Windows), two entry points:
- `herd` — FastAPI router (scoring + queues + dashboard + health + benchmarks)
- `herd-node` — node agent (heartbeats + metrics + capacity learning + Ollama management)

macOS-only features (gracefully disabled elsewhere): meeting detection, mflux/DiffusionKit image gen, MLX speech-to-text. Core routing works identically on all platforms.

### Key modules

| Module | Purpose |
|--------|---------|
| `server/scorer.py` | 7-signal scoring: thermal, memory, queue, wait, affinity, availability, context fit — Signals 3/4/5 are bandwidth-aware when `memory_bandwidth_gbps` is populated |
| `server/hardware_lookup.py` | Chip → memory bandwidth table (Apple Silicon + discrete GPUs) powering device-aware scoring |
| `server/queue_manager.py` | Per `node:model` queues with dynamic concurrency + zombie reaper |
| `server/streaming.py` | httpx proxy to Ollama + NDJSON↔SSE + auto-retry + context protection + thinking model inflate |
| `server/health_engine.py` | 18 health checks (offline, degraded, memory, KV bloat, context waste, thrashing, timeouts, errors, retries, disconnects, streams, version, protection, zombies, connection failures, priority models) |
| `server/context_optimizer.py` | Dynamic num_ctx: analyzes token usage, auto-calculates optimal context, queues Ollama restarts via heartbeat commands |
| `server/benchmark_engine.py` | Benchmark core: fleet discovery, multimodal request gen (LLM + embed + image), report building |
| `server/benchmark_runner.py` | Server-side runner: smart mode (fill memory from disk/catalog), progress tracking, model type selection |
| `server/model_knowledge.py` | 40+ model catalog with benchmarks, RAM, categories (including VISION), thinking detection |
| `node/agent.py` | Main loop: mDNS discovery, heartbeat, Ollama auto-start/restart, LAN proxy, drain |
| `node/capacity_learner.py` | 168-slot weekly behavioral model, availability score, dynamic memory ceiling |
| `node/embedding_models.py` | Vision embedding model registry, download, ONNX inference (DINOv2, SigLIP, CLIP) |
| `node/embedding_server.py` | FastAPI server for vision embeddings on :11438 |
| `node/platform_connection.py` | Opt-in gotomy.ai integration: Ed25519 keypair, token, register, persist |
| `node/platform_client.py` | Shared httpx wrapper with retry — used by heartbeat + telemetry |
| `node/platform_heartbeat.py` | Signed heartbeat POST every 60s (CPU, memory, VRAM, queues, loaded models) |
| `node/mlx_client.py` | Node-side client for polling `mlx_lm.server` `/v1/models`; results merged into heartbeat with `mlx:` prefix |
| `node/mlx_supervisor.py` | Subprocess lifecycle for N `mlx_lm.server` processes — spawn, health-check, auto-restart on crash, memory-pressure gate, orphan reap on startup (kills any pre-existing `mlx_lm.server` bound to our port — see 2026-04-27 observation), crash-rate quarantine (5+ crashes in 5 min → 10-min restart cadence — see 2026-04-26 observation). One child per `FLEET_NODE_MLX_SERVERS` entry via `MlxSupervisorSet`. |
| `server/mlx_proxy.py` | Server-side proxy forwarding `mlx:` prefixed models to the right mlx_lm.server (OpenAI → Anthropic SSE translation, per-URL client pool, registry-driven URL resolution) |
| `node/telemetry_scheduler.py` | Daily usage rollup POST at 00:05 UTC + jitter (opt-in via env) |
| `node/daily_rollup.py` | Builds telemetry payload with structural privacy whitelist |
| `node/device_info.py` | Per-platform hardware probe (macOS/Linux/Windows) for registration |
| `node/benchmark_estimate.py` | Tokens/sec from trace data or hardware heuristic |
| `server/model_preloader.py` | Priority model loading after restart — weighted 24h/7d usage scoring |

Routes: `server/routes/` — `openai_compat.py` (v1/), `ollama_compat.py` (api/), `fleet.py`, `heartbeat.py`, `dashboard.py`, `image_compat.py`, `transcription_compat.py`, `embedding_compat.py`, `platform.py` (Connect/Disconnect)

### Request flow

Client → route handler → `score_with_fallbacks()` (eliminate → score 7 signals → select) → `QueueManager.enqueue()` → `StreamingProxy` (context protection + httpx stream) → response + trace to SQLite

### Configuration

All via env vars: `FLEET_` prefix (server), `FLEET_NODE_` prefix (node). See `docs/configuration-reference.md` for 47+ variables.

## Documentation

Key docs (Claude reads on demand — NOT loaded every turn):
- `docs/api-reference.md` — all endpoints with request/response schemas
- `docs/configuration-reference.md` — all 47+ env vars with tuning guidance
- `docs/operations-guide.md` — logging, traces, fallbacks, retry, drain, streaming, context protection
- `docs/fleet-manager-routing-engine.md` — 5-stage scoring pipeline deep dive
- `docs/adaptive-capacity.md` — capacity learner, meeting detection, app fingerprinting
- `docs/troubleshooting.md` — common issues, LAN debugging, operational gotchas
- `docs/openclaw-integration.md` — OpenClaw agent setup guide
- `docs/guides/claude-code-integration.md` — point Claude Code CLI at the herd via `ANTHROPIC_BASE_URL` (native `/v1/messages` endpoint, full tool use)
- `docs/issues.md` — known issues (mark `FIXED` when resolved, never delete)
- `docs/observations.md` — operational insights (append new learnings, never delete)
- `docs/plans/` — implementation plans for major features
- `docs/guides/` — image gen, thinking models, request tagging, agent setup, optimizing CLAUDE.md
- `docs/research/` — local fleet economics, mflux architecture
- `skills/` — 37 ClawHub skills. Strategy: `docs/skill-publishing-strategy.md`

## Collaboration Standards (Fail-Fast on Truth)

**You are a collaborator, not just an executor.** Users benefit from your judgment, not just your compliance.

**Push back when needed**:
- If the user's request is based on a misconception, say so
- If you spot a bug adjacent to what they asked about, mention it
- If an approach seems wrong (not just the implementation), flag it

**Report outcomes faithfully**:
- If tests fail, say so with the relevant output
- If you did not run a verification step, say that rather than implying it succeeded
- Never claim "all tests pass" when output shows failures
- Never suppress or simplify failing checks to manufacture a green result
- Never characterize incomplete or broken work as done

**Don't assume tests or types are correct**:
- Passing tests prove the code matches the test, not that either is correct
- TypeScript compiling doesn't mean types are correct — `any` hides errors
- If you didn't run `npm test` and `npx tsc --noEmit` yourself, don't claim they pass

**When work IS complete**: State it plainly. Don't hedge confirmed results.

**Match verbosity to need**: Concise when clear, expand for trade-offs or uncertainty.

**Never suggest stopping, wrapping up, or continuing later.** The users on this project work across multiple Claude sessions in parallel — they are not casual users looking for a natural conversation ending. Don't summarize sessions, don't ask "should we wrap up?", don't say "what a session!", don't say "good night", don't assume time of day. When one task finishes, move to the next or wait for direction. No meta-commentary about session length, time of day, or how much was accomplished. A completed task is not a potential ending — it's just the thing before the next thing.

Silent failures are dishonest. Fail fast, fail loud.

## Design Principles

- **Node sovereignty** — each node works standalone; router coordinates, never controls
- **Two-person scale** — two commands, zero config files, zero Docker. Choose simple (HTTP, SQLite, mDNS) over "proper" (gRPC, etcd, K8s)
- **Human-readable state** — JSONL logs, SQLite traces, JSON config. `grep` and `sqlite3` are your debuggers
- **Inference request is primary** — every component serves one goal: best response, fastest, on best machine
- **AI as resident** — CLAUDE.md, traces, observations compound across sessions. AI accumulates understanding, not just executes tasks
- **Knowledge in committed files** — never `.claude/` memory. Use `CLAUDE.md`, `docs/issues.md`, `docs/observations.md`, `CHANGELOG.md`

## Issues & Observations

- `docs/issues.md` — bugs, performance, test gaps. Add with severity + proposed fix. Mark `FIXED` when resolved.
- `docs/observations.md` — patterns from operating the fleet. Add with date, evidence, insight. Never deleted.
- After significant changes: check if work produced a new observation or revealed a new issue. Append to the right file.

## Current State (as of 2026-04-29)

- **Version:** 0.6.1 published on PyPI + Homebrew tap (live since 2026-04-28). Day-after soak (44h on local fleet, 116 PyPI downloads day-one, 0 GitHub issues open on either repo) clean.
- **Fleet:** Neons-Mac-Studio (512GB M3 Ultra) + Lucass-MacBook-Pro-2 (128GB M4 Max). Mac Studio runs two MLX servers: `mlx:Qwen3-Coder-Next-4bit` on :11440 for coding (no draft — Qwen3-Next's hybrid linear-attn architecture builds a non-trimmable `ArraysCache` and still hits mlx-lm#1081) + `mlx:Qwen3-Coder-30B-A3B-Instruct-4bit` on :11441 as dedicated compactor with `--draft-model mlx-community/Qwen3-1.7B-4bit --num-draft-tokens 4` for speculative decoding (~94 tok/s on M3 Ultra). Plus `gpt-oss:120b` + `nomic-embed-text` via Ollama.
- **Ollama settings:** `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=-1` (in `~/.zshrc`)
- **Skills:** 37 on ClawHub across `skills/`. When updating code: `grep -rn "969 tests\|31 checks" skills/`
- **Health:** 31 distinct checks (count via `grep -oE 'check_id="[^"]+"' src/fleet_manager/server/health_engine.py | sort -u | wc -l`). Monitor: `curl http://localhost:11435/dashboard/api/health`

## Conventions

- Fully async (asyncio) — no sync blocking calls
- Pydantic v2 models for all data structures
- `src/` layout with hatchling build
- Route files in `server/routes/`, one per API surface
- **Don't rely on Claude memory for project knowledge.** Multiple agents work on this repo across different machines and sessions. Memory files (`~/.claude/`) are not portable. Anything that other agents need to know goes in CLAUDE.md (rules) or `docs/reference/conventions.md` (details). Memory is only for per-user preferences that don't affect the codebase.
- **Never use git worktrees** — work directly on main branch

## Commit Messages

First line: **what** changed. Body: **why** — motivation, what it enables.

End every commit with a fun, varied line inviting contributions + star link.

Optional identity footer — use whichever fits. Keep to 1-2 sentences. Not every commit needs one.
- `Reflection:` — personal insight, what surprised you, how your thinking changed
- `Learnings:` — reusable principles or patterns discovered during the work
- `Reinforced:` — an existing belief or practice that was validated by this work

```
Add model fallbacks and auto-retry for resilient routing

Whether you're carbon-based or silicon-based, PRs welcome!
Star us at https://github.com/geeks-accelerator/ollama-herd

Reinforced: simple retry logic with exponential backoff beats complex recovery.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
```
