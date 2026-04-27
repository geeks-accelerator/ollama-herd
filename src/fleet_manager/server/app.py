"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from fleet_manager.common.discovery import FleetServiceAdvertiser
from fleet_manager.common.logging_config import setup_logging
from fleet_manager.models.config import ServerSettings
from fleet_manager.server.latency_store import LatencyStore
from fleet_manager.server.mlx_proxy import MlxProxy
from fleet_manager.server.pinned_models import PinnedModelsStore
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.rebalancer import Rebalancer
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.server.streaming import StreamingProxy
from fleet_manager.server.trace_store import TraceStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: register mDNS, init stores, start monitors. Shutdown: cleanup."""
    settings: ServerSettings = app.state.settings

    # Set up structured JSONL logging to ~/.fleet-manager/logs/
    setup_logging(data_dir=settings.data_dir)

    # Initialize components
    registry = NodeRegistry(settings)
    latency_store = LatencyStore(settings.data_dir)
    await latency_store.initialize()
    trace_store = TraceStore(settings.data_dir)
    await trace_store.initialize()
    scorer = ScoringEngine(settings, registry, latency_store)
    queue_mgr = QueueManager(registry=registry, settings=settings)
    streaming_proxy = StreamingProxy(registry, latency_store, trace_store, settings=settings)
    rebalancer = Rebalancer(settings, registry, scorer, queue_mgr, streaming_proxy)

    # MLX backend — opt-in alternative serving path for models that can't fit
    # alongside the Ollama 3-model cap.  See `docs/plans/mlx-backend-for-large-models.md`.
    # The resolver lets the proxy dispatch to the right node+port per model
    # when multiple MLX servers are running across the fleet (see
    # ``docs/issues/multi-mlx-server-support.md``).  Legacy single-URL config
    # still works via the ``base_url`` positional.
    mlx_proxy: MlxProxy | None = None
    if getattr(settings, "mlx_enabled", False):
        def _mlx_url_resolver(model_key: str) -> str | None:
            return registry.resolve_mlx_url(model_key)

        mlx_proxy = MlxProxy(
            settings.mlx_url,  # legacy fallback when registry has no match
            trace_store=trace_store,
            url_resolver=_mlx_url_resolver,
            max_queue_depth=getattr(settings, "mlx_max_queue_depth", 10),
            retry_after_seconds=getattr(settings, "mlx_retry_after_seconds", 10),
            read_timeout_s=getattr(settings, "mlx_read_timeout_s", 1800.0),
            wall_clock_timeout_s=getattr(
                settings, "mlx_wall_clock_timeout_s", 300.0,
            ),
            max_inflight_per_model=getattr(
                settings, "mlx_max_inflight_per_model", 1,
            ),
        )
        logger.info(
            f"MLX backend enabled (fallback={settings.mlx_url}, "
            f"resolver=registry; admission: 1 in-flight + "
            f"{mlx_proxy.max_queue_depth} queued max)"
        )

    # Context Hygiene Compactor — shrinks bloated tool_result blocks before
    # the main model sees them.  Closes the effective-context gap between
    # local and hosted Claude on agent workloads.  See plan file.
    context_compactor = None
    if getattr(settings, "context_compaction_enabled", False):
        from fleet_manager.common.ollama_client import OllamaClient
        from fleet_manager.server.context_compactor import (
            ContextCompactor,
            CuratorSelector,
            OllamaCurator,
            SummaryCache,
        )
        from fleet_manager.server.model_preloader import _parse_pinned_models

        # Local Ollama on the router's machine hosts the curator model.
        # (Could extend to point at a specific node's Ollama later.)
        curator_client = OllamaClient(base_url="http://localhost:11434")
        curator = OllamaCurator(
            curator_client, model=settings.context_compaction_model,
        )
        summary_cache = SummaryCache(
            Path(settings.data_dir).expanduser() / "context_summaries.sqlite",
        )

        # Dynamic curator selection: prefer already-hot idle models
        # (especially pinned-but-idle) over cold-loading the default.
        # See CuratorSelector docstring for the full ranking policy.
        curator_selector = None
        resolve_curator_context = None
        idle_window = getattr(settings, "context_compaction_idle_window_s", 120)
        if idle_window > 0:
            curator_selector = CuratorSelector(
                default_model=settings.context_compaction_model,
                idle_window_s=idle_window,
                min_params_b=getattr(
                    settings, "context_compaction_curator_min_params_b", 7.0,
                ),
            )

            # Late-bind: resolved fresh each compaction so selector sees
            # current fleet state (hot models, activity, pin toggles).
            async def resolve_curator_context():
                env_pins = _parse_pinned_models(
                    getattr(settings, "pinned_models", ""),
                )
                per_node_pins = app.state.pinned_store.load()
                activity = await trace_store.get_request_count_by_model(
                    seconds=idle_window,
                )
                return {
                    "nodes": registry.get_online_nodes(),
                    "env_pins": env_pins,
                    "per_node_pins": per_node_pins,
                    "activity": activity,
                }

        context_compactor = ContextCompactor(
            curator=curator,
            cache=summary_cache,
            budget_tokens=settings.context_compaction_budget_tokens,
            preserve_last_turns=settings.context_compaction_preserve_turns,
            curator_selector=curator_selector,
            resolve_curator_context=resolve_curator_context,
        )
        logger.info(
            f"Context Compactor enabled "
            f"(default curator={settings.context_compaction_model}, "
            f"dynamic selection={'on' if curator_selector else 'off'}, "
            f"idle_window={idle_window}s, "
            f"budget={settings.context_compaction_budget_tokens} tokens, "
            f"preserve={settings.context_compaction_preserve_turns} turns)"
        )

    # Store on app state
    app.state.registry = registry
    app.state.scorer = scorer
    app.state.queue_mgr = queue_mgr
    app.state.streaming_proxy = streaming_proxy
    app.state.mlx_proxy = mlx_proxy
    app.state.context_compactor = context_compactor
    app.state.pinned_store = PinnedModelsStore(
        Path(settings.data_dir).expanduser() / "pinned_models.json",
    )
    # Stable-cut Layer-1 clearing state.  Persistent set of tool_use_ids
    # whose tool_results have been cleared — once in, stays in.  Preserves
    # MLX prefix-cache stability across turns.  See
    # ``server/clearing_store.py`` for the full rationale.
    from fleet_manager.server.clearing_store import ClearingStore

    app.state.clearing_store = ClearingStore(
        Path(settings.data_dir).expanduser() / "cleared_tool_uses.sqlite",
    )
    app.state.latency_store = latency_store
    app.state.trace_store = trace_store
    from fleet_manager.server.context_optimizer import ContextOptimizer

    context_optimizer = ContextOptimizer(settings, registry, trace_store)
    app.state.rebalancer = rebalancer
    app.state.context_optimizer = context_optimizer

    # Start mDNS advertisement
    advertiser = FleetServiceAdvertiser(settings.port, settings.mdns_service_name)
    await advertiser.start()

    # Start background tasks
    monitor_task = asyncio.create_task(registry.monitor_heartbeats())
    rebalancer_task = asyncio.create_task(rebalancer.run())
    optimizer_task = asyncio.create_task(context_optimizer.run())
    queue_mgr.start_reaper()

    # Priority model preloader — loads most-used models after first node registers
    from fleet_manager.server.model_preloader import preload_priority_models

    preload_task = asyncio.create_task(
        preload_priority_models(
            registry, trace_store, streaming_proxy, settings,
            pinned_store=app.state.pinned_store,
        )
    )

    logger.info(f"Ollama Herd ready on port {settings.port}")

    yield

    # Shutdown
    monitor_task.cancel()
    rebalancer_task.cancel()
    optimizer_task.cancel()
    preload_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor_task
    with contextlib.suppress(asyncio.CancelledError):
        await rebalancer_task
    with contextlib.suppress(asyncio.CancelledError):
        await optimizer_task
    await queue_mgr.shutdown()
    await streaming_proxy.close()
    if mlx_proxy is not None:
        await mlx_proxy.close()
    await advertiser.stop()
    await trace_store.close()
    await latency_store.close()
    logger.info("Ollama Herd shut down")


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    if settings is None:
        settings = ServerSettings()

    app = FastAPI(
        title="Ollama Herd",
        description="Smart inference router that herds your Ollama instances into one endpoint",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Register routes
    from fleet_manager.server.routes import (
        anthropic_compat,
        dashboard,
        embedding_compat,
        fleet,
        heartbeat,
        image_compat,
        ollama_compat,
        openai_compat,
        platform,
        transcription_compat,
    )

    app.include_router(heartbeat.router)
    app.include_router(openai_compat.router)
    app.include_router(ollama_compat.router)
    app.include_router(anthropic_compat.router)
    app.include_router(image_compat.router)
    app.include_router(transcription_compat.router)
    app.include_router(embedding_compat.router)
    app.include_router(platform.router)
    app.include_router(fleet.router)
    app.include_router(dashboard.router)

    @app.get("/")
    async def root():
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/dashboard")

    return app
