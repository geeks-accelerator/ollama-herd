"""mDNS service advertisement and discovery via zeroconf."""

from __future__ import annotations

import asyncio
import logging
import socket

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from fleet_manager.common.system_metrics import get_local_ip

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_fleet-manager._tcp.local."


class FleetServiceAdvertiser:
    """Registers the router as an mDNS service so nodes can discover it."""

    def __init__(self, port: int, service_name: str = "Fleet Manager Router"):
        self._port = port
        self._service_name = service_name
        self._azc: AsyncZeroconf | None = None

    async def start(self):
        ip = get_local_ip()
        self._azc = AsyncZeroconf()
        info = AsyncServiceInfo(
            SERVICE_TYPE,
            f"{self._service_name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=self._port,
            properties={b"version": b"0.1.0"},
        )
        await self._azc.async_register_service(info)
        logger.info(f"mDNS: advertising Fleet Manager at {ip}:{self._port}")

    async def stop(self):
        if self._azc:
            await self._azc.async_close()
            logger.info("mDNS: unregistered Fleet Manager service")


class FleetServiceDiscoverer:
    """Browses for the router's mDNS service. Used by node agents."""

    def __init__(self):
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None

    async def discover(self, timeout: float = 30.0) -> tuple[str, int]:
        """Block until router is found via mDNS. Returns (ip, port)."""
        found = asyncio.Event()
        result: dict[str, str | int] = {}

        def on_state_change(
            zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ):
            if state_change == ServiceStateChange.Added:
                asyncio.ensure_future(
                    self._resolve_service(zeroconf, service_type, name, result, found)
                )

        self._azc = AsyncZeroconf()
        self._browser = AsyncServiceBrowser(
            self._azc.zeroconf, SERVICE_TYPE, handlers=[on_state_change]
        )

        try:
            await asyncio.wait_for(found.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Could not discover Fleet Manager router via mDNS within {timeout}s. "
                "Use --router-url to specify manually."
            )
        finally:
            await self.stop()

        return str(result["ip"]), int(result["port"])

    async def _resolve_service(self, zeroconf, service_type, name, result, found):
        info = AsyncServiceInfo(service_type, name)
        await info.async_request(zeroconf, 3000)
        if info.addresses:
            addr = socket.inet_ntoa(info.addresses[0])
            result["ip"] = addr
            result["port"] = info.port
            found.set()

    async def stop(self):
        if self._browser:
            self._browser.cancel()
        if self._azc:
            await self._azc.async_close()
