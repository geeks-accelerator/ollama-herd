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

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{seconds / 60:.0f}m"
        return f"{seconds / 3600:.1f}h"
