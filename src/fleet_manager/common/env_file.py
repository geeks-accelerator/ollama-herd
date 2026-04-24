"""Load fleet env vars from ``~/.fleet-manager/env`` at process startup.

Problem this solves
-------------------

Both ``herd`` and ``herd-node`` depend on ``FLEET_*`` env vars
(MLX backend, Anthropic model map, compactor, preloader pins, etc.).
On macOS those vars typically live in ``~/.zshrc`` — but any process
that isn't launched from an interactive zsh shell (Bash subshells,
``nohup``, launchd plists without a full env setup, CI scripts) will
start with the vars *unset* and silently fall back to defaults.

We've been bitten by this twice in one day:

  1. The node agent started without ``FLEET_NODE_MLX_*`` → supervisor
     didn't auto-start the 480B, agent looked healthy, but the MLX
     backend was dark.
  2. The router started without ``FLEET_MLX_ENABLED`` +
     ``FLEET_ANTHROPIC_MODEL_MAP`` → Claude Code requests silently
     fell back to routing ``claude-sonnet-4-5`` → whatever matched
     ``qwen3-coder:30b-agent`` instead of the intended 480B.

Both failures were invisible in the logs until someone noticed the
wrong model was serving.  No error, no warning — just quietly wrong.

Design
------

At process startup, *before* any pydantic ``BaseSettings`` instantiates:

  1. Read ``~/.fleet-manager/env`` (or ``$FLEET_ENV_FILE`` if set).
  2. Parse it as KEY=value lines (``#`` comments supported).
  3. For any key NOT already in ``os.environ``, set it.

Shell env always wins — this is a fallback, not an override.  Anyone
who already has the vars exported via their shell profile sees no
behavior change.

The file is intentionally plain shell-friendly syntax (``KEY=value``,
no shell substitution) so the same file can also be ``set -a; source
~/.fleet-manager/env; set +a``-ed from a shell if someone prefers that
workflow.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_ENV_FILE = "~/.fleet-manager/env"


def load_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Populate ``os.environ`` with KEY=value pairs from the env file.

    Returns the dict of keys that were actually set (i.e. weren't
    already present in the process env).  Missing file is not an
    error — returns ``{}`` silently.  Malformed lines log a warning
    and are skipped; the rest of the file still loads.

    Never overrides an existing env var.  Shell exports take priority.
    """
    raw_path = path or os.environ.get("FLEET_ENV_FILE") or DEFAULT_ENV_FILE
    resolved = Path(raw_path).expanduser()
    if not resolved.is_file():
        return {}

    applied: dict[str, str] = {}
    skipped_existing: list[str] = []
    malformed = 0

    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"env file {resolved} exists but couldn't be read: {exc}")
        return {}

    for lineno, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Accept optional "export " prefix so the file is also sourceable
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            malformed += 1
            logger.warning(
                f"env file {resolved}:{lineno}: no '=' — skipping: {raw!r}",
            )
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching surrounding quotes (shell-style)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            malformed += 1
            continue
        if key in os.environ:
            skipped_existing.append(key)
            continue
        os.environ[key] = value
        applied[key] = value

    if applied:
        logger.info(
            f"Loaded {len(applied)} env var(s) from {resolved} "
            f"(shell env already set {len(skipped_existing)})",
        )
    return applied
