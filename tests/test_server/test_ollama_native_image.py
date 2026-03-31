"""Tests for Ollama native image generation integration."""

import pytest

from fleet_manager.server.model_knowledge import (
    ModelCategory,
    classify_model,
    is_image_model,
    lookup_model,
)


class TestImageModelDetection:
    """Tests for is_image_model() and classify_model() with image models."""

    def test_is_image_model_ollama_native(self):
        assert is_image_model("x/z-image-turbo") is True

    def test_is_image_model_flux2_klein(self):
        assert is_image_model("x/flux2-klein") is True

    def test_is_image_model_flux2_klein_9b(self):
        assert is_image_model("x/flux2-klein:9b") is True

    def test_is_not_image_model_llm(self):
        assert is_image_model("llama3.3:70b") is False

    def test_is_not_image_model_deepseek(self):
        assert is_image_model("deepseek-r1:70b") is False

    def test_classify_ollama_native_image(self):
        assert classify_model("x/z-image-turbo") == ModelCategory.IMAGE

    def test_classify_flux2_klein(self):
        assert classify_model("x/flux2-klein") == ModelCategory.IMAGE

    def test_classify_unknown_x_prefix(self):
        """Unknown x/ prefix models should still classify as IMAGE."""
        assert classify_model("x/some-future-model") == ModelCategory.IMAGE

    def test_classify_x_prefix_heuristic(self):
        """Unknown x/ prefix models classify as IMAGE via heuristic."""
        assert classify_model("x/flux-new") == ModelCategory.IMAGE
        assert classify_model("x/sdxl-base") == ModelCategory.IMAGE
        assert classify_model("x/stable-diffusion-3") == ModelCategory.IMAGE

    def test_mflux_z_image_turbo_is_not_ollama_image(self):
        """mflux's z-image-turbo (no x/ prefix) should NOT classify as image."""
        assert is_image_model("z-image-turbo") is False

    def test_lookup_z_image_turbo(self):
        spec = lookup_model("x/z-image-turbo")
        assert spec is not None
        assert spec.category == ModelCategory.IMAGE
        assert spec.params_b == 6.0

    def test_lookup_flux2_klein(self):
        spec = lookup_model("x/flux2-klein")
        assert spec is not None
        assert spec.category == ModelCategory.IMAGE
        assert spec.params_b == 4.0

    def test_lookup_flux2_klein_9b(self):
        spec = lookup_model("x/flux2-klein:9b")
        assert spec is not None
        assert spec.params_b == 9.0

    def test_llm_model_not_image(self):
        """Regular LLM models should not be classified as image."""
        assert classify_model("qwen3:8b") != ModelCategory.IMAGE
        assert classify_model("llama3.3:70b") != ModelCategory.IMAGE
        assert classify_model("deepseek-r1:70b") != ModelCategory.IMAGE


class TestOllamaGenerateImageDetection:
    """Tests for /api/generate with Ollama native image models."""

    @pytest.fixture
    def client(self):
        from contextlib import asynccontextmanager

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from fleet_manager.models.config import ServerSettings
        from fleet_manager.server.queue_manager import QueueManager
        from fleet_manager.server.registry import NodeRegistry
        from fleet_manager.server.scorer import ScoringEngine
        from fleet_manager.server.streaming import StreamingProxy

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

        from fleet_manager.server.routes import (
            dashboard,
            heartbeat,
            image_compat,
            ollama_compat,
        )

        app.include_router(heartbeat.router)
        app.include_router(ollama_compat.router)
        app.include_router(image_compat.router)
        app.include_router(dashboard.router)

        with TestClient(app) as c:
            yield c

    def test_image_model_returns_404_when_not_on_fleet(self, client):
        """Requesting an Ollama native image model returns 404 if no node has it."""
        resp = client.post(
            "/api/generate",
            json={"model": "x/z-image-turbo", "prompt": "a sunset"},
        )
        assert resp.status_code == 404

    def test_image_model_via_generate_image_routes_through_ollama(self, client):
        """
        /api/generate-image with Ollama native model should attempt
        Ollama routing (not mflux), resulting in 404 since no node has it.
        """
        client.post("/dashboard/api/settings", json={"image_generation": True})
        resp = client.post(
            "/api/generate-image",
            json={"model": "x/z-image-turbo", "prompt": "a sunset"},
        )
        # Should get 404 (model not on any node) not 503 (disabled)
        assert resp.status_code == 404

    def test_regular_model_not_affected(self, client):
        """Non-image models through /api/generate still behave normally."""
        resp = client.post(
            "/api/generate",
            json={"model": "llama3.3:70b", "prompt": "hello"},
        )
        # 404 because no nodes registered — but it's a text request, not image
        assert resp.status_code == 404

    def test_mflux_model_still_works_via_generate_image(self, client):
        """mflux models via /api/generate-image still use the mflux path."""
        from tests.conftest import make_heartbeat

        client.post("/dashboard/api/settings", json={"image_generation": True})

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
            json={"model": "z-image-turbo", "prompt": "a cat"},
        )
        # Should get 502 (proxy error to mflux server) — not 404
        # This means it found the mflux candidate and tried to route to it
        assert resp.status_code == 502
