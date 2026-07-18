"""Tests for best-loaded Anthropic routing (server/anthropic_autoroute.py).

Claude Code sends claude-* ids; with auto-routing on, an unmapped id resolves
to the best currently-loaded local model for its tier, so no hand-written map
is required.  Explicit map entries remain available as per-alias overrides.
"""

from __future__ import annotations

from fleet_manager.server.anthropic_autoroute import (
    TIER_BALANCED,
    TIER_FAST,
    TIER_PREMIUM,
    _tier_of,
    rank_candidates,
    resolve_model,
)

# ---------------------------------------------------------------------------
# Tier inference
# ---------------------------------------------------------------------------


def test_tier_inference():
    assert _tier_of("claude-haiku-4-5") == TIER_FAST
    assert _tier_of("claude-opus-4-7") == TIER_PREMIUM
    assert _tier_of("claude-sonnet-4-5") == TIER_BALANCED
    # Unknown / future claude ids fall to the safe middle, never crash.
    assert _tier_of("claude-something-new") == TIER_BALANCED
    assert _tier_of("") == TIER_BALANCED


# ---------------------------------------------------------------------------
# Candidate ranking + filtering
# ---------------------------------------------------------------------------


def test_embedding_models_are_never_candidates():
    """The catalog has no text-embedding category, so nomic-embed-text
    classifies as GENERAL — it must be excluded by name or a chat request
    would route into an embedding model and 500."""
    ranked = rank_candidates(
        ["nomic-embed-text", "qwen3-coder:30b", "bge-large", "all-minilm"],
        "claude-sonnet-4-5",
    )
    assert ranked == ["qwen3-coder:30b"]


def test_coding_model_beats_reasoning_for_coding_tier():
    """A coder should outrank a reasoning model for Claude Code's workload,
    even though the reasoning model has a high raw quality score."""
    ranked = rank_candidates(
        ["gpt-oss:120b", "qwen3-coder:30b"], "claude-sonnet-4-5"
    )
    assert ranked[0] == "qwen3-coder:30b"


def test_ranking_dedupes_and_is_stable():
    a = rank_candidates(
        ["qwen3-coder:30b", "qwen3-coder:30b", "qwen3:14b"], "claude-sonnet-4-5"
    )
    b = rank_candidates(
        ["qwen3:14b", "qwen3-coder:30b", "qwen3-coder:30b"], "claude-sonnet-4-5"
    )
    assert a == b  # order of inputs doesn't change the result
    assert a.count("qwen3-coder:30b") == 1  # deduped


def test_vision_request_keeps_only_vision_models():
    ranked = rank_candidates(
        ["qwen3-coder:30b", "gemma3:27b", "gpt-oss:120b"],
        "claude-sonnet-4-5",
        want_vision=True,
    )
    assert ranked == ["gemma3:27b"]  # only the vision model survives


def test_gemma4_is_a_vision_candidate():
    """Gemma 4 (added to the catalog for Ollama 0.30.3+) is multimodal, so it
    must be pickable for image requests — the whole point of cataloguing it."""
    from fleet_manager.server.model_knowledge import is_vision_model

    assert is_vision_model("gemma4:12b")
    ranked = rank_candidates(
        ["qwen3-coder:30b", "gemma4:12b", "gpt-oss:120b"],
        "claude-sonnet-4-5",
        want_vision=True,
    )
    assert ranked == ["gemma4:12b"]


def test_empty_and_unusable_inputs():
    assert rank_candidates([], "claude-sonnet-4-5") == []
    assert rank_candidates([None, ""], "claude-sonnet-4-5") == []
    # An all-embedding fleet has no chat candidates.
    assert rank_candidates(["nomic-embed-text"], "claude-sonnet-4-5") == []


def test_mlx_prefixed_models_are_eligible():
    ranked = rank_candidates(
        ["mlx:lmstudio-community/GLM-4.7-Flash-MLX-4bit"], "claude-sonnet-4-5"
    )
    assert ranked == ["mlx:lmstudio-community/GLM-4.7-Flash-MLX-4bit"]


# ---------------------------------------------------------------------------
# Full resolution precedence
# ---------------------------------------------------------------------------


LOADED = ["qwen3-coder:30b", "gpt-oss:120b", "nomic-embed-text"]
ONDISK = LOADED + ["gemma3:27b", "qwen3:32b"]


def test_explicit_map_entry_wins_over_auto():
    model, reason = resolve_model(
        "claude-sonnet-4-5",
        {"claude-sonnet-4-5": "gpt-oss:120b"},
        LOADED, ONDISK, auto_route=True,
    )
    assert model == "gpt-oss:120b"
    assert reason == "explicit-map"


def test_real_local_name_passes_through():
    model, reason = resolve_model(
        "qwen3-coder:30b", {}, LOADED, ONDISK, auto_route=True
    )
    assert model == "qwen3-coder:30b"
    assert reason == "passthrough"


def test_auto_prefers_loaded_over_ondisk():
    # qwen3-coder is loaded; gemma is only on disk. Coder wins (loaded + coding).
    model, reason = resolve_model(
        "claude-sonnet-4-5", {}, LOADED, ONDISK, auto_route=True
    )
    assert model == "qwen3-coder:30b"
    assert reason == "auto-loaded"


def test_auto_falls_back_to_ondisk_when_nothing_loaded():
    model, reason = resolve_model(
        "claude-sonnet-4-5", {}, [], ONDISK, auto_route=True
    )
    assert model == "qwen3-coder:30b"
    assert reason == "auto-ondisk"


def test_default_key_is_last_resort_under_auto():
    # Nothing chat-capable anywhere, but a default is configured.
    model, reason = resolve_model(
        "claude-sonnet-4-5",
        {"default": "some-model:7b"},
        ["nomic-embed-text"], ["nomic-embed-text"], auto_route=True,
    )
    assert model == "some-model:7b"
    assert reason == "default"


def test_auto_off_ignores_loaded_models():
    """With auto off, a claude-* id with no map entry and no default is
    unresolved — the pre-0.9 strict behaviour."""
    model, reason = resolve_model(
        "claude-sonnet-4-5", {}, LOADED, ONDISK, auto_route=False
    )
    assert model is None
    assert reason == "unresolved"


def test_auto_off_still_honours_explicit_and_default():
    assert resolve_model(
        "claude-sonnet-4-5", {"claude-sonnet-4-5": "x:7b"},
        LOADED, ONDISK, auto_route=False,
    ) == ("x:7b", "explicit-map")
    assert resolve_model(
        "claude-opus-4-7", {"default": "d:7b"},
        LOADED, ONDISK, auto_route=False,
    ) == ("d:7b", "default")


def test_unresolvable_returns_none():
    model, reason = resolve_model(
        "claude-sonnet-4-5", {}, [], [], auto_route=True
    )
    assert model is None
    assert reason == "unresolved"


def test_unknown_claude_id_still_resolves_under_auto():
    """A future claude-* id we've never seen still routes (balanced tier)."""
    model, reason = resolve_model(
        "claude-brandnew-9", {}, LOADED, ONDISK, auto_route=True
    )
    assert model == "qwen3-coder:30b"
    assert reason == "auto-loaded"


def test_vision_request_routes_to_loaded_vision_model():
    model, reason = resolve_model(
        "claude-sonnet-4-5", {}, ONDISK, ONDISK,
        auto_route=True, has_images=True,
    )
    assert model == "gemma3:27b"
    assert reason == "auto-loaded"
