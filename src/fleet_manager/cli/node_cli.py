"""CLI entry point for the herd-node command."""

from __future__ import annotations

import asyncio
import logging

import typer
from rich.logging import RichHandler

app = typer.Typer(
    name="herd-node",
    help="Ollama Herd — Node Agent",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def start(
    node_id: str = typer.Option("", help="Node identifier (default: hostname)"),
    ollama_host: str = typer.Option(
        "http://localhost:11434", help="Local Ollama URL"
    ),
    router_url: str = typer.Option("", help="Router URL (auto-discovered via mDNS if empty)"),
    log_level: str = typer.Option("INFO", help="Log level"),
):
    """Start the Herd Node agent on this device."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True)],
    )

    from fleet_manager.models.config import NodeSettings
    from fleet_manager.node.agent import NodeAgent

    settings = NodeSettings(
        node_id=node_id,
        ollama_host=ollama_host,
        router_url=router_url,
    )
    agent = NodeAgent(settings)

    typer.echo("Herd Node Agent")
    typer.echo(f"  Node ID:  {agent.node_id}")
    typer.echo(f"  Ollama:   {ollama_host}")
    if router_url:
        typer.echo(f"  Router:   {router_url}")
    else:
        typer.echo("  Router:   auto-discover via mDNS")
    typer.echo("")

    asyncio.run(agent.start())


def main():
    app()


if __name__ == "__main__":
    main()
