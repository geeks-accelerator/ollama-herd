"""Tests for telemetry_scheduler — state file + emit + failure handling."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from fleet_manager.node import platform_client, platform_connection, telemetry_scheduler
from fleet_manager.node.platform_connection import ConnectionState
from fleet_manager.node.telemetry_scheduler import (
    _emit_once,
    _load_last_sent_day,
    _save_last_sent_day,
    _seconds_until_next_run,
)


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Redirect all state files to a temp dir."""
    monkeypatch.setattr(platform_connection, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        platform_connection, "STATE_FILE", tmp_path / "platform.json"
    )
    monkeypatch.setattr(
        telemetry_scheduler, "_STATE_FILE", tmp_path / "telemetry_state.json"
    )
    return tmp_path


@pytest.fixture
async def connected_with_data(clean_state):
    """Platform connected + latency.db seeded with yesterday's data."""
    # Save connection state
    platform_connection.save_state(
        ConnectionState(
            platform_url="https://platform.example.com",
            operator_token="herd_test",
            node_id="uuid-test",
            connected_at=datetime.now(UTC),
        )
    )
    # Seed latency.db
    data_dir = clean_state
    db_path = data_dir / "latency.db"
    from fleet_manager.node.daily_rollup import _yesterday_utc_bounds

    _, start_ts, _ = _yesterday_utc_bounds()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            CREATE TABLE latency_observations (
                id INTEGER PRIMARY KEY,
                node_id TEXT, model_name TEXT, latency_ms REAL,
                tokens_generated INTEGER, timestamp REAL,
                prompt_tokens INTEGER, completion_tokens INTEGER
            )
            """
        )
        await db.execute(
            "INSERT INTO latency_observations (node_id, model_name, latency_ms, "
            "tokens_generated, timestamp, prompt_tokens, completion_tokens) "
            "VALUES ('n1', 'llama3:8b', 250.0, 100, ?, 50, 100)",
            (start_ts + 3600,),
        )
        await db.commit()

    # Point the rollup builder at our temp data_dir
    import fleet_manager.node.daily_rollup as _rollup

    orig_build = _rollup.build_daily_rollup

    async def _patched(**kwargs):
        kwargs["data_dir"] = str(data_dir)
        return await orig_build(**kwargs)

    _rollup.build_daily_rollup = _patched
    yield clean_state
    _rollup.build_daily_rollup = orig_build


# ---------------------------------------------------------------------------
# State file persistence
# ---------------------------------------------------------------------------


class TestStateFile:
    def test_load_returns_none_when_missing(self, clean_state):
        assert _load_last_sent_day() is None

    def test_save_and_load_roundtrip(self, clean_state):
        _save_last_sent_day("2026-04-19")
        assert _load_last_sent_day() == "2026-04-19"

    def test_save_atomic_via_tmp_rename(self, clean_state):
        _save_last_sent_day("2026-04-18")
        _save_last_sent_day("2026-04-19")
        assert _load_last_sent_day() == "2026-04-19"

    def test_load_handles_corrupt_file(self, clean_state):
        (clean_state / "telemetry_state.json").write_text("not json{{{")
        assert _load_last_sent_day() is None


# ---------------------------------------------------------------------------
# Scheduling math
# ---------------------------------------------------------------------------


class TestScheduling:
    def test_next_run_is_within_24h_plus_jitter(self):
        delay = _seconds_until_next_run()
        # 24h + max 10 min jitter
        assert 0 <= delay <= 86400 + 600 + 1

    def test_next_run_before_00_05_today_uses_today(self):
        now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        delay = _seconds_until_next_run(now=now)
        # Should be ~5 min + jitter (not 24h)
        assert delay < 900  # 15 min upper bound

    def test_next_run_after_00_05_today_uses_tomorrow(self):
        now = datetime.now(UTC).replace(hour=23, minute=59, second=0, microsecond=0)
        delay = _seconds_until_next_run(now=now)
        # Should be close to 6 minutes (midnight + 5 = next_run)
        assert delay > 60  # at least 1 minute
        assert delay < 1000  # but less than ~16 min


# ---------------------------------------------------------------------------
# _emit_once() — failure modes
# ---------------------------------------------------------------------------


class TestEmitOnce:
    @pytest.mark.asyncio
    async def test_no_connection_returns_false(self, clean_state):
        result = await _emit_once(include_tags=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_already_sent_today_returns_true_without_posting(
        self, connected_with_data, httpx_mock
    ):
        # Mark today as already sent
        from fleet_manager.node.daily_rollup import _yesterday_utc_bounds

        day, _, _ = _yesterday_utc_bounds()
        _save_last_sent_day(day)

        result = await _emit_once(include_tags=False)
        assert result is True
        # No HTTP call should have been made
        assert len(httpx_mock.get_requests()) == 0

    @pytest.mark.asyncio
    async def test_successful_emit_saves_state(
        self, connected_with_data, httpx_mock
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/telemetry/local-summary",
            method="POST",
            json={"message": "Summary ingested."},
            status_code=200,
        )
        result = await _emit_once(include_tags=False)
        assert result is True
        # State file written
        from fleet_manager.node.daily_rollup import _yesterday_utc_bounds

        day, _, _ = _yesterday_utc_bounds()
        assert _load_last_sent_day() == day

    @pytest.mark.asyncio
    async def test_409_treated_as_success(self, connected_with_data, httpx_mock):
        httpx_mock.add_response(
            url="https://platform.example.com/api/telemetry/local-summary",
            method="POST",
            status_code=409,
        )
        result = await _emit_once(include_tags=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_401_returns_false_without_saving_state(
        self, connected_with_data, httpx_mock
    ):
        httpx_mock.add_response(
            url="https://platform.example.com/api/telemetry/local-summary",
            method="POST",
            status_code=401,
        )
        result = await _emit_once(include_tags=False)
        assert result is False
        # State NOT saved (so retry is possible after reconnect)
        assert _load_last_sent_day() is None

    @pytest.mark.asyncio
    async def test_5xx_returns_false_without_saving(
        self, connected_with_data, httpx_mock, monkeypatch
    ):
        # Speed up retries to keep the test fast
        monkeypatch.setattr(platform_client, "_BASE_BACKOFF_S", 0.001)
        for _ in range(3):
            httpx_mock.add_response(
                url="https://platform.example.com/api/telemetry/local-summary",
                method="POST",
                status_code=503,
            )
        result = await _emit_once(include_tags=False)
        assert result is False
        assert _load_last_sent_day() is None

    @pytest.mark.asyncio
    async def test_empty_day_saves_state_without_posting(
        self, clean_state, httpx_mock
    ):
        """Days with zero requests are marked handled but not POSTed."""
        # Connect but seed an empty latency.db
        platform_connection.save_state(
            ConnectionState(
                platform_url="https://platform.example.com",
                operator_token="herd_test",
                node_id="uuid-test",
                connected_at=datetime.now(UTC),
            )
        )
        data_dir = clean_state
        db_path = data_dir / "latency.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                """
                CREATE TABLE latency_observations (
                    id INTEGER PRIMARY KEY, node_id TEXT, model_name TEXT,
                    latency_ms REAL, tokens_generated INTEGER, timestamp REAL,
                    prompt_tokens INTEGER, completion_tokens INTEGER
                )
                """
            )
            await db.commit()

        # Point rollup at our temp dir
        import fleet_manager.node.daily_rollup as _rollup

        orig = _rollup.build_daily_rollup

        async def _patched(**kwargs):
            kwargs["data_dir"] = str(data_dir)
            return await orig(**kwargs)

        _rollup.build_daily_rollup = _patched
        try:
            result = await _emit_once(include_tags=False)
        finally:
            _rollup.build_daily_rollup = orig

        assert result is True
        # No HTTP request made (since entries was empty)
        assert len(httpx_mock.get_requests()) == 0
        # State marked so we don't retry tomorrow
        from fleet_manager.node.daily_rollup import _yesterday_utc_bounds

        day, _, _ = _yesterday_utc_bounds()
        assert _load_last_sent_day() == day
