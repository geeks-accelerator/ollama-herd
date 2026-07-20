"""Shared serialization of registry state for the read APIs.

``/fleet/status`` and the dashboard both need to turn a ``NodeState`` into a
JSON-able dict.  They used to hand-build nearly identical dicts in two places;
this is the single serializer so new fields (``models_loaded_count``,
``free_slots``, …) are added once and both surfaces get them.
"""

from __future__ import annotations

# Fallback hot-model cap, used only when a node doesn't report its own.
#
# This is Ollama's *documented default* ("3 per GPU" when
# OLLAMA_MAX_LOADED_MODELS is unset) — NOT a hard limit.  It was long
# documented here as "macOS hardcodes 3 regardless of OLLAMA_MAX_LOADED_MODELS",
# and `free_slots` was built on that claim.  **Disproven 2026-07-17 on Ollama
# 0.32.1**: with OLLAMA_MAX_LOADED_MODELS=10 we observed 4 concurrent
# residents.  Nodes now report their configured cap in the heartbeat
# (``OllamaMetrics.max_loaded_models``), so prefer ``hot_model_cap_for(node)``
# over this constant — reporting `free_slots` from a fictional limit either
# throttles the fleet or over-promises to clients.
OLLAMA_HOT_MODEL_CAP = 3


# Ollama's documented default for OLLAMA_NUM_PARALLEL.  It was auto-selected
# (1/2/4 by available memory) until ollama PR #11330 removed auto-selection on
# 2025-07-08; since then an unset value means exactly 1.  Deliberately
# conservative: guessing high hands a backend more concurrent work than it will
# admit, and the surplus queues *inside* Ollama where herd can't see it.
OLLAMA_DEFAULT_NUM_PARALLEL = 1


def decode_parallelism_for(node) -> int:
    """How many requests this node's Ollama will actually decode at once.

    Ollama admits ``OLLAMA_NUM_PARALLEL`` requests per model and queues the rest
    internally.  Herd running more workers than that doesn't add throughput — it
    just moves the queue somewhere herd can neither reorder nor reject, which is
    how a slow model turns into a silent pile-up.

    Measured 2026-07-19 on this fleet (glm-4.7-flash, idle, ~1.5K prompt):
    aggregate throughput saturates at N=4 and is flat to N=8, and N=4 is exactly
    the configured ``OLLAMA_NUM_PARALLEL`` — the plateau was the admission limit,
    not the hardware.
    """
    ollama = getattr(node, "ollama", None) if node is not None else None
    reported = getattr(ollama, "num_parallel", 0) or 0
    return reported if reported > 0 else OLLAMA_DEFAULT_NUM_PARALLEL


def hot_model_cap_for(node) -> int:
    """The node's real hot-model cap, or the documented default if unreported.

    Prefers ground truth (the node's own ``OLLAMA_MAX_LOADED_MODELS``) over the
    guess. Older node agents report 0, in which case we fall back.
    """
    ollama = getattr(node, "ollama", None) if node is not None else None
    reported = getattr(ollama, "max_loaded_models", 0) or 0
    return reported if reported > 0 else OLLAMA_HOT_MODEL_CAP


def serialize_node(node) -> dict:
    """Serialize one ``NodeState`` to a JSON-able dict.

    Includes every capability the node reports (ollama / image / transcription
    / embeddings / mlx) plus derived convenience fields (``models_loaded_count``,
    ``free_slots``) that clients use to decide whether to pre-warm or serialize.
    """
    data: dict = {
        "node_id": node.node_id,
        "status": node.status.value,
        "hardware": {
            "memory_total_gb": node.hardware.memory_total_gb,
            "cores_physical": node.hardware.cores_physical,
            "chip": node.hardware.chip,
            "memory_bandwidth_gbps": node.hardware.memory_bandwidth_gbps,
            "arch": node.hardware.arch,
        },
        "ollama_url": node.ollama_base_url,
    }
    if node.cpu:
        data["cpu"] = node.cpu.model_dump()
    if node.memory:
        data["memory"] = node.memory.model_dump()
    cap = hot_model_cap_for(node)
    data["hot_model_cap"] = cap
    if node.ollama:
        data["ollama"] = node.ollama.model_dump()
        loaded = len(node.ollama.models_loaded)
        data["models_loaded_count"] = loaded
        data["free_slots"] = max(0, cap - loaded)
    else:
        data["models_loaded_count"] = 0
        data["free_slots"] = cap
    if node.image:
        data["image"] = node.image.model_dump()
        data["image_port"] = node.image_port
    if node.transcription:
        data["transcription"] = node.transcription.model_dump()
        data["transcription_port"] = node.transcription_port
    if node.vision_embedding:
        data["vision_embedding"] = node.vision_embedding.model_dump()
        data["vision_embedding_port"] = node.vision_embedding_port
    # Always expose backend status (even when no models cached) so operators
    # can tell "never installed" from "installed but silently broken".
    if node.vision_embedding_status:
        data["vision_embedding_status"] = dict(node.vision_embedding_status)
    if node.text_embedding:
        data["text_embedding"] = node.text_embedding.model_dump()
        data["text_embedding_port"] = node.text_embedding_port
    if node.text_embedding_status:
        data["text_embedding_status"] = dict(node.text_embedding_status)
    if node.mlx_servers:
        data["mlx_servers"] = [s.model_dump() for s in node.mlx_servers]
        data["mlx_bind_host"] = node.mlx_bind_host
    return data
