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


_OVERSIZE_HEADROOM_GB = 40.0


def _free_memory_gb(nodes) -> float:
    """Most free memory reported by any node, or inf when unknown.

    Unknown must not manufacture a warning, so it errs toward "plenty".
    """
    best = 0.0
    seen = False
    for n in nodes:
        mem = getattr(n, "memory", None) or getattr(n, "system", None)
        for attr in ("available_gb", "free_gb", "memory_available_gb"):
            v = getattr(mem, attr, None) if mem is not None else None
            if isinstance(v, (int, float)) and v > 0:
                best = max(best, float(v))
                seen = True
    return best if seen else float("inf")


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
        recommendations.extend(self._check_swap_usage(nodes))
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
        recommendations.extend(self._check_anthropic_no_chat_model(nodes))
        recommendations.extend(self._check_trace_store_write_failures(trace_store))
        # Needs no trace data — reads the preloader's own event ring.
        recommendations.extend(self._check_pin_cannot_fit())
        recommendations.extend(self._check_text_embedding_backend_missing(nodes))
        recommendations.extend(self._check_text_embedding_ollama_bypass(nodes))
        recommendations.extend(self._check_nomic_loaded_in_ollama(nodes))
        # Node state only: a co-tenant on the backend must be detectable even
        # with no trace data, which is precisely when herd is least able to
        # explain a throughput drop any other way.
        recommendations.extend(self._check_backend_bypass_clients(nodes))
        # Node state plus the router's own process; needs no trace data.
        recommendations.extend(self._check_process_memory(nodes))

        # Trace-based checks (async, queries SQLite)
        if trace_store:
            recommendations.extend(await self._check_embed_error_rate(trace_store))
            decode_stats = await trace_store.get_decode_latency_stats(
                recent_s=self.RECENT_WINDOW_S
            )
            recommendations.extend(self._check_decode_degraded(decode_stats))
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
            recommendations.extend(
                self._check_num_ctx_override_inert(nodes, prompt_stats)
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

    # Swap in use, as a share of physical RAM, at which a node is flagged.  RAM,
    # not swap total: macOS grows swap on demand, so used/total is ~100% the
    # moment any swap exists.  Half of RAM is well past "a few idle pages" --
    # 2026-10-02 after recovery sat at 36% with 77% of memory free -- and was
    # passed on 2026-10-04 (62%) with a 29 GB model fully paged out.
    SWAP_WARN_FRACTION_OF_RAM = 0.5

    def _check_swap_usage(self, nodes) -> list[Recommendation]:
        """Memory committed well beyond RAM, which pressure alone does not show.

        Pressure is a *current* signal and drops back to normal once the system
        stops actively paging.  What it leaves behind -- a pinned model swapped
        out wholesale -- is invisible to it, and costs the next request to that
        model a page-in of its weights.  Nodes reporting no swap (older agents)
        are skipped, never treated as healthy evidence.
        """
        recs = []
        for node in nodes:
            m = node.memory
            if not m or m.total_gb <= 0:
                continue
            if m.swap_used_gb < m.total_gb * self.SWAP_WARN_FRACTION_OF_RAM:
                continue
            compressed = (
                f", plus {m.compressed_gb:.1f} GB of RAM holding compressed memory"
                if m.compressed_gb > 0
                else ""
            )
            recs.append(
                Recommendation(
                    check_id="swap_usage",
                    severity=Severity.WARNING,
                    title=f"{m.swap_used_gb:.0f} GB swapped out on {node.node_id}",
                    description=(
                        f"{m.swap_used_gb:.1f} GB of swap in use on a "
                        f"{m.total_gb:.0f} GB node{compressed}. Memory is committed "
                        f"well beyond RAM: loaded models or other applications are "
                        f"paged out, and the next request to a paged-out model waits "
                        f"while its weights are paged back in."
                    ),
                    fix=(
                        f"Close large applications on {node.node_id}, or unload "
                        f"models it no longer needs."
                    ),
                    node_id=node.node_id,
                    data={
                        "swap_used_gb": round(m.swap_used_gb, 1),
                        "swap_total_gb": round(m.swap_total_gb, 1),
                        "compressed_gb": round(m.compressed_gb, 1),
                        "total_gb": round(m.total_gb, 1),
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

    # The agent process is a heartbeat loop plus bounded ONNX sessions.  Its
    # documented ceiling with both embedding models resident is ~3.7 GB
    # (docs/issues.md), so 8 GB is roughly double that -- and it is also the
    # figure from the incident itself, where the leaking server passed "more
    # than 8 GB within minutes" on its way to 28 GB.  Nothing legitimate in
    # this process approaches it.
    PROCESS_MEMORY_WARN_GB = 8.0
    PROCESS_MEMORY_CRITICAL_GB = 16.0

    def _check_process_memory(self, nodes) -> list[Recommendation]:
        """herd's own processes holding more memory than they should.

        The gap this closes: herd measured everything except itself.  When the
        native embedding server held 28 GB in one node agent and took a 48 GB
        Mac down with its co-tenants, no recorded number anywhere showed it --
        heartbeats carry *system* memory, which on a 512 GB box is dominated by
        Ollama's resident weights and hides a 20 GB process leak completely.
        Two devices hit the same bug and neither could produce a growth curve.

        Reads ``peak_gb``, not just current.  ONNX Runtime keeps the high-water
        mark of the largest run a process ever does, so one oversized request
        raises the process permanently and the *current* figure then reads as
        innocent -- which is exactly how this hid.  A peak far above current is
        the signature, and is reported as such rather than silently ignored.

        The agent's own number is what matters: the vision and text embedding
        servers are asyncio tasks inside it, so their arenas are charged there.
        Children are excluded from the comparison on purpose -- two
        ``mlx_lm.server`` processes at 17 GB each are legitimate model weights
        and would trip any threshold worth setting.
        """
        offenders: list[dict] = []

        def consider(label: str, node_id: str | None, pm) -> None:
            if pm is None:
                return
            peak = float(getattr(pm, "peak_gb", 0.0) or 0.0)
            current = float(getattr(pm, "footprint_gb", 0.0) or 0.0)
            # RSS is the fallback only where footprint is unavailable (non-macOS,
            # or an unreadable probe).  It is NOT preferred: RSS is the metric
            # that hid the original 28 GB.
            if peak <= 0 and current <= 0:
                current = peak = float(getattr(pm, "rss_gb", 0.0) or 0.0)
            worst = max(peak, current)
            if worst < self.PROCESS_MEMORY_WARN_GB:
                return
            offenders.append({
                "process": label,
                "node_id": node_id,
                "footprint_gb": round(current, 2),
                "peak_gb": round(peak, 2),
                "retained": peak > current * 1.5 and current > 0,
            })

        for node in nodes:
            consider("herd-node", node.node_id, getattr(node, "process_memory", None))

        # The router is a separate process that no heartbeat describes, so it
        # measures itself -- this check runs inside it.
        try:
            from fleet_manager.common.process_memory import probe_process_memory

            consider("herd (router)", None, probe_process_memory())
        except Exception as exc:  # noqa: BLE001 -- never fail the health pass
            logger.debug(
                f"router self-memory probe failed: {type(exc).__name__}: {exc}"
            )

        if not offenders:
            return []

        worst = max(max(o["peak_gb"], o["footprint_gb"]) for o in offenders)
        lines = "; ".join(
            f"{o['process']}"
            + (f" on {o['node_id']}" if o["node_id"] else "")
            + f" at {o['footprint_gb']:.1f} GB (peak {o['peak_gb']:.1f} GB)"
            for o in offenders
        )
        retained = [o for o in offenders if o["retained"]]
        return [
            Recommendation(
                check_id="herd_process_memory",
                severity=(
                    Severity.CRITICAL
                    if worst >= self.PROCESS_MEMORY_CRITICAL_GB
                    else Severity.WARNING
                ),
                title=f"herd process holding {worst:.1f} GB",
                description=(
                    f"{lines}. These are herd's own processes, not Ollama's "
                    f"resident models — the agent is a heartbeat loop plus "
                    f"bounded embedding sessions and should sit near "
                    f"{self.PROCESS_MEMORY_WARN_GB / 2:.0f} GB or below."
                    + (
                        " Peak is well above current, which is the ONNX Runtime "
                        "signature: it keeps the high-water mark of the largest "
                        "run a process ever does, so one oversized request "
                        "raises the floor permanently and only a restart returns "
                        "it. Current usage looking fine does not clear this."
                        if retained
                        else ""
                    )
                ),
                fix=(
                    "Restart the affected process to reclaim the high-water mark "
                    "(`launchctl kickstart -k gui/$UID/"
                    "com.geeksaccelerator.ollama-herd.node`), then find what "
                    "raised it: oversized embedding inputs are the known cause, "
                    "and `prompt_tokens` in request_traces now records real "
                    "tokenizer counts. Confirm with `footprint -p <pid>` rather "
                    "than RSS — RSS is what hid the original 28 GB."
                ),
                node_id=offenders[0]["node_id"],
                data={"processes": offenders},
            )
        ]

    def _check_backend_bypass_clients(self, nodes) -> list[Recommendation]:
        """A process other than herd is talking straight to a node's Ollama.

        This is the one failure class that no other check can see, because it is
        invisible *by construction*: every signal herd scores on — queue depth,
        free slots, session affinity, context fit — is derived from what herd
        itself dispatched, so a second client does not merely go unmeasured, it
        silently invalidates the arithmetic.  ``QueueManager`` caps concurrency
        to match what the backend admits, so a co-tenant filling the same
        llama-server slots turns herd's cap from a protection into an
        oversubscription.

        On 2026-08-21 a co-located CLI daemon contributed ~27% of Ollama's load
        this way, after its cloud provider lost its credentials and its model
        fallback chain silently redirected onto the local fleet.  Fleet decode
        fell 15%; the dashboard, the health engine and the traces all stayed
        clean, because herd's own requests genuinely were.  It took hours and
        six wrong turns to find.  See ``docs/observations.md`` (2026-08-23).

        Reported as WARNING, not CRITICAL: the fleet still serves correctly, and
        the co-tenant may well be deliberate (another team's tool, a benchmark).
        What the operator needs is to *know*, and to be told the fix is to point
        that client at the router rather than to kill it — herd is Ollama-API
        compatible, so repointing costs nothing and restores the accounting.
        """
        offenders: list[dict] = []
        for node in nodes:
            if not node.ollama:
                continue
            for client in node.ollama.backend_clients or []:
                offenders.append({
                    "node_id": node.node_id,
                    "pid": client.pid,
                    "process": client.process,
                    "cmdline": client.cmdline,
                    "peer": getattr(client, "peer", ""),
                    "connections": client.connections,
                    "loopback": client.loopback,
                })
        if not offenders:
            return []

        by_node = sorted({o["node_id"] for o in offenders})

        def _describe(o: dict) -> str:
            # An off-box client has no local pid -- its socket lives on its own
            # machine and all the node can see is Ollama's server end -- so it
            # is named by address. Printing "pid 0" would read as a bug.
            if not o["pid"]:
                return (
                    f"remote {o['peer'] or 'unknown'} ({o['connections']} conn) "
                    f"on {o['node_id']}"
                )
            return (
                f"{o['process'] or 'pid ' + str(o['pid'])} (pid {o['pid']}, "
                f"{o['connections']} conn) on {o['node_id']}"
            )

        lines = "; ".join(_describe(o) for o in offenders[:4])
        has_remote = any(not o["pid"] for o in offenders)
        return [
            Recommendation(
                check_id="backend_bypass_clients",
                severity=Severity.WARNING,
                title=(
                    f"{len(offenders)} process(es) bypassing the router on "
                    f"{len(by_node)} node(s)"
                ),
                description=(
                    f"{lines}. These hold open connections straight to Ollama, so "
                    f"their work occupies the same decode slots herd is scheduling "
                    f"into but appears in none of its metrics. Queue concurrency, "
                    f"free-slot counts and session affinity are all computed from "
                    f"herd's own dispatches, so they are now understating real "
                    f"occupancy — herd's cap oversubscribes the backend rather than "
                    f"protecting it. Expect decode throughput below baseline with a "
                    f"clean dashboard and no errors; the tail degrades first, so "
                    f"compare p25 rather than the mean."
                ),
                fix=(
                    "Point that client at the router instead — herd is Ollama-API "
                    "compatible, so changing its base URL from :11434 to :11435 is "
                    "usually the whole fix, and the work then shows up in traces and "
                    "gets scheduled with everything else. Confirm who it is with "
                    "`lsof -nP -iTCP:11434 -sTCP:ESTABLISHED` and resolve the real "
                    "binary with `lsof -p <pid> | awk '$4==\"txt\"{print $NF}'` — a "
                    "Node daemon's process name is only `process.title` and can point "
                    "at an unrelated project. Check whether it arrived by *fallback* "
                    "rather than by configuration: a cloud provider losing its API key "
                    "can silently redirect a whole workload onto the local fleet."
                    + (
                        " One or more peers are on another machine, so there is no "
                        "local process to inspect — identify them by address from "
                        "`data.clients[].peer` and look on that host. The router "
                        "itself is already excluded, so these are genuinely third "
                        "parties. If Ollama does not need to be reachable off-box, "
                        "binding it back to loopback (unset OLLAMA_HOST) removes the "
                        "whole class of access."
                        if has_remote
                        else ""
                    )
                ),
                node_id=by_node[0] if len(by_node) == 1 else None,
                data={"clients": offenders},
            )
        ]

    @staticmethod
    def _operator_num_ctx_overrides() -> dict[str, int]:
        """Per-model contexts the operator declared in ``FLEET_NUM_CTX_OVERRIDES``.

        One definition, shared by the two checks that must not contradict each
        other: ``num_ctx_override_inert`` (is the resident context what was
        asked for?) and ``context_waste`` (should we ask for less?).  A model
        named here has a deliberate value and must not be told to shrink.

        Env rather than a settings reference, matching
        ``_check_anthropic_map_targets``: the engine is stateless by design and
        ``analyze`` takes only registry + trace_store.
        """
        import json as _json
        import os

        raw = os.environ.get("FLEET_NUM_CTX_OVERRIDES", "")
        if not raw:
            return {}
        try:
            parsed = _json.loads(raw)
        except (_json.JSONDecodeError, ValueError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, int] = {}
        for name, value in parsed.items():
            try:
                out[str(name)] = int(value)
            except (TypeError, ValueError):
                continue
        return out

    def _check_num_ctx_override_inert(
        self, nodes, prompt_stats: list[dict] | None = None
    ) -> list[Recommendation]:
        """A model resident at a context that differs from its configured override.

        ``FLEET_NUM_CTX_OVERRIDES`` only takes effect on a *cold* load — shrinking a
        resident model would force an unload/reload, which is the multi-minute hang
        context protection exists to avoid.  So the router correctly defers, and then
        nothing ever triggers that cold load: the fleet runs with the wrong KV
        allocation indefinitely.

        On 2026-09-28 ``gemma3:27b`` sat at 131072 instead of its configured 32768 —
        4x the intended KV, 28 GB wasted — and the only signal was a single log line
        emitted hours earlier with a by-then-wrong value.  An oversized resident model
        also recreates the precondition for the Ollama scheduler hang of 2026-09-22,
        where a 341.7 GiB prediction for a 16 GB model deadlocked against an
        unevictable ``KEEP_ALIVE=-1`` peer, so this is worth surfacing, not tolerating.

        Reads live node state rather than the event log: the question is "is this
        true *now*", and an event only says it was true once.

        Settings come from env, matching ``_check_anthropic_map_targets`` — the engine
        is stateless by design (``analyze`` takes registry + trace_store as arguments)
        and deliberately holds no settings reference.
        """
        import json as _json
        import os

        if os.environ.get("FLEET_DYNAMIC_NUM_CTX", "").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return []
        raw = os.environ.get("FLEET_NUM_CTX_OVERRIDES", "")
        if not raw:
            return []
        try:
            overrides = _json.loads(raw)
        except (_json.JSONDecodeError, ValueError, TypeError):
            return []
        if not isinstance(overrides, dict) or not overrides:
            return []

        mismatched: list[dict] = []
        for node in nodes:
            if not node.ollama:
                continue
            for loaded in node.ollama.models_loaded:
                want = overrides.get(loaded.name, 0)
                have = loaded.context_length or 0
                if want > 0 and have > 0 and have != want:
                    mismatched.append({
                        "model": loaded.name,
                        "node_id": node.node_id,
                        "configured": want,
                        "resident": have,
                        "ratio": round(have / want, 1),
                    })
        if not mismatched:
            return []

        oversized = [m for m in mismatched if m["resident"] > m["configured"]]
        # Severity follows actual impact, not the ratio.  Serving at a LARGER
        # context than configured is functionally identical for the request --
        # a 500-token prompt does not care whether its slot holds 32K or 131K --
        # and herd deliberately does NOT force a reload to shrink a resident
        # model, because that reload is the multi-minute hang context protection
        # exists to prevent.  So this is a memory-efficiency note, not a fault,
        # and it only earns a WARNING when the waste actually threatens capacity.
        # Calling it WARNING unconditionally (as this check first did) reported a
        # deliberate trade as breakage.
        threatens_capacity = _free_memory_gb(nodes) < _OVERSIZE_HEADROOM_GB
        lines = ", ".join(
            f"{m['model']} on {m['node_id']} resident at {m['resident']} "
            f"(configured {m['configured']}, {m['ratio']}x)"
            for m in mismatched
        )
        # Is the CONFIGURED value itself too small for observed usage?  That is the
        # only case where "increase the context" is the right advice; a resident
        # context larger than the config is not evidence for it.
        usage = {
            st["model"]: max(
                st.get("total_p99", st.get("p99", 0)) or 0,
                st.get("max_total_24h", 0) or 0,
            )
            for st in (prompt_stats or [])
            if st.get("model")
        }
        under_configured = [
            f"{m['model']} (usage ~{usage[m['model']]:,} > configured {m['configured']:,})"
            for m in mismatched
            if usage.get(m["model"], 0) > m["configured"]
        ]
        lower_recs = []
        for m in mismatched:
            u = usage.get(m["model"], 0)
            if u > 0 and u * 4 < m["configured"]:
                lower_recs.append(f"{m['model']} usage ~{u:,}")
        waste_hint = ", ".join(lower_recs)

        unload_cmds = "; ".join(
            f"ollama stop {name}"
            for name in dict.fromkeys(entry["model"] for entry in mismatched)
        )
        return [
            Recommendation(
                check_id="num_ctx_override_inert",
                severity=(
                    Severity.WARNING
                    if (oversized and threatens_capacity)
                    else Severity.INFO
                ),
                title=f"num_ctx override not applied on {len(mismatched)} model(s)",
                description=(
                    f"{lines}. FLEET_NUM_CTX_OVERRIDES only applies on a cold load, so "
                    f"these models keep the context they were loaded with"
                    + (
                        ". Requests are served correctly at the resident context — herd "
                        "will not force a reload to shrink a hot model, since that reload "
                        "is the multi-minute stall context protection exists to prevent. "
                        "The cost is KV cache only. Worth knowing: each cold load at the "
                        "larger context makes Ollama predict context x OLLAMA_NUM_PARALLEL "
                        "of memory and take its evict-first path — the same path that hung "
                        "instead of erroring on 2026-09-22 when the peer model was "
                        "KEEP_ALIVE=-1 and could not be evicted."
                        if oversized
                        else ". Requests get less context than intended."
                    )
                ),
                fix=(
                    # Which direction to fix depends on whether the CONFIGURED value
                    # is right, and that is a usage question, not a residency one.
                    # Answering it here stops this check from contradicting
                    # context_waste, which computes a recommendation from the same
                    # trace data and would otherwise name a different target.
                    (
                        f"Observed usage exceeds the configured value for "
                        f"{', '.join(under_configured)} — raise the override in "
                        f"FLEET_NUM_CTX_OVERRIDES rather than shrinking to it, or "
                        f"requests will hit context protection. "
                        if under_configured
                        else ""
                    )
                    + ("Otherwise u" if under_configured else "U")
                    + f"nload so the override applies on the next load: "
                    f"{unload_cmds} — then send one request through the router "
                    f"(:11435), which injects the configured num_ctx. "
                    + (
                        f"Note `context_waste` recommends even lower values from "
                        f"observed usage ({waste_hint}); the two are consistent — this "
                        f"check is about the override not being applied, that one about "
                        f"whether the override is the right number. "
                        if waste_hint
                        else ""
                    )
                    + "Verify with the launch args, not `ollama ps`: ps -Ao args | "
                    "grep llama-server | grep -oE '-c [0-9]+ -np [0-9]+' "
                    "(per-slot context = -c / -np)."
                ),
                data={"mismatched_models": mismatched},
            )
        ]

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

        # A model named in FLEET_NUM_CTX_OVERRIDES has a context the operator
        # chose, and prompt size is not sufficient evidence to overrule that.
        #
        # The measurement here is sound -- prompt_tokens records Ollama's
        # prompt_eval_count, which is the FULL prompt length and not just the
        # cache-missed part (verified on Ollama 0.34.4: an identical prompt
        # resent reported the same 4074 while prompt_eval_duration fell 2.235s
        # -> 0.021s, so the cache was hit and the count did not move).  What is
        # not sound is the inference that a smaller context is therefore safe.
        # On 2026-09-22 gpt-oss:120b's per-slot context was cut 131072 -> 32768
        # while its p99 prompt was ~1.4K tokens -- comfortably fitting, 23x over
        # by this check's own arithmetic.  Prefix-cache reuse collapsed anyway
        # (5,772 -> 770 hits) and TTFT went 1.0s -> 6.3s for six days, on the
        # model serving 99% of traffic, while decode throughput never moved --
        # which is why nobody caught it.  The router kept requesting 131072
        # against a 32768-resident model and losing, so the operative hazard
        # looks like the requested/resident *mismatch* rather than the absolute
        # size; that mechanism is inferred from the logs, not proven, which is
        # itself the reason not to act on this check's arithmetic alone.
        # So for pinned models: report the memory cost, recommend nothing.
        pinned = self._operator_num_ctx_overrides()
        for w in wasteful:
            w["operator_pinned"] = w["model"] in pinned
        actionable = [w for w in wasteful if not w["operator_pinned"]]
        held = [w for w in wasteful if w["operator_pinned"]]

        # Severity follows what the operator can actually act on.  A fleet whose
        # every oversized model was pinned on purpose has nothing to fix, and a
        # standing WARNING with no available action is how a board stops being
        # read -- the same way the 32768 regression and the trace-write failures
        # both sat unnoticed behind noise.
        severity = Severity.INFO
        if any(w["ratio"] > 8 for w in actionable):
            severity = Severity.WARNING

        rec_lines = ", ".join(
            f"{w['model']}: {w['recommended']:,}" for w in actionable
        )
        held_lines = ", ".join(
            f"{w['model']} (pinned at {pinned[w['model']]:,})" for w in held
        )

        recs.append(
            Recommendation(
                check_id="context_waste",
                severity=severity,
                title=f"Context oversized on {len(wasteful)} model(s)",
                description=(
                    f"Allocated context far exceeds measured prompt usage: "
                    f"{model_lines}. Reducing context frees KV cache memory for "
                    f"additional models."
                    + (
                        f" {held_lines} — pinned in FLEET_NUM_CTX_OVERRIDES, so "
                        f"the value is deliberate and no change is advised here. "
                        f"Context also buys prefix-cache residency, which this "
                        f"check cannot measure: cutting gpt-oss:120b's per-slot "
                        f"context on 2026-09-22 left prompts fitting 23x over and "
                        f"still 6x'd TTFT."
                        if held
                        else ""
                    )
                ),
                fix=(
                    (
                        f"Recommended num_ctx per model: {rec_lines}. "
                        f"Enable in Settings > Context Management "
                        f"(FLEET_DYNAMIC_NUM_CTX=true) to auto-apply these values, "
                        f"or set per-model overrides via the API: POST "
                        f"/dashboard/api/settings with num_ctx_overrides. "
                        f"Requires Ollama restart to take effect on loaded models. "
                        f"Verify prefix-cache hits and TTFT after the restart, not "
                        f"just decode throughput — decode is the one metric a bad "
                        f"context change leaves untouched."
                        if actionable
                        else ""
                    )
                    + (
                        f"No action recommended for {held_lines}: remove the entry "
                        f"from FLEET_NUM_CTX_OVERRIDES first if the pin is no longer "
                        f"wanted, then re-check."
                        if held and not actionable
                        else ""
                    )
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
                            "version regression (re-run "
                            "`./scripts/setup-mlx.sh` to restore the pinned, "
                            "tested version), "
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
                            "missing from HF cache, mlx-lm older than 0.32.0 "
                            "so --kv-bits is rejected (run ./scripts/setup-mlx.sh), "
                            "or port "
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

    def _check_text_embedding_backend_missing(self, nodes) -> list[Recommendation]:
        """Text embedding weights cached but fastembed not installed.

        Mirrors ``_check_vision_backend_missing`` exactly.  Fires when the
        operator has pre-downloaded nomic-embed-text (so weights exist in
        ~/.fleet-manager/models/text-embedding/) but fastembed isn't installed
        in the herd-node venv, meaning the text embedding server can't start
        and /api/embed calls for nomic-embed-text will fall back to Ollama.

        Read from ``node.text_embedding_status`` (populated by the node
        collector via ``_text_embedding_backend_status``).  Older agents that
        predate this field send an empty dict; we skip them gracefully.
        """
        recs: list[Recommendation] = []
        for node in nodes:
            if node.status.value != "online":
                continue
            status = getattr(node, "text_embedding_status", None) or {}
            if not status:
                continue  # older agent, no signal — don't fire
            backend_available = status.get("backend_available", True)
            cached_count = int(status.get("cached_model_count", 0))
            if backend_available:
                continue  # working as intended
            if cached_count == 0:
                continue  # operator never wanted text embedding — don't nag
            recs.append(Recommendation(
                check_id="text_embedding_backend_missing",
                severity=Severity.WARNING,
                title=(
                    f"Text embedding backend not installed on {node.node_id}"
                ),
                description=(
                    f"{cached_count} text embedding model(s) are cached on disk "
                    "(nomic-embed-text-v1.5) but fastembed is not installed in "
                    "the herd-node venv, so the native text embedding server "
                    "cannot start. Embed requests for nomic-embed-text will fall "
                    "back to Ollama and may queue behind LLM inference slots."
                ),
                fix=(
                    "Run `uv sync --extra embedding` (or `uv sync --all-extras`) "
                    "on the node — fastembed is now included in the embedding extra. "
                    "Then restart `herd-node`. The text embedding server will start "
                    "automatically and this warning will clear."
                ),
                node_id=node.node_id,
                data={
                    "cached_model_count": cached_count,
                    "backend_available": False,
                },
            ))
        return recs

    # Text embedding model names that have a native fastembed equivalent
    _NATIVE_TEXT_EMBED_MODELS = frozenset({
        "nomic-embed-text",
        "nomic-embed-text:latest",
    })

    def _check_text_embedding_ollama_bypass(self, nodes) -> list[Recommendation]:
        """Warn when nomic-embed-text is in Ollama but the native server isn't running.

        The native fastembed server (port 11439) routes embed requests completely
        outside Ollama, eliminating contention with LLM inference slots.  When
        it isn't running, every embed request competes for OLLAMA_NUM_PARALLEL
        capacity — a 120B model can hold a slot for minutes while embeds queue
        and time out (the June 1 2026 incident: 202 ReadTimeout errors over 1.5h
        on a machine at 14% CPU).

        Fires per node when:
          - nomic-embed-text (or :latest) is in ollama.models_available, AND
          - text_embedding_port == 0  (native server not running)

        Does NOT fire when the native server is already up — operators who've
        installed --extra embedding are already on the better path.
        """
        recs: list[Recommendation] = []
        for node in nodes:
            if node.status.value != "online":
                continue
            if node.text_embedding_port > 0:
                continue  # native server is running — no action needed
            if not node.ollama:
                continue
            ollama_models = set(node.ollama.models_available or [])
            matched = ollama_models & self._NATIVE_TEXT_EMBED_MODELS
            if not matched:
                continue  # nomic-embed-text not present on this node

            arch = getattr(node, "arch", "") or ""
            platform_note = (
                " On Apple Silicon this is especially harmful — OLLAMA_NUM_PARALLEL "
                "limits concurrent slots, so a 120B inference run can block embeds "
                "for minutes."
                if "apple" in arch.lower()
                else " Under concurrent LLM load, embed requests can queue and time out."
            )

            recs.append(Recommendation(
                check_id="text_embedding_ollama_bypass",
                severity=Severity.WARNING,
                title=(
                    f"nomic-embed-text on {node.node_id} is routing through Ollama "
                    f"— native backend not running"
                ),
                description=(
                    f"{node.node_id} has {', '.join(sorted(matched))} available in Ollama "
                    f"but the native fastembed text embedding server (port 11439) is not "
                    f"running. Embed requests consume the same OLLAMA_NUM_PARALLEL slots "
                    f"as LLM inference.{platform_note}"
                ),
                fix=(
                    "Install the embedding extra to enable the native fastembed backend: "
                    "`uv sync --extra embedding` (or `uv sync --all-extras`) on the node, "
                    "then restart `herd-node`. nomic-embed-text requests will be routed "
                    "to port 11439 and will never touch Ollama's inference queue again. "
                    "The 130 MB model weights download automatically on the first request."
                ),
                node_id=node.node_id,
                data={
                    "ollama_embed_models": sorted(matched),
                    "native_server_running": False,
                    "arch": arch,
                },
            ))
        return recs

    def _check_nomic_loaded_in_ollama(self, nodes) -> list[Recommendation]:
        """Inform when nomic-embed-text is loaded in Ollama while the native server is up.

        Once the fastembed server is running, Ollama's copy of nomic-embed-text is
        never used — it just occupies VRAM (~275 MB) and an Ollama model slot.
        This is cosmetic, not critical, so severity is INFO.  The model will
        self-evict once KEEP_ALIVE expires; this check nudges operators who want
        to free the slot immediately.
        """
        recs: list[Recommendation] = []
        for node in nodes:
            if node.status.value != "online":
                continue
            if node.text_embedding_port == 0:
                continue  # native server not running — covered by bypass check instead
            if not node.ollama:
                continue
            loaded_names = {m.name for m in (node.ollama.models_loaded or [])}
            matched = loaded_names & self._NATIVE_TEXT_EMBED_MODELS
            if not matched:
                continue

            recs.append(Recommendation(
                check_id="nomic_loaded_in_ollama",
                severity=Severity.INFO,
                title=(
                    f"nomic-embed-text is loaded in Ollama on {node.node_id} "
                    f"but the native backend is handling all embed requests"
                ),
                description=(
                    f"The native fastembed server (port {node.text_embedding_port}) is "
                    f"running and intercepting all nomic-embed-text requests before they "
                    f"reach Ollama. However, {', '.join(sorted(matched))} is still loaded "
                    f"in Ollama, consuming ~275 MB of VRAM and an inference slot for no "
                    f"benefit. It will self-evict when OLLAMA_KEEP_ALIVE expires."
                ),
                fix=(
                    "To free the slot immediately: `ollama stop nomic-embed-text` on the "
                    "node. Or set `OLLAMA_KEEP_ALIVE=5m` (instead of -1) for embed models "
                    "so they evict after a period of disuse. No action required — it "
                    "resolves on its own."
                ),
                node_id=node.node_id,
                data={
                    "loaded_embed_models": sorted(matched),
                    "native_port": node.text_embedding_port,
                },
            ))
        return recs

    # A model's own p99 TPOT must exceed this multiple of its recent baseline
    # before we call it degraded.  Decode genuinely varies with used context and
    # batch size, so a tight threshold would fire constantly; 2x is well clear of
    # that noise while still catching the 7x collapse observed 2026-07-19.
    DECODE_DEGRADED_RATIO = 2.0
    # Below this many recent samples the p99 is not a p99.  Also guards the
    # degenerate case: a model with no recent traffic has recent_p99 == 0, which
    # would otherwise read as an enormous improvement.
    DECODE_MIN_SAMPLES = 20

    # A pin that fails its fit test this many times in 24h isn't transient
    # memory pressure — it's an instruction the fleet can't carry out.  Each
    # refresh cycle is ~10 minutes, so 3 means it has been failing for roughly
    # half an hour and will keep failing.
    PIN_FIT_FAILURE_THRESHOLD = 3

    def _check_pin_cannot_fit(self) -> list[Recommendation]:
        """Surface a pinned model the fleet keeps failing to load.

        The preloader retries pinned models every refresh.  When one can never
        fit, that becomes an infinite loop of "notice it's evicted → try to load
        → refuse → wait 10 minutes", logged entirely at INFO.  On 2026-07-19 a
        pin needing ~294GB looped 56 times over 9.5 hours on this fleet without
        producing a single WARNING, and was found only while tracing what had
        evicted three unrelated models.

        The interesting part isn't the wasted cycles — it's that a pin is a
        promise the fleet is quietly failing to keep.  Anything relying on that
        model being resident is getting cold loads or a fallback, and nothing
        says so.
        """
        from fleet_manager.server.model_preloader import get_pin_fit_failures

        by_model: dict[tuple[str, str], list[dict]] = {}
        for ev in get_pin_fit_failures(hours=24):
            by_model.setdefault((ev["model"], ev["node_id"]), []).append(ev)

        recs: list[Recommendation] = []
        for (model, node_id), events in by_model.items():
            if len(events) < self.PIN_FIT_FAILURE_THRESHOLD:
                continue
            worst = max(events, key=lambda e: e["needed_gb"] - e["available_gb"])
            recs.append(
                Recommendation(
                    check_id="pin_cannot_fit",
                    severity=Severity.WARNING,
                    title=f"Pinned model can't be loaded: {model} on {node_id}",
                    description=(
                        f"The preloader has failed to load pinned {model} "
                        f"{len(events)} times in 24h — it needs "
                        f"~{worst['needed_gb']:.0f}GB but only "
                        f"{worst['available_gb']:.0f}GB was free. The pin is "
                        f"retried every refresh, so this repeats indefinitely, "
                        f"and anything expecting {model} to be resident is "
                        f"silently getting a cold load or a fallback instead."
                    ),
                    fix=(
                        f"If the pin is no longer needed: "
                        f"DELETE /fleet/pin/{model}. If it is, free memory by "
                        f"unpinning something else (GET /fleet/limits shows what "
                        f"you're holding), or lower the model's context via "
                        f"FLEET_NUM_CTX_OVERRIDES — KV scales with context, so a "
                        f"smaller window can be the difference between fitting "
                        f"and not."
                    ),
                    node_id=node_id,
                    data={
                        "model": model,
                        "failures_24h": len(events),
                        "needed_gb": round(worst["needed_gb"], 1),
                        "available_gb": round(worst["available_gb"], 1),
                    },
                )
            )
        return recs

    def _check_decode_degraded(self, decode_stats) -> list[Recommendation]:
        """Detect decode slowing down while everything still reports success.

        This is the failure mode that has cost the most investigation time on
        this project.  When a model's generation is contended, requests still
        complete: the stream finishes, tokens are produced, no error is logged,
        and the trace status is `completed`.  Nothing in the fleet's existing
        signals distinguishes "healthy" from "taking 10x longer per token", so
        the first visible symptom is a pile-up — by which point requests are
        already failing and the cause is hours upstream.

        TPOT is the discriminating measure because it excludes TTFT, and TTFT is
        where queue wait and prefill live.  A request that waited 8 minutes for a
        slot and then generated at full speed has terrible latency and *fine*
        TPOT; that distinction is the entire point.  Comparing each model against
        its own baseline rather than an absolute threshold matters just as much —
        30 ms/token is healthy for a 120B and alarming for a 4B.
        """
        recs: list[Recommendation] = []
        for st in decode_stats or []:
            recent_n = st.get("recent_n", 0)
            recent = st.get("recent_p99", 0.0)
            baseline = st.get("baseline_p99", 0.0)
            if recent_n < self.DECODE_MIN_SAMPLES or recent <= 0 or baseline <= 0:
                continue
            ratio = recent / baseline
            if ratio < self.DECODE_DEGRADED_RATIO:
                continue
            model = st.get("model", "?")
            node_id = st.get("node_id", "?")
            recs.append(
                Recommendation(
                    check_id="decode_degraded",
                    severity=Severity.WARNING,
                    title=f"Decode {ratio:.1f}x slower than usual: {model} on {node_id}",
                    description=(
                        f"Time per output token is {recent:.0f} ms (p99) against a "
                        f"24h baseline of {baseline:.0f} ms — {ratio:.1f}x worse, "
                        f"over {recent_n} recent requests. Requests are still "
                        f"completing, so nothing else will report this: only "
                        f"generation speed changed."
                    ),
                    fix=(
                        "Usually contention rather than the model. Check what else "
                        f"is running on {node_id} — TPOT excludes queue wait, so "
                        "this is other work competing for the GPU during "
                        "generation, not requests waiting for a slot. If a caller "
                        "polls this model on a fixed interval, confirm the "
                        "interval still exceeds p99 latency; once a call outlasts "
                        "its own poll period, requests stack and each one slows "
                        "the rest."
                    ),
                    node_id=node_id,
                    data={
                        "model": model,
                        "recent_p99_ms": round(recent, 1),
                        "baseline_p99_ms": round(baseline, 1),
                        "ratio": round(ratio, 2),
                        "samples": recent_n,
                    },
                )
            )
        return recs

    def _check_trace_store_write_failures(
        self, trace_store,
    ) -> list[Recommendation]:
        """Detect ongoing SQLite write failures in the trace store.

        Surfaces the 2026-05-10 incident pattern: WAL-mode SQLite under a
        sustained read can starve writers past the busy_timeout, causing
        background trace-record tasks to fail with ``database is locked``.
        Requests still succeed end-to-end (the trace write is fire-and-
        forget), but observability vanishes — the dashboard starts showing
        ``reqs_24h=0`` because nothing is being recorded.  Without this
        check the only signal an operator gets is the absence of dashboard
        traffic and a growing ERROR rate in herd.jsonl, both easily missed.

        Threshold rationale: under healthy operation the retry loop in
        ``TraceStore.record_trace`` absorbs transient contention silently,
        so a non-zero failure count over 5 minutes means the retry budget
        is being exhausted — that's the warning trigger.  >50 in 5 min is
        a sustained incident worth a critical.
        """
        recs: list[Recommendation] = []
        if trace_store is None:
            return recs
        get_count = getattr(trace_store, "get_write_failure_count", None)
        if get_count is None:
            return recs  # older trace_store (pre-0.6.2) — no signal available
        try:
            failures_5m = int(get_count(window_s=300.0))
        except Exception:  # noqa: BLE001 — defensive; never let a health check crash the analyzer
            return recs
        if failures_5m == 0:
            return recs
        severity = Severity.CRITICAL if failures_5m >= 50 else Severity.WARNING
        recs.append(Recommendation(
            check_id="trace_store_write_failures",
            severity=severity,
            title=(
                f"{failures_5m} trace-record failures in the last 5 minutes"
            ),
            description=(
                "Background trace_record tasks are failing after the 30s "
                "busy_timeout + 3 retries.  Requests are likely still "
                "succeeding (trace writes are fire-and-forget) but "
                "observability is degraded — `reqs_24h`, latency stats, "
                "and per-model error rates on the dashboard will look "
                "stale or zeroed until writes resume.  Common causes: a "
                "long-running read transaction holding the WAL checkpoint "
                "open, an out-of-disk-space condition on the data dir, or "
                "a stale file lock from a crashed previous process."
            ),
            fix=(
                "1) Check disk space on the data dir: `df -h ~/.fleet-manager`. "
                "2) Check for a stale `latency.db-shm`/`-wal` from a crashed "
                "process: `ls -lh ~/.fleet-manager/latency.db*`.  "
                "3) Restart `herd` to release any held lock: "
                "`pkill -9 -f 'bin/herd|mlx_lm.server' && uv run herd & disown`.  "
                "4) If it recurs, file an issue with `~/.fleet-manager/logs/` "
                "attached — there may be a slow query path that needs an "
                "index."
            ),
            data={
                "failures_5m": failures_5m,
                "threshold_warning": 1,
                "threshold_critical": 50,
            },
        ))
        return recs

    async def _check_embed_error_rate(self, trace_store) -> list[Recommendation]:
        """Detect elevated embed failure rates (ReadTimeout / HTTP errors).

        Embed failures were previously untraced — they surfaced only as ERROR
        lines in herd.jsonl and were invisible to the dashboard (see 2026-06-01
        observation).  Now that embed traces are recorded, this check surfaces
        sustained timeouts so operators can tune OLLAMA_NUM_PARALLEL or identify
        the congested workload.

        Root cause pattern: OLLAMA_NUM_PARALLEL limits Ollama's concurrent
        inference slots.  When large LLM requests occupy all slots, embed
        requests queue inside Ollama.  Long-running 120B inference can stall
        embeds for minutes, causing clients to time out.  The machine may be
        lightly loaded overall — this is software queue saturation, not hardware
        pressure.

        Thresholds: ≥5 failures/hour → WARNING, ≥25/hour → CRITICAL.
        """
        recs: list[Recommendation] = []
        if trace_store is None:
            return recs
        get_stats = getattr(trace_store, "get_embed_error_stats", None)
        if get_stats is None:
            return recs  # pre-0.6.3 trace_store — no embed stats available
        try:
            stats = await get_stats(lookback_s=3600)
        except Exception:  # noqa: BLE001
            return recs
        failed = stats.get("failed", 0)
        total = stats.get("total", 0)
        if failed == 0:
            return recs
        severity = Severity.CRITICAL if failed >= 25 else Severity.WARNING
        error_pct = round((failed / total) * 100, 1) if total > 0 else 100.0
        by_model = stats.get("by_model", {})
        model_summary = ", ".join(
            f"{m}: {v['failed']}/{v['total']} failed"
            for m, v in by_model.items()
        )
        recs.append(Recommendation(
            check_id="embed_error_rate",
            severity=severity,
            title=f"{failed} embed failure(s) in the last hour ({error_pct}% error rate)",
            description=(
                f"Embedding requests are failing with ReadTimeout or HTTP errors — "
                f"the embed model is likely queued behind concurrent LLM inference "
                f"inside Ollama. {model_summary}. "
                f"Note: the machine hardware may be lightly loaded; this is "
                f"Ollama's software concurrency limit (OLLAMA_NUM_PARALLEL), not "
                f"CPU/RAM pressure."
            ),
            fix=(
                "1) Increase OLLAMA_NUM_PARALLEL to allow embed requests to run "
                "alongside LLM inference: set OLLAMA_NUM_PARALLEL=4 (or higher) in "
                "~/.zshrc and run `launchctl setenv OLLAMA_NUM_PARALLEL 4`, then "
                "restart Ollama. "
                "2) Enable FLEET_DYNAMIC_NUM_CTX=true in ~/.fleet-manager/env to "
                "reduce per-model KV cache footprint (counteracts the memory growth "
                "from higher parallelism). "
                "3) If the embed workload is large and sustained (e.g. VOD frame "
                "processing), consider routing it to off-peak hours or a dedicated "
                "node."
            ),
            data={
                "failed_1h": failed,
                "total_1h": total,
                "error_pct": error_pct,
                "by_model": by_model,
                "threshold_warning": 5,
                "threshold_critical": 25,
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

    def _check_anthropic_no_chat_model(self, nodes) -> list[Recommendation]:
        """Warn when nothing on the fleet can serve a Claude Code request.

        With auto-routing (the default) a ``claude-*`` id resolves to the best
        loaded — else on-disk — chat/coding model.  If the fleet has none, every
        Anthropic Messages (``/v1/messages``) request 404s, and today the user
        only finds out when Claude Code first calls.  The dominant case is a
        brand-new install: ``ANTHROPIC_BASE_URL`` is set but no model has been
        pulled yet.  Surface it proactively, and distinguish "pull a model" from
        the config case (auto-routing off with no map covering ``claude-*``).

        Reuses the real ``resolve_model`` so this check can't drift from what
        the route actually does.  Embedding-only models don't count as chat, so
        an embeddings-only deployment is correctly flagged (it can't serve
        Claude Code) — the WARNING says to ignore it if that's intentional.
        """
        import json as _json
        import os

        from fleet_manager.server.anthropic_autoroute import (
            rank_candidates,
            resolve_model,
        )

        model_map: dict = {}
        raw_map = os.environ.get("FLEET_ANTHROPIC_MODEL_MAP", "")
        if raw_map:
            try:
                parsed = _json.loads(raw_map)
                if isinstance(parsed, dict):
                    model_map = parsed
            except (ValueError, TypeError):
                model_map = {}
        auto_route = os.environ.get(
            "FLEET_ANTHROPIC_AUTO_ROUTE", "true"
        ).strip().lower() not in ("false", "0", "no", "off")

        loaded: set[str] = set()
        ondisk: set[str] = set()
        any_online = False
        for node in nodes:
            if node.status.value != "online":
                continue
            any_online = True
            ollama = getattr(node, "ollama", None)
            if ollama is not None:
                for m in ollama.models_loaded or []:
                    name = getattr(m, "name", None)
                    if isinstance(name, str):
                        loaded.add(name)
                for m in ollama.models_available or []:
                    if isinstance(m, str):
                        ondisk.add(m)
            for s in getattr(node, "mlx_servers", None) or []:
                if getattr(s, "status", None) == "healthy" and getattr(
                    s, "model", None
                ):
                    nm = f"mlx:{s.model}"
                    loaded.add(nm)
                    ondisk.add(nm)
        ondisk |= loaded

        # No online node → offline-fleet checks own that; nothing to say here.
        if not any_online:
            return []

        model, _reason = resolve_model(
            "claude-sonnet-4-5", model_map, loaded, ondisk, auto_route=auto_route
        )
        if model:
            return []  # a claude-* request would resolve fine

        # Unresolved — is it because there's no chat model, or a config gap?
        has_chat = bool(rank_candidates(ondisk, "claude-sonnet-4-5"))
        if not has_chat:
            return [Recommendation(
                check_id="anthropic_no_chat_model",
                severity=Severity.WARNING,
                title="No model available for Claude Code",
                description=(
                    "No chat/coding model is loaded or on disk anywhere on the "
                    "fleet, so Anthropic Messages (/v1/messages) requests — e.g. "
                    "Claude Code — will 404 until one is present. Embedding-only "
                    "models don't count. Expected on a brand-new install before "
                    "any model is pulled."
                ),
                fix=(
                    "Pull a coding model, e.g. `ollama pull qwen3-coder:30b`. "
                    "With auto-routing on (default), Claude Code resolves to it "
                    "automatically — no FLEET_ANTHROPIC_MODEL_MAP needed. If you "
                    "don't use Claude Code, ignore this."
                ),
                data={"auto_route": auto_route, "loaded": sorted(loaded)[:10]},
            )]
        return [Recommendation(
            check_id="anthropic_unrouted_config",
            severity=Severity.WARNING,
            title="Claude Code requests won't route (auto-routing off, no map)",
            description=(
                "The fleet has chat-capable models, but FLEET_ANTHROPIC_AUTO_ROUTE "
                "is off and no FLEET_ANTHROPIC_MODEL_MAP entry (or 'default' key) "
                "covers claude-* ids — so Anthropic Messages requests will 404 "
                "even though a usable model is loaded."
            ),
            fix=(
                "Either set FLEET_ANTHROPIC_AUTO_ROUTE=true (resolves claude-* to "
                "the best loaded model), or add a FLEET_ANTHROPIC_MODEL_MAP with a "
                "'default' key pointing at a loaded model."
            ),
            data={"auto_route": auto_route, "loaded": sorted(loaded)[:10]},
        )]

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

        # Models that cannot be preloaded at all.  Preloading warms a model by
        # posting to /api/generate, which Ollama refuses outright for an
        # embedding model ("does not support generate"), so reporting one as a
        # priority model that failed to load describes a thing that was never
        # going to happen.  Before this, `nomic-embed-text` sat on the dashboard
        # as a WARNING indefinitely: high request volume made it a priority
        # model, and the preloader correctly declined to warm it.
        #
        # Two signals, because neither alone is sufficient:
        #  * what Ollama reports.  Authoritative and immediate, but
        #    `model_has_capability` is presence-only by contract — it only
        #    answers in the positive, and older Ollama under-reports.
        #  * what a backend actually refused, learned at runtime.  Covers nodes
        #    that report no capabilities, but only after one failed attempt.
        from fleet_manager.server.serializers import model_has_capability
        from fleet_manager.server.streaming import get_non_generatable_models

        refused = get_non_generatable_models()

        def _cannot_be_preloaded(model: str) -> bool:
            if model in refused:
                return True
            return any(
                model_has_capability(node, model, "embedding") for node in nodes
            )

        # Check top priority models
        missing = []
        for entry in priorities:
            model = entry["model"]
            score = entry["priority_score"]
            if score < 10:
                break  # Only warn for meaningfully used models
            if model not in loaded and model in available:
                if _cannot_be_preloaded(model):
                    continue
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
