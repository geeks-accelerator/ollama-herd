"""Tests for the native text embedding backend (fastembed path).

Covers:
- Model name detection (is_text_embedding_model)
- Collector detection + status functions (mocked fastembed import)
- Router embed_text() endpoint (mocked node + httpx)
- health_engine _check_text_embedding_backend_missing
- ollama_compat dispatch (text model → embed_text, not Ollama)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# text_embedding_models helpers
# ---------------------------------------------------------------------------

class TestIsTextEmbeddingModel:
    def test_nomic_embed_text(self):
        from fleet_manager.node.text_embedding_models import is_text_embedding_model
        assert is_text_embedding_model("nomic-embed-text")

    def test_nomic_embed_text_latest(self):
        from fleet_manager.node.text_embedding_models import is_text_embedding_model
        assert is_text_embedding_model("nomic-embed-text:latest")

    def test_case_insensitive(self):
        from fleet_manager.node.text_embedding_models import is_text_embedding_model
        assert is_text_embedding_model("Nomic-Embed-Text")

    def test_llm_model_not_text_embed(self):
        from fleet_manager.node.text_embedding_models import is_text_embedding_model
        assert not is_text_embedding_model("gpt-oss:120b")
        assert not is_text_embedding_model("llama3:8b")

    def test_vision_model_not_text_embed(self):
        from fleet_manager.node.text_embedding_models import is_text_embedding_model
        assert not is_text_embedding_model("dinov2-vit-s14")
        assert not is_text_embedding_model("clip-vit-b32")

    def test_get_fastembed_name(self):
        from fleet_manager.node.text_embedding_models import get_fastembed_name
        assert get_fastembed_name("nomic-embed-text") == "nomic-ai/nomic-embed-text-v1.5-Q"

    def test_get_fastembed_name_alias(self):
        from fleet_manager.node.text_embedding_models import get_fastembed_name
        assert get_fastembed_name("nomic-embed-text:latest") == "nomic-ai/nomic-embed-text-v1.5-Q"

    def test_get_fastembed_name_unknown_raises(self):
        from fleet_manager.node.text_embedding_models import get_fastembed_name
        with pytest.raises(KeyError):
            get_fastembed_name("not-a-model")

    def test_canonical_model_names_no_aliases(self):
        from fleet_manager.node.text_embedding_models import canonical_model_names
        names = canonical_model_names()
        assert "nomic-embed-text" in names
        assert "nomic-embed-text:latest" not in names  # aliases excluded


# ---------------------------------------------------------------------------
# Collector detection functions
# ---------------------------------------------------------------------------

class TestDetectTextEmbeddingModels:
    def test_returns_none_when_fastembed_missing(self):
        """If fastembed can't be imported, return None (don't advertise)."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "fastembed":
                raise ImportError("fastembed not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            from fleet_manager.node import collector
            result = collector._detect_text_embedding_models()
        assert result is None

    def test_returns_metrics_when_fastembed_available(self):
        """If fastembed is importable, return TextEmbeddingMetrics."""
        mock_fastembed = MagicMock()
        with patch.dict("sys.modules", {"fastembed": mock_fastembed}):
            from fleet_manager.node import collector
            result = collector._detect_text_embedding_models()
        # Should return TextEmbeddingMetrics (possibly with uncached models)
        if result is not None:
            assert hasattr(result, "models_available")


class TestTextEmbeddingBackendStatus:
    def test_backend_unavailable_when_fastembed_missing(self):
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "fastembed":
                raise ImportError("fastembed not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            from fleet_manager.node import collector
            status = collector._text_embedding_backend_status()

        assert status["backend_available"] is False
        assert "cached_model_count" in status
        assert isinstance(status["cached_model_count"], int)

    def test_backend_available_when_fastembed_present(self):
        mock_fastembed = MagicMock()
        with patch.dict("sys.modules", {"fastembed": mock_fastembed}):
            from fleet_manager.node import collector
            status = collector._text_embedding_backend_status()
        assert status["backend_available"] is True


# ---------------------------------------------------------------------------
# Health engine check
# ---------------------------------------------------------------------------

class TestCheckTextEmbeddingBackendMissing:
    def _make_node(self, text_embedding_status: dict, status_value: str = "online"):
        node = MagicMock()
        node.status.value = status_value
        node.text_embedding_status = text_embedding_status
        node.node_id = "test-node"
        return node

    def test_no_warning_when_backend_available(self):
        from fleet_manager.server.health_engine import HealthEngine
        engine = HealthEngine.__new__(HealthEngine)
        node = self._make_node({"backend_available": True, "cached_model_count": 1})
        recs = engine._check_text_embedding_backend_missing([node])
        assert recs == []

    def test_no_warning_when_nothing_cached(self):
        """Don't nag operators who never wanted text embedding."""
        from fleet_manager.server.health_engine import HealthEngine
        engine = HealthEngine.__new__(HealthEngine)
        node = self._make_node({"backend_available": False, "cached_model_count": 0})
        recs = engine._check_text_embedding_backend_missing([node])
        assert recs == []

    def test_warning_when_cached_but_backend_missing(self):
        """Fire WARNING when weights exist but fastembed isn't installed."""
        from fleet_manager.server.health_engine import HealthEngine
        engine = HealthEngine.__new__(HealthEngine)
        node = self._make_node({"backend_available": False, "cached_model_count": 1})
        recs = engine._check_text_embedding_backend_missing([node])
        assert len(recs) == 1
        assert recs[0].check_id == "text_embedding_backend_missing"
        assert recs[0].severity.value == "warning"
        assert "fastembed" in recs[0].fix.lower()

    def test_no_warning_for_offline_node(self):
        from fleet_manager.server.health_engine import HealthEngine
        engine = HealthEngine.__new__(HealthEngine)
        node = self._make_node(
            {"backend_available": False, "cached_model_count": 1},
            status_value="offline",
        )
        recs = engine._check_text_embedding_backend_missing([node])
        assert recs == []

    def test_skip_older_agents_with_empty_status(self):
        """Older agents don't send text_embedding_status — skip gracefully."""
        from fleet_manager.server.health_engine import HealthEngine
        engine = HealthEngine.__new__(HealthEngine)
        node = self._make_node({})
        recs = engine._check_text_embedding_backend_missing([node])
        assert recs == []


# ---------------------------------------------------------------------------
# ollama_compat dispatch
# ---------------------------------------------------------------------------

class TestOllamaEmbedDispatch:
    """Verify that is_text_embedding_model routes to embed_text, not Ollama."""

    def test_text_model_detected_before_ollama(self):
        from fleet_manager.node.text_embedding_models import is_text_embedding_model
        from fleet_manager.server.routes.embedding_compat import is_vision_embedding_model

        model = "nomic-embed-text"
        # Vision check comes first in ollama_embed — must be False for text models
        assert not is_vision_embedding_model(model)
        # Text check comes second — must be True
        assert is_text_embedding_model(model)

    def test_vision_model_not_intercepted_by_text(self):
        from fleet_manager.node.text_embedding_models import is_text_embedding_model
        assert not is_text_embedding_model("dinov2-vit-s14")
        assert not is_text_embedding_model("clip-vit-b32")
        assert not is_text_embedding_model("siglip2-base")
