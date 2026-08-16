"""Tests for the anonymous community-telemetry payload.

These enforce a *published* promise (ollamaherd.com/telemetry) and a
cross-repo wire contract, so they are stricter than typical unit tests:
they assert set equality on field names rather than membership.  A payload
that grows a field silently is the exact failure mode they exist to stop.
"""

from __future__ import annotations

import socket

import pytest

from fleet_manager.node.anonymous_rollup import (
    ALLOWED_ENTRY_KEYS,
    ALLOWED_PAYLOAD_KEYS,
    build_anonymous_rollup,
)

INSTALL_ID = "11111111-1111-1111-1111-111111111111"

# Mirror of the receiving service's Pydantic models
# (ollama-herd-private: telemetry/app/schemas.py).  Duplicated deliberately:
# the OSS package must not import the private service, but the wire contract
# still has to be pinned somewhere that fails loudly when it drifts.  The
# server validates with extra="forbid", so an extra key here is a 422 for the
# entire payload -- not a dropped field.
SERVER_PAYLOAD_FIELDS = {
    "install_id",
    "agent_version",
    "day",
    "entries",
    "errors",
    "nickname",
    "fleet_node_count",
    "fleet_memory_gb",
}
SERVER_ENTRY_FIELDS = {
    "model",
    "requests",
    "success_count",
    "error_count",
    "prompt_tokens",
    "completion_tokens",
    "p50_latency_ms",
    "p95_latency_ms",
}


class TestWireContract:
    def test_payload_whitelist_matches_server_schema(self):
        assert ALLOWED_PAYLOAD_KEYS == SERVER_PAYLOAD_FIELDS

    def test_entry_whitelist_matches_server_schema(self):
        assert ALLOWED_ENTRY_KEYS == SERVER_ENTRY_FIELDS

    @pytest.mark.asyncio
    async def test_built_payload_contains_only_whitelisted_keys(self, tmp_path):
        payload = await build_anonymous_rollup(
            INSTALL_ID, "0.9.1", data_dir=str(tmp_path), day="2026-08-10"
        )
        assert set(payload).issubset(ALLOWED_PAYLOAD_KEYS)

    @pytest.mark.asyncio
    async def test_entries_contain_only_whitelisted_keys(self, tmp_path):
        payload = await build_anonymous_rollup(
            INSTALL_ID, "0.9.1", data_dir=str(tmp_path), day="2026-08-10"
        )
        for entry in payload["entries"]:
            assert set(entry) == ALLOWED_ENTRY_KEYS


class TestPrivacy:
    @pytest.mark.asyncio
    async def test_hostname_never_appears_anywhere_in_payload(self, tmp_path):
        """The promise the public telemetry page makes by name."""
        payload = await build_anonymous_rollup(
            INSTALL_ID, "0.9.1", data_dir=str(tmp_path), day="2026-08-10"
        )
        blob = repr(payload).lower()
        hostname = socket.gethostname()
        assert hostname.lower() not in blob
        for part in hostname.replace(".", "-").replace("_", "-").split("-"):
            if len(part) > 2:
                assert part.lower() not in blob, f"hostname fragment {part!r} leaked"

    @pytest.mark.asyncio
    async def test_nickname_omitted_entirely_when_not_opted_in(self, tmp_path):
        """Anonymous is the default: no null placeholder, no key at all."""
        payload = await build_anonymous_rollup(
            INSTALL_ID, "0.9.1", data_dir=str(tmp_path), day="2026-08-10"
        )
        assert "nickname" not in payload
        assert "fleet_node_count" not in payload
        assert "fleet_memory_gb" not in payload

    @pytest.mark.asyncio
    async def test_fleet_shape_requires_a_nickname(self, tmp_path):
        """Fleet specs are only public alongside a name; never on their own."""
        payload = await build_anonymous_rollup(
            INSTALL_ID,
            "0.9.1",
            data_dir=str(tmp_path),
            day="2026-08-10",
            nickname="",
            fleet_node_count=4,
            fleet_memory_gb=640,
        )
        assert "fleet_node_count" not in payload
        assert "fleet_memory_gb" not in payload

    @pytest.mark.asyncio
    async def test_nickname_opt_in_includes_fleet_shape(self, tmp_path):
        payload = await build_anonymous_rollup(
            INSTALL_ID,
            "0.9.1",
            data_dir=str(tmp_path),
            day="2026-08-10",
            nickname="NeonHerd",
            fleet_node_count=2,
            fleet_memory_gb=640,
        )
        assert payload["nickname"] == "NeonHerd"
        assert payload["fleet_node_count"] == 2


class TestServerBounds:
    """Fail locally rather than eating a 422 nobody reads."""

    @pytest.mark.asyncio
    async def test_agent_version_is_truncated(self, tmp_path):
        payload = await build_anonymous_rollup(
            INSTALL_ID, "x" * 100, data_dir=str(tmp_path), day="2026-08-10"
        )
        assert len(payload["agent_version"]) <= 32

    @pytest.mark.asyncio
    async def test_nickname_is_truncated(self, tmp_path):
        payload = await build_anonymous_rollup(
            INSTALL_ID,
            "0.9.1",
            data_dir=str(tmp_path),
            day="2026-08-10",
            nickname="N" * 100,
        )
        assert len(payload["nickname"]) <= 30

    @pytest.mark.asyncio
    async def test_empty_day_still_produces_a_valid_payload(self, tmp_path):
        """An idle install must still report -- that is how retention is
        distinguished from churn."""
        payload = await build_anonymous_rollup(
            INSTALL_ID, "0.9.1", data_dir=str(tmp_path), day="2026-08-10"
        )
        assert payload["entries"] == []
        assert payload["errors"] == {}
        assert payload["install_id"] == INSTALL_ID
        assert payload["day"] == "2026-08-10"
