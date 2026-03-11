"""Model Recommender — analyzes fleet hardware and usage to suggest optimal model mixes.

Given a fleet of nodes with known hardware and usage history, recommends which
models each node should run to maximize coverage of use cases, quality, and
fleet utilization. Considers:
  - Available memory per node (with OS overhead)
  - Last 24h request patterns (which categories are in demand)
  - Models already downloaded on each node
  - Cross-fleet distribution (avoid redundant large models)
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel, Field

from fleet_manager.models.node import NodeState
from fleet_manager.server.model_knowledge import (
    ModelCategory,
    ModelSpec,
    best_for_category,
    classify_model,
    lookup_model,
    models_fitting_ram,
)

logger = logging.getLogger(__name__)

# Reserve this much RAM for OS + apps on each node
OS_OVERHEAD_GB = 6.0


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class Priority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ModelRecommendation(BaseModel):
    """A single model recommendation for a node."""

    model: str  # Ollama model name
    display_name: str
    category: str
    ram_gb: float
    quality_score: float
    reason: str
    priority: Priority
    already_available: bool = False  # Already downloaded on this node


class NodePlan(BaseModel):
    """Recommended model lineup for one node."""

    node_id: str
    total_ram_gb: float
    usable_ram_gb: float
    current_models: list[str] = Field(default_factory=list)
    recommendations: list[ModelRecommendation] = Field(default_factory=list)
    total_recommended_ram_gb: float = 0.0
    ram_headroom_gb: float = 0.0


class UsageInsight(BaseModel):
    """Summary of how the fleet is being used."""

    total_requests_24h: int = 0
    category_breakdown: dict[str, int] = Field(default_factory=dict)
    top_models: list[dict] = Field(default_factory=list)
    category_coverage: dict[str, bool] = Field(default_factory=dict)


class FleetRecommendation(BaseModel):
    """Complete recommendation for the entire fleet."""

    nodes: list[NodePlan] = Field(default_factory=list)
    usage: UsageInsight = Field(default_factory=UsageInsight)
    fleet_summary: str = ""
    uncovered_categories: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ModelRecommender:
    """Analyzes fleet state and usage patterns to recommend model deployments."""

    def analyze(
        self,
        nodes: list[NodeState],
        usage_data: list[dict] | None = None,
    ) -> FleetRecommendation:
        """Generate fleet-wide model recommendations.

        Args:
            nodes: Current fleet node states from the registry.
            usage_data: Output of trace_store.get_usage_by_node_model_day(days=1).
        """
        online_nodes = [n for n in nodes if n.status != "offline"]
        if not online_nodes:
            return FleetRecommendation(fleet_summary="No online nodes in the fleet.")

        # 1. Analyze usage patterns
        usage = self._analyze_usage(usage_data or [])

        # 2. Determine priority categories from usage (or defaults)
        priority_cats = self._rank_categories(usage)

        # 3. Build per-node recommendations
        node_plans: list[NodePlan] = []
        fleet_assigned: dict[str, list[str]] = {}  # model -> [node_ids]

        # Sort nodes by RAM descending — assign best models to biggest nodes first
        sorted_nodes = sorted(
            online_nodes,
            key=lambda n: n.memory.total_gb if n.memory else 0,
            reverse=True,
        )

        for node in sorted_nodes:
            plan = self._plan_node(node, priority_cats, fleet_assigned, usage)
            node_plans.append(plan)
            for rec in plan.recommendations:
                fleet_assigned.setdefault(rec.model, []).append(node.node_id)

        # 4. Check fleet-wide coverage
        covered_cats = set()
        for plan in node_plans:
            for rec in plan.recommendations:
                covered_cats.add(rec.category)

        all_cats = {c.value for c in ModelCategory}
        uncovered = sorted(all_cats - covered_cats)

        usage.category_coverage = {c: c in covered_cats for c in all_cats}

        # 5. Summary
        total_ram = sum(p.total_ram_gb for p in node_plans)
        total_used = sum(p.total_recommended_ram_gb for p in node_plans)
        n_models = sum(len(p.recommendations) for p in node_plans)
        summary = (
            f"{len(node_plans)} node(s), {total_ram:.0f}GB total RAM, "
            f"{n_models} model(s) recommended using {total_used:.0f}GB"
        )
        if uncovered:
            summary += f". Uncovered: {', '.join(uncovered)}"

        return FleetRecommendation(
            nodes=node_plans,
            usage=usage,
            fleet_summary=summary,
            uncovered_categories=uncovered,
        )

    def _analyze_usage(self, usage_data: list[dict]) -> UsageInsight:
        """Analyze last 24h of request traces to understand usage patterns."""
        if not usage_data:
            return UsageInsight()

        total = 0
        by_category: dict[str, int] = {}
        by_model: dict[str, int] = {}

        for row in usage_data:
            count = row.get("request_count", 0)
            model = row.get("model", "unknown")
            total += count
            by_model[model] = by_model.get(model, 0) + count

            cat = classify_model(model).value
            by_category[cat] = by_category.get(cat, 0) + count

        # Top models by request count
        top_models = sorted(
            [
                {"model": m, "requests": c, "category": classify_model(m).value}
                for m, c in by_model.items()
            ],
            key=lambda x: x["requests"],
            reverse=True,
        )[:10]

        return UsageInsight(
            total_requests_24h=total,
            category_breakdown=by_category,
            top_models=top_models,
        )

    def _rank_categories(self, usage: UsageInsight) -> list[ModelCategory]:
        """Rank categories by demand. Falls back to sensible defaults."""
        if usage.category_breakdown:
            # Sort by request count, most demanded first
            ranked = sorted(
                usage.category_breakdown.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            cats = [ModelCategory(cat) for cat, _ in ranked]
            # Add any missing categories at the end
            all_cats = list(ModelCategory)
            for c in all_cats:
                if c not in cats:
                    cats.append(c)
            return cats

        # Default priority when no usage data
        return [
            ModelCategory.GENERAL,
            ModelCategory.CODING,
            ModelCategory.REASONING,
            ModelCategory.CREATIVE,
            ModelCategory.FAST_CHAT,
        ]

    def _plan_node(
        self,
        node: NodeState,
        priority_cats: list[ModelCategory],
        fleet_assigned: dict[str, list[str]],
        usage: UsageInsight,
    ) -> NodePlan:
        """Build model recommendations for a single node."""
        total_ram = node.memory.total_gb if node.memory else 0
        usable_ram = max(0, total_ram - OS_OVERHEAD_GB)

        # What's already on this node?
        current_models: list[str] = []
        if node.ollama:
            current_models = list(node.ollama.models_available)

        recommendations: list[ModelRecommendation] = []
        ram_used = 0.0
        assigned_cats: set[str] = set()
        assigned_families: set[str] = set()

        # Cap per-model RAM to leave room for variety (50% of usable)
        max_single_model_ram = usable_ram * 0.5

        # Build a lookup for loaded model sizes from node state
        loaded_model_sizes: dict[str, float] = {}
        if node.ollama:
            for lm in node.ollama.models_loaded:
                loaded_model_sizes[lm.name] = lm.size_gb

        # Phase 0: Recommend keeping actively-used models from the last 24h
        for top in usage.top_models:
            remaining = usable_ram - ram_used
            if remaining < 2.0:
                break
            model_name = top["model"]
            if not self._is_available(model_name, current_models):
                continue  # Not on this node

            spec = lookup_model(model_name)
            cat = classify_model(model_name)

            if spec:
                if spec.family in assigned_families:
                    continue
                model_ram = spec.ram_gb
                display = spec.display_name
                quality = round(spec.benchmarks.quality_score, 1)
                notes = spec.notes
                family = spec.family
            else:
                # Unknown model — use loaded size from node state if available
                model_ram = loaded_model_sizes.get(model_name, 0)
                if model_ram <= 0:
                    continue  # Can't determine size
                display = model_name
                quality = 0.0
                notes = "Custom model"
                family = model_name.split(":")[0] if ":" in model_name else model_name
                if family in assigned_families:
                    continue

            if model_ram > remaining or model_ram > max_single_model_ram:
                continue

            recommendations.append(
                ModelRecommendation(
                    model=model_name,
                    display_name=display,
                    category=cat.value,
                    ram_gb=round(model_ram, 1),
                    quality_score=quality,
                    reason=(
                        f"Currently your most-used {cat.value} model "
                        f"({top['requests']} requests in 24h). {notes}"
                    ),
                    priority=Priority.HIGH,
                    already_available=True,
                )
            )
            ram_used += model_ram
            assigned_cats.add(cat.value)
            assigned_families.add(family)

        # Phase 1: For each priority category, pick the best model that fits
        for cat in priority_cats:
            if cat.value in assigned_cats:
                continue  # Already covered by an actively-used model

            remaining = usable_ram - ram_used
            if remaining < 2.0:
                break

            # Cap individual model size
            cat_max = min(remaining, max_single_model_ram)

            # Find best model for this category that fits
            best = best_for_category(cat, cat_max)
            if not best:
                continue

            # Skip if this family is already assigned on this node
            if best.family in assigned_families:
                # Try an alternative from a different family
                alt = self._find_alternative(best, cat_max, fleet_assigned, assigned_families)
                if alt:
                    best = alt
                else:
                    continue

            # Deprioritize if this exact model is already on another node
            already_on_fleet = best.ollama_name in fleet_assigned
            if already_on_fleet and len(fleet_assigned[best.ollama_name]) >= 2:
                alt = self._find_alternative(best, cat_max, fleet_assigned, assigned_families)
                if alt:
                    best = alt

            already_available = self._is_available(best.ollama_name, current_models)

            # Determine priority
            priority = Priority.MEDIUM
            if cat == priority_cats[0] or usage.category_breakdown.get(cat.value, 0) > 0:
                priority = Priority.HIGH

            reason = self._build_reason(best, cat, node, usage, already_on_fleet)

            recommendations.append(
                ModelRecommendation(
                    model=best.ollama_name,
                    display_name=best.display_name,
                    category=cat.value,
                    ram_gb=best.ram_gb,
                    quality_score=round(best.benchmarks.quality_score, 1),
                    reason=reason,
                    priority=priority,
                    already_available=already_available,
                )
            )
            ram_used += best.ram_gb
            assigned_cats.add(cat.value)
            assigned_families.add(best.family)

        # Phase 2: If there's significant RAM left, add a fast-chat model
        remaining = usable_ram - ram_used
        if remaining >= 2.0 and ModelCategory.FAST_CHAT.value not in assigned_cats:
            fast = best_for_category(ModelCategory.FAST_CHAT, remaining)
            if fast and fast.family not in assigned_families:
                already_available = self._is_available(fast.ollama_name, current_models)
                recommendations.append(
                    ModelRecommendation(
                        model=fast.ollama_name,
                        display_name=fast.display_name,
                        category=ModelCategory.FAST_CHAT.value,
                        ram_gb=fast.ram_gb,
                        quality_score=round(fast.benchmarks.quality_score, 1),
                        reason="Fast response model for quick queries and autocomplete",
                        priority=Priority.LOW,
                        already_available=already_available,
                    )
                )
                ram_used += fast.ram_gb

        total_rec_ram = round(sum(r.ram_gb for r in recommendations), 1)

        return NodePlan(
            node_id=node.node_id,
            total_ram_gb=round(total_ram, 1),
            usable_ram_gb=round(usable_ram, 1),
            current_models=current_models,
            recommendations=recommendations,
            total_recommended_ram_gb=total_rec_ram,
            ram_headroom_gb=round(usable_ram - total_rec_ram, 1),
        )

    @staticmethod
    def _is_available(model_name: str, current_models: list[str]) -> bool:
        """Check if a model (or close variant) is already downloaded."""
        if model_name in current_models:
            return True
        # Check base name match (e.g. "qwen3:8b" matches "qwen3:8b-q4_K_M")
        base = model_name.split(":")[0] if ":" in model_name else model_name
        for m in current_models:
            m_base = m.split(":")[0] if ":" in m else m
            if base == m_base:
                return True
        return False

    def _find_alternative(
        self,
        original: ModelSpec,
        max_ram: float,
        fleet_assigned: dict[str, list[str]],
        assigned_families: set[str],
    ) -> ModelSpec | None:
        """Find an alternative model in the same category not already over-assigned."""
        candidates = [
            m
            for m in models_fitting_ram(max_ram)
            if (m.category == original.category or original.category in m.secondary_categories)
            and m.ollama_name != original.ollama_name
            and m.family not in assigned_families
            and len(fleet_assigned.get(m.ollama_name, [])) < 2
        ]
        return candidates[0] if candidates else None

    def _build_reason(
        self,
        spec: ModelSpec,
        category: ModelCategory,
        node: NodeState,
        usage: UsageInsight,
        already_on_fleet: bool,
    ) -> str:
        """Build a human-readable reason for the recommendation."""
        parts = []

        # Category demand
        demand = usage.category_breakdown.get(category.value, 0)
        if demand > 0:
            parts.append(f"{demand} {category.value} request(s) in the last 24h")
        else:
            parts.append(f"Covers {category.value} workloads")

        # Quality highlight
        if spec.benchmarks.mmlu and spec.benchmarks.mmlu >= 80:
            parts.append(f"MMLU {spec.benchmarks.mmlu:.0f}%")
        if spec.benchmarks.humaneval and spec.benchmarks.humaneval >= 80:
            parts.append(f"HumanEval {spec.benchmarks.humaneval:.0f}%")

        # MoE efficiency
        if spec.is_moe:
            parts.append(
                f"MoE: {spec.params_b:.0f}B params, only {spec.active_params_b:.0f}B active"
            )

        # Notes
        if spec.notes:
            parts.append(spec.notes)

        return ". ".join(parts)
