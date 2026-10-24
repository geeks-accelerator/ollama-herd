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


def removed_option(ctx: typer.Context, param: typer.CallbackParam, value: object) -> None:
    """Accept a removed flag, ignore it, and say so -- instead of refusing to start.

    The gotomy.ai platform these flags connected to was never deployed, so the
    flags were removed in 0.12.0 (``docs/plans/remove-platform-integration.md``).
    Rejecting them would make Typer exit with "No such option", which under a
    launchd/systemd keep-alive agent is a crash loop -- an upgrade that looks
    like an outage.  So each removed flag stays as a hidden option wired to this
    callback with ``expose_value=False``: it parses, warns once on stderr, and
    never reaches the command.  The value is never echoed (``--platform-token``
    takes a credential).

    This is the one recorded exception to CLAUDE.md's no-shim rule, and it is
    temporary: delete this function and the options using it in 0.14.0
    (tracked in ``docs/issues.md``).
    """
    if not value:
        return
    names = "/".join(param.opts)
    typer.secho(
        f"  WARNING: {names} was removed in ollama-herd 0.12.0 -- the gotomy.ai "
        "platform it connected to was never deployed.  It is ignored; remove it "
        "from your start command (it stops being accepted in 0.14.0).",
        fg="yellow",
        err=True,
    )
