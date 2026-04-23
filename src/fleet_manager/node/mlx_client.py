"""Node-side client for talking to a local `mlx_lm.server`.

Parallel to :mod:`fleet_manager.common.ollama_client` but intentionally
minimal — mlx_lm.server only exposes `/v1/models` and `/v1/chat/completions`.
We don't need a streaming client here because inference goes through the
server-side :class:`fleet_manager.server.mlx_proxy.MlxProxy`; the node just
needs to advertise MLX models in its heartbeat.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import psutil

logger = logging.getLogger(__name__)


def get_running_mlx_model() -> str | None:
    """Return the model id mlx_lm.server was started with, or None if not running.

    mlx_lm.server's ``/v1/models`` endpoint advertises every model it can
    *find* on disk (HF cache scan + the explicit ``--model`` argument).  Only
    the ``--model`` arg is actually loaded into GPU memory — the rest are just
    discoverable.  We parse the running process's command line to identify
    which one that is, so the dashboard reports loaded ≠ discoverable.
    """
    for proc in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not any("mlx_lm.server" in str(c) for c in cmdline):
            continue
        # Find the --model X arg
        for i, arg in enumerate(cmdline):
            if arg == "--model" and i + 1 < len(cmdline):
                model_arg = cmdline[i + 1]
                # If it's a path, derive the HF id from the directory layout:
                #   .../models--mlx-community--Qwen3-Coder-30B.../snapshots/<sha>
                #   → mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit
                if "/" in model_arg and "models--" in model_arg:
                    parts = Path(model_arg).parts
                    for p in parts:
                        if p.startswith("models--"):
                            # "models--mlx-community--Qwen3..." → "mlx-community/Qwen3..."
                            return p.removeprefix("models--").replace("--", "/", 1)
                return model_arg
    return None


class MlxClient:
    """Tiny async client for mlx_lm.server health + model listing."""

    def __init__(self, base_url: str = "http://localhost:11440"):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=5.0),
        )

    async def is_healthy(self) -> bool:
        """True iff mlx_lm.server answers GET /v1/models with 200."""
        try:
            resp = await self._client.get("/v1/models")
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return False
        except Exception as exc:  # noqa: BLE001 — we want any failure to mean "unhealthy"
            logger.debug(f"MLX health check failed: {type(exc).__name__}: {exc}")
            return False

    async def get_available_models(self) -> list[str]:
        """Return model IDs from mlx_lm.server (no ``mlx:`` prefix applied)."""
        try:
            resp = await self._client.get("/v1/models")
            resp.raise_for_status()
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"MLX /v1/models failed: {type(exc).__name__}: {exc}")
            return []

    async def close(self) -> None:
        await self._client.aclose()


def prefix_mlx(model_id: str) -> str:
    """Add the ``mlx:`` prefix used by herd routing when advertising.

    Idempotent — if the id is already prefixed, returns it unchanged.
    """
    if model_id.startswith("mlx:"):
        return model_id
    return f"mlx:{model_id}"
