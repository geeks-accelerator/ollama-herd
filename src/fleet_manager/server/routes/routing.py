"""Shared routing logic — holding queue with model fallback support."""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.node import MemoryPressure, NodeStatus
from fleet_manager.models.request import InferenceRequest, RoutingResult
from fleet_manager.server.model_knowledge import classify_model
from fleet_manager.server.scorer import ScoringEngine

logger = logging.getLogger(__name__)

HOLD_TIMEOUT = 30.0
HOLD_RETRY_INTERVAL = 2.0

# Track in-flight pulls to prevent duplicate concurrent pulls
_pulls_in_flight: set[str] = set()

# Recent VRAM fallback events for health visibility
_vram_fallback_events: list[dict] = []


def extract_tags(body: dict, headers=None) -> list[str]:
    """Extract tags from request body and headers.

    Sources (merged, deduplicated):
    1. body.metadata.tags (list of strings)
    2. X-Herd-Tags header (comma-separated)
    3. body.user (string, stored as "user:<value>")
    """
    tags = []

    # Source 1: metadata.tags in body
    metadata = body.get("metadata", {})
    if isinstance(metadata, dict):
        body_tags = metadata.get("tags", [])
        if isinstance(body_tags, list):
            tags.extend(str(t) for t in body_tags if t)

    # Source 2: X-Herd-Tags header
    if headers:
        header_val = headers.get("x-herd-tags", "")
        if header_val:
            tags.extend(t.strip() for t in header_val.split(",") if t.strip())

    # Source 3: user field
    user = body.get("user", "")
    if user:
        tags.append(f"user:{user}")

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return unique


async def score_with_fallbacks(
    inference_req: InferenceRequest,
    scorer,
    queue_mgr,
    registry,
    *,
    proxy=None,
    settings=None,
) -> tuple[list[RoutingResult], str]:
    """
    Try scoring the primary model, then each fallback in order.

    When vram_fallback is enabled: if the best score only has a COLD thermal
    signal (model not loaded), route to the best loaded model in the same
    category instead of triggering a slow cold load.

    If no model exists on any node and auto-pull is enabled, pulls the primary
    model onto the best available node and retries.

    Returns (results, actual_model) where actual_model is the model that was
    successfully scored. Returns ([], "") if no model/node combination works.
    """
    models_to_try = [inference_req.model] + inference_req.fallback_models
    estimated_tokens = ScoringEngine.estimate_tokens(inference_req.messages)
    vram_fallback_enabled = settings and getattr(settings, "vram_fallback", False)

    # --- First pass: try all models, check if any are HOT ---
    cold_results: tuple[list[RoutingResult], str] | None = None
    queue_depths = queue_mgr.get_queue_depths()

    for model in models_to_try:
        results = scorer.score_request(model, queue_depths, estimated_tokens)
        if results:
            winner = results[0]
            hot_threshold = settings.score_model_hot if settings else 50.0
            if winner.scores_breakdown.get("thermal", 0) >= hot_threshold:
                # Model is loaded in VRAM — use it directly
                if model != inference_req.model:
                    logger.info(
                        f"Fallback: '{inference_req.model}' unavailable, "
                        f"using '{model}' instead"
                    )
                return results, model
            # Model scored but only COLD/WARM — save as fallback
            if cold_results is None:
                cold_results = (results, model)

    # --- VRAM-aware fallback: route to a loaded model instead of cold-loading ---
    if vram_fallback_enabled and cold_results is not None:
        fallback_result = _try_vram_fallback(
            inference_req, scorer, queue_depths, estimated_tokens, models_to_try,
        )
        if fallback_result:
            return fallback_result

    # --- Return cold results if available (will trigger cold load) ---
    if cold_results is not None:
        return cold_results

    # --- Holding queue: model exists but no node available ---
    deadline = time.time() + HOLD_TIMEOUT
    hold_logged = False
    while time.time() < deadline:
        for model in models_to_try:
            queue_depths = queue_mgr.get_queue_depths()
            results = scorer.score_request(model, queue_depths, estimated_tokens)
            if results:
                if model != inference_req.model:
                    logger.info(
                        f"Fallback: '{inference_req.model}' unavailable, "
                        f"using '{model}' instead"
                    )
                return results, model

        # Check if ANY of the models exist on any node
        any_exists = False
        for model in models_to_try:
            model_exists = any(
                model in (n.ollama.models_available if n.ollama else [])
                or model in [m.name for m in (n.ollama.models_loaded if n.ollama else [])]
                for n in registry.get_all_nodes()
            )
            if model_exists:
                any_exists = True
                break

        if not any_exists:
            logger.debug(f"None of models {models_to_try} exist on any node, stopping hold")
            break

        if not hold_logged:
            logger.info(
                f"Holding request for {inference_req.model}: model exists but no node "
                f"available, will retry for up to {HOLD_TIMEOUT}s"
            )
            hold_logged = True

        await asyncio.sleep(HOLD_RETRY_INTERVAL)

    if hold_logged:
        logger.warning(
            f"Holding queue timeout: no node became available for "
            f"{inference_req.model} within {HOLD_TIMEOUT}s"
        )

    # Auto-pull: model doesn't exist on any node — try pulling it
    if proxy and settings and getattr(settings, "auto_pull", False):
        pulled_model = await _try_auto_pull(
            models_to_try, scorer, queue_mgr, registry, proxy, settings,
            estimated_tokens,
        )
        if pulled_model:
            return pulled_model

    return [], ""


def _try_vram_fallback(
    inference_req: InferenceRequest,
    scorer,
    queue_depths: dict[str, int],
    estimated_tokens: int,
    exclude_models: list[str],
) -> tuple[list[RoutingResult], str] | None:
    """Try routing to a loaded model in the same category, then any loaded model.

    Respects model priority: if the requested model has significantly
    higher usage-based priority than the fallback candidate, skip the
    fallback and let the request cold-load or queue instead.
    """
    from fleet_manager.server.model_preloader import (
        _priority_cache,
        get_model_priority,
    )

    category = classify_model(inference_req.model)

    # Try same category first
    loaded_options = scorer.score_loaded_models(
        category.value, queue_depths, estimated_tokens,
        exclude_models=exclude_models,
    )
    if loaded_options:
        best_result, best_model = loaded_options[0]

        # Priority check: don't route a high-priority model to a
        # low-priority one.  The requested model should cold-load instead.
        req_priority = get_model_priority(inference_req.model, _priority_cache)
        fallback_priority = get_model_priority(best_model, _priority_cache)
        if req_priority > 0 and req_priority > fallback_priority * 2:
            logger.info(
                f"VRAM fallback blocked: '{inference_req.model}' "
                f"(priority={req_priority:.0f}) outranks "
                f"'{best_model}' (priority={fallback_priority:.0f}) — "
                f"will cold-load instead"
            )
            return None

        logger.info(
            f"VRAM fallback: '{inference_req.model}' ({category.value}) not loaded, "
            f"routing to loaded '{best_model}' instead"
        )
        _record_vram_fallback(inference_req.model, best_model, category.value)
        return [best_result], best_model

    # No loaded model in same category — try ANY loaded model (with priority check)
    all_loaded = scorer.score_loaded_models(
        None, queue_depths, estimated_tokens,
        exclude_models=exclude_models,
    )
    if all_loaded:
        best_result, best_model = all_loaded[0]

        req_priority = get_model_priority(inference_req.model, _priority_cache)
        fallback_priority = get_model_priority(best_model, _priority_cache)
        if req_priority > 0 and req_priority > fallback_priority * 2:
            logger.info(
                f"VRAM fallback (cross-category) blocked: '{inference_req.model}' "
                f"(priority={req_priority:.0f}) outranks "
                f"'{best_model}' (priority={fallback_priority:.0f})"
            )
            return None

        fallback_cat = classify_model(best_model).value
        logger.info(
            f"VRAM fallback (cross-category): '{inference_req.model}' not loaded, "
            f"no {category.value} model available, using '{best_model}' ({fallback_cat})"
        )
        _record_vram_fallback(inference_req.model, best_model, category.value)
        return [best_result], best_model

    return None


def _record_vram_fallback(requested: str, actual: str, category: str) -> None:
    """Record a VRAM fallback event for health visibility."""
    _vram_fallback_events.append({
        "timestamp": time.time(),
        "requested_model": requested,
        "actual_model": actual,
        "category": category,
    })
    # Keep last 100 events
    if len(_vram_fallback_events) > 100:
        _vram_fallback_events.pop(0)


def get_vram_fallback_events(hours: float = 24) -> list[dict]:
    """Return VRAM fallback events from the last N hours."""
    cutoff = time.time() - (hours * 3600)
    return [e for e in _vram_fallback_events if e["timestamp"] >= cutoff]


async def _try_auto_pull(
    models_to_try, scorer, queue_mgr, registry, proxy, settings,
    estimated_tokens,
) -> tuple[list[RoutingResult], str] | None:
    """Attempt to auto-pull the first model and retry scoring."""
    model = models_to_try[0]  # Pull the primary model

    # Skip if already being pulled by another request
    if model in _pulls_in_flight:
        logger.info(f"Auto-pull: {model} already being pulled, waiting...")
        # Wait for the other pull to finish, then retry scoring
        deadline = time.time() + settings.auto_pull_timeout
        while model in _pulls_in_flight and time.time() < deadline:
            await asyncio.sleep(2.0)
        # Retry scoring — model should now be available
        queue_depths = queue_mgr.get_queue_depths()
        results = scorer.score_request(model, queue_depths, estimated_tokens)
        if results:
            return results, model
        return None

    # Pick the best node to pull onto
    node_id = _pick_pull_node(registry, model, scorer)
    if not node_id:
        logger.warning(f"Auto-pull: no suitable node for {model}")
        return None

    _pulls_in_flight.add(model)
    try:
        logger.info(f"Auto-pulling {model} onto {node_id}")
        success = await asyncio.wait_for(
            proxy.pull_model(node_id, model),
            timeout=settings.auto_pull_timeout,
        )
        if not success:
            logger.warning(f"Auto-pull {model} on {node_id} failed")
            return None

        # Pull succeeded — inject model into registry so scorer sees it
        # immediately (next heartbeat will bring the full truth).
        logger.info(f"Auto-pull {model} on {node_id} complete, retrying routing")
        node = registry.get_node(node_id)
        if node and node.ollama and model not in node.ollama.models_available:
            node.ollama.models_available.append(model)

        queue_depths = queue_mgr.get_queue_depths()
        results = scorer.score_request(model, queue_depths, estimated_tokens)
        if results:
            return results, model
        logger.warning(f"Auto-pull {model} succeeded but scoring still fails")
        return None
    except TimeoutError:
        logger.warning(
            f"Auto-pull {model} on {node_id} timed out "
            f"after {settings.auto_pull_timeout}s"
        )
        return None
    finally:
        _pulls_in_flight.discard(model)


def _pick_pull_node(registry, model: str, scorer) -> str | None:
    """Pick the online node with the most available memory that can fit the model."""
    best_node = None
    best_available = 0.0
    model_size = 10.0  # default estimate

    for node in registry.get_all_nodes():
        if node.status == NodeStatus.OFFLINE:
            continue
        if not node.ollama or not node.memory:
            continue
        if node.memory.pressure == MemoryPressure.CRITICAL:
            continue
        if node.capacity and node.capacity.mode == "paused":
            continue

        # Estimate model size from this node's perspective
        est = scorer._estimate_model_size(model, node)
        if est > 0:
            model_size = est

        available = node.memory.available_gb
        if node.capacity and node.capacity.ceiling_gb > 0:
            available = min(available, node.capacity.ceiling_gb)

        if available >= model_size and available > best_available:
            best_available = available
            best_node = node.node_id

    return best_node


def check_context_overflow(
    winner: RoutingResult,
    inference_req: InferenceRequest,
    registry,
) -> dict[str, str]:
    """Return overflow warning headers if estimated tokens exceed context window."""
    ctx_score = winner.scores_breakdown.get("context_fit", 0)
    if ctx_score >= 0:
        return {}

    # Look up the winning node's context_length for the model
    estimated_tokens = ScoringEngine.estimate_tokens(inference_req.messages)
    node = registry.get_node(winner.node_id)
    ctx_length = 0
    if node and node.ollama:
        for m in node.ollama.models_loaded:
            if m.name == inference_req.model and m.context_length > 0:
                ctx_length = m.context_length
                break

    logger.warning(
        f"Context overflow: ~{estimated_tokens} estimated tokens exceeds "
        f"{ctx_length} context window on {winner.node_id} — "
        f"input may be truncated by Ollama"
    )
    return {
        "X-Fleet-Context-Overflow": (
            f"estimated_tokens={estimated_tokens}; context_length={ctx_length}"
        ),
    }


def get_all_fleet_models(registry) -> set[str]:
    """Collect all model names from all nodes (including offline)."""
    all_models = set()
    for n in registry.get_all_nodes():
        if n.ollama:
            all_models.update(m.name for m in n.ollama.models_loaded)
            all_models.update(n.ollama.models_available)
    return all_models
