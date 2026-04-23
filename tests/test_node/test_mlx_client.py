"""Tests for the node-side MLX client + collector merge behavior."""

from __future__ import annotations

import pytest

from fleet_manager.node.mlx_client import MlxClient, prefix_mlx

# ---------------------------------------------------------------------------
# prefix_mlx — tiny helper but load-bearing for routing
# ---------------------------------------------------------------------------


def test_prefix_mlx_adds_prefix():
    assert prefix_mlx("Qwen3-Coder-480B-A35B-4bit") == "mlx:Qwen3-Coder-480B-A35B-4bit"
    assert (
        prefix_mlx("mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit")
        == "mlx:mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
    )


def test_prefix_mlx_is_idempotent():
    # Avoid double-prefixing if the server already gave us a prefixed name
    assert prefix_mlx("mlx:foo") == "mlx:foo"


# ---------------------------------------------------------------------------
# MlxClient async methods (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_healthy_true(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        json={"object": "list", "data": []},
        status_code=200,
    )
    c = MlxClient("http://localhost:11440")
    try:
        assert await c.is_healthy() is True
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_is_healthy_false_on_500(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        status_code=500,
    )
    c = MlxClient("http://localhost:11440")
    try:
        assert await c.is_healthy() is False
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_get_available_models(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        json={
            "object": "list",
            "data": [
                {"id": "mlx-community/Qwen3-Coder-30B-A3B-4bit"},
                {"id": "mlx-community/Other-4bit"},
                {"id": ""},  # blank — should be filtered out
            ],
        },
    )
    c = MlxClient("http://localhost:11440")
    try:
        models = await c.get_available_models()
        # Blank ids are dropped; real ones preserved in order
        assert models == [
            "mlx-community/Qwen3-Coder-30B-A3B-4bit",
            "mlx-community/Other-4bit",
        ]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_get_available_models_empty_on_error(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        status_code=500,
    )
    c = MlxClient("http://localhost:11440")
    try:
        assert await c.get_available_models() == []
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# collector.collect_heartbeat — MLX models merged with mlx: prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_heartbeat_merges_mlx_models_with_prefix():
    from unittest.mock import AsyncMock, MagicMock, patch

    from fleet_manager.node.collector import collect_heartbeat

    # Mock Ollama — minimal behaviour
    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=["qwen3-coder:30b", "gpt-oss:120b"])

    # Mock MLX client
    mlx = MagicMock()
    mlx.get_available_models = AsyncMock(
        return_value=["mlx-community/Qwen3-Coder-480B-A35B-4bit"]
    )

    # collector now filters /v1/models down to the actually-running --model
    # arg by inspecting the live process — mock that to claim the 480B is
    # the loaded model so the merge logic still runs.
    with patch(
        "fleet_manager.node.mlx_client.get_running_mlx_model",
        return_value="mlx-community/Qwen3-Coder-480B-A35B-4bit",
    ):
        payload = await collect_heartbeat("test-node", ollama, mlx=mlx)
    models = payload.ollama.models_available
    # Ollama models present as-is
    assert "qwen3-coder:30b" in models
    assert "gpt-oss:120b" in models
    # MLX model prefixed with mlx:
    assert "mlx:mlx-community/Qwen3-Coder-480B-A35B-4bit" in models
    # Total count = 2 Ollama + 1 MLX (only the running --model is reported)
    assert len(models) == 3


@pytest.mark.asyncio
async def test_collect_heartbeat_without_mlx_unchanged():
    from unittest.mock import AsyncMock, MagicMock

    from fleet_manager.node.collector import collect_heartbeat

    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=["foo", "bar"])

    # mlx=None path (default) — baseline behaviour unchanged
    payload = await collect_heartbeat("test-node", ollama)
    assert payload.ollama.models_available == ["foo", "bar"]


@pytest.mark.asyncio
async def test_collect_heartbeat_handles_mlx_failures_gracefully():
    from unittest.mock import AsyncMock, MagicMock

    from fleet_manager.node.collector import collect_heartbeat

    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=["foo"])

    # MLX raises — the heartbeat should still succeed with just Ollama models
    mlx = MagicMock()
    mlx.get_available_models = AsyncMock(side_effect=RuntimeError("mlx down"))

    payload = await collect_heartbeat("test-node", ollama, mlx=mlx)
    assert payload.ollama.models_available == ["foo"]
