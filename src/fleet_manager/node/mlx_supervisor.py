"""Subprocess lifecycle manager for a local `mlx_lm.server`.

Phase 3 of ``docs/plans/mlx-backend-for-large-models.md``.  Spawned by the
node agent when ``FLEET_NODE_MLX_AUTO_START`` is true so users can bring up
the whole fleet (Ollama + herd-node + MLX) with a single ``uv run herd-node``.

What this module does:
  - Spawn ``mlx_lm.server`` as a child process with the configured flags
  - Wait for it to become healthy (``GET /v1/models`` → 200) before declaring ready
  - Monitor it in the background; restart with exponential backoff on crash
  - Route its stdout/stderr to ``~/.fleet-manager/logs/mlx-server.log``
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
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Health-check cadence while waiting for the server to come up.
_HEALTH_POLL_INTERVAL = 2.0
_HEALTH_POLL_TIMEOUT = 120.0  # 2 min — big MLX models can take a while to mmap
# Restart backoff: 1s, 2s, 4s, ... capped at 60s
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


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
        log_dir: Path | None = None,
    ):
        self.model = model
        self.port = port
        self.host = host
        self.kv_bits = kv_bits
        self.prompt_cache_size = prompt_cache_size
        self.prompt_cache_bytes = prompt_cache_bytes
        self.log_dir = log_dir or (Path.home() / ".fleet-manager" / "logs")
        self._proc: subprocess.Popen | None = None
        self._monitor_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._log_fp = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

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
        log_path = self.log_dir / "mlx-server.log"
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
            return False

        if not self.model:
            logger.error(
                "FLEET_NODE_MLX_AUTO_START_MODEL is empty — set it to a "
                "local model path or Hugging Face repo id (e.g. "
                "'mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit'). "
                "Skipping MLX auto-start."
            )
            return False

        cmd = self._build_cmd(binary)
        self._log_fp = self._open_log()
        logger.info(
            f"Starting mlx_lm.server on port {self.port} "
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
            return False

        if not await self._wait_healthy():
            logger.error(
                f"mlx_lm.server failed to become healthy within "
                f"{_HEALTH_POLL_TIMEOUT:.0f}s. Killing and giving up for now."
            )
            await self._terminate()
            return False

        logger.info(f"mlx_lm.server healthy at {self.base_url}")
        # Start monitor task to restart on crash
        self._monitor_task = asyncio.create_task(
            self._monitor(), name="mlx-supervisor-monitor",
        )
        return True

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
                f"mlx_lm.server exited unexpectedly (rc={rc}); "
                f"restarting in {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
            binary = find_mlx_lm_binary()
            if binary is None:
                logger.error("mlx_lm.server binary disappeared; giving up on restart")
                return
            try:
                self._proc = subprocess.Popen(
                    self._build_cmd(binary),
                    stdout=self._log_fp,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                if await self._wait_healthy(timeout=60.0):
                    logger.info("mlx_lm.server restarted successfully")
                else:
                    logger.warning(
                        "mlx_lm.server restarted but didn't go healthy in time"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"mlx_lm.server restart failed: {exc}")

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
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            finally:
                self._log_fp = None
