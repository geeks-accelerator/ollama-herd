"""Shared routing logic — holding queue with model fallback support."""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.request import InferenceRequest, RoutingResult

logger = logging.getLogger(__name__)

HOLD_TIMEOUT = 30.0
HOLD_RETRY_INTERVAL = 2.0


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
            break  # None of the models exist at all
        await asyncio.sleep(HOLD_RETRY_INTERVAL)

    return [], ""


def get_all_fleet_models(registry) -> set[str]:
    """Collect all model names from all nodes (including offline)."""
    all_models = set()
    for n in registry.get_all_nodes():
        if n.ollama:
            all_models.update(m.name for m in n.ollama.models_loaded)
            all_models.update(n.ollama.models_available)
    return all_models
