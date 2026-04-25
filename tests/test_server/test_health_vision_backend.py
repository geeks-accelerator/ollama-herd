"""Tests for the ``vision_backend_missing`` health check and the
collector-side onnxruntime probe that drives it.

The check fires when a node has cached vision-embedding weights on disk
but the ``onnxruntime`` Python package isn't installed in the herd-node
venv — meaning the embedding server will return HTTP 500 on every
``/embed`` call. Without this signal the dashboard silently stops
showing vision embedding chips and operators have no idea why.

See `docs/observations.md` 2026-04-25 entry for the original failure
mode and `_check_vision_backend_missing` in `health_engine.py`.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from fleet_manager.server.health_engine import HealthEngine, Severity


def _make_node(*, node_id="studio", status="online", vision_status=None):
    """Stub node matching what HealthEngine reads.  ``vision_status`` mirrors
    the heartbeat's ``vision_embedding_status`` dict.  Pass ``None`` to
    simulate an older agent that doesn't send the field."""
    return SimpleNamespace(
        node_id=node_id,
        status=SimpleNamespace(value=status),
        vision_embedding_status=vision_status,
    )


# ---------------------------------------------------------------------------
# _check_vision_backend_missing — health-engine side
# ---------------------------------------------------------------------------


class TestVisionBackendMissingCheck:
    def test_fires_when_weights_cached_but_backend_missing(self):
        engine = HealthEngine()
        node = _make_node(
            vision_status={"backend_available": False, "cached_model_count": 3},
        )
        recs = engine._check_vision_backend_missing([node])
        assert len(recs) == 1
        rec = recs[0]
        assert rec.check_id == "vision_backend_missing"
        assert rec.severity == Severity.WARNING
        assert rec.node_id == "studio"
        assert rec.data["cached_model_count"] == 3
        assert rec.data["backend_available"] is False
        # Operator-actionable fix string mentions the precise command
        assert "uv sync --extra embedding" in rec.fix or "--all-extras" in rec.fix

    def test_silent_when_backend_works(self):
        engine = HealthEngine()
        node = _make_node(
            vision_status={"backend_available": True, "cached_model_count": 3},
        )
        assert engine._check_vision_backend_missing([node]) == []

    def test_silent_when_no_models_cached(self):
        # Operator never wanted vision embedding — don't nag them about
        # an optional dep they have no use for.
        engine = HealthEngine()
        node = _make_node(
            vision_status={"backend_available": False, "cached_model_count": 0},
        )
        assert engine._check_vision_backend_missing([node]) == []

    def test_silent_for_older_agents_without_field(self):
        # Older node agents send no vision_embedding_status field; we must
        # not invent a problem we can't measure.
        engine = HealthEngine()
        node = _make_node(vision_status=None)
        assert engine._check_vision_backend_missing([node]) == []

    def test_silent_for_offline_nodes(self):
        engine = HealthEngine()
        node = _make_node(
            status="offline",
            vision_status={"backend_available": False, "cached_model_count": 5},
        )
        assert engine._check_vision_backend_missing([node]) == []

    def test_handles_multiple_nodes_independently(self):
        engine = HealthEngine()
        good_node = _make_node(
            node_id="good",
            vision_status={"backend_available": True, "cached_model_count": 3},
        )
        bad_node = _make_node(
            node_id="bad",
            vision_status={"backend_available": False, "cached_model_count": 2},
        )
        recs = engine._check_vision_backend_missing([good_node, bad_node])
        assert len(recs) == 1
        assert recs[0].node_id == "bad"


# ---------------------------------------------------------------------------
# Collector-side: _detect_vision_embedding_models() + _vision_backend_status()
# ---------------------------------------------------------------------------


class TestVisionBackendCollectorProbe:
    def test_detect_returns_none_when_onnxruntime_missing(self):
        """When onnxruntime is not importable, the collector must NOT
        advertise any vision embedding models, even if weights are on disk.
        Otherwise the dashboard renders chips for things the backend can't
        actually serve and embedding requests silently 500."""
        from fleet_manager.node.collector import _detect_vision_embedding_models

        # Simulate ImportError by hiding onnxruntime from sys.modules and
        # blocking re-import via __import__ patch.  Even if a real
        # onnxruntime is installed in the test venv, this confirms the
        # fallback path is correct when it ISN'T installed.
        original_import = (
            __builtins__["__import__"] if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "onnxruntime":
                raise ImportError("simulated: no onnxruntime in this venv")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = _detect_vision_embedding_models()
        # No backend → no advertisement, regardless of disk state.
        assert result is None

    def test_status_reports_backend_unavailable(self):
        from fleet_manager.node.collector import _vision_backend_status

        original_import = (
            __builtins__["__import__"] if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "onnxruntime":
                raise ImportError("simulated")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            status = _vision_backend_status()
        assert status["backend_available"] is False
        # cached_model_count is read from disk and should be a non-negative int
        # regardless of backend state — the asymmetry IS the signal.
        assert isinstance(status["cached_model_count"], int)
        assert status["cached_model_count"] >= 0

    def test_status_reports_backend_available_when_present(self):
        from fleet_manager.node.collector import _vision_backend_status

        # Stub onnxruntime so the import succeeds even if the test venv
        # doesn't have it (the dev / non-embedding install path).
        fake_module = SimpleNamespace(__version__="1.99.99-test")
        with patch.dict(sys.modules, {"onnxruntime": fake_module}):
            status = _vision_backend_status()
        assert status["backend_available"] is True
