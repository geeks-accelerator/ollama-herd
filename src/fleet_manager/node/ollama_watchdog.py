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
        consecutive_kicks_before_serve_restart: int = 3,
        serve_restart_cooldown_s: float = 1800.0,
    ):
        self.ollama_host = ollama_host.rstrip("/")
        self.interval_s = interval_s
        self.probe_timeout_s = probe_timeout_s
        self.consecutive_failures_before_kick = consecutive_failures_before_kick
        self.cooldown_s = cooldown_s
        # Escalation: when N kicks in a row don't restore /api/chat health, the
        # problem isn't a stuck runner — it's accumulated `ollama serve` state
        # corruption.  Restart the whole server.  See docs/issues.md
        # ("Ollama watchdog can't escalate to ollama serve restart") for why.
        self.consecutive_kicks_before_serve_restart = consecutive_kicks_before_serve_restart
        self.serve_restart_cooldown_s = serve_restart_cooldown_s

        self._running = False
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._last_kick_ts: float = 0.0
        # Counts kicks that didn't lead to a healthy probe by the next cycle.
        # Reset on healthy probe.  When this hits the escalation threshold,
        # restart `ollama serve` itself (not just runners).
        self._kicks_without_recovery: int = 0
        self._last_serve_restart_ts: float = 0.0
        # Stats exposed for health-check integration (see fleet_manager.server.health_engine)
        self.stats = {
            "probes_total": 0,
            "probes_failed": 0,
            "kicks_total": 0,
            "last_kick_reason": "",
            "last_kick_at": None,  # unix time
            "serve_restarts_total": 0,
            "last_serve_restart_at": None,
            "last_serve_restart_reason": "",
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
    # Escalation: restart `ollama serve` when kicks alone don't recover
    # ------------------------------------------------------------------

    def _can_restart_serve(self) -> bool:
        """Respect the longer serve-restart cooldown to prevent flap loops."""
        elapsed = time.time() - self._last_serve_restart_ts
        return elapsed >= self.serve_restart_cooldown_s

    def _restart_serve(self, reason: str) -> bool:
        """Restart `ollama serve` itself (not just runners).

        Used as the escalation when N consecutive kicks haven't restored
        ``/api/chat`` health — see docs/issues.md for the failure mode this
        addresses.  Platform-aware: macOS uses ``open -a Ollama``, Linux uses
        ``systemctl restart ollama``.  Returns True if the restart command
        succeeded; the next probe cycle will determine if Ollama actually
        came back healthy.
        """
        logger.warning(
            f"Ollama watchdog: ESCALATING — restarting `ollama serve` ({reason}). "
            f"Kicks alone have not restored health after "
            f"{self._kicks_without_recovery} attempts."
        )
        try:
            if sys.platform == "win32":
                # Windows: stop the Ollama service via taskkill on the parent
                subprocess.run(
                    ["taskkill", "/F", "/IM", "ollama.exe"],
                    capture_output=True, timeout=15, check=False,
                )
                # Restart via `start` — assumes ollama is on PATH
                subprocess.Popen(
                    ["cmd", "/c", "start", "", "ollama", "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif sys.platform == "darwin":
                # macOS: kill ollama serve + helpers, then relaunch the .app
                subprocess.run(
                    ["pkill", "-9", "-f", "ollama serve"],
                    capture_output=True, timeout=10, check=False,
                )
                subprocess.run(
                    ["pkill", "-9", "-f", "Ollama.app"],
                    capture_output=True, timeout=10, check=False,
                )
                # `open -a Ollama` re-launches the .app which spawns ollama serve
                subprocess.run(
                    ["open", "-a", "Ollama"],
                    capture_output=True, timeout=15, check=False,
                )
            else:
                # Linux: prefer systemctl, fall back to pkill+restart
                rc = subprocess.run(
                    ["systemctl", "restart", "ollama"],
                    capture_output=True, timeout=30, check=False,
                ).returncode
                if rc != 0:
                    # systemctl unavailable — best effort kill + relaunch
                    subprocess.run(
                        ["pkill", "-9", "-f", "ollama serve"],
                        capture_output=True, timeout=10, check=False,
                    )
                    subprocess.Popen(
                        ["ollama", "serve"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
        except FileNotFoundError as exc:
            logger.error(
                f"Ollama watchdog: serve-restart command not found "
                f"({exc}); can't escalate on this platform"
            )
            return False
        except Exception as exc:  # noqa: BLE001 — never break the watchdog loop
            logger.error(
                f"Ollama watchdog: serve restart failed: {type(exc).__name__}: {exc}"
            )
            return False

        self._last_serve_restart_ts = time.time()
        self._kicks_without_recovery = 0  # reset escalation counter
        self.stats["serve_restarts_total"] += 1
        self.stats["last_serve_restart_at"] = self._last_serve_restart_ts
        self.stats["last_serve_restart_reason"] = reason
        logger.info(
            "Ollama watchdog: ollama serve restart issued — "
            "next probe will determine recovery."
        )
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
            if self._consecutive_failures or self._kicks_without_recovery:
                logger.info(
                    f"Ollama watchdog: recovered after "
                    f"{self._consecutive_failures} failure(s), "
                    f"{self._kicks_without_recovery} kick(s) without recovery"
                )
            self._consecutive_failures = 0
            self._kicks_without_recovery = 0  # successful chat → escalation reset
            return
        await self._record_failure(f"chat_probe on {model}: {chat_reason}")

    async def _record_failure(self, reason: str) -> None:
        """Increment failure counter; kick or escalate to serve restart."""
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
            # Escalation path: if kicks aren't even cooling down, the system
            # is in trouble.  Check if we've already exhausted the kick budget.
            if (
                self._kicks_without_recovery >= self.consecutive_kicks_before_serve_restart
                and self._can_restart_serve()
            ):
                self._restart_serve(
                    f"{self._kicks_without_recovery} kicks without recovery "
                    f"(cooldown blocked next kick)"
                )
            return

        # Kick: reset failure counter so we give the new runners a chance,
        # but increment kicks_without_recovery — it'll reset on the next
        # healthy probe (in _one_cycle) or trigger escalation if it doesn't.
        if self._kick_runners(reason):
            self._consecutive_failures = 0
            self._kicks_without_recovery += 1
            logger.info(
                f"Ollama watchdog: kick recorded — "
                f"kicks_without_recovery={self._kicks_without_recovery}/"
                f"{self.consecutive_kicks_before_serve_restart}"
            )
            # If we've now hit the escalation threshold, restart `ollama serve`
            # itself.  This catches the "runners die immediately on respawn
            # under load" failure mode that runner kicks alone can't fix.
            if (
                self._kicks_without_recovery >= self.consecutive_kicks_before_serve_restart
                and self._can_restart_serve()
            ):
                self._restart_serve(
                    f"{self._kicks_without_recovery} consecutive kicks did not restore health"
                )
