"""CLI entry point for the herd-node command."""

from __future__ import annotations

import asyncio
import logging

import typer
from rich.logging import RichHandler

# MUST run before any settings class is imported — pydantic-settings reads
# ``os.environ`` at instantiation.  See ``common/env_file.py``.
from fleet_manager.common.env_file import load_env_file

load_env_file()

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
    platform_token: str = typer.Option(
        "",
        "--platform-token",
        "--operator-token",
        envvar="FLEET_NODE_PLATFORM_TOKEN",
        help=(
            "Operator token for gotomy.ai (or use the "
            "dashboard Settings tab). Starts with 'herd_'. "
            "Accepts both --platform-token and --operator-token."
        ),
    ),
    platform_url: str = typer.Option(
        "",
        "--platform-url",
        envvar="FLEET_NODE_PLATFORM_URL",
        help="Platform URL (default: https://gotomy.ai)",
    ),
    telemetry_local_summary: bool = typer.Option(
        False,
        "--telemetry-local-summary",
        envvar="FLEET_NODE_TELEMETRY_LOCAL_SUMMARY",
        help=(
            "Send daily per-model usage aggregates to the platform. "
            "Opt-in, default off. Retained for 90 days rolling on the "
            "platform side. Never sends prompts, completions, or "
            "per-request data."
        ),
    ),
    telemetry_include_tags: bool = typer.Option(
        False,
        "--telemetry-include-tags",
        envvar="FLEET_NODE_TELEMETRY_INCLUDE_TAGS",
        help=(
            "When --telemetry-local-summary is on, also include "
            "per-tag request counts. Opt-in separately because tag "
            "values (e.g. 'project:internal-audit') can be mildly "
            "identifying. Default off."
        ),
    ),
    log_level: str = typer.Option("INFO", help="Log level"),
):
    """Start the Herd Node agent on this device."""
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
        "telemetry_local_summary": telemetry_local_summary,
        "telemetry_include_tags": telemetry_include_tags,
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
    if platform_url:
        settings_kwargs["platform_url"] = platform_url
    if platform_token:
        from pydantic import SecretStr

        settings_kwargs["platform_token"] = SecretStr(platform_token)
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
    if platform_token:
        url = platform_url or "https://gotomy.ai"
        typer.echo(f"  Platform: {url} (token supplied via flag)")
    typer.echo("")

    asyncio.run(agent.start())


def main():
    app()


if __name__ == "__main__":
    main()
