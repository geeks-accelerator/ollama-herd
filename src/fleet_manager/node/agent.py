"""Node agent — runs on each device, collects metrics, sends heartbeats."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket

import httpx
import psutil

from fleet_manager.common.discovery import FleetServiceDiscoverer
from fleet_manager.common.ollama_client import OllamaClient
from fleet_manager.models.config import NodeSettings
from fleet_manager.node.collector import collect_heartbeat

logger = logging.getLogger(__name__)


class NodeAgent:
    def __init__(self, settings: NodeSettings):
        self.settings = settings
        self.node_id = settings.node_id or socket.gethostname().split(".")[0]
        self.ollama = OllamaClient(settings.ollama_host)
        self.router_url: str | None = settings.router_url or None
        self._http: httpx.AsyncClient | None = None
        self._running = False

    async def start(self):
        """Main entry point. Discovers router, registers, starts polling."""
        self._running = True
        self._http = httpx.AsyncClient(timeout=10.0)

        # Discover router if not configured
        if not self.router_url:
            logger.info("Discovering Fleet Manager router via mDNS...")
            discoverer = FleetServiceDiscoverer()
            ip, port = await discoverer.discover()
            self.router_url = f"http://{ip}:{port}"
            logger.info(f"Found router at {self.router_url}")
        else:
            logger.info(f"Using configured router at {self.router_url}")

        # Install signal handlers for graceful drain
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._drain()))

        # Prime psutil cpu_percent (first call always returns 0.0)
        psutil.cpu_percent(interval=None)

        logger.info(f"Node agent started: {self.node_id}")
        logger.info(f"Ollama: {self.settings.ollama_host}")

        while self._running:
            try:
                payload = await collect_heartbeat(
                    self.node_id, self.ollama, self.settings.ollama_host
                )
                await self._send_heartbeat(payload)
            except httpx.ConnectError:
                logger.warning(f"Cannot reach router at {self.router_url}")
            except Exception as e:
                logger.warning(f"Heartbeat cycle failed: {e}")
            await asyncio.sleep(self.settings.heartbeat_interval)

    async def _send_heartbeat(self, payload):
        await self._http.post(
            f"{self.router_url}/heartbeat",
            json=payload.model_dump(),
        )

    async def _drain(self):
        """Graceful shutdown: signal the router, then stop."""
        logger.info("Drain signal received, shutting down...")
        if self._http and self.router_url:
            try:
                await self._http.post(
                    f"{self.router_url}/heartbeat",
                    json={"node_id": self.node_id, "draining": True},
                )
            except Exception:
                pass
        self._running = False
        await self.ollama.close()
        if self._http:
            await self._http.aclose()
