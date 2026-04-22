"""Cloud connector — persistent WebSocket tunnel to gotomy.ai platform.

Receives inference requests from the platform, dispatches them to the local
fleet router, and streams responses back. Enables remote access to a local
fleet from anywhere on the internet without exposing any ports.

Protocol:
    Inbound (from platform):
        {"type": "request", "request_id": "...", "path": "/v1/chat/completions",
         "body": {...}, "stream": true}

    Outbound (to platform):
        {"type": "chunk",     "request_id": "...", "data": {...}}
        {"type": "done",      "request_id": "...", "tokens_in": N, "tokens_out": N}
        {"type": "response",  "request_id": "...", "data": {...}}   # non-streaming
        {"type": "error",     "request_id": "...", "error": "..."}
        {"type": "heartbeat"}
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)


class CloudConnector:
    """Maintains a persistent WebSocket tunnel to the platform."""

    def __init__(
        self,
        platform_url: str,
        fleet_token: str,
        local_herd_url: str = "http://localhost:11435",
    ):
        self.platform_url = platform_url.rstrip("/")
        self.fleet_token = fleet_token
        self.local_herd_url = local_herd_url.rstrip("/")
        self._ws: Any = None  # set while connected
        self._stop = asyncio.Event()

    def _ws_url(self) -> str:
        base = self.platform_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/api/fleet/connect?token={self.fleet_token}"

    async def run_forever(self) -> None:
        """Connect and reconnect indefinitely with exponential backoff."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                logger.info(f"[cloud] Connecting to {self.platform_url}")
                async with websockets.connect(
                    self._ws_url(),
                    ping_interval=30,
                    ping_timeout=60,
                    max_size=32 * 1024 * 1024,  # 32MB max message
                ) as ws:
                    self._ws = ws
                    logger.info("[cloud] Connected. Fleet is reachable via platform.")
                    backoff = 1.0
                    await self._handle(ws)
            except Exception as e:
                logger.warning(f"[cloud] Connection failed: {e}. Reconnecting in {backoff:.0f}s")
            finally:
                self._ws = None

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stop.set()

    async def _handle(self, ws) -> None:
        async for message in ws:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.warning(f"[cloud] Invalid JSON: {message[:100]}")
                continue

            if data.get("type") == "request":
                asyncio.create_task(self._process_request(ws, data))

    async def _process_request(self, ws, msg: dict[str, Any]) -> None:
        req_id = msg.get("request_id")
        path = msg.get("path", "")
        body = msg.get("body", {})
        stream = bool(msg.get("stream", False))

        url = f"{self.local_herd_url}{path}"

        if not req_id:
            logger.warning(f"[cloud] Missing request_id in message")
            return

        try:
            if stream:
                await self._stream_request(ws, req_id, url, body)
            else:
                await self._json_request(ws, req_id, url, body)
        except Exception as e:
            logger.exception(f"[cloud] Request {req_id} failed")
            await self._safe_send(ws, {
                "type": "error",
                "request_id": req_id,
                "error": str(e),
            })

    async def _stream_request(self, ws, req_id: str, url: str, body: dict) -> None:
        """Forward a streaming request to local herd and stream chunks back."""
        body_copy = dict(body)
        body_copy["stream"] = True

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream("POST", url, json=body_copy) as response:
                if response.status_code >= 400:
                    err = await response.aread()
                    await self._safe_send(ws, {
                        "type": "error",
                        "request_id": req_id,
                        "error": f"Local herd returned {response.status_code}: {err[:500].decode(errors='replace')}",
                    })
                    return

                tokens_in = None
                tokens_out = None

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue

                    # Handle both SSE (data: {...}) and NDJSON formats
                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            continue
                    else:
                        payload = line

                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    # Capture usage if present
                    usage = chunk.get("usage") if isinstance(chunk, dict) else None
                    if usage:
                        tokens_in = usage.get("prompt_tokens")
                        tokens_out = usage.get("completion_tokens")

                    await self._safe_send(ws, {
                        "type": "chunk",
                        "request_id": req_id,
                        "data": chunk,
                    })

                await self._safe_send(ws, {
                    "type": "done",
                    "request_id": req_id,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                })

    async def _json_request(self, ws, req_id: str, url: str, body: dict) -> None:
        """Forward a non-streaming request and return full JSON response."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            response = await client.post(url, json=body)
            if response.status_code >= 400:
                await self._safe_send(ws, {
                    "type": "error",
                    "request_id": req_id,
                    "error": f"Local herd returned {response.status_code}: {response.text[:500]}",
                })
                return

            data = response.json()
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            await self._safe_send(ws, {
                "type": "response",
                "request_id": req_id,
                "data": data,
                "tokens_in": usage.get("prompt_tokens"),
                "tokens_out": usage.get("completion_tokens"),
            })

    async def _safe_send(self, ws, msg: dict) -> None:
        try:
            await ws.send(json.dumps(msg))
        except Exception:
            logger.exception("[cloud] Failed to send message")
