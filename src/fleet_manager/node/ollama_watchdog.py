"""Ollama watchdog — detects stuck runners and kicks them.

Observed 2026-04-22 on Ollama 0.20.4 / macOS: under concurrent ``stream=False``
requests with big bodies + large ``max_tokens``, Ollama's ``/api/chat`` endpoint
stops responding entirely while ``/api/tags`` still answers. New requests queue
forever, runner never recovers on its own. The operational fix is
``pkill -9 -f "ollama runner"`` — ``ollama serve`` respawns fresh runners within
2-3 seconds and the model cold-loads on the next request.

This watchdog automates that. It periodically probes Ollama with a cheap real
chat call (not just ``/api/tags`` — that was passing during the stuck state).
If two consecutive probes time out or return 5xx, it kills runner processes.
A cooldown prevents thrashing if the root cause is something else.

**Why in the node agent, not the router:** the node has local access to Ollama
and can ``pkill`` its own runner processes.  The router can't SSH into nodes
to kick them; it can only stop sending traffic.  Defensive liveness is the
node's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
import time

import httpx

logger = logging.getLogger(__name__)


class OllamaWatchdog:
    """Background task that keeps the local Ollama responsive."""

    def __init__(
        self,
        *,
        ollama_host: str = "http://localhost:11434",
        interval_s: float = 60.0,
        probe_timeout_s: float = 15.0,
        consecutive_failures_before_kick: int = 2,
        cooldown_s: float = 120.0,
    ):
        self.ollama_host = ollama_host.rstrip("/")
        self.interval_s = interval_s
        self.probe_timeout_s = probe_timeout_s
        self.consecutive_failures_before_kick = consecutive_failures_before_kick
        self.cooldown_s = cooldown_s

        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._last_kick_ts: float = 0.0
        # Stats exposed for health-check integration (see fleet_manager.server.health_engine)
        self.stats = {
            "probes_total": 0,
            "probes_failed": 0,
            "kicks_total": 0,
            "last_kick_reason": "",
            "last_kick_at": None,  # unix time
        }

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background watchdog task."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="ollama-watchdog")
        logger.info(
            f"Ollama watchdog enabled — probing {self.ollama_host} every "
            f"{self.interval_s:.0f}s (timeout={self.probe_timeout_s:.0f}s, "
            f"cooldown={self.cooldown_s:.0f}s)"
        )

    async def stop(self) -> None:
        """Stop the watchdog cleanly."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    async def _probe_tags(self, client: httpx.AsyncClient) -> tuple[bool, str]:
        """Cheap liveness probe — returns (ok, reason_if_failed)."""
        try:
            resp = await client.get(f"{self.ollama_host}/api/tags")
            if resp.status_code != 200:
                return False, f"tags_http_{resp.status_code}"
            return True, ""
        except httpx.TimeoutException:
            return False, "tags_timeout"
        except httpx.HTTPError as exc:
            return False, f"tags_{type(exc).__name__}"

    async def _probe_chat(self, client: httpx.AsyncClient, model: str) -> tuple[bool, str]:
        """Inference-path probe — exercises the runner (not just `ollama serve`).

        Sends a one-token completion against the cheapest currently-loaded
        model so we exercise the runner IPC path where stuck states surface.
        """
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "options": {"num_predict": 1},
        }
        try:
            resp = await client.post(
                f"{self.ollama_host}/api/chat",
                json=payload,
                timeout=self.probe_timeout_s,
            )
            if resp.status_code == 200:
                return True, ""
            return False, f"chat_http_{resp.status_code}"
        except httpx.TimeoutException:
            return False, "chat_timeout"
        except httpx.HTTPError as exc:
            return False, f"chat_{type(exc).__name__}"

    async def _pick_probe_model(self, client: httpx.AsyncClient) -> str | None:
        """Choose the smallest currently-loaded model for chat probing.

        Probing a hot model avoids triggering a cold-load.  If nothing is
        loaded, we can't safely chat-probe — return None and rely on
        ``/api/tags`` liveness alone.
        """
        try:
            resp = await client.get(f"{self.ollama_host}/api/ps", timeout=5.0)
            resp.raise_for_status()
            running = resp.json().get("models", []) or []
        except Exception:
            return None
        if not running:
            return None
        running.sort(key=lambda m: m.get("size", 0))
        return running[0].get("model") or running[0].get("name")

    # ------------------------------------------------------------------
    # Kick logic
    # ------------------------------------------------------------------

    def _can_kick(self) -> bool:
        """Respect cooldown — avoid thrashing on root causes a kick can't fix."""
        elapsed = time.time() - self._last_kick_ts
        return elapsed >= self.cooldown_s

    def _kick_runners(self, reason: str) -> bool:
        """Kill all ``ollama runner`` processes on this machine.

        ``ollama serve`` respawns them on the next request.  Returns True if
        the kill command succeeded (doesn't guarantee a process was found —
        pkill returns 1 when nothing matched).
        """
        if sys.platform == "win32":
            # Windows: use taskkill. Ollama-on-Windows is rarer but supported.
            cmd = ["taskkill", "/F", "/IM", "ollama.exe"]
        else:
            # macOS + Linux: pkill on the runner substring
            cmd = ["pkill", "-9", "-f", "ollama runner"]
        logger.warning(
            f"Ollama watchdog: KICKING stuck runner ({reason}). "
            f"Runners will respawn on next request; first request will cold-load."
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=10,
                check=False,
            )
            # pkill returns 0 when it killed something, 1 when nothing matched.
            # Both are OK from our perspective — the important thing is we
            # tried.  Log the outcome.
            if result.returncode == 0:
                logger.info("Ollama watchdog: runner processes killed")
            elif result.returncode == 1:
                logger.info("Ollama watchdog: no runner processes to kill (already down?)")
            else:
                logger.warning(
                    f"Ollama watchdog: pkill exited {result.returncode}: "
                    f"{result.stderr.decode('utf-8', errors='replace')[:200]}"
                )
        except subprocess.TimeoutExpired:
            logger.error("Ollama watchdog: pkill timed out — giving up this cycle")
            return False
        except FileNotFoundError:
            logger.error(
                f"Ollama watchdog: '{cmd[0]}' not in PATH; can't kick runners on this platform"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Ollama watchdog: kick failed: {type(exc).__name__}: {exc}")
            return False

        self._last_kick_ts = time.time()
        self.stats["kicks_total"] += 1
        self.stats["last_kick_reason"] = reason
        self.stats["last_kick_at"] = self._last_kick_ts
        return True

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Probe loop — runs until stop() is called or the task is cancelled."""
        # Give Ollama a moment to be ready before the first probe
        await asyncio.sleep(5.0)
        async with httpx.AsyncClient() as client:
            while self._running:
                try:
                    await self._one_cycle(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — don't let watchdog bugs kill node agent
                    logger.exception(
                        f"Ollama watchdog cycle error: {type(exc).__name__}: {exc}"
                    )
                try:
                    await asyncio.sleep(self.interval_s)
                except asyncio.CancelledError:
                    raise

    async def _one_cycle(self, client: httpx.AsyncClient) -> None:
        """One probe cycle — run both probes, update failure counter, maybe kick."""
        self.stats["probes_total"] += 1
        # Cheap liveness first — if this fails, runner-probe doesn't add info
        tags_ok, tags_reason = await self._probe_tags(client)
        if not tags_ok:
            await self._record_failure(f"tags_probe: {tags_reason}")
            return

        # Chat probe — only if a hot model exists
        model = await self._pick_probe_model(client)
        if model is None:
            # No models hot — nothing to probe, consider healthy
            self._consecutive_failures = 0
            return
        chat_ok, chat_reason = await self._probe_chat(client, model)
        if chat_ok:
            if self._consecutive_failures:
                logger.info(
                    f"Ollama watchdog: recovered after "
                    f"{self._consecutive_failures} failure(s)"
                )
            self._consecutive_failures = 0
            return
        await self._record_failure(f"chat_probe on {model}: {chat_reason}")

    async def _record_failure(self, reason: str) -> None:
        """Increment failure counter and kick if threshold reached."""
        self._consecutive_failures += 1
        self.stats["probes_failed"] += 1
        logger.warning(
            f"Ollama watchdog: probe failed ({reason}) — "
            f"consecutive={self._consecutive_failures}/"
            f"{self.consecutive_failures_before_kick}"
        )
        if self._consecutive_failures < self.consecutive_failures_before_kick:
            return
        if not self._can_kick():
            cooldown_remaining = self.cooldown_s - (time.time() - self._last_kick_ts)
            logger.warning(
                f"Ollama watchdog: would kick but cooldown active "
                f"({cooldown_remaining:.0f}s remaining) — something else may be wrong"
            )
            return
        # Kick and reset failure counter so we give the new runners a chance
        if self._kick_runners(reason):
            self._consecutive_failures = 0
