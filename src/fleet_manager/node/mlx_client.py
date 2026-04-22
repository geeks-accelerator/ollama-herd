"""Node-side client for talking to a local `mlx_lm.server`.

Parallel to :mod:`fleet_manager.common.ollama_client` but intentionally
minimal — mlx_lm.server only exposes `/v1/models` and `/v1/chat/completions`.
We don't need a streaming client here because inference goes through the
server-side :class:`fleet_manager.server.mlx_proxy.MlxProxy`; the node just
needs to advertise MLX models in its heartbeat.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


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
