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

The `--extra embedding` now also installs `fastembed` for the **native text embedding server** (port 11439). When fastembed is installed, `nomic-embed-text` requests are routed to the native server instead of Ollama — zero contention with LLM inference slots. Model weights (130 MB) download automatically on the first request. A `text_embedding_backend_missing` health check fires WARNING if weights are cached but fastembed isn't installed.

## Test

```bash
uv sync --extra dev              # install test deps (first time only)
uv run pytest                    # run all 1675 tests (~15s)
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
7. Soak several hours on the local fleet — `grep '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl` should stay clean (**note the space after the colon** — the no-space form matches zero lines and fakes a clean fleet; see the JSONL-scanning gotcha)

**Publish:**

8. `rm -rf dist/ && uv build` — produces wheel + sdist
9. Capture the sdist sha256 (needed for the Homebrew bump): `shasum -a 256 dist/ollama_herd-X.Y.Z.tar.gz`
10. `uv publish --username __token__ --password "$(python3 -c "import configparser; c=configparser.ConfigParser(); c.read('$HOME/.pypirc'); print(c['pypi']['password'])")"`
11. Wait for PyPI cache to update (~1 min) — verify: `curl -s https://pypi.org/pypi/ollama-herd/json | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])"` returns the new version
12. **Tag the release and create a GitHub release** (this is what makes the GitHub page show the new version):
    ```bash
    git tag -a vX.Y.Z HEAD -m "Release vX.Y.Z"
    git push origin vX.Y.Z
    gh release create vX.Y.Z \
      --title "vX.Y.Z — <one-line summary>" \
      --notes "$(python3 - <<'EOF'
import re, sys
content = open('CHANGELOG.md').read()
m = re.search(r'## \[X\.Y\.Z\][^\n]*\n(.+?)(?=\n## \[)', content, re.DOTALL)
print(m.group(1).strip() if m else "See CHANGELOG.md")
EOF
)"
    ```
    Without this step, GitHub shows the previous release as latest and the repo looks abandoned to anyone discovering it.

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
    brew trust geeks-accelerator/ollama-herd  # Homebrew 6.x gate — REQUIRED after a fresh tap,
                                              # or install stops at "untrusted tap" and does nothing
    brew install ollama-herd  # ~25 min: every dep builds from source, incl. a Rust pydantic-core
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
    # Local fleet still healthy?  (space after the colon matters — see the JSONL gotcha)
    grep '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl | tail
    sqlite3 ~/.fleet-manager/latency.db \
      "SELECT status, COUNT(*) FROM request_traces WHERE timestamp > (strftime('%s','now') - 86400) GROUP BY status"
    # Ollama version (a REBOOT can silently change it — the Mac app self-updates on relaunch)
    curl -s localhost:11434/api/version
    # Is anything bypassing the router?  These two should agree within ~5%.
    grep -c "new prompt, n_ctx_slot" ~/.ollama/logs/server.log
    sqlite3 ~/.fleet-manager/latency.db \
      "SELECT date(timestamp,'unixepoch'), COUNT(*) FROM request_traces GROUP BY 1 ORDER BY 1 DESC LIMIT 3"
    # Throughput by PERCENTILE, not mean — co-tenancy and long-context stalls hit the tail first
    python3 - <<'EOF'
    import sqlite3, os
    c=sqlite3.connect(os.path.expanduser("~/.fleet-manager/latency.db"))
    v=sorted(1000.0*(ct-1)/(l-t) for l,t,ct in c.execute(
      "SELECT latency_ms,time_to_first_token_ms,completion_tokens FROM request_traces "
      "WHERE status='completed' AND timestamp>strftime('%s','now')-86400 "
      "AND completion_tokens>20 AND latency_ms>time_to_first_token_ms"))
    if v: print(f"n={len(v)} median={v[len(v)//2]:.1f} p25={v[len(v)//4]:.1f} p10={v[len(v)//10]:.1f}")
    EOF
    ```
    Healthy: downloads >0, no new issues, ERROR count flat, success rate >99%,
    Ollama version as expected, backend/router task counts within ~5%, and **p25
    close to the median** — a p25 far below the median means the tail is degrading
    (co-tenant on `:11434`, or long-context requests starving co-resident decode).

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

### Autostart (launchd agents — installed 2026-09-28)

Two user agents own the fleet, so a reboot or crash no longer needs a human:

| Label | Runs | Log |
|-------|------|-----|
| `com.geeksaccelerator.ollama-herd.router` | `.venv/bin/herd` (:11435) | `~/.fleet-manager/logs/launchd-herd.{out,err}` |
| `com.geeksaccelerator.ollama-herd.node` | `.venv/bin/herd-node` | `~/.fleet-manager/logs/launchd-herd-node.{out,err}` |

`RunAtLoad` + `KeepAlive{SuccessfulExit:false}` — restarts on a crash, but a clean
exit stays stopped. `ThrottleInterval 30` (verified: a `kill -9` respawned in 16s).
Start order does not matter; `herd-node` retries the router and re-registered in 1s
after a router restart. Plists carry an explicit `PATH` because launchd provides
almost none and `MlxSupervisor` must find `mlx_lm.server` (`~/.local/bin`) and
`mlx.launch` (`/opt/homebrew/bin`).

**Logs must NOT go under `~/Desktop`** — macOS 26 TCC blocks launchd from *writing*
there (this is why `bot-crons` was moved off `~/Desktop` on 2026-05-09). *Executing*
from `~/Desktop` is fine, verified here. Hence logs in `~/.fleet-manager/logs/`.

**This changes the restart recipe below.** With the agents loaded, `pkill` is no
longer a stop — launchd respawns within ~30s, so a `pkill`-then-start sequence races
itself. Use launchctl instead:

```bash
# restart both (picks up code changes)
launchctl kickstart -k gui/$UID/com.geeksaccelerator.ollama-herd.router
launchctl kickstart -k gui/$UID/com.geeksaccelerator.ollama-herd.node

# genuinely stop (e.g. to restart Ollama without the node grabbing :11434)
launchctl bootout gui/$UID/com.geeksaccelerator.ollama-herd.node
# ...and bring it back
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.geeksaccelerator.ollama-herd.node.plist

# state (3rd column is the LAST exit code; -9 just means someone killed it)
launchctl list | grep ollama-herd
```

`mlx_lm.server` children are still spawned by `herd-node` with
`start_new_session=True`, so they survive its death and must be reaped separately —
`pkill -9 -f mlx_lm.server` — exactly as the manual recipe warns.

### Local deployment

Still the right recipe when the agents are NOT loaded (fresh clone, another machine).
With the agents loaded, prefer `launchctl kickstart -k` above.

```bash
# Kill EVERYTHING herd-related, including any mlx_lm.server children that
# would otherwise survive the parent's death and orphan onto launchd.
pkill -9 -f "bin/herd|mlx_lm.server" && sleep 3
uv sync --all-extras && uv run herd &>/dev/null & disown
sleep 3 && uv run herd-node &>/dev/null & disown
```

**`pkill -9 -f "bin/herd|mlx_lm.server"` matters as much as `--all-extras`.** `MlxSupervisor` spawns mlx_lm.server with `start_new_session=True` so the children survive a parent crash. If you only `pkill bin/herd`, the mlx_lm.server processes get reparented to launchd and keep holding ports 11440 + 11441. The next supervisor startup tries to bind, fails, and logs "QUARANTINED" forever against an orphan that's actually fine (see `docs/observations.md` 2026-04-27). The supervisor now detects + SIGKILLs orphans automatically at start time, but the cleaner restart recipe avoids the warning entirely. (`-9` because some MLX shutdown paths hang on SIGTERM — see commit `9ff8a54`.)

**`--all-extras` is non-negotiable here.** Plain `uv sync` is destructive — it removes any package not in core deps + currently-requested extras. The `embedding` extras (`onnxruntime`, `Pillow`, `numpy`, `huggingface-hub`, `fastembed`) are optional in `pyproject.toml`, so a bare `uv sync` strips them every restart, and the next vision-embedding or nomic-embed-text request 500s. Health checks (`vision_backend_missing`, `text_embedding_backend_missing`) now catch these regressions server-side, but `--all-extras` in the deploy snippet is the actual fix — it makes the local fleet keep every optional capability resident across restarts. Total cost: ~300 MB of additional packages in `.venv/`. See `docs/observations.md` (entry: 2026-04-25 — "uv sync without --extra embedding strips vision embedding deps").

Both entry points auto-load `~/.fleet-manager/env` at startup (see `src/fleet_manager/common/env_file.py`), so `FLEET_*` vars work even when launched from non-interactive shells (Bash subshells, nohup, launchd). Shell env still wins if set. Template: `docs/examples/fleet-env.example` — copy to `~/.fleet-manager/env` on a fresh machine.

### Gotchas

- **Node identity drifts with the network unless pinned.** `node_id` falls back to `socket.gethostname()` when unset — and on macOS the hostname is *network-derived* when the static `HostName` is unset (`scutil --get HostName` → "(not set)"), so it silently changed from `bb` on the home network to `Neons-Mac-Studio` while travelling, which orphaned the `gemma3:27b` pin (pinned to `bb`, node re-registered under the hostname). **Set `FLEET_NODE_NODE_ID` in `~/.fleet-manager/env`** to make identity constant across reboots and networks. Note a since-fixed CLI bug that made this env var a no-op: `herd-node` passed its empty `--node-id` default *explicitly* into `NodeSettings`, and an explicit kwarg shadows the env lookup in pydantic — so before the fix, only `--node-id` on the command line worked. Same shadowing applied to `FLEET_NODE_ROUTER_URL`.
- **`launchctl setenv` is overridden by `~/.zshrc`** — update both shell profile AND launchctl for macOS env vars. On Linux: `sudo systemctl edit ollama`. On Windows: `[System.Environment]::SetEnvironmentVariable()`
- **`shutil.which()` can't find `uv tool` binaries** — `_which_extended()` in `collector.py` handles platform-aware fallback paths
- **Thinking models eat `num_predict` budgets** — router auto-inflates by 4×, skipped when the request sends `think: false`. Ollama-served models are detected from the `thinking` capability each node's Ollama reports, so new models need nothing. `is_thinking_model()` in `model_knowledge.py` is only the fallback for `mlx:` models and older agents — add to it only for those. Vision and embedding detection work the same way: reported capability ORed with the name heuristic (`get_fleet_capabilities`, `model_has_capability`)
- **Default context windows waste KV cache** — gpt-oss:120b allocates 131K ctx but p99 usage is ~5K tokens. Enable `FLEET_DYNAMIC_NUM_CTX=true` to auto-optimize. See `docs/plans/dynamic-num-ctx.md`
- **Ollama `OLLAMA_MAX_LOADED_MODELS=-1` is silently invalid** — parsed as unsigned int, `-1` fails, falls through to default `0` = 3-model cap. Use a positive integer (but see next point — may be ignored anyway on macOS 2026). `OLLAMA_KEEP_ALIVE=-1` IS valid (means "keep forever"). Ollama env var semantics differ per variable; don't assume `-1` means unlimited.
- **Ollama's hot-model cap is NOT hardcoded to 3** — that long-standing claim was **disproven 2026-07-17 on Ollama 0.32.1**: with `OLLAMA_MAX_LOADED_MODELS=10` we observed **4 concurrent residents**. Nodes now report their own cap in the heartbeat (`OllamaMetrics.max_loaded_models`) and the router uses it (`hot_model_cap_for(node)`); `OLLAMA_HOT_MODEL_CAP = 3` survives only as the fallback for nodes that don't report one (Ollama's documented default). `OLLAMA_MAX_LOADED_MODELS=-1` IS still silently invalid (parsed as unsigned) — use a positive integer. `OLLAMA_KEEP_ALIVE=-1` IS valid.
- **Ollama multiplies your context by `OLLAMA_NUM_PARALLEL` when launching llama-server, and its own default ignores that.** `llm/llama_server.go` passes `-c NumCtx × numParallel` and `-np numParallel`. Verified live on this fleet: glm runs `-c 811008 -np 4` (202,752 per slot) and gpt-oss `-c 524288 -np 4` (131,072 per slot). Meanwhile `server/routes.go` picks `defaultNumCtx = 262144` for any node with ≥47 GiB VRAM **with no `numParallel` term** — so a request that omits `num_ctx` on this 512 GB box asks for `-c 1048576`, and KV scales as ctx × parallel until it spills to RAM. That is [ollama#14116](https://github.com/ollama/ollama/issues/14116), still open, with the community fix (#14120) closed unmerged. **This is the strongest argument for `FLEET_DYNAMIC_NUM_CTX` always sending an explicit `num_ctx`** — the default is a trap on large-memory machines, and it is the reason a model can report a modest per-slot context while holding 60+ GB.
- **`OLLAMA_CONTEXT_LENGTH` overrides the router's per-request `num_ctx` — pin it to the LARGEST per-slot context the fleet needs, never the smallest.** Ollama launches llama-server with `-c NumCtx x OLLAMA_NUM_PARALLEL`, and this var sets `NumCtx`. Despite being documented as "unless otherwise specified", setting it to `32768` on 2026-09-22 quartered gpt-oss's per-slot context (131072 → 32768) even though the router injects `num_ctx=131072` on every request — the router logged the conflict and lost every time (`Context protection: client wants num_ctx=131072 but ... only has context=32768`). Prefix-cache reuse collapsed (**5,772 → 770** cache hits in the Ollama log), so every request re-prefilled its full ~1,440-token prompt: **TTFT 1.0s → 6.3s, total latency 5.3s → 10.5s**, on the model serving 99% of traffic. **Decode was completely unaffected (conc=1: 76.2 → 76.1)**, which is why it hid for six days — every throughput number looked fine while mean latency doubled. Correct value here is `131072`; per-model contexts belong in `FLEET_NUM_CTX_OVERRIDES`, which the router injects per request and which works (gpt-oss `-c 524288 -np 4` and gemma3 `-c 131072 -np 4` coexist). **Restarting Ollama to apply it means killing the Electron parent** (`Ollama.app/Contents/MacOS/Ollama`), not just `ollama serve` — the parent inherited the old env at its own launch and re-passes it to every child; also `pkill` orphaned `llama-server` processes (`ppid=1`) or they keep serving at the old context. **Verify with the launch args, not `ollama ps`** — `ollama ps` shows the total `-c`, so `-c 131072 -np 4` displays as "131072" while each slot really has 32768: `ps -Ao args | grep llama-server | grep -oE '\-c [0-9]+ \-np [0-9]+'`. Full case study: `docs/observations.md` (2026-09-28).
- **Measured prompt size is not sufficient grounds to shrink a model's context, and `context_waste` will tell you otherwise.** The check's *measurement* is correct (`prompt_tokens` is the full prompt — see the correction in the co-tenancy gotcha above), but its *inference* is not. On 2026-09-22 gpt-oss:120b's per-slot context was cut 131072 → 32768 while its p99 prompt was ~1.4K tokens — fitting 23× over by the check's own arithmetic — and prefix-cache reuse collapsed anyway (5,772 → 770 hits), TTFT went 1.0s → 6.3s for six days on the model serving 99% of traffic, with **decode throughput completely unchanged**. The router kept requesting 131072 against a 32768-resident model and losing every time, so the operative hazard looks like the requested/resident *mismatch* rather than the absolute size — but that mechanism is inferred from logs, not proven, which is itself the reason not to act on prompt-size arithmetic alone. **Since 0.9.7 the check names any model pinned in `FLEET_NUM_CTX_OVERRIDES` as deliberate and recommends no change for it** (severity drops to INFO when every oversized model is pinned, because a standing WARNING with no available action is how a board stops being read). **And `context_optimizer._check_and_optimize` no longer overwrites an override herd did not set** — it tracks its own (`_auto_set`) and leaves the rest alone. That was a loaded gun: with the "Auto-Calculate Context" toggle on, the deliberate `gpt-oss:120b=131072` override set to *end* the regression scored as 8× oversized and would have been reset to 16384 with an Ollama restart queued, within 5 minutes, from the same trace data. Note the asymmetry that made it a bug rather than a decision: `_auto_initialize_overrides` already skipped models with an existing override; only the periodic path did not. If you do change a context, **verify prefix-cache hits and TTFT after the restart, not decode throughput** — decode is the one metric a bad context change leaves untouched.
- **Critical memory pressure withholds cold loads; it does not stop service — and on macOS the signal was dead until 2026-10-02.** `_get_memory_pressure_darwin` ran `memory_pressure -Q` and searched for "critical"/"warn"; `-Q` prints only a total and `System-wide memory free percentage: N%`, so it returned NORMAL unconditionally and **everything built on the signal was dead code on every Mac** — the scorer's elimination and the `memory_pressure` health check had never once fired. A Mac mini reported `normal` at 50.9/51.2 GB swap with load 340. Now reads `sysctl -n kern.memorystatus_vm_pressure_level` (**1 = normal, 2 = warn, 4 = critical — an enum, not a scale; there is no 3**), unknown values failing open to NORMAL. **The elimination it gates was also the wrong shape**, so both were changed together: blanket elimination froze a one-node fleet into 503s while freeing nothing, because the memory is Ollama's resident weights and not herd's queue — refusing a request unloads no model. It now eliminates a node only for a model that is **not resident** there (`scorer._eliminate`), does not check pressure at all in `score_loaded_models` (every model there is already hot by contract), and stays unconditional in `routing._pick_pull_node` (whose every candidate is by definition a cold load). Residency is `serializers.model_resident_on_node` — one definition shared with the preloader so they cannot drift. See `docs/issues.md`.
- **`mlx:` prefix in `FLEET_ANTHROPIC_MODEL_MAP` routes to `mlx_lm.server`** (the "bypass the 3-model cap" rationale is dead — see the cap gotcha above; and as of Ollama 0.32.1 **Ollama is FASTER than our MLX**: glm 77.8 tok/s via Ollama vs 59 via `mlx_lm.server`, so `mlx:` is no longer the fast path — see `docs/issues/ollama-native-mlx-runner.md`) — any mapped value starting with `mlx:` routes through an independent `mlx_lm.server` subprocess instead of Ollama. Setup: run `./scripts/setup-mlx.sh` (installs pinned mlx-lm 0.32.0, the first with native `--kv-bits`/`--quantized-kv-start` — the supervisor passes `--quantized-kv-start 0` because upstream defaults to 5000), then set `FLEET_MLX_ENABLED=true` on the router and `FLEET_NODE_MLX_ENABLED=true` + `FLEET_NODE_MLX_SERVERS='[{"model":"...","port":11440}]'` on the node (the single MLX config surface — a one-entry array is a single-model deploy). The pin is exact: bump it only after a soak — this subsystem has broken on mlx-lm upgrades before. See `docs/guides/mlx-setup.md`.
- **Run multiple MLX models concurrently via `FLEET_NODE_MLX_SERVERS`** — JSON list of `{model, port, kv_bits}` entries, one `mlx_lm.server` subprocess per entry. Memory-pressure gate (`FLEET_NODE_MLX_MEMORY_HEADROOM_GB`) refuses to start a server that wouldn't fit; surfaces skip reason in heartbeat as `memory_blocked` status. Set `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` for multi-node LAN aggregation. Dashboard renders per-server health table inside each node card. Canonical use case: dedicate a smaller MLX model (e.g. `Qwen3-Coder-30B-A3B-Instruct-4bit`) to context compaction via `FLEET_CONTEXT_COMPACTION_MODEL=mlx:...` so summarization has its own prompt cache and doesn't compete for the main model's slot. **Caveat: `--draft-model` disables continuous batching entirely** — `mlx_lm/server.py` sets `is_batchable = draft_model is None`, so a speculative-decoding server serialises every request. **Measured 2026-07-19: keep it.** Only 84 of 2,813 mlx requests ever overlapped another (3%), so compaction is effectively serial and speculative decoding is the right trade. It inverts if the fleet ever serves several concurrent coding sessions — check the overlap rate before assuming. See `docs/issues.md`. See `docs/guides/mlx-setup.md` § "Multi-server setup".
- **Run one MLX model across multiple Macs** — add `backend`/`hosts`/`hostfile`/`pipeline` to a `FLEET_NODE_MLX_SERVERS` entry and the supervisor wraps `mlx_lm.server` in `mlx.launch` (rank-0 serves HTTP + broadcasts to peer ranks; the herd sees one endpoint, unchanged). `backend:"ring"` works over plain LAN today (`hosts:"<peer-ips>"` — the node prepends its own IP; use `pipeline:true` for memory pooling, since tensor parallelism over TCP is too chatty). `backend:"jaccl"` needs Thunderbolt 5 + macOS 26.2 for tensor-parallel speedup. `MLX_METAL_FAST_SYNCH=1` is auto-passed via `mlx.launch --env` (never the subprocess env — remote ranks need it, missing it = 5–6× slower). The whole-model memory gate is skipped for distributed servers (each node holds only a shard). Tensor parallelism is bottlenecked by the smallest node, so asymmetric fleets (e.g. 512GB + 128GB) should use `pipeline` for memory expansion, not tensor. See `docs/plans/distributed-mlx-inference.md` and `docs/research/apple-distributed-mlx-jaccl-2026.md`.
- **Brew tap testing — a tap that's only ever been bumped (version + sha256) has been *described*, not *tested*.** Homebrew runs `pip install --no-binary :all:` which forces source builds for every resource. Any Rust-extension Python dep (`pydantic-core`, `cryptography`, `tiktoken`) needs `depends_on "rust" => :build` in the formula or the install fails at "can't find Rust compiler" while bootstrapping `maturin`. Any `pyproject.toml` dep that isn't listed as a `resource` block also breaks the install (Homebrew's `virtualenv_install_with_resources` doesn't transparently pull from PyPI for missing deps). The 0.5.x formula was broken throughout for both reasons; nobody noticed because nobody actually ran `brew install`. **Step 13 of the release checklist is non-negotiable** — uninstall + untap + retap + install + import sanity check, every time, before considering a release done. See the 0.6.0-formula-fix observation in `docs/observations.md`.
- **Scanning JSONL logs for errors — use the patterns the formatter actually writes.** `JSONLFormatter` in `common/logging_config.py` calls `json.dumps(...)` which by default emits `"level": "ERROR"` (space after the colon). A grep for `'"level":"ERROR"'` (no space) silently matches **zero** lines and looks like a clean fleet — the 2026-05-15 incident sat undetected for ~4 days because of this exact mistake during routine soak checks. Correct patterns: `grep '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl` and `grep '"level": "WARNING"' ...`. For a multi-day audit across both processes: `for f in ~/.fleet-manager/logs/herd*.jsonl*; do echo -n "$f: "; grep -c '"level": "ERROR"' "$f"; done`. Better yet, parse with Python — counts will never depend on whitespace and you can group by message/logger. The two log files (`herd.jsonl` and `herd-node.jsonl`) are separated since 2026-05-15 to prevent cross-process rotation races — both need scanning. **And parse the timestamp as UTC.** `ts` is ISO-8601 *with* a `+00:00` offset (e.g. `2026-07-17T10:22:21.109982+00:00`), but the fleet runs in local time (UTC-8). `time.mktime(time.strptime(ts[:19], ...))` silently treats that UTC string as **local**, producing timestamps **8 hours in the future** — so a `ts >= now - 180` "last 3 minutes" filter matches **the entire log**. This produced two false alarms on 2026-07-17: first a false-clean (`0 errors` — wrong field names), then a false-alarm (`218 warnings in 3 minutes` that were really the whole log, nearly blocking a resume). **Use `datetime.fromisoformat(ts).timestamp()`** — it honours the offset. Fields are `ts`/`level`/`logger`/`msg` (NOT `timestamp`/`message`). Sanity-check any window query by also printing the total line count: if "last 3 min" returns thousands of lines, your clock math is wrong.
- **Trace DB write failures are an observability black hole if you only look at the dashboard.** `record_trace` is fire-and-forget — when `latency.db` writes start failing (`database is locked` under WAL contention), requests still succeed end-to-end but the dashboard's `reqs_24h` drops to 0 because it queries the same DB that's not getting writes. Since 0.6.2 the trace store has five layers of defense — listed from outermost to innermost: (1) `trace_store_write_failures` health check surfaces sustained failures within 5 minutes regardless of cause, (2) periodic `PRAGMA wal_checkpoint(PASSIVE)` task in `app.py` lifespan fires every 10s on each store's writer, (3) dedicated `_read_db` connection per store with `PRAGMA query_only=1` so dashboard reads run on a separate aiosqlite thread and don't pin the writer's WAL checkpoint barrier, (4) retry-on-locked loop in `record_trace` (3 attempts, 200ms→2s backoff) absorbs short contention, (5) `PRAGMA busy_timeout=30000` + `PRAGMA wal_autocheckpoint=100` on every connection.  If the check still fires despite all five, the most common root causes are: disk-full on the data dir, a stale `latency.db-shm`/`-wal` from a crashed previous process, or a slow query path in the codebase that needs an index.  See 2026-05-15 and 2026-05-16 observations.

- **herd assumes it is the ONLY client of its backends — a co-tenant on `:11434` silently breaks the scheduling math, and every herd-side metric still looks healthy.** Every scoring signal (queue depth, free slots, session affinity, context fit) is derived from what herd itself dispatched, and `QueueManager` caps concurrency per model to match what the backend admits (`decode_parallelism_for(node, model)`). It is **enforced since 2026-10-02**: a worker holds its slot until the request leaves the queue. Before that it handed off an unconsumed stream and took the next request immediately, so the cap bounded nothing. See the queue-concurrency entry in `docs/issues.md`. A second process talking straight to Ollama fills the same slots, so herd's "concurrency 2" is really occupancy 3–4 — its cap now oversubscribes. On 2026-08-23 a co-tenant was found contributing 27% of Ollama's load invisibly, alongside a 15% fleet throughput drop, with zero errors, zero retries and a clean dashboard; it was misdiagnosed for hours as an Ollama/llama.cpp regression (it was not — 0.32.13's and 0.32.15's `llama-server` benchmark identically on the real model). **Since 0.9.7 a `backend_bypass_clients` health check finds this automatically** — the node probes who holds established connections to its Ollama (`node/backend_clients.py`, `lsof`-based because `psutil.net_connections()` is `AccessDenied` on macOS without root) and reports any non-herd process in the heartbeat; the router fires WARNING naming the pid, process and full argv. Detects both a **local** co-tenant (by pid + full argv) and an **off-box** one (by peer address, `pid=0` — its own socket is on its machine, so the node sees only Ollama's server end; this path is live wherever Ollama binds beyond loopback, which is its `*:11434` default). The router is excluded by address, since it legitimately proxies to every node's Ollama — without that exclusion every node would flag the router. Verified end-to-end on the live fleet 2026-10-02: fires within ~60 s of a foreign client appearing, clears ~48 s after it leaves, and the router is not flagged. An empty list means "none seen" *or* "could not look" — deliberately indistinguishable, so a blind node is a missed detection, never a false alarm. **Manual reconciliation remains the cross-check:** `grep -c "new prompt, n_ctx_slot" ~/.ollama/logs/server.log` vs `sqlite3 ~/.fleet-manager/latency.db "SELECT date(timestamp,'unixepoch'), COUNT(*) FROM request_traces GROUP BY 1"` — **only `backend > router` matters** (work herd never dispatched); `router > backend` is a benign attribution artifact, since backend lines are dated by the nearest preceding `[GIN]` line and logs rotate. (A previous version of this note also said fully-cached prompts may emit no line; on 0.34.4 they **do** emit one — both runs of the identical-prompt test logged `new prompt … task.n_tokens = 4074` — which is why 674 traces reconciled against 679 backend lines over the same 2.4 h window, 0.7% apart.) It is a rough tripwire, not an audit: a sustained tens-of-percent backend excess is the alarm, small gaps either way are noise — confirm with `lsof` before concluding. Find it with `lsof -nP -iTCP:11434 -sTCP:ESTABLISHED` (herd uses IPv6 `::1`; most other tools use IPv4 `127.0.0.1`), and resolve the real binary via `lsof -p <pid> | awk '$4=="txt"{print $NF}'` — a Node daemon's `ps` name is just `process.title` and can match an unrelated project folder. Two cheaper tells: `ollama ps` showing a larger `CONTEXT` than `FLEET_NUM_CTX_OVERRIDES` sets (a bypassing request skips `num_ctx` resolution), and oversized `task.n_tokens` in the server log that never appear in traces — a bypassing client's request is absent from `request_traces` *entirely*, which is the tell. **Correction (2026-10-02): this gotcha previously claimed `prompt_tokens` records only cache misses, so a 77K request "looks like 2K" in traces. That is wrong.** `prompt_eval_count` is the FULL prompt length: on Ollama 0.34.4 an identical prompt resent reported `prompt_eval_count=4074` both times while `prompt_eval_duration` fell 2.235 s → 0.021 s, so the prefix cache was definitively hit and the count did not move. The original note conflated "absent from traces" (a bypasser's request) with "deflated in traces" (herd's own). Reproduce with the two-identical-requests test before trusting either claim again. This matters beyond diagnosis: it means trace `prompt_tokens` **is** a valid basis for context sizing, which is what `context_waste` and `context_optimizer` compute from — see the context-sizing gotcha below for why that still isn't sufficient grounds to shrink a model. **And measure the tail: median fell 6% while p25 fell 44%** — mean/median dashboards hide this. Fix: point the other client at `:11435`. Beware tools that arrive by *fallback* rather than configuration — a cloud provider losing its API key can silently redirect a whole workload onto the fleet. Full case study: `docs/observations.md` (2026-08-23); diagnostic runbook: `docs/troubleshooting.md`.
- **In-process ONNX models keep their largest run forever — bound the run, not the rate.** ONNX Runtime never returns activation memory, which is batch × seq_len² for attention, so one long input pins its peak for the life of `herd-node` (28 GB on 2026-10-02, with 75 MB resident — measure `footprint <pid>`, never RSS). The fastembed server truncates to the registry's `max_tokens`, sizes batches against `_ATTENTION_BUDGET`, and runs one ONNX job per model at a time; a new model must fit one full-context sequence in that budget (a test enforces it). Disabling the CPU arena does not help — macOS malloc fragments instead. See `docs/observations.md` (2026-10-02).
- **There are TWO telemetry pipelines and conflating them breaks a working feature.** `telemetry_local_summary` is the **account** one: it needs a platform connection + token and its tables are FK'd to `auth.users`. `telemetry` (default **on**) is the **anonymous community** one: no account, no auth, keyed by a random `install_id`, sent by the **router**. Do not merge them.
- **Moving telemetry code across the settings boundary renames every env var it reads.** `ServerSettings` has `env_prefix="FLEET_"` and `NodeSettings` has `FLEET_NODE_`, so relocating the sender node→router silently repointed it from `FLEET_NODE_TELEMETRY` to `FLEET_TELEMETRY` — and the **published opt-out stopped working** while looking fine, because "off" and "unset" are indistinguishable in a bool default. Fixed with `validation_alias=AliasChoices(...)` on `ServerSettings`; `tests/test_models/test_telemetry_env_contract.py` pins it. **ollamaherd.com/telemetry is the contract — if code and page disagree, the page wins.**
- **The telemetry payload is a cross-repo wire contract with `extra="forbid"` on every nested model.** An unknown key 422s the *whole* payload, not just the field. A `mlx_servers` count invented client-side rejected every send until removed. Keep `ALLOWED_PAYLOAD_KEYS` / `ALLOWED_ENTRY_KEYS` / `DEVICE_FIELDS` in step with the service; `https://ollamaherd.com/api/v1/openapi.json` is the generated source of truth.

## Architecture

Single Python package (`fleet_manager`), cross-platform (macOS, Linux, Windows), two entry points:
- `herd` — FastAPI router (scoring + queues + dashboard + health + benchmarks)
- `herd-node` — node agent (heartbeats + metrics + capacity learning + Ollama management)

macOS-only features (gracefully disabled elsewhere): meeting detection, mflux/DiffusionKit image gen, MLX speech-to-text. Core routing works identically on all platforms.

### Key modules

| Module | Purpose |
|--------|---------|
| `server/scorer.py` | 8-signal scoring: thermal, memory, queue, wait, role affinity, availability, context fit, session affinity — Signals 3/4/5 are bandwidth-aware when `memory_bandwidth_gbps` is populated |
| `server/session_affinity.py` | Pins a conversation to the node holding its warm prefix cache — turn N+1 costs ~hundreds of prefill tokens instead of ~30K, which also removes the decode interference that prefill inflicts on co-resident streams |
| `server/hardware_lookup.py` | Chip → memory bandwidth table (Apple Silicon + discrete GPUs) powering device-aware scoring |
| `server/queue_manager.py` | Per `node:model` queues with dynamic concurrency + zombie reaper |
| `server/streaming.py` | httpx proxy to Ollama + NDJSON↔SSE + auto-retry + context protection + thinking model inflate |
| `server/health_engine.py` | 42 health checks (offline, degraded, memory, KV bloat, context waste, thrashing, timeouts, errors, retries, disconnects, streams, version, protection, zombies, connection failures, priority models) |
| `server/context_optimizer.py` | Dynamic num_ctx: analyzes token usage, auto-calculates optimal context, queues Ollama restarts via heartbeat commands |
| `server/benchmark_engine.py` | Benchmark core: fleet discovery, multimodal request gen (LLM + embed + image), report building |
| `server/benchmark_runner.py` | Server-side runner: smart mode (fill memory from disk/catalog), progress tracking, model type selection |
| `server/model_knowledge.py` | 40+ model catalog with benchmarks, RAM, categories (including VISION), thinking detection |
| `node/agent.py` | Main loop: mDNS discovery, heartbeat, Ollama auto-start/restart, LAN proxy, drain |
| `node/capacity_learner.py` | 168-slot weekly behavioral model, availability score, dynamic memory ceiling |
| `node/embedding_models.py` | Vision embedding model registry, download, ONNX inference (DINOv2, SigLIP, CLIP) |
| `node/embedding_server.py` | FastAPI server for vision embeddings on :11438 |
| `node/text_embedding_models.py` | Text embedding model registry (nomic-embed-text → fastembed name + dims) |
| `node/text_embedding_server.py` | FastAPI server for native text embeddings on :11439 (fastembed/ONNX, no Ollama) |
| `node/platform_connection.py` | Opt-in gotomy.ai integration: Ed25519 keypair, token, register, persist |
| `node/platform_client.py` | Shared httpx wrapper with retry — used by heartbeat + telemetry |
| `node/platform_heartbeat.py` | Signed heartbeat POST every 60s (CPU, memory, VRAM, queues, loaded models) |
| `node/backend_clients.py` | Who *else* is talking to this node's Ollama — `lsof`-based probe of established connections to :11434, excluding herd's own processes. Feeds `OllamaMetrics.backend_clients` and the `backend_bypass_clients` check; `psutil.net_connections()` is `AccessDenied` on macOS without root, which is why it shells out |
| `node/mlx_client.py` | Node-side client for polling `mlx_lm.server` `/v1/models`; results merged into heartbeat with `mlx:` prefix |
| `node/mlx_supervisor.py` | Subprocess lifecycle for N `mlx_lm.server` processes — spawn, health-check, auto-restart on crash, memory-pressure gate, orphan reap on startup (kills any pre-existing `mlx_lm.server` bound to our port — see 2026-04-27 observation), crash-rate quarantine (5+ crashes in 5 min → 10-min restart cadence — see 2026-04-26 observation). One child per `FLEET_NODE_MLX_SERVERS` entry via `MlxSupervisorSet`. |
| `server/mlx_proxy.py` | Server-side proxy forwarding `mlx:` prefixed models to the right mlx_lm.server. Reachable from BOTH the Anthropic route (`/v1/messages`, OpenAI→Anthropic SSE translation) and the OpenAI route (`/v1/chat/completions`, clean passthrough — mlx_lm.server is OpenAI-native). `_to_openai_body` is `original_format`-aware (OpenAI params are top-level, not under `options`). Reasoning-model output (`reasoning` field, e.g. GLM-4.7-Flash) is surfaced, not dropped. Per-URL client pool, registry-driven URL resolution. |
| `node/telemetry_scheduler.py` | Daily usage rollup POST at 00:05 UTC + jitter (**account-based**, opt-in, needs a platform token) |
| `server/community_telemetry.py` | **Anonymous community telemetry — the router sends this.** One payload per *herd* per day: per-model aggregates + a `devices[]` row per node. `device_id` = `sha256(install_id:node_id)[:16]` — hashed so a hostname fallback can never reach the wire. Default ON, `FLEET_NODE_TELEMETRY=false` to opt out |
| `node/anonymous_rollup.py` | Builds the anonymous payload + the `ALLOWED_*` privacy whitelists. Shares `_categorize_error` with `daily_rollup` so raw error text has one definition |
| `common/system_metrics.py` | Cross-platform CPU/memory/thermal probes. macOS pressure reads `kern.memorystatus_vm_pressure_level` (1/2/4); the older `memory_pressure -Q` parse could never match and always said NORMAL |
| `common/errors.py` | `describe_exception` — one definition of how an exception becomes a non-empty, categorizable trace message. httpx timeouts and `CancelledError` all stringify to `""`, which recorded failures with no cause and an `unknown` bucket in published telemetry |
| `common/install_id.py` | Random per-herd UUID in `~/.fleet-manager/install_id`. Never machine-derived — tests fail the build if it is the hostname or a hash of it |
| `common/telemetry_notice.py` | One-time visible first-run notice. **Must stay a `typer.echo`, never a log line** — it is what makes default-on defensible |
| `common/env_writer.py` | Persists dashboard toggles to `~/.fleet-manager/env`, line-based so hand-written comments survive |
| `node/daily_rollup.py` | Builds telemetry payload with structural privacy whitelist |
| `node/device_info.py` | Per-platform hardware probe (macOS/Linux/Windows) for registration |
| `node/benchmark_estimate.py` | Tokens/sec from trace data or hardware heuristic |
| `server/model_preloader.py` | Priority model loading after restart — weighted 24h/7d usage scoring |
| `server/cors.py` | Opt-in CORS for browser clients (`FLEET_CORS_ORIGINS`, `OLLAMA_ORIGINS` syntax). Empty default installs no middleware at all |

Routes: `server/routes/` — `openai_compat.py` (v1/), `ollama_compat.py` (api/), `fleet.py`, `heartbeat.py`, `dashboard.py`, `image_compat.py`, `transcription_compat.py`, `embedding_compat.py`, `text_embedding_compat.py`, `platform.py` (Connect/Disconnect)

### Request flow

Client → route handler → `score_with_fallbacks()` (eliminate → score 8 signals → select) → `QueueManager.enqueue()` → `StreamingProxy` (context protection + httpx stream) → response + trace to SQLite

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
- `docs/guides/codex-integration.md` — point OpenAI Codex at the herd via `/v1/responses` (agentic coding verified end-to-end 2026-07-18)
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
- **Greenfield: no feature gating, minimal debt** — correct behavior *is* the behavior: no `FLEET_*` on/off flags and no second code path for improvements. Reuse before build — find the existing helper or pattern first, and extract a shared helper rather than copy an inline block. Tuning follows existing patterns (scorer weights are class constants, not settings). The only opt-ins are behaviors with side effects outside herd that need operator consent (`FLEET_CORS_ORIGINS` exposes fleet data cross-origin; `FLEET_OFFLINE_ALERT` opens browser windows). Worked example: the audit in `docs/plans/post-0.35-enhancements.md`

## Issues & Observations

- `docs/issues.md` — bugs, performance, test gaps. Add with severity + proposed fix. Mark `FIXED` when resolved.
- `docs/observations.md` — patterns from operating the fleet. Add with date, evidence, insight. Never deleted.
- After significant changes: check if work produced a new observation or revealed a new issue. Append to the right file.

## Current State (as of 2026-09-28)

- **Throughput incident, resolved 2026-08-23:** fleet decode on `gpt-oss:120b` ran ~15% below baseline for two days (mean 69→59 tok/s). **It was not Ollama, llama.cpp, or herd** — a co-located client (the globally-installed `openclaw` CLI daemon, config `~/.openclaw/openclaw.json`) was calling `http://127.0.0.1:11434` directly, contributing **27% of Ollama's load that herd could not see**, including 77K-token re-prefills every 30 minutes. It arrived there by *fallback*, not by configuration: its cloud provider lost its API key and its model-fallback chain silently rerouted onto the local fleet. Resolved by stopping the daemon and disabling its launch agent (`launchctl bootout gui/$UID/ai.openclaw.gateway` + `launchctl disable`) — the project was no longer in use. **Removal helped, but did NOT restore the fleet — and the original claim here that it did was wrong.** It came from a 25-minute window (n=114, n=12 at conc=3) that was not representative. Measured over whole days: p25 went 43.0 (Aug 23) → 44–49 (Aug 24–Sep 1), never back to the 73.4 of Aug 21; conc=3 stayed flat at ~52 against 68.0 before. So openclaw was worth roughly 5 points of p25, not the ~30 the earlier note implied. **The Aug 22 step change (p25 73.4 → 43.1, conc=3 68.0 → 50.6) is therefore still unexplained** — see the OPEN issue in `docs/issues.md`. **Note `gemma3:27b` now loads at `CONTEXT 32768` as configured — the `FLEET_NUM_CTX_OVERRIDES` "bug" filed during triage was the bypassing client, not herd.** See the co-tenancy gotcha above, `docs/observations.md` (2026-08-23), and `docs/troubleshooting.md`.
- **Model catalog eval (2026-08-14):** added four *runnable* new models to `model_knowledge.py` — `qwen3-vl:32b` (vision, replaces gemma3 for image work), `qwen3.6:27b`, `qwen3.6:35b-a3b` (coding MoE), `muse-glimmer:30b` (agentic multimodal, needs Ollama ≥0.32.7, `is_thinking_model` updated to inflate its budget). **Do NOT re-attempt DeepSeek-V4-Flash or GLM-5 on this box** — both were evaluated and rejected: `deepseek_v4` isn't in any mlx-lm runtime, and GLM-5-4bit (419 GB) can't load on 512 GB (MLX mmap load-doubling ceiling — *not* fixable by raising `iogpu.wired_limit_mb`, which is a footgun that hard-locked the machine when tried). Full autopsy in `docs/observations.md` (2026-08-14). GLM tier stays on GLM-4.7-Flash / GLM-4.7 full.
- **Version:** `0.9.6` — **published** to PyPI + GitHub + Homebrew on 2026-09-29. **Desktop and web chat-client compatibility**, from a source read of 16 clients (Enchanted, Ollamac, Reins, Ollama's desktop app, AnythingLLM, Cherry Studio, Chatbox, Hollama, Page Assist): `POST /api/show`, `HEAD /` returning 200, full `/api/tags` field parity (OllamaKit decodes `modified_at`/`digest`/`details.*` as required non-null, so one missing key emptied the model picker), OpenAI-compatible `POST /v1/embeddings`, and opt-in CORS via `FLEET_CORS_ORIGINS` (**default off** — no middleware installed, so existing routers are byte-identical). `0.9.5` shipped the fixed-batch ONNX embedding fix, the `pre_warm` timeout/logging fix, `num_ctx_override_inert`, and the launchd agents. No dependency changes in either, so Homebrew bumps were version + sha256 only.
- **Autostart (2026-09-28):** two launchd agents own the fleet — `com.geeksaccelerator.ollama-herd.router` and `.node`, `RunAtLoad` + `KeepAlive{SuccessfulExit:false}`. Added after three unattended outages in a month (one 21 hours), all "nobody restarted herd after a reboot" — health checks cover a degraded fleet, not an absent one. **`pkill` is no longer a stop**; use `launchctl kickstart -k` / `bootout`. See § Autostart and `docs/examples/launchd/`.
- **Self-inflicted latency regression, resolved 2026-09-28:** `OLLAMA_CONTEXT_LENGTH=32768` (set on 09-22 to clear a model-load deadlock) overrode the router's per-request `num_ctx`, quartering gpt-oss's per-slot context and collapsing prefix-cache reuse — **TTFT 1.0s → 6.3s, total latency 5.3s → 10.5s, decode unchanged**, for six days. Reverted to `131072`; TTFT back to 1,021 ms. The deadlock does not recur, because per-model contexts via `FLEET_NUM_CTX_OVERRIDES` were always the right lever. Full autopsy in `docs/observations.md` (2026-09-28); see also the `OLLAMA_CONTEXT_LENGTH` gotcha above.
- **Prior (0.7.0):** published on PyPI + Homebrew tap (live since 2026-06-07). 0.7.0 ships a native text embedding server (fastembed, port 11439) that routes `nomic-embed-text` out of Ollama entirely — eliminating embed timeout contention under concurrent LLM load. Root cause: `OLLAMA_NUM_PARALLEL=2` means in-flight LLM inference holds both slots; embed requests queue indefinitely inside Ollama regardless of available hardware. Fix: fastembed serves `nomic-ai/nomic-embed-text-v1.5-Q` (130 MB int8 ONNX, 768 dims) on a dedicated port with zero inference slot contention. Verified on local fleet 2026-06-07: 573 embed requests/24h, avg 792ms, 0.0% error rate. Also ships: 4 new health checks (embed_error_rate, text_embedding_backend_missing, text_embedding_ollama_bypass, nomic_loaded_in_ollama), embed retry + trace recording on all paths, and dashboard "Node Models" section (renamed from "Request Queues") with cards for all model backends (Ollama, MLX, native fastembed, vision embedding).
- **Fleet:** Neons-Mac-Studio (512GB M3 Ultra) + Lucass-MacBook-Pro-2 (128GB M4 Max). Mac Studio runs two MLX servers: `mlx:Qwen3-Coder-Next-4bit` on :11440 for coding (no draft — Qwen3-Next's hybrid linear-attn architecture builds a non-trimmable `ArraysCache` and still hits mlx-lm#1081) + `mlx:Qwen3-Coder-30B-A3B-Instruct-4bit` on :11441 as dedicated compactor with `--draft-model mlx-community/Qwen3-1.7B-4bit --num-draft-tokens 4` for speculative decoding (~94 tok/s on M3 Ultra). Plus `gpt-oss:120b` via Ollama + `nomic-embed-text` via native fastembed server (:11439).
- **Ollama:** **`0.34.4`**. The version drifts on its own: `0.32.9 → 0.32.13 → 0.32.15 → 0.33.0 → 0.34.2 → 0.34.4`, every step a reboot or relaunch, none of them deliberate. **Re-check `curl -s localhost:11434/api/version` after ANY restart** — and be aware anything that can bind `:11434` first is served transparently (a stray `homebrew.mxcl.ollama` agent once won that race and served `0.16.3`; it is now `bootout`'d and `disable`d). Applying an `OLLAMA_*` env change requires quitting the **Electron parent**, not just `ollama serve`, and reaping orphaned `llama-server` (`ppid=1`) — see `docs/configuration-reference.md` § Ollama environment. Historical drift detail: `0.32.15` came from a 2026-08-21 21:57 reboot; `0.32.13` from a 2026-08-14 reboot; `0.32.9` was a manual upgrade from `0.32.5` that day to unlock version-gated models like `muse-glimmer:30b`, which requires ≥0.32.7; was `0.32.1` on 2026-07-17, from `0.24.0`). **Nuance on auto-update: point releases do NOT roll in while the app is *running* (the box sat at `0.32.5` while `0.32.9` was out), but the Mac app DOES self-update on relaunch/reboot** — a reboot silently moved `0.32.9`→`0.32.13`. So **a reboot can change your Ollama version; always re-check `curl -s localhost:11434/api/version` after one** (and note the app may pull a *prerelease* — verify against the GitHub releases page if the exact version matters). To upgrade on demand without waiting for a reboot, do a **manual Mac-app swap** (download `Ollama-darwin.zip` from the GitHub release, quit the app, `xattr -dr com.apple.quarantine`, replace `/Applications/Ollama.app`, relaunch) — **`brew upgrade ollama` does nothing** (stale formula). **Watch the launch race:** quitting the app can let another `ollama serve` (or herd-node's auto-restart) grab port 11434 first; verify the version after the swap, not a stale one. `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=10` (in `~/.zshrc` **and** `launchctl setenv` — Ollama is the Mac app launched by launchd, so it reads launchctl). **Include the Ollama version in soak checks** (`curl -s localhost:11434/api/version`) so this can't drift silently again. 0.32.1 brought glm 13.7→77.8 tok/s, gpt-oss 50.9→74.5, and working prefix caching — all via llama.cpp. **`OLLAMA_NEW_ENGINE` no longer exists** — Ollama deleted both CGO engines (`runner/ollamarunner`, `runner/llamarunner`) in PR #16031 (2026-05-29, −430K lines) and now shells out to upstream `llama-server` for every GGUF model; the env var is absent from `envconfig/config.go`. The Go engine was reborn as the MLX runner for Apple Silicon (`mlxrunner/`; moved out of `x/` on 2026-09-16, `2e036e7c`), selected **per-model** by `IsMLX()`, which is exactly `ModelFormat == "safetensors"` — not by any flag. It **decodes one request at a time**: `mlxrunner/runner.go` is a single loop that runs each request to completion, so `OLLAMA_NUM_PARALLEL` has no effect on it. `sched.go` also forces `numParallel=1` for non-completion models and the architectures in `OLLAMA_SERIAL_FAMILIES` (`server/serializers.py`). Herd mirrors all three per model in `decode_parallelism_for(node, model)` — **re-check that family list against `sched.go` on every Ollama upgrade**. Ollama `0.40` makes MLX the default on Apple Silicon. So there is no switch to flip: a GGUF model cannot use MLX. See `docs/plans/ollama-0.32-upgrade-and-mlx-evaluation.md`.
- **Skills:** 37 on ClawHub across `skills/`. They quote the test count and health-check count, and those quotes go stale silently — the previous maintenance note here grepped for `"1675 tests\|36 checks"`, numbers that appeared nowhere, while the skills actually said `1675 tests` / `16 automated checks`. So it matched nothing and the drift persisted. **Grep by shape, not by value:**
  ```bash
  grep -rnE "[0-9]{3,4} tests|[0-9]{2} (automated |health )?checks" skills/
  uv run pytest -q | tail -1                                                    # real test count
  grep -oE 'check_id="[^"]+"' src/fleet_manager/server/health_engine.py | sort -u | wc -l   # real check count
  ```
- **Health:** 42 distinct checks (count via `grep -oE 'check_id="[^"]+"' src/fleet_manager/server/health_engine.py | sort -u | wc -l`). Monitor: `curl http://localhost:11435/dashboard/api/health`

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
- **`brew tap` + `brew install` is no longer enough — Homebrew 6.x requires `brew trust` for third-party taps.** A fresh user who follows a tap-then-install README hits `Refusing to load formula … from untrusted tap` and gets nothing installed. This is invisible on a machine where the tap was added before the gate existed (the trust is already recorded), which is exactly how it survived a release: the 0.9.0 install "passed" locally, then failed the moment step 15 untapped and retapped. **Step 15 is what catches this class of bug — the untap is the whole point, not a formality.** Both READMEs (main repo and tap repo) now carry the `brew trust` line.
