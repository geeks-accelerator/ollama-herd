"""Tests for vision model support — classification, token estimation, and routing."""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import InferenceRequest, RequestFormat
from fleet_manager.server.model_knowledge import (
    ModelCategory,
    classify_model,
    is_vision_model,
    lookup_model,
)
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from tests.conftest import make_heartbeat


@pytest.fixture
def settings():
    return ServerSettings()


@pytest.fixture
def registry(settings):
    return NodeRegistry(settings)


@pytest.fixture
def scorer(settings, registry):
    return ScoringEngine(settings, registry)


# ---------------------------------------------------------------------------
# Model classification
# ---------------------------------------------------------------------------


class TestVisionClassification:
    def test_vision_category_exists(self):
        assert ModelCategory.VISION == "vision"

    def test_gemma3_27b_is_vision(self):
        spec = lookup_model("gemma3:27b")
        assert spec is not None
        assert spec.category == ModelCategory.VISION

    def test_gemma3_27b_also_general(self):
        spec = lookup_model("gemma3:27b")
        assert ModelCategory.GENERAL in spec.secondary_categories

    def test_llama32_vision_in_catalog(self):
        spec = lookup_model("llama3.2-vision:11b")
        assert spec is not None
        assert spec.category == ModelCategory.VISION
        assert spec.ram_gb == 8.0

    def test_llava_in_catalog(self):
        spec = lookup_model("llava:7b")
        assert spec is not None
        assert spec.category == ModelCategory.VISION

    def test_moondream_in_catalog(self):
        spec = lookup_model("moondream:1.8b")
        assert spec is not None
        assert spec.category == ModelCategory.VISION
        assert spec.ram_gb == 2.0

    def test_minicpm_v_in_catalog(self):
        spec = lookup_model("minicpm-v:8b")
        assert spec is not None
        assert spec.category == ModelCategory.VISION

    def test_is_vision_model_known(self):
        assert is_vision_model("gemma3:27b") is True
        assert is_vision_model("llama3.2-vision:11b") is True
        assert is_vision_model("llava:7b") is True
        assert is_vision_model("moondream:1.8b") is True
        assert is_vision_model("minicpm-v:8b") is True

    def test_is_vision_model_heuristic(self):
        """Unknown vision models detected by name heuristic."""
        assert is_vision_model("some-vision-model:latest") is True
        assert is_vision_model("custom-llava:13b") is True

    def test_is_vision_model_negative(self):
        assert is_vision_model("llama3.3:70b") is False
        assert is_vision_model("qwen2.5-coder:32b") is False

    def test_classify_model_vision_heuristic(self):
        assert classify_model("unknown-vision-model") == ModelCategory.VISION
        assert classify_model("my-llava-finetune") == ModelCategory.VISION
        assert classify_model("moondream-custom") == ModelCategory.VISION

    def test_classify_model_non_vision(self):
        assert classify_model("llama3.3:70b") != ModelCategory.VISION


# ---------------------------------------------------------------------------
# Image token estimation
# ---------------------------------------------------------------------------


class TestImageTokenEstimation:
    def test_openai_format_image_tokens(self):
        """OpenAI multimodal format adds image tokens."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
                ],
            }
        ]
        tokens = ScoringEngine.estimate_tokens(messages)
        # Text: "Describe this image" = ~5 tokens + overhead
        # Image: 150 tokens
        assert tokens >= 150

    def test_openai_format_multiple_images(self):
        """Multiple images in one message."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Compare these"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,img1"}},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,img2"}},
                ],
            }
        ]
        tokens = ScoringEngine.estimate_tokens(messages)
        assert tokens >= 300  # 2 images * 150

    def test_ollama_format_image_tokens(self):
        """Ollama format with images field."""
        messages = [
            {
                "role": "user",
                "content": "Describe this image",
                "images": ["base64encodeddata"],
            }
        ]
        tokens = ScoringEngine.estimate_tokens(messages)
        assert tokens >= 150

    def test_ollama_format_multiple_images(self):
        messages = [
            {
                "role": "user",
                "content": "Compare",
                "images": ["img1base64", "img2base64", "img3base64"],
            }
        ]
        tokens = ScoringEngine.estimate_tokens(messages)
        assert tokens >= 450  # 3 images * 150

    def test_text_only_no_image_tokens(self):
        """Text-only messages should not include image tokens."""
        messages = [{"role": "user", "content": "Hello world"}]
        tokens = ScoringEngine.estimate_tokens(messages)
        assert tokens < 150  # Well below one image's worth

    def test_mixed_messages_count_all_images(self):
        """Images across multiple messages are all counted."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,a"}},
                ],
            },
            {"role": "assistant", "content": "I see a photo."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Second image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,b"}},
                ],
            },
        ]
        tokens = ScoringEngine.estimate_tokens(messages)
        assert tokens >= 300  # 2 images


# ---------------------------------------------------------------------------
# has_images auto-detection on InferenceRequest
# ---------------------------------------------------------------------------


class TestHasImagesDetection:
    def test_openai_format_detected(self):
        req = InferenceRequest(
            model="gemma3:27b",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                    ],
                }
            ],
            original_format=RequestFormat.OPENAI,
        )
        assert req.has_images is True

    def test_ollama_format_detected(self):
        req = InferenceRequest(
            model="gemma3:27b",
            messages=[
                {
                    "role": "user",
                    "content": "Describe this",
                    "images": ["base64data"],
                }
            ],
            original_format=RequestFormat.OLLAMA,
        )
        assert req.has_images is True

    def test_text_only_not_detected(self):
        req = InferenceRequest(
            model="llama3.3:70b",
            messages=[{"role": "user", "content": "Hello"}],
        )
        assert req.has_images is False

    def test_empty_messages(self):
        req = InferenceRequest(model="gemma3:27b", messages=[])
        assert req.has_images is False


# ---------------------------------------------------------------------------
# OpenAI → Ollama image format conversion
# ---------------------------------------------------------------------------


class TestImageFormatConversion:
    def test_openai_image_url_converted(self):
        """OpenAI image_url parts are converted to Ollama images field."""
        from fleet_manager.server.streaming import StreamingProxy

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc123def"},
                    },
                ],
            }
        ]
        converted = StreamingProxy._convert_messages_for_ollama(messages)
        assert converted[0]["content"] == "Describe this"
        assert converted[0]["images"] == ["abc123def"]

    def test_multiple_images_converted(self):
        from fleet_manager.server.streaming import StreamingProxy

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Compare"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,img1"},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,img2"},
                    },
                ],
            }
        ]
        converted = StreamingProxy._convert_messages_for_ollama(messages)
        assert converted[0]["images"] == ["img1", "img2"]

    def test_text_only_passthrough(self):
        """Messages without images pass through unchanged."""
        from fleet_manager.server.streaming import StreamingProxy

        messages = [{"role": "user", "content": "Hello"}]
        converted = StreamingProxy._convert_messages_for_ollama(messages)
        assert converted == messages

    def test_preserves_role_and_other_fields(self):
        from fleet_manager.server.streaming import StreamingProxy

        messages = [
            {
                "role": "user",
                "name": "test_user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,xyz"},
                    },
                ],
            }
        ]
        converted = StreamingProxy._convert_messages_for_ollama(messages)
        assert converted[0]["role"] == "user"
        assert converted[0]["name"] == "test_user"
        assert converted[0]["content"] == "What is this?"
        assert converted[0]["images"] == ["xyz"]


# ---------------------------------------------------------------------------
# Vision model routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVisionRouting:
    async def test_vision_model_routes_to_loaded_node(self, scorer, registry):
        """Vision model request routes to node with vision model hot."""
        hb = make_heartbeat(
            node_id="studio",
            memory_total=192.0,
            memory_used=40.0,
            loaded_models=[("gemma3:27b", 19.0)],
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_request("gemma3:27b", {})
        assert len(results) == 1
        assert results[0].node_id == "studio"
        assert results[0].scores_breakdown["thermal"] == 50.0  # hot

    async def test_vision_model_prefers_hot_node(self, scorer, registry):
        """Vision request prefers node with model already loaded."""
        hb_hot = make_heartbeat(
            node_id="hot-node",
            memory_total=64.0,
            memory_used=30.0,
            loaded_models=[("llama3.2-vision:11b", 8.0)],
        )
        hb_cold = make_heartbeat(
            node_id="cold-node",
            memory_total=128.0,
            memory_used=20.0,
            available_models=["llama3.2-vision:11b"],
        )
        await registry.update_from_heartbeat(hb_hot)
        await registry.update_from_heartbeat(hb_cold)

        results = scorer.score_request("llama3.2-vision:11b", {})
        assert results[0].node_id == "hot-node"

    async def test_image_tokens_affect_context_fit(self, scorer, registry):
        """Vision requests with images use higher token estimates for context scoring."""
        # Node with small context
        hb = make_heartbeat(
            node_id="small-ctx",
            memory_total=64.0,
            memory_used=20.0,
            loaded_models=[("gemma3:27b", 19.0, 4096)],
        )
        await registry.update_from_heartbeat(hb)

        # Text-only request — small token count
        text_messages = [{"role": "user", "content": "Hello"}]
        text_tokens = ScoringEngine.estimate_tokens(text_messages)

        # Vision request — includes image tokens
        vision_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                ],
            }
        ]
        vision_tokens = ScoringEngine.estimate_tokens(vision_messages)

        assert vision_tokens > text_tokens
        assert vision_tokens >= ScoringEngine.IMAGE_TOKENS_PER_IMAGE
