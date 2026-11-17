# A node advertises a model its backend says it does not have

**Status:** Steps 0, 1 and 2 **implemented 2026-11-17** (unreleased); steps 3-5 proposed.
Investigated, reproduced, and audited against the code. **"Implementation plan (after the codebase audit)" at the end is the plan**; the
earlier "Plan" and "Revised plan" lists are kept for their reasoning only.
**Issue:** `docs/issues.md`, "A backend 404 is never retried…" (OPEN).
**Found in:** community telemetry, not on this fleet. `model_not_found` is 58% of all
errors reported in the 30 days to 2026-11-17 (1,515 of 2,594).

## What the telemetry says

Telemetry counts only `failed` traces (`anonymous_rollup.py`: `elif status == "failed"`).
A model no node has is `rejected` and never reaches this category. So every one of these
1,515 is the same sequence: **herd picked a node that advertised the model, dispatched,
and that node's backend answered 404.** `categorize_error` maps any message containing
`404` and `not found`, which for an Ollama dispatch is httpx's
`Client error '404 Not Found' for url '.../api/chat'`.

Per install (read-only query against the telemetry database, 2026-11-17):

| Install | Errors | Days | Model | Notes |
|---|---|---|---|---|
| `a238cc88` | 1,440 | Nov 4 only | `qwen2.5-coder:14b`: 1,440 requests, **0 successes** | 3 Linux nodes, 63 GB each. Every request went to `e67db4`, the one node still on agent 0.11.3; router 0.12.3. Exactly one request a minute for 24 h |
| `9623f6c5` | 63 | Oct 22–23 | `qwen3-coder:30b` (364 of 410 failed), `llama3.1:8b`, `qwen3-coder:64k` | Agent 0.9.6, mixed AMD / NVIDIA / Apple nodes |
| `ddcef947` | 10 | Nov 16 | `llama3.2:3b`, 10 of 14 failed | 7 Intel + NVIDIA nodes |
| `334e4b13` | 2 | Oct 23 | `qwen3-embedding:4b` | An Ollama reporting version `0.1.0-harmonyos-proxy` |

This fleet has recorded none in 30 days.

## Two gaps in herd

### 1. A backend 404 teaches herd nothing

- `StreamingProxy._is_retryable_error` retries connection failures and 5xx only, so a
  404 is not retried on another node that has the model.
- The registry keeps the model on that node; the next heartbeat re-asserts it, so the
  same node wins the next request.
- Nothing names the condition. The `a238cc88` client failed 1,440 times in a row.

### 2. The router can send a remote node's traffic to its own local Ollama

`NodeRegistry._build_ollama_url` treats a heartbeat that arrives from loopback as "the
node is on this machine" and returns `http://localhost:<port>`, **before** it looks at an
explicit non-loopback `ollama_host`. Verified:

```
remote node, ollama_host=http://10.0.0.50:11434
  heartbeat from 10.0.0.50 -> http://10.0.0.50:11434
  heartbeat from 127.0.0.1 -> http://localhost:11434    <- the router's own Ollama
```

A heartbeat arrives from loopback whenever something on the router's machine forwards it:
an SSH tunnel (`ssh -L 11435:...` from the node), a reverse proxy in front of `:11435`,
some container port-forwarders. Every remote node then routes to the router machine's
Ollama. Models present there are served by the wrong machine, silently; models absent
there 404. Whether this is what happened at `a238cc88` cannot be shown from telemetry.

## Plan

Smallest and safest first. Each step ships with tests; no flag (greenfield rule).

1. **A 404 for the requested model is a routing fact, not a client error.**
   - In `_stream_with_retry` (and the non-streaming path), when the backend answers 404
     and the body names the model as not found, add the node to `excluded_nodes` and
     re-score, as for a 5xx. A 404 for another reason (wrong endpoint) is not retried.
   - Record it on the registry: the model is treated as absent on that node until a
     heartbeat **received after the 404** lists it again. A heartbeat cannot erase a
     404 newer than itself.
   - Log a WARNING naming node, model and the URL herd sent it to. That URL is the clue
     for gap 2.
2. **Make the loopback shortcut the last resort.** Order in `_build_ollama_url`: an
   explicit non-loopback `ollama_host`; then, for a loopback heartbeat, `localhost` only
   if the payload's `lan_ip` is one of the router's own addresses (or absent); otherwise
   the node's `lan_ip`. The Docker case from issue #1 keeps working: it is the explicit
   `ollama_host` branch.
3. **Check that the router talks to the Ollama the node describes.** On registration and
   when the URL changes, fetch `/api/tags` from the URL the router will use and compare
   it with the heartbeat's `models_available`. Disagreement is a health check
   (`backend_mismatch`) naming the node, the URL and the models in dispute. It catches
   gap 2 and anything shaped like it, without inferring topology.
4. **Health check for the residue:** `backend_model_missing` fires when step 1 has
   excluded a node:model in the last hour, so a client polling a broken pairing is
   visible on the board, not only in logs.
5. **Related routing holes found while probing (they feed `bad_request`, 32%):**
   a chat request for an embedding-only model is sent to Ollama `/api/chat` (400), and
   an `mlx:` model on the Ollama `/api/chat` route goes to Ollama, not the MLX server
   (400). Reject both up front with a message saying why.

## Verification

- Unit: retry-on-404 picks the other node; the 404 mark outlives older heartbeats and
  yields to a newer one; a non-model 404 is not retried; `_build_ollama_url` for every
  source-IP and `ollama_host` combination, including issue #1's Docker case.
- Live: a second router on another port with a fake remote node whose heartbeat comes in
  over loopback, before and after step 2.
- Telemetry, a week after release: `model_not_found` falls for installs on the new
  version.

## Findings (2026-11-17)

### Reproduced end to end

A throwaway router on `:11535` (own data dir, telemetry off, its own mDNS type), a fake
backend on `:11999` that lists nothing and answers every chat with Ollama's exact 404
body, and fake nodes posting heartbeats. Scripts:
`scratchpad/repro404/{router,fake404,scenarios,surfaces}.py` (session scratch, not
committed; the shape is what step 6 of the verification turns into tests).

**Gap 2, loopback heartbeat.** Node `remote-tunnel` with
`ollama_host=http://10.255.255.1:11434`, heartbeat posted to `127.0.0.1`. The router
resolved it to `http://localhost:11434`, this machine's Ollama, which does not have
`qwen2.5-coder:14b`: 3 of 3 requests `failed` with
`Client error '404 Not Found' for url 'http://localhost:11434/api/chat'`, the exact
message shape telemetry categorises as `model_not_found`.

**Gap 1, no learning.** Nodes `a-broken` (fake 404 backend, favoured: 512 GB, idle) and
`b-good` (real Ollama with `gemma3:27b` resident) both advertising `gemma3:27b`.
**4 of 4 requests went to `a-broken` and failed; `b-good` was never tried.** The health
board afterwards said only `high_error_rate` at INFO ("11 failure(s) on a-broken").

### A third gap: the Ollama-native surface answers a backend 4xx with a 500

Same two nodes, every surface:

| Surface | What the client got |
|---|---|
| `/api/chat`, `/api/generate` non-streaming | **500 `Internal Server Error`** (plain text; the exception leaves the app, so uvicorn also closes the keep-alive connection) |
| `/api/chat` streaming | **connection dropped mid-body** (`RemoteProtocolError: incomplete chunked read`) |
| OpenAI, Anthropic, Responses non-streaming | 404 with Ollama's message. Correct |
| OpenAI, Anthropic streaming | 200 then an in-stream error carrying httpx's text, **including the backend URL** (`http://10.0.0.92:11999/api/chat`) |

`routing.client_error_passthrough` already turns a backend 4xx into the same 4xx with
the backend's message; OpenAI, Anthropic and Responses call it (20 call sites), the
native routes never do (`ollama_compat._dispatch`, around the `async for chunk in stream`
at the end). A once-a-minute poller is most likely an Ollama-native client, so the
`a238cc88` client was probably told "Internal Server Error" 1,440 times, never
"model not found". The URL in stream errors is the same class as the MLX 4xx URL leak
fixed in 0.11.4.

### Why a node can list a model Ollama then 404s

From Ollama's `server/routes.go` (main, fetched 2026-11-17), `/api/chat` and
`/api/generate` answer 404 when:
- the manifest is missing (`getExistingModelForRunner` → `fs.ErrNotExist`): the model
  really is not on *that* Ollama, which is what gap 2 produces;
- a blob is missing at load (`scheduleError` → `os.ErrNotExist`,
  `"model %q not found, try pulling it first"`): the manifest is listed by `/api/tags`
  but its data is gone. A genuinely broken store, and the only cause where a node
  honestly lists a model it cannot serve;
- a cloud-backed model is requested with a `:local` source suffix.

So "node lists it, Ollama 404s it" is either a corrupt store or herd talking to a
different Ollama than the node reports. Step 3 tells those apart.

### Refinement to step 2

The collector rewrites a loopback `ollama_host` to `http://<lan_ip>:<port>` before
sending (`collector._make_lan_reachable_url`), so a router **cannot** tell an explicit
`FLEET_NODE_OLLAMA_HOST` from a rewritten one by looking at the payload. "Prefer a
non-loopback `ollama_host`" alone would send a co-located node through its LAN address
and lose the localhost shortcut the code deliberately takes. The discriminator is
`payload.lan_ip`: for a loopback heartbeat, the node is on this machine only if its
`lan_ip` is one of the router's own addresses (`psutil.net_if_addrs()`), or it has none.
Then use localhost **with the host part of `ollama_host` only if that host is `lan_ip`**;
an `ollama_host` naming some other machine is honoured. A loopback heartbeat from a
`lan_ip` that is not ours is a tunnel or a proxy: use `ollama_host` as sent.

Where loopback heartbeats come from in practice: uvicorn's defaults
(`proxy_headers=True`, `forwarded_allow_ips` → `127.0.0.1`) **do** recover the real
client from `X-Forwarded-For` when a local proxy sets it, so Caddy and Tailscale Serve
are fine. nginx's default `proxy_pass` does not set that header, and an SSH tunnel
cannot, so both arrive as `127.0.0.1`.

### What the telemetry cannot say

Which of the three causes hit `a238cc88`. The node that took all 1,440 requests
(`e67db4`) was the one still on agent 0.11.3, but nothing in node reporting or URL
building changed between 0.11.3 and 0.12.3 (the only diff is the platform removal), so
the version skew is a coincidence or a symptom, not the mechanism.

### Revised plan

0. **Native routes answer a backend 4xx in-protocol** (new, smallest, first): call
   `client_error_passthrough` on the non-streaming path; on the streaming path end with
   an Ollama-shaped `{"error": "..."}` line instead of dropping the connection. Stream
   errors on every surface carry the backend's message, never httpx's text with the URL.
1. Retry a model 404 on another node; mark the pairing absent until a newer heartbeat
   (unchanged).
2. Loopback shortcut keyed on `lan_ip` being the router's own (refined above).
3. Router-side `/api/tags` cross-check → `backend_mismatch` (unchanged; it is also what
   distinguishes gap 2 from a corrupt store).
4. `backend_model_missing` health check (unchanged).
5. Embedding-only and `mlx:` models on `/api/chat` rejected up front (unchanged).

## Codebase audit (2026-11-17)

Greenfield rules: no flag, no second code path, reuse before build. Every step was
checked for an existing helper or pattern. Three steps shrink, one disappears, and the
audit found a fourth defect.

### What already exists, per step

| Step | Existing code to reuse | Verdict |
|---|---|---|
| 0, native 4xx | `routing.client_error_passthrough` already parses the backend's `{"error": ...}` body. Native error bodies are already the literal `{"error": str}` shape in 27 places in `ollama_compat.py`. The pull stream already ends with a `{"error": ...}` NDJSON line (`ollama_compat.py:690`), and that line is exactly what Ollama itself sends and what `_recover_backend_errors` parses | Extract the body parsing into **one** helper; no new envelope |
| 0, URL in stream errors | All five in-stream error sites pass `describe_exception(exc)`, which is httpx's text with the URL | Same helper, client-facing only. **Traces keep `describe_exception`**, because `categorize_error` reads them and the telemetry histogram is a wire contract |
| 1, retry | `_stream_with_retry` already has `excluded_nodes`, re-scoring, the `retried` trace and the retry budget. `_is_queue_full_error` is already a body-reading predicate on an `HTTPStatusError`. `stream_from_node` already `aread()`s an error body before `raise_for_status`, so the body is available. Both streaming and non-streaming go through `_is_retryable_error` | One predicate, `_is_model_not_found_error`, beside `_is_queue_full_error` |
| 1, registry mark | Nothing like it exists, and nothing needs it (see below) | **Dropped** |
| 2, loopback | `common.system_metrics.get_local_ip()` is what the node's collector puts in `lan_ip` (`collector.py:731`). The router's `FleetServiceAdvertiser` already tracks its own current value (`discovery.py`, `self._ip`, kept fresh by `watch_address`) | Compare `payload.lan_ip` with `get_local_ip()`: the same function on the same machine gives the same answer. **No `psutil.net_if_addrs()`** |
| 3, cross-check | `StreamingProxy.query_node_models` already fetches `/api/tags` through `_get_client(node_id)`, i.e. the URL herd really dispatches to, which is the one thing this check must use | Reuse it. It currently returns `[]` on any error, which a cross-check would read as "every model missing", so it must tell "unreachable" from "empty". Its only caller is the dashboard |
| 4, health check | `get_model_timeouts_24h` is the same query shape (failed/retried traces by `node_id, model`). `_check_mlx_requests_unservable` is the same check shape ("traffic for something that cannot be served"). `_min_samples_for_rate` exists if a rate is ever wanted | Trace-derived, **no new state**. Categorise with `categorize_error` in Python, not a SQL `LIKE`, so "model not found" keeps one definition |
| 5, mlx on native | `openai_compat` already gates `mlx:` with `no_mlx_server_reason`, a classified 503 | Reuse the gate; route mlx through the native surface only if a translation already exists (it does not) |
| 5, embedding on chat | `model_has_capability(node, model, "embedding")`, `get_non_generatable_models()` and `is_text_embedding_model` are the same three signals `priority_model_not_loaded` already combines | Reuse the combination |

### Dropped: the registry mark

Step 1 proposed marking a node:model absent until a heartbeat newer than the 404. Once
the retry exists, the mark buys one avoided round trip, and a 404 answers in
milliseconds. Its costs are new state on the registry, a timestamp race against
heartbeats, and a second place that decides what a node has. The health check (step 4)
reads the `retried` traces the retry already writes, so visibility does not need it
either. **Retry alone fixes every multi-node case.** The single-node case, the
a238cc88 shape, is not helped by any mark: only steps 2 and 3 fix it.

### Found: native non-streaming fabricates `done: true`

`ollama_compat._route_and_stream`, non-streaming: when no `done` line arrives,
`final_data` is `None` and the route answers 200 with `"done": True` and the partial
text. 0.11.4 fixed this exact failure on OpenAI, Anthropic and Responses
(`upstream_failure_response`, a 502). The native *streaming* path is deliberately left
bare, because a native client checks `done` itself (`streaming.py:898`). The
non-streaming path invents the very field that check relies on. It belongs in step 0:
same function, same accumulator.

### Implementation plan (after the codebase audit)

0. **Native surface answers failures in-protocol; no backend URL reaches a client.**
   - `common/errors.backend_error_detail(exc) -> str | None`: the backend's own message
     from an `HTTPStatusError` body (`error`, then `message`, then the first 300 chars of
     text), else `None`. It is moved out of `client_error_passthrough`, which then calls it.
   - Every in-stream error site sends `backend_error_detail(exc) or describe_exception(exc)`.
     Traces are unchanged.
   - `ollama_compat._route_and_stream`:
     - Non-streaming: wrap the accumulate loop. A 4xx returns the same status with
       `{"error": detail}`; anything else returns 502 with `{"error": ...}`. The
       envelope is the literal shape the file already uses, not a third flavour of
       `error_body`.
     - No `done` line: 502 with `UPSTREAM_INCOMPLETE_MESSAGE`, not a fabricated
       `done: true`.
     - Streaming: `_stream_and_cleanup` catches and ends with one
       `{"error": ...}\n` line, the line Ollama itself sends.
1. **Retry a model 404 on another node.** Add `_is_model_not_found_error(e)` beside
   `_is_queue_full_error`: status 404, and a body that names a model as not found.
   `_is_retryable_error` returns `True` for it. Do not invalidate the client on it:
   `_invalidate_client` is keyed on `_is_retryable_error` today, so key it on the
   transport-error half instead. Nothing else changes: the exclusion, re-scoring, the
   `retried` trace and the budget are all existing. A 404 for any other reason (a
   wrong endpoint) is still terminal.
2. **Loopback shortcut only for a node on this machine.** In `_build_ollama_url`, a
   loopback heartbeat means local only when `payload.lan_ip` is empty, loopback, or
   equal to `get_local_ip(warn=False)`.
   - Local: `localhost` as today, unless `ollama_host` names a host other than
     `lan_ip` (a deliberate setting); that is honoured.
   - Otherwise: fall through to the existing remote order, which already honours
     `ollama_host` (issue #1's Docker case).
3. **`backend_mismatch` check.**
   - When a node registers or its URL changes, `query_node_models` the URL herd uses
     and compare the result with the heartbeat's `models_available`.
   - The result is cached on the router and evaluated by a pure health check, the way
     the other checks read pre-gathered data.
   - `query_node_models` returns `None` for "could not ask", so an unreachable backend
     is a missed detection, never a false alarm. That is the `backend_bypass_clients`
     rule.
4. **`backend_model_missing` check.**
   - A trace query beside `get_model_timeouts_24h`: failed and retried rows by
     `node_id, model, error_message`, categorised with `categorize_error`.
   - It fires on `model_not_found`. With step 1 these are mostly `retried` rows: the
     client was served, and the node is still wrong.
   - What it fires on when everything works: nothing, because a healthy node never
     404s a model it lists.
5. **Up-front rejections on the native routes.**
   - `mlx:`: reuse `openai_compat`'s gate and `no_mlx_server_reason` message. Without
     an MLX→Ollama translation, an `mlx:` model on `/api/chat` is a 400 that says to
     use `/v1/chat/completions`.
   - Embedding-only models: the `priority_model_not_loaded` signal set, answered 400.

Order and size: 0 and 1 are small and independent, and fix every multi-node failure
and every misleading answer. 2 is a few lines plus tests. 3 is the only step with new
moving parts, and the only one that distinguishes gap 2 from a corrupt store.

## As built: steps 0 and 1 (2026-11-17)

- **`common/errors.backend_error_detail` and `client_error_message`.**
  - `client_error_passthrough` now calls the first.
  - The ten client-facing sites call the second: five in-stream errors and five
    non-streaming 502s. The 502s also carried the backend URL, for a backend 5xx;
    the audit had counted only the in-stream half.
  - Traces and log lines keep `describe_exception`, so the telemetry category is
    unchanged. A test pins this.
- **Native routes.**
  - Non-streaming: a backend 4xx is returned with the same status and
    `{"error": msg}`; anything else is a 502. No `done` line is a 502 with
    `UPSTREAM_INCOMPLETE_MESSAGE`.
  - Streaming: a failure ends the stream with an `{"error": msg}` line.
- **`_is_model_not_found_error` and `_is_transport_error`** sit beside
  `_is_queue_full_error`. The pooled client is now dropped only on a transport
  error, so a 5xx no longer drops it either, since an HTTP answer proves the
  connection works.
- **Found while testing: the retry loop lost the backend's error when no other node
  was left.** It recorded `retried`, then raised `RuntimeError("No available nodes
  after excluding [...]")`.
  - The failure was invisible before, because only connection errors and 5xx
    reached it. Retrying 404s made it the single-node path, where it turned
    Ollama's 404 into a 502.
  - It also left the request with **no terminal trace**: the same gap the
    "retries exhausted" branch was fixed for.
  - Now the loop looks for an alternative node *before* recording anything. With
    none, the attempt is traced `failed` and the original exception propagates.
- **Telemetry consequence:** a 404 that a peer then serves is traced `retried`, and
  telemetry counts only `failed` traces. So after release `model_not_found` will
  show only the single-node cases (the `a238cc88` shape), which steps 2 and 3
  address. The retried ones are what step 4 surfaces locally.
- **Verified live** on a scratch router (`:11535`):
  - Two nodes, favoured one 404ing: 7 of 7 surface and stream combinations served
    (7 `retried` on the broken node, 7 `completed` on the good one). Before the
    fix, every one failed.
  - One node: native non-streaming 404 `{"error":"model '...' not found"}`; native
    stream ends with that line; OpenAI and Anthropic streams carry the message
    with no URL. All five traced `failed`.
- **Tests:** `tests/test_server/test_backend_model_not_found.py` (36), and
  `test_retry.py::test_no_nodes_left_after_exclusion` updated to the new contract.
  2114 pass.

## As built: step 2 (2026-11-17)

- **`NodeRegistry._node_is_on_this_machine`.** A heartbeat is local only if it
  arrived from loopback (or an empty address) **and** the node's `lan_ip` is empty,
  loopback, or equal to the router's `get_local_ip(warn=False)`. The node fills
  `lan_ip` with the same function (`collector.py`), so on the same machine the two
  agree without enumerating interfaces.
- **Local:** `localhost` with `ollama_host`'s port, unless `ollama_host` names a
  host other than `lan_ip`. That is a deliberate setting (a node on this box driving
  another machine's Ollama), and it is now honoured; it used to be sent to
  `localhost` too.
- **Not local:** the existing remote order is unchanged. The collector has already
  rewritten a loopback `ollama_host` to `http://<lan_ip>:<port>`, so a tunnelled
  node resolves to its own address.
- **A router booted before the network** (`get_local_ip` falls back to `127.0.0.1`,
  the 0.12.3 case) does not recognise its co-located node as local. The node then
  gets its own LAN address, which is this machine, so it is still reachable.
- **Tests:** 11 in `test_backend_model_not_found.py::TestLoopbackHeartbeats`. Two
  existing co-located tests used a `lan_ip` that was not the host's, so they now
  patch `get_local_ip`. 2125 pass.
- **Verified live.**
  - Scratch router: the tunnel scenario (`lan_ip=10.255.255.1`, heartbeat over
    loopback) resolves to `http://10.255.255.1:11434`, where it used to resolve to
    `http://localhost:11434`.
  - Live router restarted onto the change: node `bb` still resolves to
    `http://localhost:11434` and serves.
- **Side effect of the testing, reverted.** The scratch router has no
  `FLEET_NUM_CTX_OVERRIDES`, so its requests to the real Ollama reloaded
  `gemma3:27b` at 131072 per slot. `ollama stop gemma3:27b` plus one request
  through the live router restored 32768 (`-c 131072 -np 4`). **Run any future
  scratch router with the operator's override settings, or against a fake backend
  only.**
