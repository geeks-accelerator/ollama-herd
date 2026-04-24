"""Tests for ClearingStore — persistent sticky-cleared tool_use_ids.

Contract: once we decide to clear a tool_use_id, it stays cleared.
Across process restarts (persistent SQLite).  That's what makes Layer 1
stable-cut instead of stateless-sliding.
"""

from __future__ import annotations

import time

import pytest

from fleet_manager.server.clearing_store import ClearingStore


@pytest.fixture
def store(tmp_path):
    return ClearingStore(tmp_path / "cleared.sqlite")


def test_empty_store_loads_empty_set(store):
    assert store.load_all() == set()


def test_add_ids_persists(store):
    store.add(["toolu_abc", "toolu_def"])
    assert store.load_all() == {"toolu_abc", "toolu_def"}


def test_add_is_idempotent(store):
    store.add(["toolu_abc"])
    store.add(["toolu_abc"])
    store.add(["toolu_abc"])
    assert store.load_all() == {"toolu_abc"}


def test_add_ignores_empty_ids(store):
    store.add(["", None, "toolu_real"])  # type: ignore
    assert store.load_all() == {"toolu_real"}


def test_survives_reopen(tmp_path):
    """Persistence — same path, new instance, state carries over."""
    path = tmp_path / "persist.sqlite"
    s1 = ClearingStore(path)
    s1.add(["toolu_a", "toolu_b"])
    del s1
    s2 = ClearingStore(path)
    assert s2.load_all() == {"toolu_a", "toolu_b"}


def test_touch_last_seen_does_not_add(store):
    """touch_last_seen should NOT insert new rows."""
    store.touch_last_seen(["toolu_not_there"])
    assert store.load_all() == set()


def test_touch_last_seen_updates_existing(store):
    """touch_last_seen should bump last_seen on existing rows."""
    store.add(["toolu_abc"])
    time.sleep(0.01)
    store.touch_last_seen(["toolu_abc"])
    s = store.stats()
    assert s["total_cleared_ids"] == 1
    # last_seen should be >= cleared_at by at least a bit
    assert s["newest_seen_at"] >= s["oldest_cleared_at"]


def test_prune_older_than_drops_stale(store):
    store.add(["toolu_old", "toolu_fresh"])
    # Manually backdate one entry's last_seen
    import sqlite3
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "UPDATE cleared_tool_uses SET last_seen = ? WHERE tool_use_id = ?",
            (time.time() - 30 * 86400, "toolu_old"),
        )
    n = store.prune_older_than(days=7)
    assert n == 1
    assert store.load_all() == {"toolu_fresh"}


def test_prune_noop_when_all_fresh(store):
    store.add(["toolu_a"])
    assert store.prune_older_than(days=7) == 0
    assert store.load_all() == {"toolu_a"}


def test_stats_shape(store):
    store.add(["a", "b", "c"])
    s = store.stats()
    assert s["total_cleared_ids"] == 3
    assert s["oldest_cleared_at"] is not None
    assert s["newest_seen_at"] is not None
