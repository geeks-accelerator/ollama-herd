"""CLI entry point for the herd command."""

from __future__ import annotations

import logging

import typer
import uvicorn
from rich.logging import RichHandler

app = typer.Typer(
    name="herd",
    help="Ollama Herd — Smart Inference Router",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def start(
    host: str = typer.Option("0.0.0.0", help="Bind address"),
    port: int = typer.Option(8080, help="Listen port"),
    log_level: str = typer.Option("INFO", help="Log level"),
):
    """Start the Ollama Herd router and API server."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_time=True)],
    )

    from fleet_manager.models.config import ServerSettings
    from fleet_manager.server.app import create_app

    settings = ServerSettings(host=host, port=port)
    application = create_app(settings)

    typer.echo("")
    typer.echo("  Ollama Herd")
    typer.echo("  ───────────")
    typer.echo(f"  API (OpenAI):  http://localhost:{port}/v1")
    typer.echo(f"  API (Ollama):  http://localhost:{port}/api")
    typer.echo(f"  Fleet Status:  http://localhost:{port}/fleet/status")
    typer.echo(f"  Dashboard:     http://localhost:{port}/dashboard")
    typer.echo(f"  Models:        http://localhost:{port}/v1/models")
    typer.echo("")

    uvicorn.run(application, host=host, port=port, log_level=log_level.lower())


def main():
    app()


if __name__ == "__main__":
    main()
