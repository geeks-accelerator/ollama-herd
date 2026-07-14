"""Tests for the node-side MLX prefix helper + collector merge behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_manager.node.mlx_client import prefix_mlx

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
# collector.collect_heartbeat — MLX models merged with mlx: prefix
#
# The MLX backend is driven entirely by the MlxSupervisorSet: its per-server
# statuses populate heartbeat.mlx_servers, and each *healthy* server's model
# is merged into models_available with an mlx: prefix.
# ---------------------------------------------------------------------------


def _fake_supervisor_set(statuses):
    """A stand-in MlxSupervisorSet exposing just what collect_heartbeat calls."""
    s = MagicMock()
    s.refresh_health = AsyncMock(return_value=None)
    s.statuses = MagicMock(return_value=statuses)
    return s


def _status(model, status="healthy", port=11440):
    st = MagicMock()
    st.port = port
    st.model = model
    st.status = status
    st.status_reason = ""
    st.kv_bits = 0
    st.model_size_gb = 0.0
    st.last_ok_ts = 0.0
    st.distributed = False
    st.backend = ""
    st.node_count = 1
    return st


@pytest.mark.asyncio
async def test_collect_heartbeat_merges_healthy_mlx_models_with_prefix():
    from fleet_manager.node.collector import collect_heartbeat

    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=["qwen3-coder:30b", "gpt-oss:120b"])

    supervisor_set = _fake_supervisor_set([
        _status("mlx-community/Qwen3-Coder-480B-A35B-4bit", status="healthy"),
    ])

    payload = await collect_heartbeat(
        "test-node", ollama, mlx_supervisor_set=supervisor_set,
    )
    models = payload.ollama.models_available
    assert "qwen3-coder:30b" in models
    assert "gpt-oss:120b" in models
    # Healthy MLX server's model is advertised with the mlx: prefix
    assert "mlx:mlx-community/Qwen3-Coder-480B-A35B-4bit" in models
    assert len(models) == 3
    # And it surfaces in the structured mlx_servers list
    assert [s.model for s in payload.mlx_servers] == [
        "mlx-community/Qwen3-Coder-480B-A35B-4bit"
    ]


@pytest.mark.asyncio
async def test_collect_heartbeat_skips_unhealthy_mlx_models_but_reports_status():
    from fleet_manager.node.collector import collect_heartbeat

    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=["foo"])

    # A configured-but-unhealthy server must NOT be advertised as serveable,
    # yet must still appear in mlx_servers so the dashboard shows the failure.
    supervisor_set = _fake_supervisor_set([
        _status("some/broken-model", status="memory_blocked", port=11441),
    ])

    payload = await collect_heartbeat(
        "test-node", ollama, mlx_supervisor_set=supervisor_set,
    )
    assert payload.ollama.models_available == ["foo"]
    assert [s.status for s in payload.mlx_servers] == ["memory_blocked"]


@pytest.mark.asyncio
async def test_collect_heartbeat_without_mlx_unchanged():
    from fleet_manager.node.collector import collect_heartbeat

    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=["foo", "bar"])

    # No supervisor set (default) — baseline behaviour unchanged
    payload = await collect_heartbeat("test-node", ollama)
    assert payload.ollama.models_available == ["foo", "bar"]
    assert payload.mlx_servers == []


@pytest.mark.asyncio
async def test_collect_heartbeat_handles_mlx_failures_gracefully():
    from fleet_manager.node.collector import collect_heartbeat

    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=["foo"])

    # Supervisor status collection raises — the heartbeat should still succeed
    # with just Ollama models.
    supervisor_set = MagicMock()
    supervisor_set.refresh_health = AsyncMock(side_effect=RuntimeError("mlx down"))
    supervisor_set.statuses = MagicMock(side_effect=RuntimeError("mlx down"))

    payload = await collect_heartbeat(
        "test-node", ollama, mlx_supervisor_set=supervisor_set,
    )
    assert payload.ollama.models_available == ["foo"]
