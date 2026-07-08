"""Tests for the node agent's Ollama auto-start functionality."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fleet_manager.models.config import NodeSettings
from fleet_manager.node.agent import NodeAgent


def _free_port() -> int:
    """Grab an unused TCP port (small race, fine for tests)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def agent():
    settings = NodeSettings(
        node_id="test-node",
        ollama_host="http://localhost:11434",
        router_url="http://localhost:4373",
    )
    return NodeAgent(settings)


@pytest.mark.asyncio
async def test_ensure_ollama_already_running(agent):
    """If Ollama is already healthy, _ensure_ollama returns True immediately."""
    agent.ollama.is_healthy = AsyncMock(return_value=True)

    result = await agent._ensure_ollama()

    assert result is True
    agent.ollama.is_healthy.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_ollama_starts_when_down(agent):
    """If Ollama is down, _ensure_ollama starts it and waits for health."""
    # First call: not healthy. Second call (after start): healthy.
    agent.ollama.is_healthy = AsyncMock(side_effect=[False, True])

    with patch("fleet_manager.node.agent.shutil.which", return_value="/usr/local/bin/ollama"), \
         patch("fleet_manager.node.agent.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=12345)

        result = await agent._ensure_ollama()

    assert result is True
    mock_popen.assert_called_once()
    assert agent._ollama_process is not None
    assert agent._ollama_process.pid == 12345


@pytest.mark.asyncio
async def test_ensure_ollama_binary_not_found(agent):
    """If ollama binary is not in PATH, return False."""
    agent.ollama.is_healthy = AsyncMock(return_value=False)

    with patch("fleet_manager.node.agent.shutil.which", return_value=None):
        result = await agent._ensure_ollama()

    assert result is False


@pytest.mark.asyncio
async def test_ensure_ollama_start_fails(agent):
    """If subprocess.Popen raises, return False."""
    agent.ollama.is_healthy = AsyncMock(return_value=False)

    with patch("fleet_manager.node.agent.shutil.which", return_value="/usr/local/bin/ollama"), \
         patch("fleet_manager.node.agent.subprocess.Popen", side_effect=OSError("permission denied")):
        result = await agent._ensure_ollama()

    assert result is False


@pytest.mark.asyncio
async def test_ensure_ollama_timeout(agent):
    """If Ollama never becomes healthy within the timeout, return False."""
    agent.ollama.is_healthy = AsyncMock(return_value=False)

    with patch("fleet_manager.node.agent.shutil.which", return_value="/usr/local/bin/ollama"), \
         patch("fleet_manager.node.agent.subprocess.Popen") as mock_popen, \
         patch("fleet_manager.node.agent._OLLAMA_START_TIMEOUT", 2), \
         patch("fleet_manager.node.agent._OLLAMA_POLL_INTERVAL", 1.0):
        mock_popen.return_value = MagicMock(pid=99999)

        result = await agent._ensure_ollama()

    assert result is False


@pytest.mark.asyncio
async def test_start_exits_if_ollama_unavailable(agent):
    """Agent.start() should return early if Ollama can't be started."""
    agent.ollama.is_healthy = AsyncMock(return_value=False)

    with patch("fleet_manager.node.agent.shutil.which", return_value=None):
        # start() should return without entering the heartbeat loop
        await agent.start()

    # _running should have been set True then exited gracefully
    assert agent._running is True  # set at top of start()


# --- Embedding sidecar bind failure must not crash the node ---
# Regression for docs/issues/embedding-sidecar-bind-failure-crashes-node.md:
# uvicorn's sys.exit(1) on a busy port used to escape create_task() as a
# SystemExit and kill the whole node. We now bind eagerly and fail soft.


def test_bind_sidecar_socket_free_port_returns_socket(agent):
    """A free port yields a bound socket the caller can hand to uvicorn."""
    port = _free_port()

    sock = agent._bind_sidecar_socket("127.0.0.1", port, "test sidecar")

    assert sock is not None
    assert sock.getsockname()[1] == port
    sock.close()


def test_bind_sidecar_socket_busy_port_returns_none(agent):
    """A busy port returns None (fail soft) instead of raising."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    busy_port = holder.getsockname()[1]

    try:
        result = agent._bind_sidecar_socket("127.0.0.1", busy_port, "test sidecar")
        assert result is None  # no OSError, no SystemExit — just soft failure
    finally:
        holder.close()


@pytest.mark.asyncio
async def test_ensure_embedding_server_survives_port_conflict():
    """A busy embedding port disables the sidecar but keeps the node alive.

    Without the eager bind, uvicorn would sys.exit(1) inside the asyncio task
    and the SystemExit would propagate out of the event loop, exiting the
    process. Here the method must simply return with the port set to 0.
    """
    # Pick a free port P, then point ollama_host at P-4 so the vision embedding
    # port (ollama_port + 4) resolves to exactly P. Hold P so the bind conflicts.
    port = _free_port()
    settings = NodeSettings(
        node_id="test-node",
        ollama_host=f"http://localhost:{port - 4}",
        router_url="http://localhost:4373",
    )
    agent = NodeAgent(settings)

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("0.0.0.0", port))
    holder.listen(1)

    fake_metrics = MagicMock()
    fake_metrics.models_available = [MagicMock(name="dinov2-vit-s14")]

    try:
        with patch(
            "fleet_manager.node.collector._detect_vision_embedding_models",
            return_value=fake_metrics,
        ):
            # Must not raise SystemExit or anything else.
            await agent._ensure_embedding_server()

        assert agent._embedding_port == 0
        assert agent._embedding_server_task is None
    finally:
        holder.close()
