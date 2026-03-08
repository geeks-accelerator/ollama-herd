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

            recommendations.extend(
                self._check_model_thrashing(
                    cold_loads["by_node"], recent_cold["by_node"], nodes
                )
            )
            recommendations.extend(
                self._check_error_rates(error_rates, recent_errors)
            )
            recommendations.extend(self._check_retry_rates(retry_stats))

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

    # ------------------------------------------------------------------
    # Trace-based checks
    # ------------------------------------------------------------------

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
