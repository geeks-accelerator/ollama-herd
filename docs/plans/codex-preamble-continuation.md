# Codex: continuing an abandoned preamble

**Status:** designed, not implemented — detection shipped, continuation deferred
**Date:** 2026-07-18
**Depends on:** [`codex-code-mode-escalation.md`](codex-code-mode-escalation.md)

## The failure

Codex instructs the model to *"send a brief preamble to the user explaining what
you're about to do"* before each tool call. Frontier models emit the preamble
**and** the call in one response. Local models sometimes emit only the preamble —
and a text-only response means "turn complete" in the Responses protocol, so the
agentic loop ends mid-task and the client looks hung.

Observed live: the model explored the repo, read both files, correctly diagnosed
the bug, then said *"Now I understand the issue. Let me run pytest first to
confirm the tests fail, then fix the function."* and stopped.

## Measured rate

Against the real 21KB Codex instructions and real 18-tool schema, on
`qwen3-coder:30b`, 8 trials per cell:

| condition | acted |
|---|---|
| history depth 0 / 2 / 4 / 6 / 8 | 7/8, 8/8, 8/8, 8/8, 8/8 |
| prior text-only assistant turns 0 / 1 / 2 / 3 | 8/8, 7/8, 8/8, 8/8 |

**~3%, flat.** Two hypotheses were tested and falsified: it is *not* a
context-depth effect, and it is *not* conversational poisoning from earlier
text-only turns. It is stochastic.

Note the mitigation already in place — `TURN_COMPLETION_GUIDANCE` — is active in
all of those cells. Without it the rate was 8/15 on a first-turn probe, so the
guidance does most of the work; this is the residue.

## What shipped

`looks_like_abandoned_preamble()` plus a WARNING on both the streaming and
non-streaming paths. This costs nothing and closes a real observability hole:
**the turn succeeds by every server-side measure** — stream completed, tokens
generated, no error — so nothing else distinguishes "the model finished" from
"the model quit mid-task". The rate is now measurable in production:

```bash
grep -c 'ABANDONED PREAMBLE' ~/.fleet-manager/logs/herd.jsonl
```

Detection is deliberately narrow: tools were offered, none were called, and the
text is short *and* reads as an announcement (ends on `:`/`…`, or opens with
"Let me" / "I'll" / "Now I" / "First,"). A genuine final report is longer and
doesn't end on a colon.

## What was deferred, and why

The fix is a server-side continuation: on detecting an abandoned preamble,
re-issue the turn once with the preamble appended plus a nudge, and merge any
tool call into the same response.

Straightforward on the non-streaming path. On the **streaming** path it means
intercepting before `response.completed`, re-entering the SSE state machine with
a second Ollama stream, and opening a new `function_call` item after the message
item has already closed. Codex streams by default, so a non-streaming-only
implementation would help almost nobody.

That is meaningful surgery on the hot path **every** Codex turn traverses, to
recover **~3%** of turns. A defect there converts a 3% stall into a 100% outage.
It should be built deliberately, with the SSE sequence tested against a real
client, rather than appended to a long session.

**Prerequisite before building it:** let the WARNING run in production for a few
days. If the real rate is materially higher than 3%, the tradeoff changes; if it
is lower, this may not be worth building at all. The measurement now exists —
use it.

## Risks for the implementation

- **Double-billing the user's tokens** on every continuation; the second request
  resends the full conversation.
- **Infinite loop** if the continuation also abandons — retry exactly once.
- **Codex may reject an item sequence** it didn't expect (message item closed,
  then a function_call item appearing after). Verify against a real client, not
  a synthetic capture — that distinction has caught two separate bugs already.
- A **false positive** on a genuine short final answer would suppress a
  legitimate completion and cost a pointless round trip.
