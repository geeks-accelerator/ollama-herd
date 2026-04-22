"""Platform HTTP client — thin httpx wrapper with retry logic.

Single client instance reused across modules (connection UX, telemetry,
future P2P capability advertisement).  Handles:

- Exponential backoff retry on 5xx and network errors (max 3 tries)
- Idempotent 409 treated as success (for telemetry re-sends)
- Authorization header injection from saved state
- Structured exceptions for callers to handle without parsing responses
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from fleet_manager.node.platform_connection import (
    InvalidTokenError,
    PlatformUnreachableError,
    load_state,
)

logger = logging.getLogger(__name__)

# Retry configuration
_MAX_RETRIES = 3
_BASE_BACKOFF_S = 1.0  # exponential: 1s, 2s, 4s


class TelemetryDuplicateError(Exception):
    """Platform returned 409 — telemetry already sent for this period.

    Callers treat this as success since telemetry is idempotent.
    """


async def post(
    path: str,
    json: dict,
    timeout: float = 30.0,
    max_retries: int | None = None,
) -> dict:
    """POST to a platform endpoint using the saved operator token.

    Automatically retries on 5xx and network errors with exponential
    backoff.  Raises:
      - InvalidTokenError on 401 (no retry)
      - TelemetryDuplicateError on 409 (caller may treat as success)
      - PlatformUnreachableError on persistent network/5xx after retries

    Args:
        max_retries: override the default retry count.  Set to 0 for
            fire-and-forget callers (e.g. heartbeats at 60s cadence)
            that shouldn't pile up retries across ticks.

    Returns the parsed JSON response body on success.
    """
    retries = max_retries if max_retries is not None else _MAX_RETRIES
    state = load_state()
    if state is None:
        raise PlatformUnreachableError(
            "Not connected to platform — no saved state. "
            "Run connect_to_platform() first."
        )

    url = f"{state.platform_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {state.operator_token}",
        "Content-Type": "application/json",
    }

    # Always at least 1 attempt; max_retries controls additional retries.
    total_attempts = max(1, retries)
    last_exc: Exception | None = None
    for attempt in range(total_attempts):
        if attempt > 0:
            backoff = _BASE_BACKOFF_S * (2 ** (attempt - 1))
            logger.debug(
                f"Retry {attempt}/{total_attempts - 1} for POST {path} "
                f"after {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.debug(f"POST {path} network error (attempt {attempt + 1}): {exc}")
            continue

        if resp.status_code == 200 or resp.status_code == 201:
            return resp.json()

        if resp.status_code == 409:
            # Duplicate — platform already has this data.  Idempotent success.
            raise TelemetryDuplicateError(
                f"Platform rejected POST {path} with 409 — already ingested"
            )

        if resp.status_code == 401:
            # Token revoked — no point retrying
            raise InvalidTokenError(
                "Operator token rejected. Reconnect via dashboard or "
                "regenerate at gotomy.ai/web/"
            )

        if resp.status_code >= 500:
            # Server-side issue — retry
            last_exc = PlatformUnreachableError(
                f"Platform returned {resp.status_code}: {resp.text[:200]}"
            )
            logger.debug(
                f"POST {path} 5xx (attempt {attempt + 1}): {resp.status_code}"
            )
            continue

        # 4xx other than 401/409 — client error, don't retry
        raise PlatformUnreachableError(
            f"POST {path} returned {resp.status_code}: {resp.text[:200]}"
        )

    # All retries exhausted
    if isinstance(last_exc, httpx.HTTPError):
        raise PlatformUnreachableError(
            f"Cannot reach platform at {url} after {total_attempts} attempts: "
            f"{last_exc}"
        ) from last_exc
    if last_exc is not None:
        raise last_exc
    raise PlatformUnreachableError(
        f"POST {path} failed after {total_attempts} attempts"
    )


async def post_local_summary(payload: dict) -> dict[str, Any]:
    """POST /api/telemetry/local-summary with the daily rollup payload.

    Handles 409 (idempotent) by converting to a success-like return value
    so callers can update their state file without raising.
    """
    try:
        return await post("/api/telemetry/local-summary", json=payload)
    except TelemetryDuplicateError:
        logger.info(
            f"telemetry: summary for {payload.get('day', '?')} "
            f"already ingested (409) — treating as success"
        )
        return {"status": "duplicate", "day": payload.get("day")}
