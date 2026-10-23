# Upstream changes, October 2026: what herd should do about them

**Status**: 🔨 **Phases 1, 2, 4, 5 DONE (2026-10-22, unreleased); 3 gated; 6 scope only.**

| Phase | Outcome |
|-------|---------|
| 1 — truncated response reported as success | **Done, and broader than planned.** Non-streaming paths too (now 502), mid-stream exceptions now end in-protocol instead of tearing the connection, and two bugs found while live-testing: gpt-oss's OpenAI tool calls were dropped (they ride the `done` line), and OpenAI clients could never see `finish_reason: "length"`. Verified with the real OpenAI and Anthropic SDKs, Python and TypeScript; **not** with Claude Code or Codex themselves (a nested Claude Code refuses to launch inside a session, and was not bypassed). |
| 2 — Ollama's four Responses-layer bugs | **No fixes needed.** Each either cannot occur in herd's translator or is already handled; three tests pin that. |
| 3 — upgrade this box's Ollama | **Not done, deliberately.** Waits for the `0.11.3` day-after check (2026-10-23) so a fleet change cannot confound it, and needs the operator's go-ahead for the app download. |
| 4 — mlx-lm `0.32.2` | **Done, and it found a correctness bug.** Coder 65 → 124 tok/s, GLM 45 → 64. `--quantized-kv-start 0` broke tool calling (0/5 vs 8/8 at `5000`); now `5000`. This box had been on a patched `0.31.3`. One start-`5000` instance answered wrongly from birth — OPEN in `docs/issues.md`. |
| 5 — Windows docs | **Done.** `docs/troubleshooting.md` § *Running a node on Windows*, README platform note. |
| 6 — text-to-speech | Scope only, as planned. |
| Watch list | mflux bf16 seed note added to `docs/guides/image-generation.md`. |

Unplanned, found on the way: `FLEET_MDNS_SERVICE_TYPE` was inert (fixed), and an
over-long service type crashed behind a misleading "stale service" message (fails
fast now).

**Constraints this plan is held to** (CLAUDE.md § Design Principles): greenfield,
so **no `FLEET_*` on/off flags and no second code path** — the corrected behaviour
*is* the behaviour; **reuse before build** — find the existing helper or pattern
first, and extract a shared one rather than copy an inline block.

**Source.** A research pass (another session) read the release notes for everything
herd depends on — Ollama `0.40.4`–`0.40.9`, mlx-lm `0.32.0`–`0.32.2`, mlx `0.32.4`,
mflux `0.21`–`0.23`, fastembed — and checked each against how herd calls it. The
first draft of this plan verified that research against the repo; the **audit**
then read the code each phase would touch, looking for what already exists. It
changed most phases — see § *What the audit changed* at the end.

---

## Phase 1 — Stop reporting a truncated response as a successful one

**Confirmed bug, and broader than first drafted. Do this first.** It lands on the
surfaces herd is marketed for — Claude Code and Codex — and on non-streaming
clients too.

### What happens now

When Ollama's stream ends without a `done:true` line, the proxy notices
(`streaming.py`, both `_stream_with_tracking` and `_stream_with_retry`) and records
the trace `incomplete`. **Then the client is told the response succeeded:**

| surface | mode | what the client gets after an upstream drop | where |
|---|---|---|---|
| OpenAI | stream | `data: [DONE]`, **unconditionally**, no `finish_reason` chunk | `streaming.py:790` |
| OpenAI | non-stream | **HTTP 200** with `finish_reason` hard-coded to `"stop"` | `routes/openai_compat.py` (~:542) |
| Responses | stream | a synthesized `response.completed` | `routes/responses_compat.py:284` |
| Responses | both | `_map_status("")` → `"completed"` | `responses_translator.py:690` |
| Anthropic | stream, Ollama path | hand-built `end_turn` + `message_stop` | `routes/anthropic_compat.py` (~:1150) |
| Anthropic | stream, MLX path | the same hand-built block, a second copy | `routes/anthropic_compat.py:595` |
| Anthropic | non-stream | accumulator starts at `done_reason = ""` → `_map_done_reason` → `"end_turn"` | `anthropic_translator.py` (~:525) |
| Ollama-native | stream | the stream simply ends without `done` | — **correct** |

Ollama-native is correct: that is Ollama's own protocol, and a native client checks
`done` itself. Everything translated turns a failure into a success, and an agent
then acts on a half-written answer or a tool call cut off mid-argument.

**And on the Anthropic MLX path even the trace lies.** It records
`status = "failed" if error else "completed"` (`anthropic_compat.py:~637`), and the
synthesized stop sets no error — so an MLX drop is traced **`completed`**, not
`incomplete`. The first draft of this plan said "the trace correctly says
`incomplete`"; on that path it doesn't.

**Mid-stream exceptions have the adjacent problem.** Both Anthropic generators
`raise` after logging; once SSE headers are sent, that tears the connection
without an error event. Phase 1 should fix both failure modes through one path,
or it fixes half of "how a stream fails" and leaves the two inconsistent.

### The root cause is one missing value, not eight bad sites

Every surface already funnels through a **per-surface `done_reason` mapper** —
`_map_status` (Responses), `_map_done_reason` (Anthropic), and the OpenAI
`finish_reason` expression. "No `done` line ever arrived" reaches each of them as
`""`, which each reads as an ordinary stop. So the fix is to make *upstream ended
without `done`* a **first-class value** and teach the existing mappers one case
each, instead of patching eight call sites.

### The fix, by reuse

1. **One definition of the condition.** A single module-level constant in
   `streaming.py` (e.g. `UPSTREAM_INCOMPLETE`) for the reason, and one message
   string. Today the message `"Stream ended without done:true from Ollama"` is
   already duplicated across `_stream_with_tracking` and `_stream_with_retry`, each
   with its own copy of the warn-and-trace block — **extract that block into one
   `_record_incomplete(...)` helper** both call.
2. **Extend the existing mappers, don't add new ones.** `_map_status` gains
   `UPSTREAM_INCOMPLETE → "failed"`; `_map_done_reason` and the OpenAI expression
   gain the same case. The Responses synthesis already routes through
   `ollama_chunk_to_responses_events` with an injected `done_reason: "stop"` —
   **change that one argument** to the sentinel and let the mapper do the rest.
   Keep the existing `length → "incomplete"` mapping; that is a real model stop.
3. **One error envelope per surface, beside the one that already exists.**
   `routes/routing.py::client_error_passthrough` already builds both the Anthropic
   (`{"type":"error","error":{…}}`) and OpenAI (`{"error":{…}}`) envelopes for 4xx.
   Add its streaming sibling there — one function returning the terminal event for
   a surface — so the three translated surfaces cannot drift. Anthropic gets
   `event: error`; Responses gets `response.failed`; OpenAI gets an in-stream
   `{"error": …}` and **no `[DONE]`** (`[DONE]` only after a real `finish_reason`).
   **Non-streaming** surfaces have sent nothing yet, so they return an honest
   **502** through the same envelope instead of a 200 with partial content.
4. **Collapse the two hand-built Anthropic synthesis blocks** (Ollama and MLX
   paths) into a call to that helper. Route the mid-stream-exception path through
   it too, instead of re-raising into a torn connection.
5. **Fix the MLX trace status** to `incomplete` when `not state.finished` — an
   **existing** status value; no new status, no schema change. `record_trace_mlx`
   already takes `status` and `error_message`, and `describe_exception` is already
   imported there for the exception path.

What is preserved: the requirement behind the synthesis — *every stream ends with
a terminal event*, added 2026-07-18 because Codex hung without one. Only which
event it is changes.

### Recording how much the client got — without polluting a measured column

`completion_tokens` is NULL on all 60 historical `incomplete` traces, because real
counts arrive on the `done` line that never came. The first draft proposed
recording herd's streamed count instead. **The audit says don't:** that count is
`output_token_count += max(1, len(text) // 4)` (`streaming.py:755`) — an
*estimate*. Writing it into `completion_tokens` would mix estimated and measured
values in one column, which is exactly what `0.11.0` added a separate
`estimated_tokens` column to avoid. Instead, put it in the **existing
`error_message`** — `"…ended without done:true after N content chunks (~M tokens
estimated)"` — human-readable, greppable, no schema change, and honest about being
an estimate.

### Verification

1. **Reuse the drop-simulation helpers that exist.** `tests/test_server/test_streaming_failures.py`
   has `_make_fake_stream(proxy, chunks)` and `_content_chunk(...)`, already used by
   `TestIncompleteStream` to feed a stream with no `done` line. Drive each surface's
   route through them: the result must be the error terminal (stream) or a 502
   (non-stream), never a success. One parametrized test over surfaces and modes —
   the same shape as `test_context_protection_every_format.py`, which exists because
   per-surface fixes kept missing a surface.
2. **Read existing tests before flipping any.** `test_mlx_proxy.py:286`, `:383` and
   the `[DONE]` fixtures around `:960`–`:1074` assert `end_turn` / `[DONE]`. Most are
   **normal completions that must keep passing**; only drop cases should change.
3. **Against the real clients, not just mocks.** Reproduce a drop deterministically
   (kill the runner mid-stream) and confirm **Claude Code** and **Codex** end the turn
   as a failure — not hang, not continue on truncated output. This is the step that
   guards against reintroducing the 2026-07-18 Codex hang.
4. Confirm an induced drop on the MLX path is now traced `incomplete`.

Client-visible behaviour changes, so it is a `Changed` CHANGELOG entry.

### Frequency, for proportion

60 `incomplete` traces all time — 54 on the correct `ollama` surface, 5 on
`responses` (latest 2026-07-18), 1 on `anthropic` — and **none on a translated
surface in the last 60 days**. Note that MLX-path drops would not appear in that
count at all, because they were traced `completed`; the true number is unknown,
which is itself an argument for step 5. Medium severity, low observed frequency.

---

## Phase 2 — Audit herd's translators for the bugs Ollama found in its own

**Narrowed by the audit: two of four are already handled by design and tested.**

herd translates Responses and Anthropic into `/api/chat` itself, so Ollama's fixes
to its own compatibility layers don't reach herd. Each candidate, against the code:

| Ollama | bug | herd today | action |
|---|---|---|---|
| `0.40.7` | switching thinking ↔ non-thinking models mid-conversation | prior-turn `reasoning` items are **dropped** (`responses_translator.py:395`), Anthropic thinking blocks too — tested by `test_reasoning_items_are_dropped` and `test_thinking_blocks_dropped` | **none** — the bug class can't occur when history carries no reasoning |
| `0.40.5` | continuing a Responses conversation with custom tool calls | explicit `custom_tool_call` / `custom_tool_call_output` handling (`:363`–`:394`), tested in `test_responses_translator.py` | add **one multi-turn round-trip test** if none exists; fix only if it fails |
| `0.40.7` | another provider's compaction items in history | unknown item types are **dropped with a once-per-type warning** (`_log_unknown_item_type_once`, `:274`) — but **no test pins it** | add a test pinning drop-and-warn. Then decide deliberately whether *dropping* compacted context is acceptable, since it silently loses the summary — that is a semantic question, not a crash |
| `0.40.7` | streamed tool-call events before open reasoning items (Codex web search) | `ResponsesSSEState` ordering; `sequence_number` tests exist | **the one real investigation**: read Ollama's diff, reproduce against herd's state machine |

Method for the remaining items is unchanged: read Ollama's actual diff for the
failure's shape, write a failing test, fix only if it fails, and record the ones
that don't apply so nobody re-asks.

---

## Phase 3 — Upgrade this box's Ollama, deliberately, as an experiment

**This box is on `0.40.2`.** Every fix in the research landed in `0.40.4`–`0.40.9`.

**Timing: after the `0.11.3` day-after check (2026-10-23)**, so an Ollama change
doesn't confound the release's read.

**It is also the test of a hypothesis.** `0.40.2` is the upgrade that rewrote both
residents' digests and doubled them (114 GB, then 156 GB). `0.40.4`'s notes mention
*"reusing verified manifests and already loaded models."* The next upgrade is the
only way to know whether that fixed it.

**No new tooling — herd already reports the snapshot.** `/fleet/status` carries the
Ollama version and, per resident, `context_length`, `runner` and `digest` (verified
2026-10-22: `0.40.2`; gpt-oss 131072 / gemma3 32768 / glm 32768, all `llamacpp`).
And the `duplicate_runners` check *is* the after-test.

- **Before:** `curl -s localhost:11435/fleet/status > before.json`, plus the one
  thing herd doesn't report — the launch args
  (`ps -Ao args | grep llama-server | grep -oE '\-c [0-9]+ \-np [0-9]+'`).
- **Do:** the manual Mac-app swap from CLAUDE.md (`brew upgrade ollama` does nothing),
  watching the `:11434` launch race.
- **After:** the same two captures, diffed; `duplicate_runners` on
  `/dashboard/api/health` (**the hypothesis**); launch args and the `num_ctx` pin
  intact; `OLLAMA_SERIAL_FAMILIES` (`server/serializers.py`) re-checked against
  `sched.go`, as CLAUDE.md requires every upgrade. Soak; record the result in
  `docs/observations.md` either way.

**Keep `duplicate_runners` regardless of outcome** — other operators upgrade from
older versions on their own schedule.

---

## Phase 4 — Bump the mlx-lm pin `0.32.0` → `0.32.2`

herd pins mlx-lm exactly, so users get `0.32.1`/`0.32.2`'s fixes — `max_tokens: 0`
handling, and the `tool_calls` arguments parsing fix (`mlx-lm#1904`, the same
string-vs-object class herd fixed on the Ollama side in `0.11.0`,
[#5](https://github.com/geeks-accelerator/ollama-herd/pull/5)) — only when the pin moves.

**The audit found a trap: "0.32.0" means two different things in the code.**

| meaning | where | on the bump |
|---|---|---|
| **the pin** — what's installed and tested | `scripts/setup-mlx.sh: PINNED_VERSION`, `docs/guides/mlx-setup.md:22` | **change** |
| **the capability floor** — first release with native `--kv-bits` | `mlx_supervisor.py:443`, `:514`, `:747`–`:759`; `health_engine.py:2202`; `mlx-setup.md:32`, `:171` | **leave** |

A find-and-replace would wrongly raise the floor and tell users on `0.32.0` that
their working install is too old. Change the two pin sites only. Worth noting the
supervisor detects the floor by **probing** for `--kv-bits`, not by comparing
version strings — keep that; it is why the floor survives a pin bump untouched.

**Soak it** — this subsystem has broken on mlx-lm upgrades before. On both servers:
`--kv-bits` / `--quantized-kv-start 0` accepted; the compactor's `--draft-model` still
starts and holds ~94 tok/s; GLM's `reasoning` field still surfaces; one MLX
tool-calling round trip through `/v1/messages`.

**Measure mlx `0.32.4`'s claimed attention-memory reduction** (M3 Ultra is pre-M5)
with the **existing** `process_memory` children footprint, before and after — the
heartbeat already labels the two `mlx` children. Not RSS.

**Run in a different window from Phase 3**, so a regression can be attributed.

---

## Phase 5 — Windows: tell operators which Ollama they need

Ollama `0.40.6` fixed runners orphaned on Windows, arriving just as `herd-node`
became able to start there (`0.11.3`, [#8](https://github.com/geeks-accelerator/ollama-herd/issues/8)).

**Docs only.** `docs/troubleshooting.md` has no Windows section — only a PowerShell
`netstat` line under "Connection refused." Add a short **Windows** section there
(Ollama `>= 0.40.6`, the drain-on-stop behaviour from `_install_drain_handlers`, and
that the platform is not CI-tested), and a matching line in the README's install
notes.

**No health check.** The heartbeat already carries Ollama's version
(`OllamaMetrics.version`) but not the node's OS — `device_info` computes `os` only
for platform registration. A check would put OS on the heartbeat for one platform
floor affecting one known user. Revisit only if OS reaches the heartbeat for
another reason.

---

## Phase 6 — Text-to-speech routing (scope only; it gets its own plan)

Ollama `0.40.9`: OpenAI-compatible `/v1/audio/speech`, a `phonon` model with one
voice, streamed `wav`/`pcm` at 24 kHz mono, up to 4,096 characters, `400` on
unsupported options, and a **`speech` capability** in `/api/show`.

**What already exists to build it from:**

- **Candidate selection:** `serializers.model_has_capability(node, model, "speech")`
  and `routes/routing.py::get_fleet_capabilities(registry)` — capability is reported
  by each node's Ollama and the router reads it. (`model_has_capability` is
  presence-only; that is fine here because `phonon` is in Ollama's own metadata —
  the gotcha it carries is for native-server models, which are absent from it.)
- **Tracing:** `routes/routing.py::direct_proxy_tracer` — the shared closure for
  routes that pick a node and POST directly instead of going through
  `StreamingProxy`. It was extracted in `0.11.3` precisely because each direct-proxy
  route had been carrying its own copy, and the vision path never got one.
- **Error envelope:** the Phase 1 helper, so TTS failures match every other surface.

**The trap: do not copy `transcription_compat.py`'s opening guard.** It begins
`if not settings.transcription: return 503`, and `ServerSettings` carries three such
per-surface switches — `image_generation`, `transcription`, `vision_embedding`.
TTS must not add a fourth. A node that reports `speech` gets TTS traffic; one that
doesn't, doesn't. Capability *is* the switch.

**Open before committing:** the notes say "on Apple Silicon," the docs list Linux and
Windows playback tools — measure on our nodes. And whether a speech request
occupies an Ollama scheduler slot, which decides if it needs the queue
(`QueueManager`) or can be direct-proxied like embeddings.

**Stays off ollamaherd.com until herd routes it and it has been verified.**

---

## Watch only — checked, no action now

| item | finding |
|---|---|
| **Ollama native multimodal embeddings** (`0.40.4`) | herd intercepts an embedding request only on an exact name match with its own vision models, so Ollama's native ones pass through. Strategically, Ollama serving image embeddings natively weakens the case for herd's vision server — where the Pillow advisories live. **Not now:** that server carries **329 real requests in 6 h** (`dinov2-vit-s14`, visible only since `0.11.3`'s tracing). Retiring it needs Ollama to serve those models, measured. |
| **fastembed #829** (`pillow<13`) | Still open, no maintainer response. The JPEG/PNG/WebP format gate remains the user-side mitigation. Re-check each release. |
| **mflux `0.21`–`0.23`** | `mflux-generate` and `mflux-generate-z-image-turbo` still exist. Z-Image now runs bfloat16: ~30% faster on M5, but the same seed gives slightly different images (`--float32` restores them). One line in the image-generation guide. |
| **mlx `0.32.4`** | Adds Python 3.15 support. Not relevant to #8's reporter — Windows, where MLX doesn't run. |

---

## Adjacent debt found by the audit — not in this plan's scope

Recorded so it isn't rediscovered, and so this plan doesn't add to it:

- **Three per-surface on/off flags** — `image_generation`, `transcription`,
  `vision_embedding` in `ServerSettings`. They predate the greenfield principle,
  which says "no `FLEET_*` on/off flags." Worth a separate decision on whether each
  is an operator-consent opt-in (the only allowed kind) or removable. Phase 6 must
  not add a fourth.
- **~16 inline copies of the Anthropic error envelope** in
  `routes/anthropic_compat.py`. Phase 1's new terminal events go through one helper;
  migrating the existing literals onto it is a mechanical follow-up commit, kept
  separate so Phase 1's diff stays about behaviour.

---

## What the audit changed

| phase | first draft said | the code says |
|---|---|---|
| 1 | four streaming sites send a false success | **eight** sites: non-streaming OpenAI, Anthropic and Responses do it too, as HTTP 200 |
| 1 | the trace correctly says `incomplete` | not on the Anthropic MLX path — it is traced **`completed`** |
| 1 | fix four terminal events | the root cause is one missing value (`""` read as "stop"); extend the **existing** per-surface mappers and the **existing** envelope builder |
| 1 | record streamed tokens on `incomplete` | that count is a `len // 4` **estimate**; it belongs in `error_message`, not the measured `completion_tokens` |
| 1 | — | the "no done" warn-and-trace block is already **duplicated**; extract it |
| 1 | — | mid-stream exceptions **re-raise into a torn stream**; fix through the same path |
| 2 | four hypotheses | two already handled by design and tested; one handled but **untested**; one real investigation |
| 3 | record `/api/ps` and launch args by hand | `/fleet/status` already carries version, context, runner and digest; `duplicate_runners` is the after-test |
| 4 | bump the pin | "0.32.0" is **both** the pin and a capability floor; change only the pin's two sites |
| 5 | add a Windows section | `troubleshooting.md` has none to extend — one is needed |
| 6 | route by `speech` capability | **don't copy `transcription_compat`'s flag**; reuse `model_has_capability`, `get_fleet_capabilities`, `direct_proxy_tracer` |

The research findings that changed on first contact, before the audit:

- **"herd hasn't been checked for silent stream endings"** → herd *detects* them,
  then reports them to the client as success. Became Phase 1.
- **"mlx-lm jumped to 0.32 after six months on 0.31"** → herd already pins `0.32.0`;
  the real question is a pin bump. Became Phase 4.

---

## Order and rationale

1. **Phase 1** — a confirmed correctness bug on the Claude Code and Codex surfaces,
   now known to cover non-streaming clients and to mis-trace MLX drops.
2. **Phase 3** — gated on 2026-10-23; no new code, and it tests a real hypothesis.
3. **Phase 2** — two test additions and one investigation.
4. **Phase 4** — its own soak window, separate from Phase 3's.
5. **Phase 5** — documentation, any time.
6. **Phase 6** — separate plan, separate decision.
