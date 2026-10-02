"""Surface a node going offline instead of waiting to be asked.

``node_offline`` has been a CRITICAL health check for a long time, and it is
correct -- but it is *pull-only*.  A fleet whose every node has vanished
serves errors while ``/dashboard/api/health`` sits there, right, and unread.
That is the same shape of gap the launchd agents closed for *process*
absence: a node that is alive but cannot reach the router has an identical
blast radius and had no equivalent answer.  On 2026-10-01 a DHCP lease change
(``192.168.40.104`` -> ``.105``) left this fleet with zero usable nodes for
3.9 hours while ``launchctl`` showed both agents healthy and every request
timed out.

Two deliveries, because either one alone has a hole:

* **A connected dashboard raises a browser notification.**  The page already
  receives per-node ``status`` over the existing SSE stream, so this costs no
  new endpoint and no polling -- see ``routes/dashboard.py``.
* **If no dashboard is connected, open one.**  A notification with nowhere to
  land is not an alert.  Only the host process can launch a browser; a web
  page cannot open itself.

Connected-client count is tracked here rather than guessed from request
timestamps, because the SSE stream already knows exactly when a reader
arrives and leaves.  ``> 0`` means somebody is genuinely watching.

Opening a window is intrusive, so the whole feature is **off by default**
(``FLEET_OFFLINE_ALERT``), matching the posture ``cors_origins`` takes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time

logger = logging.getLogger(__name__)

# Don't open a second window while the operator is presumably still looking at
# the first one.  A flapping node would otherwise spawn a browser tab per
# transition, which is worse than the silence this replaces.
REOPEN_COOLDOWN_S = 300.0


def _browser_command(url: str) -> list[str] | None:
    """Return the platform command that opens ``url``, or None if unavailable.

    Resolved through ``shutil.which`` so a missing opener is a logged no-op
    rather than a ``FileNotFoundError`` raised inside the heartbeat monitor.
    """
    if sys.platform == "darwin":
        opener = shutil.which("open")
        return [opener, url] if opener else None
    if sys.platform == "win32":
        # ``start`` is a cmd builtin, not an executable, so it cannot be
        # resolved with ``which`` and must run through the shell.
        return ["cmd", "/c", "start", "", url]
    opener = shutil.which("xdg-open")
    return [opener, url] if opener else None


class OfflineAlerter:
    """Tracks dashboard watchers and opens one when a node drops unwatched."""

    def __init__(self, settings, *, port: int | None = None):
        self._settings = settings
        self._port = port or getattr(settings, "port", 11435)
        self._clients = 0
        self._last_open_at = 0.0

    # -- dashboard client tracking (called by the SSE endpoint) -------------

    def client_connected(self) -> None:
        self._clients += 1

    def client_disconnected(self) -> None:
        # Clamp rather than assert: a stream can be torn down along a path
        # that skips the paired increment, and a negative count would make
        # ``is_watched`` permanently wrong -- failing open into "somebody is
        # watching" is exactly the silence this module exists to remove.
        self._clients = max(0, self._clients - 1)

    @property
    def client_count(self) -> int:
        return self._clients

    @property
    def is_watched(self) -> bool:
        return self._clients > 0

    # -- alerting ----------------------------------------------------------

    @property
    def url(self) -> str:
        return (
            getattr(self._settings, "offline_alert_url", "")
            or f"http://localhost:{self._port}/dashboard"
        )

    async def node_went_offline(self, node_id: str) -> None:
        """Called on an online -> offline transition.  Never raises.

        The caller is ``NodeRegistry.monitor_heartbeats``, which holds the
        registry lock and must keep monitoring every other node, so every
        failure here is swallowed and logged.
        """
        if not getattr(self._settings, "offline_alert", False):
            return

        # A connected dashboard notifies on its own from the SSE payload;
        # opening another window on top of that is noise.
        if self.is_watched:
            logger.info(
                f"Node {node_id} offline — {self._clients} dashboard "
                f"client(s) connected, leaving the alert to them"
            )
            return

        now = time.monotonic()
        if now - self._last_open_at < REOPEN_COOLDOWN_S:
            logger.info(
                f"Node {node_id} offline — dashboard opened "
                f"{now - self._last_open_at:.0f}s ago, not reopening"
            )
            return

        cmd = _browser_command(self.url)
        if cmd is None:
            logger.warning(
                f"Node {node_id} offline and no dashboard is connected, but "
                f"no browser opener was found on this platform "
                f"({sys.platform}) — open {self.url} manually"
            )
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as e:
            logger.warning(f"Failed to open dashboard for offline {node_id}: {e!r}")
            return

        self._last_open_at = now
        logger.warning(
            f"Node {node_id} went OFFLINE with no dashboard connected — opened {self.url}"
        )
