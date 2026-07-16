"""Tests for the fleet read + pin API (client-ergonomics batch 2)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from fleet_manager.server.serializers import OLLAMA_HOT_MODEL_CAP
from tests.conftest import make_heartbeat
from tests.test_server.test_routes import create_test_app

# ---------------------------------------------------------------------------
# serialize_node via /fleet/status — derived slot fields on a real node
# ---------------------------------------------------------------------------


def test_fleet_status_reports_free_slots_per_node():
    with TestClient(create_test_app()) as c:
        c.post("/heartbeat", json=make_heartbeat(
            node_id="bb",
            loaded_models=[("gpt-oss:120b", 71.0), ("gemma3:27b", 39.0)],
            available_models=["gpt-oss:120b", "gemma3:27b"],
        ).model_dump())
        node = c.get("/fleet/status").json()["nodes"][0]
        assert node["models_loaded_count"] == 2
        assert node["free_slots"] == OLLAMA_HOT_MODEL_CAP - 2


# ---------------------------------------------------------------------------
# /fleet/limits + /fleet/status — read API
# ---------------------------------------------------------------------------


def test_fleet_limits_reports_constraints():
    with TestClient(create_test_app()) as c:
        r = c.get("/fleet/limits")
        assert r.status_code == 200
        body = r.json()
        assert body["hot_model_cap"] == OLLAMA_HOT_MODEL_CAP
        assert "max_retries" in body
        assert "mlx_max_inflight_per_model" in body
        assert isinstance(body["nodes"], list)


def test_fleet_status_serializes_without_error():
    with TestClient(create_test_app()) as c:
        r = c.get("/fleet/status")
        assert r.status_code == 200
        body = r.json()
        assert "fleet" in body and "nodes" in body and "queues" in body


# ---------------------------------------------------------------------------
# /fleet/pin + /fleet/pin/{model} — model management API
# ---------------------------------------------------------------------------


def test_fleet_pin_requires_model():
    with TestClient(create_test_app()) as c:
        r = c.post("/fleet/pin", json={})
        assert r.status_code == 400
        assert r.json()["ok"] is False


def test_fleet_pin_404_when_model_not_on_disk():
    # Empty fleet → the model is nowhere on disk → explicit 404, not a substitute.
    with TestClient(create_test_app()) as c:
        r = c.post("/fleet/pin", json={"model": "nonexistent:999b"})
        assert r.status_code == 404
        assert "not on disk" in r.json()["error"]


def test_fleet_unpin_accepts_slashed_model_names():
    # DELETE with a name containing '/' and ':' must route (model:path).
    with TestClient(create_test_app()) as c:
        r = c.delete("/fleet/pin/mlx-community/Qwen3-Coder-30B:latest")
        assert r.status_code == 200
        assert r.json()["ok"] is True
