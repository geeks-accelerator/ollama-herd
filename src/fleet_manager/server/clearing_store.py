"""Persistent store of tool_use_ids whose tool_results have been cleared.

**What problem this solves**:

Our original Layer 1 mechanical clearing (``context_management.py``) was
stateless — on every request it re-computed "keep the last N
tool_results, clear the rest."  That meant on turn N a tool_result at
position 97 (of 100) was kept intact, but on turn N+1 a new tool_result
got appended, making position 97 now position 97 of 101 — outside the
"last 3" window — so it newly got cleared.  The byte-level placeholder
insertion at midpoint of the prefix invalidated MLX's prompt cache for
everything after that point, and every subsequent turn was a full cold
prefill.

Observed on 2026-04-24: 0% cache hit on every turn, 200–250 seconds
spent re-prefilling the same 127K-token prompt over and over.

**The fix**:

Make clearing *sticky*.  Once a specific ``tool_use_id`` is decided to
be "cleared," it stays cleared forever.  Store the set persistently
(SQLite, same pattern as ``SummaryCache``).  On each request:

1. Any tool_result whose ``tool_use_id`` is already in the store →
   clear (same bytes as last time → cache hits).
2. Any tool_result not in the store → check whether it should be added.
   If the prompt is still over the trigger AND this one is outside the
   "keep N most recent" window → add to store + clear.

Result: the cleared/intact boundary only ever *advances* — never
regresses — so the only cache invalidation happens at the moment new
IDs are added (the minority of turns), not on every single turn.

**Persistence rationale**: same SQLite pattern as SummaryCache.  IDs
are Anthropic-generated UUIDs (`toolu_<12-char-hex>`), stable across
any number of turns in the same conversation.  An ID seen once will
reappear in every subsequent request for the same session until that
session ends.  Storage is cheap (~20 bytes per ID), and a session of
2,000 tool calls is 40KB of state.  We LRU-evict stale entries so the
table doesn't grow forever.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


# Default: drop tool_use_ids we haven't seen in a week.  Claude Code
# sessions rarely span weeks; anything that old is almost certainly a
# stale entry from an abandoned session.
DEFAULT_PRUNE_DAYS = 7


class ClearingStore:
    """SQLite-backed persistent set of tool_use_ids to sticky-clear.

    Thread-safe via per-call connection (SQLite's default-serialized mode).
    Safe to share a single instance across concurrent requests.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=5.0)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS cleared_tool_uses (
                    tool_use_id TEXT PRIMARY KEY,
                    cleared_at REAL NOT NULL,
                    last_seen REAL NOT NULL
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_cleared_last_seen
                ON cleared_tool_uses(last_seen)
            """)

    def load_all(self) -> set[str]:
        """Return the full set of cleared tool_use_ids.  Fast — one query."""
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT tool_use_id FROM cleared_tool_uses",
                ).fetchall()
            return {row["tool_use_id"] for row in rows}
        except sqlite3.OperationalError as exc:
            logger.warning(f"ClearingStore.load_all failed: {exc} — returning empty")
            return set()

    def add(self, tool_use_ids: Iterable[str]) -> None:
        """Add tool_use_ids to the cleared set.  Idempotent — re-adds
        bump ``last_seen`` but don't disturb ``cleared_at``."""
        ids = [tid for tid in tool_use_ids if tid]
        if not ids:
            return
        now = time.time()
        try:
            with self._conn() as c:
                c.executemany(
                    "INSERT INTO cleared_tool_uses "
                    "(tool_use_id, cleared_at, last_seen) VALUES (?, ?, ?) "
                    "ON CONFLICT(tool_use_id) DO UPDATE SET last_seen = ?",
                    [(tid, now, now, now) for tid in ids],
                )
        except sqlite3.OperationalError as exc:
            logger.warning(f"ClearingStore.add failed: {exc}")

    def touch_last_seen(self, tool_use_ids: Iterable[str]) -> None:
        """Bump ``last_seen`` for IDs without adding new ones.  Used to
        keep active-session IDs alive against the pruning policy even if
        they're in the ``keep_recent`` window and haven't been cleared
        yet but might be later."""
        ids = [tid for tid in tool_use_ids if tid]
        if not ids:
            return
        now = time.time()
        try:
            with self._conn() as c:
                # Only update rows that already exist — never create new ones
                c.executemany(
                    "UPDATE cleared_tool_uses SET last_seen = ? "
                    "WHERE tool_use_id = ?",
                    [(now, tid) for tid in ids],
                )
        except sqlite3.OperationalError as exc:
            logger.debug(f"ClearingStore.touch_last_seen failed: {exc}")

    def prune_older_than(self, days: float = DEFAULT_PRUNE_DAYS) -> int:
        """Drop entries whose ``last_seen`` is older than ``days``.
        Returns the number of rows deleted.  Safe to call periodically
        from a background task or at agent startup."""
        cutoff = time.time() - (days * 86400)
        try:
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM cleared_tool_uses WHERE last_seen < ?",
                    (cutoff,),
                )
                return cur.rowcount or 0
        except sqlite3.OperationalError as exc:
            logger.warning(f"ClearingStore.prune_older_than failed: {exc}")
            return 0

    def stats(self) -> dict:
        """Basic observability for the dashboard / debug."""
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT COUNT(*) as n, "
                    "MIN(cleared_at) as oldest_cleared, "
                    "MAX(last_seen) as newest_seen "
                    "FROM cleared_tool_uses",
                ).fetchone()
            return {
                "total_cleared_ids": row["n"] or 0,
                "oldest_cleared_at": row["oldest_cleared"],
                "newest_seen_at": row["newest_seen"],
            }
        except sqlite3.OperationalError:
            return {"total_cleared_ids": 0}


# EXTRACTION SEAM (recorded 2026-04-24):
# - Fleet-manager dependencies: NONE.  stdlib + sqlite3 only.
# - External dependencies: NONE.
# - Public surface: ClearingStore (load_all, add, touch_last_seen,
#   prune_older_than, stats).  DEFAULT_PRUNE_DAYS constant.
# - Pairs with ``context_management.py`` to provide the stable-cut
#   behavior described in that module's docstring.  Could be extracted
#   alongside it cleanly as part of the reliability-layer package.
