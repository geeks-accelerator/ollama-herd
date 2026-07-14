"""Shared binary-discovery helper.

`which_extended` finds an executable, checking common tool-install locations
beyond `$PATH`. uv tool, pipx, and Homebrew install binaries in directories
that may not be on PATH when the node agent starts under launchd, cron, or a
Windows service — so a plain `shutil.which` misses them.

Promoted from `node/collector.py` (was `_which_extended`) so every caller —
the collector's model probes, the image server, and the MLX supervisor — shares
one platform-aware resolver instead of maintaining divergent copies. See
`docs/plans/distributed-mlx-inference.md` (codebase-audit section).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def which_extended(binary: str) -> str | None:
    """Find a binary, checking common tool install paths beyond ``$PATH``.

    Returns an absolute path, or ``None`` if the binary can't be found in
    ``$PATH`` or any of the platform-aware fallback locations.
    """
    found = shutil.which(binary)
    if found:
        return found
    # Check common tool binary locations (platform-aware)
    extra_dirs = [
        Path.home() / ".local" / "bin",           # uv tool, pipx (Unix/Linux/macOS)
    ]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        if local:
            extra_dirs.append(Path(local) / "Programs" / "Python" / "Scripts")
        if appdata:
            extra_dirs.append(Path(appdata) / "Python" / "Scripts")
        extra_dirs.append(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links")
    else:
        extra_dirs.append(Path("/opt/homebrew/bin"))   # Homebrew (Apple Silicon)
        extra_dirs.append(Path("/usr/local/bin"))      # Homebrew (Intel), system
    for d in extra_dirs:
        candidate = d / binary
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None
