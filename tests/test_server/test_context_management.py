"""Tests for mechanical tool-result clearing — the cheap first-layer of
context management that runs before the LLM-based compactor.

Contract: pure function, never mutates input, preserves conversation
structure (tool_use blocks + tool_use_ids stay intact).
"""

from __future__ import annotations

import copy

from fleet_manager.server.context_management import (
    CLEARED_PLACEHOLDER,
    clear_old_tool_results,
)


def _tool_use_pair(tool_id: str, tool_name: str, result_text: str) -> list[dict]:
    """Build a realistic assistant(tool_use) + user(tool_result) pair."""
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"I'll use {tool_name}."},
                {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": result_text},
            ],
        },
    ]


def _build_long_session(n_tool_calls: int, body_size_chars: int = 8000) -> list[dict]:
    """Construct a session with N tool_use/tool_result pairs.  Each result
    body is large enough to push total tokens over reasonable triggers."""
    msgs: list[dict] = [{"role": "user", "content": "help me debug"}]
    body = "x" * body_size_chars
    for i in range(n_tool_calls):
        msgs.extend(_tool_use_pair(f"tu_{i}", "Bash", f"{body}\nresult {i}"))
    return msgs


# ---------------------------------------------------------------------------
# Trigger gating
# ---------------------------------------------------------------------------


def test_under_trigger_passes_through_unchanged():
    msgs = _build_long_session(n_tool_calls=3, body_size_chars=1000)
    out, report = clear_old_tool_results(
        msgs, keep_recent=3, trigger_tokens=1_000_000,
    )
    assert out == msgs  # exact equality — no mutation, no change
    assert report.triggered is False
    assert report.tool_results_cleared == 0


def test_over_trigger_with_too_few_results_still_passes_through():
    """Trigger fires but keep_recent >= total → nothing to clear."""
    msgs = _build_long_session(n_tool_calls=2, body_size_chars=20_000)
    out, report = clear_old_tool_results(
        msgs, keep_recent=5, trigger_tokens=1000,
    )
    assert out == msgs
    assert report.triggered is False
    assert report.tool_results_kept == 2


# ---------------------------------------------------------------------------
# Actually clearing
# ---------------------------------------------------------------------------


def test_keeps_newest_n_clears_rest():
    msgs = _build_long_session(n_tool_calls=10, body_size_chars=8000)
    out, report = clear_old_tool_results(
        msgs, keep_recent=3, trigger_tokens=1000,
    )
    assert report.triggered is True
    assert report.tool_results_total == 10
    assert report.tool_results_kept == 3
    assert report.tool_results_cleared == 7
    # Oldest 7 tool_use_ids cleared
    assert set(report.cleared_tool_use_ids) == {f"tu_{i}" for i in range(7)}


def test_cleared_tool_results_preserve_tool_use_id_and_shape():
    """The placeholder must NOT strip the tool_use_id — the model still
    needs to see that structure to keep the conversation coherent."""
    msgs = _build_long_session(n_tool_calls=5, body_size_chars=8000)
    out, _ = clear_old_tool_results(
        msgs, keep_recent=1, trigger_tokens=1000,
    )
    # Walk out, find cleared tool_results
    cleared_count = 0
    for m in out:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for block in c:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("content") == CLEARED_PLACEHOLDER
            ):
                cleared_count += 1
                # tool_use_id must still be there
                assert block.get("tool_use_id", "").startswith("tu_")
    assert cleared_count == 4  # 5 total minus 1 kept


def test_assistant_tool_use_blocks_never_touched():
    """Clearing operates ONLY on tool_result blocks.  Assistant-side
    tool_use blocks are the model's own output and must stay verbatim
    to preserve the reasoning trail."""
    msgs = _build_long_session(n_tool_calls=4, body_size_chars=10_000)
    out, _ = clear_old_tool_results(
        msgs, keep_recent=1, trigger_tokens=1000,
    )
    # Count tool_use blocks in input and output — must match
    def count_tool_uses(ms: list[dict]) -> int:
        n = 0
        for m in ms:
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n += 1
        return n
    assert count_tool_uses(out) == count_tool_uses(msgs)


def test_token_count_drops_substantially():
    msgs = _build_long_session(n_tool_calls=10, body_size_chars=8000)
    out, report = clear_old_tool_results(
        msgs, keep_recent=2, trigger_tokens=1000,
    )
    # Rough check: clearing 8 of 10 ~2K-token bodies should cut ~16K tokens
    # from the 20K-total session.  Ratio should be < 0.3.
    assert report.ratio < 0.4


def test_does_not_mutate_input():
    msgs = _build_long_session(n_tool_calls=5, body_size_chars=8000)
    original = copy.deepcopy(msgs)
    clear_old_tool_results(msgs, keep_recent=1, trigger_tokens=1000)
    assert msgs == original  # input unchanged


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_messages():
    out, report = clear_old_tool_results([], keep_recent=3, trigger_tokens=1000)
    assert out == []
    assert report.triggered is False
    assert report.tool_results_total == 0


def test_messages_with_string_content_only():
    """Plain string content (no block list) should pass through."""
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi back"},
    ]
    out, report = clear_old_tool_results(msgs, keep_recent=3, trigger_tokens=1)
    assert out == msgs
    assert report.tool_results_total == 0


def test_keep_recent_zero_clears_everything():
    msgs = _build_long_session(n_tool_calls=3, body_size_chars=8000)
    out, report = clear_old_tool_results(
        msgs, keep_recent=0, trigger_tokens=1000,
    )
    assert report.tool_results_cleared == 3
    assert report.tool_results_kept == 0


def test_multiple_tool_results_in_same_message():
    """Some agents emit multiple tool_results in one user message.  Each
    should be treated as its own positional entry for recency purposes."""
    msgs = [
        {"role": "user", "content": "do two things"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "a", "name": "Bash", "input": {}},
                {"type": "tool_use", "id": "b", "name": "Grep", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "x" * 8000},
                {"type": "tool_result", "tool_use_id": "b", "content": "y" * 8000},
            ],
        },
        # ... plus more to push over trigger
        *_build_long_session(4, 8000)[1:],  # skip the leading user msg
    ]
    out, report = clear_old_tool_results(
        msgs, keep_recent=2, trigger_tokens=1000,
    )
    assert report.triggered is True
    # Total: 2 in-message + 4 from the extended session = 6 tool_results
    assert report.tool_results_total == 6
    assert report.tool_results_kept == 2


# ---------------------------------------------------------------------------
# Sticky-cut behavior — the real fix for the prefix-cache busting bug
# ---------------------------------------------------------------------------


def _make_turn(n_tool_pairs: int, body_size_chars: int = 8000) -> list[dict]:
    """Build a conversation with N tool_use/tool_result pairs."""
    msgs = [{"role": "user", "content": "help me debug"}]
    body = "x" * body_size_chars
    for i in range(n_tool_pairs):
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"tu_{i}", "name": "Bash", "input": {}},
            ],
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"tu_{i}",
                 "content": f"{body}\nresult {i}"},
            ],
        })
    return msgs


class _InMemoryStickyStore:
    """Duck-typed ClearingStore substitute for tests — no SQLite dep."""

    def __init__(self):
        self.cleared: set[str] = set()
        self.add_calls: list[list[str]] = []
        self.touches: list[list[str]] = []

    def load_all(self):
        return set(self.cleared)

    def add(self, ids):
        ids = [i for i in ids if i]
        self.cleared.update(ids)
        self.add_calls.append(list(ids))

    def touch_last_seen(self, ids):
        self.touches.append(list(i for i in ids if i))


def test_sticky_cut_once_cleared_stays_cleared_next_turn():
    """THE critical fix: on turn N+1 the 'last 3' window shifts, but the
    tool_use_id we cleared on turn N must remain cleared — producing
    byte-identical placeholder at that position so MLX prefix-cache hits."""
    from fleet_manager.server.context_management import clear_old_tool_results
    store = _InMemoryStickyStore()

    # Turn N: 10 tool pairs, keep last 3.  Expect tu_0..tu_6 cleared.
    turn_n = _make_turn(n_tool_pairs=10)
    out_n, r_n = clear_old_tool_results(
        turn_n, keep_recent=3, trigger_tokens=1000, sticky_store=store,
    )
    assert r_n.triggered
    assert r_n.tool_results_cleared == 7
    assert store.cleared == {f"tu_{i}" for i in range(7)}

    # Turn N+1: 11 tool pairs (one new added). 'Last 3' is now tu_8..tu_10.
    # Old stateless behavior: tu_7 would NEWLY be cleared now.
    # Sticky behavior: tu_7 is STILL not in store, tu_0..tu_6 are.
    #   => clear_positions = {tu_0..tu_6} (sticky) + {tu_7} (new advance)
    #   => window for keep_recent=3 is {tu_8, tu_9, tu_10}
    turn_np1 = _make_turn(n_tool_pairs=11)
    out_np1, r_np1 = clear_old_tool_results(
        turn_np1, keep_recent=3, trigger_tokens=1000, sticky_store=store,
    )
    assert r_np1.triggered
    # tu_0..tu_7 all cleared now
    assert r_np1.tool_results_cleared == 8
    assert store.cleared == {f"tu_{i}" for i in range(8)}
    # AND — critical — the placeholder bytes at tu_0..tu_6 positions
    # should be IDENTICAL between turn N and turn N+1 outputs
    from fleet_manager.server.context_management import CLEARED_PLACEHOLDER
    def find_tool_result(msgs, tuid):
        for m in msgs:
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result" \
                        and b.get("tool_use_id") == tuid:
                    return b
        return None
    for i in range(7):
        a = find_tool_result(out_n, f"tu_{i}")
        b = find_tool_result(out_np1, f"tu_{i}")
        assert a is not None and b is not None
        assert a["content"] == CLEARED_PLACEHOLDER
        assert b["content"] == CLEARED_PLACEHOLDER
        # And both have the same tool_use_id preserved
        assert a["tool_use_id"] == b["tool_use_id"] == f"tu_{i}"


def test_sticky_cut_under_trigger_does_not_advance_but_respects_existing():
    """If prompt is under trigger but some IDs are already sticky, we
    should STILL clear those.  Partial clearing is cheap; letting old
    sticky IDs un-clear would break cache stability."""
    from fleet_manager.server.context_management import clear_old_tool_results
    store = _InMemoryStickyStore()
    store.cleared = {"tu_0", "tu_1"}  # pretend previous turn cleared these

    # Short conversation that's under trigger
    msgs = _make_turn(n_tool_pairs=3, body_size_chars=500)
    out, report = clear_old_tool_results(
        msgs, keep_recent=3, trigger_tokens=1_000_000, sticky_store=store,
    )
    # Sticky IDs still get cleared even though we're under trigger
    assert report.triggered
    assert report.tool_results_cleared == 2
    # No new IDs added — we were under trigger
    assert store.cleared == {"tu_0", "tu_1"}


def test_sticky_cut_pure_under_trigger_passes_through():
    """Under trigger AND no sticky IDs yet → pure pass-through."""
    from fleet_manager.server.context_management import clear_old_tool_results
    store = _InMemoryStickyStore()
    msgs = _make_turn(n_tool_pairs=2, body_size_chars=500)
    out, report = clear_old_tool_results(
        msgs, keep_recent=3, trigger_tokens=1_000_000, sticky_store=store,
    )
    assert out == msgs
    assert report.triggered is False
    # Touch last_seen was called, but no IDs were added
    assert store.cleared == set()
    assert len(store.touches) >= 1  # touched active-session IDs


def test_stateless_path_without_store_unchanged():
    """When sticky_store is None, behavior matches the original stateless
    clearing — keep backwards compat + existing tests still pass."""
    from fleet_manager.server.context_management import clear_old_tool_results
    msgs = _make_turn(n_tool_pairs=10, body_size_chars=8000)
    out, report = clear_old_tool_results(
        msgs, keep_recent=3, trigger_tokens=1000, sticky_store=None,
    )
    assert report.triggered
    assert report.tool_results_cleared == 7
    assert report.tool_results_kept == 3
