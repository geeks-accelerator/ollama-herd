"""Platform connection logic — token validation, keypair, persistence.

Pure-logic module that can be unit-tested without the HTTP server layer.
The routes in server/routes/platform.py wrap this for dashboard use.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Default platform URL (production).  Override via platform_url.
DEFAULT_PLATFORM_URL = "https://platform.ollamaherd.com"

# File paths — all under ~/.fleet-manager/ for consistency.
_STATE_DIR = Path.home() / ".fleet-manager"
STATE_FILE = _STATE_DIR / "platform.json"
KEYPAIR_FILE = _STATE_DIR / "node_key.ed25519"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlatformConnectionError(Exception):
    """Base class for connection errors surfaced to the dashboard."""


class InvalidTokenError(PlatformConnectionError):
    """Operator token was rejected by the platform."""


class PlatformUnreachableError(PlatformConnectionError):
    """Platform URL returned 5xx or network error."""


class RegistrationError(PlatformConnectionError):
    """Node registration failed for a reason other than auth."""


class BenchmarkError(PlatformConnectionError):
    """Benchmark failed to produce a result."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PlatformIdentity:
    """Identity returned by GET /api/auth/me."""

    user_email: str
    user_display_name: str


@dataclass
class ConnectionState:
    """Full platform connection state persisted to platform.json."""

    platform_url: str
    operator_token: str  # Stored in file mode 0600
    node_id: str  # Platform-issued UUID
    connected_at: datetime
    user_email: str | None = None
    user_display_name: str | None = None

    def to_dict(self) -> dict:
        return {
            "platform_url": self.platform_url,
            "operator_token": self.operator_token,
            "node_id": self.node_id,
            "connected_at": self.connected_at.isoformat(),
            "user_email": self.user_email,
            "user_display_name": self.user_display_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConnectionState:
        return cls(
            platform_url=data["platform_url"],
            operator_token=data["operator_token"],
            node_id=data["node_id"],
            connected_at=datetime.fromisoformat(data["connected_at"]),
            user_email=data.get("user_email"),
            user_display_name=data.get("user_display_name"),
        )


# ---------------------------------------------------------------------------
# Keypair (Ed25519)
# ---------------------------------------------------------------------------


def load_or_generate_keypair() -> tuple[bytes, bytes]:
    """Load the node's Ed25519 keypair from disk, or generate a new one.

    Returns (private_key_bytes, public_key_bytes).  Private key file is
    written with mode 0600.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _STATE_DIR.mkdir(parents=True, exist_ok=True)

    if KEYPAIR_FILE.exists():
        # Load existing
        with open(KEYPAIR_FILE, "rb") as f:
            priv_bytes = f.read()
        private_key = serialization.load_pem_private_key(
            priv_bytes, password=None
        )
        public_key = private_key.public_key()
        pub_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return priv_bytes, pub_bytes

    # Generate new
    private_key = Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    # Write with mode 0600 (user read/write only)
    with open(KEYPAIR_FILE, "wb") as f:
        f.write(priv_pem)
    os.chmod(KEYPAIR_FILE, 0o600)
    logger.info(f"Generated new Ed25519 keypair at {KEYPAIR_FILE}")

    return priv_pem, pub_bytes


def public_key_b64() -> str:
    """Return the node's public key as base64, generating one if needed."""
    _, pub_bytes = load_or_generate_keypair()
    return base64.b64encode(pub_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# Platform API calls
# ---------------------------------------------------------------------------


async def validate_token(platform_url: str, token: str) -> PlatformIdentity:
    """Validate an operator token via GET /api/auth/me.

    Raises InvalidTokenError on 401, PlatformUnreachableError on 5xx/network.
    """
    url = f"{platform_url.rstrip('/')}/api/auth/me"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        raise PlatformUnreachableError(
            f"Cannot reach {platform_url}: {exc}"
        ) from exc

    if resp.status_code == 401:
        raise InvalidTokenError(
            "Operator token rejected by platform. "
            "Generate a new one at platform.ollamaherd.com/web/"
        )
    if resp.status_code >= 500:
        raise PlatformUnreachableError(
            f"Platform returned {resp.status_code} on token validation"
        )
    if resp.status_code != 200:
        raise InvalidTokenError(
            f"Unexpected {resp.status_code} validating token"
        )

    data = resp.json()
    # Platform may wrap response in a data envelope; handle both shapes.
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    return PlatformIdentity(
        user_email=data.get("email", ""),
        user_display_name=data.get("display_name") or data.get("name") or "",
    )


async def register_node(
    platform_url: str,
    token: str,
    public_key: str,
    node_name: str,
    benchmark: dict | None = None,
    region: str | None = None,
    device_info: dict | None = None,
) -> str:
    """Register this node with the platform.  Returns platform-issued UUID.

    Handles 409 (already registered with this key) by extracting and
    returning the existing node_id.  Raises RegistrationError on other
    failures.
    """
    url = f"{platform_url.rstrip('/')}/api/nodes/register"
    body: dict = {
        "name": node_name,
        "public_key": public_key,
    }
    if benchmark:
        body["benchmark"] = benchmark
    if region:
        body["region"] = region
    if device_info:
        body["device_info"] = device_info

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise PlatformUnreachableError(
            f"Cannot reach {platform_url}: {exc}"
        ) from exc

    if resp.status_code in (200, 201):
        data = resp.json()
        # Handle wrapped envelope
        if "data" in data and isinstance(data["data"], dict):
            data = data["data"]
        node_id = data.get("id") or data.get("node_id")
        if not node_id:
            raise RegistrationError(
                f"Registration succeeded but no node_id returned: {data}"
            )
        return node_id

    if resp.status_code == 409:
        # Already registered with this public key — extract existing UUID
        try:
            data = resp.json()
            details = data.get("details") or {}
            existing = details.get("existing_node_id")
            if isinstance(existing, list) and existing:
                return existing[0]
            if isinstance(existing, str):
                return existing
        except (ValueError, KeyError):
            pass
        raise RegistrationError(
            "Node already registered but platform did not return "
            "existing_node_id — contact platform support"
        )

    if resp.status_code == 401:
        raise InvalidTokenError("Operator token rejected during registration")
    if resp.status_code >= 500:
        raise PlatformUnreachableError(
            f"Platform returned {resp.status_code} during registration"
        )
    raise RegistrationError(
        f"Registration failed: HTTP {resp.status_code} — {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def save_state(state: ConnectionState) -> None:
    """Persist connection state to platform.json with mode 0600."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.chmod(tmp, 0o600)
    # Atomic rename
    tmp.replace(STATE_FILE)
    logger.info(f"Platform connection state saved to {STATE_FILE}")


def load_state() -> ConnectionState | None:
    """Load saved connection state, or None if not connected."""
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return ConnectionState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning(f"Failed to load platform state: {exc}")
        return None


def clear_state() -> None:
    """Remove the persisted state file (disconnect)."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        logger.info("Platform connection state cleared")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def default_node_name() -> str:
    """Fallback node name when settings.node_id isn't set."""
    return socket.gethostname()


def is_connected() -> bool:
    """Quick check: do we have saved connection state?"""
    return STATE_FILE.exists()


# ---------------------------------------------------------------------------
# High-level Connect flow — used by both the route handler and CLI
# ---------------------------------------------------------------------------


async def connect_to_platform(
    token: str,
    platform_url: str = DEFAULT_PLATFORM_URL,
    node_name: str | None = None,
    benchmark: dict | None = None,
    region: str | None = None,
) -> ConnectionState:
    """Full connect flow: validate → keypair → register → persist.

    Raises InvalidTokenError, PlatformUnreachableError, or
    RegistrationError on failure.  On success, returns the saved
    ConnectionState and writes platform.json.
    """
    if not token.startswith("herd_"):
        raise InvalidTokenError(
            "Operator tokens start with 'herd_'. Check what you pasted."
        )

    # 1. Validate token
    identity = await validate_token(platform_url, token)

    # 2. Ensure keypair exists
    pub_b64 = public_key_b64()

    # 3. Build benchmark payload (platform requires at minimum tokens_per_sec)
    if benchmark is None:
        from fleet_manager.node.benchmark_estimate import build_benchmark_payload

        benchmark = await build_benchmark_payload()

    # 4. Probe hardware details (optional, best-effort)
    try:
        from fleet_manager.node.device_info import probe_device_info

        device_info = probe_device_info()
    except Exception as exc:
        logger.debug(f"device_info probe failed (continuing): {exc}")
        device_info = None

    # 5. Register node
    name = node_name or default_node_name()
    node_id = await register_node(
        platform_url=platform_url,
        token=token,
        public_key=pub_b64,
        node_name=name,
        benchmark=benchmark,
        region=region,
        device_info=device_info,
    )

    # 4. Persist state
    state = ConnectionState(
        platform_url=platform_url,
        operator_token=token,
        node_id=node_id,
        connected_at=datetime.now(UTC),
        user_email=identity.user_email,
        user_display_name=identity.user_display_name,
    )
    save_state(state)

    logger.info(
        f"platform-connect: connected to {platform_url} as "
        f"{identity.user_display_name or identity.user_email} "
        f"(node_id={node_id})"
    )
    return state


def disconnect_from_platform() -> None:
    """Local disconnect: clear state file, stop platform-dependent tasks.

    Does NOT call platform's deregister endpoint — the platform-side
    node record survives so the user can reconnect from the same machine.
    """
    clear_state()
    logger.info("platform-connect: disconnected")
