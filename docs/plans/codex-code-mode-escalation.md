# Codex code-mode: escalation and the JSON-vs-JavaScript gap

**Status:** implemented 2026-07-18 — verification pending a herd restart
**Date:** 2026-07-18
**Depends on:** [`codex-responses-api-support.md`](codex-responses-api-support.md)

## Problem

Under Codex's default approval policy (`on-request`), a sandboxed command that
needs network or write access should raise an approval prompt. Against Herd it
does not — the command fails sandboxed, the model confabulates a "sandbox
security policy" to explain it, and the only workaround is switching the
ChatGPT Desktop app to **Full Access**.

## Root cause

Escalation in Codex is **model-initiated through tool arguments**. There is no
approval item type in the Responses API — the only approval members of the
32-member `ResponseInputItemParam` union are `mcp_approval_request` /
`mcp_approval_response`, which concern remote MCP, not local shell. Codex's own
model-facing prompt is explicit:

> ALWAYS proceed to use the `sandbox_permissions` and `justification`
> parameters — do not message the user before requesting approval.

So the model must emit `sandbox_permissions: "require_escalated"` itself. It
does. The problem is the shape it emits it in, and that is our doing.

### The two tool surfaces are not equivalent

| Slug family | `exec` arrives as | Escalation expressed as |
|---|---|---|
| non-Lite (`gpt-5-codex`, …) | plain function, JSON-schema parameters | a structured field — trivially emitted |
| Lite (`sol`/`terra`/`luna`, **the Desktop default**) | `custom` tool, code-mode | a field inside **generated JavaScript** |

Measured against `qwen3-coder:30b` on the real captured schemas:

- **non-Lite:** sets `sandbox_permissions: "require_escalated"` *unprompted*,
  3/3. (Test 1 omitted `justification`, which the schema calls the
  "user-facing approval question" — see Risks.)
- **Lite:** emits
  `{"cmd": "git pull", "sandbox_permissions": "require_escalated"}`.

That Lite payload is the bug. The `exec` description says verbatim:

> Accepts raw JavaScript source text, **not JSON**, quoted strings, or markdown
> code fences.

and the Lark grammar constrains nothing that would catch it:

```lark
SOURCE: /[\s\S]+/
```

`{"cmd": "git pull", …}` is a `SyntaxError: Unexpected token ':'` as a JS
program. `tools.exec_command` is therefore never called, the escalation never
becomes a structured request, and Codex has nothing to render a prompt from.
Full Access "fixes" it only because it removes the need for the prompt.

### Why this is ours to fix

`_tools_to_openai` bridges the `custom` tool to a function with one string
parameter (`CUSTOM_TOOL_ARG = "input"`). Framed as a function taking a string,
a JSON-shaped argument is the natural thing for a model to produce — we steer
it into the one form the tool explicitly rejects. The current appended hint
says "plain text only, no JSON" but never shows the **call shape**, so the
model knows what not to do and not what to do.

**Scope honesty:** this is intermittent, not universal. Commands *did* execute
in a live Desktop session, so the model emits valid JS some of the time. This
raises the success rate and makes escalation reachable; it is not a fix for a
total outage.

## Plan

### 1. Teach the bridge the call shape

In `_tools_to_openai` (`responses_translator.py`), replace the negative-only
hint with a concrete exemplar for grammar-backed custom tools:

```
Provide raw JavaScript source as the `input` argument, e.g.
  await tools.exec_command({cmd: "git pull"})
To run a command that the sandbox would block, add
  sandbox_permissions: "require_escalated" and a one-sentence justification.
Do NOT emit JSON, quoted strings, or markdown fences.
```

This is an **edit to existing code**, not new machinery — `_tools_to_openai`
already appends a grammar hint (`desc += …`) for grammar-backed custom tools.
The current text says *"plain text only, no JSON"*: it tells the model what not
to do and never shows the call shape. Fix the sibling parameter description in
the same place — it currently reads *"Raw source text for this tool."*, which
is the string sitting closest to the argument the model is filling in, and it
does nothing to discourage JSON.

### 2. Repair JSON payloads in `_unwrap_custom_input`

Steering is probabilistic; this is not. When the unwrapped text parses as a
JSON object carrying a `cmd` key, transpile it to the equivalent call:

```
{"cmd": "git pull", "sandbox_permissions": "require_escalated"}
  → await tools.exec_command({"cmd": "git pull", "sandbox_permissions": "require_escalated"})
```

Rules:

- Only fires when the payload is a JSON **object** containing `cmd` — anything
  else returns unchanged. Valid JavaScript never round-trips through this path.
- **Pass `cmd` through verbatim. No shell-splitting.** The captured schema is
  `cmd: {"type": "string", "description": "Shell command to execute."}` — a
  string, not `string[]`. (`prefix_rule` is the array.) An earlier draft of
  this plan called for `shlex.split`; that was wrong, and dropping it removes
  a stdlib import used nowhere else in the repo plus the Windows-splitting
  risk.
- Preserve every sibling key verbatim (`sandbox_permissions`, `justification`,
  `prefix_rule`, `timeout_ms`, …) — do not allow-list; unknown keys are the
  tool's business, not ours.
- Re-serialize with `json.dumps` so the object literal is valid JS.
- **Order it before the existing `len(parsed) == 1` fallback** — see the
  latent bug below.
- Log via the existing `_log_unknown_item_type_once` pattern (once per process,
  keyed by tool name) at WARNING. An earlier draft said DEBUG; that is both
  invisible in practice and inconsistent with `_normalize_tool_args`, which
  warns on exactly this class of model misbehaviour.

This is the same posture as the rest of the shim, and `_normalize_tool_args` is
the precedent to mirror — *"a non-JSON string is wrapped rather than dropped, so
a misbehaving model degrades instead of breaking the stream."*

### 2a. Latent bug this uncovers

`_unwrap_custom_input`'s existing `len(parsed) == 1` fallback already mangles
the single-key case, today, silently:

```
{"cmd": "git pull"}                                    → 'git pull'      ← bare string, not JS
{"cmd": "git pull", "sandbox_permissions": "…"}        → unchanged JSON  ← the live failure
await tools.exec_command({cmd: "git pull"})            → unchanged       ← correct
```

The one-key form degrades into a bare command string that is *also* not valid
JavaScript, but looks plausible enough to pass unnoticed. That fallback exists
to catch a model renaming our bridge parameter; it will unwrap any single-key
object. Narrowing it to "single key **and** that key is not a known `exec_command`
field" is part of this change, not a follow-up.

### 3. Tests

In `tests/test_server/test_responses_translator.py`:

- JSON-with-`cmd` → `await tools.exec_command({...})`, escalation keys survive.
- **Single-key `{"cmd": "…"}` repairs rather than degrading to a bare string**
  (the latent bug in 2a — this is the regression test that pins it).
- Valid JavaScript passes through byte-identical (the critical regression).
- `{"input": "..."}` bridge-unwrap behaviour is unchanged.
- JSON object *without* `cmd` passes through unchanged.
- Unknown sibling keys are preserved.

Both call sites — non-streaming (`:489`) and streaming (`:695`) — go through
`_unwrap_custom_input`, so the repair lands once and covers both paths. No
duplication, no per-path branching.

**Implementation note — the first cut was wrong, and the tests caught it.**
The plan above assumes the JSON payload arrives *as* the argument object, but
the dominant live shape is JSON nested **inside** the bridge parameter:
`{"input": "{\"cmd\": …}"}`. That hits the `CUSTOM_TOOL_ARG in parsed` early
return and never reached the repair — so the version matching this plan fixed
only the rarer case. Repair now runs on the final text whichever path produced
it (`_repair_if_json_exec`), which is also why it is a separate helper rather
than an inline branch.

Verified in a real V8 async module: the payload the model actually sent raises
`SyntaxError: Unexpected token ':'`, while the repaired form parses and calls
`exec_command` with `sandbox_permissions` intact.

### 4. Corrections to land in the same pass

- **`d13c18d`'s commit message and any doc text claiming unrecognised input
  item types are the likely approval mechanism is wrong.** Correct it in
  [`codex-responses-api-support.md`](codex-responses-api-support.md): escalation
  is model-initiated via tool arguments; no approval item type exists.
- `docs/guides/codex-integration.md` — replace the Full Access workaround with
  the real mechanism, and note that approval prompts depend on the model
  emitting `sandbox_permissions` itself.

## Verification (needs a herd restart)

1. Restart herd **and** herd-node.
2. Desktop app, approvals set to **On request**, fresh chat (a poisoned
   conversation re-derives its own refusals — see the integration guide).
3. Ask for something the sandbox blocks (`git pull`).
4. Expect an approval prompt rather than a sandboxed failure.
5. `grep 'unrecognised input item type' ~/.fleet-manager/logs/herd.jsonl` —
   this is the **first build that can emit that line** (herd started 01:15:47,
   the warning landed at 01:23:49), so it is also the first real read on
   whether Codex sends item types we drop.

## Risks and open questions

- **Missing `justification`.** Non-Lite test 1 emitted `require_escalated` with
  no justification, which the schema calls the user-facing approval question.
  If Codex needs it to render a prompt, escalation still fails silently. Worth
  measuring before considering synthesizing one — fabricating a user-facing
  justification the model didn't write is a bad trade.
- **Codex-side failure is possible independent of us.** openai/codex#21982
  reports a correctly-emitted `require_escalated` failing to surface through
  app-server. If step 4 still shows no prompt with a well-formed call in the
  log, the remaining fault is upstream and should be reported as such.
- **Item-type coverage is unproven.** Captures show only `message` and
  `additional_tools`, but all are short/first-turn — a weak sample. The union
  contains first-class `shell_call` / `apply_patch_call` surfaces we do not
  handle. Step 5 is what settles it; do not treat the current silence as
  coverage.
- ~~`shlex.split` on a Windows-style command would mangle it.~~ **Resolved by
  the audit** — `cmd` is a string in the real schema, so nothing is split and
  the cross-platform risk never arises.
