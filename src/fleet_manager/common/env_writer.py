"""Persist settings changes back to ``~/.fleet-manager/env``.

The dashboard's settings endpoint used to mutate ``app.state.settings`` in
memory and nothing else, so every toggle silently reverted on the next
restart.  That is merely annoying for ``auto_pull``; for a telemetry opt-out it
would be a broken promise -- a user turns it off, restarts, and it is quietly
back on.  So toggles are written here.

Round-tripping matters more than elegance.  This file is hand-edited by
operators and carries comments explaining hard-won gotchas (the node-id drift,
the router-URL pin).  A writer that rewrote it from parsed key/value pairs
would silently delete all of that, so instead we edit the *lines*: an existing
key is replaced in place, keeping its position and any comment above it, and a
new key is appended.  Everything we do not recognise is passed through byte for
byte.

Shell env still wins at load time (see ``env_file.load_env_file``), so a value
exported in a profile will keep overriding what the dashboard writes.  The API
layer is responsible for telling the user when that has happened rather than
letting the UI claim a change it cannot deliver.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = Path.home() / ".fleet-manager" / "env"


def _format_value(value: object) -> str:
    """Render a Python value the way the loader expects to read it back."""
    if isinstance(value, bool):
        # The loader hands strings to pydantic, which accepts true/false.
        return "true" if value else "false"
    text = str(value)
    # Quote only when needed; an unquoted value with spaces would parse but
    # reads badly and breaks `set -a; source env`.
    if text == "" or any(c in text for c in ' \t"#'):
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def set_env_var(key: str, value: object, path: Path | None = None) -> bool:
    """Create or update ``key`` in the env file.  Returns True on success.

    Never raises: a settings write failing must not 500 the dashboard.  The
    in-memory change has already been applied by the caller, so a failure here
    degrades to "works until restart" -- the old behaviour -- rather than
    breaking the request.
    """
    target = path or DEFAULT_ENV_FILE
    rendered = f"{key}={_format_value(value)}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        original = target.read_text(encoding="utf-8") if target.exists() else ""
        lines = original.splitlines()

        # Match the key whether or not it carries a shell-style `export `.
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        replaced = False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                continue
            if pattern.match(line):
                lines[i] = rendered
                replaced = True
                break  # first occurrence wins, matching the loader

        if not replaced:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(rendered)

        tmp = target.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError as exc:
        logger.warning("could not persist %s to %s: %s", key, target, exc)
        return False


def unset_env_var(key: str, path: Path | None = None) -> bool:
    """Remove ``key`` so the code default applies again.  True on success."""
    target = path or DEFAULT_ENV_FILE
    if not target.exists():
        return True
    try:
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        lines = target.read_text(encoding="utf-8").splitlines()
        kept = [
            ln for ln in lines if ln.lstrip().startswith("#") or not pattern.match(ln)
        ]
        tmp = target.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError as exc:
        logger.warning("could not remove %s from %s: %s", key, target, exc)
        return False
