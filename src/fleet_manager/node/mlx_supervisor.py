"""Subprocess lifecycle manager for a local `mlx_lm.server`.

Phase 3 of ``docs/plans/mlx-backend-for-large-models.md``.  Spawned by the
node agent when ``FLEET_NODE_MLX_AUTO_START`` is true so users can bring up
the whole fleet (Ollama + herd-node + MLX) with a single ``uv run herd-node``.

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
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Health-check cadence while waiting for the server to come up.
_HEALTH_POLL_INTERVAL = 2.0
_HEALTH_POLL_TIMEOUT = 120.0  # 2 min — big MLX models can take a while to mmap
# Restart backoff: 1s, 2s, 4s, ... capped at 60s
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


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
    prompt_cache_size: int = 4
    prompt_cache_bytes: int = 17_179_869_184  # 16 GiB
    draft_model: str = ""
    num_draft_tokens: int = 4

    @classmethod
    def from_dict(cls, data: dict) -> MlxServerSpec:
        """Build a spec from a JSON-dict, tolerating missing optional keys.

        Raises ``ValueError`` if the required ``model`` / ``port`` keys are
        missing or empty — we fail loudly here so a typo'd
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
        return cls(
            model=model,
            port=port,
            kv_bits=int(data.get("kv_bits", 0)),
            prompt_cache_size=int(data.get("prompt_cache_size", 4)),
            prompt_cache_bytes=int(data.get("prompt_cache_bytes", 17_179_869_184)),
            draft_model=str(data.get("draft_model", "")),
            num_draft_tokens=int(data.get("num_draft_tokens", 4)),
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

    Checks ``$PATH`` first, then falls back to common install locations
    (uv tool, pipx, Homebrew, user-local bin).  Returns ``None`` if mlx-lm
    isn't installed — the supervisor will log a clear error in that case.
    """
    found = shutil.which("mlx_lm.server")
    if found:
        return found
    # Common install locations — keep in sync with collector._which_extended
    for candidate in [
        Path.home() / ".local" / "bin" / "mlx_lm.server",
        Path("/opt/homebrew/bin/mlx_lm.server"),
        Path("/usr/local/bin/mlx_lm.server"),
    ]:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class MlxSupervisor:
    """Owns the lifecycle of a local ``mlx_lm.server`` subprocess."""

    def __init__(
        self,
        *,
        model: str,
        port: int = 11440,
        host: str = "127.0.0.1",
        kv_bits: int = 0,
        prompt_cache_size: int = 4,
        prompt_cache_bytes: int = 17_179_869_184,
        draft_model: str = "",
        num_draft_tokens: int = 4,
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
        #   "stopped"         — gracefully terminated or never started
        self._status: str = "stopped"
        self._status_reason: str = ""
        self._last_ok_ts: float = 0.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

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

    def _build_cmd(self, binary: str) -> list[str]:
        """Build the mlx_lm.server command line from configured flags."""
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
        return cmd

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
                "FLEET_NODE_MLX_AUTO_START_MODEL is empty — set it to a "
                "local model path or Hugging Face repo id (e.g. "
                "'mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit'). "
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
        if self.memory_headroom_gb > 0.0:
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

        # Preflight: if the user asked for KV quantization but the installed
        # mlx_lm.server doesn't expose --kv-bits, fail fast with actionable
        # guidance instead of letting Popen surface as a 120s health-check
        # timeout.  Upstream mlx-lm drops this flag; we depend on a local
        # patch (see ``docs/experiments/mlx-lm-server-kv-bits.patch``).
        if self.kv_bits is not None and not self._binary_supports_kv_bits(binary):
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

        cmd = self._build_cmd(binary)
        self._log_fp = self._open_log()
        self._status = "starting"
        self._status_reason = ""
        logger.info(
            f"Starting mlx_lm.server on {self.host}:{self.port} "
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

    async def _monitor(self) -> None:
        """Watch the subprocess and restart it on unexpected exit."""
        backoff = _BACKOFF_INITIAL
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            if self._proc is None:
                return
            rc = self._proc.poll()
            if rc is None:
                # Still running — reset backoff once we've been up for a while
                backoff = _BACKOFF_INITIAL
                continue
            if self._stop.is_set():
                return
            logger.warning(
                f"mlx_lm.server(port={self.port}) exited unexpectedly "
                f"(rc={rc}); restarting in {backoff:.1f}s"
            )
            self._status = "unhealthy"
            self._status_reason = f"subprocess exited rc={rc}"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
            binary = find_mlx_lm_binary()
            if binary is None:
                logger.error("mlx_lm.server binary disappeared; giving up on restart")
                self._status = "stopped"
                self._status_reason = "binary disappeared"
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
            # Process running but not responding → unhealthy (monitor will restart)
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
