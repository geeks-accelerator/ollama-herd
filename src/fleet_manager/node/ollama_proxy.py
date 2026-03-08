"""Lightweight TCP reverse proxy for Ollama.

When Ollama is bound to localhost only (the default), this proxy listens on
the node's LAN IP and forwards traffic to localhost:11434, making Ollama
reachable by the router without requiring manual OLLAMA_HOST configuration.

This is transparent — the router connects to http://<lan_ip>:11434 and the
proxy pipes bytes to localhost:11434 and back.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Forward data from reader to writer until EOF."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
):
    """Proxy a single TCP connection to the target."""
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            target_host, target_port
        )
    except Exception as e:
        logger.debug(f"Proxy: cannot connect to {target_host}:{target_port}: {e}")
        client_writer.close()
        return

    await asyncio.gather(
        _pipe(client_reader, upstream_writer),
        _pipe(upstream_reader, client_writer),
    )


class OllamaProxy:
    """TCP reverse proxy: LAN IP → localhost Ollama."""

    def __init__(
        self,
        listen_host: str,
        listen_port: int = 11434,
        target_host: str = "127.0.0.1",
        target_port: int = 11434,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self._server: asyncio.Server | None = None

    async def start(self) -> bool:
        """Start the proxy server. Returns True if successful."""
        try:
            self._server = await asyncio.start_server(
                lambda r, w: _handle_connection(
                    r, w, self.target_host, self.target_port
                ),
                self.listen_host,
                self.listen_port,
            )
            logger.info(
                f"Ollama LAN proxy: {self.listen_host}:{self.listen_port} "
                f"-> {self.target_host}:{self.target_port}"
            )
            return True
        except OSError as e:
            logger.warning(
                f"Could not start Ollama proxy on "
                f"{self.listen_host}:{self.listen_port}: {e}"
            )
            return False

    async def stop(self):
        """Stop the proxy server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Ollama LAN proxy stopped")
