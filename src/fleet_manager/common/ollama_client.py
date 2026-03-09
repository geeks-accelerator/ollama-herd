"""Async HTTP client for the Ollama API. Shared by node agent and server."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from fleet_manager.models.node import LoadedModel

logger = logging.getLogger(__name__)


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=10.0),
        )

    async def is_healthy(self) -> bool:
        try:
            resp = await self._client.get("/")
            return resp.status_code == 200
        except httpx.ConnectError:
            logger.debug(f"Ollama health check: connection refused at {self._base_url}")
            return False
        except httpx.ConnectTimeout:
            logger.debug(f"Ollama health check: connection timed out at {self._base_url}")
            return False
        except Exception as e:
            logger.debug(f"Ollama health check failed: {type(e).__name__}: {e}")
            return False

    async def get_running_models(self) -> list[LoadedModel]:
        """GET /api/ps — models currently loaded in memory."""
        try:
            resp = await self._client.get("/api/ps")
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("models", []):
                size_bytes = m.get("size", 0)
                size_gb = round(size_bytes / (1024**3), 2)
                name = m.get("model", m.get("name", "unknown"))
                details = m.get("details", {})
                models.append(
                    LoadedModel(
                        name=name,
                        size_gb=size_gb,
                        parameter_size=details.get("parameter_size", ""),
                        quantization=details.get("quantization_level", ""),
                        context_length=m.get("context_length", 0),
                    )
                )
            return models
        except httpx.HTTPStatusError as e:
            logger.warning(f"Ollama /api/ps returned HTTP {e.response.status_code}")
            return []
        except Exception as e:
            logger.debug(f"Failed to get running models: {type(e).__name__}: {e}")
            return []

    async def get_available_models(self) -> list[str]:
        """GET /api/tags — all models available on disk."""
        try:
            resp = await self._client.get("/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [m.get("model", m.get("name", "")) for m in data.get("models", [])]
        except httpx.HTTPStatusError as e:
            logger.warning(f"Ollama /api/tags returned HTTP {e.response.status_code}")
            return []
        except Exception as e:
            logger.debug(f"Failed to get available models: {type(e).__name__}: {e}")
            return []

    async def chat_stream(self, body: dict) -> AsyncIterator[bytes]:
        """POST /api/chat with streaming. Yields raw response lines."""
        async with self._client.stream("POST", "/api/chat", json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    yield line.encode()

    async def generate_stream(self, body: dict) -> AsyncIterator[bytes]:
        """POST /api/generate with streaming. Yields raw response lines."""
        async with self._client.stream("POST", "/api/generate", json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    yield line.encode()

    async def chat(self, body: dict) -> dict:
        """POST /api/chat without streaming."""
        body["stream"] = False
        resp = await self._client.post("/api/chat", json=body)
        resp.raise_for_status()
        return resp.json()

    async def get_tags_raw(self) -> dict:
        """GET /api/tags — raw response for proxying."""
        resp = await self._client.get("/api/tags")
        resp.raise_for_status()
        return resp.json()

    async def get_ps_raw(self) -> dict:
        """GET /api/ps — raw response for proxying."""
        resp = await self._client.get("/api/ps")
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self._client.aclose()
