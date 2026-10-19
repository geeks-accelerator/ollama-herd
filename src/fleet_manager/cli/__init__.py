"""Shared helpers for the ``herd`` and ``herd-node`` entry points."""

from __future__ import annotations

import typer


def version_callback(value: bool) -> None:
    """Print the installed version and exit.

    Wired as an eager option so it runs before the group callback body —
    ``herd --version`` never loads the env file, binds a port, or starts a
    server.  The download page uses it as the post-install verify step.
    """
    if value:
        from fleet_manager import __version__

        typer.echo(f"ollama-herd {__version__}")
        raise typer.Exit()
