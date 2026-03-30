"""Node agent — runs on each device, collects metrics, sends heartbeats."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import socket
import subprocess

import httpx
import psutil

from fleet_manager.common.discovery import FleetServiceDiscoverer
from fleet_manager.common.ollama_client import OllamaClient
from fleet_manager.common.system_metrics import get_local_ip
from fleet_manager.models.config import NodeSettings
from fleet_manager.node.collector import collect_heartbeat
from fleet_manager.node.ollama_proxy import OllamaProxy

logger = logging.getLogger(__name__)

# How long to wait for Ollama to become healthy after starting it.
_OLLAMA_START_TIMEOUT = 30
_OLLAMA_POLL_INTERVAL = 1.0


class NodeAgent:
    def __init__(self, settings: NodeSettings):
        self.settings = settings
        self.node_id = settings.node_id or socket.gethostname().split(".")[0]
        self.ollama = OllamaClient(settings.ollama_host)
        self.router_url: str | None = settings.router_url or None
        self._http: httpx.AsyncClient | None = None
        self._running = False
        self._capacity_learner = None
        self._ollama_process: subprocess.Popen | None = None
        self._ollama_proxy: OllamaProxy | None = None
        self._image_server_task: asyncio.Task | None = None
        self._image_port: int = 0

    async def _ensure_ollama(self) -> bool:
        """Check if Ollama is running; if not, try to start it.

        Returns True if Ollama is healthy, False if we couldn't start it.
        """
        if await self.ollama.is_healthy():
            logger.info(f"Ollama is healthy at {self.settings.ollama_host}")
            return True

        logger.warning(
            f"Ollama is not reachable at {self.settings.ollama_host}, attempting to start it..."
        )

        ollama_bin = shutil.which("ollama")
        if not ollama_bin:
            logger.error(
                "Ollama binary not found in PATH. "
                "Install Ollama from https://ollama.com and try again."
            )
            return False

        # Start 'ollama serve' as a detached background process.
        # Bind to 0.0.0.0 so the router can reach us over the LAN.
        import os

        env = os.environ.copy()
        env.setdefault("OLLAMA_HOST", "0.0.0.0:11434")
        try:
            self._ollama_process = subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            logger.info(
                f"Started ollama serve (pid={self._ollama_process.pid}, "
                f"OLLAMA_HOST={env['OLLAMA_HOST']})"
            )
        except Exception as e:
            logger.error(f"Failed to start Ollama: {e}")
            return False

        # Wait for Ollama to become healthy
        for _i in range(int(_OLLAMA_START_TIMEOUT / _OLLAMA_POLL_INTERVAL)):
            await asyncio.sleep(_OLLAMA_POLL_INTERVAL)
            if await self.ollama.is_healthy():
                logger.info("Ollama is now running and healthy")
                return True

        logger.error(
            f"Ollama did not become healthy within {_OLLAMA_START_TIMEOUT}s. "
            "Check if another instance is already running or if port is in use."
        )
        return False

    async def start(self):
        """Main entry point. Discovers router, registers, starts polling."""
        self._running = True
        self._http = httpx.AsyncClient(timeout=10.0)

        # Ensure Ollama is running before we begin
        if not await self._ensure_ollama():
            logger.error("Cannot proceed without Ollama. Exiting.")
            return

        # Initialize capacity learner if enabled
        if self.settings.enable_capacity_learning:
            mem = psutil.virtual_memory()
            total_gb = mem.total / (1024**3)
            from fleet_manager.node.capacity_learner import AdaptiveCapacityLearner

            self._capacity_learner = AdaptiveCapacityLearner(
                total_memory_gb=total_gb,
                data_dir=self.settings.data_dir,
                node_id=self.node_id,
            )
            logger.info(
                f"Capacity learning enabled: {total_gb:.0f}GB total memory, "
                f"{self._capacity_learner.days_observed} days observed"
            )

        # Discover router if not configured
        if not self.router_url:
            logger.info("Discovering Fleet Manager router via mDNS...")
            discoverer = FleetServiceDiscoverer()
            ip, port = await discoverer.discover()
            self.router_url = f"http://{ip}:{port}"
            logger.info(f"Found router at {self.router_url}")
        else:
            logger.info(f"Using configured router at {self.router_url}")

        # Auto-start LAN proxy if Ollama is only on localhost
        await self._ensure_lan_proxy()

        # Start image generation server if mflux is available
        await self._ensure_image_server()

        # Install signal handlers for graceful drain
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._drain()))

        # Prime psutil cpu_percent (first call always returns 0.0)
        psutil.cpu_percent(interval=None)

        logger.info(f"Node agent started: {self.node_id}")
        logger.info(f"Ollama: {self.settings.ollama_host}")
        logger.info(f"Router: {self.router_url}")
        logger.info(f"Heartbeat interval: {self.settings.heartbeat_interval}s")

        self._ollama_failures = 0
        heartbeat_count = 0

        while self._running:
            try:
                payload = await collect_heartbeat(
                    self.node_id,
                    self.ollama,
                    self.settings.ollama_host,
                    capacity_learner=self._capacity_learner,
                )
                if self._image_port:
                    payload.image_port = self._image_port
                await self._send_heartbeat(payload)
                self._ollama_failures = 0
                heartbeat_count += 1

                # Log summary every 60 heartbeats (~5 min at 5s interval)
                if heartbeat_count % 60 == 0:
                    models_loaded = len(payload.ollama.models_loaded) if payload.ollama else 0
                    models_available = len(payload.ollama.models_available) if payload.ollama else 0
                    logger.info(
                        f"Heartbeat #{heartbeat_count}: "
                        f"cpu={payload.cpu.utilization_pct:.0f}%, "
                        f"mem={payload.memory.used_gb:.1f}/{payload.memory.total_gb:.0f}GB "
                        f"({payload.memory.pressure.value}), "
                        f"models={models_loaded} loaded/{models_available} available"
                    )

            except httpx.ConnectError as e:
                logger.warning(f"Cannot reach router at {self.router_url}: {e}")
            except httpx.ConnectTimeout:
                logger.warning(
                    f"Connection to router timed out at {self.router_url} "
                    f"(is the firewall blocking port?)"
                )
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Router rejected heartbeat: HTTP {e.response.status_code} "
                    f"from {self.router_url}"
                )
            except Exception as e:
                logger.warning(f"Heartbeat cycle failed: {type(e).__name__}: {e}")

                # If Ollama keeps failing, try to restart it
                if not await self.ollama.is_healthy():
                    self._ollama_failures += 1
                    logger.warning(
                        f"Ollama health check failed "
                        f"(consecutive failures: {self._ollama_failures})"
                    )
                    if self._ollama_failures >= 3:
                        logger.warning("Ollama appears down, attempting restart...")
                        await self._ensure_ollama()
                        self._ollama_failures = 0

            await asyncio.sleep(self.settings.heartbeat_interval)

    async def _ensure_lan_proxy(self):
        """Start a LAN proxy if Ollama is only listening on localhost.

        This makes Ollama reachable by the router over the network without
        requiring manual OLLAMA_HOST configuration on each node.
        """
        from urllib.parse import urlparse

        parsed = urlparse(self.settings.ollama_host)
        ollama_port = parsed.port or 11434

        lan_ip = get_local_ip()
        if not lan_ip or lan_ip == "127.0.0.1":
            return  # Can't determine LAN IP

        # Test if Ollama is already reachable on the LAN IP
        try:
            async with httpx.AsyncClient() as test:
                resp = await test.get(f"http://{lan_ip}:{ollama_port}/", timeout=2)
                if resp.status_code == 200:
                    logger.info(
                        f"Ollama already reachable on LAN at "
                        f"{lan_ip}:{ollama_port}, no proxy needed"
                    )
                    return
        except Exception:
            pass  # Not reachable — need the proxy

        self._ollama_proxy = OllamaProxy(
            listen_host=lan_ip,
            listen_port=ollama_port,
            target_host=parsed.hostname or "127.0.0.1",
            target_port=ollama_port,
        )
        if await self._ollama_proxy.start():
            logger.info(
                f"LAN proxy active: router can reach Ollama at http://{lan_ip}:{ollama_port}"
            )
        else:
            self._ollama_proxy = None

    async def _ensure_image_server(self):
        """Start a lightweight HTTP server for image generation if mflux is available."""
        from fleet_manager.node.collector import _detect_image_models

        image_metrics = _detect_image_models()
        if not image_metrics or not image_metrics.models_available:
            logger.debug("No mflux binaries found, skipping image server")
            return

        from urllib.parse import urlparse

        parsed = urlparse(self.settings.ollama_host)
        ollama_port = parsed.port or 11434
        image_port = ollama_port + 2  # 11434 → 11436

        try:
            import uvicorn
            from fastapi import FastAPI

            from fleet_manager.node.image_server import router as image_router

            app = FastAPI(title="Herd Image Server")
            app.include_router(image_router)

            config = uvicorn.Config(
                app, host="0.0.0.0", port=image_port, log_level="warning"
            )
            server = uvicorn.Server(config)
            self._image_server_task = asyncio.create_task(server.serve())
            self._image_port = image_port

            models = ", ".join(m.name for m in image_metrics.models_available)
            logger.info(
                f"Image server started on 0.0.0.0:{image_port} "
                f"(models: {models})"
            )
        except Exception as e:
            logger.warning(f"Failed to start image server: {repr(e)}")
            self._image_port = 0

    async def _send_heartbeat(self, payload):
        resp = await self._http.post(
            f"{self.router_url}/heartbeat",
            json=payload.model_dump(),
        )
        if resp.status_code != 200:
            logger.warning(f"Heartbeat response: HTTP {resp.status_code} body={resp.text[:200]}")
            resp.raise_for_status()

    async def _drain(self):
        """Graceful shutdown: signal the router, then stop."""
        logger.info("Drain signal received, shutting down...")
        if self._capacity_learner:
            self._capacity_learner.save()
        if self._http and self.router_url:
            try:
                await self._http.post(
                    f"{self.router_url}/heartbeat",
                    json={"node_id": self.node_id, "draining": True},
                )
            except Exception as e:
                logger.warning(f"Failed to send drain signal to router: {e}")
        self._running = False
        if self._image_server_task:
            self._image_server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._image_server_task
        if self._ollama_proxy:
            await self._ollama_proxy.stop()
        await self.ollama.close()
        if self._http:
            await self._http.aclose()
