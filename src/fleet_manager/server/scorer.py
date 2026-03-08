"""Scoring Engine — ranks candidate nodes for routing decisions."""

from __future__ import annotations

import logging
import time

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import MemoryPressure, NodeState, NodeStatus
from fleet_manager.models.request import RoutingResult
from fleet_manager.server.registry import NodeRegistry

logger = logging.getLogger(__name__)

WARM_WINDOW_SECONDS = 1800  # 30 minutes


class ScoringEngine:
    def __init__(self, settings: ServerSettings, registry: NodeRegistry, latency_store=None):
        self._s = settings
        self._registry = registry
        self._latency_store = latency_store

    def score_request(self, model: str, queue_depths: dict[str, int]) -> list[RoutingResult]:
        """
        Score all candidate nodes for a model request.
        Returns ranked list (highest score first), empty if no candidates survive.
        """
        candidates = self._eliminate(model)
        if not candidates:
            return []

        results = []
        for node in candidates:
            breakdown = {}

            s1 = self._score_thermal(node, model)
            breakdown["thermal"] = s1

            s2 = self._score_memory_fit(node, model)
            breakdown["memory_fit"] = s2

            queue_key = f"{node.node_id}:{model}"
            depth = queue_depths.get(queue_key, 0)
            s3 = self._score_queue_depth(depth)
            breakdown["queue_depth"] = s3

            s4 = self._score_wait_time(node, model, depth)
            breakdown["wait_time"] = s4

            s5 = self._score_role_affinity(node, model)
            breakdown["role_affinity"] = s5

            total = s1 + s2 + s3 + s4 + s5
            breakdown["total"] = total

            results.append(
                RoutingResult(
                    node_id=node.node_id,
                    queue_key=queue_key,
                    score=total,
                    scores_breakdown=breakdown,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)

        if results:
            winner = results[0]
            logger.info(
                f"Routing {model} → {winner.node_id} "
                f"(score={winner.score:.0f}: "
                f"thermal={winner.scores_breakdown.get('thermal', 0):.0f}, "
                f"mem={winner.scores_breakdown.get('memory_fit', 0):.0f}, "
                f"queue={winner.scores_breakdown.get('queue_depth', 0):.0f}, "
                f"wait={winner.scores_breakdown.get('wait_time', 0):.0f}, "
                f"affinity={winner.scores_breakdown.get('role_affinity', 0):.0f})"
            )

        return results

    def _eliminate(self, model: str) -> list[NodeState]:
        """Stage 1: hard elimination — remove nodes that cannot serve the request."""
        survivors = []
        all_nodes = self._registry.get_all_nodes()
        for node in all_nodes:
            if node.status == NodeStatus.OFFLINE:
                logger.debug(f"Eliminated {node.node_id}: offline")
                continue
            if node.ollama is None:
                logger.debug(f"Eliminated {node.node_id}: no Ollama state")
                continue
            if node.memory and node.memory.pressure == MemoryPressure.CRITICAL:
                logger.debug(f"Eliminated {node.node_id}: critical memory pressure")
                continue

            loaded_names = [m.name for m in node.ollama.models_loaded]
            if model not in loaded_names and model not in node.ollama.models_available:
                logger.debug(f"Eliminated {node.node_id}: model '{model}' not available")
                continue

            # Check memory can fit if model needs loading
            if model not in loaded_names:
                model_size = self._estimate_model_size(model, node)
                if node.memory and node.memory.available_gb < model_size:
                    logger.debug(
                        f"Eliminated {node.node_id}: insufficient memory "
                        f"({node.memory.available_gb:.1f}GB avail < {model_size:.1f}GB needed)"
                    )
                    continue

            survivors.append(node)

        if not survivors and all_nodes:
            logger.warning(f"All {len(all_nodes)} nodes eliminated for model '{model}'")

        return survivors

    def _score_thermal(self, node: NodeState, model: str) -> float:
        """Signal 1: hot (+50), warm/recently unloaded (+30), cold on disk (+10)."""
        loaded = [m.name for m in node.ollama.models_loaded]
        if model in loaded:
            return self._s.score_model_hot  # +50

        # Warm tier: model was loaded within the last 30 minutes (OS page cache likely hot)
        unloaded_at = node.model_unloaded_at.get(model)
        if unloaded_at and (time.time() - unloaded_at) < WARM_WINDOW_SECONDS:
            return self._s.score_model_warm  # +30

        if model in node.ollama.models_available:
            return self._s.score_model_cold  # +10
        return 0.0

    def _score_memory_fit(self, node: NodeState, model: str) -> float:
        """Signal 2: How comfortably does the model fit in available memory?"""
        loaded_names = [m.name for m in node.ollama.models_loaded]
        if model in loaded_names:
            return self._s.score_memory_fit_max

        model_size = self._estimate_model_size(model, node)
        if model_size <= 0 or not node.memory:
            return 0.0

        fit_ratio = node.memory.available_gb / model_size
        if fit_ratio > 2.0:
            return 20.0
        elif fit_ratio > 1.5:
            return 15.0
        elif fit_ratio > 1.2:
            return 8.0
        elif fit_ratio >= 1.0:
            return 3.0
        return 0.0

    def _score_queue_depth(self, depth: int) -> float:
        """Signal 3: Penalty for busy queues."""
        penalty = min(
            self._s.score_queue_depth_max_penalty,
            depth * self._s.score_queue_depth_penalty_per,
        )
        return -penalty

    def _score_wait_time(self, node: NodeState, model: str, depth: int) -> float:
        """Signal 4: Penalty based on estimated wait time using historical latency."""
        if depth == 0 or self._latency_store is None:
            return 0.0

        p75_ms = self._latency_store.get_cached_percentile(node.node_id, model)
        if p75_ms is None:
            # Heuristic: estimate from model size
            model_size = self._estimate_model_size(model, node)
            tokens_per_sec = max(1.0, 100.0 / max(1.0, model_size))
            p75_ms = (100.0 / tokens_per_sec) * 1000

        est_wait_s = (depth * p75_ms) / 1000.0
        penalty = min(self._s.score_wait_time_max_penalty, est_wait_s / 10.0)
        return -penalty

    def _score_role_affinity(self, node: NodeState, model: str) -> float:
        """Signal 5: Large models prefer big nodes, small models prefer small nodes."""
        model_size = self._estimate_model_size(model, node)
        node_mem = node.hardware.memory_total_gb

        if model_size > self._s.score_role_large_threshold_gb:
            if node_mem >= 128:
                return 15.0
            elif node_mem >= 32:
                return 5.0
            return 0.0
        elif model_size < self._s.score_role_small_threshold_gb:
            if node_mem <= 32:
                return 15.0
            elif node_mem <= 128:
                return 8.0
            return 3.0
        return 5.0

    def _estimate_model_size(self, model: str, node: NodeState) -> float:
        """Estimate model size in GB. Check loaded models first, then all nodes."""
        for m in node.ollama.models_loaded:
            if m.name == model:
                return m.size_gb

        for other in self._registry.get_all_nodes():
            if other.ollama:
                for m in other.ollama.models_loaded:
                    if m.name == model:
                        return m.size_gb

        name_lower = model.lower()
        if "671b" in name_lower:
            return 370.0
        if "405b" in name_lower:
            return 230.0
        if "70b" in name_lower:
            return 40.0
        if "32b" in name_lower or "8x7b" in name_lower:
            return 20.0
        if "22b" in name_lower:
            return 14.0
        if "14b" in name_lower:
            return 9.0
        if "7b" in name_lower or "8b" in name_lower:
            return 5.0
        if "3b" in name_lower or "4b" in name_lower:
            return 2.5
        if "1b" in name_lower or "0.5b" in name_lower:
            return 1.0
        if "embed" in name_lower:
            return 0.3
        logger.debug(f"Model size unknown for '{model}', defaulting to 10.0GB")
        return 10.0
