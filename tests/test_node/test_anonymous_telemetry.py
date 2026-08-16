"""Tests for the anonymous telemetry sender.

The opt-out tests back a published promise; the resilience tests back the rule
that telemetry must never affect inference.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fleet_manager.node.anonymous_telemetry import (
    _load_last_sent_day,
    _save_last_sent_day,
    _seconds_until_next_run,
    send_once,
)

YESTERDAY = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")


class FakeSettings:
    def __init__(self, **kw):
        self.telemetry = kw.get("telemetry", True)
        self.telemetry_url = kw.get("telemetry_url", "https://example.test/api/v1/telemetry")
        self.herd_nickname = kw.get("herd_nickname", "")
        self.data_dir = kw.get("data_dir", "~/.fleet-manager")


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state.json"


class TestOptOut:
    @pytest.mark.asyncio
    async def test_disabled_sends_nothing(self, state, tmp_path):
        s = FakeSettings(telemetry=False, data_dir=str(tmp_path))
        assert await send_once(s, "0.9.1", state) is False

    @pytest.mark.asyncio
    async def test_disabled_creates_no_files(self, state, tmp_path):
        """A user who opted out gets no state written on their behalf."""
        s = FakeSettings(telemetry=False, data_dir=str(tmp_path))
        await send_once(s, "0.9.1", state)
        assert not state.exists()

    @pytest.mark.asyncio
    async def test_disabled_does_not_build_a_payload(self, state, tmp_path, monkeypatch):
        """Opt-out short-circuits before install_id is even generated."""
        called = []
        monkeypatch.setattr(
            "fleet_manager.common.install_id.get_install_id",
            lambda *a, **k: called.append(1) or "x",
        )
        s = FakeSettings(telemetry=False, data_dir=str(tmp_path))
        await send_once(s, "0.9.1", state)
        assert called == [], "install_id must not be created when opted out"


class TestSending:
    @pytest.mark.asyncio
    async def test_successful_send_records_the_day(self, state, tmp_path, monkeypatch):
        sent = {}

        async def fake_post(self, url, json=None, **kw):
            sent["url"] = url
            sent["payload"] = json
            return httpx.Response(202, json={"status": "accepted"})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        s = FakeSettings(data_dir=str(tmp_path))
        assert await send_once(s, "0.9.1", state) is True
        assert _load_last_sent_day(state) == YESTERDAY
        assert sent["payload"]["day"] == YESTERDAY
        assert sent["payload"]["agent_version"] == "0.9.1"

    @pytest.mark.asyncio
    async def test_does_not_resend_the_same_day(self, state, tmp_path, monkeypatch):
        calls = []

        async def fake_post(self, url, json=None, **kw):
            calls.append(1)
            return httpx.Response(202, json={})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        s = FakeSettings(data_dir=str(tmp_path))
        await send_once(s, "0.9.1", state)
        await send_once(s, "0.9.1", state)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_nickname_is_forwarded_when_set(self, state, tmp_path, monkeypatch):
        sent = {}

        async def fake_post(self, url, json=None, **kw):
            sent.update(json or {})
            return httpx.Response(202, json={})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        s = FakeSettings(herd_nickname="NeonHerd", data_dir=str(tmp_path))
        await send_once(s, "0.9.1", state)
        assert sent["nickname"] == "NeonHerd"

    @pytest.mark.asyncio
    async def test_anonymous_payload_carries_no_nickname(self, state, tmp_path, monkeypatch):
        sent = {}

        async def fake_post(self, url, json=None, **kw):
            sent.update(json or {})
            return httpx.Response(202, json={})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        await send_once(FakeSettings(data_dir=str(tmp_path)), "0.9.1", state)
        assert "nickname" not in sent


class TestNeverEscalates:
    """Telemetry must never be able to affect inference."""

    @pytest.mark.asyncio
    async def test_network_failure_is_swallowed(self, state, tmp_path, monkeypatch):
        async def boom(self, url, json=None, **kw):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx.AsyncClient, "post", boom)
        s = FakeSettings(data_dir=str(tmp_path))
        assert await send_once(s, "0.9.1", state) is False
        assert _load_last_sent_day(state) is None, "a failed send must retry tomorrow"

    @pytest.mark.asyncio
    async def test_server_error_retries_tomorrow(self, state, tmp_path, monkeypatch):
        async def fail(self, url, json=None, **kw):
            return httpx.Response(500, text="boom")

        monkeypatch.setattr(httpx.AsyncClient, "post", fail)
        assert await send_once(FakeSettings(data_dir=str(tmp_path)), "0.9.1", state) is False
        assert _load_last_sent_day(state) is None

    @pytest.mark.asyncio
    async def test_rejected_payload_is_not_retried_forever(self, state, tmp_path, monkeypatch):
        """A 4xx means this build is wrong; retrying daily would just be noise."""

        async def reject(self, url, json=None, **kw):
            return httpx.Response(422, json={"detail": "bad"})

        monkeypatch.setattr(httpx.AsyncClient, "post", reject)
        assert await send_once(FakeSettings(data_dir=str(tmp_path)), "0.9.1", state) is False
        assert _load_last_sent_day(state) == YESTERDAY

    @pytest.mark.asyncio
    async def test_corrupt_state_file_is_survivable(self, state, tmp_path, monkeypatch):
        state.write_text("{not json")

        async def ok(self, url, json=None, **kw):
            return httpx.Response(202, json={})

        monkeypatch.setattr(httpx.AsyncClient, "post", ok)
        assert await send_once(FakeSettings(data_dir=str(tmp_path)), "0.9.1", state) is True


class TestSchedule:
    def test_next_run_is_within_a_day(self):
        delay = _seconds_until_next_run()
        assert 0 < delay <= 86400 + 600

    def test_jitter_spreads_the_fleet(self):
        """Every node firing at the same second would look like an attack."""
        delays = {_seconds_until_next_run() for _ in range(20)}
        assert len(delays) > 1

    def test_state_round_trips(self, state):
        _save_last_sent_day("2026-08-14", state)
        assert _load_last_sent_day(state) == "2026-08-14"
        assert json.loads(state.read_text())["last_sent_day"] == "2026-08-14"


class TestStartupCatchUp:
    """Without this, start time of day decides whether an install ever reports.

    The wall-clock schedule fires at 00:05 UTC, so a router started at 00:10
    waits 23h55m for its first send, and one restarted daily after 00:05 never
    sends at all. Both are indistinguishable from "nobody uses this" on the
    receiving end. Our own fleet ran 12 hours with zero automatic sends before
    this existed -- and the account-side scheduler had already solved it.
    """

    @pytest.mark.asyncio
    async def test_catch_up_sends_before_sleeping(self, state, tmp_path, monkeypatch):
        import asyncio

        from fleet_manager.server import community_telemetry as ct

        sent = []

        async def fake_send(settings, registry, agent_version, state_file=None):
            sent.append(agent_version)
            return True

        # A sleep long enough that anything sent must have come from catch-up.
        monkeypatch.setattr(ct, "send_once", fake_send)
        monkeypatch.setattr(ct, "_seconds_until_next_run", lambda *a, **k: 3600)

        task = asyncio.create_task(
            ct.run_scheduler(FakeSettings(data_dir=str(tmp_path)), object(), "0.9.2")
        )
        await asyncio.sleep(0.05)
        task.cancel()
        assert sent == ["0.9.2"], "startup catch-up did not fire before the sleep"

    @pytest.mark.asyncio
    async def test_catch_up_can_be_disabled(self, tmp_path, monkeypatch):
        import asyncio

        from fleet_manager.server import community_telemetry as ct

        sent = []

        async def fake_send(*a, **k):
            sent.append(1)
            return True

        monkeypatch.setattr(ct, "send_once", fake_send)
        monkeypatch.setattr(ct, "_seconds_until_next_run", lambda *a, **k: 3600)

        task = asyncio.create_task(
            ct.run_scheduler(
                FakeSettings(data_dir=str(tmp_path)), object(), "0.9.2",
                startup_catchup=False,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        assert sent == []

    @pytest.mark.asyncio
    async def test_opt_out_still_wins_over_catch_up(self, tmp_path, monkeypatch):
        """A disabled install must not send on startup either."""
        import asyncio

        from fleet_manager.server import community_telemetry as ct

        sent = []

        async def fake_send(*a, **k):
            sent.append(1)
            return True

        monkeypatch.setattr(ct, "send_once", fake_send)
        task = asyncio.create_task(
            ct.run_scheduler(
                FakeSettings(telemetry=False, data_dir=str(tmp_path)), object(), "0.9.2"
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        assert sent == []

    @pytest.mark.asyncio
    async def test_catch_up_failure_does_not_stop_the_scheduler(self, tmp_path, monkeypatch):
        import asyncio

        from fleet_manager.server import community_telemetry as ct

        async def boom(*a, **k):
            raise RuntimeError("network down at boot")

        monkeypatch.setattr(ct, "send_once", boom)
        monkeypatch.setattr(ct, "_seconds_until_next_run", lambda *a, **k: 3600)
        task = asyncio.create_task(
            ct.run_scheduler(FakeSettings(data_dir=str(tmp_path)), object(), "0.9.2")
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "a failed catch-up must not kill the daily loop"
        task.cancel()
