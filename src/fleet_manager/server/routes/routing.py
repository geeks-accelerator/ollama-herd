"""Shared routing logic — holding queue with model fallback support."""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.request import InferenceRequest, RoutingResult

logger = logging.getLogger(__name__)

HOLD_TIMEOUT = 30.0
HOLD_RETRY_INTERVAL = 2.0


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
) -> tuple[list[RoutingResult], str]:
    """
    Try scoring the primary model, then each fallback in order.

    Returns (results, actual_model) where actual_model is the model that was
    successfully scored. Returns ([], "") if no model/node combination works.
    """
    models_to_try = [inference_req.model] + inference_req.fallback_models

    deadline = time.time() + HOLD_TIMEOUT
    hold_logged = False
    while time.time() < deadline:
        for model in models_to_try:
            queue_depths = queue_mgr.get_queue_depths()
            results = scorer.score_request(model, queue_depths)
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
                or model
                in [m.name for m in (n.ollama.models_loaded if n.ollama else [])]
                for n in registry.get_all_nodes()
            )
            if model_exists:
                any_exists = True
                break

        if not any_exists:
            logger.debug(f"None of models {models_to_try} exist on any node, stopping hold")
            break  # None of the models exist at all

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
    return [], ""


def get_all_fleet_models(registry) -> set[str]:
    """Collect all model names from all nodes (including offline)."""
    all_models = set()
    for n in registry.get_all_nodes():
        if n.ollama:
            all_models.update(m.name for m in n.ollama.models_loaded)
            all_models.update(n.ollama.models_available)
    return all_models
