"""SQLite-backed per-request trace log for routing decisions and request outcomes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Retry policy for "database is locked" — three attempts with exponential
# backoff covers the typical case (a long-running query holding the write
# lock during WAL checkpoint) without blocking the caller for more than ~3s.
# Past failures we observed (2026-05-10 incident): unbounded WAL growth during
# a sustained read held off writes until busy_timeout expired.  Combined with
# the now-30s busy_timeout (PRAGMA), this gives writes ~90s of cumulative
# patience before we declare the trace lost.
_RETRY_BACKOFFS_S = (0.2, 0.8, 2.0)


class TraceStore:
    """Records and queries per-request trace data in the same SQLite DB as LatencyStore."""

    def __init__(self, data_dir: str = "~/.fleet-manager"):
        self._db_path = Path(data_dir).expanduser() / "latency.db"
        # Writer connection — used by record_trace, save_benchmark_run,
        # save_briefing, and the periodic wal_checkpoint(PASSIVE) task.
        self._db: aiosqlite.Connection | None = None
        # Dedicated read connection — used by every analytics/dashboard
        # query method.  Splitting reads onto a separate aiosqlite connection
        # serves two purposes (see docs/plans/trace-store-read-connection-and-checkpoint.md):
        # 1) aiosqlite serializes operations per-connection through a single
        #    background thread; a slow read on the shared connection blocks
        #    queued writes for the read's duration.  Two connections = two
        #    threads, no per-connection serialization between read and write.
        # 2) Read snapshots pin the WAL checkpoint barrier.  When reads live
        #    on a separate connection, the writer connection's view of the
        #    WAL can advance and checkpoints fire between read snapshots.
        # Configured with PRAGMA query_only=1 so an accidental INSERT through
        # this connection errors out immediately instead of silently working
        # against the writer's expectations.
        self._read_db: aiosqlite.Connection | None = None
        # Rolling write-failure timestamps for the trace_store_write_failures
        # health check.  Bounded deque keeps memory flat; the health engine
        # filters by age.
        self._write_failure_times: deque[float] = deque(maxlen=1000)

    async def initialize(self):
        """Create connection and request_traces table if it doesn't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        # Enable WAL mode for concurrent readers/writers
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Wait up to 30s for lock instead of failing immediately.  The previous
        # 5s threshold was hit during a sustained-read scenario on 2026-05-10
        # that produced ~40K failed background trace-record tasks over ~4 days
        # (see docs/observations.md).  30s tolerates a checkpoint stall without
        # losing the trace; combined with retry-on-locked below this gives us
        # ~90s of cumulative patience before declaring the trace lost.
        await self._db.execute("PRAGMA busy_timeout=30000")
        # Bound WAL growth so a long-running reader can't let the WAL grow
        # unboundedly and starve writers — auto-checkpoint after this many
        # pages.  Default is 1000; 100 is more aggressive but keeps writer
        # latency predictable.
        await self._db.execute("PRAGMA wal_autocheckpoint=100")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS request_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                model TEXT NOT NULL,
                original_model TEXT NOT NULL,
                node_id TEXT NOT NULL,
                score REAL,
                scores_breakdown TEXT,
                status TEXT NOT NULL,
                latency_ms REAL,
                time_to_first_token_ms REAL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                retry_count INTEGER DEFAULT 0,
                fallback_used INTEGER DEFAULT 0,
                excluded_nodes TEXT,
                client_ip TEXT,
                original_format TEXT,
                error_message TEXT,
                timestamp REAL NOT NULL
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_request_id ON request_traces(request_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON request_traces(timestamp)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_model_timestamp "
            "ON request_traces(model, timestamp)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_node_model ON request_traces(node_id, model)"
        )
        # Schema migration: add tags column if it doesn't exist
        try:
            await self._db.execute("ALTER TABLE request_traces ADD COLUMN tags TEXT")
            logger.info("Added 'tags' column to request_traces")
        except Exception:
            pass  # Column already exists
        # Schema migration: finish_reason separates "the model chose to stop"
        # from "it hit the token budget" from "a stop token misfired". Without
        # it, a turn that ends mid-task is only diagnosable by eyeballing
        # completion_tokens — see docs/plans/codex-code-mode-escalation.md.
        try:
            await self._db.execute(
                "ALTER TABLE request_traces ADD COLUMN finish_reason TEXT"
            )
            logger.info("Added 'finish_reason' column to request_traces")
        except Exception:
            pass  # Column already exists
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_traces_tags ON request_traces(tags)")

        # Benchmark runs table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS benchmark_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                timestamp REAL NOT NULL,
                duration_s REAL NOT NULL,
                total_requests INTEGER NOT NULL,
                total_failures INTEGER NOT NULL,
                total_prompt_tokens INTEGER NOT NULL,
                total_completion_tokens INTEGER NOT NULL,
                requests_per_sec REAL,
                tokens_per_sec REAL,
                latency_p50_ms REAL,
                latency_p95_ms REAL,
                latency_p99_ms REAL,
                ttft_p50_ms REAL,
                ttft_p95_ms REAL,
                ttft_p99_ms REAL,
                fleet_snapshot TEXT,
                per_model_results TEXT,
                per_node_results TEXT,
                peak_utilization TEXT
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_benchmark_runs_timestamp ON benchmark_runs(timestamp)"
        )

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS fleet_briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                briefing TEXT NOT NULL,
                model TEXT,
                fleet_data TEXT
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fleet_briefings_ts ON fleet_briefings(timestamp)"
        )

        await self._db.commit()

        # Open the dedicated read connection.  Must come AFTER the writer's
        # commit so the read connection's first snapshot sees the schema we
        # just created (otherwise PRAGMA query_only=1 below would prevent
        # this connection from doing its own CREATE TABLE IF NOT EXISTS
        # passes — and we don't want it to).
        try:
            self._read_db = await aiosqlite.connect(str(self._db_path))
            await self._read_db.execute("PRAGMA journal_mode=WAL")
            await self._read_db.execute("PRAGMA busy_timeout=30000")
            # Defense-in-depth: any accidental write through this connection
            # raises ``OperationalError: attempt to write a readonly database``
            # rather than silently competing with the writer.
            await self._read_db.execute("PRAGMA query_only=1")
        except Exception:
            # If the read connection fails to open, close the writer to
            # avoid leaking it and re-raise — the caller's __aenter__ will
            # propagate the error and we won't run in a half-initialized
            # state.
            await self._db.close()
            self._db = None
            raise
        logger.info(f"Trace store initialized at {self._db_path}")

    async def checkpoint_passive(self) -> tuple[int, int, int] | None:
        """Run ``PRAGMA wal_checkpoint(PASSIVE)`` on the writer connection.

        Returns ``(busy, log_pages, checkpointed_pages)`` per SQLite's
        documented return shape, or ``None`` if the connection isn't open.

        Designed to be called on a fixed cadence (every ~10s) from a
        background task in ``server/app.py``'s lifespan.  ``PASSIVE`` is
        non-blocking — it advances whatever WAL pages it can past the
        current set of reader snapshots and returns immediately without
        waiting for readers to finish.  Safe to run frequently.

        Why this matters even with ``wal_autocheckpoint=100`` already set:
        autocheckpoint is tied to write *volume* (fires after N WAL page
        writes); under bursty traffic the WAL can sit at 99 pages for an
        hour while readers accumulate snapshots, and by the time the 100th
        page write triggers autocheckpoint, those snapshots have pinned
        the checkpoint barrier so far back that very little can advance.
        Tying checkpoints to *wall-clock* via this method makes them fire
        in the gaps between reader snapshots rather than only when a write
        happens to land on the threshold.
        """
        if self._db is None:
            return None
        try:
            cursor = await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            return (int(row[0]), int(row[1]), int(row[2]))
        except Exception as exc:  # noqa: BLE001 — never crash a background task
            logger.debug(f"trace_store checkpoint_passive failed: {exc}")
            return None

    async def record_trace(
        self,
        request_id: str,
        model: str,
        original_model: str,
        node_id: str,
        score: float | None = None,
        scores_breakdown: dict | None = None,
        status: str = "completed",
        latency_ms: float | None = None,
        time_to_first_token_ms: float | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        retry_count: int = 0,
        fallback_used: bool = False,
        excluded_nodes: list[str] | None = None,
        client_ip: str = "",
        original_format: str = "",
        error_message: str | None = None,
        tags: list[str] | None = None,
        finish_reason: str | None = None,
    ):
        """Insert a single trace record.

        Retries up to three times on ``database is locked`` errors before
        giving up — see ``_RETRY_BACKOFFS_S`` for the backoff schedule and
        the 2026-05-10 observation entry for the failure mode this guards.
        Failures (after all retries) are counted in ``_write_failure_times``
        so the ``trace_store_write_failures`` health check can surface them
        without operators having to grep the log.
        """
        if not self._db:
            return
        params = (
            request_id,
            model,
            original_model,
            node_id,
            score,
            json.dumps(scores_breakdown) if scores_breakdown else None,
            status,
            latency_ms,
            time_to_first_token_ms,
            prompt_tokens,
            completion_tokens,
            retry_count,
            int(fallback_used),
            json.dumps(excluded_nodes) if excluded_nodes else None,
            client_ip,
            original_format,
            error_message,
            json.dumps(tags) if tags else None,
            time.time(),
            finish_reason,
        )
        sql = (
            "INSERT INTO request_traces "
            "(request_id, model, original_model, node_id, score, scores_breakdown, "
            "status, latency_ms, time_to_first_token_ms, prompt_tokens, completion_tokens, "
            "retry_count, fallback_used, excluded_nodes, client_ip, original_format, "
            "error_message, tags, timestamp, finish_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        # Retry-on-locked: the busy_timeout PRAGMA above absorbs short
        # contention, but a stuck-reader scenario can still expire it.
        # We try up to ``len(_RETRY_BACKOFFS_S) + 1`` times total before
        # incrementing the failure counter and re-raising — the caller's
        # _create_logged_task done-callback will then log it once.
        for backoff in (*_RETRY_BACKOFFS_S, None):
            try:
                await self._db.execute(sql, params)
                await self._db.commit()
                return
            except aiosqlite.OperationalError as exc:
                if "locked" not in str(exc).lower() or backoff is None:
                    self._write_failure_times.append(time.time())
                    raise
                await asyncio.sleep(backoff)

    def get_write_failure_count(self, window_s: float = 300.0) -> int:
        """Return number of unrecoverable write failures in the last ``window_s`` seconds.

        Used by the ``trace_store_write_failures`` health check to flag
        ongoing DB-lock incidents without operators having to scan logs.
        """
        cutoff = time.time() - window_s
        # Right-side prune so subsequent calls are cheap — deque trim from
        # the left while head is older than cutoff.
        while self._write_failure_times and self._write_failure_times[0] < cutoff:
            self._write_failure_times.popleft()
        return len(self._write_failure_times)

    async def get_recent_traces(self, limit: int = 100) -> list[dict]:
        """Return the most recent traces, newest first."""
        if not self._db:
            return []
        cursor = await self._read_db.execute(
            "SELECT request_id, model, original_model, node_id, score, "
            "scores_breakdown, status, latency_ms, time_to_first_token_ms, "
            "prompt_tokens, completion_tokens, retry_count, fallback_used, "
            "excluded_nodes, client_ip, original_format, error_message, timestamp, tags "
            "FROM request_traces ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def get_trace_by_request_id(self, request_id: str) -> list[dict]:
        """Look up all trace entries for a given request (may have retries)."""
        if not self._db:
            return []
        cursor = await self._read_db.execute(
            "SELECT request_id, model, original_model, node_id, score, "
            "scores_breakdown, status, latency_ms, time_to_first_token_ms, "
            "prompt_tokens, completion_tokens, retry_count, fallback_used, "
            "excluded_nodes, client_ip, original_format, error_message, timestamp, tags "
            "FROM request_traces WHERE request_id = ? ORDER BY timestamp",
            (request_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # -- Usage stats queries --

    async def get_usage_by_node_model_day(self, days: int = 7) -> list[dict]:
        """Per-node, per-model, per-day aggregated stats from request_traces."""
        if not self._db:
            return []
        cutoff = time.time() - (days * 86400)
        cursor = await self._read_db.execute(
            """
            SELECT
                node_id,
                model,
                CAST(timestamp / 86400 AS INTEGER) * 86400 AS day_bucket,
                COUNT(*) AS request_count,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                AVG(latency_ms) AS avg_latency_ms,
                AVG(time_to_first_token_ms) AS avg_ttft_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens,
                SUM(retry_count) AS total_retries,
                SUM(fallback_used) AS total_fallbacks
            FROM request_traces
            WHERE timestamp >= ?
            GROUP BY node_id, model, day_bucket
            ORDER BY day_bucket DESC, node_id, model
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "node_id": row[0],
                "model": row[1],
                "day_bucket": row[2],
                "request_count": row[3],
                "completed_count": row[4],
                "failed_count": row[5],
                "avg_latency_ms": round(row[6], 1) if row[6] else 0,
                "avg_ttft_ms": round(row[7], 1) if row[7] else None,
                "total_prompt_tokens": row[8],
                "total_completion_tokens": row[9],
                "total_retries": row[10],
                "total_fallbacks": row[11],
            }
            for row in rows
        ]

    async def get_last_used_by_node_model(self) -> list[dict]:
        """Per-node, per-model last-used timestamp and total request count."""
        if not self._db:
            return []
        cursor = await self._read_db.execute(
            """
            SELECT
                node_id,
                model,
                MAX(timestamp) AS last_used,
                COUNT(*) AS total_requests
            FROM request_traces
            GROUP BY node_id, model
            ORDER BY node_id, model
            """
        )
        rows = await cursor.fetchall()
        return [
            {
                "node_id": row[0],
                "model": row[1],
                "last_used": row[2],
                "total_requests": row[3],
            }
            for row in rows
        ]

    async def get_node_summary(self) -> list[dict]:
        """Per-node all-time aggregate stats."""
        if not self._db:
            return []
        cursor = await self._read_db.execute(
            """
            SELECT
                node_id,
                COUNT(*) AS total_requests,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens,
                SUM(retry_count) AS total_retries,
                MIN(timestamp) AS first_seen,
                MAX(timestamp) AS last_seen
            FROM request_traces
            GROUP BY node_id
            ORDER BY total_requests DESC
            """
        )
        rows = await cursor.fetchall()
        return [
            {
                "node_id": row[0],
                "total_requests": row[1],
                "completed_count": row[2],
                "failed_count": row[3],
                "avg_latency_ms": round(row[4], 1) if row[4] else 0,
                "total_prompt_tokens": row[5],
                "total_completion_tokens": row[6],
                "total_retries": row[7],
                "first_seen": row[8],
                "last_seen": row[9],
            }
            for row in rows
        ]

    async def get_usage_overview(self) -> dict:
        """Global overview: total requests, tokens, errors, retries."""
        if not self._db:
            return {
                "total_requests": 0,
                "completed_count": 0,
                "failed_count": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "total_retries": 0,
                "total_fallbacks": 0,
            }
        cursor = await self._read_db.execute(
            """
            SELECT
                COUNT(*) AS total_requests,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens,
                SUM(retry_count) AS total_retries,
                SUM(fallback_used) AS total_fallbacks
            FROM request_traces
            """
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {
                "total_requests": 0,
                "completed_count": 0,
                "failed_count": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "total_retries": 0,
                "total_fallbacks": 0,
            }
        return {
            "total_requests": row[0],
            "completed_count": row[1],
            "failed_count": row[2],
            "total_prompt_tokens": row[3],
            "total_completion_tokens": row[4],
            "total_tokens": row[3] + row[4],
            "total_retries": row[5],
            "total_fallbacks": row[6],
        }

    # -- Tag analytics queries --

    async def get_usage_by_tag(
        self, days: int = 7, start_ts: float = 0, end_ts: float = 0,
    ) -> list[dict]:
        """Per-tag aggregated stats using SQLite json_each() to explode tags."""
        if not self._db:
            return []
        cutoff = start_ts if start_ts and end_ts else time.time() - days * 86400
        cursor = await self._read_db.execute(
            """
            SELECT
                j.value AS tag,
                COUNT(*) AS request_count,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                AVG(latency_ms) AS avg_latency_ms,
                AVG(time_to_first_token_ms) AS avg_ttft_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens
            FROM request_traces, json_each(request_traces.tags) AS j
            WHERE timestamp >= ? AND timestamp <= ? AND tags IS NOT NULL
            GROUP BY j.value
            ORDER BY request_count DESC
            """,
            (cutoff, end_ts if end_ts else time.time()),
        )
        rows = await cursor.fetchall()
        return [
            {
                "tag": row[0],
                "request_count": row[1],
                "completed_count": row[2],
                "failed_count": row[3],
                "avg_latency_ms": round(row[4], 1) if row[4] else 0,
                "avg_ttft_ms": round(row[5], 1) if row[5] else None,
                "total_prompt_tokens": row[6],
                "total_completion_tokens": row[7],
            }
            for row in rows
        ]

    async def get_tag_daily_stats(
        self, days: int = 7, start_ts: float = 0, end_ts: float = 0,
    ) -> list[dict]:
        """Per-tag, per-day breakdown for charting."""
        if not self._db:
            return []
        cutoff = start_ts if start_ts and end_ts else time.time() - days * 86400
        cursor = await self._read_db.execute(
            """
            SELECT
                j.value AS tag,
                CAST(timestamp / 86400 AS INTEGER) * 86400 AS day_bucket,
                COUNT(*) AS request_count,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)) AS total_tokens
            FROM request_traces, json_each(request_traces.tags) AS j
            WHERE timestamp >= ? AND timestamp <= ? AND tags IS NOT NULL
            GROUP BY j.value, day_bucket
            ORDER BY day_bucket ASC, tag
            """,
            (cutoff, end_ts if end_ts else time.time()),
        )
        rows = await cursor.fetchall()
        return [
            {
                "tag": row[0],
                "day_bucket": row[1],
                "request_count": row[2],
                "avg_latency_ms": round(row[3], 1) if row[3] else 0,
                "total_tokens": row[4],
            }
            for row in rows
        ]

    async def get_tag_summary(self) -> list[dict]:
        """All-time per-tag aggregates."""
        if not self._db:
            return []
        cursor = await self._read_db.execute(
            """
            SELECT
                j.value AS tag,
                COUNT(*) AS total_requests,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens,
                MIN(timestamp) AS first_seen,
                MAX(timestamp) AS last_seen
            FROM request_traces, json_each(request_traces.tags) AS j
            WHERE tags IS NOT NULL
            GROUP BY j.value
            ORDER BY total_requests DESC
            """
        )
        rows = await cursor.fetchall()
        return [
            {
                "tag": row[0],
                "total_requests": row[1],
                "completed_count": row[2],
                "failed_count": row[3],
                "avg_latency_ms": round(row[4], 1) if row[4] else 0,
                "total_prompt_tokens": row[5],
                "total_completion_tokens": row[6],
                "first_seen": row[7],
                "last_seen": row[8],
            }
            for row in rows
        ]

    # -- Health analysis queries --

    async def get_cold_loads_24h(
        self, ttft_threshold_ms: float = 40_000, lookback_s: int = 86400
    ) -> dict:
        """Count cold model loads (TTFT > threshold) by node in the given window."""
        if not self._db:
            return {"total_count": 0, "by_node": {}}
        cutoff = time.time() - lookback_s
        cursor = await self._read_db.execute(
            """
            SELECT node_id, COUNT(*) AS cold_count
            FROM request_traces
            WHERE timestamp >= ?
              AND time_to_first_token_ms > ?
              AND status = 'completed'
            GROUP BY node_id
            """,
            (cutoff, ttft_threshold_ms),
        )
        rows = await cursor.fetchall()
        by_node = {row[0]: row[1] for row in rows}
        total = sum(by_node.values())
        return {"total_count": total, "by_node": by_node}

    async def get_embed_error_stats(self, lookback_s: int = 3600) -> dict:
        """Embed-specific failure stats for the given window (default 1h).

        Filters on models whose name contains 'embed' (e.g. nomic-embed-text).
        Does NOT filter on tags — client pipelines may tag LLM requests with
        'embed' too, which would produce false positives.
        Returns total requests, failed count, and per-model breakdown so the
        health check can surface ReadTimeout storms that bypass LLM error-rate
        checks (embed failures were previously untraced — see 2026-06-01
        observation).
        """
        if not self._read_db:
            return {"total": 0, "failed": 0, "by_model": {}}
        cutoff = time.time() - lookback_s
        cursor = await self._read_db.execute(
            """
            SELECT
                model,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM request_traces
            WHERE timestamp >= ?
              AND model LIKE '%embed%'
            GROUP BY model
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        by_model = {}
        total = 0
        failed = 0
        for row in rows:
            by_model[row[0]] = {"total": row[1], "failed": row[2]}
            total += row[1]
            failed += row[2]
        return {"total": total, "failed": failed, "by_model": by_model}

    async def get_error_rates_24h(self, lookback_s: int = 86400) -> list[dict]:
        """Per-node error rates for the given window (default 24h)."""
        if not self._db:
            return []
        cutoff = time.time() - lookback_s
        cursor = await self._read_db.execute(
            """
            SELECT
                node_id,
                COUNT(*) AS total,
                SUM(CASE WHEN status != 'completed' AND status != 'retried'
                    THEN 1 ELSE 0 END) AS failed
            FROM request_traces
            WHERE timestamp >= ?
            GROUP BY node_id
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "node_id": row[0],
                "total": row[1],
                "failed": row[2],
                "error_rate_pct": round((row[2] / row[1]) * 100, 1) if row[1] > 0 else 0,
            }
            for row in rows
        ]

    async def get_retry_stats_24h(self) -> dict:
        """Fleet-wide retry stats for the last 24 hours."""
        if not self._db:
            return {"total_requests": 0, "total_retries": 0}
        cutoff = time.time() - 86400
        cursor = await self._read_db.execute(
            """
            SELECT COUNT(*) AS total, SUM(retry_count) AS retries
            FROM request_traces
            WHERE timestamp >= ?
            """,
            (cutoff,),
        )
        row = await cursor.fetchone()
        return {
            "total_requests": row[0] if row else 0,
            "total_retries": row[1] if row and row[1] else 0,
        }

    async def get_overall_stats_24h(self) -> dict:
        """Overall request stats for the last 24 hours: count, error rate, avg TTFT."""
        if not self._db:
            return {
                "total_requests": 0,
                "error_rate_pct": 0,
                "avg_ttft_ms": None,
                "total_retries": 0,
            }
        cutoff = time.time() - 86400
        cursor = await self._read_db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status != 'completed' AND status != 'retried'
                    THEN 1 ELSE 0 END) AS failed,
                AVG(time_to_first_token_ms) AS avg_ttft,
                SUM(retry_count) AS retries
            FROM request_traces
            WHERE timestamp >= ?
            """,
            (cutoff,),
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {
                "total_requests": 0,
                "error_rate_pct": 0,
                "avg_ttft_ms": None,
                "total_retries": 0,
            }
        return {
            "total_requests": row[0],
            "error_rate_pct": round((row[1] / row[0]) * 100, 1),
            "avg_ttft_ms": round(row[2], 1) if row[2] else None,
            "total_retries": row[3] or 0,
        }

    async def get_request_count_by_model(
        self, seconds: int = 120,
    ) -> dict[str, int]:
        """Return {model_name: request_count} over the last N seconds.

        Used by the context compactor's curator selector: a model with
        active traffic is a poor curator choice (we'd queue summary work
        behind real user requests), while an idle model — especially a
        pinned one — is ideal.  Short window (default 2 min) captures
        "currently in a conversation" rather than all-time popularity.
        """
        if not self._db:
            return {}
        cutoff = time.time() - seconds
        cursor = await self._read_db.execute(
            """
            SELECT model, COUNT(*) as n
            FROM request_traces
            WHERE timestamp >= ? AND model != ''
            GROUP BY model
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_recently_used_models(self, seconds: int = 3600) -> set[str]:
        """Return model names with at least 1 request in the last N seconds.

        Used to respect user intent: if a model hasn't been requested
        recently, don't auto-reload it just because it has historical
        priority.  The user may have intentionally unloaded it.
        """
        if not self._db:
            return set()
        cutoff = time.time() - seconds
        cursor = await self._read_db.execute(
            """
            SELECT DISTINCT model
            FROM request_traces
            WHERE timestamp >= ? AND model != ''
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def get_silent_fallback_stats(
        self, lookback_s: int = 86400
    ) -> list[dict]:
        """Detect silent model fallback — requested X but got Y.

        When VRAM fallback routes a request away from the requested
        model to a different one, the trace records both.  This catches
        prolonged degradation where requests appear successful but are
        being answered by the wrong (usually weaker) model.

        Returns counts grouped by (requested → actual) pairs, sorted
        by count desc.  Only includes pairs where requested != actual.
        """
        if not self._db:
            return []
        cutoff = time.time() - lookback_s
        cursor = await self._read_db.execute(
            """
            SELECT original_model, model, COUNT(*) as count,
                   MIN(timestamp) as first_ts, MAX(timestamp) as last_ts
            FROM request_traces
            WHERE timestamp >= ?
              AND original_model IS NOT NULL
              AND original_model != ''
              AND original_model != model
            GROUP BY original_model, model
            ORDER BY count DESC
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "requested": row[0],
                "actual": row[1],
                "count": row[2],
                "first_ts": row[3],
                "last_ts": row[4],
            }
            for row in rows
        ]

    async def get_model_priority_scores(self) -> list[dict]:
        """Compute model priority scores for startup preloading.

        Weights recent usage (24h) 3x higher than weekly average to
        catch workload shifts quickly.  Returns models sorted by score
        (highest priority first).

        Score = (requests_24h * 3) + (requests_7d_daily_avg * 1)
        """
        if not self._db:
            return []
        now = time.time()
        cutoff_24h = now - 86400
        cutoff_7d = now - 7 * 86400

        cursor = await self._read_db.execute(
            """
            SELECT
                model,
                SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS requests_24h,
                COUNT(*) AS requests_7d,
                MAX(timestamp) AS last_used
            FROM request_traces
            WHERE timestamp >= ?
            GROUP BY model
            ORDER BY
                (SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) * 3
                 + COUNT(*) / 7.0) DESC
            """,
            (cutoff_24h, cutoff_7d, cutoff_24h),
        )
        rows = await cursor.fetchall()
        return [
            {
                "model": row[0],
                "requests_24h": row[1],
                "requests_7d": row[2],
                "daily_avg_7d": round(row[2] / 7.0, 1),
                "priority_score": round(row[1] * 3 + row[2] / 7.0, 1),
                "last_used": row[3],
            }
            for row in rows
            if row[0]  # Skip empty model names
        ]

    async def get_stream_reliability_24h(self, lookback_s: int = 86400) -> dict:
        """Count client disconnects and incomplete streams in the given window."""
        if not self._db:
            return {
                "client_disconnected": 0,
                "incomplete": 0,
                "total_requests": 0,
                "by_model": {},
            }
        cutoff = time.time() - lookback_s
        cursor = await self._read_db.execute(
            """
            SELECT
                status,
                model,
                COUNT(*) AS cnt
            FROM request_traces
            WHERE timestamp >= ? AND status IN ('client_disconnected', 'incomplete')
            GROUP BY status, model
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        disconnected = 0
        incomplete = 0
        by_model: dict[str, dict] = {}
        for status, model, cnt in rows:
            if status == "client_disconnected":
                disconnected += cnt
            elif status == "incomplete":
                incomplete += cnt
            if model not in by_model:
                by_model[model] = {"client_disconnected": 0, "incomplete": 0}
            by_model[model][status] = cnt

        # Get total requests for rate calculation
        cursor2 = await self._read_db.execute(
            "SELECT COUNT(*) FROM request_traces WHERE timestamp >= ?",
            (cutoff,),
        )
        row = await cursor2.fetchone()
        total = row[0] if row else 0

        return {
            "client_disconnected": disconnected,
            "incomplete": incomplete,
            "total_requests": total,
            "by_model": by_model,
        }

    async def get_model_timeouts_24h(
        self, timeout_threshold_ms: float = 120_000, lookback_s: int = 86400
    ) -> dict:
        """Count model load timeouts (failed/retried requests with high latency) by node and model.

        Catches the pattern where a model keeps timing out because it's being
        evicted and can't reload fast enough — the smoking gun for model thrashing
        that cold-load detection misses (since those requests never complete).
        """
        if not self._db:
            return {"total_count": 0, "by_node": {}, "by_model": {}}
        cutoff = time.time() - lookback_s
        cursor = await self._read_db.execute(
            """
            SELECT node_id, model, COUNT(*) AS timeout_count
            FROM request_traces
            WHERE timestamp >= ?
              AND status IN ('retried', 'failed')
              AND latency_ms > ?
            GROUP BY node_id, model
            """,
            (cutoff, timeout_threshold_ms),
        )
        rows = await cursor.fetchall()
        by_node: dict[str, int] = {}
        by_model: dict[str, dict] = {}
        for node_id, model, count in rows:
            by_node[node_id] = by_node.get(node_id, 0) + count
            if model not in by_model:
                by_model[model] = {"count": 0, "nodes": []}
            by_model[model]["count"] += count
            by_model[model]["nodes"].append(node_id)
        total = sum(by_node.values())
        return {"total_count": total, "by_node": by_node, "by_model": by_model}

    # -- Benchmark runs --

    async def save_benchmark_run(self, data: dict):
        """Insert a benchmark run record."""
        if not self._db:
            return
        await self._db.execute(
            "INSERT OR REPLACE INTO benchmark_runs "
            "(run_id, timestamp, duration_s, total_requests, total_failures, "
            "total_prompt_tokens, total_completion_tokens, requests_per_sec, "
            "tokens_per_sec, latency_p50_ms, latency_p95_ms, latency_p99_ms, "
            "ttft_p50_ms, ttft_p95_ms, ttft_p99_ms, fleet_snapshot, "
            "per_model_results, per_node_results, peak_utilization) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["run_id"],
                data.get("timestamp", time.time()),
                data["duration_s"],
                data["total_requests"],
                data["total_failures"],
                data["total_prompt_tokens"],
                data["total_completion_tokens"],
                data.get("requests_per_sec"),
                data.get("tokens_per_sec"),
                data.get("latency_p50_ms"),
                data.get("latency_p95_ms"),
                data.get("latency_p99_ms"),
                data.get("ttft_p50_ms"),
                data.get("ttft_p95_ms"),
                data.get("ttft_p99_ms"),
                json.dumps(data.get("fleet_snapshot")) if data.get("fleet_snapshot") else None,
                json.dumps(data.get("per_model_results"))
                if data.get("per_model_results")
                else None,
                json.dumps(data.get("per_node_results")) if data.get("per_node_results") else None,
                json.dumps(data.get("peak_utilization")) if data.get("peak_utilization") else None,
            ),
        )
        await self._db.commit()

    # -- Context usage analysis --

    async def get_prompt_token_stats(self, days: int = 7) -> list[dict]:
        """Per-model token usage stats with percentiles for both prompt and total.

        Uses p99 of total tokens (prompt + completion) instead of raw MAX
        to avoid one outlier request skewing the recommendation.
        """
        if not self._db:
            return []
        cutoff = time.time() - days * 86400
        cutoff_24h = time.time() - 86400
        cursor = await self._read_db.execute(
            """
            WITH base AS (
                SELECT model, prompt_tokens, completion_tokens,
                       prompt_tokens + COALESCE(completion_tokens, 0) as total_tokens,
                       timestamp
                FROM request_traces
                WHERE timestamp >= ?
                  AND prompt_tokens > 0
                  AND status = 'completed'
            ),
            prompt_ranked AS (
                SELECT model, prompt_tokens,
                       PERCENT_RANK() OVER (
                           PARTITION BY model ORDER BY prompt_tokens
                       ) as prank
                FROM base
            ),
            total_ranked AS (
                SELECT model, total_tokens,
                       PERCENT_RANK() OVER (
                           PARTITION BY model ORDER BY total_tokens
                       ) as trank
                FROM base
            ),
            recent AS (
                SELECT model,
                       MAX(total_tokens) as max_total_24h,
                       COUNT(*) as count_24h
                FROM base
                WHERE timestamp >= ?
                GROUP BY model
            )
            SELECT
                pr.model,
                COUNT(*) as request_count,
                CAST(AVG(pr.prompt_tokens) AS INTEGER) as avg_prompt,
                MAX(CASE WHEN pr.prank <= 0.50 THEN pr.prompt_tokens END) as prompt_p50,
                MAX(CASE WHEN pr.prank <= 0.75 THEN pr.prompt_tokens END) as prompt_p75,
                MAX(CASE WHEN pr.prank <= 0.95 THEN pr.prompt_tokens END) as prompt_p95,
                MAX(CASE WHEN pr.prank <= 0.99 THEN pr.prompt_tokens END) as prompt_p99,
                MAX(pr.prompt_tokens) as max_prompt,
                (SELECT MAX(CASE WHEN trank <= 0.95 THEN total_tokens END)
                 FROM total_ranked WHERE total_ranked.model = pr.model) as total_p95,
                (SELECT MAX(CASE WHEN trank <= 0.99 THEN total_tokens END)
                 FROM total_ranked WHERE total_ranked.model = pr.model) as total_p99,
                (SELECT MAX(total_tokens)
                 FROM total_ranked WHERE total_ranked.model = pr.model) as max_total,
                r.max_total_24h,
                r.count_24h
            FROM prompt_ranked pr
            LEFT JOIN recent r ON r.model = pr.model
            GROUP BY pr.model
            ORDER BY COUNT(*) DESC
            """,
            (cutoff, cutoff_24h),
        )
        rows = await cursor.fetchall()
        return [
            {
                "model": row[0],
                "request_count": row[1],
                "avg_prompt": row[2],
                "prompt_p50": row[3] or 0,
                "prompt_p75": row[4] or 0,
                "prompt_p95": row[5] or 0,
                "prompt_p99": row[6] or 0,
                "max_prompt": row[7] or 0,
                "total_p95": row[8] or 0,
                "total_p99": row[9] or 0,
                "max_total": row[10] or 0,
                "max_total_24h": row[11] or 0,
                "count_24h": row[12] or 0,
            }
            for row in rows
        ]

    # -- Fleet briefing storage --

    async def save_briefing(
        self, briefing: str, model: str, fleet_data: str = ""
    ) -> None:
        """Persist a fleet intelligence briefing to SQLite."""
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO fleet_briefings (timestamp, briefing, model, fleet_data) "
            "VALUES (?, ?, ?, ?)",
            (time.time(), briefing, model, fleet_data),
        )
        await self._db.commit()

    async def get_briefings(self, limit: int = 20) -> list[dict]:
        """Return recent fleet briefings, newest first."""
        if not self._db:
            return []
        cursor = await self._read_db.execute(
            "SELECT timestamp, briefing, model, fleet_data "
            "FROM fleet_briefings ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "generated_at": row[0],
                "briefing": row[1],
                "model": row[2],
                "fleet_data": row[3],
            }
            for row in rows
        ]

    # -- Benchmark storage --

    async def get_benchmark_runs(self, limit: int = 50) -> list[dict]:
        """Return benchmark runs, newest first."""
        if not self._db:
            return []
        cursor = await self._read_db.execute(
            "SELECT run_id, timestamp, duration_s, total_requests, total_failures, "
            "total_prompt_tokens, total_completion_tokens, requests_per_sec, "
            "tokens_per_sec, latency_p50_ms, latency_p95_ms, latency_p99_ms, "
            "ttft_p50_ms, ttft_p95_ms, ttft_p99_ms, fleet_snapshot, "
            "per_model_results, per_node_results, peak_utilization "
            "FROM benchmark_runs ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._benchmark_row_to_dict(row) for row in rows]

    async def get_benchmark_run(self, run_id: str) -> dict | None:
        """Return a single benchmark run by run_id."""
        if not self._db:
            return None
        cursor = await self._read_db.execute(
            "SELECT run_id, timestamp, duration_s, total_requests, total_failures, "
            "total_prompt_tokens, total_completion_tokens, requests_per_sec, "
            "tokens_per_sec, latency_p50_ms, latency_p95_ms, latency_p99_ms, "
            "ttft_p50_ms, ttft_p95_ms, ttft_p99_ms, fleet_snapshot, "
            "per_model_results, per_node_results, peak_utilization "
            "FROM benchmark_runs WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._benchmark_row_to_dict(row)

    def _benchmark_row_to_dict(self, row) -> dict:
        """Convert a benchmark_runs row to dict with JSON parsing."""

        def _parse_json(val):
            if val is None:
                return None
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val

        return {
            "run_id": row[0],
            "timestamp": row[1],
            "duration_s": row[2],
            "total_requests": row[3],
            "total_failures": row[4],
            "total_prompt_tokens": row[5],
            "total_completion_tokens": row[6],
            "requests_per_sec": row[7],
            "tokens_per_sec": row[8],
            "latency_p50_ms": row[9],
            "latency_p95_ms": row[10],
            "latency_p99_ms": row[11],
            "ttft_p50_ms": row[12],
            "ttft_p95_ms": row[13],
            "ttft_p99_ms": row[14],
            "fleet_snapshot": _parse_json(row[15]),
            "per_model_results": _parse_json(row[16]),
            "per_node_results": _parse_json(row[17]),
            "peak_utilization": _parse_json(row[18]),
        }

    def _row_to_dict(self, row) -> dict:
        """Convert a SELECT row into a dict with JSON parsing."""
        breakdown = None
        if row[5]:
            try:
                breakdown = json.loads(row[5])
            except json.JSONDecodeError:
                logger.debug(f"Corrupt scores_breakdown JSON in trace {row[0]}")
                breakdown = row[5]
        excluded = None
        if row[13]:
            try:
                excluded = json.loads(row[13])
            except json.JSONDecodeError:
                logger.debug(f"Corrupt excluded_nodes JSON in trace {row[0]}")
                excluded = row[13]
        tags = None
        if len(row) > 18 and row[18]:
            try:
                tags = json.loads(row[18])
            except json.JSONDecodeError:
                logger.debug(f"Corrupt tags JSON in trace {row[0]}")
                tags = row[18]
        return {
            "request_id": row[0],
            "model": row[1],
            "original_model": row[2],
            "node_id": row[3],
            "score": row[4],
            "scores_breakdown": breakdown,
            "status": row[6],
            "latency_ms": row[7],
            "time_to_first_token_ms": row[8],
            "prompt_tokens": row[9],
            "completion_tokens": row[10],
            "retry_count": row[11],
            "fallback_used": bool(row[12]),
            "excluded_nodes": excluded,
            "client_ip": row[14],
            "original_format": row[15],
            "error_message": row[16],
            "timestamp": row[17],
            "tags": tags,
        }

    async def close(self):
        # Close the read connection first so any in-flight read finishes
        # against a still-open writer (otherwise readers can race the
        # writer's closure and emit "connection closed" tracebacks during
        # shutdown).
        if self._read_db:
            await self._read_db.close()
            self._read_db = None
        if self._db:
            await self._db.close()
            self._db = None
        logger.debug("Trace store closed")
