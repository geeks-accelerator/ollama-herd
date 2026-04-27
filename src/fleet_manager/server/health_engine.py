"""Fleet Health Engine — analyzes registry state and traces to surface recommendations."""

from __future__ import annotations

import logging
import time
from enum import StrEnum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Recommendation(BaseModel):
    """A single actionable health recommendation."""

    check_id: str  # e.g. "model_thrashing", "underutilized_memory"
    severity: Severity
    title: str  # Short: "Model Thrashing Detected"
    description: str  # What's happening
    fix: str  # Actionable fix instruction
    node_id: str | None = None
    data: dict = Field(default_factory=dict)


class FleetVitals(BaseModel):
    """Top-level fleet health summary stats."""

    nodes_total: int = 0
    nodes_online: int = 0
    nodes_degraded: int = 0
    nodes_offline: int = 0
    overall_error_rate_pct: float = 0.0
    cold_loads_24h: int = 0
    avg_ttft_ms: float | None = None
    total_requests_24h: int = 0
    total_retries_24h: int = 0
    client_disconnects_24h: int = 0
    incomplete_streams_24h: int = 0
    image_generations_24h: int = 0
    transcriptions_24h: int = 0
    health_score: int = 100


class HealthReport(BaseModel):
    """Complete health analysis result."""

    vitals: FleetVitals
    recommendations: list[Recommendation] = Field(default_factory=list)
    checked_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class HealthEngine:
    """Analyzes fleet state and produces actionable health recommendations."""

    COLD_LOAD_THRESHOLD_MS = 40_000  # TTFT > 40s = cold load
    ERROR_RATE_THRESHOLD_PCT = 5.0
    MEMORY_UNDERUTIL_PCT = 50.0
    RETRY_RATE_THRESHOLD = 0.3  # avg retries per request
    RECENT_WINDOW_S = 3600  # 1 hour — used to detect if issues are still active

    async def analyze(self, registry, trace_store) -> HealthReport:
        """Run all health checks and return a complete report."""
        recommendations: list[Recommendation] = []
        nodes = registry.get_all_nodes()

        # Build vitals from registry
        vitals = self._compute_vitals(nodes)

        # Registry-based checks (synchronous, in-memory)
        recommendations.extend(self._check_degraded_offline_nodes(nodes))
        recommendations.extend(self._check_memory_pressure(nodes))
        recommendations.extend(self._check_underutilized_memory(nodes))
        recommendations.extend(self._check_vram_fallbacks())
        recommendations.extend(self._check_version_mismatch(nodes))
        recommendations.extend(self._check_context_protection())
        recommendations.extend(self._check_zombie_reaper())
        recommendations.extend(self._check_kv_cache_bloat(nodes))
        recommendations.extend(self._check_image_generation(nodes))
        recommendations.extend(self._check_transcription(nodes))
        recommendations.extend(self._check_connection_failures(nodes))
        recommendations.extend(self._check_mlx_backend(nodes))
        recommendations.extend(self._check_vision_backend_missing(nodes))
        recommendations.extend(self._check_mapped_models_hot(nodes))

        # Trace-based checks (async, queries SQLite)
        if trace_store:
            cold_loads = await trace_store.get_cold_loads_24h()
            error_rates = await trace_store.get_error_rates_24h()
            retry_stats = await trace_store.get_retry_stats_24h()
            overall_24h = await trace_store.get_overall_stats_24h()

            # Recent window — used to detect if issues are still active
            recent_cold = await trace_store.get_cold_loads_24h(
                lookback_s=self.RECENT_WINDOW_S
            )
            recent_errors = await trace_store.get_error_rates_24h(
                lookback_s=self.RECENT_WINDOW_S
            )

            vitals.cold_loads_24h = cold_loads["total_count"]
            vitals.total_requests_24h = overall_24h["total_requests"]
            vitals.total_retries_24h = overall_24h["total_retries"]
            vitals.overall_error_rate_pct = overall_24h["error_rate_pct"]
            vitals.avg_ttft_ms = overall_24h["avg_ttft_ms"]

            stream_reliability = await trace_store.get_stream_reliability_24h()
            recent_reliability = await trace_store.get_stream_reliability_24h(
                lookback_s=self.RECENT_WINDOW_S
            )
            vitals.client_disconnects_24h = stream_reliability["client_disconnected"]
            vitals.incomplete_streams_24h = stream_reliability["incomplete"]

            recommendations.extend(
                self._check_stream_reliability(stream_reliability, recent_reliability)
            )

            model_timeouts = await trace_store.get_model_timeouts_24h()
            recent_timeouts = await trace_store.get_model_timeouts_24h(
                lookback_s=self.RECENT_WINDOW_S
            )

            recommendations.extend(
                self._check_model_thrashing(
                    cold_loads["by_node"], recent_cold["by_node"], nodes
                )
            )
            recommendations.extend(
                self._check_model_load_timeouts(
                    model_timeouts, recent_timeouts, nodes
                )
            )
            recommendations.extend(
                self._check_error_rates(error_rates, recent_errors)
            )
            recommendations.extend(self._check_retry_rates(retry_stats))

            # Context waste analysis
            prompt_stats = await trace_store.get_prompt_token_stats(days=7)
            recommendations.extend(
                self._check_context_waste(prompt_stats, nodes)
            )

            # Priority model check
            priorities = await trace_store.get_model_priority_scores()
            recommendations.extend(
                self._check_priority_models(priorities, nodes)
            )

        # Suppress misleading "underutilized memory" when there's active
        # model thrashing or timeouts on the same node — telling users to
        # "load more models" while models are timing out is contradictory.
        nodes_with_load_issues: set[str] = set()
        for r in recommendations:
            if (
                r.check_id in ("model_thrashing", "model_load_timeout")
                and r.severity != Severity.INFO  # don't suppress for resolved issues
            ):
                if r.node_id:
                    nodes_with_load_issues.add(r.node_id)
                # model_load_timeout spans nodes — check data field too
                for n in r.data.get("nodes", []):
                    nodes_with_load_issues.add(n)
        if nodes_with_load_issues:
            recommendations = [
                r
                for r in recommendations
                if not (
                    r.check_id == "underutilized_memory"
                    and r.node_id in nodes_with_load_issues
                )
            ]

        # Populate multimodal vitals
        try:
            from fleet_manager.server.routes.image_compat import get_image_gen_events
            vitals.image_generations_24h = len(
                [e for e in get_image_gen_events(24) if e["status"] == "completed"]
            )
        except Exception:
            pass
        try:
            from fleet_manager.server.routes.transcription_compat import (
                get_transcription_events,
            )
            vitals.transcriptions_24h = len(
                [e for e in get_transcription_events(24) if e["status"] == "completed"]
            )
        except Exception:
            pass

        # Compute health score
        vitals.health_score = self._compute_health_score(recommendations)

        # Sort: critical first, then warning, then info
        severity_order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
        recommendations.sort(key=lambda r: severity_order[r.severity])

        return HealthReport(vitals=vitals, recommendations=recommendations)

    # ------------------------------------------------------------------
    # Vitals
    # ------------------------------------------------------------------

    def _compute_vitals(self, nodes) -> FleetVitals:
        online = sum(1 for n in nodes if n.status.value == "online")
        degraded = sum(1 for n in nodes if n.status.value == "degraded")
        offline = sum(1 for n in nodes if n.status.value == "offline")
        return FleetVitals(
            nodes_total=len(nodes),
            nodes_online=online,
            nodes_degraded=degraded,
            nodes_offline=offline,
        )

    # ------------------------------------------------------------------
    # Registry-based checks
    # ------------------------------------------------------------------

    def _check_degraded_offline_nodes(self, nodes) -> list[Recommendation]:
        recs = []
        now = time.time()
        for node in nodes:
            if node.status.value == "offline":
                ago = now - node.last_heartbeat
                recs.append(
                    Recommendation(
                        check_id="node_offline",
                        severity=Severity.CRITICAL,
                        title=f"Node {node.node_id} is offline",
                        description=f"Last heartbeat was {self._fmt_duration(ago)} ago.",
                        fix=(
                            f"Check that herd-node is running on {node.node_id} "
                            f"and the machine is reachable."
                        ),
                        node_id=node.node_id,
                        data={"last_heartbeat_ago_s": round(ago)},
                    )
                )
            elif node.status.value == "degraded":
                ago = now - node.last_heartbeat
                recs.append(
                    Recommendation(
                        check_id="node_degraded",
                        severity=Severity.WARNING,
                        title=f"Node {node.node_id} is degraded",
                        description=(
                            f"Missed heartbeats. Last seen {self._fmt_duration(ago)} ago."
                        ),
                        fix=f"Check network connectivity to {node.node_id}.",
                        node_id=node.node_id,
                        data={"missed_heartbeats": node.missed_heartbeats},
                    )
                )
        return recs

    def _check_memory_pressure(self, nodes) -> list[Recommendation]:
        recs = []
        for node in nodes:
            if not node.memory or node.memory.pressure.value == "normal":
                continue
            pressure = node.memory.pressure.value
            severity = Severity.CRITICAL if pressure == "critical" else Severity.WARNING
            recs.append(
                Recommendation(
                    check_id="memory_pressure",
                    severity=severity,
                    title=f"Memory pressure on {node.node_id}",
                    description=(
                        f"Node is under {pressure} memory pressure "
                        f"({node.memory.used_gb:.1f}/{node.memory.total_gb:.1f} GB used)."
                    ),
                    fix=f"Reduce loaded models or close other applications on {node.node_id}.",
                    node_id=node.node_id,
                    data={
                        "used_gb": round(node.memory.used_gb, 1),
                        "total_gb": round(node.memory.total_gb, 1),
                        "pressure": pressure,
                    },
                )
            )
        return recs

    def _check_underutilized_memory(self, nodes) -> list[Recommendation]:
        recs = []
        for node in nodes:
            if not node.memory or not node.ollama:
                continue
            if node.status.value != "online":
                continue
            avail_pct = (node.memory.available_gb / node.memory.total_gb) * 100
            models_loaded = len(node.ollama.models_loaded)
            if avail_pct > self.MEMORY_UNDERUTIL_PCT and models_loaded <= 2:
                recs.append(
                    Recommendation(
                        check_id="underutilized_memory",
                        severity=Severity.INFO,
                        title=f"Underutilized memory on {node.node_id}",
                        description=(
                            f"Node has {node.memory.available_gb:.1f} GB free "
                            f"({avail_pct:.0f}%) but only {models_loaded} model(s) loaded."
                        ),
                        fix=(
                            f"Node {node.node_id} could keep more models hot. "
                            f"Set OLLAMA_MAX_LOADED_MODELS=-1 to auto-fill available memory."
                        ),
                        node_id=node.node_id,
                        data={
                            "available_gb": round(node.memory.available_gb, 1),
                            "available_pct": round(avail_pct, 1),
                            "models_loaded": models_loaded,
                        },
                    )
                )
        return recs

    def _check_kv_cache_bloat(self, nodes) -> list[Recommendation]:
        """Detect OLLAMA_NUM_PARALLEL being too high, causing KV cache bloat.

        When OLLAMA_NUM_PARALLEL is high (e.g., 16), each parallel slot
        pre-allocates KV cache for the full context window. A single model
        can consume 100+ GB of KV cache on top of its weights, preventing
        other models from loading. This check compares VRAM used by loaded
        models against expected weight sizes to detect the bloat.
        """
        recs = []
        for node in nodes:
            if not node.ollama or not node.memory:
                continue
            if node.status.value != "online":
                continue

            # Sum up VRAM used by loaded models
            total_vram_gb = sum(m.size_gb for m in node.ollama.models_loaded)
            if total_vram_gb == 0:
                continue

            # Estimate expected weight sizes from parameter counts
            # Rough heuristic: parameter_size like "116.8B" at Q4 ≈ 0.5 bytes/param
            total_expected_gb = 0.0
            bloated_models = []
            for m in node.ollama.models_loaded:
                # Estimate expected size from parameter count
                expected_gb = self._estimate_weight_size(m.parameter_size)
                if expected_gb > 0:
                    overhead_ratio = m.size_gb / expected_gb
                    if overhead_ratio > 1.5:
                        # VRAM is 50%+ more than expected weights = KV cache bloat
                        bloated_models.append({
                            "name": m.name,
                            "vram_gb": round(m.size_gb, 1),
                            "expected_gb": round(expected_gb, 1),
                            "overhead_pct": round((overhead_ratio - 1) * 100),
                            "context_length": m.context_length,
                        })
                    total_expected_gb += expected_gb

            if not bloated_models:
                continue

            # Calculate how much is KV cache vs weights
            kv_cache_gb = total_vram_gb - total_expected_gb
            kv_pct = (kv_cache_gb / total_vram_gb) * 100 if total_vram_gb > 0 else 0

            model_lines = ", ".join(
                f"{m['name']} ({m['vram_gb']}GB VRAM, ~{m['expected_gb']}GB "
                f"weights, {m['overhead_pct']}% overhead, ctx={m['context_length']})"
                for m in bloated_models
            )

            # Severity: WARNING if KV cache > 30% of VRAM, CRITICAL if >50%
            severity = Severity.INFO
            if kv_pct > 50:
                severity = Severity.CRITICAL
            elif kv_pct > 30:
                severity = Severity.WARNING

            recs.append(
                Recommendation(
                    check_id="kv_cache_bloat",
                    severity=severity,
                    title=(
                        f"KV cache bloat on {node.node_id}: "
                        f"~{kv_cache_gb:.0f} GB overhead"
                    ),
                    description=(
                        f"Loaded models use {total_vram_gb:.1f} GB VRAM but only "
                        f"~{total_expected_gb:.0f} GB is model weights. "
                        f"The remaining ~{kv_cache_gb:.0f} GB ({kv_pct:.0f}%) is "
                        f"KV cache from OLLAMA_NUM_PARALLEL being too high. "
                        f"Bloated models: {model_lines}. "
                        f"This prevents other models from loading."
                    ),
                    fix=(
                        f"Set OLLAMA_NUM_PARALLEL=2 on {node.node_id}: "
                        f"`launchctl setenv OLLAMA_NUM_PARALLEL 2` (macOS), "
                        f"`sudo systemctl edit ollama` and add Environment= (Linux), "
                        f"or set system environment variable (Windows), "
                        f"then restart Ollama. "
                        f"This reduces KV cache from ~{kv_cache_gb:.0f} GB to "
                        f"~{kv_cache_gb / 8:.0f} GB, freeing memory for more models."
                    ),
                    node_id=node.node_id,
                    data={
                        "total_vram_gb": round(total_vram_gb, 1),
                        "estimated_weights_gb": round(total_expected_gb, 1),
                        "kv_cache_gb": round(kv_cache_gb, 1),
                        "kv_cache_pct": round(kv_pct, 1),
                        "bloated_models": bloated_models,
                    },
                )
            )
        return recs

    @staticmethod
    def _estimate_weight_size(parameter_size: str) -> float:
        """Estimate model weight size in GB from parameter_size string.

        Uses ~0.5 bytes/param for Q4 quantization (most common),
        ~1.0 bytes/param for Q8, ~2.0 bytes/param for F16.
        Returns 0 if parameter_size can't be parsed.
        """
        if not parameter_size:
            return 0.0
        try:
            # Parse "116.8B", "7B", "137M" etc.
            size_str = parameter_size.upper().strip()
            if size_str.endswith("B"):
                params = float(size_str[:-1])
            elif size_str.endswith("M"):
                params = float(size_str[:-1]) / 1000
            else:
                return 0.0
            # Assume Q4-ish quantization (~0.5 bytes/param)
            return params * 0.5
        except (ValueError, IndexError):
            return 0.0

    def _check_vram_fallbacks(self) -> list[Recommendation]:
        """Surface VRAM fallback events as an INFO health card."""
        from fleet_manager.server.routes.routing import get_vram_fallback_events

        events = get_vram_fallback_events(hours=24)
        if not events:
            return []

        # Aggregate: which models were requested but not loaded
        from collections import Counter

        requested_counts: Counter[str] = Counter()
        for e in events:
            requested_counts[e["requested_model"]] += 1

        top_models = requested_counts.most_common(5)
        model_lines = ", ".join(f"{m} ({c}x)" for m, c in top_models)

        return [
            Recommendation(
                check_id="vram_fallback_active",
                severity=Severity.INFO,
                title=f"VRAM fallback active: {len(events)} request(s) rerouted in 24h",
                description=(
                    f"Requests for unloaded models were routed to loaded alternatives "
                    f"to avoid cold-load delays. Most requested: {model_lines}."
                ),
                fix=(
                    "Consider loading frequently-requested models to avoid fallbacks. "
                    + " ".join(
                        f"ollama pull {m}" for m, _ in top_models[:3]
                    )
                ),
                data={
                    "total_fallbacks": len(events),
                    "top_requested": dict(top_models),
                },
            )
        ]

    def _check_version_mismatch(self, nodes) -> list[Recommendation]:
        """Detect nodes running different versions than the router."""
        from fleet_manager import __version__ as router_version

        mismatched = []
        unknown = []
        for node in nodes:
            if node.status.value == "offline":
                continue
            if not node.agent_version:
                unknown.append(node.node_id)
            elif node.agent_version != router_version:
                mismatched.append((node.node_id, node.agent_version))

        recs = []
        if mismatched:
            node_lines = ", ".join(f"{n} (v{v})" for n, v in mismatched)
            recs.append(
                Recommendation(
                    check_id="version_mismatch",
                    severity=Severity.WARNING,
                    title=(
                        f"Node version mismatch: {len(mismatched)} node(s) "
                        f"differ from router v{router_version}"
                    ),
                    description=(
                        f"The router is running v{router_version} but these nodes report "
                        f"different versions: {node_lines}. Version mismatches can cause "
                        f"unexpected behavior."
                    ),
                    fix=(
                        "Update node agents to match the router version: "
                        "pip install --upgrade ollama-herd"
                    ),
                    data={
                        "router_version": router_version,
                        "mismatched_nodes": {n: v for n, v in mismatched},
                    },
                )
            )
        if unknown:
            recs.append(
                Recommendation(
                    check_id="version_unknown",
                    severity=Severity.INFO,
                    title=f"{len(unknown)} node(s) not reporting version",
                    description=(
                        f"These nodes don't send agent_version in heartbeats: "
                        f"{', '.join(unknown)}. They may be running an older version."
                    ),
                    fix="Upgrade node agents: pip install --upgrade ollama-herd",
                    data={"unknown_nodes": unknown},
                )
            )
        return recs

    def _check_context_protection(self) -> list[Recommendation]:
        """Surface context protection activity as health cards."""
        from fleet_manager.server.streaming import get_context_protection_events

        events = get_context_protection_events(hours=24)
        if not events:
            return []

        from collections import Counter

        actions = Counter(e["action"] for e in events)
        stripped = actions.get("stripped", 0)
        upgraded = actions.get("upgraded", 0)
        warnings = actions.get("warning", 0)

        recs = []

        if warnings > 0:
            # Clients want more context than any loaded model has
            warning_models = Counter(e["model"] for e in events if e["action"] == "warning")
            model_lines = ", ".join(f"{m} ({c}x)" for m, c in warning_models.most_common(5))
            recs.append(
                Recommendation(
                    check_id="context_protection_insufficient",
                    severity=Severity.WARNING,
                    title=f"Context too small for {warnings} request(s) in 24h",
                    description=(
                        f"Clients requested more context than loaded models provide. "
                        f"Affected models: {model_lines}. These requests proceed with "
                        f"the requested num_ctx, which may trigger Ollama model reloads."
                    ),
                    fix=(
                        "Load models with larger context windows, or tell clients to "
                        "omit num_ctx and use the model's default context."
                    ),
                    data={
                        "warning_count": warnings,
                        "affected_models": dict(warning_models.most_common(5)),
                    },
                )
            )

        if stripped > 0 or upgraded > 0:
            parts = []
            if stripped:
                parts.append(f"{stripped} had num_ctx stripped")
            if upgraded:
                parts.append(f"{upgraded} were upgraded to a larger model")
            recs.append(
                Recommendation(
                    check_id="context_protection_active",
                    severity=Severity.INFO,
                    title=f"Context protection active: {len(events)} event(s) in 24h",
                    description=(
                        f"The router intercepted num_ctx values to prevent Ollama model "
                        f"reloads: {', '.join(parts)}. This is expected behavior that "
                        f"prevents multi-minute hangs."
                    ),
                    fix=(
                        "No action needed. To reduce events, tell clients to stop sending "
                        "num_ctx in requests — the model's default context is usually sufficient."
                    ),
                    data={
                        "stripped": stripped,
                        "upgraded": upgraded,
                        "warnings": warnings,
                        "total": len(events),
                    },
                )
            )

        return recs

    def _check_context_waste(
        self, prompt_stats: list[dict], nodes
    ) -> list[Recommendation]:
        """Detect models with allocated context far exceeding actual usage."""
        recs = []
        if not prompt_stats:
            return recs

        # Build allocated context map from nodes
        allocated_ctx: dict[str, int] = {}
        for node in nodes:
            if not node.ollama:
                continue
            for m in node.ollama.models_loaded:
                allocated_ctx[m.name] = max(
                    allocated_ctx.get(m.name, 0), m.context_length or 0
                )

        wasteful = []
        total_waste_ratio = 0
        for stats in prompt_stats:
            model = stats["model"]
            alloc = allocated_ctx.get(model, 0)
            total_p99 = stats.get("total_p99", stats.get("p99", 0))
            if alloc == 0 or total_p99 == 0 or stats["request_count"] < 10:
                continue
            ratio = alloc / total_p99
            if ratio > 4:  # Allocated > 4x actual p99 total
                from fleet_manager.server.context_optimizer import compute_recommended_ctx
                max_24h = stats.get("max_total_24h", 0)
                recommended = compute_recommended_ctx(total_p99, max_24h)
                savings_pct = round((alloc - recommended) / alloc * 100)
                wasteful.append({
                    "model": model,
                    "allocated": alloc,
                    "total_p99": total_p99,
                    "ratio": round(ratio, 1),
                    "recommended": recommended,
                    "savings_pct": savings_pct,
                    "requests": stats["request_count"],
                })
                total_waste_ratio += ratio

        if not wasteful:
            return recs

        model_lines = "; ".join(
            f"{w['model']} (alloc {w['allocated']:,} vs total p99 {w['total_p99']:,}, "
            f"{w['ratio']}x over)"
            for w in wasteful[:3]
        )

        severity = Severity.INFO
        if any(w["ratio"] > 8 for w in wasteful):
            severity = Severity.WARNING

        # Build specific per-model recommendations
        rec_lines = ", ".join(
            f"{w['model']}: {w['recommended']:,}"
            for w in wasteful
        )

        recs.append(
            Recommendation(
                check_id="context_waste",
                severity=severity,
                title=f"Context oversized on {len(wasteful)} model(s)",
                description=(
                    f"Allocated context far exceeds actual usage: {model_lines}. "
                    f"Reducing context frees KV cache memory for additional models."
                ),
                fix=(
                    f"Recommended num_ctx per model: {rec_lines}. "
                    f"Enable in Settings > Context Management (FLEET_DYNAMIC_NUM_CTX=true) "
                    f"to auto-apply these values, or set per-model overrides via the API: "
                    f"POST /dashboard/api/settings with num_ctx_overrides. "
                    f"Requires Ollama restart to take effect on loaded models."
                ),
                data={"wasteful_models": wasteful},
            )
        )
        return recs

    def _check_zombie_reaper(self) -> list[Recommendation]:
        """Surface zombie reaper activity as health cards."""
        from fleet_manager.server.queue_manager import get_reaper_events

        events = get_reaper_events(hours=24)
        if not events:
            return []

        total = len(events)
        avg_stuck = sum(e["stuck_seconds"] for e in events) / total
        queues_affected = set(e["queue_key"] for e in events)

        severity = Severity.CRITICAL if total > 10 else Severity.WARNING

        return [
            Recommendation(
                check_id="zombie_reaper_active",
                severity=severity,
                title=f"Zombie reaper: {total} stuck request(s) cleaned up in 24h",
                description=(
                    f"The reaper detected {total} in-flight request(s) that were stuck "
                    f"for an average of {avg_stuck:.0f} seconds. Affected queues: "
                    f"{', '.join(sorted(queues_affected))}. Zombies consume concurrency "
                    f"slots and block new requests."
                ),
                fix=(
                    "Check Ollama stability — zombies indicate requests that started "
                    "streaming but never completed. Common causes: Ollama process crash, "
                    "client disconnects during long generation, or out-of-memory kills. "
                    "Check logs: grep 'Stream error' ~/.fleet-manager/logs/herd.jsonl"
                ),
                data={
                    "total_reaped": total,
                    "avg_stuck_seconds": round(avg_stuck),
                    "queues_affected": sorted(queues_affected),
                },
            )
        ]

    def _check_image_generation(self, nodes) -> list[Recommendation]:
        """Surface image generation activity and suggest mflux expansion."""
        from fleet_manager.server.routes.image_compat import get_image_gen_events

        events = get_image_gen_events(hours=24)
        if not events:
            return []

        recs: list[Recommendation] = []
        completed = [e for e in events if e["status"] == "completed"]
        failed = [e for e in events if e["status"] == "failed"]

        # Summary card
        avg_ms = (
            sum(e["generation_ms"] for e in completed) / len(completed)
            if completed
            else 0
        )
        nodes_used = {e["node_id"] for e in events}
        summary = (
            f"{len(completed)} images generated"
            f" ({len(failed)} failed) in 24h."
            f" Avg generation: {avg_ms / 1000:.1f}s."
            f" Nodes used: {', '.join(sorted(nodes_used))}."
        )

        severity = Severity.WARNING if failed else Severity.INFO
        recs.append(Recommendation(
            check_id="image_generation",
            severity=severity,
            title="Image Generation Activity",
            description=summary,
            fix="Check failed generations in router logs."
            if failed
            else "Image generation is healthy.",
        ))

        # Recommend mflux on nodes that don't have it
        nodes_with_mflux = {
            n.node_id
            for n in nodes
            if n.image and n.image.models_available
        }
        nodes_without_mflux = [
            n
            for n in nodes
            if n.node_id not in nodes_with_mflux
            and n.status.value == "online"
            and n.memory
            and n.memory.available_gb >= 8.0
        ]

        if nodes_without_mflux and len(completed) >= 3:
            node_names = ", ".join(n.node_id for n in nodes_without_mflux)
            recs.append(Recommendation(
                check_id="mflux_expansion",
                severity=Severity.INFO,
                title="Expand Image Generation to More Nodes",
                description=(
                    f"Image generation was used {len(completed)} times "
                    f"in the last 24h but only {len(nodes_with_mflux)} "
                    f"node(s) have mflux installed. "
                    f"{len(nodes_without_mflux)} online node(s) with "
                    f"sufficient memory could also serve images: "
                    f"{node_names}."
                ),
                fix=(
                    "Install mflux on additional nodes: "
                    "`uv tool install mflux` — first image request "
                    "will download model weights (~3GB)."
                ),
            ))

        return recs

    def _check_transcription(self, nodes) -> list[Recommendation]:
        """Surface transcription activity and suggest STT expansion."""
        from fleet_manager.server.routes.transcription_compat import (
            get_transcription_events,
        )

        events = get_transcription_events(hours=24)
        if not events:
            return []

        recs: list[Recommendation] = []
        completed = [e for e in events if e["status"] == "completed"]
        failed = [e for e in events if e["status"] == "failed"]

        avg_ms = (
            sum(e["processing_ms"] for e in completed) / len(completed)
            if completed
            else 0
        )
        nodes_used = {e["node_id"] for e in events}
        summary = (
            f"{len(completed)} transcriptions"
            f" ({len(failed)} failed) in 24h."
            f" Avg processing: {avg_ms / 1000:.1f}s."
            f" Nodes used: {', '.join(sorted(nodes_used))}."
        )

        severity = Severity.WARNING if failed else Severity.INFO
        recs.append(Recommendation(
            check_id="transcription_activity",
            severity=severity,
            title="Transcription Activity",
            description=summary,
            fix="Check failed transcriptions in router logs."
            if failed
            else "Transcription is healthy.",
        ))

        # Recommend mlx-qwen3-asr on nodes that don't have it
        nodes_with_stt = {
            n.node_id
            for n in nodes
            if n.transcription and n.transcription.models_available
        }
        nodes_without_stt = [
            n
            for n in nodes
            if n.node_id not in nodes_with_stt
            and n.status.value == "online"
            and n.memory
            and n.memory.available_gb >= 4.0
        ]

        if nodes_without_stt and len(completed) >= 3:
            node_names = ", ".join(n.node_id for n in nodes_without_stt)
            recs.append(Recommendation(
                check_id="stt_expansion",
                severity=Severity.INFO,
                title="Expand Transcription to More Nodes",
                description=(
                    f"Transcription was used {len(completed)} times "
                    f"in the last 24h but only {len(nodes_with_stt)} "
                    f"node(s) have mlx-qwen3-asr installed. "
                    f"{len(nodes_without_stt)} online node(s) with "
                    f"sufficient memory could also serve STT: "
                    f"{node_names}."
                ),
                fix=(
                    "Install on additional nodes: "
                    "`uv tool install 'mlx-qwen3-asr[serve]' --python 3.14` "
                    "— first transcription downloads the model (~1.2GB)."
                ),
            ))

        return recs

    def _check_connection_failures(self, nodes) -> list[Recommendation]:
        """Detect nodes that have experienced connection failures to the router."""
        recs = []
        for node in nodes:
            total = node.connection_failures_total
            recent = node.connection_failures
            if total == 0:
                continue

            if recent > 0:
                # Active failures — node currently having trouble
                severity = Severity.CRITICAL if recent > 10 else Severity.WARNING
                recs.append(Recommendation(
                    check_id="connection_failures",
                    severity=severity,
                    title=(
                        f"Node {node.node_id}: {recent} active "
                        f"connection failures"
                    ),
                    description=(
                        f"{node.node_id} failed to reach the router "
                        f"{recent} times since its last successful heartbeat "
                        f"({total} total since agent start). This usually "
                        f"indicates a network issue — WiFi dropout, DHCP "
                        f"renewal, or macOS sleep/wake."
                    ),
                    fix=(
                        f"Check network connectivity on {node.node_id}. "
                        f"The node agent will auto-reconnect when the "
                        f"network recovers. If persistent, restart the "
                        f"node agent with `herd-node`."
                    ),
                    node_id=node.node_id,
                    data={
                        "recent_failures": recent,
                        "total_failures": total,
                    },
                ))
            elif total > 50:
                # Past failures, now recovered — informational
                recs.append(Recommendation(
                    check_id="connection_failures",
                    severity=Severity.INFO,
                    title=(
                        f"Node {node.node_id} recovered from "
                        f"{total} connection failures"
                    ),
                    description=(
                        f"{node.node_id} experienced {total} connection "
                        f"failures since agent start but is now connected. "
                        f"This may indicate intermittent network issues."
                    ),
                    fix="No action needed — node auto-reconnected.",
                    node_id=node.node_id,
                    data={
                        "recent_failures": 0,
                        "total_failures": total,
                    },
                ))
        return recs

    # ------------------------------------------------------------------
    # MLX backend checks (Phase 5 of docs/plans/mlx-backend-for-large-models.md)
    # ------------------------------------------------------------------

    def _check_mlx_backend(self, nodes) -> list[Recommendation]:
        """Check that nodes advertising MLX models have the backend reachable.

        An `mlx:`-prefixed model in a node's ``models_available`` means the
        node ran an MLX poll at heartbeat time and got a response — so the
        MLX backend is alive on that node.  Absence of any `mlx:` prefix
        across all nodes, when the server-side ``mlx_proxy`` is configured,
        means the wiring is broken somewhere.
        """
        recs: list[Recommendation] = []
        for node in nodes:
            if node.status.value != "online":
                continue
            ollama = getattr(node, "ollama", None)
            if ollama is None:
                continue
            mlx_models = [
                m for m in (ollama.models_available or [])
                if isinstance(m, str) and m.startswith("mlx:")
            ]
            if mlx_models:
                # MLX active and advertising — INFO only, for dashboard display
                recs.append(Recommendation(
                    check_id="mlx_backend_active",
                    severity=Severity.INFO,
                    title="MLX backend active",
                    description=(
                        f"Node {node.node_id} is advertising "
                        f"{len(mlx_models)} model(s) via the MLX backend."
                    ),
                    fix=(
                        "No action needed. To add more MLX models: "
                        "`herd mlx pull <model-id>` then restart the node."
                    ),
                    node_id=node.node_id,
                    data={
                        "mlx_models": mlx_models,
                        "count": len(mlx_models),
                    },
                ))

            # Multi-MLX: surface non-healthy individual servers regardless of
            # whether the node has *any* healthy MLX.  Each server is a
            # separate failure unit — a compactor-dedicated 30B can be down
            # while the main Next-4bit is fine.
            servers = getattr(node, "mlx_servers", None) or []
            for srv in servers:
                if srv.status == "memory_blocked":
                    recs.append(Recommendation(
                        check_id="mlx_memory_blocked",
                        severity=Severity.WARNING,
                        title=(
                            f"MLX server {srv.model} skipped start "
                            f"(memory gate) on {node.node_id}"
                        ),
                        description=(
                            srv.status_reason
                            or "Available RAM insufficient at start time."
                        ),
                        fix=(
                            "Free RAM (stop an Ollama model or drop a "
                            "pinned model) and restart the node, OR lower "
                            "FLEET_NODE_MLX_MEMORY_HEADROOM_GB on this node, "
                            "OR remove the entry from FLEET_NODE_MLX_SERVERS."
                        ),
                        node_id=node.node_id,
                        data={
                            "port": srv.port,
                            "model": srv.model,
                            "model_size_gb": srv.model_size_gb,
                        },
                    ))
                elif srv.status == "quarantined":
                    # Supervisor backed off to slow restart cadence after
                    # too many crashes in a short window — a persistent
                    # upstream bug or model corruption is the likely cause.
                    # Distinct from mlx_server_down so operators can tell
                    # "transient failure being retried" from "supervisor
                    # gave up trying fast restarts."
                    recs.append(Recommendation(
                        check_id="mlx_server_quarantined",
                        severity=Severity.CRITICAL,
                        title=(
                            f"MLX server {srv.model} on "
                            f"{node.node_id}:{srv.port} is QUARANTINED"
                        ),
                        description=(
                            srv.status_reason
                            or "Supervisor saw repeated crashes in a short "
                            "window and backed off to slow-restart cadence "
                            "to stop burning CPU."
                        ),
                        fix=(
                            f"Inspect ~/.fleet-manager/logs/mlx-server-"
                            f"{srv.port}.log on {node.node_id} for the "
                            "stack trace.  Common upstream causes: mlx-lm "
                            "version regression (try `uv tool upgrade "
                            "mlx-lm` then re-run `./scripts/setup-mlx.sh`), "
                            "model weights corrupted (delete + re-download "
                            "the HF cache dir), or a request payload "
                            "tickling an mlx_lm bug.  After fixing, "
                            "restart `herd-node` to clear quarantine."
                        ),
                        node_id=node.node_id,
                        data={
                            "port": srv.port,
                            "model": srv.model,
                            "status": srv.status,
                        },
                    ))
                elif srv.status in ("unhealthy", "stopped", "starting"):
                    # "starting" at heartbeat time > 30s old implies wedged —
                    # the start call would have completed or timed out.  We
                    # treat it the same as unhealthy for surfacing purposes.
                    severity = (
                        Severity.WARNING if srv.status == "starting"
                        else Severity.CRITICAL
                    )
                    recs.append(Recommendation(
                        check_id="mlx_server_down",
                        severity=severity,
                        title=(
                            f"MLX server {srv.model} on {node.node_id}:{srv.port} "
                            f"is {srv.status}"
                        ),
                        description=(
                            srv.status_reason
                            or f"mlx_lm.server process status: {srv.status}"
                        ),
                        fix=(
                            f"Check ~/.fleet-manager/logs/mlx-server-{srv.port}.log "
                            f"on {node.node_id}.  Common causes: model weights "
                            "missing from HF cache, --kv-bits patch wiped "
                            "(re-run ./scripts/setup-mlx.sh), or port "
                            "collision from a leftover subprocess."
                        ),
                        node_id=node.node_id,
                        data={
                            "port": srv.port,
                            "model": srv.model,
                            "status": srv.status,
                        },
                    ))
        return recs

    def _check_vision_backend_missing(self, nodes) -> list[Recommendation]:
        """Vision-embedding weights cached but onnxruntime not installed.

        Asymmetric state: the operator pre-downloaded DINOv2 / SigLIP / CLIP
        weights (so they're sitting in ``~/.cache/huggingface/hub``) but the
        node agent's venv doesn't have ``onnxruntime``, so the embedding
        server can't actually serve them.  Without this check, the dashboard
        would silently stop showing vision-embedding chips and the operator
        would have no idea why — see the 2026-04-25 observation in
        ``docs/observations.md`` for the original failure mode.

        Read from ``node.vision_embedding_status`` (populated by the node
        collector via ``_vision_backend_status``).  Older agents that
        predate this field send an empty dict; we skip them gracefully.
        """
        recs: list[Recommendation] = []
        for node in nodes:
            if node.status.value != "online":
                continue
            status = getattr(node, "vision_embedding_status", None) or {}
            if not status:
                continue  # older agent, no signal — don't fire
            backend_available = status.get("backend_available", True)
            cached_count = int(status.get("cached_model_count", 0))
            if backend_available:
                continue  # working as intended
            if cached_count == 0:
                continue  # operator never wanted vision embedding — don't nag
            recs.append(Recommendation(
                check_id="vision_backend_missing",
                severity=Severity.WARNING,
                title=(
                    f"Vision embedding backend not installed on "
                    f"{node.node_id}"
                ),
                description=(
                    f"{cached_count} vision embedding model(s) are cached "
                    "on disk (DINOv2 / SigLIP / CLIP) but onnxruntime is "
                    "not installed in the herd-node venv, so /embed calls "
                    "will return HTTP 500.  Dashboard chips for these "
                    "models are hidden until the backend is installed."
                ),
                fix=(
                    "Run `uv sync --extra embedding` (or `uv sync "
                    "--all-extras`) on the node, then restart `herd-node`. "
                    "The next heartbeat will re-advertise the cached "
                    "models and this warning will clear."
                ),
                node_id=node.node_id,
                data={
                    "cached_model_count": cached_count,
                    "backend_available": False,
                },
            ))
        return recs

    def _check_mapped_models_hot(self, nodes) -> list[Recommendation]:
        """Detect Anthropic-map targets that aren't loaded on any node.

        When ``FLEET_ANTHROPIC_MODEL_MAP`` points at a model that no node has
        hot (or even available on disk), the next Claude Code request pays a
        cold-load penalty — or worse, falls back to a different model and
        silently degrades tool use.  Ties into the broader hot-fleet-health
        plan in ``docs/plans/hot-fleet-health-checks.md``.

        For MLX-routed models (``mlx:`` prefix) we check the node's
        ``models_available`` — the node only advertises them when the MLX
        backend is actually reachable.  For Ollama models we match against
        both ``models_loaded`` (ideal — hot) and ``models_available``
        (good enough — on disk).
        """
        import os

        # Read the map from env (we don't have direct access to settings here,
        # but all the mapped values that matter to this check are the values)
        raw_map = os.environ.get("FLEET_ANTHROPIC_MODEL_MAP", "")
        if not raw_map:
            return []
        try:
            import json as _json

            model_map = _json.loads(raw_map)
        except (_json.JSONDecodeError, ValueError, TypeError):
            return []
        if not isinstance(model_map, dict):
            return []

        mapped_targets = {
            v for v in model_map.values()
            if isinstance(v, str) and v
        }
        if not mapped_targets:
            return []

        # Collect all model names across the online fleet
        all_available: set[str] = set()
        all_loaded: set[str] = set()
        for node in nodes:
            if node.status.value != "online":
                continue
            ollama = getattr(node, "ollama", None)
            if ollama is None:
                continue
            for m in ollama.models_available or []:
                if isinstance(m, str):
                    all_available.add(m)
            for m in ollama.models_loaded or []:
                name = getattr(m, "name", None)
                if isinstance(name, str):
                    all_loaded.add(name)

        missing_entirely: list[str] = []
        not_hot: list[str] = []
        for target in mapped_targets:
            if target not in all_available:
                missing_entirely.append(target)
            elif target.startswith("mlx:"):
                # MLX models don't appear in models_loaded (that's Ollama's
                # hot-list).  Presence in models_available means the MLX
                # server is reachable — good enough.
                continue
            elif target not in all_loaded:
                not_hot.append(target)

        recs: list[Recommendation] = []
        if missing_entirely:
            recs.append(Recommendation(
                check_id="mapped_model_missing",
                severity=Severity.CRITICAL,
                title="Mapped model not on any node",
                description=(
                    f"{len(missing_entirely)} model(s) in "
                    f"FLEET_ANTHROPIC_MODEL_MAP aren't on any fleet node: "
                    f"{', '.join(sorted(missing_entirely))}. Claude Code "
                    f"requests mapped to them will fail with 404."
                ),
                fix=(
                    "Pull the missing models — `ollama pull <name>` for "
                    "Ollama models, or `herd mlx pull <name>` (then start "
                    "mlx_lm.server) for MLX models. "
                    "If the name is wrong, fix FLEET_ANTHROPIC_MODEL_MAP."
                ),
                data={"missing": sorted(missing_entirely)},
            ))
        if not_hot:
            recs.append(Recommendation(
                check_id="mapped_model_cold",
                severity=Severity.WARNING,
                title="Mapped model not currently hot",
                description=(
                    f"{len(not_hot)} mapped model(s) are available on disk "
                    f"but not currently loaded: {', '.join(sorted(not_hot))}. "
                    f"Next Claude Code request pays cold-load penalty (~30s) "
                    f"and may trigger VRAM fallback."
                ),
                fix=(
                    "Pre-warm with: ollama run <name> 'hi' (or a curl POST to "
                    "/api/generate with keep_alive=-1). Or accept the "
                    "first-request cold-load cost."
                ),
                data={"not_hot": sorted(not_hot)},
            ))
        return recs

    # ------------------------------------------------------------------
    # Trace-based checks
    # ------------------------------------------------------------------

    def _check_model_load_timeouts(
        self, timeouts, recent_timeouts, nodes
    ) -> list[Recommendation]:
        """Detect models that repeatedly time out — they can't load fast enough.

        This catches the pattern where a model keeps getting evicted and
        requested again, but takes so long to reload that requests time out.
        The cold-load detector misses these because the requests never complete.
        """
        recs = []
        if timeouts["total_count"] < 3:
            return recs

        # Find the worst-offending models
        for model, info in timeouts["by_model"].items():
            if info["count"] < 3:
                continue
            recent_model = recent_timeouts["by_model"].get(model, {})
            recent_count = recent_model.get("count", 0)
            still_active = recent_count >= 1
            node_list = ", ".join(sorted(set(info["nodes"])))

            if still_active:
                recs.append(
                    Recommendation(
                        check_id="model_load_timeout",
                        severity=Severity.WARNING,
                        title=f"Model {model} repeatedly timing out",
                        description=(
                            f"{info['count']} timeout(s) for {model} in the last 24h "
                            f"({recent_count} in the last hour) on {node_list}. "
                            f"The model is likely being evicted from memory and can't "
                            f"reload before the request timeout."
                        ),
                        fix=(
                            f"Keep {model} loaded: "
                            f"curl http://localhost:11434/api/generate "
                            f"-d '{{\"model\":\"{model}\",\"keep_alive\":-1}}'. "
                            f"Or set OLLAMA_MAX_LOADED_MODELS=-1 to let Ollama "
                            f"fill available memory."
                        ),
                        data={
                            "model": model,
                            "timeouts_24h": info["count"],
                            "timeouts_1h": recent_count,
                            "nodes": info["nodes"],
                        },
                    )
                )
            else:
                recs.append(
                    Recommendation(
                        check_id="model_load_timeout",
                        severity=Severity.INFO,
                        title=f"Model {model} timeouts resolved",
                        description=(
                            f"{info['count']} timeout(s) in the last 24h, but none in "
                            f"the last hour."
                        ),
                        fix="No action needed. This will clear as historical data ages out.",
                        data={
                            "model": model,
                            "timeouts_24h": info["count"],
                            "timeouts_1h": 0,
                            "resolved": True,
                        },
                    )
                )
        return recs

    def _check_model_thrashing(
        self, cold_loads_by_node, recent_cold_by_node, nodes
    ) -> list[Recommendation]:
        """Cross-reference cold loads with node memory to detect thrashing."""
        recs = []
        node_map = {n.node_id: n for n in nodes}
        for node_id, count in cold_loads_by_node.items():
            if count < 3:
                continue  # occasional cold load is fine
            node = node_map.get(node_id)
            has_free_memory = node and node.memory and node.memory.available_gb > 4.0
            if not has_free_memory:
                continue

            recent_count = recent_cold_by_node.get(node_id, 0)
            still_active = recent_count >= 1

            if still_active:
                recs.append(
                    Recommendation(
                        check_id="model_thrashing",
                        severity=Severity.WARNING,
                        title=f"Model thrashing on {node_id}",
                        description=(
                            f"{count} cold loads (TTFT > 40s) in the last 24h "
                            f"({recent_count} in the last hour), "
                            f"but {node.memory.available_gb:.1f} GB memory is free. "
                            f"Models are being unloaded and reloaded unnecessarily."
                        ),
                        fix=(
                            f"Set OLLAMA_KEEP_ALIVE=-1 and OLLAMA_MAX_LOADED_MODELS=-1 "
                            f"on {node_id} to keep models in memory."
                        ),
                        node_id=node_id,
                        data={
                            "cold_loads_24h": count,
                            "cold_loads_1h": recent_count,
                            "available_gb": round(node.memory.available_gb, 1),
                        },
                    )
                )
            else:
                recs.append(
                    Recommendation(
                        check_id="model_thrashing",
                        severity=Severity.INFO,
                        title=f"Model thrashing resolved on {node_id}",
                        description=(
                            f"{count} cold loads in the last 24h, but none in the "
                            f"last hour — fix appears to be working."
                        ),
                        fix="No action needed. This will clear as historical data ages out.",
                        node_id=node_id,
                        data={
                            "cold_loads_24h": count,
                            "cold_loads_1h": 0,
                            "available_gb": round(node.memory.available_gb, 1),
                            "resolved": True,
                        },
                    )
                )
        return recs

    def _check_error_rates(self, error_rates, recent_errors) -> list[Recommendation]:
        recs = []
        recent_map = {e["node_id"]: e for e in recent_errors}
        for entry in error_rates:
            if entry["error_rate_pct"] < self.ERROR_RATE_THRESHOLD_PCT:
                continue

            recent = recent_map.get(entry["node_id"])
            recent_rate = recent["error_rate_pct"] if recent else 0.0
            still_active = recent_rate >= self.ERROR_RATE_THRESHOLD_PCT

            if still_active:
                recs.append(
                    Recommendation(
                        check_id="high_error_rate",
                        severity=Severity.WARNING,
                        title=f"High error rate on {entry['node_id']}",
                        description=(
                            f"{entry['error_rate_pct']:.1f}% error rate in the last 24h "
                            f"({entry['failed']}/{entry['total']} requests failed)."
                        ),
                        fix=(
                            f"Check connectivity and Ollama health on {entry['node_id']}. "
                            f"Verify Ollama is running and responding."
                        ),
                        node_id=entry["node_id"],
                        data={
                            "error_rate_pct": entry["error_rate_pct"],
                            "failed": entry["failed"],
                            "total": entry["total"],
                        },
                    )
                )
            else:
                recs.append(
                    Recommendation(
                        check_id="high_error_rate",
                        severity=Severity.INFO,
                        title=f"Error rate recovered on {entry['node_id']}",
                        description=(
                            f"{entry['error_rate_pct']:.1f}% error rate in the last 24h, "
                            f"but {recent_rate:.1f}% in the last hour — recovering."
                        ),
                        fix="No action needed. This will clear as historical data ages out.",
                        node_id=entry["node_id"],
                        data={
                            "error_rate_pct": entry["error_rate_pct"],
                            "recent_error_rate_pct": recent_rate,
                            "failed": entry["failed"],
                            "total": entry["total"],
                            "resolved": True,
                        },
                    )
                )
        return recs

    def _check_retry_rates(self, retry_stats) -> list[Recommendation]:
        recs = []
        if retry_stats["total_requests"] == 0:
            return recs
        avg_retries = retry_stats["total_retries"] / retry_stats["total_requests"]
        if avg_retries >= self.RETRY_RATE_THRESHOLD:
            recs.append(
                Recommendation(
                    check_id="high_retry_rate",
                    severity=Severity.INFO if avg_retries < 0.5 else Severity.WARNING,
                    title="High retry rate across the fleet",
                    description=(
                        f"Average {avg_retries:.2f} retries per request in the last 24h "
                        f"({retry_stats['total_retries']} retries across "
                        f"{retry_stats['total_requests']} requests)."
                    ),
                    fix=(
                        "Check node connectivity and Ollama stability. "
                        "Retries indicate transient failures."
                    ),
                    data={
                        "avg_retries_per_request": round(avg_retries, 2),
                        "total_retries": retry_stats["total_retries"],
                        "total_requests": retry_stats["total_requests"],
                    },
                )
            )
        return recs

    def _check_stream_reliability(
        self, reliability, recent_reliability
    ) -> list[Recommendation]:
        """Surface client disconnects and incomplete streams as health cards."""
        recs = []
        disconnected = reliability["client_disconnected"]
        incomplete = reliability["incomplete"]
        total = reliability["total_requests"]

        if total == 0:
            return recs

        # Client disconnects — clients timing out or dropping connections
        if disconnected >= 3:
            recent_disc = recent_reliability["client_disconnected"]
            still_active = recent_disc >= 1
            rate = (disconnected / total) * 100

            # Which models are most affected?
            model_lines = ", ".join(
                f"{m} ({v['client_disconnected']}x)"
                for m, v in sorted(
                    reliability["by_model"].items(),
                    key=lambda x: x[1].get("client_disconnected", 0),
                    reverse=True,
                )[:5]
                if v.get("client_disconnected", 0) > 0
            )

            if still_active:
                recs.append(
                    Recommendation(
                        check_id="client_disconnects",
                        severity=Severity.WARNING if rate > 1.0 else Severity.INFO,
                        title=f"Client disconnects: {disconnected} in 24h ({rate:.1f}%)",
                        description=(
                            f"{disconnected} requests ended because the client disconnected "
                            f"before the response completed ({recent_disc} in the last hour). "
                            f"This usually means client-side timeouts are too short for "
                            f"large generations. Affected models: {model_lines}."
                        ),
                        fix=(
                            "Increase client-side timeout (e.g., httpx timeout, "
                            "OpenAI SDK timeout). Large models on slower hardware "
                            "can take minutes for long generations."
                        ),
                        data={
                            "disconnects_24h": disconnected,
                            "disconnects_1h": recent_disc,
                            "rate_pct": round(rate, 1),
                            "by_model": {
                                m: v.get("client_disconnected", 0)
                                for m, v in reliability["by_model"].items()
                                if v.get("client_disconnected", 0) > 0
                            },
                        },
                    )
                )
            else:
                recs.append(
                    Recommendation(
                        check_id="client_disconnects",
                        severity=Severity.INFO,
                        title=f"Client disconnects resolved ({disconnected} in 24h, none recent)",
                        description=(
                            f"{disconnected} client disconnects in 24h but none in the "
                            f"last hour."
                        ),
                        fix="No action needed. This will clear as historical data ages out.",
                        data={
                            "disconnects_24h": disconnected,
                            "disconnects_1h": 0,
                            "resolved": True,
                        },
                    )
                )

        # Incomplete streams — Ollama dropping connections mid-response
        if incomplete >= 2:
            recent_inc = recent_reliability["incomplete"]
            still_active = recent_inc >= 1
            rate = (incomplete / total) * 100

            model_lines = ", ".join(
                f"{m} ({v['incomplete']}x)"
                for m, v in sorted(
                    reliability["by_model"].items(),
                    key=lambda x: x[1].get("incomplete", 0),
                    reverse=True,
                )[:5]
                if v.get("incomplete", 0) > 0
            )

            if still_active:
                recs.append(
                    Recommendation(
                        check_id="incomplete_streams",
                        severity=Severity.WARNING if rate > 0.5 else Severity.INFO,
                        title=f"Incomplete streams: {incomplete} in 24h ({rate:.1f}%)",
                        description=(
                            f"{incomplete} responses were truncated — Ollama dropped the "
                            f"connection before sending the final chunk "
                            f"({recent_inc} in the last hour). This indicates Ollama "
                            f"process instability (OOM, crash, or connection limits). "
                            f"Affected models: {model_lines}."
                        ),
                        fix=(
                            "Check Ollama process health and system memory. "
                            "Common causes: out-of-memory kills during large generations, "
                            "Ollama process crashes, or TCP connection limits. "
                            "Check: journalctl -u ollama (Linux) or "
                            "Console.app > crash reports (macOS)."
                        ),
                        data={
                            "incomplete_24h": incomplete,
                            "incomplete_1h": recent_inc,
                            "rate_pct": round(rate, 1),
                            "by_model": {
                                m: v.get("incomplete", 0)
                                for m, v in reliability["by_model"].items()
                                if v.get("incomplete", 0) > 0
                            },
                        },
                    )
                )
            else:
                recs.append(
                    Recommendation(
                        check_id="incomplete_streams",
                        severity=Severity.INFO,
                        title=f"Incomplete streams resolved ({incomplete} in 24h, none recent)",
                        description=(
                            f"{incomplete} incomplete streams in 24h but none in the "
                            f"last hour."
                        ),
                        fix="No action needed. This will clear as historical data ages out.",
                        data={
                            "incomplete_24h": incomplete,
                            "incomplete_1h": 0,
                            "resolved": True,
                        },
                    )
                )

        return recs

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _compute_health_score(self, recommendations) -> int:
        score = 100
        for r in recommendations:
            if r.severity == Severity.CRITICAL:
                score -= 20
            elif r.severity == Severity.WARNING:
                score -= 10
            elif r.severity == Severity.INFO:
                score -= 3
        return max(0, score)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    # Priority model check
    # ------------------------------------------------------------------

    def _check_priority_models(
        self, priorities: list[dict], nodes: list
    ) -> list[Recommendation]:
        """Warn when high-priority models are not loaded."""
        recs: list[Recommendation] = []
        if not priorities:
            return recs

        # Collect all loaded models across nodes
        loaded: set[str] = set()
        available: set[str] = set()
        for node in nodes:
            if node.ollama:
                for m in node.ollama.models_loaded:
                    loaded.add(m.name)
                for m in node.ollama.models_available:
                    available.add(m)

        # Check top priority models
        missing = []
        for entry in priorities:
            model = entry["model"]
            score = entry["priority_score"]
            if score < 10:
                break  # Only warn for meaningfully used models
            if model not in loaded and model in available:
                missing.append((model, score))

        if missing:
            names = ", ".join(f"{m} (score={s:.0f})" for m, s in missing[:3])
            recs.append(Recommendation(
                check_id="priority_model_not_loaded",
                severity=Severity.WARNING,
                title=f"Priority model(s) not loaded: {missing[0][0]}",
                description=(
                    f"High-usage models available on disk but not loaded: "
                    f"{names}. These models have high request volume but are "
                    f"not in memory, causing cold loads or VRAM fallback to "
                    f"less capable models."
                ),
                fix=(
                    "Models will be auto-loaded by the priority preloader on "
                    "next restart. To load now, send a request for the model "
                    "or use the dashboard."
                ),
                data={"missing_models": [
                    {"model": m, "priority_score": s} for m, s in missing
                ]},
            ))

        return recs

    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{seconds / 60:.0f}m"
        return f"{seconds / 3600:.1f}h"
