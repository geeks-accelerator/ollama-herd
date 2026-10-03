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
        # fastembed serves the -Q file from the base repo, and the HF cache is
        # keyed by repo — so the cache check must look here, not at the name.
        "hf_repo": "nomic-ai/nomic-embed-text-v1.5",
        "dimensions": 768,
        # What Ollama's own nomic-embed-text declares (GGUF
        # nomic-bert.context_length), and what the server truncates to.  NOT
        # the model card's 8192: attention memory is quadratic in it, and ONNX
        # Runtime keeps its peak — see _ATTENTION_BUDGET in the server.
        "max_tokens": 2048,
        "size_mb": 130,
        "description": (
            "nomic-embed-text-v1.5 int8-quantized (130 MB) — "
            "high-quality 768-dim embeddings, 2K token context. "
            "Replaces Ollama nomic-embed-text with a native ONNX backend "
            "that runs independently of LLM inference slots."
        ),
    },
    "nomic-embed-text:latest": {
        # Ollama tag alias — same model, same cache entry
        "fastembed_name": "nomic-ai/nomic-embed-text-v1.5-Q",
        # fastembed serves the -Q file from the base repo, and the HF cache is
        # keyed by repo — so the cache check must look here, not at the name.
        "hf_repo": "nomic-ai/nomic-embed-text-v1.5",
        "dimensions": 768,
        "max_tokens": 2048,
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

# Cross-encoder rerankers, served by the same fastembed server (``/rerank``).
# Same spec shape as TEXT_EMBEDDING_MODELS, so the lookups below resolve both.
# Deliberately a SIBLING dict: ``is_text_embedding_model`` and
# ``TEXT_EMBEDDING_MODEL_NAMES`` stay embed-only, so the /api/embed dispatcher
# can never send an embed request to a reranker.  Keys are lowercase because
# every lookup lowercases; ``fastembed_name`` keeps fastembed's own casing.
# These are the rerankers fastembed 0.8 supports (TextCrossEncoder).
RERANK_MODELS: dict[str, dict] = {
    "ms-marco-minilm-l-6-v2": {
        "fastembed_name": "Xenova/ms-marco-MiniLM-L-6-v2",
        "size_mb": 80,
        "description": "Smallest and fastest; English. The default.",
    },
    "ms-marco-minilm-l-12-v2": {
        "fastembed_name": "Xenova/ms-marco-MiniLM-L-12-v2",
        "size_mb": 120,
        "description": "Deeper MiniLM; English.",
    },
    "jina-reranker-v1-tiny-en": {
        "fastembed_name": "jinaai/jina-reranker-v1-tiny-en",
        "size_mb": 130,
        "description": "Jina tiny; English, long inputs.",
    },
    "jina-reranker-v1-turbo-en": {
        "fastembed_name": "jinaai/jina-reranker-v1-turbo-en",
        "size_mb": 150,
        "description": "Jina turbo; English, long inputs.",
    },
    "bge-reranker-base": {
        "fastembed_name": "BAAI/bge-reranker-base",
        "size_mb": 1040,
        "description": "BAAI BGE; strong English/Chinese quality.",
    },
    "jina-reranker-v2-base-multilingual": {
        "fastembed_name": "jinaai/jina-reranker-v2-base-multilingual",
        "size_mb": 1110,
        "description": "Jina v2; multilingual quality pick.",
    },
}
DEFAULT_RERANK_MODEL = "ms-marco-minilm-l-6-v2"
RERANK_MODEL_NAMES: set[str] = set(RERANK_MODELS)


def _spec(model: str) -> dict:
    """The registry spec for an embedding *or* rerank model; KeyError if neither."""
    key = model.lower().strip()
    if key in TEXT_EMBEDDING_MODELS:
        return TEXT_EMBEDDING_MODELS[key]
    return RERANK_MODELS[key]


def is_text_embedding_model(model: str) -> bool:
    """Return True if ``model`` should be routed to the native text embedding backend."""
    return model.lower().strip() in TEXT_EMBEDDING_MODEL_NAMES


def is_rerank_model(model: str) -> bool:
    """Return True if ``model`` is a reranker the native server can load."""
    return model.lower().strip() in RERANK_MODEL_NAMES


def get_fastembed_name(model: str) -> str:
    """Resolve an Ollama model name to its fastembed model identifier.

    Raises ``KeyError`` if the model is not in the registry.
    """
    return _spec(model)["fastembed_name"]


def get_model_spec(model: str) -> dict:
    """Return the full spec dict for an Ollama model name.

    Raises ``KeyError`` if the model is not in the registry.
    """
    return _spec(model)


def is_model_cached(model: str) -> bool:
    """Check whether a model's weights are present on disk without loading fastembed.

    fastembed stores model files under ``<cache_dir>/<org>/<name>/``.  We check
    for a non-empty subdirectory matching the fastembed_name to avoid importing
    fastembed on every heartbeat tick (which would slow startup and penalise nodes
    that haven't installed the extra).
    """
    try:
        spec = _spec(model)
    except KeyError:
        return False
    # fastembed stores models in the HF cache layout, keyed by the *source repo*:
    # <cache_dir>/models--<org>--<repo>/.  That is usually the fastembed name,
    # but not always — nomic-embed-text-v1.5-Q lives in models--nomic-ai--
    # nomic-embed-text-v1.5.  Keying on the name made this always False for
    # nomic, so the heartbeat said cached=False and the backend-missing health
    # check could never fire.  ``hf_repo`` records the exceptions.
    repo = spec.get("hf_repo") or spec["fastembed_name"]
    sanitised = repo.replace("/", "--")
    model_dir = TEXT_EMBEDDING_CACHE_DIR / f"models--{sanitised}"
    if not model_dir.exists():
        return False
    # Must contain at least one file (not just an empty dir)
    return any(model_dir.rglob("*"))


def canonical_model_names() -> list[str]:
    """Return canonical model names (no :latest aliases) for collector reporting."""
    return sorted(_CANONICAL_NAMES)


def canonical_rerank_names() -> list[str]:
    """Return all registered reranker names."""
    return sorted(RERANK_MODEL_NAMES)
