"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fleet_manager.common.discovery import FleetServiceAdvertiser
from fleet_manager.common.logging_config import setup_logging
from fleet_manager.models.config import ServerSettings
from fleet_manager.server.latency_store import LatencyStore
from fleet_manager.server.mlx_proxy import MlxProxy
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
    mlx_proxy: MlxProxy | None = None
    if getattr(settings, "mlx_enabled", False):
        mlx_proxy = MlxProxy(
            settings.mlx_url,
            trace_store=trace_store,
            max_queue_depth=getattr(settings, "mlx_max_queue_depth", 3),
            retry_after_seconds=getattr(settings, "mlx_retry_after_seconds", 10),
        )
        logger.info(
            f"MLX backend enabled at {settings.mlx_url} "
            f"(admission: 1 in-flight + {mlx_proxy.max_queue_depth} queued max)"
        )

    # Store on app state
    app.state.registry = registry
    app.state.scorer = scorer
    app.state.queue_mgr = queue_mgr
    app.state.streaming_proxy = streaming_proxy
    app.state.mlx_proxy = mlx_proxy
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
        preload_priority_models(registry, trace_store, streaming_proxy, settings)
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
