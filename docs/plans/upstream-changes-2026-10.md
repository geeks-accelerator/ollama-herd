# Upstream changes, October 2026: what herd should do about them

**Status**: 📋 **PROPOSED (2026-10-22).** Nothing implemented yet. Phase 1 is a
confirmed bug; the rest are ordered by evidence, not by how interesting they are.

**Source.** A research pass (another session) read the release notes for everything
herd depends on — Ollama `0.40.4`–`0.40.9`, mlx-lm `0.32.0`–`0.32.2`, mlx `0.32.4`,
mflux `0.21`–`0.23`, fastembed — and checked each against how herd's released code
calls it. This plan verified that research against the repo and the live trace
store on 2026-10-22 before writing anything down. Two of its findings changed on
contact, recorded at the end.

> **The headline is not an upstream change at all.** The research flagged that
> Ollama `0.40.9` fixed *"streams ending silently after partial output instead of
> reporting the failure,"* and noted herd hadn't been checked for it. Checking
> found herd does something worse than silence: on every translated surface, it
> **tells the client a truncated response finished normally.**

---

## Phase 1 — Stop reporting a truncated stream as a successful one

**Confirmed. Do this first.** It is the one item here that is a bug in herd today,
and it lands on the two surfaces herd is marketed for: Claude Code and Codex.

### What happens now

When Ollama's NDJSON stream ends without a `done:true` line, herd's proxy detects
it correctly — the trace is recorded `incomplete` with a WARNING
(`streaming.py:351`). That detection is not the problem. **What the client is sent
next is:**

| surface | what herd emits after an upstream drop | where |
|---|---|---|
| OpenAI `/v1/chat/completions` | `data: [DONE]` — **unconditionally**, after the line loop, with no `finish_reason` chunk before it | `streaming.py:790` |
| Responses `/v1/responses` | a synthesized `response.completed` with `done_reason: "stop"` | `routes/responses_compat.py:284` |
| Anthropic `/v1/messages` (Ollama path) | a synthesized `message_delta` with `stop_reason: "end_turn"`, then `message_stop` | `routes/anthropic_compat.py:~1150` |
| Anthropic `/v1/messages` (MLX path) | the same `end_turn` + `message_stop` | `routes/anthropic_compat.py:595` |
| Ollama-native `/api/chat` | raw NDJSON passthrough; the stream simply ends without `done:true` | — |

The Ollama-native surface is **correct**: that is Ollama's own protocol, and a
native client is expected to check for `done`. Every *translated* surface converts
a failure into a success. An agentic client — Claude Code, Codex — then acts on a
half-written answer, a truncated file, or a tool call cut off mid-argument as if it
were complete, and has no signal to retry.

### Why it was built this way, and why the intent was right

The Responses synthesis carries its reason in a comment: *"so Codex doesn't hang
waiting for `response.completed`."* That was a real failure (2026-07-18), and the
requirement it encodes is correct — **every stream must end with a terminal event.**
The mistake is the choice of terminal event, not the decision to send one. The fix
keeps the guarantee and changes what it says.

### How often it happens

| | count | latest |
|---|---|---|
| `incomplete` traces, all time | **60** | 2026-10-11 |
| … on `ollama` format (correct behaviour) | 54 | 2026-10-11 |
| … on `responses` | 5 | 2026-07-18 |
| … on `anthropic` | 1 | 2026-04-22 |
| … on any translated surface, last 60 days | **0** | — |

So it is **rare on this fleet**, and the translated-surface cases predate several
reliability fixes. Severity is set by the consequence, not the rate: when it does
happen, a coding agent is silently handed truncated work. Medium severity, low
frequency — worth fixing properly, not urgently.

### A gap the measurement exposed

`completion_tokens` is **NULL on every one of the 60** incomplete traces, because
token counts come from the `done` line that never arrived. So the trace store
cannot say whether the client received partial output before the drop — which is
the exact question that decides how bad a given incident was. herd already counts
streamed output per chunk (`output_token_count` in `streaming.py`); recording that
on the `incomplete` path closes it. Same family as the 2026-10-09 lesson: a layer
that could be told something it isn't recording.

### The fix

One terminal event per surface, each a real error rather than a success:

| surface | emit instead | client effect |
|---|---|---|
| Anthropic | `event: error` with `{"type":"error","error":{"type":"api_error","message":…}}` | Claude Code treats it as a failed request and can retry |
| Responses | `response.failed` with an `error` object | Codex ends the turn as failed rather than hanging *or* trusting it |
| OpenAI | an in-stream `data: {"error": {…}}` and **no** `[DONE]` | `[DONE]` is only sent after a real `finish_reason` |

Keep `responses_translator`'s existing `length → "incomplete"` mapping; that path
is a real model stop and is already right.

**Reuse before build.** The error text should come from one place — the
`describe_exception` pattern in `common/errors.py` exists precisely so a failure
becomes one categorizable message — and the three surfaces should share a single
"upstream ended without `done`" definition rather than three inline strings.

### Verification — this is the part that can't be skipped

1. **Unit tests per surface**: a mocked upstream that yields content chunks then
   ends with no `done` line must produce the error terminal, never a success one.
   Some existing tests assert `end_turn` / `[DONE]` (`test_mlx_proxy.py:286`,
   `:383`, and the `[DONE]` fixtures around `:960`–`:1074`). **Read each before
   changing it**: most are *normal* completions that must keep passing; only the
   ones exercising a drop should flip.
2. **Against the real clients, not just mocks.** The Responses synthesis exists
   because Codex hung; the new terminal must be proven not to reintroduce that.
   Reproduce a drop deterministically (kill the runner mid-stream, or a fault
   proxy in front of `:11434`) and observe **Claude Code** and **Codex** end the
   turn as a failure — not hang, not continue on truncated output.
3. **Record partial tokens**, then confirm an induced drop leaves a trace with
   `status=incomplete` and a non-null `completion_tokens`.

It changes client-visible behaviour, so it is a `Changed` entry in the CHANGELOG,
not only `Fixed`.

---

## Phase 2 — Audit herd's translators for the bugs Ollama found in its own

**Unverified. Each item is a hypothesis until a failing test exists.**

herd translates Responses and Anthropic into `/api/chat` itself, so Ollama's fixes
to *its* compatibility layers do not reach herd. But these bugs were found by
real Codex and Claude Code traffic against a translation layer much like herd's,
which makes them good candidates:

| Ollama release | bug | herd's equivalent to test |
|---|---|---|
| `0.40.5` | continuing a Responses conversation that contains custom tool calls | `responses_translator` input conversion, custom tools |
| `0.40.7` | switching between thinking and non-thinking models mid-conversation | thinking blocks in history sent to a non-thinking model, and back |
| `0.40.7` | history containing another provider's compaction items | unknown item types in Responses `input` |
| `0.40.7` | streamed tool-call events arriving before open reasoning items — misordered or duplicate tool calls with Codex web search | `ResponsesSSEState` event ordering |

**Method, per item:** read Ollama's actual diff to learn the failure's shape, write
a test that reproduces it against herd's translator, and fix only if it fails.
Expect some not to apply — herd's translator is not Ollama's — and record the ones
that don't, so nobody re-asks. A test that passes on first write is still worth
keeping: it pins behaviour Ollama found worth fixing.

---

## Phase 3 — Upgrade this box's Ollama, deliberately, as an experiment

**This box is on `0.40.2`.** Every fix in the research — the stream reporting,
the Responses fixes, the manifest change — landed in `0.40.4`–`0.40.9`, so none
of them are here.

**Timing: after the `0.11.3` day-after check (2026-10-23).** An Ollama change on
the day the release is being judged would confound the read; the check needs a
stable baseline.

**The upgrade is also the test of a hypothesis.** `0.40.2` is the upgrade that
rewrote both resident models' manifest digests and doubled them (114 GB, then
156 GB) — the incident `duplicate_runners` exists for. `0.40.4`'s notes mention
*"reusing verified manifests and already loaded models."* That may fix it. The only
way to know is to watch the next upgrade.

**Before:** record `/api/ps` names + digests + `context_length`, the launch args
(`ps -Ao args | grep llama-server | grep -oE '\-c [0-9]+ \-np [0-9]+'`), and the
version.

**Do:** the manual Mac-app swap from CLAUDE.md (`brew upgrade ollama` does nothing),
watching the launch race for `:11434`.

**After:**

- `/api/ps` for duplicate names — **the result that answers the hypothesis**
- launch args unchanged, and the `num_ctx` pin holding both models at their
  configured per-slot contexts
- `OLLAMA_SERIAL_FAMILIES` re-checked against `sched.go`, as CLAUDE.md requires on
  every upgrade
- the `RUNNER` column still `llamacpp` for both GGUF residents
- soak, and write the result to `docs/observations.md` either way

**Keep `duplicate_runners` regardless of the outcome.** Other operators upgrade
from older versions on their own schedule, and a check that costs nothing when
silent is worth more than the code it would remove.

---

## Phase 4 — Bump the mlx-lm pin `0.32.0` → `0.32.2`

**The research missed that herd pins mlx-lm exactly** —
`scripts/setup-mlx.sh: PINNED_VERSION="0.32.0"`. So herd is already on 0.32; what is
new is `0.32.1` and `0.32.2`, and **herd users get neither until the pin moves.**

**What it brings:** correct handling of `max_tokens: 0`, and a fix to parsing
`tool_calls` arguments (`mlx-lm#1904`) — the same string-versus-object problem herd
fixed on the Ollama side in `0.11.0` ([#5](https://github.com/geeks-accelerator/ollama-herd/pull/5)).

**Soak it; CLAUDE.md is explicit that this subsystem has broken on mlx-lm upgrades
before.** Verify on both MLX servers:

- `--kv-bits` and `--quantized-kv-start 0` still accepted
- the compactor's `--draft-model` speculative decoding still starts and holds ~94 tok/s
- GLM-4.7-Flash's `reasoning` field still surfaces, not dropped
- an MLX tool-calling round trip through `/v1/messages`

**And measure mlx `0.32.4`'s claimed attention-memory reduction on pre-M5 chips**
(this box is an M3 Ultra) with `process_memory`'s children footprint before and
after — not RSS. A measured number is worth recording whichever way it goes.

---

## Phase 5 — Windows: tell operators which Ollama they need

Ollama `0.40.6` fixed runners being orphaned on Windows. That arrived exactly as
`herd-node` became able to start on Windows at all (`0.11.3`, [#8](https://github.com/geeks-accelerator/ollama-herd/issues/8)).

**Do: document it.** A Windows section in `docs/troubleshooting.md` and the README's
install notes: Ollama `>= 0.40.6` on Windows nodes, with the reason.

**Don't build a check yet.** The heartbeat already carries Ollama's version
(`OllamaMetrics.version`), but not the node's OS — `device_info` computes `os` only
for platform registration. A check would mean putting OS on the heartbeat for one
platform floor that one known user is affected by. Revisit if a Windows user
reports orphaned runners, or if OS reaches the heartbeat for another reason.

---

## Phase 6 — Text-to-speech routing (scope only; it gets its own plan)

Ollama `0.40.9` ships TTS: an OpenAI-compatible `/v1/audio/speech`, a `phonon`
model with one voice, streamed `wav`/`pcm` at 24 kHz mono, up to 4,096 characters,
`400` on unsupported options, and a **`speech` capability** reported by `/api/show`.

That last detail is what makes it cheap for herd: capability detection already
works this way (`model_has_capability`, `get_fleet_capabilities` — the node's Ollama
reports capabilities and the router reads them). TTS routing is therefore mostly
*routing*, not a backend: send `/v1/audio/speech` to a node whose Ollama reports
`speech`, with the same scoring and tracing as every other surface.

**Open question before committing:** the release notes say "on Apple Silicon," the
docs list Linux and Windows playback tools. Measure on our own nodes rather than
resolving it from text.

**Stays off ollamaherd.com until herd actually routes it and it has been
verified.** Write a dedicated plan when this is picked up.

---

## Watch only — checked, no action now

| item | finding |
|---|---|
| **Ollama native multimodal embeddings** (`0.40.4`) | herd intercepts an embedding request only when the model name exactly matches one of its own vision models, so Ollama's native multimodal models pass straight through — herd does not shadow the feature. **Strategically it matters more than it looks:** if Ollama serves image embeddings natively, the case for herd's own vision server weakens, and that server is where the Pillow advisories live. **But not now** — vision-embedding tracing, new in `0.11.3`, shows **329 real requests in 6 hours** on `dinov2-vit-s14`, a workload that was invisible until this release. Retiring the server would need Ollama to serve those models, measured. |
| **fastembed #829** (the `pillow<13` cap) | Still open, no maintainer response, `main` still pins it. PyPI `[embedding]` installs keep resolving Pillow 12.3; the JPEG/PNG/WebP format gate remains the user-side mitigation. Re-check at each release. |
| **mflux `0.21`–`0.23`** | Both commands herd shells out to — `mflux-generate`, `mflux-generate-z-image-turbo` — still exist. One behaviour change worth a line in the image-generation guide: Z-Image now runs in bfloat16, ~30% faster on M5, but **the same seed produces slightly different images**; `--float32` restores the old output. Matters only to anyone relying on seed reproducibility. |
| **mlx `0.32.4`** | Adds Python 3.15 support. Not relevant to the #8 reporter, who is on Windows, where MLX does not run. |

---

## Where the research changed on contact

Recorded so the next research pass knows which kinds of claims need a repo check:

- **"herd hasn't been checked for silent stream endings"** → checked: herd *detects*
  them server-side, but reports them to the client as **success** on every translated
  surface. Worse than silence, and now Phase 1.
- **"mlx-lm jumped to 0.32 after six months on 0.31"** → true upstream, but herd
  already pins `0.32.0` exactly, so the real question is a pin bump to `0.32.2`,
  which needs a soak (Phase 4).

Both were reasonable readings of release notes. Neither survived a `grep` of the
code that actually calls the dependency — which is the method the research itself
used, applied one level deeper.

---

## Order and rationale

1. **Phase 1** — a confirmed correctness bug on the Claude Code and Codex surfaces.
2. **Phase 3** — gated on 2026-10-23; cheap, and it tests a real hypothesis.
3. **Phase 2** — test-first audit; each item may evaporate.
4. **Phase 4** — needs its own soak window; don't overlap it with Phase 3's.
5. **Phase 5** — a documentation change, any time.
6. **Phase 6** — separate plan, separate decision.

Phases 3 and 4 both change a runtime under the fleet. **Run them in separate
windows** so a regression can be attributed to one of them.
