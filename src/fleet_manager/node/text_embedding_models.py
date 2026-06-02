"""Text embedding model registry for the native fastembed backend.

This module defines which Ollama-style text embedding model names map to
fastembed model identifiers, and provides helpers for cache detection and
model resolution.  The fastembed backend runs entirely outside Ollama —
no OLLAMA_NUM_PARALLEL slot is consumed, so embed requests can never be
starved by concurrent LLM inference.

Cache location: ~/.fleet-manager/models/text-embedding/<fastembed_name>/
  Consistent with the vision embedding cache at ~/.fleet-manager/models/.
  Overridable via FASTEMBED_CACHE_PATH or the cache_dir constructor arg.

Adding a new model: add an entry to TEXT_EMBEDDING_MODELS with the
  fastembed_name, dimensions, max_tokens, and description.  No code changes
  needed — the server, collector, and health engine all read from this dict.
  To add an Ollama-tag alias, add a second entry pointing to the same
  fastembed_name.
"""

from __future__ import annotations

from pathlib import Path

# Cache directory — mirrors ~/.fleet-manager/models/ used by vision embedding.
TEXT_EMBEDDING_CACHE_DIR = Path.home() / ".fleet-manager" / "models" / "text-embedding"

# Registry: Ollama model name → fastembed spec.
# Keys are the model names clients send in POST /api/embed {"model": "..."}.
# Add :latest aliases for Ollama-tag compat (they share the fastembed entry).
TEXT_EMBEDDING_MODELS: dict[str, dict] = {
    "nomic-embed-text": {
        "fastembed_name": "nomic-ai/nomic-embed-text-v1.5-Q",
        "dimensions": 768,
        "max_tokens": 8192,
        "size_mb": 130,
        "description": (
            "nomic-embed-text-v1.5 int8-quantized (130 MB) — "
            "high-quality 768-dim embeddings, 8K token context. "
            "Replaces Ollama nomic-embed-text with a native ONNX backend "
            "that runs independently of LLM inference slots."
        ),
    },
    "nomic-embed-text:latest": {
        # Ollama tag alias — same model, same cache entry
        "fastembed_name": "nomic-ai/nomic-embed-text-v1.5-Q",
        "dimensions": 768,
        "max_tokens": 8192,
        "size_mb": 130,
        "description": "Alias for nomic-embed-text (Ollama :latest tag).",
    },
}

# Fast set lookup used by the dispatcher in ollama_compat.py
TEXT_EMBEDDING_MODEL_NAMES: set[str] = set(TEXT_EMBEDDING_MODELS.keys())

# Canonical names (no aliases) — used by collector to count distinct models
_CANONICAL_NAMES: set[str] = {
    name for name in TEXT_EMBEDDING_MODELS if not name.endswith(":latest")
}


def is_text_embedding_model(model: str) -> bool:
    """Return True if ``model`` should be routed to the native text embedding backend."""
    return model.lower().strip() in TEXT_EMBEDDING_MODEL_NAMES


def get_fastembed_name(model: str) -> str:
    """Resolve an Ollama model name to its fastembed model identifier.

    Raises ``KeyError`` if the model is not in the registry.
    """
    spec = TEXT_EMBEDDING_MODELS[model.lower().strip()]
    return spec["fastembed_name"]


def get_model_spec(model: str) -> dict:
    """Return the full spec dict for an Ollama model name.

    Raises ``KeyError`` if the model is not in the registry.
    """
    return TEXT_EMBEDDING_MODELS[model.lower().strip()]


def is_model_cached(model: str) -> bool:
    """Check whether a model's weights are present on disk without loading fastembed.

    fastembed stores model files under ``<cache_dir>/<org>/<name>/``.  We check
    for a non-empty subdirectory matching the fastembed_name to avoid importing
    fastembed on every heartbeat tick (which would slow startup and penalise nodes
    that haven't installed the extra).
    """
    spec = TEXT_EMBEDDING_MODELS.get(model.lower().strip())
    if not spec:
        return False
    fastembed_name = spec["fastembed_name"]
    # fastembed stores models as <cache_dir>/models--<org>--<name>/
    # e.g. models--nomic-ai--nomic-embed-text-v1.5-Q
    sanitised = fastembed_name.replace("/", "--")
    model_dir = TEXT_EMBEDDING_CACHE_DIR / f"models--{sanitised}"
    if not model_dir.exists():
        return False
    # Must contain at least one file (not just an empty dir)
    return any(model_dir.rglob("*"))


def canonical_model_names() -> list[str]:
    """Return canonical model names (no :latest aliases) for collector reporting."""
    return sorted(_CANONICAL_NAMES)
