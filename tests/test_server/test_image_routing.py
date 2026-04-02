"""Tests for image generation routing."""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import ImageMetrics, ImageModel
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.server.streaming import StreamingProxy
from tests.conftest import make_heartbeat, make_node


def _create_image_test_app() -> FastAPI:
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

        yield

        await queue_mgr.shutdown()
        await streaming_proxy.close()

    app = FastAPI(lifespan=lifespan)

    from fleet_manager.server.routes import dashboard, heartbeat, image_compat

    app.include_router(heartbeat.router)
    app.include_router(image_compat.router)
    app.include_router(dashboard.router)

    return app


@pytest.fixture
def client():
    app = _create_image_test_app()
    with TestClient(app) as c:
        yield c


class TestImageRoute:
    """Tests for the /api/generate-image endpoint."""

    def test_image_disabled_returns_503(self, client):
        # Disable first (defaults to True now)
        client.post("/dashboard/api/settings", json={"image_generation": False})
        resp = client.post(
            "/api/generate-image",
            json={"model": "z-image-turbo", "prompt": "a cat"},
        )
        assert resp.status_code == 503
        assert "disabled" in resp.json()["error"]
        client.post("/dashboard/api/settings", json={"image_generation": True})

    def test_image_enabled_no_nodes_returns_404(self, client):
        client.post(
            "/dashboard/api/settings",
            json={"image_generation": True},
        )
        resp = client.post(
            "/api/generate-image",
            json={"model": "z-image-turbo", "prompt": "a cat"},
        )
        assert resp.status_code == 404
        assert "not available" in resp.json()["error"]

    def test_image_missing_prompt_returns_400(self, client):
        client.post(
            "/dashboard/api/settings",
            json={"image_generation": True},
        )
        resp = client.post(
            "/api/generate-image",
            json={"model": "z-image-turbo"},
        )
        assert resp.status_code == 400
        assert "prompt" in resp.json()["error"]

    def test_image_missing_model_returns_400(self, client):
        client.post(
            "/dashboard/api/settings",
            json={"image_generation": True},
        )
        resp = client.post(
            "/api/generate-image",
            json={"prompt": "a cat"},
        )
        assert resp.status_code == 400
        assert "model" in resp.json()["error"]

    def test_image_node_with_model_selected(self, client):
        """Node with image model and image_port is found as candidate."""
        client.post(
            "/dashboard/api/settings",
            json={"image_generation": True},
        )
        # Register a node with image capabilities
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[("phi4:14b", 9.0)],
        ).model_dump()
        hb["image"] = {
            "models_available": [
                {"name": "z-image-turbo", "binary": "mflux-generate-z-image-turbo"}
            ],
            "generating": False,
        }
        hb["image_port"] = 11436
        client.post("/heartbeat", json=hb)

        # Request will fail at proxy level (no actual image server),
        # but we can verify it got past routing (502, not 404)
        resp = client.post(
            "/api/generate-image",
            json={"model": "z-image-turbo", "prompt": "a cat"},
        )
        # Should get 502 (proxy error) not 404 (model not found)
        assert resp.status_code == 502

    def test_image_lists_available_models_in_404(self, client):
        """404 response includes list of available image models."""
        client.post(
            "/dashboard/api/settings",
            json={"image_generation": True},
        )
        hb = make_heartbeat(node_id="studio").model_dump()
        hb["image"] = {
            "models_available": [
                {"name": "z-image-turbo", "binary": "mflux-generate-z-image-turbo"}
            ],
            "generating": False,
        }
        hb["image_port"] = 11436
        client.post("/heartbeat", json=hb)

        resp = client.post(
            "/api/generate-image",
            json={"model": "nonexistent-model", "prompt": "test"},
        )
        assert resp.status_code == 404
        assert "z-image-turbo" in resp.json()["error"]


class TestImageScoring:
    """Tests for image candidate scoring."""

    def test_prefer_non_generating_node(self):
        from fleet_manager.server.routes.image_compat import _score_image_candidates

        idle_node = make_node(
            node_id="idle",
            memory_total=64.0,
            memory_used=20.0,
        )
        idle_node.image = ImageMetrics(
            models_available=[ImageModel(name="z-image-turbo", binary="mflux")],
            generating=False,
        )

        busy_node = make_node(
            node_id="busy",
            memory_total=128.0,
            memory_used=20.0,
        )
        busy_node.image = ImageMetrics(
            models_available=[ImageModel(name="z-image-turbo", binary="mflux")],
            generating=True,
        )

        best = _score_image_candidates([busy_node, idle_node], None)
        assert best.node_id == "idle"

    def test_prefer_more_memory(self):
        from fleet_manager.server.routes.image_compat import _score_image_candidates

        small_node = make_node(
            node_id="small",
            memory_total=32.0,
            memory_used=20.0,
        )
        small_node.image = ImageMetrics(
            models_available=[ImageModel(name="z-image-turbo", binary="mflux")],
        )

        big_node = make_node(
            node_id="big",
            memory_total=128.0,
            memory_used=20.0,
        )
        big_node.image = ImageMetrics(
            models_available=[ImageModel(name="z-image-turbo", binary="mflux")],
        )

        best = _score_image_candidates([small_node, big_node], None)
        assert best.node_id == "big"


class TestImageDetection:
    """Tests for mflux detection in collector."""

    def test_detect_no_mflux(self, monkeypatch):
        from fleet_manager.node.collector import _detect_image_models

        monkeypatch.setattr(
            "fleet_manager.node.collector._which_extended", lambda _: None
        )
        result = _detect_image_models()
        assert result is None

    def test_detect_z_image_turbo(self, monkeypatch):
        from fleet_manager.node.collector import _detect_image_models

        def fake_which(name):
            if name == "mflux-generate-z-image-turbo":
                return "/usr/local/bin/mflux-generate-z-image-turbo"
            return None

        monkeypatch.setattr(
            "fleet_manager.node.collector._which_extended", fake_which
        )
        # Mock psutil to avoid real process scanning
        monkeypatch.setattr(
            "fleet_manager.node.collector.psutil",
            type("FakePsutil", (), {"process_iter": staticmethod(lambda attrs: [])}),
            raising=False,
        )
        result = _detect_image_models()
        assert result is not None
        assert len(result.models_available) == 1
        assert result.models_available[0].name == "z-image-turbo"
        assert result.generating is False


class TestImageSettings:
    """Tests for image generation settings toggle."""

    def test_settings_includes_image_toggle(self, client):
        resp = client.get("/dashboard/api/settings")
        data = resp.json()
        toggles = data["config"]["toggles"]
        assert "image_generation" in toggles
        assert data["config"]["toggles"]["image_generation"] is True

    def test_toggle_image_generation(self, client):
        resp = client.post(
            "/dashboard/api/settings",
            json={"image_generation": True},
        )
        assert resp.json()["status"] == "updated"
        assert resp.json()["updated"]["image_generation"] is True


class TestImageTracking:
    """Tests for image generation event tracking."""

    def test_record_and_retrieve_events(self):
        from fleet_manager.server.routes.image_compat import (
            _image_gen_events,
            _record_image_gen,
            get_image_gen_events,
        )

        # Clear state
        _image_gen_events.clear()

        _record_image_gen("z-image-turbo", "studio", "completed", 15000, 1024, 1024)
        _record_image_gen("z-image-turbo", "studio", "failed", 5000, 512, 512, "timeout")

        events = get_image_gen_events(hours=1)
        assert len(events) == 2
        assert events[0]["status"] == "completed"
        assert events[0]["generation_ms"] == 15000
        assert events[1]["status"] == "failed"
        assert events[1]["error"] == "timeout"

        _image_gen_events.clear()

    def test_image_stats_api(self, client):
        resp = client.get("/dashboard/api/image-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "completed" in data
        assert "failed" in data
        assert "by_node" in data


class TestImageHealthCheck:
    """Tests for image generation health check."""

    def test_no_events_no_recommendation(self):
        from fleet_manager.server.routes.image_compat import _image_gen_events

        _image_gen_events.clear()

        from fleet_manager.server.health_engine import HealthEngine

        engine = HealthEngine()
        recs = engine._check_image_generation([])
        assert len(recs) == 0

        _image_gen_events.clear()

    def test_events_produce_recommendation(self):
        from fleet_manager.server.routes.image_compat import (
            _image_gen_events,
            _record_image_gen,
        )

        _image_gen_events.clear()
        for _ in range(5):
            _record_image_gen("z-image-turbo", "studio", "completed", 15000, 1024, 1024)

        from fleet_manager.server.health_engine import HealthEngine

        engine = HealthEngine()
        recs = engine._check_image_generation([])
        assert len(recs) >= 1
        assert recs[0].check_id == "image_generation"
        assert "5 images generated" in recs[0].description

        _image_gen_events.clear()

    def test_mflux_expansion_recommendation(self):
        from fleet_manager.server.routes.image_compat import (
            _image_gen_events,
            _record_image_gen,
        )

        _image_gen_events.clear()
        for _ in range(5):
            _record_image_gen("z-image-turbo", "studio", "completed", 15000, 1024, 1024)

        # Node with mflux
        node_with = make_node(node_id="studio", memory_total=64, memory_used=20)
        node_with.image = ImageMetrics(
            models_available=[ImageModel(name="z-image-turbo", binary="mflux")],
        )
        # Node without mflux but with enough memory
        node_without = make_node(node_id="mini", memory_total=32, memory_used=10)

        from fleet_manager.server.health_engine import HealthEngine

        engine = HealthEngine()
        recs = engine._check_image_generation([node_with, node_without])

        check_ids = [r.check_id for r in recs]
        assert "mflux_expansion" in check_ids
        expansion = next(r for r in recs if r.check_id == "mflux_expansion")
        assert "mini" in expansion.description

        _image_gen_events.clear()
