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
uv run pytest                    # run all 2020 tests (~25s)
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
4. `uv run ruff check src/` — clean, then `./scripts/health.sh` — its dependency CVE audit (pip-audit over the locked set, all extras) must pass
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
    - Regenerate **every** `resource` block with Homebrew's own resolver — never by hand, and every release, not only when our deps change. Hand-curation is how the 0.11.2 formula shipped 25 known vulnerabilities in transitive pins nobody touched, and missed `requests` (which tiktoken needs) entirely:
      ```bash
      HOMEBREW_NO_AUTO_UPDATE=1 brew update-python-resources geeks-accelerator/ollama-herd/ollama-herd
      ```
      It applies a release cooldown, so a days-old fix can be skipped — hence the audit. **The same cooldown hides the release you just uploaded**, so run straight after `uv publish` it fails with "Unable to determine dependencies"; resolve from the local sdist instead (see the `0.11.4` entry under Current State):
    - Audit the result; it must report **no known vulnerabilities** (bump any block the cooldown held back by hand):
      ```bash
      grep -oE 'packages/[^"]+\.tar\.gz' Formula/ollama-herd.rb | sed -E 's#.*/##; s#\.tar\.gz##' \
        | grep -v '^ollama_herd-' | sed -E 's/-([0-9][^-]*)$/==\1/' > /tmp/brew-reqs.txt
      uvx pip-audit -r /tmp/brew-reqs.txt --no-deps --disable-pip
      ```
    - **If the new release adds a Rust-extension dep** (cryptography, pydantic-core, tiktoken, etc.): ensure `depends_on "rust" => :build` is present
13. **End-to-end install test (this step is non-negotiable — see "Brew tap testing" gotcha below):**
    ```bash
    brew uninstall ollama-herd  # if previously installed
    brew untap geeks-accelerator/ollama-herd  # forces a fresh tap clone
    brew tap geeks-accelerator/ollama-herd
    # Homebrew 6.0+ gate. Required when no trust record exists for the tap --
    # NOT "after a fresh tap": the record lives in ~/.homebrew/trust.json and
    # survives `brew untap`, so a retap alone leaves it trusted and the gate
    # untested. Add `brew untrust --tap geeks-accelerator/ollama-herd` first to
    # actually exercise it.
    brew trust geeks-accelerator/ollama-herd
    brew install ollama-herd  # ~25 min from a cold cache; ~3 min when Homebrew's
                              # build cache is warm (measured 2m54s for 0.11.0)
    /opt/homebrew/Cellar/ollama-herd/X.Y.Z/libexec/bin/python -c "import fleet_manager; from fleet_manager.server.app import create_app; print('ok')"
    /opt/homebrew/bin/herd --help
    ```
    All must succeed. If pip fails on a Rust-extension dep with "can't find Rust compiler" → add `depends_on "rust" => :build`. If pydantic complains about pydantic-core version → bump both together.
14. Commit + push the formula
15. One more `brew uninstall && brew untap && brew tap && brew install ollama-herd` against the pushed-to-GitHub formula to confirm a real fresh-user install works. **Add `brew untrust --tap geeks-accelerator/ollama-herd` before the install** or the trust gate is not exercised at all — see the Homebrew-trust gotcha below. Step 13 tests the formula you are about to publish (staged into `$(brew --repository)/Library/Taps/...`); step 15 tests the one you published.

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
    Ollama version as expected, backend/router task counts within ~5%.

    **Do NOT read p25-against-median here — that criterion was wrong and this
    check carried it.** The decode distribution is bimodal (see the gotcha), so a
    p25 far below the median is the *healthy* signature, not a degrading tail:
    measured 2026-10-22 on a fleet with zero errors and zero warnings, median
    76.9 and p25 41.9. By the old wording that reads as an alarm. **Read the
    fast-mode share instead** — the fraction of requests at >=70 tok/s:

    ```bash
    uv run python - <<'EOF'
    import sqlite3, os
    c=sqlite3.connect(os.path.expanduser("~/.fleet-manager/latency.db"))
    v=[1000.0*(ct-1)/(l-t) for l,t,ct in c.execute(
      "SELECT latency_ms,time_to_first_token_ms,completion_tokens FROM request_traces "
      "WHERE status='completed' AND timestamp>strftime('%s','now')-86400 "
      "AND completion_tokens>20 AND latency_ms>time_to_first_token_ms")]
    f=sum(1 for x in v if x>=70)
    print(f"n={len(v)} fast={100*f/len(v):.1f}% (healthy 43-70%)")
    EOF
    ```

    Healthy is **43-70%** across full hours. That band is two measurements, not
    one: 47-61% on 2026-10-13 (n=6,554) and 43.0-69.9% on 2026-10-22 (n=7,923,
    23 full hours) on a fleet whose logs carried zero errors and zero warnings.
    The first band was quoted here from a single day and was narrower than
    reality — widen it again rather than treating an excursion as a regression,
    and confirm a real one by concurrency, never by the median.

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

**A back-to-back kickstart of both agents logs exactly two WARNINGs, and they are
the restart, not a fault.** Measured 2026-10-22: `herd-node.jsonl` gets
`Failed to send drain signal to router: All connection attempts failed` — the old
node's SIGTERM drain handler firing correctly at a router that is also restarting —
and `herd.jsonl` gets `No nodes for model=... fallbacks=[]` for whatever request
arrives before the node re-registers. Both are self-explaining and clear within
seconds. **This matters because soak notes claim "zero warnings"**, so a check run
too soon after a deploy will find two and look like a regression; count from after
the restart settles, or kickstart the node first so its drain reaches a live router.

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
- **`OLLAMA_CONTEXT_LENGTH` overrides the router's per-request `num_ctx` — pin it to the LARGEST per-slot context the fleet needs, never the smallest.** Ollama launches llama-server with `-c NumCtx x OLLAMA_NUM_PARALLEL`, and this var sets `NumCtx`. Despite being documented as "unless otherwise specified", setting it to `32768` on 2026-09-22 quartered gpt-oss's per-slot context (131072 → 32768) even though the router injects `num_ctx=131072` on every request — the router logged the conflict and lost every time (`Context protection: client wants num_ctx=131072 but ... only has context=32768`). Prefix-cache reuse collapsed (**5,772 → 770** cache hits in the Ollama log), so every request re-prefilled its full ~1,440-token prompt: **TTFT 1.0s → 6.3s, total latency 5.3s → 10.5s**, on the model serving 99% of traffic. **Decode was completely unaffected (conc=1: 76.2 → 76.1)**, which is why it hid for six days — every throughput number looked fine while mean latency doubled. Correct value here is `131072`; per-model contexts belong in `FLEET_NUM_CTX_OVERRIDES`, which the router injects per request and which works (gpt-oss `-c 524288 -np 4` and gemma3 `-c 131072 -np 4` coexist). **Restarting Ollama to apply it means killing the Electron parent** (`Ollama.app/Contents/MacOS/Ollama`), not just `ollama serve` — the parent inherited the old env at its own launch and re-passes it to every child; also `pkill` orphaned `llama-server` processes (`ppid=1`) or they keep serving at the old context. **Verify with the launch args, not `ollama ps`** — but not for the reason this note used to give. Measured 2026-10-11 on `0.40.2` across two models whose contexts differ 4x: `/api/ps` `context_length` is the **per-slot** value (gpt-oss `-c 524288 -np 4` reports 131072; gemma3 `-c 131072 -np 4` reports 32768), *not* the total as previously claimed here. Read the launch args because they show `-c` **and** `-np`, which is what tells you total KV allocation — `context_length` alone gives the window and nothing about how many slots multiply it: `ps -Ao args | grep llama-server | grep -oE '\-c [0-9]+ \-np [0-9]+'`. Full case study: `docs/observations.md` (2026-09-28).
- **An absent `num_ctx` is not neutral — Ollama fills it from `OLLAMA_CONTEXT_LENGTH` and reloads the model when that differs from what is resident.** This is why `FLEET_NUM_CTX_OVERRIDES` could never hold: once a model landed at exactly its configured context, the router correctly concluded there was nothing to inject and sent the request bare, Ollama substituted 131072, and the model reloaded at the larger window — **reverting the override in a single request, and evicting its co-resident peer on the way**. Reproduced 2026-10-09: `ollama stop gemma3:27b` + one request via `:11435` gave `-c 131072 -np 4` (32768/slot, correct); one more request gave `-c 524288 -np 4` (131072/slot) and evicted `gpt-oss:120b`. Three paths in `_apply_context_protection` left the field absent believing it meant "keep what is loaded" — the branch written to avoid a reload was causing one. Fixed by `StreamingProxy._pin_resident_num_ctx`: when the resident context is known, **send it**, which cannot trigger a reload because it is the value Ollama is already running. herd still never forces a *shrink* onto a hot model; it pins the model where it is instead of letting the env var decide. `passthrough` mode and an unknown resident context are untouched. The `stripped` protection event is now `pinned` (the check counts both names so the card doesn't read zero for a day after a deploy). **Note this supersedes a conclusion, not a gap:** the 2026-09-29 investigation measured the mechanism exactly — 199 of 203 gemma3 requests reached Ollama with no `num_ctx` — then framed it as "enforce and pay a reload, or accept a larger context" and chose to fix only the reporting. That trade never existed; the fleet was already paying the reload *and* running the wrong context. See `docs/issues.md` and `docs/observations.md` (2026-10-09).
- **`kv_cache_bloat` guessed weights the node was already reporting, and invented 12 GB.** It estimated weights as `parameter_count x 0.5 bytes` — which is Q4_0, while Ollama's common default is Q4_K_M at ~0.64 — so `gemma3:27b` was guessed at 13.7 GB against a true **17.4 GB** and 3.7 GB of *weights* were reported as KV cache, carrying a 1.39x model over the 1.5x threshold. The node reports true per-model on-disk size in `models_available_sizes` straight from `/api/tags`; with it, nothing on this fleet fires. The check also asserted a single cause (`OLLAMA_NUM_PARALLEL` too high) when KV is `context x parallel`, and prescribed a node-wide parallelism drop that would have recovered *less* than the per-model reload actually available while slowing every co-resident model on a bandwidth-bound box. Its savings arithmetic was impossible too: `kv / 8` promised beside a 4 → 2 recommendation. **And `context x parallel` bounds KV rather than predicting it** — applying gemma3's 32768 was predicted to return ~6.4 GB and returned nothing, because `gemma3.attention.sliding_window = 1024` over 62 blocks caps most layers' KV at the local window regardless of context. The figure is now `recoverable_gb_max` ("at most ~N GB, and possibly none"). **Generalized question for any new check: is it computing something it could instead be told?** And when it must estimate, bias toward losing sensitivity rather than inventing findings — understating weights manufactures overhead, overstating them only costs detection.
- **Measured prompt size is not sufficient grounds to shrink a model's context, and `context_waste` will tell you otherwise.** The check's *measurement* is correct (`prompt_tokens` is the full prompt — see the correction in the co-tenancy gotcha above), but its *inference* is not. On 2026-09-22 gpt-oss:120b's per-slot context was cut 131072 → 32768 while its p99 prompt was ~1.4K tokens — fitting 23× over by the check's own arithmetic — and prefix-cache reuse collapsed anyway (5,772 → 770 hits), TTFT went 1.0s → 6.3s for six days on the model serving 99% of traffic, with **decode throughput completely unchanged**. The router kept requesting 131072 against a 32768-resident model and losing every time, so the operative hazard looks like the requested/resident *mismatch* rather than the absolute size — but that mechanism is inferred from logs, not proven, which is itself the reason not to act on prompt-size arithmetic alone. **Since 0.9.7 the check names any model pinned in `FLEET_NUM_CTX_OVERRIDES` as deliberate and recommends no change for it** (severity drops to INFO when every oversized model is pinned, because a standing WARNING with no available action is how a board stops being read). **And `context_optimizer._check_and_optimize` no longer overwrites an override herd did not set** — it tracks its own (`_auto_set`) and leaves the rest alone. That was a loaded gun: with the "Auto-Calculate Context" toggle on, the deliberate `gpt-oss:120b=131072` override set to *end* the regression scored as 8× oversized and would have been reset to 16384 with an Ollama restart queued, within 5 minutes, from the same trace data. Note the asymmetry that made it a bug rather than a decision: `_auto_initialize_overrides` already skipped models with an existing override; only the periodic path did not. If you do change a context, **verify prefix-cache hits and TTFT after the restart, not decode throughput** — decode is the one metric a bad context change leaves untouched. **And "cannot apply" means mismatch, not equality (fixed 2026-10-08):** the injection gate is `override <= resident`, which is right for deciding whether to inject but wrong as a warning condition — at equality the override is *in effect*. (That gate is still right about injecting, but the request must not then go out with `num_ctx` absent — see the absent-`num_ctx` gotcha above, which is why the override kept reverting.) It warned about `gemma3:27b` resident at exactly its configured 32768, with remediation for a non-problem. `_check_num_ctx_override_inert` was always correct (`have != want`), which is why the card and the log disagreed. **And since 2026-10-09 the gap itself is visible: `num_ctx_unmanaged`** flags models that served traffic with *no* override, because herd injects `num_ctx` only for configured models and everything else gets Ollama's GPU-memory heuristic — which on a ≥47 GiB node picks `defaultNumCtx = 262144` with **no `numParallel` term** while Ollama launches with `-c NumCtx x numParallel` (ollama#14116). Measured 2026-10-09: 5 override entries covering 9 of 30 models, so 21 were unmanaged. Traffic-gated, and it excludes native-embedding models via `is_text_embedding_model` rather than `model_has_capability` — the latter is presence-only and native-server models are absent from Ollama's metadata, so it returned False for exactly the models needing exclusion and the check fired on `nomic-embed-text` with 2,829 requests on its first live run. **Traces now also carry `estimated_tokens`** beside `prompt_tokens` (what the router thought vs what the backend evaluated), and the long-standing overflow detection in `routing.py` is finally *recorded* rather than only logged and header'd. A truncation threshold is deliberately NOT built: measured pairs are 5,685/4,288 and 10/73, so the estimator runs high on long prompts and low on short ones where the chat template dominates — a single ratio would be wrong both ways. See `docs/plans/context-window-blindness.md`.
- **Two hot models cost each other ~24% of decode, and raising concurrency does not recover it.** Measured 2026-10-05 on one M3 Ultra with `gpt-oss:120b` and `gemma3:27b` both resident and serving (~12,300 + ~7,600 requests/24 h): gemma3 ran **6.2 tok/s with gpt-oss active vs 7.7 tok/s alone**, TTFT 2,619 vs 2,175 ms. That is an accepted trade here, not a fault. **Do not answer it by raising queue concurrency** — herd's per-model concurrency is *derived* from `OLLAMA_NUM_PARALLEL` via `decode_parallelism_for`, so raising it only relocates the queue from herd (visible, rejectable) into Ollama (neither). Each model already gets its own `-np 4` from a separate `llama-server`, so neither is slot-starved. The box is **bandwidth**-bound: single-stream gemma3 is 22.5 tok/s and four slots deliver 27.1, i.e. **1.2x of one slot** — if admission were the limit that would approach 4x. Raising `OLLAMA_NUM_PARALLEL` lowers per-request TTFT while each stream decodes proportionally slower (same batch wall-clock) and doubles KV via `-c NumCtx x numParallel`. **The cheap discriminator before tuning anything: compare single-stream decode against delivered tok/s at full load.** And never derive "concurrency" from overlapping trace intervals — that measures occupancy including queue wait, not parallel decode, and it inverted this very conclusion on the first pass. See `docs/observations.md` (2026-10-05).
- **herd reports its own process memory, and `peak` is the field that matters.** Heartbeats carry *system* memory, which on this box is dominated by Ollama's ~91 GB plus two `mlx_lm.server` children at 17 GB each — so a 20 GB leak inside `herd-node` is invisible in it, which is exactly why a 28 GB leak on two devices left no diagnosable history on either. `HeartbeatPayload.process_memory` (from `common/process_memory.py`) now carries the agent's own `phys_footprint`, its **`phys_footprint_peak`**, and labelled children. **Read peak, not current:** ONNX Runtime keeps the high-water mark of the largest run a process ever does, so one oversized request raises the floor permanently and current usage then reads innocent. **Footprint, not RSS** — `psutil.memory_full_info()` is `AccessDenied` on macOS without root (same wall `backend_clients` hit), so this shells out to `/usr/bin/footprint -p <pid>`, which works as the ordinary user at ~50 ms and is therefore sampled on a 60 s TTL, not per heartbeat. The embedding servers are `asyncio.Task`s *inside* the agent, not subprocesses, so their arenas are charged to the agent — children (mlx, transcription) are reported separately and excluded from the `herd_process_memory` threshold, because 17 GB of model weights would trip anything useful. Children are labelled from **argv**: the transcription server's process name is just `Python`. Three visibility surfaces on purpose — the heartbeat log line (`self=…GB (peak …)`) for greppable history, `/fleet/status` unconditionally so the *trend* is visible before it is a problem, and the check at 8 GB WARNING / 16 GB CRITICAL. Measured ceiling with both embedding models warm: **3.684 GB** (0.112 GB at start), matching the 3.71 GB measured by hand.
- **Critical memory pressure withholds cold loads; it does not stop service — and on macOS the signal was dead until 2026-10-02.** `_get_memory_pressure_darwin` ran `memory_pressure -Q` and searched for "critical"/"warn"; `-Q` prints only a total and `System-wide memory free percentage: N%`, so it returned NORMAL unconditionally and **everything built on the signal was dead code on every Mac** — the scorer's elimination and the `memory_pressure` health check had never once fired. A Mac mini reported `normal` at 50.9/51.2 GB swap with load 340. Now reads `sysctl -n kern.memorystatus_vm_pressure_level` (**1 = normal, 2 = warn, 4 = critical — an enum, not a scale; there is no 3**), unknown values failing open to NORMAL. **The elimination it gates was also the wrong shape**, so both were changed together: blanket elimination froze a one-node fleet into 503s while freeing nothing, because the memory is Ollama's resident weights and not herd's queue — refusing a request unloads no model. It now eliminates a node only for a model that is **not resident** there (`scorer._eliminate`), does not check pressure at all in `score_loaded_models` (every model there is already hot by contract), and stays unconditional in `routing._pick_pull_node` (whose every candidate is by definition a cold load). Residency is `serializers.model_resident_on_node` — one definition shared with the preloader so they cannot drift. See `docs/issues.md`.
- **`mlx:` prefix in `FLEET_ANTHROPIC_MODEL_MAP` routes to `mlx_lm.server`** (the "bypass the 3-model cap" rationale is dead — see the cap gotcha above; and a 2026-07-17 note here claimed **Ollama is FASTER than our MLX** at glm 77.8 tok/s vs 59 — **that is not reproducible: re-measured 2026-10-11 on `0.40.2`, GLM via Ollama has 30 completions against 235 timeouts and 12 failures all-time (worst 87 minutes), while MLX has 27 completions and 1 failure and answered in 2.93 s for 84 tokens.** Ollama's GLM also emits ~3,268 output tokens where MLX emits ~556. **MLX is the only usable GLM path on this box**, so `mlx:` keeps earning its place for this model; attempting the comparison also wedged Ollama into serving `/api/version` while unable to start any runner, needing the documented full restart. See `docs/issues.md` — see `docs/issues/ollama-native-mlx-runner.md`) — any mapped value starting with `mlx:` routes through an independent `mlx_lm.server` subprocess instead of Ollama. Setup: run `./scripts/setup-mlx.sh` (installs pinned mlx-lm **0.32.2**; native `--kv-bits`/`--quantized-kv-start` since 0.32.0. **The supervisor passes `--quantized-kv-start 5000`, mlx-lm's own default — it used to pass `0`, and that broke output**: quantizing the KV of the prompt itself left Qwen3-Coder unable to read it, answering "weather in Paris?" by inventing a task about four other cities — 0/5 correct at 0 against 8/8 at 5000 on an isolated server, measured 2026-10-22. The `0` came from an April benchmark that measured TTFT only. **And re-run the script after any pin change** — this fleet ran a patched 0.31.3 for three weeks after the pin moved to 0.32.0, because a `git pull` moves nothing; `uv tool list | grep mlx-lm` shows what is really installed. 0.32.2 measured +90% decode on the coder (65→124 tok/s) and +44% on GLM (45→64)), then set `FLEET_MLX_ENABLED=true` on the router and `FLEET_NODE_MLX_ENABLED=true` + `FLEET_NODE_MLX_SERVERS='[{"model":"...","port":11440}]'` on the node (the single MLX config surface — a one-entry array is a single-model deploy). The pin is exact: bump it only after a soak — this subsystem has broken on mlx-lm upgrades before. See `docs/guides/mlx-setup.md`.
- **Run multiple MLX models concurrently via `FLEET_NODE_MLX_SERVERS`** — JSON list of `{model, port, kv_bits}` entries, one `mlx_lm.server` subprocess per entry. Memory-pressure gate (`FLEET_NODE_MLX_MEMORY_HEADROOM_GB`) refuses to start a server that wouldn't fit; surfaces skip reason in heartbeat as `memory_blocked` status. Set `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` for multi-node LAN aggregation. Dashboard renders per-server health table inside each node card. Canonical use case: dedicate a smaller MLX model (e.g. `Qwen3-Coder-30B-A3B-Instruct-4bit`) to context compaction via `FLEET_CONTEXT_COMPACTION_MODEL=mlx:...` so summarization has its own prompt cache and doesn't compete for the main model's slot. **Caveat: `--draft-model` disables continuous batching entirely** — `mlx_lm/server.py` sets `is_batchable = draft_model is None`, so a speculative-decoding server serialises every request. **Measured 2026-07-19: keep it.** Only 84 of 2,813 mlx requests ever overlapped another (3%), so compaction is effectively serial and speculative decoding is the right trade. It inverts if the fleet ever serves several concurrent coding sessions — check the overlap rate before assuming. See `docs/issues.md`. See `docs/guides/mlx-setup.md` § "Multi-server setup".
- **Run one MLX model across multiple Macs** — add `backend`/`hosts`/`hostfile`/`pipeline` to a `FLEET_NODE_MLX_SERVERS` entry and the supervisor wraps `mlx_lm.server` in `mlx.launch` (rank-0 serves HTTP + broadcasts to peer ranks; the herd sees one endpoint, unchanged). `backend:"ring"` works over plain LAN today (`hosts:"<peer-ips>"` — the node prepends its own IP; use `pipeline:true` for memory pooling, since tensor parallelism over TCP is too chatty). `backend:"jaccl"` needs Thunderbolt 5 + macOS 26.2 for tensor-parallel speedup. `MLX_METAL_FAST_SYNCH=1` is auto-passed via `mlx.launch --env` (never the subprocess env — remote ranks need it, missing it = 5–6× slower). The whole-model memory gate is skipped for distributed servers (each node holds only a shard). Tensor parallelism is bottlenecked by the smallest node, so asymmetric fleets (e.g. 512GB + 128GB) should use `pipeline` for memory expansion, not tensor. See `docs/plans/distributed-mlx-inference.md` and `docs/research/apple-distributed-mlx-jaccl-2026.md`.
- **Brew tap testing — a tap that's only ever been bumped (version + sha256) has been *described*, not *tested*.** Homebrew runs `pip install --no-binary :all:` which forces source builds for every resource. Any Rust-extension Python dep (`pydantic-core`, `cryptography`, `tiktoken`) needs `depends_on "rust" => :build` in the formula or the install fails at "can't find Rust compiler" while bootstrapping `maturin`. Any `pyproject.toml` dep that isn't listed as a `resource` block also breaks the install (Homebrew's `virtualenv_install_with_resources` doesn't transparently pull from PyPI for missing deps). The 0.5.x formula was broken throughout for both reasons; nobody noticed because nobody actually ran `brew install`. **Step 13 of the release checklist is non-negotiable** — uninstall + untap + retap + install + import sanity check, every time, before considering a release done. See the 0.6.0-formula-fix observation in `docs/observations.md`.
- **A Homebrew formula you bump every release can still hold vulnerable pins — regenerate its resources, never curate them.** The 0.11.2 formula shipped **25 known vulnerabilities across 9 packages** (Starlette, python-multipart, zeroconf among them) while three releases in a row noted "version + sha256 only" — true of herd's own deps, irrelevant to the 39 transitive pins under them. Hand-curation had also dropped `requests`, which tiktoken needs, so brew installs most likely counted tokens as `len(text) // 4` without saying so. Step 12 of the checklist now runs `brew update-python-resources` and audits the result with pip-audit. Its release cooldown can skip a fix only days old, so the audit is not optional. **Related: fastembed 0.9 caps `pillow<13`**, holding every PyPI `[embedding]` install on a Pillow with 28 advisories ([qdrant/fastembed#829](https://github.com/qdrant/fastembed/issues/829)). This repo runs 13 through a `[tool.uv]` override (removal condition in `docs/issues.md`); what protects users is that the vision server decodes only JPEG/PNG/WebP (`embedding_server.ALLOWED_IMAGE_FORMATS`). `./scripts/health.sh` fails on any known vulnerability in the lock. Raise a `pyproject.toml` floor only when herd reaches the vulnerable code — json-repair's advisory needs a schema herd never passes, so it kept its floor. See `docs/observations.md` (2026-10-22).
- **We claim macOS, Linux and Windows, and the whole suite runs on one of them — `herd-node` had never started on Windows.** `NodeAgent.start()` called POSIX-only `loop.add_signal_handler` before the heartbeat loop, so on Windows' `ProactorEventLoop` it raised `NotImplementedError`, `start()` died, and the router showed an empty fleet. Every version shipped that way until 2026-10-22, past a suite of nineteen hundred tests, and **an outside reporter found it** ([#8](https://github.com/geeks-accelerator/ollama-herd/issues/8)) — with the diagnosis and the patch, which is better than we did. Fixed in `_install_drain_handlers`, which guards on `win32` and **logs the consequence** (a stopped Windows node does not deregister; the router drops it when the heartbeat goes stale) rather than degrading quietly. **The transferable part is why it survived so long:** the same file already branches on `win32` twice for Popen flags, so it *reads* as Windows-aware, and `mlx_supervisor`'s `os.killpg`/`SIGKILL` are POSIX-only but unreachable there (behind `mlx_enabled`, default `False`, Apple-Silicon-only). One unguarded call in a file full of guarded ones is invisible to review and to a suite that only ever runs on macOS. **So: a documented capability nobody executes is a claim, not a feature** — the same shape as step 15's Homebrew trust gate, which four releases "tested" without ever firing it. When touching startup paths, grep for the POSIX-only set (`add_signal_handler`, `os.killpg`, `os.getuid`, `start_new_session`, `signal.SIGKILL`, `fcntl`, `pwd`) and ask which of them run before the first heartbeat. Mocking `sys.platform` proves a guard exists, not that the agent starts; the honest fix is a CI matrix on three platforms asserting one heartbeat registers, logged as an open gap in `docs/issues.md`.
- **`uv run pytest` reads your shell's `FLEET_*` vars, so on a machine that runs a herd the suite tests *your* configuration.** Fixed 2026-10-22 by an autouse `_hermetic_env` fixture in `tests/conftest.py`, and the reason to know about it anyway: **a test failing here and passing in CI is first a question about the environment, not the code.** Three tests went red on this fleet because `~/.zshrc` exports `FLEET_ANTHROPIC_MODEL_MAP` and `_check_mapped_model_missing` reads it straight from `os.environ` — `mapped_model_missing` fired CRITICAL inside `test_empty_fleet` (health score 80, not 100) and `/v1/responses` answered 503 instead of the empty-fleet 404. The code was correct. Both settings classes read the whole namespace through pydantic (`FLEET_NODE_` starts with `FLEET_`, so one prefix covers both), and six places read `os.environ` directly — enumerate them with `grep -rnoE '(os\.environ\.get|os\.getenv)\(["'"'"'][A-Z_]+' src/` rather than from memory. **`CLOUD_FLEET_TOKEN` is cleared too**: it is a real platform credential when the operator has one, and a test that can read it can leak it. **The shape of the mistake is the part worth keeping.** Three modules had already hit this and each wrote its own fixture clearing the variables its author remembered — six telemetry keys in one, a single `FLEET_NODE_NODE_ID` in another, `FLEET_TEST_*` in a third — so each fix guaranteed only that *that* module was safe and left the next caller exposed. A per-site patch for a process-wide condition is not a fix, it is a sample. Same lesson as `resident_context_for`'s centralisation: when several callers need one answer, one definition or they drift. `tests/test_hermetic_env.py` pins it, because the failure it prevents is invisible to whoever has a clean shell — which is everyone in CI. **Check it both ways when touching test setup:** the suite must pass with the operator's vars present *and* stripped (`env $(env | grep -oE '^FLEET_[A-Z_]+' | sed 's/^/-u /' | tr '\n' ' ') uv run pytest -q`).
- **Scanning JSONL logs for errors — use the patterns the formatter actually writes.** `JSONLFormatter` in `common/logging_config.py` calls `json.dumps(...)` which by default emits `"level": "ERROR"` (space after the colon). A grep for `'"level":"ERROR"'` (no space) silently matches **zero** lines and looks like a clean fleet — the 2026-05-15 incident sat undetected for ~4 days because of this exact mistake during routine soak checks. Correct patterns: `grep '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl` and `grep '"level": "WARNING"' ...`. For a multi-day audit across both processes: `for f in ~/.fleet-manager/logs/herd*.jsonl*; do echo -n "$f: "; grep -c '"level": "ERROR"' "$f"; done`. Better yet, parse with Python — counts will never depend on whitespace and you can group by message/logger. The two log files (`herd.jsonl` and `herd-node.jsonl`) are separated since 2026-05-15 to prevent cross-process rotation races — both need scanning. **And parse the timestamp as UTC.** `ts` is ISO-8601 *with* a `+00:00` offset (e.g. `2026-07-17T10:22:21.109982+00:00`), but the fleet runs in local time (UTC-8). `time.mktime(time.strptime(ts[:19], ...))` silently treats that UTC string as **local**, producing timestamps **8 hours in the future** — so a `ts >= now - 180` "last 3 minutes" filter matches **the entire log**. This produced two false alarms on 2026-07-17: first a false-clean (`0 errors` — wrong field names), then a false-alarm (`218 warnings in 3 minutes` that were really the whole log, nearly blocking a resume). **Use `datetime.fromisoformat(ts).timestamp()`** — it honours the offset. Fields are `ts`/`level`/`logger`/`msg` (NOT `timestamp`/`message`). Sanity-check any window query by also printing the total line count: if "last 3 min" returns thousands of lines, your clock math is wrong.
- **Trace DB write failures are an observability black hole if you only look at the dashboard.** `record_trace` is fire-and-forget — when `latency.db` writes start failing (`database is locked` under WAL contention), requests still succeed end-to-end but the dashboard's `reqs_24h` drops to 0 because it queries the same DB that's not getting writes. Since 0.6.2 the trace store has five layers of defense — listed from outermost to innermost: (1) `trace_store_write_failures` health check surfaces sustained failures within 5 minutes regardless of cause, (2) periodic `PRAGMA wal_checkpoint(PASSIVE)` task in `app.py` lifespan fires every 10s on each store's writer, (3) dedicated `_read_db` connection per store with `PRAGMA query_only=1` so dashboard reads run on a separate aiosqlite thread and don't pin the writer's WAL checkpoint barrier, (4) retry-on-locked loop in `record_trace` (3 attempts, 200ms→2s backoff) absorbs short contention, (5) `PRAGMA busy_timeout=30000` + `PRAGMA wal_autocheckpoint=100` on every connection.  If the check still fires despite all five, the most common root causes are: disk-full on the data dir, a stale `latency.db-shm`/`-wal` from a crashed previous process, or a slow query path in the codebase that needs an index.  See 2026-05-15 and 2026-05-16 observations.

- **herd assumes it is the ONLY client of its backends — a co-tenant on `:11434` silently breaks the scheduling math, and every herd-side metric still looks healthy.** Every scoring signal (queue depth, free slots, session affinity, context fit) is derived from what herd itself dispatched, and `QueueManager` caps concurrency per model to match what the backend admits (`decode_parallelism_for(node, model)`). It is **enforced since 2026-10-02**: a worker holds its slot until the request leaves the queue. Before that it handed off an unconsumed stream and took the next request immediately, so the cap bounded nothing. See the queue-concurrency entry in `docs/issues.md`. A second process talking straight to Ollama fills the same slots, so herd's "concurrency 2" is really occupancy 3–4 — its cap now oversubscribes. On 2026-08-23 a co-tenant was found contributing 27% of Ollama's load invisibly, alongside a 15% fleet throughput drop, with zero errors, zero retries and a clean dashboard; it was misdiagnosed for hours as an Ollama/llama.cpp regression (it was not — 0.32.13's and 0.32.15's `llama-server` benchmark identically on the real model). **Since 0.9.7 a `backend_bypass_clients` health check finds this automatically** — the node probes who holds established connections to its Ollama (`node/backend_clients.py`, `lsof`-based because `psutil.net_connections()` is `AccessDenied` on macOS without root) and reports any non-herd process in the heartbeat; the router fires WARNING naming the pid, process and full argv. Detects both a **local** co-tenant (by pid + full argv) and an **off-box** one (by peer address, `pid=0` — its own socket is on its machine, so the node sees only Ollama's server end; this path is live wherever Ollama binds beyond loopback, which is its `*:11434` default). The router is excluded by address, since it legitimately proxies to every node's Ollama — without that exclusion every node would flag the router. Verified end-to-end on the live fleet 2026-10-02: fires within ~60 s of a foreign client appearing, clears ~48 s after it leaves, and the router is not flagged. An empty list means "none seen" *or* "could not look" — deliberately indistinguishable, so a blind node is a missed detection, never a false alarm. **Manual reconciliation remains the cross-check:** `grep -c "new prompt, n_ctx_slot" ~/.ollama/logs/server.log` vs `sqlite3 ~/.fleet-manager/latency.db "SELECT date(timestamp,'unixepoch'), COUNT(*) FROM request_traces GROUP BY 1"` — **only `backend > router` matters** (work herd never dispatched); `router > backend` is a benign attribution artifact, since backend lines are dated by the nearest preceding `[GIN]` line and logs rotate. (A previous version of this note also said fully-cached prompts may emit no line; on 0.34.4 they **do** emit one — both runs of the identical-prompt test logged `new prompt … task.n_tokens = 4074` — which is why 674 traces reconciled against 679 backend lines over the same 2.4 h window, 0.7% apart.) It is a rough tripwire, not an audit: a sustained tens-of-percent backend excess is the alarm, small gaps either way are noise — confirm with `lsof` before concluding. Find it with `lsof -nP -iTCP:11434 -sTCP:ESTABLISHED` (herd uses IPv6 `::1`; most other tools use IPv4 `127.0.0.1`), and resolve the real binary via `lsof -p <pid> | awk '$4=="txt"{print $NF}'` — a Node daemon's `ps` name is just `process.title` and can match an unrelated project folder. Two cheaper tells: `ollama ps` showing a larger `CONTEXT` than `FLEET_NUM_CTX_OVERRIDES` sets (a bypassing request skips `num_ctx` resolution), and oversized `task.n_tokens` in the server log that never appear in traces — a bypassing client's request is absent from `request_traces` *entirely*, which is the tell. **Correction (2026-10-02): this gotcha previously claimed `prompt_tokens` records only cache misses, so a 77K request "looks like 2K" in traces. That is wrong.** `prompt_eval_count` is the FULL prompt length: on Ollama 0.34.4 an identical prompt resent reported `prompt_eval_count=4074` both times while `prompt_eval_duration` fell 2.235 s → 0.021 s, so the prefix cache was definitively hit and the count did not move. The original note conflated "absent from traces" (a bypasser's request) with "deflated in traces" (herd's own). Reproduce with the two-identical-requests test before trusting either claim again. This matters beyond diagnosis: it means trace `prompt_tokens` **is** a valid basis for context sizing, which is what `context_waste` and `context_optimizer` compute from — see the context-sizing gotcha below for why that still isn't sufficient grounds to shrink a model. **And measure the tail: median fell 6% while p25 fell 44%** — mean/median dashboards hide this. Fix: point the other client at `:11435`. Beware tools that arrive by *fallback* rather than configuration — a cloud provider losing its API key can silently redirect a whole workload onto the fleet. Full case study: `docs/observations.md` (2026-08-23); diagnostic runbook: `docs/troubleshooting.md`.
- **Every Ollama-loaded model carries up to 8 GiB of llama.cpp prompt cache that nobody reports.** Ollama shells out to `llama-server`, whose host-RAM prompt cache defaults to `--cache-ram 8192` MiB *per process*; Ollama never sets it, `/api/ps` excludes it, and herd's resident estimates do too (gemma3:27b: 17.7 GB reported, 29 GB real, 7.8 GB of it cache with 0 hits in 100 lookups). The bundled `llama-server` reads `LLAMA_ARG_CACHE_RAM` and inherits Ollama's environment. Check the hit rate before changing it: `grep -c "found better prompt"` vs `grep -c "looking for better prompt"` in `~/.ollama/logs/server.log`. **Persisting it needs two places**, because two things start Ollama at login: the env LaunchAgent (`docs/examples/launchd/com.geeksaccelerator.ollama-env.plist`) for the app, AND the node plist's `EnvironmentVariables`, because `herd-node` spawns `ollama serve` with its *own* environment when Ollama is down at boot. Set on the Mac mini since 2026-10-04 (`=0`). See `docs/configuration-reference.md` § Ollama environment.
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
| `server/health_engine.py` | 49 health checks (offline, degraded, memory, KV bloat, context waste, thrashing, timeouts, errors, retries, disconnects, streams, version, protection, zombies, connection failures, priority models) |
| `server/context_optimizer.py` | Dynamic num_ctx: analyzes token usage, auto-calculates optimal context, queues Ollama restarts via heartbeat commands |
| `server/benchmark_engine.py` | Benchmark core: fleet discovery, multimodal request gen (LLM + embed + image), report building |
| `server/benchmark_runner.py` | Server-side runner: smart mode (fill memory from disk/catalog), progress tracking, model type selection |
| `server/model_knowledge.py` | 40+ model catalog with benchmarks, RAM, categories (including VISION), thinking detection |
| `node/agent.py` | Main loop: mDNS discovery, heartbeat, Ollama auto-start/restart, LAN proxy, drain |
| `node/capacity_learner.py` | 168-slot weekly behavioral model, availability score, dynamic memory ceiling |
| `node/embedding_models.py` | Vision embedding model registry, download, ONNX inference (DINOv2, SigLIP, CLIP) |
| `node/embedding_server.py` | FastAPI server for vision embeddings on :11438. Decodes **only JPEG/PNG/WebP** (`ALLOWED_IMAGE_FORMATS`) — the server takes unauthenticated LAN bytes, and Pillow's other decoders carry most of its advisories |
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
| `common/process_memory.py` | herd's own process memory — `phys_footprint` + **peak** via `/usr/bin/footprint` (psutil's is `AccessDenied` on macOS), children labelled by argv. The number whose absence made a 28 GB leak undiagnosable on two devices |
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
- `docs/listings.md` — curated lists that include herd (awesome-mlx, Awesome-LLMOps, awesome-ai-gateway), what each entry says, and submission rules learned. **awesome-selfhosted bans LLM-generated submissions** — a resubmission must be filed by a human

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

## Current State (as of 2026-10-24)

- **Version: `0.11.4` — published** to PyPI + GitHub + Homebrew on 2026-10-24. Patch over `0.11.3`, all fixes, from the October upstream-changes plan (`docs/plans/upstream-changes-2026-10.md`). **A truncated response now reaches the client as a failure on every surface** — OpenAI an in-stream error with no `[DONE]`, Responses `response.failed`, Anthropic `event: error`, non-streaming a **502** — where 0.11.3 told Claude Code, Codex and OpenAI clients the answer had finished. **gpt-oss's tool calls reach OpenAI clients** (on Ollama 0.40.x they ride the `done:true` line, which the OpenAI converter dropped). **MLX tool calling works again:** `--quantized-kv-start 0` scored 0/5 against 8/8 at `5000`, and mlx-lm is pinned to `0.32.2` (re-run `./scripts/setup-mlx.sh`). Also: the mDNS service-type setting takes effect, mid-response backend failures are answered in-protocol, an MLX 4xx no longer leaks the backend URL, and `priority_model_not_loaded` counts a healthy MLX server as resident. **2020 tests, 49 checks.** No `pyproject.toml` dependency changes.

  **Soaked 32 h on the exact released code** (only the version string differs): zero errors, zero warnings, 100% completion, fast-mode share in band. **The honest gap is the same shape as 0.11.1's:** this fleet's production traffic is Ollama-format gpt-oss plus dinov2, so the OpenAI tool-call, truncation and MLX paths were proven by tests, live hand probes and a 40-cell real-SDK matrix, not by the soak. **The publish gate held for real this time:** artifact checks and a clean-venv install chained by `&&` into `uv publish`, and when the install step failed (system `python3` is 3.9, below `requires-python`) nothing was published.

  **Homebrew: `brew update-python-resources` cannot run right after `uv publish`.** It passes `--uploaded-prior-to=P1D`, which hides the package you just uploaded, and fails with "Unable to determine dependencies". Resolve the same closure from the local sdist with the same cooldown instead — `$(brew --prefix python@3.12)/libexec/bin/python -m pip install --dry-run --ignore-installed --uploaded-prior-to=P1D --report=- dist/ollama_herd-X.Y.Z.tar.gz` — diff it against the formula's resources, and update the changed blocks from PyPI's sdist URL and sha256. For 0.11.4: same 45 packages, three newer (certifi, fastapi, json-repair), pip-audit clean. Step 15 fired the trust gate for the third release running.

- **Prior release (`0.11.3`, 2026-10-22):** published to PyPI + GitHub + Homebrew. A security dependency pass (`docs/plans/security-dependency-upgrades-2026-10.md`) — vision server decodes only JPEG/PNG/WebP, Pillow 13 locally via override, floors raised for `zeroconf`/`python-multipart`/`starlette` (+`fastapi>=0.133.0`), Chart.js pinned with SRI, a CVE gate in `health.sh`, and vision embedding requests are now traced. Plus, found while verifying that merge, **the suite no longer reads the operator's `FLEET_*` environment** — three tests failed here and passed in CI because `~/.zshrc` exports `FLEET_ANTHROPIC_MODEL_MAP`, and the three modules that had already hit this each cleared only the variables their own author remembered. And **`herd-node` could not start on Windows, on any version** — the drain handlers called POSIX-only `add_signal_handler` before the heartbeat loop, so the node never registered ([#8](https://github.com/geeks-accelerator/ollama-herd/issues/8), found by running the overdue day-after check). **One thousand nine hundred fifty-three tests at release.**

  **Verified before the soak, on the set a real `pip install` resolves** (built the
  wheel, installed it into a clean 3.14 venv with plain pip so `[tool.uv]`
  overrides do not apply): fastapi 0.159.1, pydantic 2.14.1, starlette 1.9.0,
  onnxruntime 1.31.0, fastembed 0.9.0, cryptography 50.0.2, **pillow 12.3.0** —
  confirming fastembed 0.9's `<13` cap, so the three critical Pillow advisories
  stay open for PyPI users and the format gate is their mitigation, not a fix.
  **The release's full suite (one thousand nine hundred fifty-three) passes there**, independent of this repo's lockfile.

  Then on the live fleet: all five request surfaces non-streaming *and* streaming
  (`/api/chat`, `/api/generate`, `/v1/chat/completions`, `/v1/messages`,
  `/v1/responses`), 30 concurrent mixed requests with zero failures, the format
  gate with real weights (PNG/JPEG/WebP → 200 at 384 dims, GIF/TIFF → 400),
  text embeddings at 768 dims, a multipart upload through `/api/transcribe` → 200
  (the path `python-multipart`'s floor was raised for), **mDNS resolved by zeroconf
  0.151.5** including `FleetServiceDiscoverer.discover()` returning a real address
  (the node sets `FLEET_NODE_ROUTER_URL`, so the soak alone never exercises
  discovery), the Chart.js **SRI hash recomputed from the CDN file and matched**
  with all four chart pages rendering in a real browser at zero console errors, and
  `pip-audit` clean across all extras. `scripts/health.sh` now reports **6/6**.

  **Soaked 3.2 h on the shipping dependency set before publishing, and the
  qualifier is the point.** The 24 h of clean data that preceded it ran on
  **Starlette 0.x** — 1.9 only reached this venv at 08:32 that morning, because
  `uv.lock` is gitignored so another machine's `uv lock --upgrade` never crosses.
  A soak predating the dependency change measures the old configuration, and
  nothing in the process would have caught it: the fleet looked perfect either
  way. **Check the installed version of whatever a release changes before
  believing its soak** — "the fleet has been clean for a day" and "the change has
  been running for a day" are different claims, and only the second is evidence.

  Result: **0 ERROR and 0 WARNING across 20,654 log lines** in both processes,
  **1,109 traces at 100.00% completion**, no restarts (router uptime matched the
  soak exactly), residents stable with no duplicate runners and launch args
  unchanged, and `process_memory` **flat at 1.10 GB with peak 1.11 GB** — against
  a 3.68–3.80 GB historical baseline, with peak 0.01 GB above current meaning no
  excursion ever happened. Throughput **52.0% fast-mode share**, inside the
  43–70% band; the one hour that read 21.5% was my own 30-concurrent load test,
  demonstrated by fast share tracking offered load monotonically (overlap
  4.04 → 2.78 → 2.58 → 1.70 as share went 21.5 → 51.7 → 63.2 → 95.7%). gemma3's
  10.6 tok/s median was the same burst: 20 of its 23 requests landed in one
  13-second window, and the three outside it ran at 30–31 tok/s, *above* the
  22.5 single-stream baseline.

  **The Homebrew formula was REGENERATED for this release, not version-bumped, and
  that was not optional.** The tap sat at 0.11.2 with 38 resource blocks against a
  true core closure of **45**, and three of its pins *violated* the floors this
  release raises: `python-multipart 0.0.24` against `>=0.0.31`, `zeroconf 0.148.0`
  against `>=0.149.16`, `starlette 1.0.0` against `>=1.3.1`. Eight packages were
  missing outright (`requests`, `pydantic_core`, `urllib3`, `charset-normalizer`,
  `httptools`, `uvloop`, `watchfiles`, `opentelemetry-api`), and brew runs pip with
  `--no-binary :all:`, which does not transparently fetch a missing resource. **A
  url+sha256 bump — the shape of every tap bump since 0.10.2 — would have given
  brew users a failed build or the exact versions this release exists to raise**,
  while the changelog said otherwise. Closure computed on **Python 3.12**, which is
  what the formula pins, not the 3.14 this repo develops against.

  **`health.sh` now reports 6/6.** The 66 pre-existing unformatted files were
  formatted in their own commit (verified AST-identical across all 66 — compare
  ASTs, not tokens: `ruff format` joins implicitly-concatenated string literals,
  which changes the token count while the parser folds them anyway). Two
  follow-ons that pass is worth knowing about: `ruff format` and `ruff check`
  disagreed on one line, format joining an f-string to 102 columns that check then
  rejected as E501 and re-split was undone by the next format run; and the
  README check had never verified anything, greping for `run all N tests` against
  a README that said `run all tests (~5s)`.

  **Step 15's trust gate fired, for the second release running.** `brew untrust
  --tap geeks-accelerator/ollama-herd` emptied `~/.homebrew/trust.json` — which
  `brew untap` had left intact moments earlier in the same sequence, which is the
  whole reason this step reads the way it does — and the install was then refused
  with *"Refusing to load formula … from untrusted tap"* before `brew trust` let
  it through. Two releases is not yet a habit; keep the `untrust` line in step 15
  or it silently stops testing anything again.

  **Post-publish checks due: day-after 2026-10-23, week-after 2026-10-29.** The
  0.11.2 pair ran a week late, and `gh issue list` — the cheapest line in it and
  the only one that looks outside this machine — is what surfaced the Windows
  outage after it had sat for six days.

- **Prior release (`0.11.2`, 2026-10-14):** published to PyPI + GitHub + Homebrew. Patch over `0.11.1`, three items all about making an existing signal tell the truth rather than adding behaviour. **`model_load_timeout` now says which of its two causes it is** — it was written for eviction and asserted that cause unconditionally, while Ollama can answer `/api/version` in under a millisecond and still be unable to start any runner; the discriminator is that **two or more distinct models timing out with ample memory cannot be eviction**, and it needed no new data. Detection only, and a test asserts the check never shells out, because the watchdog that killed stuck runners was removed 2026-04-23 after its escalation wiped every pinned model. **Importing either CLI no longer injects the operator's env file** (28 `FLEET_*` variables → 0; the call moved into each CLI's existing Typer callback, before `server_cli`'s subcommand early-return since `herd mlx pull` reads settings too). And the **last two untested modules are covered** — `discovery` (9 tests) and `rebalancer` (15) — closing that item entirely. **1921 tests, 49 checks.** No dependency changes.

  **Soaked 22 h on HEAD:** 7,295 requests, 100% completed, **zero warnings and zero errors across 145,247 log lines**, no restarts, health 91→94. **Worth stating what that does and does not prove:** only four source files changed and three have no normal-traffic surface — the CLI move is startup-only (proven at restart on both processes), and the timeout discrimination only fires during an Ollama wedge, which has not recurred. The SSE state hoist is the one path the soak genuinely exercised. This release's evidence is absence of harm, not presence of function.

  **One process error worth not repeating:** the artifact-verification loop asserted on a string split across two f-string literals, so it failed spuriously — and because it was a separate statement in the same script, **`uv publish` ran anyway**. The artifact was correct (re-verified four ways against the published sdist and the Homebrew bottle), but the gate did not hold. Put the publish behind the verification's exit status, or verify in a step that cannot be followed by a publish in the same invocation.

- **Prior release (`0.11.1`, 2026-10-12):** published to PyPI + GitHub + Homebrew. Patch over `0.11.0`: three new check IDs but no schema change and no new API surface, which is the opposite of what made `0.11.0` a minor. Five fixes, the first affecting a whole API surface — **context protection silently skipped `/v1/chat/completions`** (`_apply_context_protection` was called only on the Ollama/Anthropic/Responses branch of `_build_ollama_body`, so OpenAI-format requests reached Ollama with no `num_ctx` at all; a cold load over that route came up at 131072/slot instead of its configured 32768, 48 GB resident for a 19 GB model). Also: an `mlx:` model no node hosts no longer goes to whatever holds the legacy `FLEET_MLX_URL` port (63 traced failures blaming transport for configuration); rate health checks have a sample floor **derived from their own threshold**; and one model resident twice no longer makes its resident context ambiguous. **49 checks, 1886 tests.** No dependency changes, so the Homebrew bump was version + sha256 only.

  **Soaked 13 h on HEAD before the bump** — 4,220 requests, 100% completed, zero warnings and zero errors across 82,927 log lines, no restarts. **Verify the running code is actually HEAD rather than assuming it:** the last code commit landed 99 seconds *after* the router started, which looked wrong, and the check that settled it was `find src/ -name '*.py' -newermt "<router start>"` returning nothing plus the three later commits being doc-only. **One honest gap:** all 4,217 traced requests were `ollama` format, so the OpenAI-surface fix saw no production traffic — hand-verified and covered by tests parametrized over all four formats, but not soaked.

  **Step 15 of the release checklist finally tested what it claims to.** `brew untrust --tap …` before the install made the gate fire for real — *"Refusing to load formula … from untrusted tap"* — then `brew trust` and a clean install. Every prior release's step 15 passed without exercising it, because the trust record survives `brew untap`.

- **Prior release (`0.11.0`, 2026-10-11):** published to PyPI + GitHub + Homebrew. Minor rather than patch: alongside two fixes it adds a trace column (`estimated_tokens`, via ALTER TABLE), a new `no_output` request status and a new `num_ctx_unmanaged` check. **1886 tests, 49 health checks.** No dependency changes, so the Homebrew bump was version + sha256 only.

  **The headline fix: an absent `num_ctx` is not neutral.** A per-model `FLEET_NUM_CTX_OVERRIDES` entry could not survive one request — the router correctly declined to re-inject once a model was resident at its configured context, the request went out bare, and Ollama filled the gap from `OLLAMA_CONTEXT_LENGTH` and reloaded the model at the larger window, evicting its co-resident peer. `StreamingProxy._pin_resident_num_ctx` now sends the resident value, which cannot trigger a reload. Also `kv_cache_bloat` no longer invents KV cache out of guessed weights. Both in the gotchas above.

  **Soaked ~45 h before publishing, and the soak got the event it needed rather than just the hours.** A reboot plus an unrequested Ollama `0.35.1 → 0.40.2` upgrade delivered all three cases the fix shipped unable to reproduce by hand — eviction, the heartbeat race, preloader cold loads — and the pin held: both models returned at their configured per-slot contexts. 0 ERROR lines, 0 retries, 5,613 requests at 100% completion in the final 24 h.

- **Release-process correction from this run:** step 15's untap does **not** test the Homebrew trust gate — the record is in `~/.homebrew/trust.json` and survives `brew untap`, so the 0.11.0 step-15 run installed with no gate at all. `brew untrust --tap …` is the missing step, verified to produce the refusal and then the success. See the Homebrew-trust gotcha. Install was 2m54s with a warm build cache, not the ~25 min the docs assumed.

- **Open, in the order worth taking them.** Two entries were removed on
  2026-10-22 after checking rather than trusting them, which is the third time a
  triage pass has found this file describing work that had already shipped:
  **duplicate runners** (`LoadedModel.digest`, `resident_context_for`,
  `distinct_resident_models` and the `duplicate_runners` check are all in the
  tree) and **test pollution** (the autouse fixture is in `tests/conftest.py`,
  and `pytest tests/test_server/test_streaming.py tests/test_server/test_health_engine.py`
  — the order that used to fail three tests — passes 67/67). **Verify before
  acting on anything below.**

  1. **Phase 1 step 3** of `docs/plans/context-window-blindness.md` — the truncation threshold, deliberately unbuilt. Calibration argues for a size-banded rule, not one ratio: 176 `estimated_tokens`/`prompt_tokens` pairs give a 0.94 median above 100 estimated tokens and ~8x below it, where the chat template dominates.
  2. **The `/api/ps` total-vs-per-slot contradiction**, still OPEN — and 0.40's `RUNNER` column may have changed what it reports.
  3. **No CI, and every test runs on one OS** (`docs/issues.md`, 2026-10-22) — the gap that let `herd-node` ship unable to start on Windows past nineteen hundred tests. The real fix is a GitHub Actions matrix (macOS, Ubuntu, Windows) asserting one heartbeat registers; four `sys.platform` mocks prove a guard exists, not that the agent starts.
  4. **`/api/embed-text` is unreachable** (`docs/issues.md`, 2026-10-22) — decorated on a router `app.py` never includes, so it 404s and is absent from all 72 paths in `/openapi.json`. Documented in a test docstring for months, which is the point: a docstring did not get it removed.
  5. **Post-publish checks** — `0.11.3` week-after **2026-10-29** (its day-after ran 2026-10-23: clean, 110 downloads on release day, no new issues); `0.11.4` day-after **2026-10-25**, week-after **2026-10-31**.
  6. **Upstream changes, October 2026** (`docs/plans/upstream-changes-2026-10.md`) — Phases 1, 2, 4 and 5 **shipped in `0.11.4`**; the plan opens with each phase's outcome. **Remaining: Phase 3**, upgrading this box's Ollama `0.40.2` → latest as the test of whether `0.40.4` fixed the digest-rewrite duplicates — deliberately after `0.11.4`'s day-after check so the two are not measured together, and it needs the operator's go-ahead for the app download. Also OPEN from that work: one MLX coder instance answered wrongly from its first request at the fixed `--quantized-kv-start 5000` (`docs/issues.md`) — if it recurs, keep it running and diff it against a fresh instance before restarting anything.

  **Two plans cover work that is now done, kept for their reasoning rather than their task lists:** **[`docs/plans/ollama-mlx-runner-subsystem-decision.md`](docs/plans/ollama-mlx-runner-subsystem-decision.md)** and **[`docs/plans/duplicate-runner-ambiguity.md`](docs/plans/duplicate-runner-ambiguity.md)**. **Their shared first step shipped 2026-10-11:** `LoadedModel` now carries `runner` and `digest` from `/api/ps`, and the node card's existing backend label names the runner when it is not `llamacpp`. Both residents verified at `runner=llamacpp`, matching `format=gguf` — so `ModelTagMeta.format` (which `decode_parallelism_for` already uses, and which *is* Ollama's `IsMLX()`) predicts the choice, while `runner` reports what was done. A divergence between them means our model of Ollama's selection is wrong.

  **One caution on reading the MLX usage numbers:** low traffic to the MLX servers measures *our* cost, never the project's — this is open source, the subsystem is opt-in (`mlx_enabled` defaults False) and published in 9 of 37 skills, so removal is an announced deprecation, never a percentage call. And the 33 GB those two idle servers hold is **not** a fleet-wide tax: it is the price of warm readiness, this box has 334 GB free with zero swap and `underutilized_memory` advising *more* models, and `FLEET_NODE_MLX_SERVERS` is per-node, so a small machine simply runs none.

  **Both plans are now closed.** `duplicate-runner-ambiguity.md` is **complete** — all four phases, with `serializers.resident_context_for` as the single definition of a model's resident context, returning *unknown* when runners disagree so the pin passes through rather than guessing, and `distinct_resident_models` / `distinct_resident_model_names` separating runner counts (VRAM — two runners are two processes holding real memory) from logical-model counts (`free_slots`, `models_loaded_count`, `underutilized_memory`). All three sit beside `model_resident_on_node` for the reason that one was centralised: several callers need the answer and must not drift. **Digest is stable across a full Ollama restart** (verified 2026-10-11, both models kept theirs), which is what makes dedupe-by-digest sound. `ollama-mlx-runner-subsystem-decision.md` is **RESOLVED — HOLD**: keep the `mlx_lm.server` subsystem, stop extending it, skip the Ollama-MLX parity measurements. The deciding evidence is `GLM-4.7-Flash` — all-time on this box, Ollama has **30 completions against 235 timeouts and 12 failures** (worst 87 min) while MLX has **27 completions and 1 failure** and answers in ~3 s. **89% of Ollama GLM attempts never completed normally**, so the subsystem is the only working path for a model this fleet serves, not sentiment. What would reopen it: Ollama fixing `glm4moelite` ([ollama#14045](https://github.com/ollama/ollama/issues/14045)). Also answered on the way: compaction has never fired because the 20,000-token budget is never crossed (87 of 1,504,192 requests ever reached it, 2 on `gpt-oss`), which is not a bug — and 17 of those 87 were on the MLX coding model at up to 129,159 tokens, so the capability served a real past workload. The rest of the open list is small enough to do without a plan.

  **And triage before trusting the list.** A full pass on 2026-10-13 closed **5 of 26** examined — none fixed that day, all shipped earlier with the entry never updated (the MLX *hold* decision, dynamic `num_ctx`'s three phases, the `NUM_PARALLEL` bloat entry whose "Remaining" item *is* `FLEET_DYNAMIC_NUM_CTX`, `HeartbeatPayload.arch`'s `_detect_arch`, and unused imports now enforced by ruff's `F401`). It also found that **counting `###` headings overstates the work by ~45%** — 18 of 43 are sub-headings inside already-resolved `##` sections. `docs/issues.md` now opens with a reading guide. Earlier passes found the same: the `/api/ps` contradiction and the "Ollama 3-model cap" — `/api/ps` `context_length` (resolved by measurement that day) and the "Ollama 3-model cap" (disproven 2026-07-17, while CLAUDE.md carried the refutation and the issue stayed OPEN). An agent reading `issues.md` and one reading `CLAUDE.md` would have disagreed. Expect more of these.

- **The decode distribution is bimodal, and the median is the wrong statistic for it.** Measured 2026-10-13 over 24 h (n=6,554): a fast mode at ≥70 tok/s and a slow one at <45, with the hourly median swinging **42–79 on a healthy fleet** because it sits in the valley between the modes. **Prompt size does not discriminate them** — fast median 835 tokens vs slow 864 — but **concurrency does: 0.77 overlapping requests vs 3.42.** So it is plain co-tenancy on a bandwidth-bound box (the 2026-10-05 measurement: single-stream gemma3 22.5 tok/s, four slots 27.1), not long-context starvation. **Read the fast-mode share, not the median** — it held 47–61% in every full hour. A median dropping 77 → 60 means the mix moved, not that the fleet slowed, and that swing was reported as a possible regression on two consecutive days before anyone measured the cause. **The consumer-side statistic is already right, and was verified:** `get_decode_latency_stats` computes p99 TPOT per `node:model` against that model's own 24 h baseline — its docstring explains why per-request tok/s is the wrong measure, citing the 2026-07-19 investigation that lost hours to it — and `decode_degraded` fires at 2.0x. Replaying that comparison across 48 half-hour windows gives a worst observed ratio of **1.33x**, a **1.51x margin**, zero would-have-fired windows, and it has never fired in any log. Worth watching rather than changing: p99 TPOT varies *more* hour-to-hour (49% of mean) than median tok/s (34%), so that margin is thinner than the threshold implies. Next-steps analysis: `docs/plans/next-issues-worth-doing.md`.
- **Seven checks in three weeks have fired on correct behaviour** — and the seventh, `high_error_rate`, was found by *my own two test requests* tripping a WARNING. It had no minimum denominator, so at a 5% threshold one failure crossed it at any total ≤ 20, and all MLX traffic lands on the `mlx-local` pseudo-node (233 lifetime requests), whose denominator is almost always tiny. Fixed with `_min_samples_for_rate`, **derived from the threshold** so they cannot drift. **Two of the four rate-shaped checks were exposed; the other two fire on absolute counts and a denominator would have broken them** — `stream_reliability`'s `no_output >= 1` is what 0.11.0 made visible on purpose. And the floor *reports* below the threshold rather than hiding, because silence conceals 3-of-3 as effectively as WARNING over-reports 1-of-3. Beware `1.0 // 0.05`, which is **19.0**: the first fix returned a floor of 20, where one failure is exactly 5.0%, and since the gate is `< threshold` the check still fired — an off-by-one that defeated the whole fix while looking right.
- **Prior six checks that fired on correct behaviour** — `priority_model_not_loaded`, `context_waste`, the stale reaper/worker pair, the `num_ctx` equality warning, and `kv_cache_bloat` twice over (phantom weights, wrong lever). Two questions to ask of any new check: **what does it fire on when everything is working**, and **is it deriving a quantity the system already reports?** Both 2026-10-09 bugs were a layer computing something it could have been told — on-disk model size, and the resident context.

- **Memory incident, resolved 2026-10-02/03 (two devices).** The native text embedding server kept ONNX Runtime's high-water mark forever — `batch x seq_len^2`, with 8,192-token inputs, flat batches of 32 and concurrent runs on one session — reaching **28 GB in one node agent** and taking a 48 GB Mac out along with its co-tenants. Fixed by truncating to the registry `max_tokens` (2048), sizing batches against an attention budget, and serialising ONNX runs per model; measured peak with both models now **3.71 GB**, previously >8 GB within minutes. `prompt_eval_count` is now a real tokenizer count rather than `len(t.split())` — which also explains the long-standing embed-latency gap, since an unspaced 8 K-char input recorded as `1`. **Still open:** memory is bounded but not *released* (~3.7 GB retained until restart; proposed fix is idle eviction from `_slots`, and **measure with `footprint`, not RSS** — RSS is what hid the 28 GB).
- **herd now measures its own processes (2026-10-03).** `HeartbeatPayload.process_memory` carries the agent's `phys_footprint`, its **peak**, and labelled children — the number whose absence made the 28 GB undiagnosable on both devices, because heartbeats carried only *system* memory, which here is dominated by Ollama's ~91 GB plus two mlx children at 17 GB each. First 9 h soak: peak rose once to 3.80 GB then held **exactly flat for 10 h** while current drifted down 3.53 → 3.47. See the memory gotcha above and `docs/observations.md` (2026-10-03).
- **macOS memory pressure was dead code until 2026-10-02**, and the elimination it gated was the wrong shape. `memory_pressure -Q` never prints the words the probe searched for, so every Mac always reported `normal`; now reads `kern.memorystatus_vm_pressure_level`. Critical pressure now withholds *cold loads* and keeps serving resident models, because blanket elimination froze a one-node fleet into 503s while freeing nothing. See the pressure gotcha above.
- **`backend_bypass_clients` (2026-10-02)** closes the co-tenancy blind spot that cost hours in August: nodes probe who holds connections to their Ollama and the router names the pid, process and full argv. Detects off-box clients too. Verified end-to-end; currently silent, i.e. nothing is bypassing.
- **Aug-22 step change: unbounded dispatch ELIMINATED (2026-10-03).** Queue-concurrency enforcement went live 2026-10-02 against a pre-registered prediction, and p25 stayed inside its nine-day 49–55 band (52.5 and 56.6 on the enforced days) against a pre-Aug-22 73.4. The issue remains **unexplained** and now carries an explicit *do not re-propose* list. See `docs/issues.md`.
- **Throughput incident, resolved 2026-08-23:** fleet decode on `gpt-oss:120b` ran ~15% below baseline for two days (mean 69→59 tok/s). **It was not Ollama, llama.cpp, or herd** — a co-located client (the globally-installed `openclaw` CLI daemon, config `~/.openclaw/openclaw.json`) was calling `http://127.0.0.1:11434` directly, contributing **27% of Ollama's load that herd could not see**, including 77K-token re-prefills every 30 minutes. It arrived there by *fallback*, not by configuration: its cloud provider lost its API key and its model-fallback chain silently rerouted onto the local fleet. Resolved by stopping the daemon and disabling its launch agent (`launchctl bootout gui/$UID/ai.openclaw.gateway` + `launchctl disable`) — the project was no longer in use. **Removal helped, but did NOT restore the fleet — and the original claim here that it did was wrong.** It came from a 25-minute window (n=114, n=12 at conc=3) that was not representative. Measured over whole days: p25 went 43.0 (Aug 23) → 44–49 (Aug 24–Sep 1), never back to the 73.4 of Aug 21; conc=3 stayed flat at ~52 against 68.0 before. So openclaw was worth roughly 5 points of p25, not the ~30 the earlier note implied. **The Aug 22 step change (p25 73.4 → 43.1, conc=3 68.0 → 50.6) is therefore still unexplained** — see the OPEN issue in `docs/issues.md`. **Note `gemma3:27b` now loads at `CONTEXT 32768` as configured — the `FLEET_NUM_CTX_OVERRIDES` "bug" filed during triage was the bypassing client, not herd.** See the co-tenancy gotcha above, `docs/observations.md` (2026-08-23), and `docs/troubleshooting.md`.
- **Model catalog eval (2026-08-14):** added four *runnable* new models to `model_knowledge.py` — `qwen3-vl:32b` (vision, replaces gemma3 for image work), `qwen3.6:27b`, `qwen3.6:35b-a3b` (coding MoE), `muse-glimmer:30b` (agentic multimodal, needs Ollama ≥0.32.7, `is_thinking_model` updated to inflate its budget). **Do NOT re-attempt DeepSeek-V4-Flash or GLM-5 on this box** — both were evaluated and rejected: `deepseek_v4` isn't in any mlx-lm runtime, and GLM-5-4bit (419 GB) can't load on 512 GB (MLX mmap load-doubling ceiling — *not* fixable by raising `iogpu.wired_limit_mb`, which is a footgun that hard-locked the machine when tried). Full autopsy in `docs/observations.md` (2026-08-14). GLM tier stays on GLM-4.7-Flash / GLM-4.7 full.
- **Prior release (`0.10.2`, 2026-10-08):** patch over `0.10.1`, three fixes all of one family — a check firing on correct behaviour. **Both slot-release paths now key on progress, not elapsed time:** the reaper declared a request stuck at 600 s that finished normally with 9,610 tokens, and fixing it *promoted* the queue worker's dormant 660 s timeout (`stale_timeout + reaper_interval`, sized to fire after the reaper so its path was dead code), which then did the same to two more. **Proven over 23 h:** three requests ran past 660 s — the longest **1,323 s with 16,384 tokens**, twice the old threshold — and all completed with zero warnings. A test asserts no path offsets the shared threshold, since two paths with two numbers is what let it hide for a day. Also: `"num_ctx override cannot apply"` no longer warns when resident *equals* configured (the injection gate `override <= resident` is right for deciding whether to inject, wrong as a warning condition); `_check_num_ctx_override_inert` was always correct at `have != want`, which is why the card and the log disagreed. No dependency changes; Homebrew bump was version + sha256. (That release also claimed the `brew trust` gate was "re-verified from clean" — on 2026-10-11 the untap-based procedure was measured *not* to clear the trust record, so this and earlier such claims were almost certainly untested. See the Homebrew-trust gotcha.) **Four of these in two weeks** — `priority_model_not_loaded`, `context_waste`, the reaper/worker, and this one — so it is a class: a condition encoding an assumption about what "wrong" looks like, meeting a state the system reaches legitimately. **Ask of any new check what it fires on when everything is working.** `0.10.1` (2026-10-05) carried the [#6](https://github.com/geeks-accelerator/ollama-herd/issues/6) renderer-lifecycle fix, swap/compressed memory with a `swap_usage` check, and the self-explaining native-embed 503. `0.10.0` (2026-10-03) was the minor with the 28 GB embedding fix, kernel memory pressure, cold-load withholding, `backend_bypass_clients` and `process_memory`. **[#7](https://github.com/geeks-accelerator/ollama-herd/issues/7) is OPEN and unreproduced** — `brew install` fails on macOS 27 inside a `flit_core` build; the thread has since pinned a Homebrew bug (`JSON::Coder` rejecting `Pathname` from `BuildError#args`, upstream commit `9e4ebea`) that masked the real error, and the remaining lead is a stray `py3.13` `.pth` in that machine's python@3.12 site-packages breaking `import json` at startup — environmental, no formula change indicated. `pip install` is unaffected.

- **Autostart (2026-09-28):** two launchd agents own the fleet — `com.geeksaccelerator.ollama-herd.router` and `.node`, `RunAtLoad` + `KeepAlive{SuccessfulExit:false}`. Added after three unattended outages in a month (one 21 hours), all "nobody restarted herd after a reboot" — health checks cover a degraded fleet, not an absent one. **`pkill` is no longer a stop**; use `launchctl kickstart -k` / `bootout`. See § Autostart and `docs/examples/launchd/`.
- **Self-inflicted latency regression, resolved 2026-09-28:** `OLLAMA_CONTEXT_LENGTH=32768` (set on 09-22 to clear a model-load deadlock) overrode the router's per-request `num_ctx`, quartering gpt-oss's per-slot context and collapsing prefix-cache reuse — **TTFT 1.0s → 6.3s, total latency 5.3s → 10.5s, decode unchanged**, for six days. Reverted to `131072`; TTFT back to 1,021 ms. The deadlock does not recur, because per-model contexts via `FLEET_NUM_CTX_OVERRIDES` were always the right lever. Full autopsy in `docs/observations.md` (2026-09-28); see also the `OLLAMA_CONTEXT_LENGTH` gotcha above.
- **Prior (0.7.0):** published on PyPI + Homebrew tap (live since 2026-06-07). 0.7.0 ships a native text embedding server (fastembed, port 11439) that routes `nomic-embed-text` out of Ollama entirely — eliminating embed timeout contention under concurrent LLM load. Root cause: `OLLAMA_NUM_PARALLEL=2` means in-flight LLM inference holds both slots; embed requests queue indefinitely inside Ollama regardless of available hardware. Fix: fastembed serves `nomic-ai/nomic-embed-text-v1.5-Q` (130 MB int8 ONNX, 768 dims) on a dedicated port with zero inference slot contention. Verified on local fleet 2026-06-07: 573 embed requests/24h, avg 792ms, 0.0% error rate. Also ships: 4 new health checks (embed_error_rate, text_embedding_backend_missing, text_embedding_ollama_bypass, nomic_loaded_in_ollama), embed retry + trace recording on all paths, and dashboard "Node Models" section (renamed from "Request Queues") with cards for all model backends (Ollama, MLX, native fastembed, vision embedding).
- **Fleet:** Neons-Mac-Studio (512GB M3 Ultra) + Lucass-MacBook-Pro-2 (128GB M4 Max). Read the live config rather than this list — `grep FLEET_NODE_MLX_SERVERS ~/.fleet-manager/env` and `curl -s localhost:11435/fleet/status`; it has drifted from what was written here before. Mac Studio as of 2026-10-10 runs two MLX servers: **`mlx:mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` on :11441**, the dedicated compactor (`FLEET_CONTEXT_COMPACTION_MODEL` points at it), with `kv_bits: 8` and `--draft-model mlx-community/Qwen3-1.7B-4bit --num-draft-tokens 4` for speculative decoding (~94 tok/s on M3 Ultra), and **`mlx:lmstudio-community/GLM-4.7-Flash-MLX-4bit` on :11442**, no draft. Plus `gpt-oss:120b` and `gemma3:27b` via Ollama (both GGUF, both `llamacpp`-run) and `nomic-embed-text` via the native fastembed server (:11439), which also has `ms-marco-minilm-l-6-v2` registered for rerank but not cached. **`mlx:Qwen3-Coder-Next-4bit` on :11440 is gone** — it is no longer configured, and the reason it was run without a draft model is still worth knowing: Qwen3-Next's hybrid linear-attention architecture builds a non-trimmable `ArraysCache` and hits mlx-lm#1081.
- **Ollama:** **`0.40.2`**. The version drifts on its own: `0.32.9 → 0.32.13 → 0.32.15 → 0.33.0 → 0.34.2 → 0.34.4 → 0.35.1 → 0.40.2`, every step a reboot or relaunch, none of them deliberate. `0.40.2` arrived with a reboot on 2026-10-10 — **two minor versions in one step, including the release that makes MLX the default on Apple Silicon.** Re-checked on arrival: both resident models are still GGUF (`gpt-oss` MXFP4, `gemma3` Q4_K_M) so `IsMLX()` (`format == "safetensors"`) leaves both on llama.cpp; `decode_parallelism_for` computes 4 for both and the launch args confirm `-np 4`; `OLLAMA_SERIAL_FAMILIES` unchanged. Launch args intact at `-c 524288 -np 4` / `-c 131072 -np 4`, and **the `num_ctx` pin survived the reboot** — both models came back at their configured per-slot contexts, which is the cold-load case that could not be reproduced by hand. **Two new things in 0.40:** `ollama ps` has a **`RUNNER` column** (`llamacpp` / MLX per model — read it instead of inferring from `format`), and **the upgrade rewrote the manifest digest of both already-resident models**, which silently doubled each one on its next request (114 GB, then 156 GB, for two logical models) because Ollama treats a new digest as a different model and `KEEP_ALIVE=-1` means neither copy ever idles out. Clearing it needs **`ollama stop <name>` twice** — the first call takes the *current* digest and leaves the stale one. **Check `/api/ps` for duplicate names after any Ollama upgrade.** See `docs/issues.md` (2026-10-10). `0.35.1` had arrived on 2026-10-02 while herd was shut down for ~2 h — a relaunch during a network outage recovery, nobody asked for it — and soaked 9 h clean (2,745 requests, 0 failures). **Re-check `curl -s localhost:11434/api/version` after ANY restart** — and be aware anything that can bind `:11434` first is served transparently (a stray `homebrew.mxcl.ollama` agent once won that race and served `0.16.3`; it is now `bootout`'d and `disable`d). Applying an `OLLAMA_*` env change requires quitting the **Electron parent**, not just `ollama serve`, and reaping orphaned `llama-server` (`ppid=1`) — see `docs/configuration-reference.md` § Ollama environment. Historical drift detail: `0.32.15` came from a 2026-08-21 21:57 reboot; `0.32.13` from a 2026-08-14 reboot; `0.32.9` was a manual upgrade from `0.32.5` that day to unlock version-gated models like `muse-glimmer:30b`, which requires ≥0.32.7; was `0.32.1` on 2026-07-17, from `0.24.0`). **Nuance on auto-update: point releases do NOT roll in while the app is *running* (the box sat at `0.32.5` while `0.32.9` was out), but the Mac app DOES self-update on relaunch/reboot** — a reboot silently moved `0.32.9`→`0.32.13`. So **a reboot can change your Ollama version; always re-check `curl -s localhost:11434/api/version` after one** (and note the app may pull a *prerelease* — verify against the GitHub releases page if the exact version matters). To upgrade on demand without waiting for a reboot, do a **manual Mac-app swap** (download `Ollama-darwin.zip` from the GitHub release, quit the app, `xattr -dr com.apple.quarantine`, replace `/Applications/Ollama.app`, relaunch) — **`brew upgrade ollama` does nothing** (stale formula). **Watch the launch race:** quitting the app can let another `ollama serve` (or herd-node's auto-restart) grab port 11434 first; verify the version after the swap, not a stale one. `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=10` (in `~/.zshrc` **and** `launchctl setenv` — Ollama is the Mac app launched by launchd, so it reads launchctl). **Include the Ollama version in soak checks** (`curl -s localhost:11434/api/version`) so this can't drift silently again. 0.32.1 brought glm 13.7→77.8 tok/s, gpt-oss 50.9→74.5, and working prefix caching — all via llama.cpp. **`OLLAMA_NEW_ENGINE` no longer exists** — Ollama deleted both CGO engines (`runner/ollamarunner`, `runner/llamarunner`) in PR #16031 (2026-05-29, −430K lines) and now shells out to upstream `llama-server` for every GGUF model; the env var is absent from `envconfig/config.go`. The Go engine was reborn as the MLX runner for Apple Silicon (`mlxrunner/`; moved out of `x/` on 2026-09-16, `2e036e7c`), selected **per-model** by `IsMLX()`, which is exactly `ModelFormat == "safetensors"` — not by any flag. It **decodes one request at a time**: `mlxrunner/runner.go` is a single loop that runs each request to completion, so `OLLAMA_NUM_PARALLEL` has no effect on it. `sched.go` also forces `numParallel=1` for non-completion models and the architectures in `OLLAMA_SERIAL_FAMILIES` (`server/serializers.py`). Herd mirrors all three per model in `decode_parallelism_for(node, model)` — **re-check that family list against `sched.go` on every Ollama upgrade**. Ollama `0.40` makes MLX the default on Apple Silicon. So there is no switch to flip: a GGUF model cannot use MLX. See `docs/plans/ollama-0.32-upgrade-and-mlx-evaluation.md`.
- **Skills:** 37 on ClawHub across `skills/`. They quote the test count and the health-check count, and those quotes go stale silently — 19 of them were a release behind as recently as 2026-10-09. **Two traps here, both hit:**
  1. A maintenance grep written with *literal* numbers matches nothing once they move, so the drift it was meant to catch continues undetected. **Grep by shape, not by value.**
  2. A note that *describes* stale numbers cannot itself contain them in the live shape (`N tests`, `N checks`) — a refresh script rewrites the anecdote and turns it into nonsense. This bullet used to do exactly that. Spell historical values out in words, or keep them out.

  ```bash
  grep -rnE "[0-9]{3,4} tests|[0-9]{2} (automated |health )?checks" skills/ AGENTS.md README.md CLAUDE.md
  uv run pytest -q | tail -1                                                    # real test count
  grep -oE 'check_id="[^"]+"' src/fleet_manager/server/health_engine.py | sort -u | wc -l   # real check count
  ```
  Refresh **all four locations**, not just CLAUDE.md — the skills are where the drift hides.

- **Three checks added 2026-10-11** — `duplicate_runners` (one name, two digests, one node; rates by reclaimable GB and carries the two-call `ollama stop` remediation), `mlx_routing_disabled` (a node running `mlx_lm.server` while the router has `FLEET_MLX_ENABLED` unset — nothing surfaced it, because `mlx_backend_active` fires INFO on that same state), and `mlx_requests_unservable` (traffic for an `mlx:` model no healthy server hosts). Runbooks for all three plus the rate-check sample floor are in `docs/troubleshooting.md`.
- **Health:** 49 distinct checks (count via `grep -oE 'check_id="[^"]+"' src/fleet_manager/server/health_engine.py | sort -u | wc -l`). Monitor: `curl http://localhost:11435/dashboard/api/health`

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
- **`brew tap` + `brew install` is no longer enough — Homebrew 6.0+ requires `brew trust` for third-party taps.** A fresh user who follows a tap-then-install README hits `Refusing to load formula … from untrusted tap` and gets nothing installed. Both READMEs (main repo and tap repo) carry the `brew trust` line. **Correction (2026-10-11, measured on Homebrew 7.0.9): untapping does NOT reproduce this, so step 15 never tested it.** Trust is recorded per tap in `~/.homebrew/trust.json` (or `$XDG_CONFIG_HOME/homebrew/trust.json`) and **survives `brew untap`** — on this machine the 0.11.0 step-15 run tapped fresh and installed with no gate at all, and `brew trust` answered `Already trusted tap`. To actually test the fresh-user path you must clear the record:

  ```bash
  brew untrust --tap geeks-accelerator/ollama-herd   # the step that was missing
  brew uninstall ollama-herd
  brew install ollama-herd    # MUST fail: "Refusing to load formula ... from untrusted tap"
  brew trust geeks-accelerator/ollama-herd
  brew install ollama-herd    # now succeeds
  ```

  Verified in exactly that order on 2026-10-11: refusal, then success. The untap is still worth doing — it forces a fresh clone and proves the *pushed* formula is the one being built — but it is not what guards the trust gate, and the earlier note here said it was. The 0.9.0 failure it cites was real; the mechanism attributed to it was not.
