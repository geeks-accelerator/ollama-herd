"""Tests for vision embedding service — model detection, routing, and proxy."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from fleet_manager.models.node import (
    VisionEmbeddingMetrics,
    VisionEmbeddingModel,
)


# ---------------------------------------------------------------------------
# Model registry & detection
# ---------------------------------------------------------------------------


class TestVisionEmbeddingModels:
    def test_model_registry_has_all_models(self):
        from fleet_manager.node.embedding_models import VISION_EMBEDDING_MODELS

        assert "dinov2-vit-s14" in VISION_EMBEDDING_MODELS
        assert "siglip2-base" in VISION_EMBEDDING_MODELS
        assert "clip-vit-b32" in VISION_EMBEDDING_MODELS

    def test_dinov2_spec(self):
        from fleet_manager.node.embedding_models import VISION_EMBEDDING_MODELS

        spec = VISION_EMBEDDING_MODELS["dinov2-vit-s14"]
        assert spec["runtime"] == "onnx"
        assert spec["dimensions"] == 384
        assert spec["size_mb"] == 85

    def test_clip_spec(self):
        from fleet_manager.node.embedding_models import VISION_EMBEDDING_MODELS

        spec = VISION_EMBEDDING_MODELS["clip-vit-b32"]
        assert spec["runtime"] == "onnx"
        assert spec["dimensions"] == 512

    def test_siglip2_spec(self):
        from fleet_manager.node.embedding_models import VISION_EMBEDDING_MODELS

        spec = VISION_EMBEDDING_MODELS["siglip2-base"]
        assert spec["runtime"] == "onnx"
        assert spec["dimensions"] == 768

    def test_select_default_model_nothing_downloaded(self):
        """Default to DINOv2 when nothing is downloaded."""
        from fleet_manager.node.embedding_models import select_default_model

        with patch(
            "fleet_manager.node.embedding_models.is_model_downloaded",
            return_value=False,
        ):
            assert select_default_model() == "dinov2-vit-s14"

    def test_select_default_model_prefers_downloaded(self):
        """Prefers whatever model is already downloaded."""
        from fleet_manager.node.embedding_models import select_default_model

        def fake_downloaded(name):
            return name == "siglip2-base"

        with patch(
            "fleet_manager.node.embedding_models.is_model_downloaded",
            side_effect=fake_downloaded,
        ):
            assert select_default_model() == "siglip2-base"

    def test_model_dir_path(self):
        from fleet_manager.node.embedding_models import get_model_dir

        path = get_model_dir("dinov2-vit-s14")
        assert str(path).endswith("models/dinov2-vit-s14")


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------


class TestImagePreprocessing:
    def test_preprocess_output_shape(self):
        from PIL import Image

        from fleet_manager.node.embedding_models import preprocess_image

        img = Image.new("RGB", (640, 480), color=(255, 0, 0))
        result = preprocess_image(img, input_size=224)
        assert result.shape == (1, 3, 224, 224)
        assert result.dtype == np.float32

    def test_preprocess_dinov2_size(self):
        from PIL import Image

        from fleet_manager.node.embedding_models import preprocess_image

        img = Image.new("RGB", (1024, 768), color=(0, 128, 255))
        result = preprocess_image(img, input_size=518)
        assert result.shape == (1, 3, 518, 518)

    def test_preprocess_grayscale_converted(self):
        """Grayscale images are converted to RGB."""
        from PIL import Image

        from fleet_manager.node.embedding_models import preprocess_image

        img = Image.new("L", (100, 100), color=128)
        result = preprocess_image(img, input_size=224)
        assert result.shape == (1, 3, 224, 224)

    def test_preprocess_rgba_converted(self):
        """RGBA images are converted to RGB."""
        from PIL import Image

        from fleet_manager.node.embedding_models import preprocess_image

        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        result = preprocess_image(img, input_size=224)
        assert result.shape == (1, 3, 224, 224)


# ---------------------------------------------------------------------------
# ONNX backend
# ---------------------------------------------------------------------------


class TestONNXBackend:
    def test_onnx_embed_returns_correct_shape(self):
        """ONNX backend returns (N, 512) normalized embeddings."""
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            pytest.skip("onnxruntime not installed")

        from fleet_manager.node.embedding_models import ONNXBackend

        # Mock the ONNX session
        mock_session = MagicMock()
        # Return fake 512-dim embeddings
        fake_output = np.random.randn(2, 512).astype(np.float32)
        mock_session.run.return_value = [fake_output]
        mock_session.get_inputs.return_value = [MagicMock(name="pixel_values")]

        from pathlib import Path

        fake_dir = Path("/fake/model")
        with patch.object(Path, "exists", return_value=True):
            with patch("onnxruntime.InferenceSession", return_value=mock_session):
                with patch(
                    "onnxruntime.get_available_providers",
                    return_value=["CPUExecutionProvider"],
                ):
                    backend = ONNXBackend(fake_dir, "clip-vit-b32")

        from PIL import Image

        images = [Image.new("RGB", (64, 64)), Image.new("RGB", (64, 64))]
        result = backend.embed(images)

        assert result.shape == (2, 512)
        # Verify L2 normalized (each row should have unit norm)
        norms = np.linalg.norm(result, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-5)

    def _backend_with_input_shape(self, shape, per_call_output, model="dinov2-vit-s14"):
        """Build an ONNXBackend over a mock session with a given input shape."""
        from pathlib import Path

        from fleet_manager.node.embedding_models import ONNXBackend

        mock_input = MagicMock()
        mock_input.name = "input"
        mock_input.shape = shape

        mock_session = MagicMock()
        mock_session.get_inputs.return_value = [mock_input]
        mock_session.run.return_value = [per_call_output]

        with patch.object(Path, "exists", return_value=True):
            with patch("onnxruntime.InferenceSession", return_value=mock_session):
                with patch(
                    "onnxruntime.get_available_providers",
                    return_value=["CPUExecutionProvider"],
                ):
                    backend = ONNXBackend(Path("/fake/model"), model)
        return backend, mock_session

    def test_fixed_batch_model_runs_one_image_per_call(self):
        """A [1, 3, 224, 224] input axis must not be fed a concatenated batch.

        dinov2-vit-s14's ONNX export bakes the batch axis to 1.  Feeding it
        three stacked images failed the whole request with INVALID_ARGUMENT
        ("Got: 3 Expected: 1"), so batch requests 500'd while single-image
        ones worked.
        """
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            pytest.skip("onnxruntime not installed")

        from PIL import Image

        backend, session = self._backend_with_input_shape(
            [1, 3, 224, 224], np.random.randn(1, 384).astype(np.float32)
        )
        assert backend._fixed_batch is True

        images = [Image.new("RGB", (64, 64)) for _ in range(3)]
        result = backend.embed(images)

        # One session call per image, each fed exactly one image.
        assert session.run.call_count == 3
        for call in session.run.call_args_list:
            assert call[0][1]["input"].shape == (1, 3, 224, 224)

        assert result.shape == (3, 384)
        np.testing.assert_allclose(np.linalg.norm(result, axis=1), 1.0, atol=1e-5)

    def test_dynamic_batch_model_still_batches_in_one_call(self):
        """A dynamic batch axis keeps the single-call fast path."""
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            pytest.skip("onnxruntime not installed")

        from PIL import Image

        backend, session = self._backend_with_input_shape(
            ["batch", 3, 224, 224],
            np.random.randn(3, 512).astype(np.float32),
            model="clip-vit-b32",
        )
        assert backend._fixed_batch is False

        result = backend.embed([Image.new("RGB", (64, 64)) for _ in range(3)])

        assert session.run.call_count == 1
        assert session.run.call_args[0][1]["input"].shape == (3, 3, 224, 224)
        assert result.shape == (3, 512)


# ---------------------------------------------------------------------------
# Model classification
# ---------------------------------------------------------------------------


class TestVisionEmbeddingCategory:
    def test_vision_embedding_category_exists(self):
        from fleet_manager.server.model_knowledge import ModelCategory

        assert hasattr(ModelCategory, "VISION_EMBEDDING")

    def test_dinov2_in_catalog(self):
        from fleet_manager.server.model_knowledge import lookup_model

        spec = lookup_model("dinov2-vit-s14")
        assert spec is not None
        assert spec.category.value == "vision-embedding"

    def test_clip_in_catalog(self):
        from fleet_manager.server.model_knowledge import lookup_model

        spec = lookup_model("clip-vit-b32")
        assert spec is not None
        assert spec.category.value == "vision-embedding"

    def test_siglip2_in_catalog(self):
        from fleet_manager.server.model_knowledge import lookup_model

        spec = lookup_model("siglip2-base")
        assert spec is not None
        assert spec.category.value == "vision-embedding"


# ---------------------------------------------------------------------------
# Routing — model name detection
# ---------------------------------------------------------------------------


class TestVisionEmbeddingRouting:
    def test_is_vision_embedding_model_canonical(self):
        from fleet_manager.server.routes.embedding_compat import is_vision_embedding_model

        assert is_vision_embedding_model("dinov2-vit-s14")
        assert is_vision_embedding_model("clip-vit-b32")
        assert is_vision_embedding_model("siglip2-base")

    def test_is_vision_embedding_model_aliases(self):
        from fleet_manager.server.routes.embedding_compat import is_vision_embedding_model

        assert is_vision_embedding_model("clip")
        assert is_vision_embedding_model("dinov2")
        assert is_vision_embedding_model("siglip")

    def test_is_not_vision_embedding_model(self):
        from fleet_manager.server.routes.embedding_compat import is_vision_embedding_model

        assert not is_vision_embedding_model("nomic-embed-text")
        assert not is_vision_embedding_model("llama3.2:3b")
        assert not is_vision_embedding_model("gpt-oss:120b")

    def test_resolve_aliases(self):
        from fleet_manager.server.routes.embedding_compat import _resolve_model_name

        assert _resolve_model_name("clip") == "clip-vit-b32"
        assert _resolve_model_name("dinov2") == "dinov2-vit-s14"
        assert _resolve_model_name("siglip") == "siglip2-base"
        assert _resolve_model_name("siglip2") == "siglip2-base"
        # Canonical names pass through
        assert _resolve_model_name("dinov2-vit-s14") == "dinov2-vit-s14"


# ---------------------------------------------------------------------------
# Heartbeat & node state
# ---------------------------------------------------------------------------


class TestVisionEmbeddingHeartbeat:
    def test_heartbeat_payload_has_fields(self):
        from fleet_manager.models.node import HeartbeatPayload

        fields = HeartbeatPayload.model_fields
        assert "vision_embedding" in fields
        assert "vision_embedding_port" in fields

    def test_node_state_has_fields(self):
        from fleet_manager.models.node import NodeState

        fields = NodeState.model_fields
        assert "vision_embedding" in fields
        assert "vision_embedding_port" in fields

    def test_vision_embedding_metrics_serialization(self):
        metrics = VisionEmbeddingMetrics(
            models_available=[
                VisionEmbeddingModel(
                    name="dinov2-vit-s14", runtime="mlx", dimensions=384
                )
            ],
            processing=False,
        )
        data = metrics.model_dump()
        assert len(data["models_available"]) == 1
        assert data["models_available"][0]["name"] == "dinov2-vit-s14"
        assert data["models_available"][0]["dimensions"] == 384


# ---------------------------------------------------------------------------
# Embedding server
# ---------------------------------------------------------------------------


class TestEmbeddingServer:
    def test_server_has_embed_endpoint(self):
        from fleet_manager.node.embedding_server import router

        paths = [r.path for r in router.routes]
        assert "/embed" in paths

    def test_server_has_models_endpoint(self):
        from fleet_manager.node.embedding_server import router

        paths = [r.path for r in router.routes]
        assert "/models" in paths


# ---------------------------------------------------------------------------
# Streaming proxy
# ---------------------------------------------------------------------------


class TestEmbeddingProxy:
    def test_streaming_proxy_has_embedding_methods(self):
        """StreamingProxy should have embedding process fn and on-node method."""
        from fleet_manager.server.streaming import StreamingProxy

        assert hasattr(StreamingProxy, "make_embedding_process_fn")
        assert hasattr(StreamingProxy, "embed_image_on_node")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestEmbeddingConfig:
    def test_config_has_vision_embedding_fields(self):
        from fleet_manager.models.config import ServerSettings

        settings = ServerSettings()
        assert settings.vision_embedding is True
        assert settings.vision_embedding_timeout == 30.0
