# Four failures herd cannot see, or cannot explain

**Status:** planned and audited 2026-11-17, after the 0.12.5 release. Nothing implemented. The implementation order is at the end, after the codebase audit.

Second telemetry pass after `model_not_found` shipped. Two of the four issues are
new; two are already designed and audited in
[`telemetry-errors-2026-11.md`](telemetry-errors-2026-11.md), and this plan only
fixes their place in the order. The MLX-runner `context_too_long` item from that plan
is **not** in this set: it is larger, needs a reproduction first, and does not block
any of these.

| # | Issue | Visible today? | Where designed |
|---|---|---|---|
| 1 | Legacy `/api/embeddings` answers 200 with an empty or wrongly-keyed vector | **No** (traced `completed`) | here |
| 2 | Traces record httpx's text, not the backend's reason | As `bad_request` / `other` only | `telemetry-errors-2026-11.md` item 1 |
| 3 | Auto-routing sends Claude Code and Codex to models without tools | As `bad_request` only | `telemetry-errors-2026-11.md` item 3 |
| 4 | Telemetry counts only `completed` and `failed` | **No** (2,678 requests in 30 days) | here |

Design rules, as in the earlier plans: no feature flags, no second code path, extend
the existing helper rather than add a parallel one, and ask of every new check what
it fires on when everything works.

---

## Issue 1: legacy `/api/embeddings`

### Evidence (reproduced 2026-11-17 on 0.12.5)

`ollama_compat.py` registers one handler on both paths:

```python
@router.post("/api/embed")
@router.post("/api/embeddings")
async def ollama_embed(request): ... return await dispatch_embed(request, body)
```

The two endpoints speak different shapes:

| | Request | Response |
|---|---|---|
| `/api/embed` (current) | `input`: string or list | `{"embeddings": [[...]]}` |
| `/api/embeddings` (legacy) | `prompt`: string | `{"embedding": [...]}` |

`dispatch_embed` forwards the body unchanged to Ollama's `/api/embed`, which ignores
`prompt`. Results through the router:

| Model | Backend | Legacy call through herd | Ollama directly |
|---|---|---|---|
| `bge-m3:latest` | Ollama | `{"embeddings": []}`, 200, trace `completed` | `{"embedding": [...]}` |
| `mxbai-embed-large:latest` | Ollama | `{"embeddings": []}`, 200, trace `completed` | `{"embedding": [...]}` |
| `nomic-embed-text` | native fastembed | vectors under `embeddings` (the native server already accepts `prompt`) | `{"embedding": [...]}` |

A legacy client gets nothing it can read, and every signal herd has calls it a
success. Clients still on the legacy call include older `ollama-python`
`embeddings()` and LangChain's legacy `OllamaEmbeddings`.

### Plan

**1a. A thin translation on the legacy path, exactly like `/v1/embeddings`.**
`openai_compat.openai_embeddings` is already the pattern: validate its own request
shape, build an `/api/embed` body, call the shared `dispatch_embed`, translate the
response. The legacy handler does the same, and `/api/embed` keeps its handler alone:

- Split the decorator: `/api/embed` stays on `ollama_embed`; `/api/embeddings` gets
  `ollama_embeddings_legacy`.
- Request: `prompt` must be a non-empty string (400 otherwise, Ollama's own
  wording). Build `{**body, "input": prompt}` minus `prompt`, so `options`,
  `keep_alive` and `metadata` keep working as they do today.
- Response: a 200 with `embeddings[0]` becomes `{"embedding": embeddings[0]}`.
  Anything else (4xx/5xx, the vision path's errors) passes through untouched, so
  error handling stays in one place.
- Headers: keep `dispatch_embed`'s `X-Fleet-*` headers on the rebuilt response.
- Vision models: the legacy call cannot carry an image, so `dispatch_embed`'s vision
  branch answers as it does now. No special case.

**1b. A wrong number of vectors is not a completed request.** This is what let the
bug hide. Wherever the embedding path records `completed`, it first checks that the
backend returned one vector per input. If not, the request is recorded **`failed`**
with `"backend returned N embedding(s) for M input(s)"` and answered 502.
`/v1/embeddings` already makes exactly this check, but *after* the trace was written
as `completed`, so it moves down into the shared path and `/v1/embeddings` drops its
copy. (Revised by the audit: this first said `no_output`, but the check that reads
`no_output` describes empty *generations*. See the audit below.)

What it fires on when everything works: nothing. A string input expects one vector
and a list expects one per item. An empty input list expects zero, so a legitimate
empty request still completes.

### Tests

- Legacy path, one test per backend: Ollama (mocked `/api/embed`), native text and
  vision. Response is `{"embedding": [...]}`, and the vector equals the one
  `/api/embed` returns.
- Missing, empty or non-string `prompt` → 400.
- Backend errors pass through unchanged on the legacy path.
- `/api/embed` and `/v1/embeddings` responses unchanged (the existing suite, plus one
  shape assertion each).
- A wrong number of vectors (including none) → 502 and a `failed` trace, on the
  Ollama and native backends; an empty input list stays a 200.

### Verification (live)

Through `:11435`, call `/api/embeddings` for `bge-m3`, `mxbai-embed-large` and
`nomic-embed-text`. Each must return `embedding` with a cosine similarity of 1.0
against Ollama's own legacy answer for the same prompt. The trace must be
`completed`, and `/api/embed` must still return `embeddings`.

---

## Issue 2: the backend's reason in traces

Designed and audited in `telemetry-errors-2026-11.md`, item 1 and the audit's
"Item 1" section. In short:

- Extend `describe_exception` with the backend's message (its 22 callers are all
  traces or logs), and make `categorize_error` read the status first.
- Fold the two hand-built `HTTP {status}:` exception sites onto it, one of them the
  `dispatch_embed` Ollama branch that issue 1 also touches.
- Add a `_request_backend_error` side channel (the `_request_partial` pattern), so a
  stream Ollama ends with an error line is traced with its reason, and the OpenAI,
  Anthropic and Responses surfaces send that reason instead of "retry".

**Decided (2026-11-17):** such a stream is traced **`failed`**. The backend said
why it stopped, and `incomplete` keeps meaning "it stopped without saying". The
client-message fix lands with it.

Do issue 1 first: both touch the embed exception site, and doing issue 1 first keeps
each diff small.

---

## Issue 3: tools-aware auto-routing

Designed and audited in `telemetry-errors-2026-11.md`, item 3 and the audit's
"Item 3" section:

- **3a.** `has_tools` in `anthropic_autoroute.resolve_model`, mirroring `has_images`,
  passed by `/v1/messages` and `/v1/responses`.
- **3b.** The `anthropic_no_chat_model` check passes the same `capabilities` and
  `has_tools=True` (today it calls `resolve_model` without capabilities, so it can
  disagree with the routes). One `_anthropic_model_map()` parser replaces the two
  hand-written ones. The "mapped target cannot take tools" rule goes inside
  `_check_mapped_models_hot`.
- **3c.** Generalise `trace_store.get_backend_not_found` to `get_backend_failures`,
  with the tools check reading Ollama's `does not support tools` text from traces.
  **3c depends on issue 2**: today's traces do not contain that text.

---

## Issue 4: telemetry outcomes beyond completed and failed

### Evidence (community telemetry, 30 days to 2026-11-17)

`anonymous_rollup` adds every trace to `requests` but counts only `completed`
(success) and `failed` (error). `rejected`, `incomplete`, `no_output`,
`client_disconnected` and `retried` fall in the gap:

| | Requests |
|---|---|
| Total | 325,601 |
| Completed | 320,393 |
| Failed | 2,530 |
| **Neither** | **2,678** |

On some installs and models, the gap is most of the traffic:

| Model | Neither completed nor failed | Notes |
|---|---|---|
| `granite4.2:8b` | 275 of 283 | median latency 423 s |
| `qwen3:0.6b` | 127 of 175 | |
| `qwen3.5:27b` | 66 of 83 | |
| `llama3.3:70b-frame` | 48 of 48 | no latency recorded, which reads as rejected |

`retried` inflates the gap harmlessly: one request that retried once is two rows. The
rest are requests the client experienced as failures, and the payload cannot say
which kind.

### Plan

**4a. `requests` counts client requests.** A `retried` row is one attempt, not a
request: exclude it, so `requests` is exactly the sum of terminal outcomes. This is
a meaning fix in the contract, so the telemetry page says so with the date.

**4b. Per-outcome counts on each model entry.** Four new bounded integers on
`ModelEntry`, defaulting to 0:

| Field | Trace status | Meaning to the client |
|---|---|---|
| `rejected_count` | `rejected` | no node could take it |
| `incomplete_count` | `incomplete` | the answer was cut off |
| `no_output_count` | `no_output` | the model generated nothing |
| `disconnected_count` | `client_disconnected` | the client gave up |

`error_count` keeps meaning `failed`, so the existing series stays comparable. The
invariant `success + error + the four ≤ requests` is checked server-side.

The error-category histogram is deliberately not touched. Categories describe why
something *failed*; mixing outcomes into it would change what every existing
category count means.

Privacy: counts only, in the same class as `success_count`, with no new identifiers
and no free text. `ALLOWED_ENTRY_KEYS` gains exactly these four names.

### Order across repositories (the payload is `extra="forbid"`)

A new client sending these fields to an old server gets a 422 for the **whole**
payload, so the service must ship first:

1. **Service** (`ollama-herd-private/telemetry`):
   - Accept the four fields on `ModelEntry` (default 0, bounded like `error_count`).
   - Add the invariant check.
   - Migration `0002`: add the columns, default 0.
   - Store the fields.
   - Update `site/telemetry.html`, which is the contract: list the four fields and
     the `requests` change, with the date.
   - Deploy. Then verify `https://ollamaherd.com/api/v1/openapi.json` lists the
     fields, an old-shape payload is still accepted, and a new-shape one is stored.
2. **Client** (this repo):
   - Update `anonymous_rollup`, `ALLOWED_ENTRY_KEYS` and the golden payload test.
   - Add a test that fails if any trace status is neither counted nor explicitly
     excluded, so a future status cannot fall into the gap again.
3. **Release gate:** before `uv publish`, check that the live openapi lists the four
   fields, added to the artifact-check script. A client published ahead of the
   service would silently lose a day of telemetry from every upgraded install.

Older clients keep working unchanged, since the fields default to 0. History is not
backfilled.

**Deploying the service is outward-facing, so it needs the maintainers' go-ahead.**
It is the one step in this plan that changes something other people's installs talk
to.

---

## Order and release

1. **Issue 1**: herd only, independent, the highest severity. 1a and 1b in one
   commit.
2. **Issue 2**: herd only. Unblocks 3c and makes `bad_request` / `other` readable.
3. **Issue 3**: 3a/3b, then 3c.
4. **Issue 4**: service first. It can run alongside 2 and 3, since the client half
   only lands after the service is live.

Issues 1 to 3 can ship as a patch on their own. If issue 4's service is deployed in
time, its client half rides the same release. Otherwise it follows, and nothing
waits on it.

### Soak focus

The local fleet's traffic is Ollama-format `gpt-oss` plus `dinov2`, so a soak alone
proves only absence of harm, as in recent releases. Each issue needs its own live
check:

- Issue 1: the legacy calls above.
- Issue 2: a gemma3 request with tools, checking that its trace carries
  `does not support tools`.
- Issue 3: a `claude-*` request with tools resolving to a tools-capable model, with
  gemma3 loaded.
- Issue 4: one real payload posted to the live service and read back from the
  database, read-only.

---

## Codebase audit (2026-11-17)

Read against the code on `main` after 0.12.5 (`6f34c8c`), asking of each step what
already exists to extend, what debt it would add, and what existing debt it should
retire rather than build next to. Issues 2 and 3 were audited in
`telemetry-errors-2026-11.md`; this section covers what is new here, plus where the
four issues meet.

### Issue 1: the embedding path

- **Empty embeddings must not reuse `no_output`.** `no_output` is read by one place:
  `trace_store.get_stream_reliability_24h` feeds `_check_stream_reliability`, which
  fires **`empty_generations` at a count of one** with a description of a *model* that
  "generated zero tokens", "counted from Ollama's own eval_count", and a fix about
  llama-server load failures. An empty embedding response would fire it with an
  explanation that is wrong in every sentence. Hence `failed` (1b, revised above).
  `failed` is also already counted in telemetry, so the fix does not wait for
  issue 4.
- **The safeguard already half-exists, in the wrong layer.** `openai_embeddings`
  checks `len(embeddings) != len(texts)` and answers 502, but `dispatch_embed` has
  already traced `completed` by then. So `/v1/embeddings` tells the client the truth
  while the trace store and telemetry record a success. Moving the check down
  removes that disagreement, and `/v1/embeddings` drops its own copy, keeping only
  its `dimensions` check, which is OpenAI-specific.
- **One helper, two call sites.** The embedding path records `completed` in two
  places: the Ollama branch of `dispatch_embed`, and
  `text_embedding_compat.proxy_to_native_text_server` for the native server. The
  native server matters too: its own comment says fastembed *drops empty strings*,
  "which would silently shift every later index". So the count check is one
  function, `embedding_count_error(body, result) -> str | None`, beside
  `dispatch_embed`, and both sites call it. The vision path takes images, not a
  text `input`, and is out of scope.
- **The Ollama branch builds its trace by hand four times** (completed, HTTP error,
  other exception, timeout exhausted), each a full `record_trace(...)` call
  differing in a single field. `routing.direct_proxy_tracer` exists for exactly this.
  It was written because the native and vision paths each carried their own copy,
  and its docstring records that the vision path once traced nothing as a result.
  It lacks three things the Ollama branch records: the request's own `request_id`,
  the routing `score`, and `retry_count`. Give `direct_proxy_tracer` those as
  optional keyword arguments, and the four calls become `trace("completed")`,
  `trace("failed", error=...)` and so on. Do this first in issue 1: it is the line
  1b edits and the line issue 2 edits, so each later diff is one line instead of
  four.
- **Rebuilding a response with its fleet headers would be a second copy.**
  `openai_embeddings` already rebuilds `dispatch_embed`'s response with its
  `X-Fleet-*` headers ("drop the ones Starlette recomputes"). The legacy handler
  needs the same, so extract `_embed_result(resp) -> (status, data, headers)` beside
  `dispatch_embed` and use it in both translations.
- **Request validation is already shared in spirit, not in code.** `/v1/embeddings`
  validates `input` with OpenAI's error envelope; the legacy handler validates
  `prompt` with Ollama's `{"error": ...}`. The rules differ (legacy takes exactly one
  string), so each keeps its own few lines. A shared validator would need a flag for
  the format, which is worse than two short checks.
- **The native text server already accepts `prompt`** (`text_embedding_server.py`
  falls back to it), which is why `nomic-embed-text` returned vectors. The legacy
  handler still renames `prompt` to `input` before dispatch, so every backend sees
  one request shape and nothing relies on that fallback.
- **Existing debt touched, not widened:** the Ollama branch's error bodies
  (`"Ollama returned HTTP 4xx: ..."`, `"Failed to reach Ollama on <node>: ..."`)
  predate `client_error_message`. Folding them onto it belongs to issue 2's item 1,
  which already lists this exception site, and the tracer change above makes that
  one edit.

### Issue 4: telemetry outcomes

- **There is no definition of the trace statuses.** `RequestStatus` in
  `models/request.py` is the *queue's* status (pending, in-flight, completed,
  failed). Trace statuses are bare strings at about 50 call sites:
  `completed`, `failed`, `retried`, `rejected`, `incomplete`, `no_output`,
  `client_disconnected`. That is why `anonymous_rollup` could miss five of them with
  nothing noticing. The "every status counted or excluded" test needs something to
  enumerate, so add `TRACE_STATUSES` (a frozenset) in `trace_store.py` beside
  `record_trace`. `record_trace` logs an ERROR for any status outside it and still
  records the row: a typo then surfaces in the log, never as a lost trace. Converting
  the call sites to an enum would be a large diff for no behaviour change, so they
  stay strings.
- **Excluding `retried` from `requests` matches the dashboard's existing rule.**
  `trace_store` already filters `status != 'retried'` in its request counts and
  failure rates (`get_timeseries` and two summary queries). So 4a makes telemetry
  agree with what the operator already sees, rather than inventing a new rule.
- **The rollup stays one loop.** `anonymous_rollup` maps status to count in a single
  `if/elif`. That becomes one mapping from trace status to field name
  (`rejected` → `rejected_count`, and so on), with `retried` explicitly excluded and
  the test asserting `mapping keys | excluded == TRACE_STATUSES`. No second query.
- **`get_stream_reliability_24h` already groups three of the four outcomes by
  model,** but it is a 24-hour health query on the read connection, while telemetry
  reads a closed UTC day on its own `TraceStore`. Sharing it would couple the health
  pass to the telemetry window, so the rollup keeps its own query.
- **Service side: four edits, all following existing fields.** `ModelEntry` (same
  bounds as `error_count`), `DailyRollup` (same `BigInteger, default=0`), and
  `routes/ingest.py`, which lists every field twice: in the insert and in the
  upsert's `excluded` set. Missing the second would silently keep the first
  snapshot of the day, since the payload is a cumulative daily snapshot. Plus
  migration `0002_outcome_counts.sql`, plain DDL in the format `migrate.py` splits,
  and the field table in `site/telemetry.html`. `stats.py` sums `requests` for
  public totals, so 4a lowers the published request count slightly from the change
  date. The telemetry page's change note covers it, and no stats code changes.
- **The golden payload test is the guard that should fail.**
  `test_community_payload_golden.py` pins the payload byte for byte. It must be
  updated in the same commit as the rollup, with the four fields at zero in its
  fixture, so the diff shows exactly what the wire gains.

### Where the four meet

- Issue 1's tracer change and issue 2's `describe_exception` change touch the same
  exception path. Doing issue 1 first leaves issue 2 a single edit there, as the
  order already says.
- Issue 1b's `failed` traces reach telemetry immediately as category `other`. Once
  issue 2 lands, that category reads correctly without further work.
- Issue 4's coverage test also protects issues 1 to 3: any status a future fix adds
  must be counted or explicitly excluded before the build passes.

### Recorded, not in scope

- `/api/embed` with an empty `input` list is passed to Ollama as is. Ollama answers
  it, so it is not a bug, but it is the one case the count check must allow.
- The vision path (`embed_image`) has no count check either; it takes images, and
  nothing in telemetry points at it.

---

## Implementation plan (after the audit)

1. **Issue 1**:
   1. Optional `request_id` / `score` / `retry_count` on `direct_proxy_tracer`, and
      the four hand-built traces in `dispatch_embed`'s Ollama branch moved onto it.
      No behaviour change; the existing embed tests must pass untouched.
   2. `embedding_count_error`, called by the Ollama branch and the native proxy
      before `completed`. `/v1/embeddings` drops its own count check.
   3. `_embed_result`, then `ollama_embeddings_legacy` on `/api/embeddings`.
      `openai_embeddings` moves onto `_embed_result` too.
   4. Tests as listed under issue 1, plus a check that `/v1/embeddings`'s
      wrong-count case now traces `failed`.
2. **Issue 2**: as in `telemetry-errors-2026-11.md` step 1. Its embed exception
   site is now one `trace("failed", error=describe_exception(e))`.
3. **Issue 3**: as in that plan's steps 2 and 3.
4. **Issue 4**:
   1. Service (with the go-ahead to deploy): `ModelEntry`, `DailyRollup`, both
      `ingest.py` field lists, migration `0002`, `telemetry.html`, then deploy and
      verify.
   2. Client: `TRACE_STATUSES` with the `record_trace` guard, the status-to-field
      mapping with `retried` excluded, `ALLOWED_ENTRY_KEYS`, the golden fixture,
      the coverage test, and the openapi check in the release gate.

**Decisions (2026-11-17, maintainers):**
- A stream Ollama ends with an error line is traced **`failed`**, with the
  backend's reason. `incomplete` keeps meaning "stopped without saying why".
- The telemetry service change is **approved for deploy** (issue 4, step 1).

---

## As built

### Issue 4, step 1: telemetry service (deployed 2026-11-18 07:50 UTC)

`ollama-herd-private` `d828f36`:

- **Fields:** `ModelEntry` gains `rejected_count`, `incomplete_count`,
  `no_output_count` and `disconnected_count` (default 0, same bounds as
  `error_count`), plus a validator: success, error and the four together must not
  exceed `requests`.
- **Storage:** `DailyRollup` has the four columns, and `ingest.py` writes them in
  both the insert and the upsert's `excluded` set. A test replays a day to prove
  the second list.
- **Migration:** `0002_outcome_counts.sql`. A test checks it adds exactly the
  model's columns, because the SQLite tests build the schema from the models and
  would never notice a mismatch.
- **Contract page:** `site/telemetry.html` lists the fields and notes the
  `requests` change.
- **Tests:** 117 pass (6 new). The site suite passes locally and against
  production (78 of 78).

**Verified live, without writing to production:**
- The openapi schema lists all four fields.
- A payload carrying them with a deliberately invalid `install_id` is rejected
  *only* for the `install_id`, so the fields are accepted.
- An outcome sum over `requests` is rejected with the new message.
- `schema_migrations` shows `0002` applied at 07:50:05 UTC, and the columns exist.

The first real payloads after the deploy arrive with each herd's daily send.
Confirm they keep landing: the newest `updated_at` must move past 07:50.

**One edge, accepted:** the upsert's monotonic guard (`requests <= excluded`)
means that on the day a herd upgrades, the first payload from the new client
counts fewer `requests` (retries excluded) than the old client's row for that
day. That one day's update is skipped. Every later day is unaffected.

**Next:** the client half (issue 4, step 2) can ship in any herd release from now
on, since the service accepts the fields.

### Issues 1 to 4: herd (2026-11-18, unreleased)

**Issue 1, embeddings:**
- `direct_proxy_tracer` takes an optional `request_id`, `original_model`,
  `score`, `scores_breakdown` and per-call `retry_count`. The Ollama embed
  branch's four hand-built `record_trace` calls became `trace(...)`.
- `routing.embedding_count_mismatch(body, result)` is called before
  `completed` by the Ollama branch and by the native proxy, through a
  `count_mismatch` field on `_NativeKind` set for `EMBED` only. The trace
  text `EMBEDDING_COUNT_MISMATCH` is digit-free; counts go to the log and the
  client.
- `ollama_compat.embed_result(resp)` is shared by `/v1/embeddings` and the
  new `ollama_embeddings_legacy`. `/v1/embeddings` keeps a one-line guard for
  the path `dispatch_embed` does not count (image models) instead of its old
  full copy.

**Issue 2, the reason:**
- `describe_exception` appends `backend_error_detail` to httpx's first line.
- `categorize_error` checks stated reasons first (out of memory, context
  exceeded), then a parsed status (`(client|server) error 'NNN'` or
  `HTTP NNN:`), and uses bare-digit rules only when no status is stated. The
  tests caught a 429 holding "5000" falling through to the digit rules: a
  known status now skips them.
- One `ollama_stream_error(chunk)` is read by the proxy (trace and OpenAI
  terminal), both translators and both non-streaming collectors, and by the
  native accumulator. Anthropic and Responses keep the message on their own
  translator state, so there is no second proxy side channel to race.
  `_request_backend_error` is trace-path only, and `_record_incomplete`
  traces `failed` with `describe_backend_stream_error` when it is set.

**Issue 3, tools:**
- `resolve_model(has_tools=...)` uses `_candidate_score(want_tools=...)`,
  active only when some candidate reports `tools`.
- The routing helpers are split into `fleet_loaded_and_ondisk(nodes)` and
  `fleet_capabilities(nodes)`, with the registry wrappers unchanged. That
  retired the two health checks' copies of the loaded/on-disk loop.
- `health_engine._anthropic_model_map()` replaces two parsers.
- `mapped_model_no_tools` sits inside `_check_mapped_models_hot`.
- `trace_store.get_backend_failures` replaces `get_backend_not_found`, with
  kinds in `BACKEND_FAILURE_KINDS`; it feeds `tools_unsupported_requests`.

**Issue 4, telemetry:**
- `trace_store.TRACE_STATUSES`, with `record_trace` logging an ERROR for
  anything else.
- `anonymous_rollup.OUTCOME_FIELDS` and `EXCLUDED_STATUSES`, the four fields
  in `ALLOWED_ENTRY_KEYS`, and the golden fixture seeding one row of each
  outcome.
- Release checklist step 3 now runs the live contract test with
  `FLEET_TEST_REMOTE_CONTRACT=1`, so it fails rather than skips. Device-level
  `requests` still counts attempts: they measure load per machine, and a
  retried attempt was real load.

**Tests and counts:** 2216 tests (+76) and 54 checks (+2). New test modules:
`test_embedding_shapes.py`, `test_backend_reason.py`, `test_tools_routing.py`
and `test_telemetry_outcomes.py`.

**Verified live, after restarting both agents:**
- `/api/embeddings` for `bge-m3` and `mxbai-embed-large` matches Ollama's own
  legacy answer (cosine 1.0). `nomic-embed-text`'s legacy answer equals
  herd's `/api/embed` answer (1.0). Its 0.95 against Ollama is the native
  server's model build, present before this change too.
- A gemma3 + tools request traces `... does not support tools`, and
  `tools_unsupported_requests` fired on it.
- Context launch args unchanged (`-c 524288 -np 4`, `-c 131072 -np 4`).
- The live contract test passes.

**Not exercised live:**
- An Ollama in-stream error line, covered by route tests on every surface.
- Auto-routing with a no-tools model loaded, since this fleet uses an
  explicit map; covered by unit tests.
