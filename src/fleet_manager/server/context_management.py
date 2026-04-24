"""Mechanical context-management strategies — tool-result clearing by age.

Closes the biggest structural gap vs hosted Claude Code: dropping old
tool_result blocks from the conversation without invoking an LLM.  This
is what Anthropic's [Context Editing API](https://platform.claude.com/docs/en/build-with-claude/context-editing)
does for ``tool_results`` — a cheap, deterministic layer that runs
BEFORE the expensive LLM-based compaction.

Why this module exists independently of ``context_compactor.py``:

- The compactor summarises ``tool_result`` content via a curator LLM
  call.  That's expensive, context-blocking, and has a 60s timeout.
- Most of the bloat in a long Claude Code session is stale tool output
  the model will never reference again (that ``ls -la`` from turn 7).
  Running an LLM to "summarise" a 50K-token directory listing is
  overkill — just drop it.
- Hosted Claude does exactly this.  Observed session traces (2026-04-23)
  suggest Claude Code's hosted path keeps roughly the last 3-5
  ``tool_use/tool_result`` pairs intact and replaces the rest with a
  placeholder that preserves conversation structure.

Policy (matches our observed Claude Code hosted behavior):

  - Keep ALL messages that aren't ``tool_result`` blocks (user text,
    assistant text, assistant ``tool_use`` calls) — those are cheap and
    the model's own reasoning trail needs them to stay coherent.
  - Keep the N most recent ``tool_result`` blocks verbatim.
  - Replace older ``tool_result`` content with a short placeholder
    (``[tool_result cleared — see earlier turns]``) keyed to the same
    ``tool_use_id`` so the model still sees "tool X was called and
    returned something" without the 50K-token body.
  - Never clear tool_results from the preserved-recency window even
    if they're "old" by turn count.

This is pure — no I/O, no LLM call, no cache.  Runs in microseconds.

# EXTRACTION SEAM (recorded 2026-04-24):
# - Fleet-manager dependencies: NONE.  stdlib + logging only.
# - External dependencies: NONE.
# - Public surface to preserve if extracted:
#     CLEARED_PLACEHOLDER (str)
#     ClearingReport (dataclass)
#     clear_old_tool_results(messages, *, keep_recent, trigger_tokens, placeholder)
#     clear_if_over_budget(messages, *, keep_recent, trigger_tokens)
# - Input shape: Anthropic-native message dicts.  Output shape: same dicts.
#   No coupling to fleet routing / queue / trace store.
# - Drop-in copyable to any Anthropic proxy.  Pairs well with ``tool_schema_fixup``
#   and ``tool_call_repair`` — the three form a self-contained "reliability
#   layer" that's independent of the fleet machinery.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Sentinel text we substitute for cleared tool_result bodies.  Keep short —
# this is what the model sees in place of the original content.  Stable
# across runs so the prefix cache (MLX / mlx_lm.server) stays hot after a
# clear event.
CLEARED_PLACEHOLDER = "[tool_result cleared — not in window of recent context]"


@dataclass
class ClearingReport:
    """What the clearer did on a single request.  Flows into traces/logs."""

    triggered: bool = False
    tokens_before: int = 0
    tokens_after: int = 0
    tool_results_total: int = 0
    tool_results_kept: int = 0
    tool_results_cleared: int = 0
    cleared_tool_use_ids: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        if not self.tokens_before:
            return 1.0
        return self.tokens_after / self.tokens_before

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "ratio": round(self.ratio, 3),
            "tool_results_total": self.tool_results_total,
            "tool_results_kept": self.tool_results_kept,
            "tool_results_cleared": self.tool_results_cleared,
            "cleared_tool_use_ids": list(self.cleared_tool_use_ids),
        }


def _estimate_tokens(text: str) -> int:
    """Match the compactor's estimator — 4 chars/token."""
    return max(1, len(text) // 4)


def _total_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across all messages' content."""
    import json
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += _estimate_tokens(c)
        elif isinstance(c, list):
            for block in c:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    total += _estimate_tokens(block.get("text") or "")
                elif block.get("type") == "tool_use":
                    total += _estimate_tokens(
                        json.dumps(block.get("input") or {})
                    )
                elif block.get("type") == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, str):
                        total += _estimate_tokens(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                total += _estimate_tokens(sub.get("text") or "")
    return total


def _iter_tool_result_positions(
    messages: list[dict],
) -> list[tuple[int, int, str]]:
    """Return [(msg_idx, block_idx, tool_use_id), ...] for every tool_result,
    in conversation order — oldest first."""
    out: list[tuple[int, int, str]] = []
    for i, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for j, block in enumerate(c):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
            ):
                tuid = block.get("tool_use_id") or f"unknown-{i}-{j}"
                out.append((i, j, tuid))
    return out


def clear_old_tool_results(
    messages: list[dict],
    *,
    keep_recent: int = 3,
    trigger_tokens: int = 100_000,
    placeholder: str = CLEARED_PLACEHOLDER,
    sticky_store=None,
) -> tuple[list[dict], ClearingReport]:
    """Drop old ``tool_result`` bodies, keeping the ``keep_recent`` newest intact.

    Args:
        messages: Anthropic-format messages (content may be string or block list).
        keep_recent: Number of most-recent ``tool_result`` blocks to preserve
            verbatim.  Older ones have their content replaced with ``placeholder``
            but the block itself (and its ``tool_use_id``) stays so the
            conversation structure is preserved.
        trigger_tokens: Only run when the estimated total token count exceeds
            this threshold.  Below, pass through unchanged.  Default 100K
            matches hosted Claude's observed behavior of starting to trim
            around 150K but leaving short sessions alone.
        placeholder: Replacement text.  Kept stable across runs for prefix
            cache stability on MLX.
        sticky_store: Optional ``ClearingStore``-like object with
            ``load_all()`` / ``add(ids)`` / ``touch_last_seen(ids)``
            methods.  When provided, enables STABLE-CUT behavior: once a
            ``tool_use_id`` has been cleared, it stays cleared in all
            future turns even if it would now be within the ``keep_recent``
            window.  This preserves MLX prefix-cache hit rate across turns
            (without this, the "last 3" window shifts forward each turn,
            bytes at prior positions change, cache invalidates).  See
            ``server/clearing_store.py`` for the full rationale.

    Returns:
        (new_messages, report).  Does NOT modify input.

    Pure / synchronous / no I/O (unless ``sticky_store`` is provided;
    that introduces one SQLite read + one SQLite write on clear events).
    """
    report = ClearingReport()
    report.tokens_before = _total_tokens(messages)

    positions = _iter_tool_result_positions(messages)
    report.tool_results_total = len(positions)

    # Load previously-cleared IDs (persistent sticky set).  Empty set if
    # sticky mode is disabled or the store is empty.
    sticky_ids: set[str] = set()
    if sticky_store is not None:
        try:
            sticky_ids = sticky_store.load_all()
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                f"sticky_store.load_all failed: {type(exc).__name__}: {exc}",
            )
            sticky_ids = set()

    # If we're under trigger AND there's nothing sticky to clear, skip
    # entirely.  The usual "short session" fast path.
    has_sticky_to_clear = any(tuid in sticky_ids for _, _, tuid in positions)
    if report.tokens_before <= trigger_tokens and not has_sticky_to_clear:
        report.tokens_after = report.tokens_before
        # Still touch last_seen so active-session IDs don't get pruned
        if sticky_store is not None and positions:
            with contextlib.suppress(Exception):
                sticky_store.touch_last_seen([tuid for _, _, tuid in positions])
        return messages, report

    # STABLE-CUT logic when sticky_store provided:
    #   1. Clear everything whose tool_use_id is already in sticky_ids
    #      (these are bytes we've already committed to the cleared form —
    #       keeping them consistent preserves prefix-cache stability).
    #   2. If we're still over trigger AFTER step 1, advance the boundary:
    #      add the oldest not-yet-sticky tool_use_ids to the store and
    #      clear them too, walking forward until we'd enter the
    #      keep_recent window.
    new_sticky_ids: list[str] = []  # IDs to add to store at end

    if sticky_store is not None:
        # Step 1: clear everything already in sticky_ids
        clear_positions: dict[tuple[int, int], str] = {
            (mi, bi): tuid for mi, bi, tuid in positions if tuid in sticky_ids
        }
        # Step 2: if still over, advance
        #
        # "still over" is estimated by remaining non-cleared tokens.  We
        # use a cheap approximation: if len(non-sticky) > keep_recent AND
        # tokens_before > trigger, we know layer-1 would bite anyway.
        non_sticky = [(mi, bi, tuid) for mi, bi, tuid in positions if tuid not in sticky_ids]
        if (
            report.tokens_before > trigger_tokens
            and len(non_sticky) > keep_recent
        ):
            # Clear oldest non-sticky up to the keep_recent boundary
            to_advance = non_sticky[:-keep_recent] if keep_recent > 0 else non_sticky
            for mi, bi, tuid in to_advance:
                clear_positions[(mi, bi)] = tuid
                new_sticky_ids.append(tuid)
        # keep_ids is everything not in clear_positions
        keep_ids = {
            tuid for mi, bi, tuid in positions
            if (mi, bi) not in clear_positions
        }
    else:
        # Original stateless behavior (fallback for when sticky_store is None)
        if report.tokens_before <= trigger_tokens:
            report.tokens_after = report.tokens_before
            return messages, report
        if len(positions) <= keep_recent:
            report.tokens_after = report.tokens_before
            report.tool_results_kept = len(positions)
            return messages, report
        # Positions are oldest-first; keep the last `keep_recent` intact
        to_clear = positions[:-keep_recent] if keep_recent > 0 else positions
        keep_ids = {tuid for _, _, tuid in positions[-keep_recent:]} if keep_recent > 0 else set()
        clear_positions = {(mi, bi): tuid for mi, bi, tuid in to_clear}

    if not clear_positions:
        # Nothing to clear (was over trigger but sticky-store path didn't
        # advance — e.g. tokens over trigger but non_sticky already fits
        # within keep_recent).  Pass through.
        report.tokens_after = report.tokens_before
        report.tool_results_kept = len(positions)
        return messages, report

    report.triggered = True
    report.tool_results_kept = len(keep_ids)
    report.tool_results_cleared = len(clear_positions)
    report.cleared_tool_use_ids = list(clear_positions.values())

    # Rebuild messages with cleared tool_results — never mutate input
    out_messages: list[dict] = []
    for i, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            out_messages.append(m)
            continue
        any_cleared = any((i, j) in clear_positions for j in range(len(c)))
        if not any_cleared:
            out_messages.append(m)
            continue
        new_blocks: list[Any] = []
        for j, block in enumerate(c):
            if (i, j) in clear_positions and isinstance(block, dict):
                # Replace content but keep shape + tool_use_id
                new_block = {
                    **block,
                    "content": placeholder,
                }
                new_blocks.append(new_block)
            else:
                new_blocks.append(block)
        out_messages.append({**m, "content": new_blocks})

    # Persist newly-cleared IDs to the store so next turn they stay
    # sticky.  Also touch last_seen for ALL encountered IDs (keep active
    # session IDs alive against the pruning policy).
    if sticky_store is not None:
        if new_sticky_ids:
            try:
                sticky_store.add(new_sticky_ids)
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.warning(
                    f"sticky_store.add failed: {type(exc).__name__}: {exc}",
                )
        with contextlib.suppress(Exception):
            sticky_store.touch_last_seen([tuid for _, _, tuid in positions])

    report.tokens_after = _total_tokens(out_messages)
    return out_messages, report


def clear_if_over_budget(
    messages: list[dict],
    *,
    keep_recent: int = 3,
    trigger_tokens: int = 100_000,
    sticky_store=None,
) -> tuple[list[dict], ClearingReport]:
    """Convenience wrapper that logs at INFO when clearing fires.

    Used by the Anthropic route so operators can see clearing events in
    the standard JSONL log without needing to introspect the report dict.

    When ``sticky_store`` is passed, enables stable-cut behavior that
    preserves MLX prefix-cache hits across turns — see
    ``clear_old_tool_results`` docstring.
    """
    new_messages, report = clear_old_tool_results(
        messages,
        keep_recent=keep_recent,
        trigger_tokens=trigger_tokens,
        sticky_store=sticky_store,
    )
    if report.triggered:
        logger.info(
            "Tool-result clearing: %d→%d tokens (%.1f%%), "
            "cleared %d of %d tool_results (kept %d recent)%s",
            report.tokens_before,
            report.tokens_after,
            report.ratio * 100,
            report.tool_results_cleared,
            report.tool_results_total,
            report.tool_results_kept,
            " [sticky-cut]" if sticky_store is not None else "",
        )
    return new_messages, report
