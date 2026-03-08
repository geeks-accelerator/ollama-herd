"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from fleet_manager.common.discovery import FleetServiceAdvertiser
from fleet_manager.common.logging_config import setup_logging
from fleet_manager.models.config import ServerSettings
from fleet_manager.server.latency_store import LatencyStore
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
    queue_mgr = QueueManager(registry=registry)
    streaming_proxy = StreamingProxy(registry, latency_store, trace_store)
    rebalancer = Rebalancer(settings, registry, scorer, queue_mgr, streaming_proxy)

    # Store on app state
    app.state.registry = registry
    app.state.scorer = scorer
    app.state.queue_mgr = queue_mgr
    app.state.streaming_proxy = streaming_proxy
    app.state.latency_store = latency_store
    app.state.trace_store = trace_store
    app.state.rebalancer = rebalancer

    # Start mDNS advertisement
    advertiser = FleetServiceAdvertiser(settings.port, settings.mdns_service_name)
    await advertiser.start()

    # Start background tasks
    monitor_task = asyncio.create_task(registry.monitor_heartbeats())
    rebalancer_task = asyncio.create_task(rebalancer.run())

    logger.info(f"Ollama Herd ready on port {settings.port}")

    yield

    # Shutdown
    monitor_task.cancel()
    rebalancer_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    try:
        await rebalancer_task
    except asyncio.CancelledError:
        pass
    await queue_mgr.shutdown()
    await streaming_proxy.close()
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
    from fleet_manager.server.routes import dashboard, fleet, heartbeat, ollama_compat, openai_compat

    app.include_router(heartbeat.router)
    app.include_router(openai_compat.router)
    app.include_router(ollama_compat.router)
    app.include_router(fleet.router)
    app.include_router(dashboard.router)

    @app.get("/")
    async def root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/dashboard")

    return app
