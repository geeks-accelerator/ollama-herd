"""Stable, random install identifier for anonymous telemetry.

This exists so telemetry can count *installs* — active installs, retention,
per-install aggregates — without ever learning who or where you are.

**It must never be derived from anything about the machine.**  The obvious
shortcut (hash the hostname, the MAC, a serial number) would be stable and
convenient and would also be a fingerprint: a value that identifies the same
physical machine across reinstalls, across users, and across anyone else who
computes the same hash.  A random UUID has all the analytic value and none of
that.  ``node_id`` already falls back to ``socket.gethostname()``, and Macs get
named ``johns-macbook``, so the hostname in particular must never reach a
payload — ``tests/test_common/test_install_id.py`` fails the build if it does.

The id is stored as one plain line in ``~/.fleet-manager/install_id`` precisely
so it is easy to find and easy to delete.  Deleting it makes this a brand-new
install with no link to the old one; that is the intended escape hatch, and the
public telemetry contract at ollamaherd.com/telemetry promises it.

We call this **pseudonymous**, not anonymous: a value that persists across runs
links one day's data to the next, which is exactly what the retention analysis
needs and exactly what "anonymous" would overstate.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_ID_FILE = Path.home() / ".fleet-manager" / "install_id"


def _read(path: Path) -> str | None:
    """Return a valid stored id, or None if absent/unreadable/corrupt.

    Corruption is treated as absence rather than an error.  A truncated file
    from a power cut should cost the caller a new id, not a crash on a code
    path that is meant to be invisible.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    try:
        # Round-trip through UUID so a hand-edited or partially-written file
        # cannot inject arbitrary text into a telemetry payload.
        return str(uuid.UUID(raw))
    except ValueError:
        logger.warning(
            "install_id file %s is not a valid UUID; generating a new one", path
        )
        return None


def _write(path: Path, value: str) -> bool:
    """Persist atomically.  Returns False if the id could not be stored.

    Atomic because two agents starting together (router and node on one box)
    would otherwise race and could leave a half-written file that the next
    read discards — churning the id and inflating the install count.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("could not persist install_id to %s: %s", path, exc)
        return False


def get_install_id(path: Path | None = None) -> str:
    """Return this install's id, creating and persisting one on first call.

    Never raises: telemetry is a background nicety and must not be able to take
    down an agent.  If the id cannot be written (read-only home, full disk), a
    fresh one is returned for this process instead.  That over-counts installs
    slightly, which is strictly better than crashing the node — and the failure
    is logged rather than silent.
    """
    target = path or DEFAULT_INSTALL_ID_FILE

    existing = _read(target)
    if existing is not None:
        return existing

    new_id = str(uuid.uuid4())
    if not _write(target, new_id):
        logger.warning(
            "install_id is not persistent this run; telemetry will count this "
            "as a new install each restart"
        )
    return new_id
