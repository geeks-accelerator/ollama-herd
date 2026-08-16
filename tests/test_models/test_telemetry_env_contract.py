"""The telemetry env-var names are a published contract, not an internal detail.

ollamaherd.com/telemetry documents FLEET_NODE_TELEMETRY=false as *the* opt-out,
and the dashboard writes FLEET_NODE_HERD_NICKNAME.  The sender moved from the
node to the router in v2, and ``env_prefix="FLEET_"`` silently repointed it at
FLEET_TELEMETRY -- so an opt-out that the page promises worked did nothing, and
a nickname set in the UI never reached a payload.

Both failed silently because "off" and "unset" are indistinguishable in a bool
default, and "" means anonymous rather than misconfigured.  These tests exist so
the next relocation fails the build instead of the user.
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import NodeSettings, ServerSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "FLEET_NODE_TELEMETRY", "FLEET_TELEMETRY",
        "FLEET_NODE_HERD_NICKNAME", "FLEET_HERD_NICKNAME",
        "FLEET_NODE_TELEMETRY_URL", "FLEET_TELEMETRY_URL",
    ):
        monkeypatch.delenv(key, raising=False)


class TestPublishedOptOut:
    """A default-on feature whose documented opt-out does nothing is the
    worst bug this feature can have."""

    def test_documented_opt_out_stops_the_router_sender(self, monkeypatch):
        monkeypatch.setenv("FLEET_NODE_TELEMETRY", "false")
        assert ServerSettings().telemetry is False

    def test_documented_opt_out_also_stops_the_node(self, monkeypatch):
        monkeypatch.setenv("FLEET_NODE_TELEMETRY", "false")
        assert NodeSettings().telemetry is False

    def test_unprefixed_variant_is_also_honoured(self, monkeypatch):
        """Accepted as an alias so a router-centric operator isn't surprised."""
        monkeypatch.setenv("FLEET_TELEMETRY", "false")
        assert ServerSettings().telemetry is False

    def test_default_is_on_when_nothing_is_set(self):
        assert ServerSettings().telemetry is True


class TestNicknameReachesTheSender:
    def test_dashboard_written_key_is_read_by_the_router(self, monkeypatch):
        """The dashboard writes the FLEET_NODE_ form; the sender must see it."""
        monkeypatch.setenv("FLEET_NODE_HERD_NICKNAME", "LiveNeon @ Geeks in the Woods")
        assert ServerSettings().herd_nickname == "LiveNeon @ Geeks in the Woods"

    def test_empty_means_anonymous(self):
        assert ServerSettings().herd_nickname == ""

    def test_nickname_reaches_a_built_payload(self, monkeypatch):
        """End of the chain: env -> settings -> payload."""
        import asyncio

        monkeypatch.setenv("FLEET_NODE_HERD_NICKNAME", "NeonHerd")
        from fleet_manager.node.anonymous_rollup import build_anonymous_rollup

        settings = ServerSettings()
        payload = asyncio.run(
            build_anonymous_rollup(
                "11111111-1111-1111-1111-111111111111",
                "0.9.1",
                day="2026-08-10",
                nickname=settings.herd_nickname,
            )
        )
        assert payload["nickname"] == "NeonHerd"


class TestUrlOverride:
    def test_node_prefixed_url_is_honoured(self, monkeypatch):
        monkeypatch.setenv("FLEET_NODE_TELEMETRY_URL", "http://localhost:9/x")
        assert ServerSettings().telemetry_url == "http://localhost:9/x"
