"""Integration tests for API routes using TestClient."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    CpuMetrics,
    LoadedModel,
    MemoryMetrics,
    MemoryPressure,
    OllamaMetrics,
)
from fleet_manager.server.latency_store import LatencyStore
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.server.streaming import StreamingProxy
from fleet_manager.server.trace_store import TraceStore

from tests.conftest import make_heartbeat


def _seed_latency_db(db_path: str) -> None:
    """Pre-populate a latency.db with sample data using sync sqlite3.

    This lets us test dashboard API endpoints that query the LatencyStore
    without needing to call async methods from synchronous tests.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS latency_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            latency_ms REAL NOT NULL,
            tokens_generated INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            timestamp REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_latency_node_model "
        "ON latency_observations(node_id, model_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_latency_timestamp "
        "ON latency_observations(timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_latency_model_timestamp "
        "ON latency_observations(model_name, timestamp)"
    )
    now = time.time()
    rows = [
        ("studio", "phi4:14b", 500.0, None, 50, 200, now),
        ("studio", "phi4:14b", 600.0, None, 60, 300, now - 100),
        ("studio", "llama3.3:70b", 3000.0, None, 100, 500, now - 200),
    ]
    conn.executemany(
        "INSERT INTO latency_observations "
        "(node_id, model_name, latency_ms, tokens_generated, "
        "prompt_tokens, completion_tokens, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def create_test_app(tmp_path=None) -> FastAPI:
    """Create a test app without mDNS or rebalancer.

    If *tmp_path* is provided, a LatencyStore is initialised so that
    dashboard API endpoints return real data instead of empty defaults.
    """
    settings = ServerSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = NodeRegistry(settings)
        scorer = ScoringEngine(settings, registry)
        queue_mgr = QueueManager()
        streaming_proxy = StreamingProxy(registry)

        app.state.settings = settings
        app.state.registry = registry
        app.state.scorer = scorer
        app.state.queue_mgr = queue_mgr
        app.state.streaming_proxy = streaming_proxy

        if tmp_path is not None:
            store = LatencyStore(data_dir=str(tmp_path))
            await store.initialize()
            app.state.latency_store = store
            trace_store = TraceStore(data_dir=str(tmp_path))
            await trace_store.initialize()
            app.state.trace_store = trace_store

        yield

        if tmp_path is not None and hasattr(app.state, "latency_store"):
            await app.state.latency_store.close()
        if tmp_path is not None and hasattr(app.state, "trace_store"):
            await app.state.trace_store.close()
        await queue_mgr.shutdown()
        await streaming_proxy.close()

    app = FastAPI(lifespan=lifespan)

    from fleet_manager.server.routes import dashboard, fleet, heartbeat, ollama_compat, openai_compat

    app.include_router(heartbeat.router)
    app.include_router(openai_compat.router)
    app.include_router(ollama_compat.router)
    app.include_router(fleet.router)
    app.include_router(dashboard.router)

    @app.get("/")
    async def root():
        return {"name": "Ollama Herd", "version": "0.1.0"}

    return app


@pytest.fixture
def client():
    app = create_test_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_with_store(tmp_path):
    """TestClient with a pre-seeded LatencyStore on app.state."""
    _seed_latency_db(str(tmp_path / "latency.db"))
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as c:
        yield c


class TestRootEndpoint:
    def test_root(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Ollama Herd"


class TestHeartbeatRoute:
    def test_heartbeat_register(self, client):
        hb = make_heartbeat(node_id="test-node").model_dump()
        resp = client.post("/heartbeat", json=hb)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["node_status"] == "online"

    def test_heartbeat_drain(self, client):
        # First register
        hb = make_heartbeat(node_id="draining-node").model_dump()
        client.post("/heartbeat", json=hb)

        # Then drain
        resp = client.post("/heartbeat", json={"node_id": "draining-node", "draining": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "draining"

    def test_heartbeat_updates_metrics(self, client):
        hb1 = make_heartbeat(node_id="studio", cpu_pct=10.0).model_dump()
        client.post("/heartbeat", json=hb1)

        hb2 = make_heartbeat(node_id="studio", cpu_pct=90.0).model_dump()
        client.post("/heartbeat", json=hb2)

        resp = client.get("/fleet/status")
        nodes = resp.json()["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["cpu"]["utilization_pct"] == 90.0


class TestFleetStatus:
    def test_empty_fleet(self, client):
        resp = client.get("/fleet/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fleet"]["nodes_total"] == 0
        assert data["fleet"]["nodes_online"] == 0

    def test_fleet_with_nodes(self, client):
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[("phi4:14b", 9.0)],
            available_models=["phi4:14b", "llama3.3:70b"],
        ).model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/fleet/status")
        data = resp.json()
        assert data["fleet"]["nodes_total"] == 1
        assert data["fleet"]["nodes_online"] == 1
        assert data["fleet"]["models_loaded"] == 1
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["node_id"] == "studio"


class TestOpenAICompat:
    def test_list_models_empty(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert data["data"] == []

    def test_list_models_with_node(self, client):
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[("phi4:14b", 9.0)],
            available_models=["phi4:14b", "llama3.3:70b"],
        ).model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/v1/models")
        data = resp.json()
        model_ids = [m["id"] for m in data["data"]]
        assert "phi4:14b" in model_ids
        assert "llama3.3:70b" in model_ids

    def test_chat_completions_no_model(self, client):
        resp = client.post("/v1/chat/completions", json={"messages": []})
        assert resp.status_code == 400

    def test_chat_completions_model_not_found(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "nonexistent:999b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "not available" in data["error"]["message"]


class TestOllamaCompat:
    def test_tags_empty(self, client):
        resp = client.get("/api/tags")
        assert resp.status_code == 200
        assert resp.json()["models"] == []

    def test_tags_with_models(self, client):
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[("phi4:14b", 9.0)],
            available_models=["phi4:14b"],
        ).model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/api/tags")
        models = resp.json()["models"]
        assert len(models) >= 1
        names = [m["name"] for m in models]
        assert "phi4:14b" in names

    def test_ps_empty(self, client):
        resp = client.get("/api/ps")
        assert resp.status_code == 200
        assert resp.json()["models"] == []

    def test_ps_with_loaded_models(self, client):
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[("phi4:14b", 9.0), ("qwen2.5:0.5b", 0.4)],
        ).model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/api/ps")
        models = resp.json()["models"]
        assert len(models) == 2
        assert all(m["fleet_node"] == "studio" for m in models)

    def test_chat_no_model(self, client):
        resp = client.post("/api/chat", json={"messages": []})
        assert resp.status_code == 400

    def test_chat_model_not_found(self, client):
        resp = client.post(
            "/api/chat",
            json={"model": "nonexistent:999b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404

    def test_generate_no_model(self, client):
        resp = client.post("/api/generate", json={"prompt": "hello"})
        assert resp.status_code == 400

    def test_generate_model_not_found(self, client):
        resp = client.post(
            "/api/generate",
            json={"model": "nonexistent:999b", "prompt": "hello"},
        )
        assert resp.status_code == 404


class TestDashboard:
    def test_dashboard_html(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Ollama Herd" in resp.text
        assert "EventSource" in resp.text

    def test_dashboard_events_endpoint_exists(self, client):
        # We can't easily test the infinite SSE stream with a sync TestClient,
        # but we can verify the endpoint is registered and responds correctly.
        # The SSE stream was verified via manual curl testing.
        # Verify the endpoint is at least reachable by checking /dashboard HTML
        # includes the correct EventSource URL.
        resp = client.get("/dashboard")
        assert "/dashboard/events" in resp.text

    def test_trends_page_html(self, client):
        resp = client.get("/dashboard/trends")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "chart.js" in resp.text
        assert "Trends" in resp.text

    def test_models_page_html(self, client):
        resp = client.get("/dashboard/models")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "chart.js" in resp.text
        assert "Model Insights" in resp.text

    def test_apps_page_html(self, client):
        resp = client.get("/dashboard/apps")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "chart.js" in resp.text
        assert "Apps" in resp.text
        assert "X-Herd-Tags" in resp.text  # Usage instructions

    def test_benchmarks_page_html(self, client):
        resp = client.get("/dashboard/benchmarks")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "chart.js" in resp.text
        assert "Benchmarks" in resp.text
        assert "Capacity Growth" in resp.text

    def test_health_page_html(self, client):
        resp = client.get("/dashboard/health")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Health" in resp.text
        assert "Fleet Vitals" in resp.text
        assert "Recommendations" in resp.text

    def test_settings_page_html(self, client):
        resp = client.get("/dashboard/settings")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Settings" in resp.text
        assert "Feature Toggles" in resp.text
        assert "Fleet Nodes" in resp.text

    def test_all_pages_have_nav(self, client):
        """All dashboard pages include navigation links to all tabs."""
        for path in ("/dashboard", "/dashboard/trends", "/dashboard/models", "/dashboard/apps", "/dashboard/benchmarks", "/dashboard/health", "/dashboard/settings"):
            resp = client.get(path)
            assert resp.status_code == 200
            assert "/dashboard" in resp.text
            assert "/dashboard/trends" in resp.text
            assert "/dashboard/models" in resp.text
            assert "/dashboard/apps" in resp.text
            assert "/dashboard/benchmarks" in resp.text
            assert "/dashboard/health" in resp.text
            assert "/dashboard/settings" in resp.text
            # Nav tab labels
            assert "Dashboard" in resp.text
            assert "Trends" in resp.text
            assert "Model Insights" in resp.text
            assert "Apps" in resp.text
            assert "Benchmarks" in resp.text
            assert "Health" in resp.text
            assert "Settings" in resp.text


class TestDashboardAPI:
    """Tests for the dashboard JSON data endpoints."""

    def test_trends_api_empty(self, client):
        """Trends API returns empty data when no latency store is set."""
        resp = client.get("/dashboard/api/trends")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert data["data"] == []
        assert "hours" in data

    def test_trends_api_custom_hours(self, client):
        resp = client.get("/dashboard/api/trends?hours=24")
        data = resp.json()
        assert data["hours"] == 24

    def test_models_api_empty(self, client):
        """Models API returns empty arrays when no latency store is set."""
        resp = client.get("/dashboard/api/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "daily" in data
        assert "summary" in data
        assert data["daily"] == []
        assert data["summary"] == []

    def test_overview_api_empty(self, client):
        """Overview API returns zeroes when no latency store is set."""
        resp = client.get("/dashboard/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 0
        assert data["total_prompt_tokens"] == 0
        assert data["total_completion_tokens"] == 0
        assert data["total_tokens"] == 0
        assert data["models_count"] == 0

    def test_trends_api_with_data(self, client_with_store):
        """Trends API returns real data when store has observations."""
        resp = client_with_store.get("/dashboard/api/trends?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) >= 1
        bucket = data["data"][0]
        assert bucket["request_count"] >= 1
        assert bucket["avg_latency_ms"] > 0

    def test_models_api_with_data(self, client_with_store):
        """Models API returns per-model stats when store has observations."""
        resp = client_with_store.get("/dashboard/api/models?days=7")
        data = resp.json()
        assert len(data["summary"]) == 2
        models = {m["model_name"] for m in data["summary"]}
        assert "phi4:14b" in models
        assert "llama3.3:70b" in models
        # daily should also have entries
        assert len(data["daily"]) >= 1

    def test_overview_api_with_data(self, client_with_store):
        """Overview API aggregates totals from all models."""
        resp = client_with_store.get("/dashboard/api/overview")
        data = resp.json()
        # Seeded: 2 phi4 records (50+60=110 prompt, 200+300=500 completion)
        #         1 llama record (100 prompt, 500 completion)
        assert data["total_requests"] == 3
        assert data["total_prompt_tokens"] == 210
        assert data["total_completion_tokens"] == 1000
        assert data["total_tokens"] == 1210
        assert data["models_count"] == 2

    def test_health_api_empty(self, client):
        """Health API returns a valid report even with no data."""
        resp = client.get("/dashboard/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "vitals" in data
        assert "recommendations" in data
        assert data["vitals"]["health_score"] <= 100
        assert isinstance(data["recommendations"], list)

    def test_health_api_with_node(self, client):
        """Health API detects issues when a node is registered."""
        from tests.conftest import make_heartbeat

        hb = make_heartbeat(
            node_id="studio",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[("phi4:14b", 9.0)],
        ).model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/dashboard/api/health")
        data = resp.json()
        assert data["vitals"]["nodes_total"] == 1
        assert data["vitals"]["nodes_online"] == 1
        # With 108GB free and only 1 model, should get underutilized memory info
        check_ids = [r["check_id"] for r in data["recommendations"]]
        assert "underutilized_memory" in check_ids

    def test_settings_api_returns_config(self, client):
        resp = client.get("/dashboard/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "router_version" in data
        assert "config" in data
        assert "nodes" in data
        assert "toggles" in data["config"]
        assert data["config"]["toggles"]["auto_pull"] is True
        assert data["config"]["toggles"]["vram_fallback"] is True

    def test_settings_api_toggle_auto_pull(self, client):
        resp = client.get("/dashboard/api/settings")
        assert resp.json()["config"]["toggles"]["auto_pull"] is True

        resp = client.post("/dashboard/api/settings", json={"auto_pull": False})
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"
        assert resp.json()["updated"]["auto_pull"] is False

        resp = client.get("/dashboard/api/settings")
        assert resp.json()["config"]["toggles"]["auto_pull"] is False

    def test_settings_api_toggle_vram_fallback(self, client):
        resp = client.post("/dashboard/api/settings", json={"vram_fallback": False})
        assert resp.status_code == 200
        assert resp.json()["updated"]["vram_fallback"] is False

        resp = client.get("/dashboard/api/settings")
        assert resp.json()["config"]["toggles"]["vram_fallback"] is False

    def test_settings_api_rejects_non_mutable(self, client):
        resp = client.post("/dashboard/api/settings", json={"score_model_hot": 999.0})
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_change"

        resp = client.get("/dashboard/api/settings")
        assert resp.json()["config"]["scoring"]["score_model_hot"] == 50.0

    def test_settings_api_nodes_list(self, client):
        from tests.conftest import make_heartbeat

        hb = make_heartbeat(node_id="test-node", agent_version="0.1.0").model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/dashboard/api/settings")
        nodes = resp.json()["nodes"]
        assert len(nodes) >= 1
        node = next(n for n in nodes if n["node_id"] == "test-node")
        assert node["agent_version"] == "0.1.0"
        assert node["status"] == "online"

    def test_settings_api_empty_nodes(self, client):
        resp = client.get("/dashboard/api/settings")
        assert resp.json()["nodes"] == []


class TestFallbackRoutes:
    """Test model fallback behavior through the route layer."""

    def test_fallback_models_field_optional(self, client):
        """Requests without fallback_models still work (backward compat)."""
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "nonexistent:99b", "messages": [{"role": "user", "content": "hi"}]},
        )
        # Should get 404 (model not found), not a crash
        assert resp.status_code == 404

    def test_no_fallback_all_missing_404(self, client):
        """When primary + fallback models all missing, return 404."""
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent:99b",
                "fallback_models": ["also-gone:99b"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 404
        assert "nonexistent:99b" in resp.json()["error"]["message"]
        assert "also-gone:99b" in resp.json()["error"]["message"]

    def test_ollama_fallback_all_missing_404(self, client):
        """Ollama endpoint also returns 404 listing all tried models."""
        resp = client.post(
            "/api/chat",
            json={
                "model": "gone:99b",
                "fallback_models": ["also-gone:99b"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 404
        assert "gone:99b" in resp.json()["error"]


class TestUsageAndTracesAPI:
    """Test the new /dashboard/api/usage and /dashboard/api/traces endpoints."""

    def test_usage_api_empty(self, client):
        """Usage API returns empty data when no trace store."""
        resp = client.get("/dashboard/api/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"] == []

    def test_traces_api_empty(self, client):
        """Traces API returns empty list when no trace store."""
        resp = client.get("/dashboard/api/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["traces"] == []

    def test_usage_api_with_store(self, client_with_store):
        """Usage API returns data structure when store exists."""
        resp = client_with_store.get("/dashboard/api/usage?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "days" in data
        assert "data" in data

    def test_traces_api_with_store(self, client_with_store):
        """Traces API returns data structure when store exists."""
        resp = client_with_store.get("/dashboard/api/traces?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "traces" in data

    def test_apps_api_empty(self, client):
        """Apps API returns empty data when no trace store."""
        resp = client.get("/dashboard/api/apps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"] == []
        assert data["summary"] == []

    def test_apps_daily_api_empty(self, client):
        """Apps daily API returns empty data when no trace store."""
        resp = client.get("/dashboard/api/apps/daily")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"] == []

    def test_apps_api_with_store(self, client_with_store):
        """Apps API returns data structure when store exists."""
        resp = client_with_store.get("/dashboard/api/apps?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert "summary" in data
        assert "days" in data

    def test_benchmarks_api_empty(self, client):
        """Benchmarks API returns empty list when no trace store."""
        resp = client.get("/dashboard/api/benchmarks")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_benchmarks_api_save_and_list(self, client_with_store):
        """POST and GET benchmark results."""
        bench = {
            "run_id": "test-bench-001",
            "timestamp": 1700000000,
            "duration_s": 60,
            "total_requests": 10,
            "total_failures": 1,
            "total_prompt_tokens": 500,
            "total_completion_tokens": 2000,
            "requests_per_sec": 0.17,
            "tokens_per_sec": 33.3,
        }
        resp = client_with_store.post("/dashboard/api/benchmarks", json=bench)
        assert resp.status_code == 200
        assert resp.json()["status"] == "saved"

        resp = client_with_store.get("/dashboard/api/benchmarks")
        assert resp.status_code == 200
        runs = resp.json()["data"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == "test-bench-001"

    def test_benchmarks_api_detail(self, client_with_store):
        """GET single benchmark by run_id."""
        bench = {
            "run_id": "detail-test",
            "timestamp": 1700000000,
            "duration_s": 30,
            "total_requests": 5,
            "total_failures": 0,
            "total_prompt_tokens": 200,
            "total_completion_tokens": 1000,
        }
        client_with_store.post("/dashboard/api/benchmarks", json=bench)
        resp = client_with_store.get("/dashboard/api/benchmarks/detail-test")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data is not None
        assert data["run_id"] == "detail-test"
        assert data["total_requests"] == 5


class TestTagExtraction:
    """Test the extract_tags helper function."""

    def test_tags_from_metadata_body(self):
        from fleet_manager.server.routes.routing import extract_tags

        tags = extract_tags({"metadata": {"tags": ["app-1", "prod"]}})
        assert tags == ["app-1", "prod"]

    def test_tags_from_header(self):
        from fleet_manager.server.routes.routing import extract_tags

        class FakeHeaders:
            def get(self, key, default=""):
                if key == "x-herd-tags":
                    return "app-1, staging"
                return default

        tags = extract_tags({}, FakeHeaders())
        assert tags == ["app-1", "staging"]

    def test_tags_from_user_field(self):
        from fleet_manager.server.routes.routing import extract_tags

        tags = extract_tags({"user": "alice"})
        assert tags == ["user:alice"]

    def test_tags_merged_and_deduplicated(self):
        from fleet_manager.server.routes.routing import extract_tags

        class FakeHeaders:
            def get(self, key, default=""):
                if key == "x-herd-tags":
                    return "app-1, extra"
                return default

        body = {
            "metadata": {"tags": ["app-1", "prod"]},
            "user": "bob",
        }
        tags = extract_tags(body, FakeHeaders())
        # app-1 from body, prod from body, extra from header (app-1 deduped), user:bob
        assert tags == ["app-1", "prod", "extra", "user:bob"]

    def test_tags_empty_when_no_sources(self):
        from fleet_manager.server.routes.routing import extract_tags

        tags = extract_tags({"model": "phi4:14b", "messages": []})
        assert tags == []

    def test_tags_with_invalid_metadata(self):
        from fleet_manager.server.routes.routing import extract_tags

        tags = extract_tags({"metadata": "not-a-dict"})
        assert tags == []
