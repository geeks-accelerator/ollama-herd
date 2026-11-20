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


def test_fleet_status_reports_the_agent_version():
    """Benchmark manifests record which herd-node each machine ran."""
    from fleet_manager.server.serializers import serialize_node
    from tests.conftest import make_node

    node = make_node("n1")
    node.agent_version = "0.12.10"
    assert serialize_node(node)["agent_version"] == "0.12.10"
    node.agent_version = ""
    assert serialize_node(node)["agent_version"] is None


@pytest.mark.parametrize(
    "name", ["qwen2.5vl:7b", "qwen2.5vl:72b", "qwen3-vl:8b", "qwen3-vl:235b", "qwen2.5-vl:3b"]
)
def test_the_qwen_vl_family_is_vision(name):
    assert model_type(name) is ModelType.VISION


@pytest.mark.parametrize(
    "name",
    [
        "llama3.1:8b",
        "llama3.2:3b",
        "llama3.3:70b",
        "qwen3:14b",
        "qwen2.5:7b",
        "qwen2.5-coder:7b",
        "qwen3-coder:30b",
        "qwq:32b",
        "deepseek-r1:14b",
        "gpt-oss:20b",
        "mistral:7b",
        "mistral-nemo:12b",
        "mixtral:8x7b",
        "phi4:14b",
        "gemma2:9b",
        "devstral:24b",
        "codestral:22b",
        "command-r:35b",
        "glm-4.7-flash",
        "granite3.3:8b",
        "olmo2:13b",
        "smollm2:1.7b",
        "dolphin3:8b",
        "starcoder2:15b",
        "nemotron:70b",
        "tinyllama",
        "solar:10.7b",
        "yi:34b",
        "openchat:7b",
        "vicuna:13b",
        "wizardlm2:7b",
    ],
)
def test_no_common_chat_model_is_taken_for_vision(name):
    assert model_type(name) is ModelType.LLM
