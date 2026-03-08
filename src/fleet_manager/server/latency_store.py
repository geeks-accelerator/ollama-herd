"""SQLite-backed storage for per-node, per-model latency observations."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


class LatencyStore:
    def __init__(self, data_dir: str = "~/.fleet-manager"):
        self._db_path = Path(data_dir).expanduser() / "latency.db"
        self._db: aiosqlite.Connection | None = None
        # Synchronous cache for scorer lookups
        self._percentile_cache: dict[str, float] = {}

    async def initialize(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS latency_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                tokens_generated INTEGER,
                timestamp REAL NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_latency_node_model
            ON latency_observations(node_id, model_name)
        """)
        # Migration: add token tracking columns
        for col in ("prompt_tokens INTEGER", "completion_tokens INTEGER"):
            try:
                await self._db.execute(f"ALTER TABLE latency_observations ADD COLUMN {col}")
            except Exception as e:
                if "duplicate column" in str(e).lower():
                    pass  # Column already exists — expected
                else:
                    logger.warning(f"Migration error adding column {col}: {e}")
        # Indexes for time-range dashboard queries
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_latency_timestamp
            ON latency_observations(timestamp)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_latency_model_timestamp
            ON latency_observations(model_name, timestamp)
        """)
        await self._db.commit()
        # Pre-populate cache from existing data
        await self._refresh_cache()
        logger.info(f"Latency store initialized at {self._db_path}")

    async def record(
        self,
        node_id: str,
        model_name: str,
        latency_ms: float,
        tokens: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ):
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO latency_observations "
            "(node_id, model_name, latency_ms, tokens_generated, "
            "prompt_tokens, completion_tokens, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                model_name,
                latency_ms,
                tokens,
                prompt_tokens,
                completion_tokens,
                time.time(),
            ),
        )
        await self._db.commit()
        # Update cache for this pair
        p75 = await self.get_percentile(node_id, model_name, 75)
        if p75 is not None:
            self._percentile_cache[f"{node_id}:{model_name}"] = p75

    async def get_percentile(
        self, node_id: str, model_name: str, percentile: int = 75
    ) -> float | None:
        if not self._db:
            return None
        cursor = await self._db.execute(
            "SELECT latency_ms FROM latency_observations "
            "WHERE node_id = ? AND model_name = ? "
            "ORDER BY latency_ms",
            (node_id, model_name),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        values = [r[0] for r in rows]
        idx = int(len(values) * percentile / 100)
        return values[min(idx, len(values) - 1)]

    def get_cached_percentile(self, node_id: str, model_name: str) -> float | None:
        """Synchronous lookup of cached p75 latency for scorer use."""
        return self._percentile_cache.get(f"{node_id}:{model_name}")

    # -- Dashboard query methods --

    async def get_hourly_trends(self, hours: int = 72) -> list[dict]:
        """Aggregate request count, avg latency, and token sums per hour."""
        if not self._db:
            return []
        cutoff = time.time() - (hours * 3600)
        cursor = await self._db.execute(
            """
            SELECT
                CAST(timestamp / 3600 AS INTEGER) * 3600 AS hour_bucket,
                COUNT(*) AS request_count,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens
            FROM latency_observations
            WHERE timestamp >= ?
            GROUP BY hour_bucket
            ORDER BY hour_bucket
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "hour_bucket": row[0],
                "request_count": row[1],
                "avg_latency_ms": round(row[2], 1),
                "total_prompt_tokens": row[3],
                "total_completion_tokens": row[4],
            }
            for row in rows
        ]

    async def get_model_daily_stats(self, days: int = 7) -> list[dict]:
        """Per-model, per-day aggregated stats."""
        if not self._db:
            return []
        cutoff = time.time() - (days * 86400)
        cursor = await self._db.execute(
            """
            SELECT
                model_name,
                CAST(timestamp / 86400 AS INTEGER) * 86400 AS day_bucket,
                COUNT(*) AS request_count,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens
            FROM latency_observations
            WHERE timestamp >= ?
            GROUP BY model_name, day_bucket
            ORDER BY model_name, day_bucket
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "model_name": row[0],
                "day_bucket": row[1],
                "request_count": row[2],
                "avg_latency_ms": round(row[3], 1),
                "total_prompt_tokens": row[4],
                "total_completion_tokens": row[5],
            }
            for row in rows
        ]

    async def get_model_summary(self) -> list[dict]:
        """All-time per-model aggregate stats."""
        if not self._db:
            return []
        cursor = await self._db.execute(
            """
            SELECT
                model_name,
                COUNT(*) AS total_requests,
                AVG(latency_ms) AS avg_latency_ms,
                MIN(latency_ms) AS min_latency_ms,
                MAX(latency_ms) AS max_latency_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens,
                MIN(timestamp) AS first_seen,
                MAX(timestamp) AS last_seen
            FROM latency_observations
            GROUP BY model_name
            ORDER BY total_requests DESC
            """
        )
        rows = await cursor.fetchall()
        return [
            {
                "model_name": row[0],
                "total_requests": row[1],
                "avg_latency_ms": round(row[2], 1),
                "min_latency_ms": round(row[3], 1),
                "max_latency_ms": round(row[4], 1),
                "total_prompt_tokens": row[5],
                "total_completion_tokens": row[6],
                "first_seen": row[7],
                "last_seen": row[8],
            }
            for row in rows
        ]

    async def get_node_model_daily_stats(self, days: int = 7) -> list[dict]:
        """Per-node, per-model, per-day aggregated stats."""
        if not self._db:
            return []
        cutoff = time.time() - (days * 86400)
        cursor = await self._db.execute(
            """
            SELECT
                node_id,
                model_name,
                CAST(timestamp / 86400 AS INTEGER) * 86400 AS day_bucket,
                COUNT(*) AS request_count,
                AVG(latency_ms) AS avg_latency_ms,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens
            FROM latency_observations
            WHERE timestamp >= ?
            GROUP BY node_id, model_name, day_bucket
            ORDER BY node_id, model_name, day_bucket
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "node_id": row[0],
                "model_name": row[1],
                "day_bucket": row[2],
                "request_count": row[3],
                "avg_latency_ms": round(row[4], 1),
                "total_prompt_tokens": row[5],
                "total_completion_tokens": row[6],
            }
            for row in rows
        ]

    async def _refresh_cache(self):
        """Pre-populate the percentile cache from all existing data."""
        if not self._db:
            return
        cursor = await self._db.execute(
            "SELECT DISTINCT node_id, model_name FROM latency_observations"
        )
        pairs = await cursor.fetchall()
        for node_id, model_name in pairs:
            p75 = await self.get_percentile(node_id, model_name, 75)
            if p75 is not None:
                self._percentile_cache[f"{node_id}:{model_name}"] = p75
        logger.info(f"Loaded p75 latency cache for {len(self._percentile_cache)} node:model pairs")

    async def close(self):
        if self._db:
            await self._db.close()
            logger.debug("Latency store closed")
