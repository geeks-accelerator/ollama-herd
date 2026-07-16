"""Shared serialization of registry state for the read APIs.

``/fleet/status`` and the dashboard both need to turn a ``NodeState`` into a
JSON-able dict.  They used to hand-build nearly identical dicts in two places;
this is the single serializer so new fields (``models_loaded_count``,
``free_slots``, …) are added once and both surfaces get them.
"""

from __future__ import annotations

# macOS Ollama hot-load cap is hardcoded at 3 regardless of
# OLLAMA_MAX_LOADED_MODELS (see docs/issues.md / CLAUDE.md).  Used to compute
# ``free_slots`` so a client can tell whether a new model will cold-load
# (evicting another) vs. slot into free capacity.
OLLAMA_HOT_MODEL_CAP = 3


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
    if node.ollama:
        data["ollama"] = node.ollama.model_dump()
        loaded = len(node.ollama.models_loaded)
        data["models_loaded_count"] = loaded
        data["free_slots"] = max(0, OLLAMA_HOT_MODEL_CAP - loaded)
    else:
        data["models_loaded_count"] = 0
        data["free_slots"] = OLLAMA_HOT_MODEL_CAP
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
