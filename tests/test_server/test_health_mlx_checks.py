"""Tests for MLX-aware health checks added in Phase 5.

Isolates the two new check methods (_check_mlx_backend, _check_mapped_models_hot)
and feeds them stub nodes without hitting real registry / trace infrastructure.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from fleet_manager.server.health_engine import HealthEngine, Severity


def _make_node(
    *,
    node_id="test-node",
    status="online",
    loaded=None,
    available=None,
):
    """Build a minimal stub node that matches the shape HealthEngine reads."""
    loaded_stub = [SimpleNamespace(name=n) for n in (loaded or [])]
    return SimpleNamespace(
        node_id=node_id,
        status=SimpleNamespace(value=status),
        ollama=SimpleNamespace(
            models_loaded=loaded_stub,
            models_available=list(available or []),
        ),
    )


# ---------------------------------------------------------------------------
# _check_mlx_backend — INFO when MLX active
# ---------------------------------------------------------------------------


def test_mlx_backend_info_when_mlx_models_present():
    engine = HealthEngine()
    node = _make_node(
        available=[
            "qwen3-coder:30b",
            "mlx:mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
            "mlx:Other-4bit",
        ],
    )
    recs = engine._check_mlx_backend([node])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.check_id == "mlx_backend_active"
    assert rec.severity == Severity.INFO
    assert rec.data["count"] == 2
    assert all(m.startswith("mlx:") for m in rec.data["mlx_models"])


def test_mlx_backend_no_recommendation_when_no_mlx_models():
    engine = HealthEngine()
    node = _make_node(available=["qwen3-coder:30b", "gpt-oss:120b"])
    assert engine._check_mlx_backend([node]) == []


def test_mlx_backend_ignores_offline_nodes():
    engine = HealthEngine()
    node = _make_node(status="offline", available=["mlx:foo"])
    assert engine._check_mlx_backend([node]) == []


def test_mlx_backend_handles_nodes_without_ollama_attr():
    engine = HealthEngine()
    # A node with ollama=None shouldn't crash — just gets skipped
    node = SimpleNamespace(
        node_id="n", status=SimpleNamespace(value="online"), ollama=None,
    )
    assert engine._check_mlx_backend([node]) == []


# ---------------------------------------------------------------------------
# _check_mapped_models_hot — CRITICAL + WARNING paths
# ---------------------------------------------------------------------------


def test_mapped_models_hot_no_map_set():
    engine = HealthEngine()
    node = _make_node(loaded=["qwen3-coder:30b"], available=["qwen3-coder:30b"])
    # No env var → no-op
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FLEET_ANTHROPIC_MODEL_MAP", None)
        assert engine._check_mapped_models_hot([node]) == []


def test_mapped_models_hot_all_good():
    engine = HealthEngine()
    node = _make_node(
        loaded=["qwen3-coder:30b"],
        available=["qwen3-coder:30b", "mlx:my-model"],
    )
    map_val = json.dumps({
        "default": "qwen3-coder:30b",
        "claude-sonnet-4-5": "qwen3-coder:30b",
        "claude-opus-4-7": "mlx:my-model",
    })
    with patch.dict(os.environ, {"FLEET_ANTHROPIC_MODEL_MAP": map_val}):
        recs = engine._check_mapped_models_hot([node])
    # qwen3-coder is hot + available; mlx:my-model is available
    # (MLX models don't appear in models_loaded — that's an Ollama-only list)
    assert recs == []


def test_mapped_models_hot_critical_when_missing_entirely():
    engine = HealthEngine()
    node = _make_node(
        loaded=["qwen3-coder:30b"],
        available=["qwen3-coder:30b"],
    )
    map_val = json.dumps({
        "claude-opus-4-7": "nonexistent-model:999b",
    })
    with patch.dict(os.environ, {"FLEET_ANTHROPIC_MODEL_MAP": map_val}):
        recs = engine._check_mapped_models_hot([node])
    assert len(recs) == 1
    assert recs[0].check_id == "mapped_model_missing"
    assert recs[0].severity == Severity.CRITICAL
    assert "nonexistent-model:999b" in recs[0].data["missing"]


def test_mapped_models_hot_warning_when_available_but_not_loaded():
    engine = HealthEngine()
    node = _make_node(
        loaded=["qwen3-coder:30b"],  # hot
        available=["qwen3-coder:30b", "gpt-oss:120b"],  # gpt-oss on disk but NOT loaded
    )
    map_val = json.dumps({
        "claude-opus-4-7": "gpt-oss:120b",
    })
    with patch.dict(os.environ, {"FLEET_ANTHROPIC_MODEL_MAP": map_val}):
        recs = engine._check_mapped_models_hot([node])
    assert len(recs) == 1
    assert recs[0].check_id == "mapped_model_cold"
    assert recs[0].severity == Severity.WARNING
    assert "gpt-oss:120b" in recs[0].data["not_hot"]


def test_mapped_models_hot_mlx_not_on_disk_is_critical():
    """An `mlx:` target that no node advertises → missing (not cold)."""
    engine = HealthEngine()
    node = _make_node(
        loaded=["qwen3-coder:30b"],
        available=["qwen3-coder:30b"],  # no MLX advertised
    )
    map_val = json.dumps({
        "claude-opus-4-7": "mlx:mlx-community/Qwen3-Coder-480B-A35B-4bit",
    })
    with patch.dict(os.environ, {"FLEET_ANTHROPIC_MODEL_MAP": map_val}):
        recs = engine._check_mapped_models_hot([node])
    assert len(recs) == 1
    assert recs[0].check_id == "mapped_model_missing"
    assert recs[0].severity == Severity.CRITICAL


def test_mapped_models_hot_combined_warning_plus_critical():
    """Both a missing model and a cold model → two distinct recommendations."""
    engine = HealthEngine()
    node = _make_node(
        loaded=["qwen3-coder:30b"],
        available=["qwen3-coder:30b", "llama3.3:70b"],
    )
    map_val = json.dumps({
        "claude-sonnet-4-5": "qwen3-coder:30b",    # OK
        "claude-haiku-4-5": "llama3.3:70b",         # cold (available, not loaded)
        "claude-opus-4-7": "missing-model",         # missing entirely
    })
    with patch.dict(os.environ, {"FLEET_ANTHROPIC_MODEL_MAP": map_val}):
        recs = engine._check_mapped_models_hot([node])
    by_id = {r.check_id: r for r in recs}
    assert "mapped_model_missing" in by_id
    assert "mapped_model_cold" in by_id
    assert "missing-model" in by_id["mapped_model_missing"].data["missing"]
    assert "llama3.3:70b" in by_id["mapped_model_cold"].data["not_hot"]


def test_mapped_models_hot_malformed_env_no_crash():
    engine = HealthEngine()
    node = _make_node(loaded=[], available=[])
    with patch.dict(os.environ, {"FLEET_ANTHROPIC_MODEL_MAP": "{not valid json"}):
        # Should return [] rather than raising
        assert engine._check_mapped_models_hot([node]) == []


def test_mapped_models_hot_empty_map_value_no_crash():
    engine = HealthEngine()
    node = _make_node(loaded=[], available=[])
    with patch.dict(os.environ, {"FLEET_ANTHROPIC_MODEL_MAP": "[]"}):
        # List instead of dict → no-op
        assert engine._check_mapped_models_hot([node]) == []


# ---------------------------------------------------------------------------
# Multi-MLX per-server health: memory_blocked + server_down
# ---------------------------------------------------------------------------


def _make_node_with_mlx_servers(*, mlx_servers, **kw):
    """Like _make_node but attaches mlx_servers for the per-server checks."""
    base = _make_node(**kw)
    base.mlx_servers = mlx_servers
    return base


def test_mlx_memory_blocked_emits_warning():
    from fleet_manager.models.node import MlxServerInfo

    engine = HealthEngine()
    node = _make_node_with_mlx_servers(
        available=[],
        mlx_servers=[
            MlxServerInfo(
                port=11441,
                model="big/oversize",
                status="memory_blocked",
                status_reason="memory gate: 1000 GB needed, 32 GB avail",
                model_size_gb=999.0,
            ),
        ],
    )
    recs = engine._check_mlx_backend([node])
    ids = {r.check_id: r for r in recs}
    assert "mlx_memory_blocked" in ids
    rec = ids["mlx_memory_blocked"]
    assert rec.severity == Severity.WARNING
    assert rec.data["port"] == 11441
    assert rec.data["model"] == "big/oversize"
    assert "memory gate" in rec.description


def test_mlx_server_down_emits_error():
    from fleet_manager.models.node import MlxServerInfo

    engine = HealthEngine()
    node = _make_node_with_mlx_servers(
        available=["mlx:a/healthy-main"],  # main is fine
        mlx_servers=[
            MlxServerInfo(port=11440, model="a/healthy-main", status="healthy"),
            MlxServerInfo(
                port=11441, model="a/helper",
                status="unhealthy",
                status_reason="subprocess exited rc=1",
            ),
        ],
    )
    recs = engine._check_mlx_backend([node])
    ids = {r.check_id: [x for x in recs if x.check_id == r.check_id]
           for r in recs}
    assert "mlx_server_down" in ids
    # Should emit exactly one for the unhealthy helper, not for the healthy main
    down_recs = [r for r in recs if r.check_id == "mlx_server_down"]
    assert len(down_recs) == 1
    assert down_recs[0].data["port"] == 11441
    assert down_recs[0].severity == Severity.CRITICAL


def test_mlx_healthy_servers_emit_no_warnings():
    from fleet_manager.models.node import MlxServerInfo

    engine = HealthEngine()
    node = _make_node_with_mlx_servers(
        available=["mlx:a/main", "mlx:a/helper"],
        mlx_servers=[
            MlxServerInfo(port=11440, model="a/main", status="healthy"),
            MlxServerInfo(port=11441, model="a/helper", status="healthy"),
        ],
    )
    recs = engine._check_mlx_backend([node])
    ids = {r.check_id for r in recs}
    assert "mlx_memory_blocked" not in ids
    assert "mlx_server_down" not in ids
    # But mlx_backend_active should still fire (INFO)
    assert "mlx_backend_active" in ids
