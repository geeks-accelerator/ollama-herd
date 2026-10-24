"""CLI entry point for the herd-node command."""

from __future__ import annotations

import asyncio
import logging

import typer
from rich.logging import RichHandler

# Called inside the ``start()`` callback below, NOT at module scope: it mutates
# ``os.environ``, so at module scope merely *importing* this module injected the
# operator's whole env file into the importing process.  The ordering requirement
# still holds — pydantic-settings reads ``os.environ`` at instantiation, Typer
# runs this group callback before any subcommand, and ``NodeSettings`` is
# imported *inside* ``start()``.  See ``common/env_file.py``.
from fleet_manager.cli import removed_option, version_callback
from fleet_manager.common.env_file import load_env_file

app = typer.Typer(
    name="herd-node",
    help="Ollama Herd — Node Agent",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def start(
    node_id: str = typer.Option("", help="Node identifier (default: hostname)"),
    ollama_host: str = typer.Option("http://localhost:11434", help="Local Ollama URL"),
    router_url: str = typer.Option("", help="Router URL (auto-discovered via mDNS if empty)"),
    learn_capacity: bool = typer.Option(
        False, "--learn-capacity", help="Enable adaptive capacity learning (for work machines)"
    ),
    # Removed in 0.12.0 with the never-deployed platform; accepted and ignored
    # with a warning so an old start command still starts.  No ``envvar=``: a
    # leftover FLEET_NODE_PLATFORM_* variable is simply inert.  Delete in 0.14.0.
    platform_token: str = typer.Option(
        "",
        "--platform-token",
        "--operator-token",
        hidden=True,
        callback=removed_option,
        expose_value=False,
    ),
    platform_url: str = typer.Option(
        "", "--platform-url", hidden=True, callback=removed_option, expose_value=False
    ),
    telemetry_local_summary: bool = typer.Option(
        False,
        "--telemetry-local-summary",
        hidden=True,
        callback=removed_option,
        expose_value=False,
    ),
    telemetry_include_tags: bool = typer.Option(
        False,
        "--telemetry-include-tags",
        hidden=True,
        callback=removed_option,
        expose_value=False,
    ),
    log_level: str = typer.Option("INFO", help="Log level"),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=version_callback,
        is_eager=True,
        help="Show the installed version and exit",
    ),
):
    """Start the Herd Node agent on this device."""
    load_env_file()
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True)],
    )

    from fleet_manager.common.logging_config import setup_logging
    from fleet_manager.models.config import NodeSettings
    from fleet_manager.node.agent import NodeAgent

    # Use a per-process log name so the node and the router don't race at
    # midnight rotation when both run on the same machine (see the
    # logging_config.py docstring for the full failure mode).
    setup_logging(log_name="herd-node")

    settings_kwargs = {
        "ollama_host": ollama_host,
        "enable_capacity_learning": learn_capacity,
    }
    # `node_id` and `router_url` default to "" on the CLI, meaning "not given —
    # use the env var / auto-discovery". Passing that "" *explicitly* into
    # NodeSettings shadows the env var, because an explicit kwarg always beats
    # BaseSettings' env lookup. So only include them when the user actually set
    # them on the command line; otherwise let FLEET_NODE_NODE_ID /
    # FLEET_NODE_ROUTER_URL flow through. Without this, FLEET_NODE_NODE_ID could
    # never take effect and the node fell back to the macOS hostname — which is
    # network-derived when the static HostName is unset, so it silently changed
    # ("bb" at home, "Neons-Mac-Studio" travelling) and orphaned pins.
    if node_id:
        settings_kwargs["node_id"] = node_id
    if router_url:
        settings_kwargs["router_url"] = router_url
    settings = NodeSettings(**settings_kwargs)
    agent = NodeAgent(settings)

    typer.echo("Herd Node Agent")
    typer.echo(f"  Node ID:  {agent.node_id}")
    typer.echo(f"  Ollama:   {ollama_host}")
    if router_url:
        typer.echo(f"  Router:   {router_url}")
    else:
        typer.echo("  Router:   auto-discover via mDNS")
    if learn_capacity:
        typer.echo("  Capacity: adaptive learning enabled")
    typer.echo("")

    # Shown once, before the agent starts and therefore before anything could
    # ever be sent. ollamaherd.com/telemetry promises both of those, and that
    # the opt-out works "including the first run" -- which is why this is a
    # visible typer.echo and not a log line, and why it is gated on
    # settings.telemetry rather than shown unconditionally.
    from fleet_manager.common.telemetry_notice import show_first_run_notice_if_needed

    show_first_run_notice_if_needed(settings.telemetry, echo=typer.echo)

    asyncio.run(agent.start())


def main():
    app()


if __name__ == "__main__":
    main()
