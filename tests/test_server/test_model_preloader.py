"""Tests for the Model Preloader — pinned + cap-aware behavior.

Regression guard against the 2026-04-23 incident where the preloader
blindly loaded 10+ priority models, trashing Ollama's 3-model hot cap
and evicting gpt-oss:120b every router restart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_manager.server.model_preloader import (
    _estimate_model_size,
    _model_is_loaded_anywhere,
    _nodes_with_model_on_disk,
    _parse_pinned_models,
)

# ----------------------------------------------------------------------------
# Pure-function helpers
# ----------------------------------------------------------------------------


def test_parse_pinned_models_empty():
    assert _parse_pinned_models("") == []
    assert _parse_pinned_models(None) == []
    assert _parse_pinned_models("   ") == []


def test_parse_pinned_models_single():
    assert _parse_pinned_models("gpt-oss:120b") == ["gpt-oss:120b"]


def test_parse_pinned_models_multiple_with_whitespace():
    assert _parse_pinned_models("gpt-oss:120b, gemma3:27b , nomic-embed-text") == [
        "gpt-oss:120b", "gemma3:27b", "nomic-embed-text",
    ]


def test_parse_pinned_models_drops_empties():
    assert _parse_pinned_models("a,,b,") == ["a", "b"]


def _node(name: str, loaded_models: list[str], disk_models: list[str], mem_gb: float = 100.0):
    """Build a mock node for the loaded/disk checks."""
    n = MagicMock()
    n.node_id = name
    n.ollama = MagicMock()
    n.ollama.models_loaded = [MagicMock(name=m) for m in loaded_models]
    # MagicMock() sets `.name` via constructor but accessing as attribute
    # needs explicit assignment for our helper
    for m, name_ in zip(n.ollama.models_loaded, loaded_models, strict=False):
        m.name = name_
    n.ollama.models_available = list(disk_models)
    n.memory = MagicMock()
    n.memory.available_gb = mem_gb
    return n


def test_model_is_loaded_anywhere_true():
    nodes = [
        _node("A", loaded_models=["foo:1b"], disk_models=["foo:1b"]),
        _node("B", loaded_models=[], disk_models=[]),
    ]
    assert _model_is_loaded_anywhere("foo:1b", nodes) is True


def test_model_is_loaded_anywhere_false():
    nodes = [_node("A", loaded_models=["other:1b"], disk_models=["foo:1b"])]
    assert _model_is_loaded_anywhere("foo:1b", nodes) is False


def test_nodes_with_model_on_disk_filters_correctly():
    nodes = [
        _node("A", loaded_models=[], disk_models=["foo:1b", "bar:7b"]),
        _node("B", loaded_models=[], disk_models=["bar:7b"]),
        _node("C", loaded_models=[], disk_models=[]),
    ]
    matches = _nodes_with_model_on_disk("foo:1b", nodes)
    assert len(matches) == 1
    assert matches[0].node_id == "A"


def test_estimate_model_size_embedding_models_are_small():
    assert _estimate_model_size("nomic-embed-text") <= 1.0
    assert _estimate_model_size("text-embedding-small") <= 1.0


def test_estimate_model_size_large_models():
    # A 120B model should estimate substantial RAM regardless of exact lookup
    size = _estimate_model_size("gpt-oss:120b")
    assert size >= 50.0


# ----------------------------------------------------------------------------
# Integration — _load_model_on_best_node picks best + respects memory
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_model_on_best_node_picks_most_free_memory():
    from fleet_manager.server.model_preloader import _load_model_on_best_node

    nodes = [
        _node("A", loaded_models=[], disk_models=["foo:7b"], mem_gb=10.0),
        _node("B", loaded_models=[], disk_models=["foo:7b"], mem_gb=100.0),
    ]
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock(return_value=None)
    ok = await _load_model_on_best_node("foo:7b", nodes, proxy, why="test")
    assert ok is True
    # Must have called pre_warm on B (more free memory)
    proxy.pre_warm.assert_called_once_with("B", "foo:7b")


@pytest.mark.asyncio
async def test_load_model_on_best_node_skips_when_memory_tight():
    """Must refuse when memory < 1.2× model size on any node."""
    from fleet_manager.server.model_preloader import _load_model_on_best_node

    # Tiny memory on the one available node
    nodes = [_node("A", loaded_models=[], disk_models=["gpt-oss:120b"], mem_gb=50.0)]
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock(return_value=None)
    ok = await _load_model_on_best_node("gpt-oss:120b", nodes, proxy, why="test")
    # 50GB free < 1.2 × ~75GB model → should skip
    assert ok is False
    proxy.pre_warm.assert_not_called()


@pytest.mark.asyncio
async def test_load_model_on_best_node_skips_when_not_on_disk():
    from fleet_manager.server.model_preloader import _load_model_on_best_node

    nodes = [_node("A", loaded_models=[], disk_models=["other:1b"], mem_gb=100.0)]
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock(return_value=None)
    ok = await _load_model_on_best_node("foo:7b", nodes, proxy, why="test")
    assert ok is False
    proxy.pre_warm.assert_not_called()


@pytest.mark.asyncio
async def test_load_model_on_best_node_handles_pre_warm_exception():
    from fleet_manager.server.model_preloader import _load_model_on_best_node

    nodes = [_node("A", loaded_models=[], disk_models=["foo:1b"], mem_gb=100.0)]
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock(side_effect=RuntimeError("boom"))
    ok = await _load_model_on_best_node("foo:1b", nodes, proxy, why="test")
    # Exception → returns False (fail-open) — doesn't propagate
    assert ok is False


# ----------------------------------------------------------------------------
# preload_priority_models — pinned models load first, cap respected
# ----------------------------------------------------------------------------


def _mock_settings(pinned: str = "", max_count: int = 3, disabled: bool = False):
    s = MagicMock()
    s.pinned_models = pinned
    s.model_preload_max_count = max_count
    s.disable_model_preloader = disabled
    return s


def _mock_registry(nodes: list):
    r = MagicMock()
    r.get_online_nodes = MagicMock(return_value=nodes)
    return r


@pytest.mark.asyncio
async def test_preloader_disabled_is_noop():
    from fleet_manager.server.model_preloader import preload_priority_models

    nodes = [_node("A", [], ["foo:1b"])]
    registry = _mock_registry(nodes)
    trace = MagicMock()
    trace.get_model_priority_scores = AsyncMock(return_value=[])
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock()

    settings = _mock_settings(disabled=True)
    await preload_priority_models(registry, trace, proxy, settings)
    proxy.pre_warm.assert_not_called()


@pytest.mark.asyncio
async def test_preloader_pinned_first_then_priority_up_to_cap(monkeypatch):
    """Pinned models load first; remaining slots filled by priority list
    UP TO max_count.  Never exceeds max_count — the regression-guard
    against the 2026-04-23 eviction incident."""
    from fleet_manager.server import model_preloader

    # One node with many models on disk, lots of memory (so memory never
    # becomes the limiting factor — we want to assert on the count cap).
    nodes = [_node(
        "studio",
        loaded_models=[],
        disk_models=[
            "gpt-oss:120b", "gemma3:27b", "qwen3-coder:30b",
            "qwen3:8b", "gemma3:4b", "nomic-embed-text",
        ],
        mem_gb=500.0,
    )]
    registry = _mock_registry(nodes)

    # Priority scores: 6 candidate models, all eligible (score >= 1.0)
    priorities = [
        {"model": "qwen3-coder:30b", "priority_score": 100},
        {"model": "qwen3:8b", "priority_score": 80},
        {"model": "gemma3:4b", "priority_score": 60},
        {"model": "nomic-embed-text", "priority_score": 40},
        {"model": "gpt-oss:120b", "priority_score": 20},
        {"model": "gemma3:27b", "priority_score": 10},
    ]
    trace = MagicMock()
    trace.get_model_priority_scores = AsyncMock(return_value=priorities)

    proxy = MagicMock()
    proxy.pre_warm = AsyncMock()

    # Skip the long startup waits
    async def fast_sleep(_s):
        pass
    monkeypatch.setattr("fleet_manager.server.model_preloader.asyncio.sleep", fast_sleep)
    # Reset the module-level priority cache so our mock returns get used
    model_preloader._priority_cache = []
    model_preloader._priority_cache_time = 0

    # Inject a hard stop on the while-True refresh loop
    calls = {"refresh": 0}
    async def _stop_refresh(*args, **kwargs):
        calls["refresh"] += 1
        raise asyncio.CancelledError
    import asyncio
    monkeypatch.setattr(
        "fleet_manager.server.model_preloader._refresh_priority_models",
        _stop_refresh,
    )

    settings = _mock_settings(pinned="gpt-oss:120b,gemma3:27b", max_count=3)

    # The coroutine goes into the infinite refresh loop; cancel after startup
    import contextlib
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(
            preload_and_cancel(registry, trace, proxy, settings),
            timeout=2.0,
        )

    # Inspect what pre_warm was called with — the CRITICAL assertions:
    calls_list = [c.args[1] for c in proxy.pre_warm.call_args_list]
    # Pinned models loaded first (in configured order)
    assert calls_list[0] == "gpt-oss:120b"
    assert calls_list[1] == "gemma3:27b"
    # Total calls ≤ max_count (3) — NEVER exceeds
    assert len(calls_list) <= 3
    # The 3rd slot (if used) goes to top-priority non-pinned model
    # qwen3-coder:30b (score 100) — NOT qwen3:8b (80) or others
    if len(calls_list) == 3:
        assert calls_list[2] == "qwen3-coder:30b"


async def preload_and_cancel(registry, trace, proxy, settings):
    """Helper: run preloader, swallow the test-injected cancellation."""
    import asyncio as _asyncio
    import contextlib
    from fleet_manager.server.model_preloader import preload_priority_models
    with contextlib.suppress(_asyncio.CancelledError):
        await preload_priority_models(registry, trace, proxy, settings)


@pytest.mark.asyncio
async def test_preloader_skips_pinned_if_already_hot(monkeypatch):
    """If a pinned model is already loaded, don't re-load it (but do
    count it against the slot budget)."""
    from fleet_manager.server import model_preloader

    # gpt-oss:120b is ALREADY hot on startup
    nodes = [_node(
        "studio", loaded_models=["gpt-oss:120b"],
        disk_models=["gpt-oss:120b", "gemma3:27b", "qwen3-coder:30b"],
        mem_gb=500.0,
    )]
    registry = _mock_registry(nodes)
    trace = MagicMock()
    trace.get_model_priority_scores = AsyncMock(return_value=[
        {"model": "qwen3-coder:30b", "priority_score": 100},
    ])
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock()

    async def fast_sleep(_s): pass
    monkeypatch.setattr("fleet_manager.server.model_preloader.asyncio.sleep", fast_sleep)
    model_preloader._priority_cache = []
    model_preloader._priority_cache_time = 0
    import asyncio
    async def _stop(*a, **k): raise asyncio.CancelledError
    monkeypatch.setattr(
        "fleet_manager.server.model_preloader._refresh_priority_models", _stop,
    )

    settings = _mock_settings(pinned="gpt-oss:120b,gemma3:27b", max_count=3)
    import contextlib
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            preload_and_cancel(registry, trace, proxy, settings), timeout=2.0,
        )

    models_loaded = [c.args[1] for c in proxy.pre_warm.call_args_list]
    # gpt-oss was already hot, should NOT have been pre-warmed again
    assert "gpt-oss:120b" not in models_loaded
    # gemma3:27b was not hot, should have been loaded
    assert "gemma3:27b" in models_loaded
    # qwen3-coder:30b gets the remaining slot (3 total: gpt-oss hot + gemma3 + qwen3-coder)
    assert len(models_loaded) <= 2  # Only 2 actually loaded (gpt-oss was already hot)


def contextlib_suppress():
    """pytest-compatible suppressor for cancellation noise."""
    import contextlib
    return contextlib.suppress(Exception)
