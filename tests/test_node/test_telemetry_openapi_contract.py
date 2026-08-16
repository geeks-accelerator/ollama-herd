"""Check the payload builder against the telemetry service's *generated* schema.

The hand-maintained mirror in ``test_anonymous_rollup.py`` has already failed
once in the way hand-maintained mirrors do: it pinned the payload and entry
levels, so when ``devices[]`` was added it created a third wire contract with
nothing watching it, and production was the first thing to notice (a 422 on
every send).  Fetching the schema the server actually generates closes that
gap -- it covers levels nobody remembered to write down, including ones that
do not exist yet.

**These tests skip rather than fail when the service is unreachable.**  A
contributor running ``pytest`` on a plane, behind a proxy, or during an
ollamaherd.com blip has changed nothing and must not get a red build.  The
static mirror remains the offline guarantee; this is the authority when it is
reachable.  Opt in explicitly with ``FLEET_TEST_REMOTE_CONTRACT=1`` in CI if
you want a hard failure there.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

OPENAPI_URL = os.environ.get(
    "FLEET_TELEMETRY_OPENAPI_URL", "https://ollamaherd.com/api/v1/openapi.json"
)
REQUIRE_REMOTE = os.environ.get("FLEET_TEST_REMOTE_CONTRACT") == "1"


def _fetch_schema() -> dict:
    try:
        with urllib.request.urlopen(OPENAPI_URL, timeout=10) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if REQUIRE_REMOTE:
            pytest.fail(f"telemetry OpenAPI unreachable and required: {exc}")
        pytest.skip(f"telemetry OpenAPI unreachable ({type(exc).__name__}); "
                    "static mirror still covers this offline")


@pytest.fixture(scope="module")
def schemas() -> dict:
    return _fetch_schema().get("components", {}).get("schemas", {})


def _properties(schemas: dict, *candidates: str) -> set[str]:
    """Return the property names of the first schema whose name matches."""
    for name in candidates:
        if name in schemas:
            return set(schemas[name].get("properties", {}))
    for name, body in schemas.items():
        if any(c.lower() in name.lower() for c in candidates):
            return set(body.get("properties", {}))
    pytest.skip(f"no schema matching {candidates} in the published OpenAPI")


class TestAgainstGeneratedSchema:
    def test_payload_whitelist_matches(self, schemas):
        from fleet_manager.node.anonymous_rollup import ALLOWED_PAYLOAD_KEYS

        server = _properties(schemas, "TelemetryPayload", "Payload")
        assert ALLOWED_PAYLOAD_KEYS == server, (
            f"client-only: {ALLOWED_PAYLOAD_KEYS - server} | "
            f"server-only: {server - ALLOWED_PAYLOAD_KEYS}"
        )

    def test_entry_whitelist_matches(self, schemas):
        from fleet_manager.node.anonymous_rollup import ALLOWED_ENTRY_KEYS

        server = _properties(schemas, "ModelEntry", "Entry")
        assert ALLOWED_ENTRY_KEYS == server, (
            f"client-only: {ALLOWED_ENTRY_KEYS - server} | "
            f"server-only: {server - ALLOWED_ENTRY_KEYS}"
        )

    def test_device_fields_match(self, schemas):
        """The level the hand-written mirror missed."""
        from fleet_manager.server.community_telemetry import DEVICE_FIELDS

        server = _properties(schemas, "DeviceEntry", "Device")
        assert DEVICE_FIELDS == server, (
            f"client-only: {DEVICE_FIELDS - server} | "
            f"server-only: {server - DEVICE_FIELDS}"
        )

    def test_static_mirror_agrees_with_the_live_schema(self, schemas):
        """If these disagree, the offline mirror has silently gone stale."""
        from tests.test_node.test_anonymous_rollup import (
            SERVER_ENTRY_FIELDS,
            SERVER_PAYLOAD_FIELDS,
        )

        assert SERVER_PAYLOAD_FIELDS == _properties(schemas, "TelemetryPayload", "Payload")
        assert SERVER_ENTRY_FIELDS == _properties(schemas, "ModelEntry", "Entry")


class TestBuiltPayloadValidates:
    def test_a_real_payload_uses_only_published_fields(self, schemas, tmp_path):
        import asyncio

        from fleet_manager.node.anonymous_rollup import build_anonymous_rollup

        payload = asyncio.run(
            build_anonymous_rollup(
                "11111111-1111-1111-1111-111111111111",
                "0.9.2",
                data_dir=str(tmp_path),
                day="2026-08-10",
                nickname="NeonHerd",
            )
        )
        server = _properties(schemas, "TelemetryPayload", "Payload")
        assert set(payload) <= server, f"would 422 on: {set(payload) - server}"
