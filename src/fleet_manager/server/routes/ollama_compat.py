"""Ollama-compatible API endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from fleet_manager.models.node import ModelTagMeta, NodeStatus
from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server.fleet_headers import affinity_from_breakdown, fleet_headers
from fleet_manager.server.model_knowledge import is_image_model
from fleet_manager.server.queue_manager import ClientConcurrencyExceeded
from fleet_manager.server.routes.routing import (
    _pick_pull_node,
    _pulls_in_flight,
    check_context_overflow,
    client_concurrency_response,
    extract_tags,
    get_all_fleet_models,
    parse_allow_fallback,
    record_routing_rejection,
    score_with_fallbacks,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ollama"])


def _build_thinking_headers(proxy, request_id: str) -> dict[str, str]:
    """Build X-Thinking-* response headers from streaming metadata.

    Returns headers for thinking token breakdown, budget usage, and done reason.
    Only includes headers when there's meaningful data (thinking tokens > 0 or
    done_reason is present).
    """
    meta = proxy.pop_request_meta(request_id)
    if not meta:
        return {}
    headers = {}
    if meta.get("thinking_tokens", 0) > 0:
        headers["X-Thinking-Tokens"] = str(meta["thinking_tokens"])
    if meta.get("output_tokens", 0) > 0:
        headers["X-Output-Tokens"] = str(meta["output_tokens"])
    if meta.get("done_reason"):
        headers["X-Done-Reason"] = meta["done_reason"]
    # Budget: completion_tokens / num_predict
    completion = meta.get("completion_tokens")
    num_predict = meta.get("num_predict")
    if completion is not None and num_predict:
        headers["X-Budget-Used"] = f"{completion}/{num_predict}"
    return headers


@router.post("/api/chat")
async def ollama_chat(request: Request):
    """Ollama-compatible chat endpoint. Routes to best available node."""
    body = await request.json()
    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        fallback_models=body.get("fallback_models", []),
        messages=body.get("messages", []),
        stream=body.get("stream", True),
        temperature=body.get("options", {}).get("temperature", 0.7),
        max_tokens=body.get("options", {}).get("num_predict"),
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
        tags=tags,
    )

    return await _route_and_stream(request, inference_req)


@router.post("/api/generate")
async def ollama_generate(request: Request):
    """Ollama-compatible generate endpoint."""
    body = await request.json()
    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    prompt = body.get("prompt", "")
    messages = [{"role": "user", "content": prompt}] if prompt else []

    # Detect Ollama native image generation models
    image_model = is_image_model(model)

    # Prefer mflux over Ollama native for image generation.
    # mflux runs as a separate subprocess and doesn't evict LLMs from Ollama's VRAM.
    if image_model:
        registry = request.app.state.registry
        # Map Ollama native model names to their mflux equivalents
        _OLLAMA_TO_MFLUX = {
            "x/z-image-turbo": "z-image-turbo",
            "x/z-image-turbo:latest": "z-image-turbo",
        }
        mflux_model = _OLLAMA_TO_MFLUX.get(model)
        if mflux_model:
            # Check if any node has this model via mflux (image server on port 11436)
            mflux_available = any(
                n.image
                and n.image_port > 0
                and any(m.name == mflux_model for m in n.image.models_available)
                for n in registry.get_online_nodes()
            )
            if mflux_available:
                logger.info(
                    f"Preferring mflux '{mflux_model}' over Ollama native '{model}' "
                    f"(avoids LLM eviction from VRAM)"
                )
                # Redirect to the image endpoint with the mflux model name
                from fleet_manager.server.routes.image_compat import generate_image

                image_body = {
                    "model": mflux_model,
                    "prompt": prompt,
                }
                # Forward image-specific params
                for key in ("width", "height", "steps", "guidance", "seed",
                            "negative_prompt", "quantize"):
                    val = body.get(key)
                    if val is not None:
                        image_body[key] = val
                request._body = __import__("json").dumps(image_body).encode()
                return await generate_image(request)

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        fallback_models=body.get("fallback_models", []),
        messages=messages,
        stream=False if image_model else body.get("stream", True),
        temperature=body.get("options", {}).get("temperature", 0.7),
        max_tokens=body.get("options", {}).get("num_predict"),
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
        tags=tags,
        request_type="image" if image_model else "text",
    )

    return await _route_and_stream(request, inference_req)


@router.get("/api/version")
async def ollama_version(request: Request):
    """Ollama-compatible: return version info.

    Many clients (Open WebUI, LangChain, etc.) call this as a health check
    to verify the Ollama API is reachable.  Returns the same ``version``
    field as Ollama (for compatibility) plus ``herd_version`` so callers
    can distinguish a Herd router from a plain Ollama instance.
    """
    from importlib.metadata import version as pkg_version

    # Get Ollama version from the first online node
    registry = request.app.state.registry
    ollama_version = None
    for node in registry.get_online_nodes():
        if node.ollama_base_url:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{node.ollama_base_url}/api/version")
                    if resp.status_code == 200:
                        ollama_version = resp.json().get("version")
                        break
            except Exception:
                pass

    result = {"version": ollama_version or "unknown"}
    result["herd_version"] = pkg_version("ollama-herd")
    return result


# Synthesized ``modified_at`` for models Ollama didn't describe (mlx:, image,
# vision-embedding, or an older node agent that doesn't send metadata).  Pinned
# to the first time this router listed the model, so the value is stable across
# calls — a client sorting by date doesn't see the list reshuffle every poll.
_SYNTH_MODIFIED_AT: dict[str, str] = {}


def _synth_modified_at(name: str) -> str:
    """RFC3339 timestamp, fractional seconds + ``Z``, like Go's RFC3339Nano."""
    if name not in _SYNTH_MODIFIED_AT:
        now = datetime.now(UTC)
        _SYNTH_MODIFIED_AT[name] = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return _SYNTH_MODIFIED_AT[name]


def _synth_digest(name: str) -> str:
    """A stable, unique stand-in digest for models with no Ollama manifest.

    Not an empty string: some clients key their model lists on ``digest``,
    so N models sharing ``""`` would collapse into one row.  Hashing the name
    keeps it unique and constant.  It is NOT a content digest — the name
    prefix makes that unmistakable to anyone who checks.
    """
    return hashlib.sha256(f"ollama-herd:{name}".encode()).hexdigest()


def _tag_entry(
    name: str,
    *,
    size: int,
    meta: ModelTagMeta | None,
    default_format: str = "",
) -> dict:
    """One Ollama-shaped ``/api/tags`` entry, never ``null`` in a required key.

    Real metadata from the node's Ollama is passed through untouched; anything
    missing gets a non-null default (``""`` / ``[]`` / synthesized date and
    digest).  See docs/api-reference.md § GET /api/tags.
    """
    m = meta or ModelTagMeta(format=default_format)
    return {
        "name": name,
        "model": name,
        "modified_at": m.modified_at or _synth_modified_at(name),
        "size": size,
        "digest": m.digest or _synth_digest(name),
        "details": {
            "parent_model": m.parent_model,
            "format": m.format or default_format,
            "family": m.family,
            "families": list(m.families),
            "parameter_size": m.parameter_size,
            "quantization_level": m.quantization_level,
        },
    }


@router.get("/api/tags")
async def ollama_tags(request: Request):
    """Ollama-compatible: list all models across the fleet.

    Each entry carries Ollama's full field set (``modified_at``, ``digest``,
    ``details.format/family/families/parameter_size/quantization_level/
    parent_model``) plus Herd's ``details.fleet_nodes``.  Strict clients
    (OllamaKit → Enchanted/Ollamac, Reins) decode those as required
    non-null strings and fail the whole listing without them.
    """
    registry = request.app.state.registry
    nodes = registry.get_online_nodes()

    # Fleet-wide lookups: the first node that reports a model's metadata or
    # on-disk size wins.  Same model name = same Ollama manifest in practice.
    fleet_meta: dict[str, ModelTagMeta] = {}
    fleet_sizes: dict[str, float] = {}
    for node in nodes:
        if not node.ollama:
            continue
        for name, meta in (node.ollama.models_available_meta or {}).items():
            fleet_meta.setdefault(name, meta)
        for name, gb in node.ollama.models_available_sizes.items():
            fleet_sizes.setdefault(name, gb)

    def _disk_bytes(name: str) -> int:
        # models_available_sizes is decimal GB (bytes / 1e9) — see ollama_client.
        gb = fleet_sizes.get(name, 0.0)
        return int(round(gb * 1e9)) if gb else 0

    seen: dict[str, dict] = {}

    def _add(name: str, node_id: str, entry_factory) -> None:
        if name not in seen:
            entry = entry_factory()
            entry["details"]["fleet_nodes"] = [node_id]
            seen[name] = entry
        elif node_id not in seen[name]["details"]["fleet_nodes"]:
            seen[name]["details"]["fleet_nodes"].append(node_id)

    for node in nodes:
        if not node.ollama:
            continue
        for m in node.ollama.models_loaded:
            # Ollama's /api/tags reports on-disk size; prefer it, and fall back
            # to the resident size only when no node reported the disk size.
            size = _disk_bytes(m.name) or int(m.size_gb * (1024**3))
            _add(m.name, node.node_id, lambda m=m, size=size: _tag_entry(
                m.name, size=size, meta=fleet_meta.get(m.name),
                default_format="mlx" if m.name.startswith("mlx:") else "",
            ))
        for name in node.ollama.models_available:
            _add(name, node.node_id, lambda name=name: _tag_entry(
                name, size=_disk_bytes(name), meta=fleet_meta.get(name),
                default_format="mlx" if name.startswith("mlx:") else "",
            ))

    # Include image models (mflux + DiffusionKit) in the unified list
    for node in nodes:
        if not node.image:
            continue
        for im in node.image.models_available:
            def _image_entry(im=im):
                entry = _tag_entry(
                    im.name, size=0, meta=None,
                    # "mflux-generate-…" → "mflux", "diffusionkit-cli" → "diffusionkit"
                    default_format=(im.binary or "").split("-")[0],
                )
                entry["details"]["type"] = "image"
                return entry
            _add(im.name, node.node_id, _image_entry)

    # Include vision embedding models (DINOv2, SigLIP, CLIP)
    for node in nodes:
        if not node.vision_embedding:
            continue
        for vm in node.vision_embedding.models_available:
            def _vision_entry(vm=vm):
                entry = _tag_entry(vm.name, size=0, meta=None, default_format=vm.runtime)
                entry["details"].update({
                    "type": "vision-embedding",
                    "runtime": vm.runtime,
                    "dimensions": vm.dimensions,
                })
                return entry
            _add(vm.name, node.node_id, _vision_entry)

    return {"models": list(seen.values())}


@router.get("/api/ps")
async def ollama_ps(request: Request):
    """Fleet-wide: all currently loaded models across all nodes."""
    registry = request.app.state.registry
    models = []
    for node in registry.get_online_nodes():
        if not node.ollama:
            continue
        for m in node.ollama.models_loaded:
            models.append(
                {
                    "name": m.name,
                    "model": m.name,
                    "size": int(m.size_gb * (1024**3)),
                    "fleet_node": node.node_id,
                }
            )
    return {"models": models}


_SHOW_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)


def _name_candidates(model: str) -> list[str]:
    """Names to look up for ``model`` — Ollama resolves a bare name to ``:latest``."""
    if ":" in model or model.startswith("mlx:"):
        return [model]
    return [model, f"{model}:latest"]


def _synth_show(
    name: str, *, fmt: str, capabilities: list[str], family: str = "",
) -> dict:
    """Minimal valid ``/api/show`` body for a model no Ollama instance serves.

    Mirrors Ollama's response shape so clients that parse it (Ollama's desktop
    app, AnythingLLM, Cherry Studio) get every key they index.  It claims only
    what Herd actually knows: no template/parameters, and ``model_info`` empty
    rather than an invented context length.
    """
    return {
        "modelfile": "",
        "parameters": "",
        "template": "",
        "details": {
            "parent_model": "",
            "format": fmt,
            "family": family,
            "families": [family] if family else [],
            "parameter_size": "",
            "quantization_level": "",
        },
        "model_info": {},
        "capabilities": capabilities,
        "modified_at": _synth_modified_at(name),
    }


@router.post("/api/show")
async def ollama_show(request: Request):
    """Ollama-compatible model details.

    Ollama's desktop app calls this before every chat, AnythingLLM reads the
    context window and tool capability from it, and Cherry Studio's "Check"
    button probes it.  Ollama-served models are proxied to a node that has the
    model (loaded nodes first, falling through on error); MLX, image and
    vision-embedding models get a synthesized minimal response.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    # "model" is current Ollama; "name" is the legacy field older clients send.
    model = str(body.get("model") or body.get("name") or "").strip()
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    registry = request.app.state.registry
    nodes = registry.get_online_nodes()
    names = _name_candidates(model)

    # MLX models live behind mlx_lm.server, which has no /api/show.
    if model.startswith("mlx:"):
        if any(
            n.ollama and model in n.ollama.models_available for n in nodes
        ):
            return _synth_show(model, fmt="mlx", capabilities=["completion"])
        return JSONResponse(status_code=404, content={"error": f"model '{model}' not found"})

    # Ollama-served: loaded nodes first (the model's metadata is warm there),
    # then any node that has it on disk.
    loaded, on_disk = [], []
    for n in nodes:
        if not n.ollama:
            continue
        loaded_names = {m.name for m in n.ollama.models_loaded}
        match = next((c for c in names if c in loaded_names), None)
        if match:
            loaded.append((n, match))
            continue
        match = next((c for c in names if c in n.ollama.models_available), None)
        if match:
            on_disk.append((n, match))

    candidates = loaded + on_disk
    if candidates:
        proxy = request.app.state.streaming_proxy
        last_error: tuple[int, dict] | None = None
        for node, resolved in candidates:
            fwd = {k: v for k, v in body.items() if k != "name"}
            fwd["model"] = resolved
            try:
                client = proxy._get_client(node.node_id)
                resp = await client.post("/api/show", json=fwd, timeout=_SHOW_TIMEOUT)
            except Exception as e:  # noqa: BLE001 — try the next node
                logger.warning(
                    f"/api/show for {resolved} failed on {node.node_id}: "
                    f"{type(e).__name__}: {e}"
                )
                last_error = (502, {"error": f"failed to reach Ollama on {node.node_id}: "
                                             f"{type(e).__name__}: {e}"})
                continue
            if resp.status_code == 200:
                return JSONResponse(
                    content=resp.json(),
                    headers={"X-Fleet-Node": node.node_id},
                )
            try:
                err_body = resp.json()
            except ValueError:
                err_body = {"error": resp.text[:500]}
            logger.warning(
                f"/api/show for {resolved} returned HTTP {resp.status_code} "
                f"on {node.node_id}"
            )
            last_error = (resp.status_code, err_body)
        # Every node refused.  A 404 from all of them means the registry is
        # stale (model deleted since the last heartbeat) — surface it as-is.
        status, content = last_error or (502, {"error": "no node answered /api/show"})
        return JSONResponse(status_code=status, content=content)

    # Non-Ollama backends that /api/tags also lists.
    for n in nodes:
        if n.vision_embedding and any(
            m.name == model for m in n.vision_embedding.models_available
        ):
            vm = next(m for m in n.vision_embedding.models_available if m.name == model)
            return _synth_show(model, fmt=vm.runtime, capabilities=["embedding"])
        if n.image and any(m.name == model for m in n.image.models_available):
            im = next(m for m in n.image.models_available if m.name == model)
            return _synth_show(
                model, fmt=(im.binary or "").split("-")[0], capabilities=["image"],
            )

    return JSONResponse(status_code=404, content={"error": f"model '{model}' not found"})


# Non-Ollama models that require separate installation
_NON_OLLAMA_MODELS: dict[str, str] = {
    "z-image-turbo": "uv tool install mflux (macOS Apple Silicon only)",
    "flux-dev": "uv tool install mflux (macOS Apple Silicon only)",
    "flux-schnell": "uv tool install mflux (macOS Apple Silicon only)",
    "sd3-medium": "uv tool install diffusionkit (macOS Apple Silicon only)",
    "sd3.5-large": "uv tool install diffusionkit (macOS Apple Silicon only)",
    "qwen3-asr": "pip install 'mlx-qwen3-asr[serve]' (macOS Apple Silicon only)",
}


def _check_non_ollama_model(model: str) -> str | None:
    """Return install instructions if model is not pullable via Ollama, else None."""
    base = model.split(":")[0]
    return _NON_OLLAMA_MODELS.get(base)


@router.post("/api/pull")
async def ollama_pull(request: Request):
    """Pull a model onto the fleet via Ollama's /api/pull.

    Accepts both 'name' (Ollama standard) and 'model' (common agent convention).
    Auto-selects the best node if node_id is not provided.
    For non-Ollama models (mflux, DiffusionKit, MLX), returns install instructions.
    """
    body = await request.json()
    model = body.get("name", body.get("model", "")).strip()
    if not model:
        return JSONResponse(
            {"error": "model name required (use 'name' or 'model' field)"},
            status_code=400,
        )

    # Normalize: add :latest tag if no tag specified
    if ":" not in model:
        model = f"{model}:latest"

    # Check non-Ollama models
    install_hint = _check_non_ollama_model(model)
    if install_hint:
        return JSONResponse(
            {
                "error": (
                    f"'{model.split(':')[0]}' is not an Ollama model and cannot be "
                    f"pulled via /api/pull. Install it directly on the node: {install_hint}"
                )
            },
            status_code=400,
        )

    # Duplicate pull check
    if model in _pulls_in_flight:
        return JSONResponse(
            {"error": f"'{model}' is already being pulled. Try again when it completes."},
            status_code=409,
        )

    registry = request.app.state.registry
    proxy = request.app.state.streaming_proxy
    scorer = request.app.state.scorer

    # Select target node
    node_id = body.get("node_id")
    if node_id:
        node = registry.get_node(node_id)
        if not node or node.status == NodeStatus.OFFLINE:
            return JSONResponse(
                {"error": f"Node '{node_id}' not found or offline"},
                status_code=404,
            )
    else:
        node_id = _pick_pull_node(registry, model, scorer)
        if not node_id:
            return JSONResponse(
                {"error": "No node has enough available memory to pull this model"},
                status_code=503,
            )

    stream = body.get("stream", True)

    if not stream:
        # Non-streaming: block until pull completes
        _pulls_in_flight.add(model)
        try:
            success = await proxy.pull_model(node_id, model)
            if success:
                node = registry.get_node(node_id)
                if node and node.ollama and model not in node.ollama.models_available:
                    node.ollama.models_available.append(model)
                return JSONResponse(
                    {"status": "success"},
                    headers={"X-Fleet-Node": node_id},
                )
            return JSONResponse(
                {"error": f"Pull failed on node '{node_id}'"},
                status_code=500,
                headers={"X-Fleet-Node": node_id},
            )
        finally:
            _pulls_in_flight.discard(model)

    # Streaming: yield NDJSON progress
    async def _stream_pull():
        _pulls_in_flight.add(model)
        success = False
        try:
            async for chunk in proxy.pull_model_streaming(node_id, model):
                yield chunk
                # Check if this was the success line
                try:
                    data = json.loads(chunk)
                    if data.get("status") == "success":
                        success = True
                except (json.JSONDecodeError, ValueError):
                    pass
            if success:
                node = registry.get_node(node_id)
                if node and node.ollama and model not in node.ollama.models_available:
                    node.ollama.models_available.append(model)
        except httpx.HTTPStatusError as e:
            yield json.dumps({"error": f"HTTP {e.response.status_code} from node"}).encode() + b"\n"
        except Exception as e:
            yield json.dumps({"error": repr(e)}).encode() + b"\n"
        finally:
            _pulls_in_flight.discard(model)

    return StreamingResponse(
        _stream_pull(),
        media_type="application/x-ndjson",
        headers={"X-Fleet-Node": node_id},
    )


@router.post("/api/embed")
@router.post("/api/embeddings")
async def ollama_embed(request: Request):
    """Ollama-compatible embeddings endpoint. Routes to best node with the model.

    Unlike chat/generate, embeddings are non-streaming — we proxy the request
    directly to Ollama's /api/embed endpoint and return the JSON response.

    Vision embedding models (clip, dinov2, siglip) are routed to the vision
    embedding service instead of Ollama.
    """
    body = await request.json()
    return await dispatch_embed(request, body)


async def dispatch_embed(
    request: Request,
    body: dict,
    *,
    original_format: RequestFormat = RequestFormat.OLLAMA,
):
    """The one embedding path: vision → text (fastembed) → Ollama ``/api/embed``.

    Shared by ``/api/embed`` and ``/v1/embeddings`` so an OpenAI-format caller
    gets exactly the same interception (e.g. nomic-embed-text → fastembed),
    scoring, retries and traces.  Returns an Ollama ``/api/embed``-shaped
    response; ``original_format`` only labels the trace.
    """
    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    # Route vision embedding models to the embedding service
    from fleet_manager.server.routes.embedding_compat import (
        embed_image,
        is_vision_embedding_model,
    )

    if is_vision_embedding_model(model):
        # Stash parsed body so embed_image doesn't re-read the stream
        request.state._parsed_body = body
        return await embed_image(request)

    # Route text embedding models (nomic-embed-text, etc.) to the native
    # fastembed server on port 11439 — completely bypasses Ollama so embed
    # requests never queue behind LLM inference slots.
    from fleet_manager.server.routes.text_embedding_compat import (
        embed_text,
        is_text_embedding_model,
    )
    if is_text_embedding_model(model):
        request.state._parsed_body = body
        return await embed_text(request)

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        messages=[],
        stream=False,
        original_format=original_format,
        raw_body=body,
        tags=tags,
        request_type="embed",
    )

    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry
    settings = request.app.state.settings

    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry,
        proxy=proxy, settings=settings,
    )

    if not results:
        all_fleet_models = get_all_fleet_models(registry)
        missing = model not in all_fleet_models
        await record_routing_rejection(
            getattr(request.app.state, "trace_store", None),
            inference_req,
            reason=(
                f"model '{model}' not found on any node"
                if missing
                else f"no node could serve '{model}' within the holding timeout"
            ),
            original_format=getattr(inference_req, "original_format", "") or "ollama",
            client_ip=(request.client.host if request.client else ""),
        )
        if missing:
            return JSONResponse(
                status_code=404,
                content={"error": f"model '{model}' not found on any node. "
                         f"Run 'ollama pull {model}' on a fleet device."},
            )
        return JSONResponse(
            status_code=503,
            content={"error": f"model '{model}' exists but no node can serve it "
                     f"right now. Try again shortly."},
        )

    winner = results[0]
    node = registry.get_node(winner.node_id)
    if not node:
        return JSONResponse(status_code=503, content={"error": "Selected node unavailable"})

    trace_store = request.app.state.trace_store
    client_ip = request.client.host if request.client else ""

    # Proxy directly to Ollama's /api/embed endpoint using the proxy's
    # managed HTTP client (handles LAN IP rewriting, connection pooling).
    embed_body = dict(body)
    embed_body.pop("metadata", None)
    embed_body.setdefault("keep_alive", -1)

    _EMBED_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)
    # Retry on ReadTimeout: Ollama queues embed requests behind concurrent LLM
    # inference. Under load a brief backoff often clears the slot.
    _EMBED_RETRY_BACKOFFS_S = (1.0, 3.0)

    start = time.time()
    last_exc: Exception | None = None
    retry_count = 0

    for backoff in (*_EMBED_RETRY_BACKOFFS_S, None):
        try:
            client = proxy._get_client(winner.node_id)
            resp = await client.post(
                "/api/embed", json=embed_body, timeout=_EMBED_TIMEOUT,
            )
            resp.raise_for_status()
            elapsed_ms = (time.time() - start) * 1000

            result = resp.json()
            logger.info(
                f"Embed {inference_req.request_id[:8]} completed on {winner.node_id} "
                f"in {elapsed_ms:.0f}ms model={actual_model}"
                + (f" (retry #{retry_count})" if retry_count else "")
            )

            if trace_store:
                asyncio.ensure_future(trace_store.record_trace(
                    request_id=inference_req.request_id,
                    model=actual_model,
                    original_model=model,
                    node_id=winner.node_id,
                    score=winner.score,
                    scores_breakdown=(
                        winner.scores_breakdown
                        if hasattr(winner, "scores_breakdown") else None
                    ),
                    status="completed",
                    latency_ms=elapsed_ms,
                    retry_count=retry_count,
                    client_ip=client_ip,
                    original_format=original_format.value,
                    tags=["embed"] + (tags or []),
                ))

            return JSONResponse(
                content=result,
                headers=fleet_headers(
                    node_id=winner.node_id,
                    served_model=actual_model,
                    requested_model=model,
                    backend="ollama",
                    score=winner.score,
                    retries=retry_count,
                ),
            )

        except httpx.HTTPStatusError as e:
            elapsed_ms = (time.time() - start) * 1000
            err_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            logger.error(f"Embed failed on {winner.node_id}: {err_msg}")
            if trace_store:
                asyncio.ensure_future(trace_store.record_trace(
                    request_id=inference_req.request_id,
                    model=actual_model,
                    original_model=model,
                    node_id=winner.node_id,
                    score=winner.score,
                    status="failed",
                    latency_ms=elapsed_ms,
                    retry_count=retry_count,
                    client_ip=client_ip,
                    original_format=original_format.value,
                    error_message=err_msg,
                    tags=["embed"] + (tags or []),
                ))
            return JSONResponse(
                status_code=e.response.status_code,
                content={"error": f"Ollama returned {err_msg}"},
            )

        except httpx.ReadTimeout as e:
            retry_count += 1
            last_exc = e
            if backoff is None:
                break  # exhausted retries — fall through to failure path
            logger.warning(
                f"Embed ReadTimeout on {winner.node_id} (attempt {retry_count}), "
                f"retrying in {backoff}s — Ollama queue likely full"
            )
            await asyncio.sleep(backoff)

        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            error_detail = str(e) or repr(e)
            logger.error(f"Embed failed on {winner.node_id}: {type(e).__name__}: {error_detail}")
            if trace_store:
                asyncio.ensure_future(trace_store.record_trace(
                    request_id=inference_req.request_id,
                    model=actual_model,
                    original_model=model,
                    node_id=winner.node_id,
                    score=winner.score,
                    status="failed",
                    latency_ms=elapsed_ms,
                    retry_count=retry_count,
                    client_ip=client_ip,
                    original_format=original_format.value,
                    error_message=f"{type(e).__name__}: {error_detail}",
                    tags=["embed"] + (tags or []),
                ))
            return JSONResponse(
                status_code=502,
                content={"error": f"Failed to reach Ollama on {winner.node_id}: "
                         f"{type(e).__name__}: {error_detail}"},
            )

    # ReadTimeout exhausted all retries
    elapsed_ms = (time.time() - start) * 1000
    error_detail = str(last_exc) or repr(last_exc)
    logger.error(
        f"Embed ReadTimeout on {winner.node_id} after {retry_count} attempt(s) "
        f"({elapsed_ms:.0f}ms total) — Ollama queue saturated"
    )
    if trace_store:
        asyncio.ensure_future(trace_store.record_trace(
            request_id=inference_req.request_id,
            model=actual_model,
            original_model=model,
            node_id=winner.node_id,
            score=winner.score,
            status="failed",
            latency_ms=elapsed_ms,
            retry_count=retry_count,
            client_ip=client_ip,
            original_format=original_format.value,
            error_message=f"ReadTimeout after {retry_count} attempt(s): {error_detail}",
            tags=["embed"] + (tags or []),
        ))
    return JSONResponse(
        status_code=504,
        content={"error": f"Embed timed out on {winner.node_id} after {retry_count} attempt(s). "
                 f"Ollama may be saturated with concurrent inference. Try again shortly."},
    )


async def _route_and_stream(request: Request, inference_req: InferenceRequest):
    """Shared routing logic for Ollama endpoints with holding queue + fallbacks."""
    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry
    settings = request.app.state.settings
    model = inference_req.original_model or inference_req.model
    if not inference_req.client_ip:
        inference_req.client_ip = request.client.host if request.client else ""
    logger.info(f"Ollama request: model={model} stream={inference_req.stream}")

    # Score with fallback support + auto-pull (per-request strict-mode override)
    allow_fallback = parse_allow_fallback(inference_req.raw_body, request.headers)
    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry,
        proxy=proxy, settings=settings, allow_fallback=allow_fallback,
    )

    if not results:
        logger.warning(f"No nodes for model={model} fallbacks={inference_req.fallback_models}")
        models_tried = [model] + inference_req.fallback_models
        all_fleet_models = get_all_fleet_models(registry)
        any_exists = any(m in all_fleet_models for m in models_tried)

        await record_routing_rejection(
            getattr(request.app.state, "trace_store", None),
            inference_req,
            reason=(
                f"no node could serve '{model}' within the holding timeout"
                if any_exists
                else f"none of {models_tried} exist on any node"
            ),
            original_format=getattr(inference_req, "original_format", "") or "ollama",
            client_ip=inference_req.client_ip,
        )

        if not any_exists:
            models_str = "', '".join(models_tried)
            return JSONResponse(
                status_code=404,
                content={
                    "error": f"model(s) '{models_str}' not found on any node. "
                    f"Run 'ollama pull <model>' on a fleet device."
                },
            )
        return JSONResponse(
            status_code=503,
            content={
                "error": f"model '{model}' exists but no node can serve it "
                f"right now. Try again shortly."
            },
        )

    # Apply fallback if a different model was selected
    fallback_used = actual_model != model
    if fallback_used:
        inference_req.model = actual_model
        if "model" in inference_req.raw_body:
            inference_req.raw_body["model"] = actual_model

    winner = results[0]
    entry = QueueEntry(
        request=inference_req,
        assigned_node=winner.node_id,
        routing_score=winner.score,
        routing_breakdown=winner.scores_breakdown,
        fallback_used=fallback_used,
    )
    queue_key = winner.queue_key

    process_fn = proxy.make_process_fn(queue_key, queue_mgr, scorer=scorer, settings=settings)
    try:
        response_future = await queue_mgr.enqueue(entry, process_fn)
    except ClientConcurrencyExceeded as e:
        return client_concurrency_response(e)
    stream = await response_future

    # Build response headers — canonical X-Fleet-* set via the shared builder.
    headers = fleet_headers(
        node_id=winner.node_id,
        served_model=actual_model,
        requested_model=model,
        backend="mlx" if actual_model.startswith("mlx:") else "ollama",
        score=winner.score,
        retries=entry.retry_count,
        affinity=affinity_from_breakdown(winner.scores_breakdown),
        extra=check_context_overflow(winner, inference_req, registry),
    )

    if inference_req.stream:

        async def _stream_and_cleanup():
            async for chunk in stream:
                yield chunk
            # Add thinking headers for streaming (trailer-style — available after stream)
            proxy.pop_token_counts(inference_req.request_id)
            proxy.pop_request_meta(inference_req.request_id)

        return StreamingResponse(
            _stream_and_cleanup(),
            media_type="application/x-ndjson",
            headers=headers,
        )
    else:
        # Non-streaming: accumulate full response, consume entire stream
        full_response = ""
        final_data = None
        async for chunk in stream:
            chunk = chunk.strip()
            if chunk:
                try:
                    data = json.loads(chunk)
                    full_response += data.get("message", {}).get("content", "")
                    full_response += data.get("response", "")
                    if data.get("done"):
                        final_data = data
                except json.JSONDecodeError as e:
                    logger.debug(f"Skipping malformed Ollama chunk: {e}")

        # Add thinking-aware headers to non-streaming response
        headers.update(_build_thinking_headers(proxy, inference_req.request_id))

        # Clean up token tracking
        proxy.pop_token_counts(inference_req.request_id)

        # Handle Ollama native image generation response
        if final_data and final_data.get("image"):
            import base64
            import time as time_mod

            from fleet_manager.server.routes.image_compat import _record_image_gen

            png_bytes = base64.b64decode(final_data["image"])
            elapsed_ms = int((time_mod.time() - inference_req.created_at) * 1000)
            _record_image_gen(
                model=inference_req.model,
                node_id=winner.node_id,
                status="completed",
                generation_ms=elapsed_ms,
            )
            logger.info(
                f"Ollama native image gen: model={inference_req.model} "
                f"node={winner.node_id} {len(png_bytes)} bytes in {elapsed_ms}ms"
            )
            return Response(
                content=png_bytes,
                media_type="image/png",
                headers={
                    **headers,
                    "X-Generation-Time": str(elapsed_ms),
                },
            )

        if final_data:
            final_data["response"] = full_response
            final_data["message"] = {"role": "assistant", "content": full_response}
            return JSONResponse(content=final_data, headers=headers)
        return JSONResponse(
            content={
                "response": full_response,
                "message": {"role": "assistant", "content": full_response},
                "done": True,
            },
            headers=headers,
        )
