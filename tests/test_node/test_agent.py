"""Tests for the node agent's Ollama auto-start functionality."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fleet_manager.models.config import NodeSettings
from fleet_manager.node.agent import NodeAgent


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
