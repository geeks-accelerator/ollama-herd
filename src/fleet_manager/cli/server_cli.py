"""CLI entry point for the herd command."""

from __future__ import annotations

import asyncio
import logging
import os

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
    port: int = typer.Option(11435, help="Listen port"),
    log_level: str = typer.Option("INFO", help="Log level"),
    cloud: bool = typer.Option(False, "--cloud", help="Enable cloud tunnel to gotomy.ai"),
    cloud_token: str = typer.Option(
        None, "--token",
        help="Fleet token from gotomy.ai (or set CLOUD_FLEET_TOKEN env var)",
    ),
    cloud_url: str = typer.Option(
        "https://gotomy.ai", "--cloud-url",
        help="Platform URL (default: https://gotomy.ai)",
    ),
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
    typer.echo("  Logs (JSONL):  ~/.fleet-manager/logs/herd.jsonl")

    if cloud:
        token = cloud_token or os.environ.get("CLOUD_FLEET_TOKEN")
        if not token:
            typer.echo("")
            typer.secho(
                "  ERROR: --cloud requires --token or CLOUD_FLEET_TOKEN env var.",
                fg="red",
            )
            typer.echo("  Get a token at https://gotomy.ai → sign in → Create fleet")
            raise typer.Exit(1)

        from fleet_manager.cloud import CloudConnector

        connector = CloudConnector(
            platform_url=cloud_url,
            fleet_token=token,
            local_herd_url=f"http://localhost:{port}",
        )

        typer.echo("")
        typer.echo(f"  Cloud tunnel:  {cloud_url} → local fleet")
        typer.echo("  Remote clients can now hit this fleet via gotomy.ai")

        # Run uvicorn + connector in the same event loop
        async def _run():
            import uvicorn as _u
            config = _u.Config(application, host=host, port=port, log_level=log_level.lower())
            server = _u.Server(config)
            await asyncio.gather(
                server.serve(),
                connector.run_forever(),
            )

        typer.echo("")
        asyncio.run(_run())
        return

    typer.echo("")
    uvicorn.run(application, host=host, port=port, log_level=log_level.lower())


def main():
    app()


if __name__ == "__main__":
    main()
