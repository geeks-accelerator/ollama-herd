"""Tests for platform_client.py — HTTP wrapper with retry + 409 handling."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fleet_manager.node import platform_client, platform_connection
from fleet_manager.node.platform_client import (
    TelemetryDuplicateError,
    post,
    post_local_summary,
)
from fleet_manager.node.platform_connection import (
    ConnectionState,
    InvalidTokenError,
    PlatformUnreachableError,
)


@pytest.fixture
def connected_state(tmp_path, monkeypatch):
    """Fake saved platform state for the client to read."""
    monkeypatch.setattr(platform_connection, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(platform_connection, "STATE_FILE", tmp_path / "platform.json")
    platform_connection.save_state(
        ConnectionState(
            platform_url="https://platform.example.com",
            operator_token="herd_test_token",
            node_id="uuid-test",
            connected_at=datetime.now(UTC),
        )
    )
    return tmp_path


@pytest.fixture
def fast_backoff(monkeypatch):
    """Shrink retry backoff so tests don't sleep."""
    monkeypatch.setattr(platform_client, "_BASE_BACKOFF_S", 0.001)


# ---------------------------------------------------------------------------
# post() — base behavior
# ---------------------------------------------------------------------------


class TestPost:
    @pytest.mark.asyncio
    async def test_200_returns_json(self, connected_state, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            json={"ok": True},
            status_code=200,
        )
        result = await post("/api/test", json={"foo": "bar"})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_201_returns_json(self, connected_state, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            json={"created": True},
            status_code=201,
        )
        result = await post("/api/test", json={})
        assert result == {"created": True}

    @pytest.mark.asyncio
    async def test_401_raises_invalid_token_no_retry(
        self, connected_state, httpx_mock
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            status_code=401,
        )
        with pytest.raises(InvalidTokenError):
            await post("/api/test", json={})

    @pytest.mark.asyncio
    async def test_409_raises_duplicate_error(self, connected_state, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            status_code=409,
        )
        with pytest.raises(TelemetryDuplicateError):
            await post("/api/test", json={})

    @pytest.mark.asyncio
    async def test_503_retries_then_fails(
        self, connected_state, httpx_mock, fast_backoff
    ):
        # Three 503s — all retries exhausted
        for _ in range(3):
            httpx_mock.add_response(
                url="https://platform.example.com/api/test",
                method="POST",
                status_code=503,
            )
        with pytest.raises(PlatformUnreachableError):
            await post("/api/test", json={})

    @pytest.mark.asyncio
    async def test_503_then_200_recovers(
        self, connected_state, httpx_mock, fast_backoff
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            status_code=503,
        )
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            json={"ok": True},
            status_code=200,
        )
        result = await post("/api/test", json={})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_400_does_not_retry(self, connected_state, httpx_mock):
        """4xx (except 401/409) should fail fast, not retry."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            status_code=400,
            text="Bad request",
        )
        with pytest.raises(PlatformUnreachableError):
            await post("/api/test", json={})

    @pytest.mark.asyncio
    async def test_not_connected_raises(self, tmp_path, monkeypatch):
        """If no saved state, post() refuses to send."""
        monkeypatch.setattr(platform_connection, "_STATE_DIR", tmp_path)
        monkeypatch.setattr(platform_connection, "STATE_FILE", tmp_path / "platform.json")
        # No state saved
        with pytest.raises(PlatformUnreachableError, match="Not connected"):
            await post("/api/test", json={})

    @pytest.mark.asyncio
    async def test_auth_header_includes_bearer_token(
        self, connected_state, httpx_mock
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/test",
            method="POST",
            json={"ok": True},
            status_code=200,
        )
        await post("/api/test", json={})
        # Inspect the request that was captured
        reqs = httpx_mock.get_requests()
        assert reqs[0].headers["authorization"] == "Bearer herd_test_token"


# ---------------------------------------------------------------------------
# post_local_summary() — treats 409 as success
# ---------------------------------------------------------------------------


class TestPostLocalSummary:
    @pytest.mark.asyncio
    async def test_409_returns_duplicate_status(
        self, connected_state, httpx_mock
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/telemetry/local-summary",
            method="POST",
            status_code=409,
        )
        result = await post_local_summary({"day": "2026-04-20"})
        assert result == {"status": "duplicate", "day": "2026-04-20"}

    @pytest.mark.asyncio
    async def test_success_returns_platform_response(
        self, connected_state, httpx_mock
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/telemetry/local-summary",
            method="POST",
            json={"message": "Summary ingested."},
            status_code=200,
        )
        result = await post_local_summary({"day": "2026-04-20"})
        assert result["message"] == "Summary ingested."
