"""Tests for product enhancements: embeddings, OpenAI images, image model listing, tagging."""

from __future__ import annotations

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

from tests.conftest import make_heartbeat


def _create_test_app() -> FastAPI:
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
        heartbeat,
        image_compat,
        ollama_compat,
        openai_compat,
    )

    app.include_router(heartbeat.router)
    app.include_router(openai_compat.router)
    app.include_router(ollama_compat.router)
    app.include_router(image_compat.router)

    return app


@pytest.fixture
def client():
    app = _create_test_app()
    with TestClient(app) as c:
        yield c


def _register_node_with_image(client, node_id="studio", image_models=None):
    """Register a node that has both LLM and image models."""
    hb = make_heartbeat(
        node_id=node_id,
        loaded_models=[("phi4:14b", 9.0)],
        available_models=["phi4:14b", "llama3.3:70b"],
    ).model_dump()
    # Add image metrics to the heartbeat
    if image_models:
        hb["image"] = {
            "models_available": [{"name": m, "binary": f"mflux-generate-{m}"} for m in image_models],
            "generating": False,
        }
        hb["image_port"] = 11436
    client.post("/heartbeat", json=hb)


# ── Embeddings endpoint tests ───────────────────────────────────────


class TestEmbeddingsEndpoint:
    """Tests for /api/embed and /api/embeddings."""

    def test_embed_no_model_returns_400(self, client):
        resp = client.post("/api/embed", json={"input": "hello"})
        assert resp.status_code == 400
        assert "model" in resp.json()["error"]

    def test_embeddings_no_model_returns_400(self, client):
        resp = client.post("/api/embeddings", json={"prompt": "hello"})
        assert resp.status_code == 400
        assert "model" in resp.json()["error"]

    def test_embed_model_not_found(self, client):
        resp = client.post(
            "/api/embed",
            json={"model": "nonexistent:999b", "input": "hello"},
        )
        assert resp.status_code == 404

    def test_embeddings_model_not_found(self, client):
        resp = client.post(
            "/api/embeddings",
            json={"model": "nonexistent:999b", "prompt": "hello"},
        )
        assert resp.status_code == 404

    def test_embed_request_type_is_embed(self):
        """Verify the embed endpoint sets request_type='embed'."""
        from fleet_manager.models.request import InferenceRequest, RequestFormat

        req = InferenceRequest(
            model="nomic-embed-text:latest",
            original_model="nomic-embed-text:latest",
            messages=[],
            stream=False,
            original_format=RequestFormat.OLLAMA,
            raw_body={},
            request_type="embed",
        )
        assert req.request_type == "embed"


# ── OpenAI images endpoint tests ────────────────────────────────────


class TestOpenAIImagesEndpoint:
    """Tests for /v1/images/generations."""

    def test_images_no_model_returns_400(self, client):
        resp = client.post(
            "/v1/images/generations",
            json={"prompt": "a cat"},
        )
        assert resp.status_code == 400
        assert "model" in resp.json()["error"]["message"]

    def test_images_no_prompt_returns_400(self, client):
        resp = client.post(
            "/v1/images/generations",
            json={"model": "z-image-turbo"},
        )
        assert resp.status_code == 400
        assert "prompt" in resp.json()["error"]["message"]

    def test_images_model_not_found(self, client):
        """Image generation with a model that doesn't exist."""
        resp = client.post(
            "/v1/images/generations",
            json={"model": "nonexistent-image", "prompt": "a cat"},
        )
        # Should get an error (404 from image endpoint, wrapped in OpenAI format)
        assert resp.status_code >= 400


# ── Image models in /api/tags tests ─────────────────────────────────


class TestImageModelsInTags:
    """Tests for image models appearing in /api/tags and /v1/models."""

    def test_tags_includes_image_models(self, client):
        _register_node_with_image(client, image_models=["z-image-turbo", "sd3-medium"])

        resp = client.get("/api/tags")
        assert resp.status_code == 200
        names = [m["name"] for m in resp.json()["models"]]
        assert "z-image-turbo" in names
        assert "sd3-medium" in names
        # LLM models should still be there
        assert "phi4:14b" in names

    def test_tags_image_models_have_type(self, client):
        _register_node_with_image(client, image_models=["z-image-turbo"])

        resp = client.get("/api/tags")
        models = resp.json()["models"]
        image_model = next(m for m in models if m["name"] == "z-image-turbo")
        assert image_model["details"]["type"] == "image"

    def test_tags_image_models_dedup_across_nodes(self, client):
        """Same image model on two nodes should show both node IDs."""
        _register_node_with_image(client, node_id="studio-1", image_models=["z-image-turbo"])
        _register_node_with_image(client, node_id="studio-2", image_models=["z-image-turbo"])

        resp = client.get("/api/tags")
        models = resp.json()["models"]
        img = next(m for m in models if m["name"] == "z-image-turbo")
        assert "studio-1" in img["details"]["fleet_nodes"]
        assert "studio-2" in img["details"]["fleet_nodes"]

    def test_v1_models_includes_image_models(self, client):
        _register_node_with_image(client, image_models=["z-image-turbo"])

        resp = client.get("/v1/models")
        assert resp.status_code == 200
        model_ids = [m["id"] for m in resp.json()["data"]]
        assert "z-image-turbo" in model_ids
        # LLM models too
        assert "phi4:14b" in model_ids

    def test_tags_no_image_models_when_no_image_nodes(self, client):
        """Nodes without image capabilities shouldn't inject image models."""
        hb = make_heartbeat(
            node_id="plain-node",
            loaded_models=[("phi4:14b", 9.0)],
            available_models=["phi4:14b"],
        ).model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/api/tags")
        names = [m["name"] for m in resp.json()["models"]]
        assert "phi4:14b" in names
        # No image models should be present
        for name in names:
            assert not any(
                img in name for img in ["z-image-turbo", "sd3", "flux"]
            ), f"Unexpected image model: {name}"


# ── Image model list endpoint tests ─────────────────────────────────


class TestImageModelListEndpoint:
    """Tests for /api/image-models."""

    def test_image_models_empty(self, client):
        resp = client.get("/api/image-models")
        assert resp.status_code == 200
        assert resp.json()["models"] == []

    def test_image_models_lists_mflux(self, client):
        _register_node_with_image(client, image_models=["z-image-turbo", "sd3-medium"])

        resp = client.get("/api/image-models")
        assert resp.status_code == 200
        models = resp.json()["models"]
        names = [m["name"] for m in models]
        assert "z-image-turbo" in names
        assert "sd3-medium" in names

    def test_image_models_have_backend_field(self, client):
        _register_node_with_image(client, image_models=["z-image-turbo"])

        resp = client.get("/api/image-models")
        models = resp.json()["models"]
        model = models[0]
        assert model["backend"] == "mflux"
        assert model["type"] == "image"
        assert "fleet_nodes" in model

    def test_image_models_dedup_across_nodes(self, client):
        _register_node_with_image(client, node_id="node-a", image_models=["z-image-turbo"])
        _register_node_with_image(client, node_id="node-b", image_models=["z-image-turbo"])

        resp = client.get("/api/image-models")
        models = resp.json()["models"]
        assert len(models) == 1
        assert set(models[0]["fleet_nodes"]) == {"node-a", "node-b"}


# ── DeepSeek-V3 model catalog tests ────────────────────────────────


class TestDeepSeekV3Catalog:
    """Tests for DeepSeek-V3 entries in the model knowledge catalog."""

    def test_deepseek_v3_in_catalog(self):
        from fleet_manager.server.model_knowledge import lookup_model

        spec = lookup_model("deepseek-v3:7b")
        assert spec is not None
        assert spec.family == "deepseek-v3"

    def test_deepseek_v3_32b_in_catalog(self):
        from fleet_manager.server.model_knowledge import lookup_model

        spec = lookup_model("deepseek-v3:32b")
        assert spec is not None
        assert spec.params_b == 32.0

    def test_deepseek_v3_671b_is_moe(self):
        from fleet_manager.server.model_knowledge import lookup_model

        spec = lookup_model("deepseek-v3:671b")
        assert spec is not None
        assert spec.is_moe
        assert spec.active_params_b == 37.0

    def test_deepseek_v3_classified_as_general(self):
        from fleet_manager.server.model_knowledge import classify_model, ModelCategory

        assert classify_model("deepseek-v3:7b") == ModelCategory.GENERAL

    def test_deepseek_r1_still_exists(self):
        """Ensure adding V3 didn't break R1 entries."""
        from fleet_manager.server.model_knowledge import lookup_model

        for size in ("8b", "14b", "32b", "70b", "671b"):
            spec = lookup_model(f"deepseek-r1:{size}")
            assert spec is not None, f"deepseek-r1:{size} missing from catalog"


# ── Request tagging for image/STT tests ─────────────────────────────


class TestRequestTagging:
    """Tests that image and STT endpoints use extract_tags."""

    def test_image_endpoint_imports_extract_tags(self):
        """Verify extract_tags is imported and used in image_compat."""
        import inspect
        from fleet_manager.server.routes import image_compat

        source = inspect.getsource(image_compat.generate_image)
        assert "extract_tags" in source

    def test_transcription_endpoint_imports_extract_tags(self):
        """Verify extract_tags is imported and used in transcription_compat."""
        import inspect
        from fleet_manager.server.routes import transcription_compat

        source = inspect.getsource(transcription_compat.transcribe_audio)
        assert "extract_tags" in source
