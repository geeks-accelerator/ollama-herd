"""The one model-type classifier every caller shares."""

from __future__ import annotations

import pytest

from fleet_manager.server.model_knowledge import (
    ModelType,
    is_embedding_model,
    model_type,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("qwen3-coder:30b", ModelType.LLM),
        ("gpt-oss:120b", ModelType.LLM),
        ("nomic-embed-text", ModelType.EMBED),
        ("nomic-embed-text:latest", ModelType.EMBED),
        ("qwen3-embedding:8b", ModelType.EMBED),
        ("bge-m3", ModelType.EMBED),
        ("snowflake-arctic-embed:l", ModelType.EMBED),
        ("all-minilm", ModelType.EMBED),
        ("dinov2-vit-s14", ModelType.EMBED_IMAGE),
        ("clip", ModelType.EMBED_IMAGE),
        ("moondream:1.8b", ModelType.VISION),
        ("llava:13b", ModelType.VISION),
        ("whisper-large-v3", ModelType.STT),
        ("qwen3-asr", ModelType.STT),
    ],
)
def test_model_type_by_name(name, expected):
    assert model_type(name) is expected


def test_reported_capabilities_widen_detection():
    """Ollama's own capabilities catch what no name pattern does."""
    assert model_type("qwen3.8:4b") is ModelType.LLM
    assert model_type("qwen3.8:4b", ["completion", "vision"]) is ModelType.VISION
    assert model_type("mystery:1b", ["embedding"]) is ModelType.EMBED


def test_capabilities_never_overrule_the_name():
    """Older Ollama under-reports, so a missing capability proves nothing."""
    assert is_embedding_model("nomic-embed-text", ["completion"])
    assert model_type("dinov2-vit-s14", ["completion"]) is ModelType.EMBED_IMAGE
