"""Unit tests for daily_rollup — WITH PRIVACY INVARIANT ENFORCEMENT.

The most critical tests here assert that the payload dict's keys
match exactly the whitelisted set, preventing future contributors
from casually adding fields that leak user data.
"""

from __future__ import annotations

import aiosqlite
import pytest

from fleet_manager.node.daily_rollup import (
    ALLOWED_ENTRY_KEYS,
    ALLOWED_PAYLOAD_KEYS,
    _percentile,
    _yesterday_utc_bounds,
    build_daily_rollup,
)


@pytest.fixture
async def seeded_db(tmp_path):
    """Create a ~/.fleet-manager/latency.db with known data for yesterday UTC."""
    data_dir = tmp_path / ".fleet-manager"
    data_dir.mkdir()
    db_path = data_dir / "latency.db"

    # Get yesterday's midnight UTC as our seed timestamps
    _, start_ts, _ = _yesterday_utc_bounds()

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            CREATE TABLE latency_observations (
                id INTEGER PRIMARY KEY,
                node_id TEXT,
                model_name TEXT,
                latency_ms REAL,
                tokens_generated INTEGER,
                timestamp REAL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER
            )
            """
        )
        # 3 observations of llama3:8b at varying latencies
        for i, lat in enumerate([200.0, 300.0, 500.0]):
            await db.execute(
                "INSERT INTO latency_observations (node_id, model_name, "
                "latency_ms, tokens_generated, timestamp, prompt_tokens, "
                "completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("n1", "llama3:8b", lat, 100, start_ts + 3600 + i, 50, 100),
            )
        # 2 observations of gpt-oss:120b
        for i, lat in enumerate([800.0, 1200.0]):
            await db.execute(
                "INSERT INTO latency_observations (node_id, model_name, "
                "latency_ms, tokens_generated, timestamp, prompt_tokens, "
                "completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("n1", "gpt-oss:120b", lat, 200, start_ts + 7200 + i, 30, 200),
            )
        # 1 observation outside the window (should be excluded)
        await db.execute(
            "INSERT INTO latency_observations (node_id, model_name, "
            "latency_ms, tokens_generated, timestamp, prompt_tokens, "
            "completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("n1", "llama3:8b", 100.0, 50, start_ts - 7200, 20, 50),  # 2h before yesterday
        )
        await db.commit()

    return str(data_dir)


# ---------------------------------------------------------------------------
# PRIVACY INVARIANT TESTS — these are the most important ones
# ---------------------------------------------------------------------------


class TestPrivacyInvariants:
    """Enforces the whitelist of keys that can appear in telemetry payloads.

    If a new field needs to go to the platform, it MUST be added to
    ALLOWED_ENTRY_KEYS or ALLOWED_PAYLOAD_KEYS in daily_rollup.py AND
    these tests MUST be updated.  Don't shortcut them.
    """

    def test_allowed_entry_keys_is_exact(self):
        """Regression guard: the whitelist is frozen at this exact set."""
        assert frozenset({
            "model",
            "local_requests",
            "local_prompt_tokens",
            "local_completion_tokens",
            "p2p_served_requests",
            "p2p_served_tokens",
            "avg_latency_ms",
            "p95_latency_ms",
            "request_count_by_tag",
            "success_count",
            "error_count",
            "error_breakdown",
        }) == ALLOWED_ENTRY_KEYS

    def test_allowed_payload_keys_is_exact(self):
        assert frozenset({
            "day",
            "node_id",
            "agent_version",
            "entries",
        }) == ALLOWED_PAYLOAD_KEYS

    @pytest.mark.asyncio
    async def test_payload_contains_only_whitelisted_top_level_keys(self, seeded_db):
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        assert set(payload.keys()) <= ALLOWED_PAYLOAD_KEYS

    @pytest.mark.asyncio
    async def test_entries_contain_only_whitelisted_keys(self, seeded_db):
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        for entry in payload["entries"]:
            assert set(entry.keys()) <= ALLOWED_ENTRY_KEYS, (
                f"Entry has non-whitelisted keys: "
                f"{set(entry.keys()) - ALLOWED_ENTRY_KEYS}"
            )

    @pytest.mark.asyncio
    async def test_payload_never_contains_prompt_or_completion_text(self, seeded_db):
        """Regression guard: the words 'prompt' and 'completion' appear
        only as token counts, never as full content."""
        import json
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        # The payload should serialize cleanly to JSON (verifies no
        # unexpected types leak in).
        json.dumps(payload)  # raises if payload has non-serializable data
        # Any key mentioning tokens must be a number, not a string
        for entry in payload["entries"]:
            assert isinstance(entry["local_prompt_tokens"], int)
            assert isinstance(entry["local_completion_tokens"], int)

    @pytest.mark.asyncio
    async def test_tags_excluded_by_default(self, seeded_db):
        """Tag counts are a SECOND opt-in — not included unless asked."""
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
            include_tags=False,  # explicit default
        )
        for entry in payload["entries"]:
            assert "request_count_by_tag" not in entry, (
                "Tag counts must not appear in payload when include_tags=False"
            )


# ---------------------------------------------------------------------------
# Aggregation correctness tests
# ---------------------------------------------------------------------------


class TestAggregation:
    @pytest.mark.asyncio
    async def test_per_model_aggregation(self, seeded_db):
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        # Two models should appear (llama3:8b had 3 rows, gpt-oss:120b had 2)
        by_model = {e["model"]: e for e in payload["entries"]}
        assert "llama3:8b" in by_model
        assert "gpt-oss:120b" in by_model

        llama = by_model["llama3:8b"]
        assert llama["local_requests"] == 3
        assert llama["local_prompt_tokens"] == 150  # 3 * 50
        assert llama["local_completion_tokens"] == 300  # 3 * 100

        gpt = by_model["gpt-oss:120b"]
        assert gpt["local_requests"] == 2
        assert gpt["local_prompt_tokens"] == 60  # 2 * 30
        assert gpt["local_completion_tokens"] == 400  # 2 * 200

    @pytest.mark.asyncio
    async def test_observation_outside_window_excluded(self, seeded_db):
        """The row seeded outside yesterday's window should not be counted."""
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        by_model = {e["model"]: e for e in payload["entries"]}
        # llama3:8b should have 3 requests, not 4 (the 4th is outside window)
        assert by_model["llama3:8b"]["local_requests"] == 3

    @pytest.mark.asyncio
    async def test_p95_latency_computed(self, seeded_db):
        """p95 should be a reasonable number given the seeded latencies."""
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        by_model = {e["model"]: e for e in payload["entries"]}
        # llama3:8b latencies: 200, 300, 500 → p95 close to 500
        assert by_model["llama3:8b"]["p95_latency_ms"] > 400
        # gpt-oss:120b latencies: 800, 1200 → p95 close to 1200
        assert by_model["gpt-oss:120b"]["p95_latency_ms"] > 1000

    @pytest.mark.asyncio
    async def test_p2p_fields_always_zero_until_routing_ships(self, seeded_db):
        """Forward compatibility: p2p_* fields are always 0 for now."""
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        for entry in payload["entries"]:
            assert entry["p2p_served_requests"] == 0
            assert entry["p2p_served_tokens"] == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_day_returns_empty_entries(self, tmp_path):
        data_dir = tmp_path / ".fleet-manager"
        data_dir.mkdir()
        db_path = data_dir / "latency.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                """
                CREATE TABLE latency_observations (
                    id INTEGER PRIMARY KEY,
                    node_id TEXT,
                    model_name TEXT,
                    latency_ms REAL,
                    tokens_generated INTEGER,
                    timestamp REAL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER
                )
                """
            )
            await db.commit()

        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=str(data_dir),
        )
        assert payload["entries"] == []
        assert payload["node_id"] == "test-uuid"
        assert payload["agent_version"] == "0.0.0-test"

    def test_percentile_single_sample(self):
        assert _percentile([500.0], 95) == 500.0

    def test_percentile_empty(self):
        assert _percentile([], 95) is None

    def test_percentile_ordering(self):
        # 10 values, p95 should be near the top
        vals = [float(i * 10) for i in range(1, 11)]  # 10, 20, ..., 100
        result = _percentile(vals, 95)
        assert 90 <= result <= 100

    def test_yesterday_bounds_returns_24h_window(self):
        day, start, end = _yesterday_utc_bounds()
        assert end - start == 86400  # exactly 24 hours
        # day string is a valid ISO date
        from datetime import date
        date.fromisoformat(day)


# ---------------------------------------------------------------------------
# Error categorization (new in extension #3)
# ---------------------------------------------------------------------------


class TestErrorCategorization:
    def test_none_returns_unknown(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        assert _categorize_error(None) == "unknown"
        assert _categorize_error("") == "unknown"

    def test_404_is_model_not_found(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        msg = "Client error '404 Not Found' for url '...'"
        assert _categorize_error(msg) == "model_not_found"

    def test_400_bare_is_bad_request(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        msg = "Client error '400 Bad Request' for url '...'"
        assert _categorize_error(msg) == "bad_request"

    def test_context_too_long_pattern(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        assert _categorize_error("context too long for model") == "context_too_long"
        assert _categorize_error("400 Bad Request: num_ctx exceeds limit") == "context_too_long"

    def test_vram_patterns(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        assert _categorize_error("CUDA out of memory") == "vram_exceeded"
        assert _categorize_error("VRAM allocation failed") == "vram_exceeded"

    def test_timeout(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        assert _categorize_error("Request timed out after 30s") == "timeout"

    def test_permission_error(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        msg = "PermissionError: [Errno 1] Operation not permitted"
        assert _categorize_error(msg) == "permission_error"

    def test_client_disconnect(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        assert _categorize_error("GeneratorExit: client disconnected") == "client_disconnected"

    def test_uncategorized_falls_to_other(self):
        from fleet_manager.node.daily_rollup import _categorize_error
        assert _categorize_error("Strange new error never seen before") == "other"


# ---------------------------------------------------------------------------
# success_count / error_count / error_breakdown integration
# ---------------------------------------------------------------------------


class TestSuccessErrorAggregation:
    @pytest.mark.asyncio
    async def test_success_and_error_counts_populated(self, tmp_path):
        """Seed both latency_observations AND request_traces (same DB file)."""
        data_dir = tmp_path / ".fleet-manager"
        data_dir.mkdir()
        _, start_ts, _ = _yesterday_utc_bounds()

        # TraceStore and LatencyStore share the same latency.db file
        async with aiosqlite.connect(str(data_dir / "latency.db")) as db:
            await db.execute(
                """CREATE TABLE latency_observations (
                    id INTEGER PRIMARY KEY, node_id TEXT, model_name TEXT,
                    latency_ms REAL, tokens_generated INTEGER, timestamp REAL,
                    prompt_tokens INTEGER, completion_tokens INTEGER)"""
            )
            await db.execute(
                "INSERT INTO latency_observations VALUES "
                "(1, 'n1', 'llama3:8b', 250.0, 100, ?, 50, 100)",
                (start_ts + 3600,),
            )
            await db.execute(
                """CREATE TABLE request_traces (
                    id INTEGER PRIMARY KEY, request_id TEXT, model TEXT,
                    original_model TEXT, node_id TEXT, score REAL,
                    scores_breakdown TEXT, status TEXT, latency_ms REAL,
                    time_to_first_token_ms REAL, prompt_tokens INTEGER,
                    completion_tokens INTEGER, retry_count INTEGER,
                    fallback_used INTEGER, excluded_nodes TEXT, client_ip TEXT,
                    original_format TEXT, error_message TEXT, timestamp REAL,
                    tags TEXT)"""
            )
            # 3 completed
            for i in range(3):
                await db.execute(
                    "INSERT INTO request_traces (model, status, timestamp) "
                    "VALUES ('llama3:8b', 'completed', ?)",
                    (start_ts + 100 + i,),
                )
            # 1 failed with 404
            await db.execute(
                "INSERT INTO request_traces (model, status, error_message, timestamp) "
                "VALUES ('llama3:8b', 'failed', ?, ?)",
                ("Client error '404 Not Found' for url '...'", start_ts + 200),
            )
            # 1 failed with VRAM error
            await db.execute(
                "INSERT INTO request_traces (model, status, error_message, timestamp) "
                "VALUES ('llama3:8b', 'failed', ?, ?)",
                ("CUDA out of memory", start_ts + 300),
            )
            await db.commit()

        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=str(data_dir),
        )
        assert len(payload["entries"]) == 1
        entry = payload["entries"][0]
        assert entry["success_count"] == 3
        assert entry["error_count"] == 2
        assert "error_breakdown" in entry
        assert entry["error_breakdown"].get("model_not_found") == 1
        assert entry["error_breakdown"].get("vram_exceeded") == 1

    @pytest.mark.asyncio
    async def test_no_errors_omits_error_breakdown(self, seeded_db):
        """When there are no errors, error_breakdown is absent entirely."""
        payload = await build_daily_rollup(
            node_uuid="test-uuid",
            agent_version="0.0.0-test",
            data_dir=seeded_db,
        )
        for entry in payload["entries"]:
            assert "error_breakdown" not in entry
            # success_count + error_count still present
            assert "success_count" in entry
            assert "error_count" in entry
