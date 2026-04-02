"""Tests for transcription routing and multimodal dashboard features."""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import TranscriptionMetrics, TranscriptionModel
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.server.streaming import StreamingProxy
from tests.conftest import make_heartbeat, make_node


def _create_multimodal_test_app() -> FastAPI:
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
        fleet,
        heartbeat,
        image_compat,
        transcription_compat,
    )

    app.include_router(heartbeat.router)
    app.include_router(image_compat.router)
    app.include_router(transcription_compat.router)
    app.include_router(fleet.router)
    app.include_router(dashboard.router)

    return app


@pytest.fixture
def client():
    app = _create_multimodal_test_app()
    with TestClient(app) as c:
        yield c


class TestTranscriptionRoute:
    """Tests for the /api/transcribe endpoint."""

    def test_transcription_disabled_returns_503(self, client):
        # Disable transcription first (defaults to True now)
        client.post("/dashboard/api/settings", json={"transcription": False})
        resp = client.post(
            "/api/transcribe",
            files={"audio": ("test.wav", b"fake-audio-data")},
        )
        assert resp.status_code == 503
        assert "disabled" in resp.json()["error"]
        # Re-enable for other tests
        client.post("/dashboard/api/settings", json={"transcription": True})

    def test_transcription_enabled_no_nodes_returns_404(self, client):
        client.post(
            "/dashboard/api/settings",
            json={"transcription": True},
        )
        resp = client.post(
            "/api/transcribe",
            files={"audio": ("test.wav", b"fake-audio-data")},
        )
        assert resp.status_code == 404
        assert "mlx-qwen3-asr" in resp.json()["error"]

    def test_transcription_node_selected(self, client):
        """Node with STT model and port is found as candidate."""
        client.post(
            "/dashboard/api/settings",
            json={"transcription": True},
        )
        hb = make_heartbeat(node_id="studio").model_dump()
        hb["transcription"] = {
            "models_available": [
                {"name": "qwen3-asr", "binary": "mlx-qwen3-asr"}
            ],
            "transcribing": False,
        }
        hb["transcription_port"] = 11437
        client.post("/heartbeat", json=hb)

        resp = client.post(
            "/api/transcribe",
            files={"audio": ("test.wav", b"fake-audio-data")},
        )
        # 502 = reached the node but it's not a real STT server
        assert resp.status_code == 502


class TestTranscriptionStats:
    """Tests for /dashboard/api/transcription-stats."""

    def test_transcription_stats_empty(self, client):
        resp = client.get("/dashboard/api/transcription-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 0
        assert "completed" in data
        assert "failed" in data
        assert "by_node" in data
        assert "by_model" in data
        assert "recent" in data

    def test_transcription_stats_after_events(self):
        from fleet_manager.server.routes.transcription_compat import (
            _record_transcription,
            _transcription_events,
            get_transcription_events,
        )

        _transcription_events.clear()
        _record_transcription("qwen3-asr", "studio", "completed", 5000)
        _record_transcription("qwen3-asr", "studio", "failed", 2000, error="timeout")

        events = get_transcription_events(hours=1)
        assert len(events) == 2
        assert events[0]["status"] == "completed"
        assert events[1]["error"] == "timeout"

        _transcription_events.clear()


class TestTranscriptionHealthCheck:
    """Tests for _check_transcription() health check."""

    def test_no_events_no_recommendation(self):
        from fleet_manager.server.routes.transcription_compat import (
            _transcription_events,
        )

        _transcription_events.clear()

        from fleet_manager.server.health_engine import HealthEngine

        engine = HealthEngine()
        recs = engine._check_transcription([])
        assert len(recs) == 0

        _transcription_events.clear()

    def test_events_produce_recommendation(self):
        from fleet_manager.server.routes.transcription_compat import (
            _record_transcription,
            _transcription_events,
        )

        _transcription_events.clear()
        for _ in range(5):
            _record_transcription("qwen3-asr", "studio", "completed", 3000)

        from fleet_manager.server.health_engine import HealthEngine

        engine = HealthEngine()
        recs = engine._check_transcription([])
        assert len(recs) >= 1
        assert recs[0].check_id == "transcription_activity"
        assert "5 transcriptions" in recs[0].description

        _transcription_events.clear()

    def test_stt_expansion_recommendation(self):
        from fleet_manager.server.routes.transcription_compat import (
            _record_transcription,
            _transcription_events,
        )

        _transcription_events.clear()
        for _ in range(5):
            _record_transcription("qwen3-asr", "studio", "completed", 3000)

        node_with = make_node(node_id="studio", memory_total=64, memory_used=20)
        node_with.transcription = TranscriptionMetrics(
            models_available=[TranscriptionModel(name="qwen3-asr", binary="mlx")],
        )
        node_without = make_node(node_id="mini", memory_total=32, memory_used=10)

        from fleet_manager.server.health_engine import HealthEngine

        engine = HealthEngine()
        recs = engine._check_transcription([node_with, node_without])

        check_ids = [r.check_id for r in recs]
        assert "stt_expansion" in check_ids
        expansion = next(r for r in recs if r.check_id == "stt_expansion")
        assert "mini" in expansion.description

        _transcription_events.clear()


class TestRequestType:
    """Tests for request_type field on InferenceRequest."""

    def test_default_request_type_is_text(self):
        from fleet_manager.models.request import InferenceRequest

        req = InferenceRequest(model="gpt-oss:120b")
        assert req.request_type == "text"

    def test_image_request_type(self):
        from fleet_manager.models.request import InferenceRequest, RequestFormat

        req = InferenceRequest(
            model="z-image-turbo",
            original_format=RequestFormat.OLLAMA,
            request_type="image",
        )
        assert req.request_type == "image"

    def test_stt_request_type(self):
        from fleet_manager.models.request import InferenceRequest, RequestFormat

        req = InferenceRequest(
            model="qwen3-asr",
            original_format=RequestFormat.OLLAMA,
            request_type="stt",
        )
        assert req.request_type == "stt"

    def test_queue_info_includes_request_type(self):
        """Queue info should infer request_type from in-flight entry."""
        from fleet_manager.models.request import InferenceRequest, QueueEntry
        from fleet_manager.server.queue_manager import DeviceModelQueue, QueueManager

        qm = QueueManager()
        entry = QueueEntry(
            request=InferenceRequest(
                model="z-image-turbo:latest",
                request_type="image",
            ),
            assigned_node="studio",
        )
        q = DeviceModelQueue(node_id="studio", model="z-image-turbo:latest")
        q.in_flight[entry.request.request_id] = entry
        qm._queues["studio:z-image-turbo:latest"] = q

        info = qm.get_queue_info()
        assert "studio:z-image-turbo:latest" in info
        assert info["studio:z-image-turbo:latest"]["request_type"] == "image"

    def test_queue_info_infers_type_from_model_name(self):
        """Queue info should infer request_type from model name when no in-flight."""
        from fleet_manager.server.queue_manager import DeviceModelQueue, QueueManager

        qm = QueueManager()
        q = DeviceModelQueue(node_id="studio", model="qwen3-asr:latest")
        qm._queues["studio:qwen3-asr:latest"] = q

        info = qm.get_queue_info()
        assert info["studio:qwen3-asr:latest"]["request_type"] == "stt"


class TestFleetStatusMultimodal:
    """Tests for fleet status including image/transcription data."""

    def test_fleet_status_includes_image_data(self, client):
        hb = make_heartbeat(node_id="studio").model_dump()
        hb["image"] = {
            "models_available": [
                {"name": "z-image-turbo", "binary": "mflux"}
            ],
            "generating": False,
        }
        hb["image_port"] = 11436
        client.post("/heartbeat", json=hb)

        resp = client.get("/fleet/status")
        assert resp.status_code == 200
        data = resp.json()
        node = data["nodes"][0]
        assert "image" in node
        assert node["image"]["models_available"][0]["name"] == "z-image-turbo"
        assert node["image_port"] == 11436

    def test_fleet_status_includes_transcription_data(self, client):
        hb = make_heartbeat(node_id="studio").model_dump()
        hb["transcription"] = {
            "models_available": [
                {"name": "qwen3-asr", "binary": "mlx-qwen3-asr"}
            ],
            "transcribing": False,
        }
        hb["transcription_port"] = 11437
        client.post("/heartbeat", json=hb)

        resp = client.get("/fleet/status")
        data = resp.json()
        node = data["nodes"][0]
        assert "transcription" in node
        assert node["transcription"]["models_available"][0]["name"] == "qwen3-asr"
        assert node["transcription_port"] == 11437

    def test_fleet_status_no_image_when_absent(self, client):
        hb = make_heartbeat(node_id="basic").model_dump()
        client.post("/heartbeat", json=hb)

        resp = client.get("/fleet/status")
        node = resp.json()["nodes"][0]
        assert "image" not in node
        assert "transcription" not in node


class TestSettingsMultimodal:
    """Tests for settings API including multimodal toggles."""

    def test_settings_includes_transcription_toggle(self, client):
        resp = client.get("/dashboard/api/settings")
        data = resp.json()
        assert "transcription" in data["config"]["toggles"]
        assert data["config"]["toggles"]["transcription"] is True

    def test_toggle_transcription(self, client):
        resp = client.post(
            "/dashboard/api/settings",
            json={"transcription": True},
        )
        assert resp.json()["status"] == "updated"

        resp = client.get("/dashboard/api/settings")
        assert resp.json()["config"]["toggles"]["transcription"] is True

    def test_settings_nodes_show_stt_models(self, client):
        hb = make_heartbeat(node_id="studio").model_dump()
        hb["transcription"] = {
            "models_available": [
                {"name": "qwen3-asr", "binary": "mlx-qwen3-asr"}
            ],
            "transcribing": False,
        }
        hb["transcription_port"] = 11437
        client.post("/heartbeat", json=hb)

        resp = client.get("/dashboard/api/settings")
        node = resp.json()["nodes"][0]
        assert "qwen3-asr" in node["stt_models"]
        assert node["transcription_port"] == 11437
