"""Integration tests for /api/platform/{status,connect,disconnect} routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.node import platform_connection
from fleet_manager.server.routes.platform import router as platform_router


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with platform routes mounted and state redirected."""
    # Redirect state files to a temp dir so tests don't touch ~/.fleet-manager
    monkeypatch.setattr(platform_connection, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(platform_connection, "STATE_FILE", tmp_path / "platform.json")
    monkeypatch.setattr(
        platform_connection, "KEYPAIR_FILE", tmp_path / "node_key.ed25519"
    )

    app = FastAPI()
    app.state.settings = ServerSettings()
    app.include_router(platform_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/platform/status
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    def test_status_not_connected_when_no_state_file(self, client):
        resp = client.get("/api/platform/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "not_connected"
        assert data["connected"] is None
        assert data["error"] is None
        assert "features" in data

    def test_status_connected_after_save_state(self, client, tmp_path):
        """If state is persisted, status should report connected."""
        from datetime import UTC, datetime

        from fleet_manager.node.platform_connection import (
            ConnectionState,
            save_state,
        )

        save_state(
            ConnectionState(
                platform_url="https://test.example.com",
                operator_token="herd_test",
                node_id="uuid-test",
                connected_at=datetime.now(UTC),
                user_email="test@example.com",
                user_display_name="Test User",
            )
        )
        resp = client.get("/api/platform/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "connected"
        assert data["connected"]["user_email"] == "test@example.com"
        assert data["connected"]["node_id"] == "uuid-test"


# ---------------------------------------------------------------------------
# POST /api/platform/connect
# ---------------------------------------------------------------------------


class TestConnectEndpoint:
    def test_connect_missing_token_400(self, client):
        resp = client.post("/api/platform/connect", json={})
        assert resp.status_code == 400
        assert "operator_token is required" in resp.json()["error"]

    def test_connect_rejects_token_without_prefix(self, client):
        resp = client.post(
            "/api/platform/connect", json={"operator_token": "notprefixed"}
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_token"

    def test_connect_invalid_json_400(self, client):
        resp = client.post(
            "/api/platform/connect",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_connect_happy_path(self, client, httpx_mock, tmp_path):
        """200 from auth/me + 201 from register → success, state persisted."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            json={"email": "user@example.com", "display_name": "User"},
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            json={"id": "uuid-new-node"},
            status_code=201,
        )
        resp = client.post(
            "/api/platform/connect",
            json={
                "operator_token": "herd_validtoken",
                "platform_url": "https://platform.example.com",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "connected"
        assert data["node_id"] == "uuid-new-node"

        # State file was persisted
        assert (tmp_path / "platform.json").exists()

    def test_connect_invalid_token_returns_400(self, client, httpx_mock):
        """Platform rejects the token with 401 → we return 400."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            status_code=401,
        )
        resp = client.post(
            "/api/platform/connect",
            json={
                "operator_token": "herd_bad",
                "platform_url": "https://platform.example.com",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_token"

    def test_connect_platform_unreachable_returns_502(self, client, httpx_mock):
        """Platform 503 → we return 502 'platform_unreachable'."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            status_code=503,
        )
        resp = client.post(
            "/api/platform/connect",
            json={
                "operator_token": "herd_valid",
                "platform_url": "https://platform.example.com",
            },
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "platform_unreachable"


# ---------------------------------------------------------------------------
# POST /api/platform/disconnect
# ---------------------------------------------------------------------------


class TestDisconnectEndpoint:
    def test_disconnect_idempotent_when_already_disconnected(self, client):
        """Disconnect when not connected is a no-op success."""
        resp = client.post("/api/platform/disconnect")
        assert resp.status_code == 200
        assert resp.json()["state"] == "not_connected"

    def test_disconnect_clears_saved_state(self, client, tmp_path):
        from datetime import UTC, datetime

        from fleet_manager.node.platform_connection import (
            ConnectionState,
            save_state,
        )

        save_state(
            ConnectionState(
                platform_url="https://example.com",
                operator_token="herd_abc",
                node_id="uuid-1",
                connected_at=datetime.now(UTC),
            )
        )
        assert (tmp_path / "platform.json").exists()

        resp = client.post("/api/platform/disconnect")
        assert resp.status_code == 200
        assert not (tmp_path / "platform.json").exists()

        # Status now reports not connected
        status_resp = client.get("/api/platform/status")
        assert status_resp.json()["state"] == "not_connected"
