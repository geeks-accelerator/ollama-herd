# Distributed MLX Inference — Multi-Node `mlx_lm.server` via `mlx.launch`

**Status**: Phase 1 implemented 2026-07-13 (ring + jaccl command construction, all unit-tested); pending hands-on LAN validation. Phase 2 (dashboard badge, orphan-reap extension) + Phase 3 (setup helper) open.
**Created**: 2026-07-13

**Groundwork already shipped (2026-07-13), independent of the distributed feature:**
- Binary discovery promoted to `common/binaries.py::which_extended`; `find_mlx_lm_binary` + new `find_mlx_launch_binary` are thin wrappers over it (no third copy).
- `self.host` overload fixed: `MlxSupervisor.health_host` decouples the health-poll target (always loopback) from the `--host` bind, closing the latent `0.0.0.0` poll bug. Regression tests added.
- Node MLX config consolidated to the single `FLEET_NODE_MLX_SERVERS` surface; legacy single-server env vars + the poll-only `MlxClient` path removed.

Remaining for this plan: the `backend`/`hosts`/`hostfile`/`pipeline` spec fields + `_build_cmd` `mlx.launch` wrapper (Phase 1 below).

**Related**:
- `docs/research/apple-distributed-mlx-jaccl-2026.md` — JACCL / EXO / topology research
- `docs/plans/mlx-backend-for-large-models.md` — the single-node MLX backend this extends
- `docs/guides/mlx-setup.md` — operator-facing MLX setup guide (gets a new "Distributed" section)
- `docs/guides/testing-distributed-mlx.md` — three-tier test plan (single-machine ring → LAN ring → JACCL/TB5)
- `src/fleet_manager/node/mlx_supervisor.py` — where nearly all the change lands
- WWDC26 session 233 "Explore distributed inference and training with MLX"

---

## Motivation

`mlx_lm.server` supports distributed execution natively: launched under `mlx.launch` across N Macs, **only rank 0 binds the HTTP server**; it broadcasts each request to the other ranks, which hold shards of the model (tensor parallelism) or slices of its layers (pipeline parallelism). From outside, the cluster looks *identical* to a single `mlx_lm.server` — same `/v1/models`, same `/v1/chat/completions`, same port.

That last fact is the whole reason this is cheap. The herd's entire MLX stack — `MlxSupervisor` lifecycle, health checks, restart/quarantine, heartbeat advertisement, `MlxProxy` routing, `mlx:` model prefix — talks to rank 0's HTTP endpoint and **does not care** whether one Mac or four are behind it. The only thing that changes is the command line the supervisor spawns.

### When this actually delivers value

Being honest about payoff, because it shapes the priority:

| Scenario | Backend | Benefit | Available today? |
|---|---|---|---|
| **Two LAN Macs, memory pooling** (e.g. run a model bigger than either node) | `ring` (TCP) | Memory expansion, **no speedup** (pipeline) | **Yes** — plain LAN, no RDMA/TB5/macOS 26.2 |
| **2–4 symmetric Macs, faster inference** | `jaccl` (RDMA/TB5) | Up to ~3× tok/s (tensor, 4 nodes) | Needs macOS 26.2 + TB5 cables + Recovery-mode RDMA |
| **1T-param models** (Kimi K2.6 INT8 ~1TB) | `jaccl` | Runs models no single Mac holds | Needs ≥2× 512GB nodes + TB5 mesh |

**The ring backend is the near-term win.** The current fleet (Mac Studio 512GB + MacBook Pro 128GB) can pool memory over its existing LAN *today* — no new hardware, no macOS upgrade — via `--backend ring --pipeline`. That enables loading a model in the ~512–640GB range that overflows the Studio alone. The JACCL/tensor path is forward-looking infrastructure that pays off when a second symmetric node or a TB5 mesh exists.

**Asymmetric-fleet caveat** (see research doc): tensor parallelism splits every layer evenly, so it is bottlenecked by the *smallest* node — a 512+128 pair caps tensor-parallel model size at 2×128=256GB, *smaller* than the Studio alone. For asymmetric pairs, **pipeline parallelism (ring or jaccl) for memory expansion is the only sensible mode**. Tensor parallelism is for symmetric clusters.

---

## Background: how the launch actually works

Verified command shape (from the MLX docs and the `alexziskind1/mlx-jaccl-cluster` community repo):

```bash
mlx.launch \
  --backend jaccl \
  --hostfile /path/to/cluster.json \
  --env MLX_METAL_FAST_SYNCH=1 \
  --no-verify-script \
  -- \
  mlx_lm.server --model <repo-or-path> --host 0.0.0.0 --port 11440 [--pipeline]
```

Key mechanics that matter for the implementation:

- **`mlx.launch` is the process the supervisor owns.** It runs locally, SSHes to the other hosts, and execs the inner command (`mlx_lm.server ...`) as one rank per host. It **monitors all ranks and terminates the rest if any one dies**, and **kills every rank if `mlx.launch` itself is signalled**. So the existing supervisor teardown (SIGTERM to the process group) cascades to remote cleanup for free.
- **Rank 0 is the local host** (where `mlx.launch` runs = where herd-node runs). Rank 0 binds the HTTP server on `--host/--port`. So the supervisor's existing `http://127.0.0.1:<port>/v1/models` health check is unchanged.
- **`--env KEY=VAL` propagates to every remote rank over SSH.** `MLX_METAL_FAST_SYNCH=1` **must** be passed this way, *not* via the local `Popen(env=…)` — the remote ranks need it, and missing it causes 5–6× slower inference. (This corrects an earlier off-the-cuff assumption that it belonged in the subprocess env.)
- **Backends & host specification differ:**
  - `ring` (TCP, default): `--hosts ip1,ip2,ip3` (comma-separated IPs). Latency ~1ms ⇒ pipeline parallelism only; tensor's per-layer all-reduce is too chatty. Works on any LAN.
  - `jaccl` (RDMA/TB5): `--hostfile cluster.json` (fully-connected mesh + RDMA device matrix). Sub-50µs ⇒ tensor parallelism viable.
- **`--no-verify-script`** skips `mlx.launch`'s check that the target exists at the given path locally; the inner binary must exist at the **same path on every host** (homogeneous install assumption — documented as a gotcha).
- **Parallelism strategy:** tensor is the default; append `--pipeline` to the *inner* `mlx_lm.server` command for pipeline parallelism.

---

## What changes / what does NOT

| Component | Change? | Notes |
|---|---|---|
| `MlxServerSpec` | **Add fields** | `backend`, `hosts`, `hostfile`, `pipeline` (no `launch_env` — see audit) |
| `mlx_supervisor._build_cmd()` | **Wrap** | Split into `_inner_server_cmd()` + optional `mlx.launch` prefix; standalone byte-identical |
| `base_url` / health-poll host | **Decouple** | Poll `127.0.0.1`; bind `--host` separately — fixes latent `0.0.0.0` poll bug |
| Binary discovery | **Reuse `_which_extended`** | Resolve `mlx.launch`; re-point `find_mlx_lm_binary` at it too. No new discovery fn |
| `_binary_supports_kv_bits()` preflight | **Small tweak** | Probe the *inner* `mlx_lm.server` binary, not `mlx.launch` |
| `agent._parse_mlx_specs()` / `from_dict()` | **Parse new keys** | Per-server JSON gains `backend`/`hosts`/`hostfile`/`pipeline` |
| `MlxSupervisorStatus` + heartbeat | **Optional** | Add `distributed`/`node_count` for dashboard display |
| Health check (`/v1/models` poll) | **None** | Rank 0 HTTP is a drop-in |
| Restart / backoff / quarantine | **None** | Killing `mlx.launch` cascades to remote ranks |
| `MlxProxy` server-side routing | **None** | Still one URL per `mlx:` model |
| `mlx_client` heartbeat polling | **None** | Polls rank 0's `/v1/models` as today |
| Orphan detection | **Optional extend** | Also match stray `mlx.launch` on the port |

The blast radius is essentially one file (`mlx_supervisor.py`) plus config parsing in `agent.py`.

---

## Codebase audit — reuse these, avoid this debt

A pass over the MLX stack and adjacent plumbing (2026-07-13) surfaced existing patterns this plan must lean on, plus one latent bug it must fix. These findings override the first-draft design where they conflict.

### Reuse, don't duplicate

- **Binary discovery → `collector._which_extended()`.** It's the canonical, platform-aware (incl. Windows) resolver. `mlx_supervisor.find_mlx_lm_binary()` already *partially* duplicates it (Unix-only fallback list). **Do NOT add a `find_mlx_launch_binary()`** — that would be a third copy. Resolve both binaries via `_which_extended("mlx_lm.server")` / `_which_extended("mlx.launch")`. Since `_which_extended` is module-private in `collector.py`, either import it directly (same `node` package) or promote it to `common/` — prefer promoting to `common/binaries.py` so `mlx_supervisor` doesn't import from `collector` (which pulls heavier deps). Re-pointing `find_mlx_lm_binary` at it fixes its Windows gap for free.
- **Self-inclusion in the host list → `agent.get_local_ip()` (`common/system_metrics`).** The node already computes its own LAN IP for the LAN proxy. Reuse it so an operator lists only *peer* IPs and the node prepends itself — fewer hand-typed values, no drift between "what I bind" and "what I advertise as rank 0."
- **Config parsing → existing `MlxServerSpec.from_dict` + `FLEET_NODE_MLX_SERVERS`.** The new keys ride the per-server JSON. No new top-level env var, no new parser. ✅
- **Standalone `_build_cmd` output must stay byte-identical** so the existing `tests/test_node/test_mlx_supervisor.py::test_build_cmd_*` assertions pass untouched. Refactor into `_inner_server_cmd()` (today's body) + an optional `mlx.launch` prefix; the standalone branch returns exactly what it returns now.

### Fix a latent bug this plan forces into the open

- **`self.host` is overloaded** — [`base_url`](../../src/fleet_manager/node/mlx_supervisor.py) = `http://{self.host}:{port}` drives *both* the `--host` bind flag *and* every local health poll / warmup. Distributed **requires** binding `0.0.0.0` (rank 0 must be LAN-reachable — `registry.resolve_mlx_url`'s docstring already instructs `FLEET_NODE_MLX_BIND_HOST=0.0.0.0` for multi-node). That means **anyone already running multi-node Ollama-MLX aggregation with `bind_host=0.0.0.0` is polling `http://0.0.0.0/v1/models` today.** Decouple: add a health-poll host that is always `127.0.0.1` (rank 0 is local) while `--host` uses the configured bind. One-line property change; closes the existing issue and unblocks distributed.

### Deliberately NOT leveraged (and why)

- **The router→node command channel** ([`heartbeat.py` `commands`](../../src/fleet_manager/server/routes/heartbeat.py) → `agent._handle_command`) *could* deliver router-resolved peer IPs from the registry (the router is the only party that knows every node's `lan_ip`). We **don't** use it for cluster formation: it would couple MLX startup to a router round-trip and a bootstrap ordering dependency, violating the **node-sovereignty** design principle ("each node works standalone; router coordinates, never controls"). Host spec stays node-local. The registry knowledge is fair game only for a *read-only* convenience later (dashboard prints the ready-to-paste `--hosts` string), never for runtime coupling.

### Pre-existing debt noted (out of scope)

- Two MLX config paths already coexist: legacy single-server (`mlx_auto_start_model`) and `FLEET_NODE_MLX_SERVERS`. This plan adds nothing new (everything rides the JSON), but greenfield-wise the legacy path is a consolidation candidate — track separately, don't expand it.

---

## Design

### `MlxServerSpec` new fields

```python
@dataclass
class MlxServerSpec:
    model: str
    port: int
    kv_bits: int = 0
    prompt_cache_size: int = 4
    prompt_cache_bytes: int = 17_179_869_184
    draft_model: str = ""
    num_draft_tokens: int = 4
    # --- distributed (new) ---
    backend: str = ""          # "" = standalone (current behavior). "ring"|"jaccl"|"mpi"
    hosts: str = ""            # comma-separated peer IPs — ring backend (node prepends its own via get_local_ip)
    hostfile: str = ""         # path to JSON hostfile — jaccl/mpi backend
    pipeline: bool = False     # True = pipeline parallelism; False = tensor (default)
```

No `launch_env` field — `MLX_METAL_FAST_SYNCH=1` is auto-injected (see below) and an arbitrary env dict is unused surface. Add it only if a concrete need appears (e.g. `HF_HUB_OFFLINE=1` on air-gapped clusters), and even then a fixed allow-list beats a free-form dict.

Distributed mode is active iff `backend != ""`. `from_dict()` validates:
- `backend in {"", "ring", "jaccl", "mpi"}`
- `ring` requires `hosts`; `jaccl`/`mpi` require `hostfile` (fail loud on mismatch, consistent with the existing "fail loud on typo'd config" stance)
- warn (not fail) if `backend == "ring"` and `pipeline is False`, since ring+tensor is a foot-gun (too chatty over TCP)

### `_build_cmd()` output

**Standalone (unchanged):**
```
mlx_lm.server --model M --host 127.0.0.1 --port 11440 --prompt-cache-size 4 … [--kv-bits 8 …] [--draft-model …]
```

**Ring (LAN memory pooling):**
```
mlx.launch --backend ring --hosts 192.168.1.10,192.168.1.11 \
  --env MLX_METAL_FAST_SYNCH=1 --no-verify-script -- \
  mlx_lm.server --model M --host 0.0.0.0 --port 11440 --prompt-cache-size 4 … --pipeline
```

**JACCL (TB5 tensor parallel):**
```
mlx.launch --backend jaccl --hostfile /…/cluster.json \
  --env MLX_METAL_FAST_SYNCH=1 --no-verify-script -- \
  mlx_lm.server --model M --host 0.0.0.0 --port 11440 --prompt-cache-size 4 … [--kv-bits 8 …]
```

Notes:
- When distributed, bind the inner server to `0.0.0.0` (or the configured `bind_host`) so rank-0 is reachable, but keep the health-check target at `127.0.0.1:<port>` — this is the `self.host` decoupling from the audit section, mandatory here and a latent-bug fix regardless.
- `MLX_METAL_FAST_SYNCH=1` is always injected as the sole `--env` (via `mlx.launch --env`, never `Popen(env=…)` — remote ranks need it). Assert its presence in a unit test so it can't be dropped in a refactor.
- The `hosts` list the node passes to `mlx.launch` is `get_local_ip()` + the operator-configured peer IPs, deduped, self first.
- Speculative-decoding flags (`--draft-model`) still go on the inner command. (Draft models under distributed are unproven upstream — document as "leave draft empty for distributed until validated".)

### Config surface

No new top-level `FLEET_` env var needed — everything rides in the existing `FLEET_NODE_MLX_SERVERS` per-server JSON:

```json
[
  {
    "model": "mlx:Kimi-K2.6",
    "port": 11440,
    "backend": "ring",
    "hosts": "192.168.1.10,192.168.1.11",
    "pipeline": true
  }
]
```

This keeps the "one JSON array describes every MLX process on this node" model intact. `bind_host` is already a node-level setting (`FLEET_NODE_MLX_BIND_HOST`); distributed specs should default their inner `--host` to it (falling back to `0.0.0.0`).

---

## Implementation phases

### Phase 1 — Ring backend (LAN, usable today) ⭐ ship first

0. ✅ **DONE (2026-07-13):** promoted `_which_extended` → `common/binaries.py::which_extended`; `find_mlx_lm_binary` + `find_mlx_launch_binary` now wrap it. Health-poll host decoupled (`MlxSupervisor.health_host`), latent `0.0.0.0` bug fixed with regression tests. Node config consolidated to `FLEET_NODE_MLX_SERVERS`.
1. ✅ **DONE (2026-07-13):** `mlx_supervisor.py`:
   - Added `backend`/`hosts`/`hostfile`/`pipeline` to `MlxServerSpec` + `from_dict()` validation (rejects unknown backend; `ring` requires `hosts`, `jaccl`/`mpi` require `hostfile`; warns on `ring` without `pipeline`).
   - Split `_build_cmd()` into `_inner_server_cmd()` (standalone byte-identical) + `_launch_prefix()` (`mlx.launch --backend … --hosts/--hostfile … --env MLX_METAL_FAST_SYNCH=1 --no-verify-script --`). `--pipeline` rides the inner command. `_resolved_hosts()` prepends `get_local_ip()` to configured peers (deduped, self first).
   - `start()` preflights `find_mlx_launch_binary()` for distributed specs; memory gate skipped for distributed (node holds only a shard); `--kv-bits` preflight fixed to only gate when `kv_bits in (4,8)` so stock (unpatched) mlx serves f16 on peer nodes.
   - `MlxSupervisorSet._make_child` threads the 4 fields through. 12 new unit tests; full suite 1021 green.
2. ✅ `agent.py::_parse_mlx_specs` — no change needed; `from_dict` handles the new keys.
3. ✅ Docs: configuration-reference `FLEET_NODE_MLX_SERVERS` row documents the distributed keys + a ring example; CLAUDE.md gotcha bullet added.

**Remaining for Phase 1 exit:** on the real 512+128 LAN fleet, bring up a `ring`/`pipeline` entry and confirm the dashboard shows rank-0 healthy + a model larger than 128 GB serves a completion end-to-end. Blocked only on wiring the two Macs for the test (passwordless SSH + identical `mlx_lm.server` path). Unit-level behavior is fully covered. A `mlx-setup.md` "Distributed — ring backend over LAN" subsection should land alongside that hands-on validation.

### Phase 2 — status surfacing + JACCL backend (TB5, forward-looking)

1. `_build_cmd()` already handles `backend=="jaccl"` from Phase 1 (hostfile path). Verify the emitted command against the community repo's known-good invocation.
2. ✅ **DONE (2026-07-13):** `MlxSupervisorStatus`/`MlxServerInfo` gained `distributed` + `backend` + `node_count`. `MlxSupervisor.node_count` computes span from config (ring: distinct peers + self; jaccl/mpi: hostfile entry count; `0` = unknown, no network I/O). Threaded through `statuses()` → collector → heartbeat → dashboard SSE. Dashboard "Node Models" card renders a `backend · N nodes` chip per distributed server. Fields default back-compatibly so older-agent heartbeats still parse. 7 new unit tests + an end-to-end serialization smoke check.
3. Optional (still open): extend `find_orphan_mlx_pids_on_port` to also reap a stray `mlx.launch` bound to the port. In practice the local rank-0 `mlx_lm.server` child still matches the existing psutil filter (its cmdline contains `mlx_lm.server`), and killing it makes `mlx.launch` tear down the rest — so the gap is a lingering parent `mlx.launch` process, not a stuck port. Low priority.
4. Docs: `mlx-setup.md` "Distributed — JACCL over Thunderbolt 5" subsection covering RDMA enablement (Recovery mode, `rdma_ctl enable`), hostfile generation via `mlx.distributed_config`, and the mesh cabling table.

**Exit criteria:** status surfacing done + unit-tested; JACCL command construction validated only when TB5-meshed hardware exists.

### Phase 3 (optional) — cluster setup helper

`scripts/setup-mlx-cluster.sh` — wraps `mlx.distributed_config --auto-setup`, verifies passwordless SSH to each host, checks the `mlx_lm.server` path is identical across hosts, and prints the `FLEET_NODE_MLX_SERVERS` snippet to paste. Mirrors the ergonomics of the existing `scripts/setup-mlx.sh`.

---

## Testing plan

- **Unit (no cluster required):** `_build_cmd` matrix (standalone / ring / jaccl × tensor / pipeline × kv_bits), `from_dict` validation, health-poll-host decoupling, shared binary discovery. These fully cover the command-construction logic, which is where the risk is.
- **Integration (ring, LAN):** two Macs on the same subnet, passwordless SSH, identical `mlx_lm.server` path; bring up a pipeline-parallel server and hit `/v1/chat/completions` through the herd. No special hardware.
- **Integration (jaccl):** deferred until TB5 mesh + macOS 26.2 hardware is available.
- Keep the full suite green: `uv run pytest` (currently 1006 tests) and `uv run ruff check src/`.

---

## Non-goals (explicit)

- **No hostfile generation** in the herd. Operators supply it, or run `mlx.distributed_config` (optionally wrapped by the Phase 3 script). The herd consumes a path.
- **No RDMA enablement automation.** `rdma_ctl enable` requires macOS Recovery and physical access — impossible from a running agent. Document it; don't attempt it.
- **No remote-rank health modeling** beyond "rank 0 HTTP is up." `mlx.launch` already fails the whole job if a remote rank dies, which the supervisor detects as a local process exit.
- **No asymmetric-aware auto-sharding.** MLX decides sharding; the herd doesn't second-guess it. We only document the tensor-vs-pipeline tradeoff.
- **No change to `MlxProxy`, scoring, or the `mlx:` routing model.** A distributed server is one endpoint like any other.

---

## Risks & gotchas

- **Same-path requirement.** `mlx.launch` execs the inner command at an identical path on every host. Heterogeneous installs (Homebrew on one, uv-tool on another) break this. Mitigation: `--no-verify-script` + a documented "install `mlx-lm` the same way on every node" gotcha; the Phase 3 helper checks it.
- **Passwordless SSH + host-key trust** must be pre-established between nodes. Out of the herd's scope; documented as a prerequisite.
- **`MLX_METAL_FAST_SYNCH=1` via `--env`, never `Popen(env=…)`.** Encode this in `_build_cmd` so it can't be forgotten; assert it in a unit test.
- **Ring + tensor is a foot-gun.** Warn at config parse time; default distributed-ring specs toward `--pipeline`.
- **Draft/speculative-decoding under distributed is unvalidated** upstream — default docs to "no draft model in distributed mode" until proven.
- **Orphan detection gap.** A killed herd-node could leave a stray `mlx.launch` (and its SSH children) holding the port. Phase 2 extends the existing psutil orphan reaper to match `mlx.launch` as well as `mlx_lm.server`.
- **This is additive and low-risk, but low-urgency** unless/until (a) the operator wants LAN memory-pooling via ring today, or (b) symmetric multi-node / TB5 hardware arrives. Phase 1 is the only part with immediate real-world payoff on the current fleet.
```
