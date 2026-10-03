"""Shared serialization of registry state for the read APIs.

``/fleet/status`` and the dashboard both need to turn a ``NodeState`` into a
JSON-able dict.  They used to hand-build nearly identical dicts in two places;
this is the single serializer so new fields (``models_loaded_count``,
``free_slots``, …) are added once and both surfaces get them.
"""

from __future__ import annotations

from fleet_manager.models.request import normalize_model_name
from fleet_manager.server.mlx_proxy import is_mlx_model, strip_mlx_prefix

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


# Architectures Ollama refuses to run with num_parallel > 1, whatever
# OLLAMA_NUM_PARALLEL says — ollama server/sched.go load(): "Some architectures
# are not safe with num_parallel > 1" (it logs a warning herd never sees).
# Read from ollama main 2026-10-02.  Re-check on every Ollama upgrade, like the
# two constants above: a family missing here means herd over-dispatches to it.
OLLAMA_SERIAL_FAMILIES = frozenset(
    {
        "mllama",
        "qwen3vl",
        "qwen3vlmoe",
        "qwen35",
        "qwen35moe",
        "qwen3next",
        "lfm2",
        "lfm2moe",
        "nemotron_h",
        "nemotron_h_moe",
        "nemotron_h_omni",
    }
)


def decode_parallelism_for(node, model: str | None = None) -> int:
    """How many requests this node's Ollama will actually decode at once.

    Ollama admits ``OLLAMA_NUM_PARALLEL`` requests per model and queues the rest
    internally.  Herd running more workers than that doesn't add throughput — it
    just moves the queue somewhere herd can neither reorder nor reject, which is
    how a slow model turns into a silent pile-up.

    Measured 2026-07-19 on this fleet (glm-4.7-flash, idle, ~1.5K prompt):
    aggregate throughput saturates at N=4 and is flat to N=8, and N=4 is exactly
    the configured ``OLLAMA_NUM_PARALLEL`` — the plateau was the admission limit,
    not the hardware.

    Ollama decides this per *model*, so pass ``model`` when there is one.  It
    serves one request at a time — regardless of ``OLLAMA_NUM_PARALLEL`` — for:

    * MLX-run models.  Ollama's ``IsMLX()`` is exactly ``format ==
      "safetensors"``, and its MLX runner (``mlxrunner/runner.go``) is a single
      loop that runs each request to completion before taking the next.
      Ollama 0.40 makes MLX the default on Apple Silicon.
    * ``OLLAMA_SERIAL_FAMILIES`` — forced to 1 in ``sched.go``.
    * Non-completion models (embedders) — ``sched.go`` again.

    Each check uses what the node's Ollama *reports* in ``/api/tags``.  A
    model with no metadata (older agent, ``mlx:`` model) gets the node-level
    value exactly as before, so missing data can't throttle anything.
    """
    ollama = getattr(node, "ollama", None) if node is not None else None
    reported = getattr(ollama, "num_parallel", 0) or 0
    node_limit = reported if reported > 0 else OLLAMA_DEFAULT_NUM_PARALLEL
    if model is None:
        return node_limit
    meta = _model_meta(node, model)
    if meta is None:
        return node_limit
    if (
        meta.format == "safetensors"
        or meta.family in OLLAMA_SERIAL_FAMILIES
        # sched.go's ``!completion`` rule, in the only form that is safe to read
        # from /api/tags: a positively reported embedder that doesn't also
        # report completion.  NOT "decision" — decision models report
        # completion too (nimble: decision, tools, thinking, completion), so
        # sched.go doesn't force them; nimble is serial via its qwen35 family.
        # And never a bare missing "completion": see model_has_capability.
        or ("embedding" in meta.capabilities and "completion" not in meta.capabilities)
    ):
        return 1
    return node_limit


def _model_meta(node, model: str):
    """The node's Ollama ``/api/tags`` metadata for ``model``, or None.

    Keyed the way Ollama keys it (``name:tag``), so the bare names clients send
    are normalized with the same helper ``InferenceRequest`` uses.
    """
    ollama = getattr(node, "ollama", None) if node is not None else None
    meta = getattr(ollama, "models_available_meta", None) or {}
    return meta.get(normalize_model_name(model)) if model else None


def model_has_capability(node, model: str, capability: str) -> bool:
    """True only when the node's Ollama *reports* ``capability`` for ``model``.

    Presence-only, deliberately.  Ollama 0.33.x under-reports capabilities in
    ``/api/tags`` (fixed in 0.34.1), so a missing entry proves nothing — False
    here means "unknown", and every caller falls back to what it did before
    this existed.  That makes the helper safe on any Ollama version without
    parsing one.
    """
    meta = _model_meta(node, model)
    return meta is not None and capability in meta.capabilities


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
        # models_available_meta exists only to answer /api/tags; leaving it out
        # keeps /fleet/status (polled by the dashboard) from growing ~250 bytes
        # per model per node for data nothing here reads.
        data["ollama"] = node.ollama.model_dump(exclude={"models_available_meta"})
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


def model_resident_on_node(model: str, node) -> bool:
    """True if ``node`` currently has ``model`` resident and serving.

    Covers both backends: Ollama (``models_loaded``) and MLX (``mlx_servers``
    entry with a ``healthy`` status -- MLX names carry the ``mlx:`` prefix,
    which the server list stores stripped).

    Lives here rather than in ``model_preloader`` because three callers now need
    it and one of them is the scorer: residency is what separates "serve what is
    already in memory" from "load something new", which is the distinction the
    scorer's memory-pressure handling turns on.  ``serializers`` is the leaf
    module the others can all import without a cycle.
    """
    if is_mlx_model(model):
        target = strip_mlx_prefix(model)
        return any(
            s.model == target and s.status == "healthy"
            for s in (getattr(node, "mlx_servers", None) or [])
        )
    ollama = getattr(node, "ollama", None)
    return bool(ollama and model in [m.name for m in ollama.models_loaded])
