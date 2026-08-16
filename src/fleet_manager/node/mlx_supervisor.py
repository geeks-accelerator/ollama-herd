"""Subprocess lifecycle manager for a local `mlx_lm.server`.

Phase 3 of ``docs/plans/mlx-backend-for-large-models.md``.  Spawned by the
node agent for each ``FLEET_NODE_MLX_SERVERS`` entry (when
``FLEET_NODE_MLX_ENABLED`` is true) so users can bring up the whole fleet
(Ollama + herd-node + MLX) with a single ``uv run herd-node``.

What this module does:
  - Spawn ``mlx_lm.server`` as a child process with the configured flags
  - Wait for it to become healthy (``GET /v1/models`` → 200) before declaring ready
  - Monitor it in the background; restart with exponential backoff on crash
  - Route its stdout/stderr to ``~/.fleet-manager/logs/mlx-server-<port>.log``
    (one file per port so multi-MLX deploys don't interleave output)
  - Terminate cleanly on shutdown (SIGTERM → wait 5s → SIGKILL)

Intentional non-goals:
  - Not a fully-featured supervisor (no retry limits, no health-degradation
    scoring) — those belong in the router / health engine
  - Not responsible for routing — that's :class:`MlxProxy` on the server side
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from fleet_manager.common.binaries import which_extended

logger = logging.getLogger(__name__)

# Health-check cadence while waiting for the server to come up.
_HEALTH_POLL_INTERVAL = 2.0
_HEALTH_POLL_TIMEOUT = 120.0  # 2 min — big MLX models can take a while to mmap
# Restart backoff: 1s, 2s, 4s, ... capped at 60s
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0

# After a crash (esp. SIGKILL), the port isn't always immediately re-bindable:
# lingering TIME_WAIT entries from the 5s health-check connections can hold it
# for a moment, so the next mlx_lm.server bind hits EADDRINUSE, fails, and the
# supervisor churns through an extra restart cycle.  Before re-spawning we poll
# until the port is actually bindable (or this timeout elapses, after which we
# spawn anyway and let the health check surface the real state).  See the
# 2026-07-13 observation on port-release-wait hardening.
_PORT_FREE_POLL_INTERVAL = 0.25
_PORT_FREE_TIMEOUT = 10.0

# Quarantine threshold — if mlx_lm.server crashes this many times within
# a short window, we stop trying to restart at the normal cadence and
# back off to the quarantine interval below.  Without this, a persistent
# upstream bug (e.g. mlx-lm v0.31.3's load_default + tqdm threadpool
# race that caused 420 crash-restarts over 2.5 hours on 2026-04-26) burns
# CPU forever and floods the log.  See ``docs/observations.md`` 2026-04-26.
_QUARANTINE_FAILURE_COUNT = 5    # 5 crashes within the window
_QUARANTINE_WINDOW_S = 300.0     # = 5 minutes
_QUARANTINE_RESTART_INTERVAL = 600.0  # back off to "try once every 10 min"


@dataclass
class MlxServerSpec:
    """Configuration for a single mlx_lm.server subprocess.

    Each spec becomes one process on one port.  Multiple specs ⇒ multiple
    processes running concurrently (multi-MLX support — see
    ``docs/issues/multi-mlx-server-support.md``).
    """

    model: str                          # HF repo id or local path
    port: int                           # unique per-node
    kv_bits: int = 0                    # 0 / 4 / 8
    # Matches mlx_lm.server's own default.  We previously set 4 with no
    # recorded reason, which silently halved how many conversations could stay
    # warm: mlx_lm.server keeps N independent prompt caches and selects among
    # them by longest common prefix, so this is directly "how many concurrent
    # sessions keep their KV cache".  Session affinity pins a conversation to a
    # node, but the node can only honour that for as many sessions as it has
    # caches -- past N, a pin is a claim the backend cannot back.  Raise it
    # further on large-memory machines; each cache costs KV for its prefix.
    prompt_cache_size: int = 10
    prompt_cache_bytes: int = 17_179_869_184  # 16 GiB
    draft_model: str = ""
    num_draft_tokens: int = 4
    # --- distributed (multi-node via mlx.launch) ---
    # backend == "" ⇒ standalone single-process (current behavior).  Set to
    # "ring" (TCP/LAN, no special hardware) / "jaccl" (RDMA over Thunderbolt 5,
    # macOS 26.2+) / "mpi" to run the server across multiple hosts.  See
    # docs/plans/distributed-mlx-inference.md.
    backend: str = ""
    # comma-separated PEER IPs — ring backend (node prepends its own IP)
    hosts: str = ""
    hostfile: str = ""                  # path to JSON hostfile — jaccl / mpi backend
    pipeline: bool = False              # True = pipeline parallelism; False = tensor (default)

    _VALID_BACKENDS = ("", "ring", "jaccl", "mpi")

    @classmethod
    def from_dict(cls, data: dict) -> MlxServerSpec:
        """Build a spec from a JSON-dict, tolerating missing optional keys.

        Raises ``ValueError`` if the required ``model`` / ``port`` keys are
        missing or empty, or if a distributed ``backend`` is set without the
        host specification it needs — we fail loudly here so a typo'd
        ``FLEET_NODE_MLX_SERVERS`` doesn't silently swallow a server.
        """
        model = (data.get("model") or "").strip()
        if not model:
            raise ValueError(f"MlxServerSpec: missing 'model' key in {data!r}")
        port = data.get("port")
        if not isinstance(port, int) or port <= 0:
            raise ValueError(
                f"MlxServerSpec: missing or invalid 'port' in {data!r} — "
                f"must be a positive integer"
            )
        backend = str(data.get("backend", "")).strip().lower()
        if backend not in cls._VALID_BACKENDS:
            raise ValueError(
                f"MlxServerSpec: invalid backend {backend!r} in {data!r} — "
                f"must be one of ring / jaccl / mpi (or omitted for standalone)"
            )
        hosts = str(data.get("hosts", "")).strip()
        hostfile = str(data.get("hostfile", "")).strip()
        pipeline = bool(data.get("pipeline", False))
        if backend == "ring" and not hosts:
            raise ValueError(
                f"MlxServerSpec: backend 'ring' requires 'hosts' (comma-separated "
                f"peer IPs) in {data!r}"
            )
        if backend in ("jaccl", "mpi") and not hostfile:
            raise ValueError(
                f"MlxServerSpec: backend {backend!r} requires 'hostfile' (path to "
                f"a JSON hostfile) in {data!r}"
            )
        if backend == "ring" and not pipeline:
            # Ring is TCP (~1ms hop); tensor parallelism's per-layer all-reduce
            # is too chatty over it.  Pipeline is the practical mode.  Warn but
            # don't block — a tiny model on a fast LAN might still be fine.
            logger.warning(
                "MlxServerSpec(model=%r): backend 'ring' without pipeline=true — "
                "tensor parallelism over TCP is very slow; set \"pipeline\": true "
                "unless you know the model is small enough to tolerate it.",
                model,
            )
        return cls(
            model=model,
            port=port,
            kv_bits=int(data.get("kv_bits", 0)),
            prompt_cache_size=int(data.get("prompt_cache_size", 10)),
            prompt_cache_bytes=int(data.get("prompt_cache_bytes", 17_179_869_184)),
            draft_model=str(data.get("draft_model", "")),
            num_draft_tokens=int(data.get("num_draft_tokens", 4)),
            backend=backend,
            hosts=hosts,
            hostfile=hostfile,
            pipeline=pipeline,
        )


def estimate_model_size_gb(model: str) -> float:
    """Estimate an MLX model's disk footprint in GB by walking the HF cache.

    Used by the memory-pressure startup gate — if the model isn't cached we
    return 0.0 (unknown) so the caller can decide whether to proceed (default:
    proceed, because the user might just have pulled the model to a non-HF
    path).  Non-fatal — any I/O error returns 0.0 with a DEBUG log.

    HF cache layout:  ~/.cache/huggingface/hub/models--<owner>--<name>/blobs/
    We sum blobs/ only (not snapshots/) because snapshots are symlinks to the
    blobs — following them would double-count.
    """
    # "mlx-community/Qwen3-Coder-Next-4bit" → "models--mlx-community--Qwen3-Coder-Next-4bit"
    # "/abs/path" — caller passed a raw path, fall back to walking that
    if "/" in model and Path(model).exists():
        root = Path(model)
    else:
        dir_name = "models--" + model.replace("/", "--")
        root = Path.home() / ".cache" / "huggingface" / "hub" / dir_name
    if not root.exists():
        return 0.0
    blobs = root / "blobs"
    walk_root = blobs if blobs.exists() else root
    total = 0
    try:
        for entry in walk_root.iterdir() if walk_root == blobs else walk_root.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError as exc:
        logger.debug(f"estimate_model_size_gb({model!r}) walk failed: {exc}")
        return 0.0
    return total / (1024 ** 3)


def available_memory_gb() -> float:
    """Return ``psutil.virtual_memory().available`` in GB, or 0.0 on failure."""
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"available_memory_gb failed: {type(exc).__name__}: {exc}")
        return 0.0


def port_is_bindable(host: str, port: int) -> bool:
    """Return True if a fresh TCP socket can bind ``(host, port)`` right now.

    Mirrors the bind ``mlx_lm.server`` itself will attempt — a plain ``bind()``
    with **no** ``SO_REUSEADDR`` — so a True result means the child's bind will
    succeed too.  After a SIGKILL, TIME_WAIT entries left by the health-check
    connections can briefly hold the port; probing lets the supervisor wait the
    kernel out instead of eating an EADDRINUSE restart cycle.

    Never raises — any unexpected error returns ``True`` (don't block a spawn on
    a probe failure; let the real bind surface it).
    """
    import socket

    bind_host = "0.0.0.0" if host in ("", "0.0.0.0", "*") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((bind_host, port))
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 — probe must never block the spawn path
        return True
    finally:
        s.close()


def memory_gate_ok(
    model: str,
    headroom_gb: float,
) -> tuple[bool, str]:
    """Check whether a given model will fit in currently-available RAM.

    Returns ``(ok, reason)``.  ``reason`` is a human-readable string suitable
    for logging — empty if ``ok`` is True.

    Policy:
      - If we can't estimate the model size (not cached), proceed — log at
        DEBUG.  Operator explicitly pointed us at this model; don't block on
        incomplete info.
      - If we can't read available memory (psutil fails), proceed.
      - Otherwise require (estimated_size + headroom) <= available.
    """
    est_gb = estimate_model_size_gb(model)
    avail_gb = available_memory_gb()
    if est_gb <= 0.0:
        return True, ""  # unknown — don't block
    if avail_gb <= 0.0:
        return True, ""  # can't probe — don't block
    needed = est_gb + headroom_gb
    if needed > avail_gb:
        return False, (
            f"memory gate: {model!r} estimated {est_gb:.1f} GB + "
            f"{headroom_gb:.1f} GB headroom = {needed:.1f} GB needed, "
            f"but only {avail_gb:.1f} GB available"
        )
    return True, ""


def find_mlx_lm_binary() -> str | None:
    """Locate ``mlx_lm.server`` — returns an absolute path or None.

    Thin wrapper over the shared :func:`which_extended` resolver (checks
    ``$PATH`` then uv tool / pipx / Homebrew / user-local locations).  Returns
    ``None`` if mlx-lm isn't installed — the supervisor logs a clear error.
    """
    return which_extended("mlx_lm.server")


def find_mlx_launch_binary() -> str | None:
    """Locate ``mlx.launch`` — returns an absolute path or None.

    ``mlx.launch`` orchestrates distributed runs across multiple hosts; it
    installs from the same ``mlx`` package as ``mlx_lm.server`` and resolves
    via the same shared path logic.  Returns ``None`` when mlx isn't installed
    or is too old to ship the launcher.
    """
    return which_extended("mlx.launch")


def find_orphan_mlx_pids_on_port(port: int) -> list[int]:
    """Return PIDs of mlx_lm.server processes already bound to ``port``.

    Used by ``MlxSupervisor.start()`` to detect orphans left behind by a
    previous herd-node session — see the 2026-04-27 observation in
    ``docs/observations.md``.  Background: ``Popen(start_new_session=True)``
    makes spawned processes survive their parent's death (they get
    reparented to launchd).  If herd-node was killed without first killing
    its mlx_lm.server children, the originals stay alive holding the port.
    The next herd-node startup tries to spawn its own mlx_lm.server, fails
    to bind because the port's taken, exits rc=1 — and the supervisor's
    crash-loop logic kicks in against a process that doesn't exist while
    the orphan is doing the actual serving.

    Filter via psutil rather than parsing ``lsof`` output: psutil is
    already a hard dependency, returns structured data, and matches the
    process identity (mlx_lm.server) which an arbitrary "owns this port"
    check wouldn't (e.g., a user's manual `mlx_lm.server` invocation
    should still get killed; an unrelated service on the same port
    should NOT).
    """
    try:
        import psutil
    except ImportError:
        # psutil should always be present (core dep), but be defensive.
        logger.debug("psutil not available; skipping orphan check")
        return []

    matching: list[int] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # Identify mlx_lm.server processes specifically — filter by both
        # binary name and command-line args so we don't kill any random
        # process that happens to mention "mlx_lm" in its name.
        if not any("mlx_lm.server" in str(c) for c in cmdline):
            continue
        # Confirm the process is actually bound to OUR port via its connections.
        try:
            conns = proc.net_connections(kind="inet")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for c in conns:
            laddr = getattr(c, "laddr", None)
            if laddr and getattr(laddr, "port", None) == port:
                matching.append(proc.info["pid"])
                break
    return matching


class MlxSupervisor:
    """Owns the lifecycle of a local ``mlx_lm.server`` subprocess."""

    def __init__(
        self,
        *,
        model: str,
        port: int = 11440,
        host: str = "127.0.0.1",
        kv_bits: int = 0,
        prompt_cache_size: int = 10,
        prompt_cache_bytes: int = 17_179_869_184,
        draft_model: str = "",
        num_draft_tokens: int = 4,
        backend: str = "",
        hosts: str = "",
        hostfile: str = "",
        pipeline: bool = False,
        memory_headroom_gb: float = 0.0,
        log_dir: Path | None = None,
    ):
        self.model = model
        self.port = port
        self.host = host
        self.kv_bits = kv_bits
        self.prompt_cache_size = prompt_cache_size
        self.prompt_cache_bytes = prompt_cache_bytes
        # Speculative decoding — draft model + per-step proposal count.
        # Empty draft_model disables.  Must share the main's tokenizer.
        self.draft_model = draft_model
        self.num_draft_tokens = num_draft_tokens
        # Distributed execution — backend "" ⇒ standalone (one local process).
        # Otherwise the inner mlx_lm.server is wrapped in mlx.launch to run one
        # rank per host.  See docs/plans/distributed-mlx-inference.md.
        self.backend = backend
        self.hosts = hosts
        self.hostfile = hostfile
        self.pipeline = pipeline
        # Memory-pressure startup gate.  0.0 disables the check (back-compat).
        # The supervisor set passes the node-wide configured headroom.
        self.memory_headroom_gb = memory_headroom_gb
        self.log_dir = log_dir or (Path.home() / ".fleet-manager" / "logs")
        self._proc: subprocess.Popen | None = None
        self._monitor_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._log_fp = None
        # Last-known status reported back to the heartbeat builder.  One of:
        #   "starting"        — spawned, waiting for health
        #   "healthy"         — /v1/models returned 200 at last check
        #   "unhealthy"       — running but /v1/models failing
        #   "memory_blocked"  — start() refused due to memory gate
        #   "quarantined"     — too many crashes in a short window;
        #                       restarting at the slow quarantine cadence
        #                       (see _QUARANTINE_* constants) so we don't
        #                       burn CPU forever on a persistent upstream bug
        #   "stopped"         — gracefully terminated or never started
        self._status: str = "stopped"
        self._status_reason: str = ""
        self._last_ok_ts: float = 0.0
        # Crash-rate tracking for quarantine.  Append the timestamp of every
        # unexpected exit and prune entries older than _QUARANTINE_WINDOW_S
        # before each check.  When the count exceeds threshold, the monitor
        # switches to the slow restart interval and stays there until at
        # least one restart succeeds (process stays up for the full window).
        self._recent_crash_ts: list[float] = []
        self._quarantined: bool = False

    # Wildcard / unspecified bind addresses that are NOT valid to *connect* to.
    # ``mlx_lm.server`` binds these to accept LAN traffic (required for
    # multi-node aggregation and distributed rank-0), but a health probe must
    # dial a concrete loopback address — connecting to 0.0.0.0 / :: is
    # unreliable across platforms and plain wrong as a client target.
    _WILDCARD_BIND_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]", "*"})

    @property
    def health_host(self) -> str:
        """Loopback host to probe, decoupled from the bind (``--host``) address.

        ``self.host`` is the address ``mlx_lm.server`` *binds*; operators set it
        to ``0.0.0.0`` for LAN exposure (``FLEET_NODE_MLX_BIND_HOST=0.0.0.0``)
        and distributed mode requires it so rank-0 is reachable.  But rank-0 is
        always local to this node, so health polling / warmup must dial
        ``127.0.0.1`` — never the wildcard bind.  Prior to this split, setting a
        ``0.0.0.0`` bind silently made the supervisor poll ``http://0.0.0.0``,
        which is not a valid connect target.  See
        ``docs/plans/distributed-mlx-inference.md`` (latent-bug fix).
        """
        return "127.0.0.1" if self.host in self._WILDCARD_BIND_HOSTS else self.host

    @property
    def base_url(self) -> str:
        """Local URL used for health checks and warmup (always loopback-safe)."""
        return f"http://{self.health_host}:{self.port}"

    @staticmethod
    def _binary_supports_kv_bits(binary: str) -> bool:
        """Probe ``mlx_lm.server --help`` for ``--kv-bits`` support.

        Stock upstream mlx-lm omits this flag; ollama-herd patches it in via
        ``scripts/setup-mlx.sh``.  Checked once at startup so the supervisor
        can fail fast with a clear remediation hint rather than letting
        Popen + health-check timeout hide the real cause.
        """
        try:
            result = subprocess.run(
                [binary, "--help"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            # If we can't even probe, let the subsequent Popen surface the
            # real error — don't pre-emptively block auto-start.
            return True
        return "--kv-bits" in (result.stdout or "") + (result.stderr or "")

    @property
    def is_distributed(self) -> bool:
        """True when this server runs across multiple hosts via mlx.launch."""
        return bool(self.backend)

    @property
    def node_count(self) -> int:
        """How many hosts this server spans — 1 when standalone.

        Computed from config (no network I/O — called per heartbeat):
          - ring: distinct configured peers + this node (self).
          - jaccl / mpi: number of entries in the hostfile JSON array.
          - unreadable / unparseable hostfile: 0 (unknown), so the dashboard
            can show "distributed" without asserting a wrong count.
        """
        if not self.is_distributed:
            return 1
        if self.hostfile:
            try:
                import json
                with open(self.hostfile) as f:
                    data = json.load(f)
                return len(data) if isinstance(data, list) else 0
            except (OSError, ValueError):
                return 0
        peers = {h.strip() for h in self.hosts.split(",") if h.strip()}
        return len(peers) + 1  # peers + self

    def _inner_server_cmd(self, binary: str) -> list[str]:
        """Build the ``mlx_lm.server`` invocation (rank command in distributed).

        This is the standalone command verbatim — kept byte-identical so
        non-distributed deploys and their tests are unaffected.  ``--pipeline``
        is appended only in distributed mode (it's meaningless standalone).
        """
        cmd = [
            binary,
            "--model", self.model,
            "--host", self.host,
            "--port", str(self.port),
            "--prompt-cache-size", str(self.prompt_cache_size),
            "--prompt-cache-bytes", str(self.prompt_cache_bytes),
            "--log-level", "INFO",
        ]
        if self.kv_bits in (4, 8):
            # Requires our patched mlx_lm.server (or upstream PR #1073 / #934).
            # Stock mlx_lm.server will reject this flag.
            cmd += ["--kv-bits", str(self.kv_bits), "--kv-group-size", "64"]
        # Speculative decoding — only add flags when configured.  Main +
        # draft must share the same tokenizer family or acceptance rate
        # collapses.  See docs/plans/claude-code-performance-improvements.md.
        if self.draft_model:
            cmd += [
                "--draft-model", self.draft_model,
                "--num-draft-tokens", str(self.num_draft_tokens),
            ]
        if self.is_distributed and self.pipeline:
            cmd += ["--pipeline"]
        return cmd

    def _resolved_hosts(self) -> str:
        """Comma-separated host list for ``mlx.launch --hosts`` (ring backend).

        The operator configures PEER IPs; we prepend this node's own LAN IP so
        the head node (where mlx.launch runs) is rank 0.  Deduplicated, self
        first.  See docs/plans/distributed-mlx-inference.md (audit: reuse
        get_local_ip, don't make operators hand-type their own address).
        """
        from fleet_manager.common.system_metrics import get_local_ip

        ordered: list[str] = []
        local = get_local_ip()
        if local:
            ordered.append(local)
        for peer in (h.strip() for h in self.hosts.split(",")):
            if peer and peer not in ordered:
                ordered.append(peer)
        return ",".join(ordered)

    def _launch_prefix(self) -> list[str]:
        """The ``mlx.launch … --`` prefix that wraps the inner server command.

        Raises ``RuntimeError`` if ``mlx.launch`` isn't installed — callers
        (``start`` preflights it; ``_monitor`` catches it) surface a clear
        status.  ``MLX_METAL_FAST_SYNCH=1`` is passed via ``--env`` (never the
        subprocess env) so it reaches every remote rank; without it inference
        runs 5–6× slower.
        """
        launch = find_mlx_launch_binary()
        if launch is None:
            raise RuntimeError(
                "mlx.launch not found — distributed MLX needs the mlx package's "
                "launcher on PATH (install/upgrade `mlx`). "
                "Install with `uv tool install mlx` or `pip install mlx`."
            )
        prefix = [launch, "--backend", self.backend]
        if self.hostfile:
            prefix += ["--hostfile", self.hostfile]
        elif self.hosts:
            prefix += ["--hosts", self._resolved_hosts()]
        prefix += [
            "--env", "MLX_METAL_FAST_SYNCH=1",
            "--no-verify-script",
            "--",
        ]
        return prefix

    def _build_cmd(self, binary: str) -> list[str]:
        """Build the process command line.

        ``binary`` is the resolved ``mlx_lm.server`` path.  Standalone: that
        invocation verbatim.  Distributed: the same invocation wrapped in an
        ``mlx.launch`` prefix that runs it as one rank per host.
        """
        inner = self._inner_server_cmd(binary)
        if not self.is_distributed:
            return inner
        return self._launch_prefix() + inner

    async def _wait_port_free(self, timeout: float = _PORT_FREE_TIMEOUT) -> bool:
        """Poll until the configured port is bindable, or ``timeout`` elapses.

        Called before a (re)spawn so the child's bind doesn't race a port the
        kernel hasn't released yet after a crash/SIGKILL.  Returns True if the
        port came free, False on timeout (caller spawns anyway — the health
        check will surface a genuine bind failure).  Honors the stop event.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._stop.is_set():
                return False
            if port_is_bindable(self.host, self.port):
                return True
            await asyncio.sleep(_PORT_FREE_POLL_INTERVAL)
        logger.warning(
            "mlx_lm.server(port=%d) port still occupied after %.0fs wait; "
            "spawning anyway.", self.port, timeout,
        )
        return False

    async def _wait_healthy(self, timeout: float = _HEALTH_POLL_TIMEOUT) -> bool:
        """Poll ``GET /v1/models`` until it returns 200 or timeout expires."""
        url = f"{self.base_url}/v1/models"
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(timeout=3.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self._stop.is_set():
                    return False
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return True
                except Exception:  # noqa: BLE001 — connect errors expected while booting
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        return False

    def _open_log(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # One log file per port so multi-MLX deploys don't interleave output.
        # Older docs reference ``mlx-server.log``; from 2026-04-24 onward
        # it's ``mlx-server-<port>.log`` (e.g. ``mlx-server-11440.log``)
        # so `tail -f ~/.fleet-manager/logs/mlx-server-*.log` Just Works.
        log_path = self.log_dir / f"mlx-server-{self.port}.log"
        # Line-buffered so we see partial output when debugging
        return open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")

    async def start(self) -> bool:
        """Spawn the subprocess and wait for it to become healthy.

        Returns True on success, False if the binary is missing or didn't
        come up within the health-check timeout.  Starts the monitor task
        on success so crashes trigger restarts.
        """
        binary = find_mlx_lm_binary()
        if binary is None:
            logger.error(
                "mlx_lm.server binary not found — install with "
                "`uv tool install mlx-lm` or `pip install mlx-lm`. "
                "Skipping MLX auto-start."
            )
            self._status = "stopped"
            self._status_reason = "mlx_lm.server binary not found"
            return False

        if not self.model:
            logger.error(
                "MLX server spec has an empty 'model' — set it in a "
                "FLEET_NODE_MLX_SERVERS entry to a local model path or Hugging "
                "Face repo id (e.g. 'mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit'). "
                "Skipping MLX auto-start."
            )
            self._status = "stopped"
            self._status_reason = "no model configured"
            return False

        # Memory-pressure startup gate.  Skip only if explicitly configured
        # with a positive headroom.  See memory_gate_ok() docstring for
        # policy.  When it blocks, we DO NOT crash-loop — just stay in
        # "memory_blocked" state and let an outer retry (or manual op)
        # bring us up later.
        #
        # Skipped for distributed servers: this node holds only a *shard* of
        # the model (that's the whole point — the model is bigger than one
        # node), so gating on the full weight size would wrongly refuse valid
        # clusters.  mlx.launch / the OS surface real OOM if the shard doesn't
        # fit.
        if self.memory_headroom_gb > 0.0 and not self.is_distributed:
            ok, reason = memory_gate_ok(self.model, self.memory_headroom_gb)
            if not ok:
                logger.warning(
                    "mlx_lm.server(port=%d, model=%r): %s. Skipping start; "
                    "supervisor will retry on the next node-level pass.",
                    self.port, self.model, reason,
                )
                self._status = "memory_blocked"
                self._status_reason = reason
                return False

        # Orphan detection: if a previous herd-node session was killed
        # without also killing its mlx_lm.server children, those orphans
        # are still bound to our configured port.  Our Popen would fail
        # to bind, exit rc=1, and the crash-loop logic would log
        # "QUARANTINED" forever against a process that's actually fine
        # (the orphan keeps serving).  See ``docs/observations.md``
        # 2026-04-27.  Detect via psutil and SIGKILL before spawning.
        orphan_pids = find_orphan_mlx_pids_on_port(self.port)
        if orphan_pids:
            logger.warning(
                "mlx_lm.server orphan(s) found on port %d: PIDs %s. "
                "Killing them before spawning a fresh process — these "
                "are leftover from a previous herd-node session that "
                "didn't tear down its MLX children.  If you intended "
                "those to keep running, stop herd-node before "
                "restarting them manually.",
                self.port, orphan_pids,
            )
            for pid in orphan_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    logger.error(
                        "Cannot kill orphan mlx_lm.server PID %d (permission "
                        "denied).  Either kill it manually or restart from a "
                        "shell that owns the process.",
                        pid,
                    )
                    self._status = "stopped"
                    self._status_reason = (
                        f"orphan mlx_lm.server on port {self.port} "
                        f"(PID {pid}) blocks bind and we lack permission "
                        "to kill it"
                    )
                    return False
            # Give the OS a moment to release the port after SIGKILL
            await asyncio.sleep(1.0)

        # Preflight: if the user asked for KV quantization but the installed
        # mlx_lm.server doesn't expose --kv-bits, fail fast with actionable
        # guidance instead of letting Popen surface as a 120s health-check
        # timeout.  Upstream mlx-lm drops this flag; we depend on a local
        # patch (see ``docs/experiments/mlx-lm-server-kv-bits.patch``).  Only
        # gate when quantization is actually requested (4/8) — stock mlx-lm
        # without the patch must still serve f16 (kv_bits=0), which matters for
        # distributed nodes that don't run the patch.
        if self.kv_bits in (4, 8) and not self._binary_supports_kv_bits(binary):
            logger.error(
                "mlx_lm.server at %s does not support --kv-bits — the "
                "ollama-herd KV-quant patch is missing (likely wiped by a "
                "fresh `uv tool install mlx-lm`). Re-run "
                "`./scripts/setup-mlx.sh` from the repo root to reapply. "
                "Skipping MLX auto-start.",
                binary,
            )
            self._status = "stopped"
            self._status_reason = "mlx_lm.server missing --kv-bits patch"
            return False

        # Preflight: distributed mode needs the mlx.launch launcher on PATH.
        # Fail fast with a clear status rather than raising inside _build_cmd.
        if self.is_distributed and find_mlx_launch_binary() is None:
            logger.error(
                "mlx_lm.server(port=%d) configured for distributed backend %r "
                "but `mlx.launch` was not found on PATH. Install/upgrade the "
                "mlx package (`uv tool install mlx`). Skipping MLX start.",
                self.port, self.backend,
            )
            self._status = "stopped"
            self._status_reason = "mlx.launch not found (distributed backend)"
            return False

        cmd = self._build_cmd(binary)
        self._log_fp = self._open_log()
        self._status = "starting"
        self._status_reason = ""
        _bind_note = (
            f" (bind {self.host}, health-poll {self.health_host})"
            if self.host in self._WILDCARD_BIND_HOSTS
            else ""
        )
        logger.info(
            f"Starting mlx_lm.server on {self.host}:{self.port}{_bind_note} "
            f"(model={self.model}, kv_bits={self.kv_bits or 'f16'})"
        )
        logger.debug(f"mlx cmd: {' '.join(cmd)}")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=self._log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group so SIGTERM hits just us
            )
        except FileNotFoundError:
            logger.error(f"mlx_lm.server not runnable at {binary}")
            self._log_fp.close()
            self._log_fp = None
            self._status = "stopped"
            self._status_reason = "binary vanished after find"
            return False

        if not await self._wait_healthy():
            logger.error(
                f"mlx_lm.server(port={self.port}) failed to become healthy "
                f"within {_HEALTH_POLL_TIMEOUT:.0f}s. Killing and giving up for now."
            )
            await self._terminate()
            self._status = "stopped"
            self._status_reason = "did not become healthy within timeout"
            return False

        self._status = "healthy"
        self._last_ok_ts = time.time()
        logger.info(f"mlx_lm.server(port={self.port}) healthy at {self.base_url}")
        # Fire a small warmup request so the KV cache has a usable starting
        # state before real traffic hits.  ``waybarrios/vllm-mlx`` reports
        # 1.3–2.25× TTFT improvement from this pattern; mlx_lm.server
        # doesn't do it natively.  Failure is non-fatal — model still
        # serves real requests, just with an extra cold prefill on turn 1.
        asyncio.create_task(
            self._warmup_prompt_cache(),
            name="mlx-supervisor-warmup",
        )
        # Start monitor task to restart on crash
        self._monitor_task = asyncio.create_task(
            self._monitor(), name="mlx-supervisor-monitor",
        )
        return True

    async def _warmup_prompt_cache(self) -> None:
        """Send a short chat completion to pre-warm the MLX prompt cache.

        Fired post-startup so the first real user request doesn't pay
        the cold-prefill cost for the tokenizer / attention buffers.
        Non-fatal — any failure is logged at DEBUG and the supervisor
        carries on.
        """
        import httpx  # local import — supervisor is used in node-only paths

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "user", "content": "hi"},
                        ],
                        "max_tokens": 1,
                        "stream": False,
                    },
                )
                if resp.status_code == 200:
                    logger.info(
                        "mlx_lm.server warmup complete — prompt cache primed",
                    )
                else:
                    logger.debug(
                        f"mlx_lm.server warmup got {resp.status_code} "
                        f"(non-fatal): {resp.text[:200]}",
                    )
        except Exception as exc:  # noqa: BLE001 — warmup must fail-open
            logger.debug(
                f"mlx_lm.server warmup failed ({type(exc).__name__}: {exc}) "
                "— real traffic will pay the first cold prefill instead",
            )

    def _record_crash_and_check_quarantine(self) -> None:
        """Append the current crash time and decide whether to enter quarantine.

        Quarantine triggers when more than ``_QUARANTINE_FAILURE_COUNT`` crashes
        happen within a rolling ``_QUARANTINE_WINDOW_S`` window.  Once
        quarantined, the monitor switches to ``_QUARANTINE_RESTART_INTERVAL``
        between restart attempts (vs the normal exponential backoff capped at
        ``_BACKOFF_MAX``).  A single successful restart that stays up past the
        window clears quarantine.

        Why this exists: 2026-04-26, mlx-lm v0.31.3's ``load_default``+
        ``snapshot_download``+``thread_map`` chain entered a state where
        every chat-completion request crashed the process.  The supervisor
        restarted 420 times over 2.5 hours at 60 s intervals — burning agent
        CPU, flooding logs, and never escaping.  See observation in
        ``docs/observations.md``.
        """
        now = time.monotonic()
        self._recent_crash_ts.append(now)
        # Prune anything older than the window
        cutoff = now - _QUARANTINE_WINDOW_S
        self._recent_crash_ts = [t for t in self._recent_crash_ts if t >= cutoff]
        if len(self._recent_crash_ts) >= _QUARANTINE_FAILURE_COUNT:
            if not self._quarantined:
                logger.error(
                    "mlx_lm.server(port=%d, model=%r) entered QUARANTINE "
                    "after %d crashes within %.0fs.  Backing off restart "
                    "cadence to once every %.0fs.  An upstream bug or "
                    "model corruption is likely; check "
                    "~/.fleet-manager/logs/mlx-server-%d.log for stack "
                    "traces.  Quarantine clears once a restart stays up "
                    "for the full window.",
                    self.port, self.model,
                    len(self._recent_crash_ts), _QUARANTINE_WINDOW_S,
                    _QUARANTINE_RESTART_INTERVAL, self.port,
                )
            self._quarantined = True

    async def _monitor(self) -> None:
        """Watch the subprocess and restart it on unexpected exit.

        Restart cadence:
          - Normal: exponential backoff 1 s → 2 s → 4 s → ... → 60 s cap.
          - Quarantine (after ``_QUARANTINE_FAILURE_COUNT`` crashes within
            ``_QUARANTINE_WINDOW_S``): fixed ``_QUARANTINE_RESTART_INTERVAL``
            so a persistent upstream bug doesn't burn CPU forever.
        """
        backoff = _BACKOFF_INITIAL
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            if self._proc is None:
                return
            rc = self._proc.poll()
            if rc is None:
                # Still running — reset backoff once we've been up for a while.
                # Also opportunistically clear quarantine if we've been up
                # for the full quarantine window without crashing.
                backoff = _BACKOFF_INITIAL
                if self._quarantined and self._last_ok_ts > 0 and (
                    time.time() - self._last_ok_ts > _QUARANTINE_WINDOW_S
                ):
                    logger.info(
                        "mlx_lm.server(port=%d) recovered: stayed up %.0fs "
                        "without crashing, exiting QUARANTINE.",
                        self.port, time.time() - self._last_ok_ts,
                    )
                    self._quarantined = False
                    self._recent_crash_ts.clear()
                continue
            if self._stop.is_set():
                return

            # Process crashed.  Record it and decide cadence.
            self._record_crash_and_check_quarantine()
            if self._quarantined:
                wait_s = _QUARANTINE_RESTART_INTERVAL
                self._status = "quarantined"
                self._status_reason = (
                    f"{len(self._recent_crash_ts)} crashes within "
                    f"{_QUARANTINE_WINDOW_S:.0f}s; next restart in "
                    f"{wait_s:.0f}s"
                )
                logger.warning(
                    f"mlx_lm.server(port={self.port}) exited unexpectedly "
                    f"(rc={rc}); QUARANTINED — restarting in {wait_s:.0f}s"
                )
            else:
                wait_s = backoff
                self._status = "unhealthy"
                self._status_reason = f"subprocess exited rc={rc}"
                logger.warning(
                    f"mlx_lm.server(port={self.port}) exited unexpectedly "
                    f"(rc={rc}); restarting in {wait_s:.1f}s"
                )

            await asyncio.sleep(wait_s)
            backoff = min(backoff * 2, _BACKOFF_MAX)
            binary = find_mlx_lm_binary()
            if binary is None:
                logger.error("mlx_lm.server binary disappeared; giving up on restart")
                self._status = "stopped"
                self._status_reason = "binary disappeared"
                return
            # Wait out any lingering hold on the port from the just-crashed
            # process (TIME_WAIT after SIGKILL) so the child's bind doesn't
            # eat an EADDRINUSE and force another restart cycle.
            await self._wait_port_free()
            if self._stop.is_set():
                return
            try:
                self._proc = subprocess.Popen(
                    self._build_cmd(binary),
                    stdout=self._log_fp,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._status = "starting"
                if await self._wait_healthy(timeout=60.0):
                    logger.info(f"mlx_lm.server(port={self.port}) restarted successfully")
                    self._status = "healthy"
                    self._last_ok_ts = time.time()
                else:
                    logger.warning(
                        f"mlx_lm.server(port={self.port}) restarted but "
                        "didn't go healthy in time"
                    )
                    self._status = "unhealthy"
                    self._status_reason = "restart health check timed out"
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"mlx_lm.server(port={self.port}) restart failed: {exc}")
                self._status = "unhealthy"
                self._status_reason = f"restart exception: {type(exc).__name__}"

    def status(self) -> str:
        """Return the supervisor's current status string."""
        return self._status

    def status_reason(self) -> str:
        """Return the reason string that accompanies the current status."""
        return self._status_reason

    def last_ok_ts(self) -> float:
        """Return the epoch timestamp of the last successful health check."""
        return self._last_ok_ts

    async def poll_health(self, timeout: float = 3.0) -> bool:
        """Hit /v1/models once and update last_ok_ts / status accordingly.

        Used by the supervisor set for heartbeat-time status refresh so the
        dashboard reflects live health, not just startup health.  Returns
        True iff the server responded 200.  Never raises.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self.base_url}/v1/models")
                if resp.status_code == 200:
                    self._last_ok_ts = time.time()
                    if self._status != "healthy":
                        self._status = "healthy"
                        self._status_reason = ""
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"mlx_lm.server(port={self.port}) poll_health: "
                f"{type(exc).__name__}: {exc}"
            )
        # Non-200 / exception
        if self._proc is not None and self._proc.poll() is None:
            # Process running but not responding → mark unhealthy for the
            # dashboard/heartbeat ONLY.  Nothing here (or in the monitor)
            # restarts a running-but-unhealthy server — `_monitor` acts solely
            # on an actual process exit (rc is not None).  A slow health poll
            # under whole-box load is contention, not a hang; treating it as a
            # kill trigger would needlessly churn an idle server.  See
            # docs/issues.md "An idle MLX server gets externally SIGKILLed".
            self._status = "unhealthy"
            self._status_reason = "health check failed while process running"
        else:
            self._status = "stopped"
            self._status_reason = "process not running"
        return False

    async def _terminate(self) -> None:
        """Kill the subprocess gracefully (SIGTERM → wait 5s → SIGKILL)."""
        if self._proc is None:
            return
        rc = self._proc.poll()
        if rc is not None:
            self._proc = None
            return
        try:
            # Signal the whole process group (start_new_session=True gave us one)
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            # Process already gone or we can't see it
            pass
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._proc and self._proc.wait(timeout=5),
            )
        except subprocess.TimeoutExpired:
            logger.warning("mlx_lm.server didn't exit in 5s; sending SIGKILL")
            try:
                pgid = os.getpgid(self._proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._proc = None

    async def stop(self) -> None:
        """Stop the supervisor and terminate the subprocess."""
        import contextlib

        self._stop.set()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._monitor_task
            self._monitor_task = None
        await self._terminate()
        self._status = "stopped"
        self._status_reason = "stopped by supervisor set"
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            finally:
                self._log_fp = None


# ---------------------------------------------------------------------------
# Multi-server: supervisor set
# ---------------------------------------------------------------------------


@dataclass
class MlxSupervisorStatus:
    """Snapshot of one supervisor for inclusion in the node heartbeat."""

    port: int
    model: str
    status: str                          # healthy/starting/unhealthy/memory_blocked/stopped
    status_reason: str = ""
    kv_bits: int = 0
    model_size_gb: float = 0.0           # estimated from HF cache on disk
    last_ok_ts: float = 0.0              # epoch seconds
    # Distributed execution (multi-node via mlx.launch).  backend == "" ⇒
    # standalone; node_count is 1 in that case.
    distributed: bool = False
    backend: str = ""                    # ring / jaccl / mpi (empty = standalone)
    node_count: int = 1                  # hosts this server spans (0 = unknown)


class MlxSupervisorSet:
    """Manages N concurrent mlx_lm.server subprocesses, one per spec.

    Parallel start / parallel stop.  One failure doesn't block the others.
    Exposes per-server status snapshots so the heartbeat can publish them
    and the dashboard can render per-URL health.

    **Why this layer exists**: `MlxSupervisor` speaks to one subprocess on
    one port; when operators run multiple MLX models concurrently (e.g.
    main + smaller compactor-dedicated model), we need coordinated
    lifecycle + aggregated status without coupling the subprocess class
    to multi-instance concerns.  See
    ``docs/issues/multi-mlx-server-support.md``.
    """

    def __init__(
        self,
        specs: list[MlxServerSpec],
        *,
        bind_host: str = "127.0.0.1",
        memory_headroom_gb: float = 10.0,
        log_dir: Path | None = None,
    ):
        self.specs = specs
        self.bind_host = bind_host
        self.memory_headroom_gb = memory_headroom_gb
        self.log_dir = log_dir or (Path.home() / ".fleet-manager" / "logs")
        # Supervisors keyed by port so lookup is stable across restarts.
        self._children: dict[int, MlxSupervisor] = {}

    def _make_child(self, spec: MlxServerSpec) -> MlxSupervisor:
        return MlxSupervisor(
            model=spec.model,
            port=spec.port,
            host=self.bind_host,
            kv_bits=spec.kv_bits,
            prompt_cache_size=spec.prompt_cache_size,
            prompt_cache_bytes=spec.prompt_cache_bytes,
            draft_model=spec.draft_model,
            num_draft_tokens=spec.num_draft_tokens,
            backend=spec.backend,
            hosts=spec.hosts,
            hostfile=spec.hostfile,
            pipeline=spec.pipeline,
            memory_headroom_gb=self.memory_headroom_gb,
            log_dir=self.log_dir,
        )

    async def start_all(self) -> dict[int, bool]:
        """Start every spec's subprocess in parallel.

        Returns a ``{port: started_ok}`` map.  A failed start leaves the
        child in place (its status reflects the failure) so the set still
        reports it via ``statuses()`` — the dashboard shows "memory_blocked"
        or "stopped" rather than the server silently not existing.
        """
        if not self.specs:
            return {}
        # Check for duplicate ports — a common env mistake that would
        # silently have the second process crash on EADDRINUSE.
        seen_ports: set[int] = set()
        deduped: list[MlxServerSpec] = []
        for spec in self.specs:
            if spec.port in seen_ports:
                logger.error(
                    "mlx supervisor set: duplicate port %d in config "
                    "(%r); skipping second entry.  Fix FLEET_NODE_MLX_SERVERS.",
                    spec.port, spec.model,
                )
                continue
            seen_ports.add(spec.port)
            deduped.append(spec)

        for spec in deduped:
            if spec.port not in self._children:
                self._children[spec.port] = self._make_child(spec)

        # Spawn in parallel — one slow-loading model shouldn't delay the rest.
        results = await asyncio.gather(
            *(child.start() for child in self._children.values()),
            return_exceptions=True,
        )
        out: dict[int, bool] = {}
        for port, result in zip(self._children.keys(), results, strict=False):
            if isinstance(result, Exception):
                logger.error(
                    f"mlx supervisor set: port {port} start raised "
                    f"{type(result).__name__}: {result}",
                )
                out[port] = False
            else:
                out[port] = bool(result)
        return out

    async def stop_all(self) -> None:
        """Stop every supervisor in parallel."""
        if not self._children:
            return
        await asyncio.gather(
            *(child.stop() for child in self._children.values()),
            return_exceptions=True,
        )

    async def refresh_health(self) -> None:
        """Poll /v1/models on each child once, updating their status.

        Cheap to call on every heartbeat tick; gives the dashboard
        sub-heartbeat-interval accuracy on server health.
        """
        if not self._children:
            return
        await asyncio.gather(
            *(child.poll_health() for child in self._children.values()),
            return_exceptions=True,
        )

    def statuses(self) -> list[MlxSupervisorStatus]:
        """Return one MlxSupervisorStatus per managed server.

        Includes servers that failed to start (status="memory_blocked" or
        "stopped"), so the heartbeat can surface them to operators.
        """
        out: list[MlxSupervisorStatus] = []
        for spec in self.specs:
            child = self._children.get(spec.port)
            if child is None:
                # Shouldn't happen after start_all, but tolerate it
                out.append(MlxSupervisorStatus(
                    port=spec.port,
                    model=spec.model,
                    status="stopped",
                    kv_bits=spec.kv_bits,
                    model_size_gb=estimate_model_size_gb(spec.model),
                    distributed=bool(spec.backend),
                    backend=spec.backend,
                ))
                continue
            out.append(MlxSupervisorStatus(
                port=child.port,
                model=child.model,
                status=child.status(),
                status_reason=child.status_reason(),
                kv_bits=child.kv_bits,
                model_size_gb=estimate_model_size_gb(child.model),
                last_ok_ts=child.last_ok_ts(),
                distributed=child.is_distributed,
                backend=child.backend,
                node_count=child.node_count,
            ))
        return out

    def healthy_models(self) -> dict[str, int]:
        """Return ``{model: port}`` for supervisors reporting healthy.

        Used by the heartbeat builder to assemble the `mlx_models` list —
        only healthy servers are advertised as serveable.
        """
        out: dict[str, int] = {}
        for child in self._children.values():
            if child.status() == "healthy":
                out[child.model] = child.port
        return out
