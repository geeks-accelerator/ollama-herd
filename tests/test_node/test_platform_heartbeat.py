"""Tests for platform_heartbeat — signing, metrics gathering, send flow."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from fleet_manager.node import platform_connection, platform_heartbeat
from fleet_manager.node.platform_connection import ConnectionState


@pytest.fixture
def connected_node(tmp_path, monkeypatch):
    """Platform connected with a real Ed25519 keypair on disk."""
    monkeypatch.setattr(platform_connection, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        platform_connection, "STATE_FILE", tmp_path / "platform.json"
    )
    monkeypatch.setattr(
        platform_connection, "KEYPAIR_FILE", tmp_path / "node_key.ed25519"
    )
    # Generate keypair
    platform_connection.load_or_generate_keypair()
    platform_connection.save_state(
        ConnectionState(
            platform_url="https://platform.example.com",
            operator_token="herd_test",
            node_id="uuid-test",
            connected_at=datetime.now(UTC),
        )
    )
    # Reset any leftover counters
    platform_heartbeat._reset_counters()
    return tmp_path


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


class TestSigning:
    def test_sign_payload_returns_base64(self, connected_node):
        sig = platform_heartbeat._sign_payload({"foo": "bar", "n": 42})
        # Base64 decodes cleanly
        decoded = base64.b64decode(sig)
        # Ed25519 signatures are 64 bytes
        assert len(decoded) == 64

    def test_signatures_differ_for_different_payloads(self, connected_node):
        sig1 = platform_heartbeat._sign_payload({"n": 1})
        sig2 = platform_heartbeat._sign_payload({"n": 2})
        assert sig1 != sig2

    def test_signature_is_deterministic_for_same_payload(self, connected_node):
        """Ed25519 signatures are deterministic per RFC 8032."""
        sig1 = platform_heartbeat._sign_payload({"n": 1, "x": "y"})
        sig2 = platform_heartbeat._sign_payload({"n": 1, "x": "y"})
        assert sig1 == sig2

    def test_signature_verifies_against_public_key(self, connected_node):
        """End-to-end: sign with our key, verify with the public key."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        payload = {"node_id": "test", "cpu_pct": 10.0}
        sig = platform_heartbeat._sign_payload(payload)
        # Reconstruct the same canonical bytes
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        # Load our public key and verify
        priv = platform_heartbeat._load_private_key()
        pub_bytes = priv.public_key().public_bytes_raw()
        pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub.verify(base64.b64decode(sig), canonical)  # raises if invalid


# ---------------------------------------------------------------------------
# Counter tracking
# ---------------------------------------------------------------------------


class TestCounters:
    def test_record_request_increments(self, connected_node):
        platform_heartbeat._reset_counters()
        platform_heartbeat.record_request(completed=True, tokens=100, compute_seconds=1.5)
        platform_heartbeat.record_request(completed=True, tokens=50, compute_seconds=0.5)
        platform_heartbeat.record_request(completed=False)
        assert platform_heartbeat._requests_completed_since_last == 2
        assert platform_heartbeat._requests_failed_since_last == 1
        assert platform_heartbeat._tokens_served_since_last == 150
        assert platform_heartbeat._compute_seconds_since_last == 2.0

    def test_reset_zeros_all(self, connected_node):
        platform_heartbeat.record_request(completed=True, tokens=10)
        platform_heartbeat._reset_counters()
        assert platform_heartbeat._requests_completed_since_last == 0
        assert platform_heartbeat._tokens_served_since_last == 0


# ---------------------------------------------------------------------------
# _send_one_heartbeat() — the full send flow
# ---------------------------------------------------------------------------


class TestSendOnce:
    @pytest.mark.asyncio
    async def test_successful_send_resets_counters(
        self, connected_node, httpx_mock, monkeypatch
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/heartbeats",
            method="POST",
            json={"message": "Heartbeat received.", "balance": 0},
            status_code=200,
        )
        # Fake the /fleet/status call so _gather_metrics doesn't hit the real router
        async def _fake_fleet(*a, **k):
            class R:
                status_code = 404
            return R()

        # Seed some counters
        platform_heartbeat.record_request(completed=True, tokens=500)
        platform_heartbeat.record_request(completed=False)

        # Patch httpx.get used by _gather_metrics for /fleet/status
        import httpx

        orig_get = httpx.AsyncClient.get

        async def fake_get(self, url, *a, **k):
            if "fleet/status" in url:
                class R:
                    status_code = 404
                return R()
            return await orig_get(self, url, *a, **k)

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        ok = await platform_heartbeat._send_one_heartbeat()
        assert ok is True
        # Counters reset after successful send
        assert platform_heartbeat._requests_completed_since_last == 0
        assert platform_heartbeat._requests_failed_since_last == 0

    @pytest.mark.asyncio
    async def test_401_returns_false_and_keeps_counters(
        self, connected_node, httpx_mock, monkeypatch
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/heartbeats",
            method="POST",
            status_code=401,
        )
        platform_heartbeat.record_request(completed=True, tokens=100)

        import httpx

        async def fake_get(self, url, *a, **k):
            class R:
                status_code = 404
            return R()

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        ok = await platform_heartbeat._send_one_heartbeat()
        assert ok is False
        # Counters NOT reset — we'll retry on next heartbeat
        assert platform_heartbeat._requests_completed_since_last == 1

    @pytest.mark.asyncio
    async def test_payload_contains_expected_fields(
        self, connected_node, httpx_mock, monkeypatch
    ):
        """Verify the outer payload has all the fields the plan specifies."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/heartbeats",
            method="POST",
            json={"ok": True},
            status_code=200,
        )

        import httpx

        async def fake_get(self, url, *a, **k):
            class R:
                status_code = 404
            return R()

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        await platform_heartbeat._send_one_heartbeat()
        reqs = httpx_mock.get_requests()
        posted = json.loads(reqs[0].content)

        # Key fields from the extension plan
        assert "node_id" in posted
        assert "cpu_pct" in posted
        assert "memory_used_gb" in posted
        assert "memory_total_gb" in posted
        assert "vram_used_gb" in posted
        assert "vram_total_gb" in posted
        assert "queue_depth" in posted
        assert "queue_depths_by_model" in posted
        assert "loaded_models" in posted
        assert "requests_completed" in posted
        assert "requests_failed" in posted
        assert "signature" in posted
        # New contract: no separate raw_payload field.  Signature covers
        # the body minus the signature field itself.
        assert "raw_payload" not in posted
        assert posted["node_id"] == "uuid-test"
