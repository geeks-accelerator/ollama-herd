"""How much memory herd's own processes are holding.

herd measured everything except itself.  When the native embedding server held
28 GB in one node agent and took a 48 GB Mac down with its co-tenants
(``docs/issues.md``, 2026-10-02), there was no recorded number anywhere to show
it: heartbeats carry *system* memory, which on a 512 GB box is dominated by
Ollama's resident weights and would hide a 20 GB process leak entirely.  Two
devices hit the same bug and neither could produce a growth curve afterwards.

Why ``footprint`` and not RSS
-----------------------------
``phys_footprint`` is what Activity Monitor shows as "Memory" and what the
kernel charges against the process.  **RSS is the metric that hid the original
28 GB**, so this reports footprint first and keeps RSS only as the portable
floor for platforms without it.

``phys_footprint_peak`` is the number that actually diagnoses this class of
bug.  ONNX Runtime keeps the high-water mark of the largest run a process ever
does, so a single oversized request permanently raises the process's memory and
*current* footprint tells you nothing about it afterwards.  The peak is the
evidence; current without peak is how you conclude "looks fine now".

``psutil.memory_full_info()`` would be the portable way to get this and raises
``AccessDenied`` on macOS without root — the same wall ``backend_clients`` hit
with ``net_connections()``.  ``/usr/bin/footprint -p <pid>`` works as the
ordinary user and costs ~50 ms, which is why the collector samples it on a TTL
rather than every heartbeat.

Lives in ``common`` because both entry points need it: the node reports its
own numbers in the heartbeat, and the router has to measure *itself* -- it is
a separate process that no heartbeat can see.

The agent's own process is the one that matters most: the vision and text
embedding servers are ``asyncio.Task``s inside it (``agent.py`` —
``_ensure_embedding_server`` / ``_ensure_text_embedding_server``), not
subprocesses, so their ONNX arenas are charged here.  Children are reported
separately and labelled, because they hold legitimate model weights — two
``mlx_lm.server`` processes at 17 GB each would swamp any total and mask exactly
what this exists to reveal.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess

from fleet_manager.models.node import ProcessMemory, ProcessMemoryEntry

logger = logging.getLogger(__name__)

_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
_FOOTPRINT_RE = re.compile(
    r"^\s*(phys_footprint(?:_peak)?):\s*([0-9.]+)\s*([KMGT]?B)\s*$", re.M
)

# Processes herd spawns, mapped to a role label.  Matched against the full argv
# because a process name alone is unreliable -- the transcription server reports
# as "Python" and the mlx children as "python3.14".
_CHILD_ROLES = (
    ("mlx_lm.server", "mlx"),
    ("mlx.launch", "mlx"),
    ("mlx-qwen3-asr", "transcription"),
    ("whisper", "transcription"),
    ("ollama", "ollama"),
    ("mflux", "image"),
    ("diffusionkit", "image"),
)

# multiprocessing bookkeeping children -- a few MB each, pure noise in a list
# meant to make one process's growth obvious.
_IGNORE_CHILD = ("resource_tracker", "multiprocessing.spawn")


def _parse_footprint(output: str) -> tuple[float, float]:
    """(current_gb, peak_gb) from ``footprint`` output; zeros if unparseable."""
    current = peak = 0.0
    for key, value, unit in _FOOTPRINT_RE.findall(output or ""):
        try:
            gb = float(value) * _UNITS.get(unit.upper(), 0) / 1024**3
        except (TypeError, ValueError):
            continue
        if key == "phys_footprint_peak":
            peak = gb
        else:
            current = gb
    return current, peak


def _footprint(pid: int) -> tuple[float, float]:
    """macOS phys_footprint for ``pid``, or (0, 0) when unavailable."""
    if platform.system() != "Darwin":
        return 0.0, 0.0
    try:
        out = subprocess.run(
            ["/usr/bin/footprint", "-p", str(pid)],
            capture_output=True, text=True, timeout=10.0, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"footprint({pid}) failed: {type(exc).__name__}: {exc}")
        return 0.0, 0.0
    return _parse_footprint(out.stdout or "")


def _role_for(cmdline: str) -> str:
    low = cmdline.lower()
    for needle, role in _CHILD_ROLES:
        if needle in low:
            return role
    return "child"


def probe_process_memory() -> ProcessMemory:
    """Memory held by this process and the children it spawned.

    Returns an empty ``ProcessMemory`` on any failure: self-telemetry must never
    be the reason a heartbeat fails to send.
    """
    try:
        import psutil

        me = psutil.Process(os.getpid())
        rss_gb = me.memory_info().rss / 1024**3
        current, peak = _footprint(me.pid)
        entries: list[ProcessMemoryEntry] = []
        child_total = child_peak = 0.0

        for child in me.children(recursive=True):
            try:
                cmdline = " ".join(child.cmdline())
                if any(skip in cmdline for skip in _IGNORE_CHILD):
                    continue
                c_rss = child.memory_info().rss / 1024**3
                c_cur, c_peak = _footprint(child.pid)
            except Exception:  # noqa: BLE001 -- a child may exit mid-probe
                continue
            child_total += c_cur or c_rss
            child_peak += c_peak or c_rss
            entries.append(
                ProcessMemoryEntry(
                    pid=child.pid,
                    role=_role_for(cmdline),
                    footprint_gb=round(c_cur, 3),
                    peak_gb=round(c_peak, 3),
                    rss_gb=round(c_rss, 3),
                )
            )

        entries.sort(key=lambda e: -(e.peak_gb or e.rss_gb))
        return ProcessMemory(
            footprint_gb=round(current, 3),
            peak_gb=round(peak, 3),
            rss_gb=round(rss_gb, 3),
            children_footprint_gb=round(child_total, 3),
            children_peak_gb=round(child_peak, 3),
            # Bounded: a heartbeat is not the place for an unbounded process
            # list, and the entries are sorted so the interesting ones survive.
            children=entries[:8],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"process memory probe failed: {type(exc).__name__}: {exc}")
        return ProcessMemory()
