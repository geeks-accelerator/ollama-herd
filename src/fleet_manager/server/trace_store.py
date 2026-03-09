"""SQLite-backed per-request trace log for routing decisions and request outcomes."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


class TraceStore:
    """Records and queries per-request trace data in the same SQLite DB as LatencyStore."""

    def __init__(self, data_dir: str = "~/.fleet-manager"):
        self._db_path = Path(data_dir).expanduser() / "latency.db"
        self._db: aiosqlite.Connection | None = None

    async def initialize(self):
        """Create connection and request_traces table if it doesn't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        # Enable WAL mode for concurrent readers/writers
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Wait up to 5s for lock instead of failing immediately
        await self._db.execute("PRAGMA busy_timeout=5000")
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

        await self._db.commit()
        logger.info(f"Trace store initialized at {self._db_path}")

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
    ):
        """Insert a single trace record."""
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO request_traces "
            "(request_id, model, original_model, node_id, score, scores_breakdown, "
            "status, latency_ms, time_to_first_token_ms, prompt_tokens, completion_tokens, "
            "retry_count, fallback_used, excluded_nodes, client_ip, original_format, "
            "error_message, tags, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
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
            ),
        )
        await self._db.commit()

    async def get_recent_traces(self, limit: int = 100) -> list[dict]:
        """Return the most recent traces, newest first."""
        if not self._db:
            return []
        cursor = await self._db.execute(
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
        cursor = await self._db.execute(
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
        cursor = await self._db.execute(
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

    async def get_node_summary(self) -> list[dict]:
        """Per-node all-time aggregate stats."""
        if not self._db:
            return []
        cursor = await self._db.execute(
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
        cursor = await self._db.execute(
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

    async def get_usage_by_tag(self, days: int = 7) -> list[dict]:
        """Per-tag aggregated stats using SQLite json_each() to explode tags."""
        if not self._db:
            return []
        cutoff = time.time() - (days * 86400)
        cursor = await self._db.execute(
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
            WHERE timestamp >= ? AND tags IS NOT NULL
            GROUP BY j.value
            ORDER BY request_count DESC
            """,
            (cutoff,),
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

    async def get_tag_daily_stats(self, days: int = 7) -> list[dict]:
        """Per-tag, per-day breakdown for charting."""
        if not self._db:
            return []
        cutoff = time.time() - (days * 86400)
        cursor = await self._db.execute(
            """
            SELECT
                j.value AS tag,
                CAST(timestamp / 86400 AS INTEGER) * 86400 AS day_bucket,
                COUNT(*) AS request_count,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)) AS total_tokens
            FROM request_traces, json_each(request_traces.tags) AS j
            WHERE timestamp >= ? AND tags IS NOT NULL
            GROUP BY j.value, day_bucket
            ORDER BY day_bucket ASC, tag
            """,
            (cutoff,),
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
        cursor = await self._db.execute(
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
        cursor = await self._db.execute(
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

    async def get_error_rates_24h(self, lookback_s: int = 86400) -> list[dict]:
        """Per-node error rates for the given window (default 24h)."""
        if not self._db:
            return []
        cutoff = time.time() - lookback_s
        cursor = await self._db.execute(
            """
            SELECT
                node_id,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
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
        cursor = await self._db.execute(
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
        cursor = await self._db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
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

    async def get_benchmark_runs(self, limit: int = 50) -> list[dict]:
        """Return benchmark runs, newest first."""
        if not self._db:
            return []
        cursor = await self._db.execute(
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
        cursor = await self._db.execute(
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
        if self._db:
            await self._db.close()
            logger.debug("Trace store closed")
