"""Tests for the Context Hygiene Compactor.

The critical correctness invariant: **same input → same output bytes, forever**.
If the compactor produces a different summary for the same raw content across
invocations, we bust mlx's prefix cache and lose the 10-100× warm-turn speedup.
Tests enforce this via a deterministic fake curator + cache verification.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fleet_manager.server.context_compactor import (
    STRATEGIES_BY_TOOL,
    STRATEGY_VERSION,
    CompactionReport,
    ContextCompactor,
    SummaryCache,
    _estimate_tokens,
    _sha256,
)

# ----------------------------------------------------------------------------
# Fake curator — returns deterministic summaries for test purposes
# ----------------------------------------------------------------------------


class DeterministicFakeCurator:
    """Returns `[FAKE_SUMMARY] <first 40 chars of input>` — deterministic."""

    def __init__(self, model: str = "fake-curator"):
        self.model = model
        self.call_count = 0
        self.last_prompt = None

    async def summarize(
        self, system: str, prompt: str, max_tokens: int = 512, timeout_s: float = 60.0,
    ) -> str | None:
        self.call_count += 1
        self.last_prompt = prompt
        # Mimic a summary: preserve some identifying info from the input so
        # downstream model can still reason.  Deterministic!
        seed = prompt[:40].replace("\n", " ")
        return f"[FAKE_SUMMARY] {seed}"


class AlwaysFailCurator:
    """Simulates curator timeouts / API failures."""

    def __init__(self):
        self.model = "always-fail"
        self.call_count = 0

    async def summarize(self, system, prompt, max_tokens=512, timeout_s=60.0):
        self.call_count += 1
        return None


# ----------------------------------------------------------------------------
# SummaryCache tests — persistence + content addressing
# ----------------------------------------------------------------------------


def _tmp_cache() -> SummaryCache:
    d = tempfile.mkdtemp(prefix="compactor_test_")
    return SummaryCache(Path(d) / "cache.sqlite")


def test_cache_empty_returns_none():
    cache = _tmp_cache()
    assert cache.get("abc123", "read") is None


def test_cache_put_then_get_roundtrip():
    cache = _tmp_cache()
    cache.put(
        content_hash="abc123", strategy="read", summary="SUMMARY OF FOO",
        original_tokens=1000, summary_tokens=50, curator_model="gpt-oss:120b",
    )
    assert cache.get("abc123", "read") == "SUMMARY OF FOO"


def test_cache_strategy_isolation():
    """Same content, different strategy → different cache entries."""
    cache = _tmp_cache()
    cache.put("abc", "read", "READ SUMMARY", 100, 10, "m")
    cache.put("abc", "bash", "BASH SUMMARY", 100, 10, "m")
    assert cache.get("abc", "read") == "READ SUMMARY"
    assert cache.get("abc", "bash") == "BASH SUMMARY"


def test_cache_version_isolation():
    """Bumping strategy version invalidates old cache entries."""
    cache = _tmp_cache()
    cache.put("abc", "read", "OLD", 100, 10, "m", version="v0")
    cache.put("abc", "read", "NEW", 100, 10, "m", version="v1")
    assert cache.get("abc", "read", version="v0") == "OLD"
    assert cache.get("abc", "read", version="v1") == "NEW"


def test_cache_stats():
    cache = _tmp_cache()
    cache.put("a", "read", "x", 1000, 50, "m")
    cache.put("b", "read", "y", 2000, 80, "m")
    # Access a twice
    cache.get("a", "read")
    cache.get("a", "read")
    cache.get("b", "read")
    s = cache.stats()
    assert s["entries"] == 2
    assert s["total_original_tokens"] == 3000
    assert s["total_summary_tokens"] == 130
    assert s["total_hits"] == 3


# ----------------------------------------------------------------------------
# ContextCompactor — pass-through when under budget
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_budget_passes_through_unchanged():
    cache = _tmp_cache()
    curator = DeterministicFakeCurator()
    compactor = ContextCompactor(curator, cache, budget_tokens=20_000)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out, report = await compactor.maybe_compact(messages)
    assert out == messages  # identical, no mutation
    assert report.triggered is False
    assert curator.call_count == 0


# ----------------------------------------------------------------------------
# ContextCompactor — invariant: deterministic compaction
# ----------------------------------------------------------------------------


def _make_long_read_conversation(file_content: str) -> list[dict]:
    """Build a realistic msgs list with a tool_use(Read) + tool_result."""
    return [
        {"role": "user", "content": "Read src/foo.py"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll read it."},
                {
                    "type": "tool_use",
                    "id": "toolu_001",
                    "name": "Read",
                    "input": {"file_path": "src/foo.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_001", "content": file_content},
            ],
        },
        # Several more turns to push over budget and make the first turn eligible
        {"role": "assistant", "content": "Analyzing..."},
        {"role": "user", "content": "Explain line 20"},
        {"role": "assistant", "content": "Line 20 does..."},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "here is more..."},
    ]


@pytest.mark.asyncio
async def test_same_content_produces_same_summary_across_runs():
    """THE CRITICAL INVARIANT: compacting the same conversation twice must
    yield byte-identical output.  If this fails, mlx cache gets busted."""
    cache = _tmp_cache()
    curator = DeterministicFakeCurator()
    # Low budget to force compaction
    compactor = ContextCompactor(
        curator, cache, budget_tokens=500, preserve_last_turns=1,
    )
    big_file = "import x\n" + ("def foo():\n    return 1\n" * 500)
    msgs = _make_long_read_conversation(big_file)

    out1, r1 = await compactor.maybe_compact(msgs)
    out2, r2 = await compactor.maybe_compact(msgs)
    assert out1 == out2  # BYTE IDENTICAL — no matter how many times we run
    assert r1.triggered is True
    # Second run hit cache; curator invoked only once total
    assert curator.call_count == 1


@pytest.mark.asyncio
async def test_summary_cache_shared_across_compactor_instances():
    """Two separate ContextCompactor instances sharing the same SQLite cache
    must see each other's summaries.  Simulates router restart: cache
    persists, next session hits it."""
    cache = _tmp_cache()
    curator_a = DeterministicFakeCurator()
    curator_b = DeterministicFakeCurator()

    big_file = "x" * 8000
    msgs = _make_long_read_conversation(big_file)

    compactor_a = ContextCompactor(curator_a, cache, budget_tokens=500, preserve_last_turns=1)
    _, _ = await compactor_a.maybe_compact(msgs)
    assert curator_a.call_count == 1

    compactor_b = ContextCompactor(curator_b, cache, budget_tokens=500, preserve_last_turns=1)
    out_b, _ = await compactor_b.maybe_compact(msgs)
    # Curator B should NOT have been called — cache hit from A's work
    assert curator_b.call_count == 0

    # Outputs must match byte-for-byte
    out_a, _ = await compactor_a.maybe_compact(msgs)
    assert out_a == out_b


# ----------------------------------------------------------------------------
# ContextCompactor — preservation guarantees
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preserves_last_n_turns_verbatim():
    """Recent turns must be passed through untouched.  Even if the recent
    turn contains a big tool_result, it stays verbatim."""
    cache = _tmp_cache()
    curator = DeterministicFakeCurator()
    compactor = ContextCompactor(
        curator, cache, budget_tokens=500, preserve_last_turns=2,
    )
    big = "x" * 10000
    msgs = [
        {"role": "user", "content": "old stuff"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "old"},
            {"type": "tool_use", "id": "toolu_old", "name": "Read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_old", "content": big},
        ]},
        # Recent 4 messages should be preserved (= 2 turns * 2 roles)
        {"role": "assistant", "content": [
            {"type": "text", "text": "recent"},
            {"type": "tool_use", "id": "toolu_recent", "name": "Read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_recent", "content": big},
        ]},
        {"role": "assistant", "content": "reasoning"},
        {"role": "user", "content": "next?"},
    ]
    out, report = await compactor.maybe_compact(msgs)
    assert report.triggered is True
    # The recent tool_result (toolu_recent) must still have the raw big content
    recent_result = next(
        b for m in out if m.get("role") == "user" and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("tool_use_id") == "toolu_recent"
    )
    assert recent_result["content"] == big  # verbatim
    # The old one should be compacted
    old_result = next(
        b for m in out if m.get("role") == "user" and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("tool_use_id") == "toolu_old"
    )
    assert "[COMPACTED" in old_result["content"]


@pytest.mark.asyncio
async def test_tool_use_blocks_never_compacted():
    """Only tool_RESULT blocks are compactable.  tool_use (assistant's own
    calls) stays verbatim — compacting model output breaks trace continuity."""
    cache = _tmp_cache()
    curator = DeterministicFakeCurator()
    compactor = ContextCompactor(
        curator, cache, budget_tokens=500, preserve_last_turns=0,
    )
    # Assistant with a big tool_use block (simulating a huge input arg)
    big_input = {"long_arg": "x" * 10000}
    msgs = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_x", "name": "Read", "input": big_input},
        ]},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    out, _ = await compactor.maybe_compact(msgs)
    # Find the tool_use block — should be verbatim
    tool_use = next(
        b for m in out if m.get("role") == "assistant" and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    )
    assert tool_use["input"] == big_input
    assert "[COMPACTED" not in str(tool_use)


@pytest.mark.asyncio
async def test_unknown_tool_passes_through():
    """tool_result from an unknown tool (not in STRATEGIES_BY_TOOL) is NOT
    compacted — we only know how to summarize specific content types."""
    cache = _tmp_cache()
    curator = DeterministicFakeCurator()
    compactor = ContextCompactor(
        curator, cache, budget_tokens=500, preserve_last_turns=0,
    )
    big = "x" * 10000
    msgs = [
        {"role": "user", "content": "trigger"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_x", "name": "SomeUnknownTool", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_x", "content": big},
        ]},
        # pad so recent-turn preservation doesn't save it
        {"role": "assistant", "content": "done"},
    ]
    out, _ = await compactor.maybe_compact(msgs)
    # Unknown tool → no compaction
    tr = next(
        b for m in out if m.get("role") == "user" and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    assert tr["content"] == big
    assert curator.call_count == 0


# ----------------------------------------------------------------------------
# ContextCompactor — min-bloat threshold
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_tool_results_not_compacted():
    """Don't waste curator calls on tiny tool_results — min_bloat_tokens guard."""
    cache = _tmp_cache()
    curator = DeterministicFakeCurator()
    compactor = ContextCompactor(
        curator, cache, budget_tokens=100, preserve_last_turns=0,
    )
    msgs = [
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_a", "name": "Read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a", "content": "short file\n" * 3},
        ]},
        {"role": "assistant", "content": "done"},
    ]
    out, _ = await compactor.maybe_compact(msgs)
    # STRATEGY_READ.min_bloat_tokens is 1500 → small content skipped
    assert curator.call_count == 0
    tr = out[2]["content"][0]
    assert "[COMPACTED" not in tr["content"]


# ----------------------------------------------------------------------------
# ContextCompactor — fail-open on curator errors
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_failure_passes_content_through():
    """If curator times out / fails, the original content must pass through
    unchanged.  Bad summary is worse than no summary."""
    cache = _tmp_cache()
    curator = AlwaysFailCurator()
    compactor = ContextCompactor(
        curator, cache, budget_tokens=500, preserve_last_turns=0,
    )
    big = "import foo\ndef bar(): pass\n" * 500  # big enough to trigger compaction
    msgs = _make_long_read_conversation(big)
    out, report = await compactor.maybe_compact(msgs)
    assert report.triggered is True
    # Curator failed → original content preserved
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert "[COMPACTED" not in (b.get("content") or "")


# ----------------------------------------------------------------------------
# ContextCompactor — report accuracy
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_accurately_reflects_work_done():
    cache = _tmp_cache()
    curator = DeterministicFakeCurator()
    compactor = ContextCompactor(
        curator, cache, budget_tokens=500, preserve_last_turns=1,
    )
    big = "x" * 8000  # ~2000 tokens — above Read's min_bloat
    msgs = _make_long_read_conversation(big)
    _, report = await compactor.maybe_compact(msgs)
    assert report.triggered is True
    assert report.tokens_before > report.tokens_after
    assert 0.0 < report.ratio < 1.0
    assert len(report.compactions) >= 1
    c0 = report.compactions[0]
    assert c0["strategy"] == "read"
    assert c0["tokens_before"] > c0["tokens_after"]


# ----------------------------------------------------------------------------
# Utility functions
# ----------------------------------------------------------------------------


def test_sha256_deterministic():
    assert _sha256("foo") == _sha256("foo")
    assert _sha256("foo") != _sha256("bar")


def test_estimate_tokens_nontrivial():
    assert _estimate_tokens("") == 1
    assert _estimate_tokens("x" * 400) == 100


def test_report_to_dict():
    r = CompactionReport(
        triggered=True, tokens_before=5000, tokens_after=2000,
        compactions=[{"strategy": "read", "tokens_before": 3000, "tokens_after": 200}],
    )
    d = r.to_dict()
    assert d["triggered"] is True
    assert d["ratio"] == 0.4
    assert len(d["compactions"]) == 1


def test_strategies_cover_expected_tools():
    """If new tools appear in captured traffic, add strategies as needed."""
    assert "Read" in STRATEGIES_BY_TOOL
    assert "Bash" in STRATEGIES_BY_TOOL
    assert "WebFetch" in STRATEGIES_BY_TOOL
    assert STRATEGY_VERSION  # non-empty
