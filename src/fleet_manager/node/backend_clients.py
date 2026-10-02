"""Find out who *else* is talking to this node's Ollama.

herd's whole scheduling model assumes it is the only client of its backends.
Every scoring signal -- queue depth, free slots, session affinity, context fit
-- is derived from what herd itself dispatched, and ``QueueManager`` caps
concurrency per model to match what the backend admits.  A second process
talking straight to ``:11434`` fills the same llama-server slots, so herd's
"two in flight" is really an occupancy of three or four, and its cap now
*oversubscribes* the backend it was meant to protect.

This is not hypothetical.  On 2026-08-21 a co-located CLI daemon began sending
~27% of Ollama's load direct to ``127.0.0.1:11434``, bypassing herd entirely.
Nobody configured it: its cloud provider lost its credentials and its model
fallback chain silently redirected the workload onto the local fleet.  Fleet
decode fell 15% and **nothing** in the dashboard, health engine or traces said
why -- herd's own requests were all healthy, because they were.  It took hours
to find, after wrongly suspecting the Ollama version, upstream llama.cpp,
memory, thermals and the clients.  See ``docs/observations.md`` (2026-08-23).

What it took to actually find it was one ``lsof``, so that is what this does.

Why not psutil
--------------
``psutil.net_connections()`` is the obvious portable answer and it does not
work: on macOS it raises ``AccessDenied`` for any process but our own unless
the agent runs as root, which herd does not and should not.  ``lsof`` returns
the same information as the ordinary user.  Verified on macOS 26 / psutil
7.2.2 -- see ``tests/test_node/test_backend_clients.py``.

Why not count requests
----------------------
Two other signals were considered and rejected.  Ollama's ``/api/ps`` reports
``expires_at`` per loaded model, so drift in it without a herd dispatch would
imply someone else -- but the canonical fleet config sets
``OLLAMA_KEEP_ALIVE=-1``, which pins ``expires_at`` to the year 2319 and makes
it constant.  Parsing ``new prompt`` lines out of Ollama's own log works (it is
the manual tripwire in CLAUDE.md) but the log path is per-platform and the
counts only reconcile roughly.  Connection ownership is exact, immediate, and
names the culprit process instead of leaving an operator to go find it.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from urllib.parse import urlparse

from fleet_manager.models.node import BackendClient

logger = logging.getLogger(__name__)

# Substrings that mark a process as one of ours.  These are matched against the
# full command line, and they are the same markers CLAUDE.md's restart recipe
# keys on (`pkill -9 -f "bin/herd|mlx_lm.server"`), so a process herd itself
# spawned can never be reported as a bypasser.
_HERD_MARKERS = (
    "bin/herd",
    "fleet_manager",
    "mlx_lm.server",
    "mlx.launch",
    "herd-node",
)

# Loopback forms a local client connects over.  herd uses IPv6 `::1`; most
# other tools use IPv4 `127.0.0.1` -- which is itself a useful tell, and the
# one that distinguished openclaw's sockets from ours in the 2026-08 incident.
_LOOPBACK = ("127.0.0.1", "::1", "[::1]", "localhost")


def ollama_port(ollama_host: str) -> int:
    """Extract the TCP port from a configured Ollama URL, defaulting to 11434."""
    try:
        return urlparse(ollama_host).port or 11434
    except (ValueError, AttributeError):
        return 11434


def _cmdline(pid: int) -> str:
    """Best-effort full command line for ``pid`` -- empty string if unavailable.

    Deliberately not ``ps -o comm``: a Node daemon's process name is just
    ``process.title``, which in the 2026-08 incident resolved to an unrelated
    project folder and sent the investigation the wrong way.  The full argv is
    what identifies a process.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_ours(pid: int, command: str, cmdline: str) -> bool:
    """True if this process is herd, a child herd spawned, or Ollama itself."""
    if pid == os.getpid():
        return True
    haystack = f"{command} {cmdline}".lower()
    if any(marker.lower() in haystack for marker in _HERD_MARKERS):
        return True
    # Ollama's own sockets: the server end of every client connection, plus
    # whatever it opens to its llama-server children.  Not a bypasser.
    return command.lower() in ("ollama", "ollama.exe", "llama-server")


def _parse_lsof(output: str, port: int) -> dict[int, dict]:
    """Group lsof rows into one entry per *client* pid.

    lsof prints both ends of a loopback connection: the client's socket
    (``[::1]:61641->[::1]:11434``) and Ollama's (``[::1]:11434->[::1]:61641``).
    Only the first is a client, identified by the *remote* side being our port.
    """
    peers: dict[int, dict] = {}
    for line in output.splitlines()[1:]:  # skip lsof's header
        parts = line.split()
        if len(parts) < 9:
            continue
        command, raw_pid, name = parts[0], parts[1], parts[-2]
        if "->" not in name:
            continue
        local, _, remote = name.partition("->")
        # The client side is the one whose *remote* endpoint is Ollama's port.
        if not remote.endswith(f":{port}"):
            continue
        try:
            pid = int(raw_pid)
        except ValueError:
            continue
        entry = peers.setdefault(pid, {"command": command, "connections": 0, "local": local})
        entry["connections"] += 1
    return peers


def _probe_lsof(port: int) -> list[BackendClient]:
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    try:
        out = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"backend client probe failed: {type(exc).__name__}: {exc}")
        return []
    # lsof exits 1 when nothing matches, which is a normal empty result.
    peers = _parse_lsof(out.stdout or "", port)

    clients: list[BackendClient] = []
    for pid, info in peers.items():
        cmd = _cmdline(pid)
        if _is_ours(pid, info["command"], cmd):
            continue
        local_host = info["local"].rsplit(":", 1)[0]
        clients.append(
            BackendClient(
                pid=pid,
                process=info["command"],
                cmdline=cmd[:300],
                connections=info["connections"],
                loopback=local_host in _LOOPBACK,
            )
        )
    clients.sort(key=lambda c: (-c.connections, c.pid))
    return clients


def probe_backend_clients(ollama_host: str) -> list[BackendClient]:
    """Processes other than herd holding open connections to this node's Ollama.

    Returns an empty list on any failure -- a probe that cannot run must never
    break a heartbeat, and "we could not look" is reported as "we saw nothing"
    rather than as a false alarm.  The router-side check only fires on a
    positive sighting, so a silent probe is a missed detection, not a bogus one.
    """
    port = ollama_port(ollama_host)
    try:
        if platform.system() == "Windows":
            # No lsof on Windows.  Left unimplemented rather than guessed at:
            # the signal is only as good as the process attribution, and
            # netstat -ano + tasklist needs its own verification pass.
            return []
        return _probe_lsof(port)
    except Exception as exc:  # noqa: BLE001 -- never break the heartbeat
        logger.debug(f"backend client probe failed: {type(exc).__name__}: {exc}")
        return []
