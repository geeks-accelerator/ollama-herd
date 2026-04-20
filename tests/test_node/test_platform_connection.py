"""Unit tests for platform_connection.py — token validation, keypair, persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fleet_manager.node import platform_connection
from fleet_manager.node.platform_connection import (
    ConnectionState,
    InvalidTokenError,
    PlatformUnreachableError,
    RegistrationError,
    clear_state,
    connect_to_platform,
    disconnect_from_platform,
    is_connected,
    load_or_generate_keypair,
    load_state,
    public_key_b64,
    register_node,
    save_state,
    validate_token,
)


@pytest.fixture
def temp_state_dir(tmp_path, monkeypatch):
    """Redirect platform state files to a temp dir."""
    monkeypatch.setattr(platform_connection, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(platform_connection, "STATE_FILE", tmp_path / "platform.json")
    monkeypatch.setattr(platform_connection, "KEYPAIR_FILE", tmp_path / "node_key.ed25519")
    return tmp_path


# ---------------------------------------------------------------------------
# Keypair tests
# ---------------------------------------------------------------------------


class TestKeypair:
    def test_generate_fresh_keypair(self, temp_state_dir):
        priv, pub = load_or_generate_keypair()
        assert priv  # Non-empty PEM bytes
        assert pub  # Non-empty raw bytes
        assert len(pub) == 32  # Ed25519 public keys are 32 bytes

        key_file = temp_state_dir / "node_key.ed25519"
        assert key_file.exists()

    def test_keypair_file_has_0600_mode(self, temp_state_dir):
        load_or_generate_keypair()
        key_file = temp_state_dir / "node_key.ed25519"
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_keypair_stable_across_calls(self, temp_state_dir):
        """Second call should load the existing keypair, not regenerate."""
        _, pub1 = load_or_generate_keypair()
        _, pub2 = load_or_generate_keypair()
        assert pub1 == pub2  # Same bytes

    def test_public_key_b64_is_ascii(self, temp_state_dir):
        pub_b64 = public_key_b64()
        assert isinstance(pub_b64, str)
        # Ed25519 pub = 32 bytes → 44 chars base64 (with padding)
        assert len(pub_b64) == 44


# ---------------------------------------------------------------------------
# State persistence tests
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_save_and_load_state(self, temp_state_dir):
        state = ConnectionState(
            platform_url="https://example.com",
            operator_token="herd_abc123",
            node_id="uuid-1234",
            connected_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            user_email="user@example.com",
            user_display_name="User",
        )
        save_state(state)

        loaded = load_state()
        assert loaded is not None
        assert loaded.platform_url == "https://example.com"
        assert loaded.operator_token == "herd_abc123"
        assert loaded.node_id == "uuid-1234"
        assert loaded.user_email == "user@example.com"

    def test_state_file_has_0600_mode(self, temp_state_dir):
        state = ConnectionState(
            platform_url="https://example.com",
            operator_token="herd_abc",
            node_id="uuid-1",
            connected_at=datetime.now(UTC),
        )
        save_state(state)
        state_file = temp_state_dir / "platform.json"
        mode = state_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_load_state_returns_none_when_missing(self, temp_state_dir):
        assert load_state() is None

    def test_clear_state_removes_file(self, temp_state_dir):
        state = ConnectionState(
            platform_url="https://example.com",
            operator_token="herd_abc",
            node_id="uuid-1",
            connected_at=datetime.now(UTC),
        )
        save_state(state)
        assert (temp_state_dir / "platform.json").exists()
        clear_state()
        assert not (temp_state_dir / "platform.json").exists()

    def test_is_connected_reflects_state_file(self, temp_state_dir):
        assert not is_connected()
        state = ConnectionState(
            platform_url="https://example.com",
            operator_token="herd_abc",
            node_id="uuid-1",
            connected_at=datetime.now(UTC),
        )
        save_state(state)
        assert is_connected()

    def test_load_state_handles_corrupt_file(self, temp_state_dir):
        state_file = temp_state_dir / "platform.json"
        state_file.write_text("not valid json{{{")
        assert load_state() is None

    def test_save_overwrites_atomically(self, temp_state_dir):
        """Save uses tmp + rename — no partial writes visible."""
        for i in range(3):
            state = ConnectionState(
                platform_url=f"https://example-{i}.com",
                operator_token=f"herd_{i}",
                node_id=f"uuid-{i}",
                connected_at=datetime.now(UTC),
            )
            save_state(state)
        loaded = load_state()
        assert loaded.node_id == "uuid-2"


# ---------------------------------------------------------------------------
# Token validation tests
# ---------------------------------------------------------------------------


class TestValidateToken:
    @pytest.mark.asyncio
    async def test_valid_token_returns_identity(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            json={"email": "user@example.com", "display_name": "User"},
            status_code=200,
        )
        identity = await validate_token("https://platform.example.com", "herd_abc")
        assert identity.user_email == "user@example.com"
        assert identity.user_display_name == "User"

    @pytest.mark.asyncio
    async def test_invalid_token_raises(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            json={"error": "Invalid token"},
            status_code=401,
        )
        with pytest.raises(InvalidTokenError):
            await validate_token("https://platform.example.com", "herd_bad")

    @pytest.mark.asyncio
    async def test_platform_5xx_raises_unreachable(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            status_code=503,
        )
        with pytest.raises(PlatformUnreachableError):
            await validate_token("https://platform.example.com", "herd_abc")

    @pytest.mark.asyncio
    async def test_wrapped_data_envelope_unwrapped(self, httpx_mock):
        """Platform may wrap response in {data: {...}}."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            json={"data": {"email": "user@example.com", "display_name": "User"}},
            status_code=200,
        )
        identity = await validate_token("https://platform.example.com", "herd_abc")
        assert identity.user_email == "user@example.com"


# ---------------------------------------------------------------------------
# Node registration tests
# ---------------------------------------------------------------------------


class TestRegisterNode:
    @pytest.mark.asyncio
    async def test_registration_201_returns_node_id(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            json={"id": "uuid-abc-123"},
            status_code=201,
        )
        node_id = await register_node(
            platform_url="https://platform.example.com",
            token="herd_abc",
            public_key="pubkey_b64",
            node_name="test-node",
        )
        assert node_id == "uuid-abc-123"

    @pytest.mark.asyncio
    async def test_registration_200_wrapped_envelope(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            json={"data": {"id": "uuid-wrapped"}},
            status_code=200,
        )
        node_id = await register_node(
            platform_url="https://platform.example.com",
            token="herd_abc",
            public_key="pubkey_b64",
            node_name="test-node",
        )
        assert node_id == "uuid-wrapped"

    @pytest.mark.asyncio
    async def test_registration_409_returns_existing_id_from_list(self, httpx_mock):
        """Platform returns 409 with existing_node_id as a list."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            json={"details": {"existing_node_id": ["uuid-existing"]}},
            status_code=409,
        )
        node_id = await register_node(
            platform_url="https://platform.example.com",
            token="herd_abc",
            public_key="pubkey_b64",
            node_name="test-node",
        )
        assert node_id == "uuid-existing"

    @pytest.mark.asyncio
    async def test_registration_409_returns_existing_id_from_string(self, httpx_mock):
        """Platform might return existing_node_id as a bare string."""
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            json={"details": {"existing_node_id": "uuid-string"}},
            status_code=409,
        )
        node_id = await register_node(
            platform_url="https://platform.example.com",
            token="herd_abc",
            public_key="pubkey_b64",
            node_name="test-node",
        )
        assert node_id == "uuid-string"

    @pytest.mark.asyncio
    async def test_registration_401_raises_invalid_token(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            status_code=401,
        )
        with pytest.raises(InvalidTokenError):
            await register_node(
                platform_url="https://platform.example.com",
                token="herd_bad",
                public_key="pubkey_b64",
                node_name="test-node",
            )

    @pytest.mark.asyncio
    async def test_registration_5xx_raises_unreachable(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            status_code=503,
        )
        with pytest.raises(PlatformUnreachableError):
            await register_node(
                platform_url="https://platform.example.com",
                token="herd_abc",
                public_key="pubkey_b64",
                node_name="test-node",
            )

    @pytest.mark.asyncio
    async def test_registration_400_raises_registration_error(self, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            json={"error": "Bad benchmark"},
            status_code=400,
        )
        with pytest.raises(RegistrationError):
            await register_node(
                platform_url="https://platform.example.com",
                token="herd_abc",
                public_key="pubkey_b64",
                node_name="test-node",
            )


# ---------------------------------------------------------------------------
# End-to-end connect flow
# ---------------------------------------------------------------------------


class TestConnectFlow:
    @pytest.mark.asyncio
    async def test_connect_rejects_token_without_herd_prefix(self, temp_state_dir):
        with pytest.raises(InvalidTokenError, match="start with 'herd_'"):
            await connect_to_platform(
                token="bogus_not_prefixed",
                platform_url="https://platform.example.com",
            )

    @pytest.mark.asyncio
    async def test_successful_connect_persists_state(
        self, temp_state_dir, httpx_mock
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/auth/me",
            json={"email": "user@example.com", "display_name": "User"},
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://platform.example.com/api/nodes/register",
            json={"id": "uuid-new"},
            status_code=201,
        )
        state = await connect_to_platform(
            token="herd_valid",
            platform_url="https://platform.example.com",
            node_name="test-node",
        )
        assert state.node_id == "uuid-new"
        assert state.user_email == "user@example.com"

        # State is persisted
        loaded = load_state()
        assert loaded is not None
        assert loaded.node_id == "uuid-new"

    @pytest.mark.asyncio
    async def test_disconnect_clears_state(self, temp_state_dir):
        state = ConnectionState(
            platform_url="https://example.com",
            operator_token="herd_abc",
            node_id="uuid-1",
            connected_at=datetime.now(UTC),
        )
        save_state(state)
        assert is_connected()

        disconnect_from_platform()
        assert not is_connected()


# ---------------------------------------------------------------------------
# Security invariants
# ---------------------------------------------------------------------------


class TestSecurityInvariants:
    def test_state_file_readable_only_by_owner(self, temp_state_dir):
        """Regression guard: the persisted token must be in a 0600 file."""
        state = ConnectionState(
            platform_url="https://example.com",
            operator_token="herd_secret_never_share",
            node_id="uuid-1",
            connected_at=datetime.now(UTC),
        )
        save_state(state)
        state_file = temp_state_dir / "platform.json"
        mode = state_file.stat().st_mode & 0o777
        assert mode == 0o600

        # Sanity: token is actually in the file (so we know 0600 matters)
        content = state_file.read_text()
        assert "herd_secret_never_share" in content

    def test_keypair_file_readable_only_by_owner(self, temp_state_dir):
        load_or_generate_keypair()
        key_file = temp_state_dir / "node_key.ed25519"
        mode = key_file.stat().st_mode & 0o777
        assert mode == 0o600
