"""CLI entry point for the herd command."""

from __future__ import annotations

import asyncio
import logging
import os

import typer
import uvicorn
from rich.logging import RichHandler

# Load ~/.fleet-manager/env before any settings class instantiates.
# ``ServerSettings`` is only imported inside ``start()`` below, so calling
# ``load_env_file()`` here at module-import time is early enough.
# See ``fleet_manager/common/env_file.py`` for the rationale.
from fleet_manager.common.env_file import load_env_file

load_env_file()

app = typer.Typer(
    name="herd",
    help="Ollama Herd — Smart Inference Router",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def start(
    ctx: typer.Context,
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
    # If a subcommand was invoked (e.g. `herd mlx pull ...`), don't run the
    # main server-start logic — let the subcommand handler run instead.
    if ctx.invoked_subcommand is not None:
        return
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


# ---------------------------------------------------------------------------
# `herd mlx` subcommand group — manage the MLX backend (see
# docs/plans/mlx-backend-for-large-models.md).
# ---------------------------------------------------------------------------


mlx_app = typer.Typer(name="mlx", help="Manage the MLX backend for large models")
app.add_typer(mlx_app, name="mlx")


@mlx_app.command("pull")
def mlx_pull(
    model: str = typer.Argument(
        ...,
        help=(
            "Hugging Face repo id or local path "
            "(e.g. 'mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit')"
        ),
    ),
    cache_dir: str = typer.Option(
        None,
        "--cache-dir",
        help="Override HF cache dir (default: $HF_HOME or ~/.cache/huggingface)",
    ),
):
    """Download an MLX-quantized model from Hugging Face.

    The model name should be the full HF repo id (not prefixed with ``mlx:``).
    Use the result in ``FLEET_ANTHROPIC_MODEL_MAP`` as ``mlx:<repo-id>``.
    """
    # Strip any accidental mlx: prefix the user might type out of habit
    if model.startswith("mlx:"):
        model = model[4:]

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        typer.secho(
            "huggingface_hub not installed. Run: uv pip install huggingface_hub",
            fg="red",
        )
        raise typer.Exit(1) from None

    import time

    typer.echo(f"  Pulling MLX model: {model}")
    typer.echo("  (this can take a while for large models — progress below)")
    t0 = time.time()
    try:
        path = snapshot_download(repo_id=model, cache_dir=cache_dir)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"  ERROR: {type(exc).__name__}: {exc}", fg="red")
        raise typer.Exit(1) from exc

    elapsed = time.time() - t0
    typer.echo("")
    typer.secho(f"  ✓ Downloaded to {path}", fg="green")
    typer.echo(f"  Took {elapsed:.1f}s")
    typer.echo("")
    typer.echo("  Next: map a Claude tier to this model in FLEET_ANTHROPIC_MODEL_MAP:")
    typer.echo(f'    "claude-opus-4-7": "mlx:{model}"')


@mlx_app.command("list")
def mlx_list(
    url: str = typer.Option(
        "http://localhost:11440", "--url",
        help="mlx_lm.server URL to query",
    ),
):
    """List models advertised by a running ``mlx_lm.server``.

    If the server isn't running, says so clearly.  This reads ``GET /v1/models``
    on the configured endpoint — it doesn't walk the HF cache directory.
    """
    import httpx

    try:
        resp = httpx.get(f"{url.rstrip('/')}/v1/models", timeout=5.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        typer.secho(
            f"  mlx_lm.server not reachable at {url}",
            fg="yellow",
        )
        typer.echo("  Start one with: herd mlx serve <model-id>")
        raise typer.Exit(1) from None
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"  ERROR querying {url}: {exc}", fg="red")
        raise typer.Exit(1) from exc

    data = resp.json().get("data", [])
    if not data:
        typer.echo("  (no models advertised)")
        return
    typer.echo(f"  Models at {url}:")
    for m in data:
        mid = m.get("id", "?")
        typer.echo(f"    mlx:{mid}")


@mlx_app.command("serve")
def mlx_serve(
    model: str = typer.Argument(..., help="HF repo id or local path"),
    port: int = typer.Option(11440, "--port", help="Listen port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    kv_bits: int = typer.Option(
        0, "--kv-bits",
        help="KV cache quantization (4 or 8 — requires patched mlx_lm.server; 0 = f16 default)",
    ),
):
    """Start ``mlx_lm.server`` as a foreground process.

    Convenience wrapper over ``mlx_lm.server`` with sensible defaults for
    Claude Code workloads (large prompt cache).  Same as running
    ``mlx_lm.server`` directly — use this if you want to keep the config
    consistent with what ``herd-node --mlx-auto-start`` would use.
    """
    from fleet_manager.node.mlx_supervisor import find_mlx_lm_binary

    binary = find_mlx_lm_binary()
    if binary is None:
        typer.secho(
            "  mlx_lm.server binary not found. Install with:\n"
            "    uv tool install mlx-lm",
            fg="red",
        )
        raise typer.Exit(1)

    cmd = [
        binary,
        "--model", model,
        "--host", host,
        "--port", str(port),
        "--prompt-cache-size", "4",
        "--prompt-cache-bytes", str(17_179_869_184),
        "--log-level", "INFO",
    ]
    if kv_bits in (4, 8):
        cmd += ["--kv-bits", str(kv_bits), "--kv-group-size", "64"]

    typer.echo(f"  Starting {binary} on {host}:{port}")
    typer.echo(f"  Model: {model}")
    if kv_bits:
        typer.echo(f"  KV cache: {kv_bits}-bit quantized")
    typer.echo("")

    import os as _os
    _os.execvp(cmd[0], cmd)  # replace our process — Ctrl+C goes straight to mlx


def main():
    app()


if __name__ == "__main__":
    main()
