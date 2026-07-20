"""Scoring Engine — ranks candidate nodes for routing decisions."""

from __future__ import annotations

import logging
import time

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import MemoryPressure, NodeState, NodeStatus
from fleet_manager.models.request import RoutingResult
from fleet_manager.server.model_knowledge import classify_model, lookup_model
from fleet_manager.server.registry import NodeRegistry

logger = logging.getLogger(__name__)

WARM_WINDOW_SECONDS = 1800  # 30 minutes


class ScoringEngine:
    def __init__(
        self, settings: ServerSettings, registry: NodeRegistry, latency_store=None,
        sessions=None,
    ):
        self._s = settings
        self._registry = registry
        self._latency_store = latency_store
        # SessionAffinityTracker, or None to disable signal 8 entirely — which
        # is what every existing caller that doesn't pass one gets, so the
        # scorer behaves exactly as before unless a session is threaded through.
        self._sessions = sessions

    def score_request(
        self, model: str, queue_depths: dict[str, int], estimated_tokens: int = 0,
        session_key: str = "",
    ) -> list[RoutingResult]:
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
            s3 = self._score_queue_depth(depth, node=node, queue_depths=queue_depths)
            breakdown["queue_depth"] = s3

            s4 = self._score_wait_time(node, model, depth)
            breakdown["wait_time"] = s4

            s5 = self._score_role_affinity(node, model)
            breakdown["role_affinity"] = s5

            s6 = self._score_availability_trend(node)
            breakdown["availability_trend"] = s6

            s7 = self._score_context_fit(node, model, estimated_tokens)
            breakdown["context_fit"] = s7

            s8 = self._score_session_affinity(node, session_key)
            breakdown["session_affinity"] = s8

            total = s1 + s2 + s3 + s4 + s5 + s6 + s7 + s8
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
                f"affinity={winner.scores_breakdown.get('role_affinity', 0):.0f}, "
                f"avail={winner.scores_breakdown.get('availability_trend', 0):.0f}, "
                f"ctx={winner.scores_breakdown.get('context_fit', 0):.0f}"
                # Only shown when non-zero — most requests have no session, and
                # a permanent "session=0" would be noise. But it must appear
                # whenever it fires, or the printed signals won't sum to the
                # total and the line becomes actively misleading.
                + (
                    f", session={winner.scores_breakdown['session_affinity']:.0f}"
                    if winner.scores_breakdown.get("session_affinity", 0)
                    else ""
                )
                + ")"
            )

        return results

    def score_loaded_models(
        self,
        category: str | None,
        queue_depths: dict[str, int],
        estimated_tokens: int = 0,
        exclude_models: list[str] | None = None,
    ) -> list[tuple[RoutingResult, str]]:
        """Score all currently-loaded models, optionally filtered by category.

        Returns [(RoutingResult, model_name), ...] sorted by score descending.
        Only considers models that are HOT (loaded in VRAM).
        """
        exclude = set(exclude_models or [])
        results: list[tuple[RoutingResult, str]] = []

        for node in self._registry.get_all_nodes():
            if node.status == NodeStatus.OFFLINE or not node.ollama:
                continue
            if node.memory and node.memory.pressure == MemoryPressure.CRITICAL:
                continue
            if node.capacity and node.capacity.mode in ("paused", "bootstrap"):
                continue

            for loaded_model in node.ollama.models_loaded:
                if loaded_model.name in exclude:
                    continue

                # Category filter
                if category is not None:
                    model_cat = classify_model(loaded_model.name)
                    spec = lookup_model(loaded_model.name)
                    secondary = [c.value for c in spec.secondary_categories] if spec else []
                    if model_cat.value != category and category not in secondary:
                        continue

                # Score using existing 7-signal pipeline
                model = loaded_model.name
                queue_key = f"{node.node_id}:{model}"
                depth = queue_depths.get(queue_key, 0)
                breakdown = {
                    "thermal": self._score_thermal(node, model),
                    "memory_fit": self._score_memory_fit(node, model),
                    "queue_depth": self._score_queue_depth(
                        depth, node=node, queue_depths=queue_depths
                    ),
                    "wait_time": self._score_wait_time(node, model, depth),
                    "role_affinity": self._score_role_affinity(node, model),
                    "availability_trend": self._score_availability_trend(node),
                    "context_fit": self._score_context_fit(node, model, estimated_tokens),
                }

                total = sum(breakdown.values())

                # Quality boost: prefer bigger/better models as compensation
                spec = lookup_model(model)
                if spec:
                    quality_bonus = spec.benchmarks.quality_score * 0.3
                    total += quality_bonus
                    breakdown["quality_bonus"] = quality_bonus

                breakdown["total"] = total
                result = RoutingResult(
                    node_id=node.node_id,
                    queue_key=queue_key,
                    score=total,
                    scores_breakdown=breakdown,
                )
                results.append((result, model))

        results.sort(key=lambda r: r[0].score, reverse=True)
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

            # Capacity-aware elimination: nodes in hard-pause or bootstrap mode
            if node.capacity:
                if node.capacity.mode == "paused":
                    logger.debug(
                        f"Eliminated {node.node_id}: capacity paused "
                        f"(reason={node.capacity.reason})"
                    )
                    continue
                if node.capacity.mode == "bootstrap":
                    logger.debug(f"Eliminated {node.node_id}: in bootstrap observation period")
                    continue
                if node.capacity.availability_score < 0.2:
                    logger.debug(
                        f"Eliminated {node.node_id}: availability score too low "
                        f"({node.capacity.availability_score:.2f})"
                    )
                    continue

            loaded_names = [m.name for m in node.ollama.models_loaded]
            if model not in loaded_names and model not in node.ollama.models_available:
                logger.debug(f"Eliminated {node.node_id}: model '{model}' not available")
                continue

            # Check memory can fit if model needs loading
            if model not in loaded_names:
                model_size = self._model_fit_cost_gb(model, node)
                # Use capacity ceiling if available, otherwise raw available memory
                available = node.memory.available_gb if node.memory else 0
                if node.capacity and node.capacity.ceiling_gb > 0:
                    available = min(available, node.capacity.ceiling_gb)
                if available < model_size:
                    logger.debug(
                        f"Eliminated {node.node_id}: insufficient memory "
                        f"({available:.1f}GB avail/ceiling < {model_size:.1f}GB needed)"
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
        """Signal 2: How comfortably does the model fit in available memory?

        Uses the capacity ceiling when available instead of raw available memory,
        so nodes with adaptive capacity limits are scored correctly.
        """
        loaded_names = [m.name for m in node.ollama.models_loaded]
        if model in loaded_names:
            return self._s.score_memory_fit_max

        model_size = self._model_fit_cost_gb(model, node)
        if model_size <= 0 or not node.memory:
            return 0.0

        # Use capacity ceiling if the node has adaptive capacity enabled
        available = node.memory.available_gb
        if node.capacity and node.capacity.ceiling_gb > 0:
            available = min(available, node.capacity.ceiling_gb)

        fit_ratio = available / model_size
        if fit_ratio > 2.0:
            return 20.0
        elif fit_ratio > 1.5:
            return 15.0
        elif fit_ratio > 1.2:
            return 8.0
        elif fit_ratio >= 1.0:
            return 3.0
        return 0.0

    def _score_queue_depth(
        self,
        depth: int,
        node: NodeState | None = None,
        queue_depths: dict[str, int] | None = None,
    ) -> float:
        """Signal 3: Penalty for busy queues.

        When ``settings.queue_penalty_bandwidth_normalize`` is enabled and the
        node has known bandwidth, the penalty is scaled down by how much
        faster this node is than the fleet median.  A queue of 4 on an 800
        GB/s Mac Studio (when fleet median is 200 GB/s) is treated like a
        queue of 1 for penalty purposes — so routing doesn't prematurely
        flip away from a fast node that's only superficially busy.
        """
        if depth == 0:
            return 0.0

        normalize = (
            node is not None
            and getattr(self._s, "queue_penalty_bandwidth_normalize", False)
            and node.hardware.memory_bandwidth_gbps > 0
        )
        if normalize:
            median_bw = self._fleet_median_bandwidth()
            if median_bw > 0:
                relative = node.hardware.memory_bandwidth_gbps / median_bw
                relative = max(0.25, min(4.0, relative))  # clamp to sane range
                effective_depth = depth / relative
            else:
                effective_depth = float(depth)
        else:
            effective_depth = float(depth)

        penalty = min(
            self._s.score_queue_depth_max_penalty,
            effective_depth * self._s.score_queue_depth_penalty_per,
        )
        return -penalty

    def _fleet_median_bandwidth(self) -> float:
        """Return the median known bandwidth across online nodes, or 0.

        Used by Signal 3 to set the "baseline" capacity for normalization.
        Cached briefly via the registry's get_all_nodes() — fleet size is
        small so recomputing per scoring pass is fine.
        """
        bws = [
            n.hardware.memory_bandwidth_gbps
            for n in self._registry.get_all_nodes()
            if n.hardware.memory_bandwidth_gbps > 0 and n.status != NodeStatus.OFFLINE
        ]
        if not bws:
            return 0.0
        bws.sort()
        mid = len(bws) // 2
        if len(bws) % 2 == 1:
            return bws[mid]
        return (bws[mid - 1] + bws[mid]) / 2.0

    def _score_wait_time(self, node: NodeState, model: str, depth: int) -> float:
        """Signal 4: Penalty based on estimated wait time.

        Primary source is historical p75 latency from the latency store.
        When that's unavailable (cold fleet, new model), falls back to a
        bandwidth-derived throughput estimate so the first N requests to a
        fresh deployment still route sensibly.
        """
        if depth == 0 or self._latency_store is None:
            return 0.0

        p75_ms = self._latency_store.get_cached_percentile(node.node_id, model)
        if p75_ms is None:
            # Cold-start fallback: derive expected tokens/sec from node's
            # memory bandwidth and model size.  Empirically, prompt-eval
            # tokens/sec on Apple Silicon scales roughly as bandwidth / model
            # size, clamped to a sensible floor.
            model_size = self._estimate_model_size(model, node)
            bw = node.hardware.memory_bandwidth_gbps
            if bw > 0:
                tokens_per_sec = max(10.0, bw * 1.2 / max(1.0, model_size / 10.0))
            else:
                tokens_per_sec = max(1.0, 100.0 / max(1.0, model_size))
            p75_ms = (100.0 / tokens_per_sec) * 1000

        est_wait_s = (depth * p75_ms) / 1000.0
        penalty = min(self._s.score_wait_time_max_penalty, est_wait_s / 10.0)
        return -penalty

    # Sized to outweigh ordinary jitter between comparable nodes without
    # overpowering a real health signal.  Thermal alone contributes up to 50, so
    # a hot or memory-pressured node still loses to a cool one — which is
    # correct: a warm prefix cache is worth a lot, but not worth routing into a
    # node that is about to throttle.
    SESSION_AFFINITY_BONUS = 20.0

    def _score_session_affinity(self, node: NodeState, session_key: str) -> float:
        """Signal 8: keep a conversation on the node holding its warm prefix.

        llama.cpp skips already-processed prompt tokens, so a returning turn can
        cost a few hundred tokens of prefill instead of tens of thousands.  Since
        prefill is the only thing that stalls other requests' decode, this both
        speeds up the session and removes the interference it would otherwise
        inflict on everything else on that node.

        Zero when there is no session, no pin, or the pin has expired — so a
        first turn, a stateless call, and a stale conversation all score exactly
        as they did before this signal existed.
        """
        if not session_key or self._sessions is None:
            return 0.0
        preferred = self._sessions.preferred_node(session_key)
        if preferred and preferred == node.node_id:
            return self.SESSION_AFFINITY_BONUS
        return 0.0

    def _score_role_affinity(self, node: NodeState, model: str) -> float:
        """Signal 5: Match model size to node capability.

        When ``settings.bandwidth_aware_scoring`` is enabled and the node has
        known bandwidth, we reward it proportional to bandwidth (the true
        prompt-eval bottleneck) instead of memory-size tiers.  Nodes without
        known bandwidth fall back to the original memory-tier logic so older
        agents and unrecognized chips keep working unchanged.

        For big models (≥ score_role_large_threshold_gb):
            Fast nodes get the full bonus (up to +25 at 800 GB/s).
        For small models (< score_role_small_threshold_gb):
            Slower, smaller nodes are preferred (keeps the big/fast node
            free for heavy work).
        """
        model_size = self._estimate_model_size(model, node)
        node_mem = node.hardware.memory_total_gb
        bw = node.hardware.memory_bandwidth_gbps

        is_large = model_size > self._s.score_role_large_threshold_gb
        is_small = model_size < self._s.score_role_small_threshold_gb

        if getattr(self._s, "bandwidth_aware_scoring", False) and bw > 0:
            # Scale continuously across the bandwidth range:
            #   100 GB/s  →  ~7.5
            #   200 GB/s  →  ~10
            #   400 GB/s  →  ~15
            #   800 GB/s  →  ~25 (clamped)
            bw_bonus = min(25.0, 5.0 + bw / 40.0)
            if is_large:
                return bw_bonus
            if is_small:
                # Small models — invert preference so small/slow nodes
                # score well.  Keeps the fast machine available for big
                # work.  Floor at +3 so a small model on a fast node
                # isn't completely unviable.
                return max(3.0, 18.0 - bw_bonus * 0.6)
            # Mid-size: partial bandwidth credit
            return bw_bonus * 0.6

        # Fallback path: original memory-tier scoring (unchanged behaviour)
        if is_large:
            if node_mem >= 128:
                return 15.0
            elif node_mem >= 32:
                return 5.0
            return 0.0
        elif is_small:
            if node_mem <= 32:
                return 15.0
            elif node_mem <= 128:
                return 8.0
            return 3.0
        return 5.0

    def _score_availability_trend(self, node: NodeState) -> float:
        """Signal 6: Availability trend for nodes with adaptive capacity.

        Only applies to nodes that have capacity data (work MacBooks).
        A rising availability score means the machine is freeing up — safe
        to route new work. A falling score means the owner is actively
        starting work — avoid adding long-running requests.

        Returns 0 for nodes without capacity data (e.g., always-on servers).
        """
        if not node.capacity:
            return 0.0

        score = node.capacity.availability_score

        # Higher availability = more bonus points (max +10)
        # This naturally prioritizes highly-available nodes
        return min(
            self._s.score_availability_trend_max, score * self._s.score_availability_trend_max
        )

    def _score_context_fit(self, node: NodeState, model: str, estimated_tokens: int) -> float:
        """Signal 7: Prefer nodes with more context window headroom.

        Compares estimated input tokens against the loaded model's context_length.
        Nodes with larger context windows score higher for token-heavy requests.
        Returns 0 if the model isn't loaded or context_length is unknown.
        """
        if estimated_tokens <= 0:
            return 0.0

        # Find this model's context_length on this node
        ctx_length = 0
        for m in node.ollama.models_loaded:
            if m.name == model and m.context_length > 0:
                ctx_length = m.context_length
                break

        if ctx_length == 0:
            return 0.0  # Unknown context — can't score

        ratio = ctx_length / estimated_tokens
        max_score = self._s.score_context_fit_max

        if ratio < 1.0:
            # Tokens may exceed context window — penalize
            return -max_score
        elif ratio < 1.5:
            # Tight fit — minimal bonus
            return max_score * 0.2
        elif ratio < 3.0:
            # Comfortable headroom
            return max_score * 0.5
        elif ratio < 8.0:
            # Plenty of room
            return max_score * 0.8
        else:
            # Massive headroom
            return max_score

    # Approximate token cost per image for vision models.
    # Conservative estimate for 1080p images — actual cost varies by model
    # but this is good enough for routing decisions.
    IMAGE_TOKENS_PER_IMAGE = 150

    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """Rough token estimate from message content (~4 chars per token).

        Good enough for routing decisions — not meant for billing accuracy.
        Accounts for image tokens in multimodal messages (both OpenAI and
        Ollama formats).
        """
        total_chars = 0
        image_count = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # Multi-modal messages (OpenAI format with text + image_url parts)
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            total_chars += len(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            image_count += 1
            # Ollama format: images field is a list of base64 strings
            images = msg.get("images")
            if isinstance(images, list):
                image_count += len(images)
            # Count role + overhead (~4 tokens per message for formatting)
            total_chars += 16
        text_tokens = total_chars // 4
        return max(1, text_tokens + image_count * ScoringEngine.IMAGE_TOKENS_PER_IMAGE)

    def _model_fit_cost_gb(self, model: str, node: NodeState) -> float:
        """What loading ``model`` on ``node`` costs in RAM, for fit decisions.

        Weights alone are the wrong number: a model costs ``weights + KV
        cache``, and the KV cache scales with context. qwen3-coder:30b is 18.6GB
        of weights and **122.9GB** resident at its default 262K context — a 6.6x
        difference the routing gates were blind to (see
        docs/issues/model-sizing-ignores-kv-cache.md).

        Uses the measured cost when the fleet has actually observed this model,
        and falls back to **plain weights** — the pre-existing behaviour — when
        it hasn't. Deliberately *not* ``weights * 1.2`` here: inflating an
        unmeasured guess would eliminate nodes that legitimately fit today
        (a 70B on a 64GB box) to defend against a case we have no evidence for.
        Evidence tightens this gate; guesswork doesn't.
        """
        from fleet_manager.server.model_preloader import (
            _expected_num_ctx,
            measured_resident_gb,
        )

        weights = self._estimate_model_size(model, node)
        measured = measured_resident_gb(
            model, node, _expected_num_ctx(model, self._s), weights_gb=weights
        )
        return measured if measured is not None else weights

    def _estimate_model_size(self, model: str, node: NodeState) -> float:
        """Model size in GB — real data when a node reports it, else a guess.

        Delegates to the **shared** estimator so there is one definition of "how
        big is this model". There used to be two — this one and the preloader's —
        with different name heuristics and different wrong answers. When the
        preloader's was taught to read Ollama's real ``/api/tags`` sizes, this
        copy kept guessing from the name, and it guessed **10 GB for
        `qwen3-coder:480b`** (really 290 GB, ~348 GB resident) and 10 GB for
        `llama4:maverick` (244 GB): its heuristic knew `671b` and `405b` but not
        `480b`, and anything unrecognised fell through to a 10 GB default.

        This is the path a **client request** takes, so that 29× under-estimate
        was load-bearing: a request for the 480b was scored as "10 GB, plenty of
        room", Ollama loaded 348 GB, and the box kernel-panicked on
        ``watchdog timeout`` — twice on 2026-07-17, from two different callers.
        Duplicated logic doesn't drift symmetrically; it drifts until one copy
        is dangerous.
        """
        from fleet_manager.server.model_preloader import (
            _UNKNOWN_MODEL_SIZE_GB,
        )
        from fleet_manager.server.model_preloader import (
            _estimate_model_size as shared_estimate,
        )

        # This node's own report — on-disk size from /api/tags, or resident
        # size if it's already loaded — is ground truth.
        size = shared_estimate(model, node)
        if size != _UNKNOWN_MODEL_SIZE_GB:
            return size

        # Unknown here; a peer may have it resident and know the real number.
        for other in self._registry.get_all_nodes():
            if other is node or not getattr(other, "ollama", None):
                continue
            peer = shared_estimate(model, other)
            if peer != _UNKNOWN_MODEL_SIZE_GB:
                return peer

        # Genuinely unknown fleet-wide — shared_estimate already logged why and
        # returns a deliberately pessimistic value (fail toward "don't load").
        return size
