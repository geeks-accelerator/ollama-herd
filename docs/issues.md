# Known Issues & Improvements

Identified via code review of the full codebase. Organized by priority.

**Status key:** `OPEN` — not yet addressed. `PARTIAL` — partially fixed. `FIXED` — resolved.

---

## Correctness

### A native embed or rerank with no node to serve it returned a silent 503 that blamed the wrong thing `FIXED` (2026-10-04)

**Severity:** medium. It drove a client off herd entirely.

`proxy_to_native_text_server` answered every "no online node serves this" case with
"No node is running the native text embedding server... Install fastembed", and
returned before logging or tracing.

On 2026-10-04 openclaw was repointed at herd for memory embeddings:

| Time | Event |
|---|---|
| 05:52:55 | Its first embed succeeded through herd (native, 270 ms) |
| 05:52:59 | herd-node was taken down for a reboot-simulation test |
| 05:53:55 | herd-node back |

Every embed in between got the install-fastembed 503. fastembed was installed. The
managing agent concluded "herd can't serve embeddings yet" and reverted at 05:54:16,
20 s after herd recovered. herd itself had no log line and no trace of any of it.

**Fixed:** `_no_native_server` classifies the cause, using the registry's retained
heartbeat for offline nodes:

- **A node serves it but is offline.** 503 + `Retry-After: 10`, naming the node.
- **No node online at all** (a restarted router before re-registration). 503 +
  `Retry-After`.
- **Online nodes exist, none run the native server.** The install hint, with no
  `Retry-After`.

Each case logs a WARNING and records a `rejected` trace via `record_routing_rejection`.
`/v1/embeddings` now passes `Retry-After` through; it rebuilt error bodies and dropped
all headers. Verified live by restarting the router and probing: 503 + `Retry-After`
at +1.6 s, 200 at +6.0 s once the node re-registered, with the WARNING and the trace
present.

---

### `priority_model_not_loaded` reported embedding models that can never be preloaded `FIXED` (2026-10-02)

**Severity:** low (misleading dashboard), but it was a standing WARNING.

Preloading warms a model by posting to `/api/generate`, which Ollama refuses outright
for an embedding model (`"does not support generate"`). So the check was reporting
something that was never going to happen: high embed volume made `nomic-embed-text` a
priority model, the preloader correctly declined to warm it (see the non-generatable
fix of 2026-10-01), and the dashboard then carried a permanent WARNING *about the
decline*. Fixing the log spam had moved the wrong signal rather than removing it.

**Two signals, because neither alone is sufficient:**

1. **What Ollama reports.** `model_has_capability(node, model, "embedding")` —
   authoritative and immediate, no failed attempt needed. Verified on this fleet:
   Ollama 0.34.4 answers `nomic-embed-text:latest → capabilities=['embedding']`, and
   `gpt-oss:120b → ['completion','tools','thinking']`.
2. **What a backend actually refused**, learned at runtime from a 400. Covers nodes
   whose Ollama reports no capabilities at all. Moved from a `StreamingProxy`
   instance attribute to module level with `get_non_generatable_models()`, matching
   the `get_context_protection_events` pattern the stateless health engine already
   uses for cross-module state.

**Why both:** `model_has_capability` is presence-only *by contract* — "False means
unknown", because Ollama 0.33.x under-reported capabilities. Treating absence as
"embedding" would have silenced every genuine miss on an older node. A test pins
that an unknown model is still reported.

**Normalization matters here and nearly slipped through.** Ollama keys its metadata
`name:tag`; a priority list carries whatever name the client used. `_model_meta`
normalizes, so a bare `nomic-embed-text` resolves against `nomic-embed-text:latest`
metadata — the first version of the test keyed the fixture bare, so the capability
signal silently never matched and the test failed for the right reason.

Verified live: the nomic card is gone, the other five checks are unchanged, and an
unloaded chat model is still flagged.

---

### The stale reaper killed the slot of a healthy long-running request `FIXED` (2026-10-06)

**Severity:** medium, and rising with load — the trigger is a fixed time limit on
a system whose decode rate swings 3–10x.

```
04:31:52  Enqueued 15b9be80 to bb:gpt-oss:120b (depth=2)
04:42:38  WARNING  Reaped stale in-flight 15b9be80 (stuck for 645s)
04:43:53  Completed 15b9be80 on bb in 720.2s (prompt=1943, completion=9610)
```

The request was never stuck. It produced **9,610 tokens** and finished normally 75
seconds after being declared dead. Three consequences:

1. **Its concurrency slot was released while it still held one**, so herd briefly
   ran 5 real in-flight against a cap of 4. Only consequential since 2026-10-02,
   when the cap started actually binding — before that it bounded nothing anyway.
2. **Queue stats and traces disagreed**: `bb:gpt-oss:120b` reported `failed=1` for
   a request `request_traces` records as `completed`.
3. A standing WARNING card and health score 85 → 75 for correct behaviour. The
   third instance of that pattern in a week, after `priority_model_not_loaded` on
   embedding models and `context_waste` on deliberately pinned ones.

**Root cause:** the reaper's only signal was `started_at`, so its test was
`now - started_at > stale_timeout`. That cannot distinguish a slow stream from a
dead one — and the code comment said it existed for "a request whose stream was
never consumed (so no `mark_*` ever runs)", which is exactly *not* this case. The
intent was right; the implementation could not express it.

**And it was getting worse, not better.** The threshold is 600 s:

| | gpt-oss decode | time for an 8,080-token reply |
|---|---|---|
| 2026-09-29 | 76.5 tok/s | ~106 s |
| 2026-10-06 (saturated) | 24.5 tok/s | ~330 s |

The load documented on 2026-10-05 is what walked legitimate work toward a fixed
limit. An absolute timeout is the wrong *shape* for this: nothing about 600 s
describes a wedged request, it only describes a slow one on a busy box.

**Fixed:**

- `QueueEntry.last_progress_at`, stamped by **both** streaming loops in
  `streaming.py` — the retry path at ~line 491 is the easy one to miss, and a
  test counts the stamps rather than trusting that.
- The reaper now tests `now - (last_progress_at or started_at) > stale_timeout`.
  The fallback to `started_at` is deliberate: a request that has produced
  *nothing* is still reaped on age, which is the genuine zombie this exists for.
- Reaper events record `stuck_seconds`, `age_seconds` **and** `produced_output`,
  because the difference between idle and age is the diagnosis — idle ≈ age means
  it never produced anything, idle << age means it stalled mid-stream. One number
  cannot say which.
- Removed a silent drift: `_STALE_IN_FLIGHT_SECONDS = 900` carried a "15 minutes"
  comment while `ServerSettings.stale_timeout = 600.0` always won, so the constant
  was dead code *and* the comment was wrong by five minutes. Renamed to
  `_STALE_NO_PROGRESS_SECONDS`, aligned at 600, with a test pinning the two equal.

Tests pin both directions. A reaper that stops false-positiving by never firing
would be worse than the bug, so genuine zombies — never produced output, or
produced some then went silent for 900 s — are asserted to still be reaped.

---

### A timed-out image render kept running and wrote output after cleanup `FIXED` (2026-10-05)

**Severity:** medium — an orphaned renderer holds GPU and memory on a node with
nothing left to reap it, and this project has precedent: `mlx_lm.server` orphans
held ports 11440/11441 for hours (2026-04-27).

Reported as [#6](https://github.com/geeks-accelerator/ollama-herd/issues/6) by
Krivo-dero with a standalone harness, and confirmed exactly as described.
`generate_image` awaited `asyncio.wait_for(proc.communicate(), timeout=180.0)` and,
on `TimeoutError`, returned 504 without signalling the child. **`wait_for` cancels
the await, not the process.** Two consequences:

1. mflux kept running after the request was answered, unreaped.
2. The `finally` unlinked `output_path` while the child was still alive, so mflux
   wrote its PNG *after* cleanup — a stray file left on disk.

The reporter's own table, which the new tests reproduce:

| case | response | child alive on return | files after child exits |
|---|---|---|---|
| normal completion | 200 | no | 0 |
| timeout (before fix) | 504 | **yes** | **1** |

**Fixed in `node/image_server.py`:**

- `_reap_renderer(proc)` — SIGTERM, bounded wait, SIGKILL, bounded wait. Bounded
  because it runs in the request's `finally`, where an unbounded wait would hang
  the handler it is cleaning up after. It never raises: a failed reap must not
  replace the response the caller is already getting.
- **Called from `finally`, not from `except TimeoutError`.** That is the part worth
  keeping: client disconnect raises `CancelledError` through the same `finally`, so
  an abandoned render is reaped by construction rather than needing its own branch.
  The reporter asked whether there was an existing renderer lifecycle contract to
  align with — there wasn't; this establishes one.
- **Reap before unlink.** The ordering *is* the defect; deleting first and reaping
  second still leaves the stray file. A test pins the ordering, not just the calls.
- `proc = None` before the `try`, so a failure in `create_subprocess_exec` itself
  reaches cleanup instead of raising `NameError` from the `finally`.
- `IMAGE_TIMEOUT_S` replaces the hardcoded `180.0`; the log line quoted "180s"
  literally, so changing one without the other would have reported a wrong number.

Only one instance of the pattern exists in the codebase (verified by grepping
`wait_for(.*communicate())`), so no sibling fix was needed. Tests use a real
subprocess, including one that ignores SIGTERM, because the bug is about signal
delivery and reaping and a mock proves nothing about either.

---

### `brew install` fails on macOS 27 while building `flit_core` `OPEN` (not reproduced)

**Severity:** high for affected users — install is the first thing anyone does.

Reported as [#7](https://github.com/geeks-accelerator/ollama-herd/issues/7) by
kerkenit against 0.10.0 on macOS 27. The real error, buried under ~200 lines of
Homebrew sandbox dump:

```
ERROR: Failed to build 'flit_core' when getting requirements to build wheel
ERROR: Failed to build '.../aiosqlite-0.22.1' when installing build dependencies
```

`aiosqlite 0.22.1` declares `requires = ["flit_core >=3.8,<4"]`, so under
Homebrew's `pip install --no-binary=:all:` pip must obtain and build `flit_core`
before it can build `aiosqlite`.

**Not reproduced on macOS 26.3.1 / Homebrew 7.0.7.** Everything below passed here:

- fresh tap, trust revoked with `brew untrust` and re-granted
- **both** pip caches moved aside (`~/Library/Caches/pip` and
  `~/Library/Caches/Homebrew/pip_cache`) — a genuinely cold machine
- the reporter's exact pip flags, including `--uploaded-prior-to=P1D`
- `flit_core>=3.8,<4` built from sdist (resolves 3.12.0, builds clean)
- `aiosqlite 0.22.1` built from sdist
- full `brew install` end to end, EXIT 0

Two hypotheses were tested and **eliminated**: a warm `flit_core` wheel in
Homebrew's pip cache masking the failure (install still succeeds with it removed,
so pip can reach PyPI from inside the build sandbox), and `--uploaded-prior-to=P1D`
changing resolution (no effect).

**Their log also contains a Homebrew bug, not ours:**
`Pathname not allowed in JSON (JSON::GeneratorError)` raised from
`Utils.report_forked_child_error`. Homebrew crashed *inside its own error
reporter* while printing the child failure, so the actual `flit_core` error text
was very likely never shown. That may be the only reason this looks mysterious.

**Known fragility, worth fixing regardless:** the formula vendors **zero** build
backends — no `flit-core`, `hatchling`, `setuptools` or `poetry-core` resource
blocks among its 39. Every source build therefore depends on pip fetching backends
mid-build. That works here, and Homebrew's own Python formulae vendor them anyway.
Vendoring would make the install immune to whether the sandbox permits that fetch.

**Not applied on a guess.** It is a plausible fix for an unconfirmed cause, and
adding ~17 resource blocks to a formula that currently installs correctly can
break the thing that works. Next step is to ask the reporter for `brew config`,
`brew doctor`, and `brew install --verbose` output so the masked `flit_core` error
is visible before changing anything.

---

### Importing a CLI module injects the operator's env file into the process `OPEN`

**Severity:** medium — latent, but it fails in the direction that is hardest to
diagnose: green on CI, red on a configured machine.

`cli/server_cli.py` and `cli/node_cli.py` call `load_env_file()` at **module import
time**, by design, so `FLEET_*` vars work under launchd and non-interactive shells.
The side effect is that merely importing either module mutates `os.environ` for the
rest of the process. On this machine that is **28 variables**, including
`FLEET_DYNAMIC_NUM_CTX`, `FLEET_NUM_CTX_OVERRIDES` and `FLEET_NODE_NODE_ID`.

Hit on 2026-10-03 while adding a test that used `inspect.getsource(server_cli.start)`
to pin a call shape. The import broke **four unrelated tests** —
`test_node_settings_defaults`, two dashboard settings-API tests, and
`test_no_num_ctx_unchanged` — and every one of them **passed in isolation**, because
pollution only exists once the CLI module has been imported earlier in the session.

The trap is the asymmetry: CI has no `~/.fleet-manager/env`, so a test that imports
the CLI is green there and red for any operator with a configured fleet. Verified both
ways — the suite passes with the env file moved aside and with it restored, but only
because the offending test was rewritten to read the CLI sources from disk instead of
importing them.

**Worked around, not fixed.** No test currently imports a CLI module;
`tests/test_models/test_cli_env_precedence.py` reads the files as text and carries a
comment explaining why.

**Proposed fix,** in rough order of preference:

1. A session-scoped autouse fixture in `tests/conftest.py` that snapshots and restores
   `os.environ`, so no test can leak env to another regardless of cause. This also
   covers the general case, not just the CLI.
2. Move `load_env_file()` out of module scope into the typer callback, so importing is
   inert and only *running* the CLI loads env. Changes nothing at runtime — the
   callback runs before `ServerSettings` instantiates, which is the only ordering
   requirement (`common/env_file.py` documents it).
3. Have `load_env_file()` no-op when `PYTEST_CURRENT_TEST` is set. Cheapest, and the
   worst of the three: it makes production and test behaviour diverge silently.

Option 2 plus option 1 is the real fix. Neither is urgent while nothing imports the
CLI, but the next person to write a CLI test will rediscover this.

---

### herd measured everything except itself `FIXED` (2026-10-02)

**Severity:** high — it is the reason a 28 GB process leak on two separate devices
left no diagnosable history on either.

Heartbeats carried *system* memory only. On a 512 GB box that number is dominated
by Ollama's resident weights (~91 GB) plus two `mlx_lm.server` children at 17 GB
each, so a 20 GB leak inside `herd-node` is invisible in it. When the native
embedding server held 28 GB and took a 48 GB Mac down with its co-tenants, there
was no recorded number anywhere to show the growth — and by the time anyone looked,
the processes had been restarted and the evidence was gone.

Worse, the system-memory series *looked fine*: over the 7 days to 2026-10-02 this
fleet oscillated 205–246 GB with no monotonic climb, and the two hours before a
suspected incident were among the lowest in the window. That reading was built
partly on `(normal)` memory pressure, which was itself unconditional on macOS (see
the entry above).

**Fixed** with `common/process_memory.py` → `HeartbeatPayload.process_memory`:

- **Footprint, not RSS.** `phys_footprint` is what the kernel charges the process
  and what Activity Monitor shows; `rss_gb` is kept only as the portable floor off
  macOS. (Measured together here they came within 2% — 3.684 vs 3.773 GB — so RSS
  is not useless, but footprint is the metric to reason about.)
- **`peak_gb` is the diagnostic field.** ONNX Runtime keeps the high-water mark of
  the largest run a process ever does, so one oversized request raises the floor
  permanently and current usage afterwards tells you nothing. A peak far above
  current is reported as `retained` and called out in the check text; current-only
  monitoring is precisely how this class of bug reads as "fine now".
- **The agent's own process is the signal.** The vision and text embedding servers
  are `asyncio.Task`s inside it (`_ensure_embedding_server` /
  `_ensure_text_embedding_server`), **not** subprocesses, so their ONNX arenas are
  charged to the agent. Children are reported separately and labelled by **argv**,
  not process name — the transcription server reports as `Python` and the mlx
  children as `python3.14`, so a name tells an operator nothing.
- **`psutil.memory_full_info()` is `AccessDenied` on macOS without root**, the same
  wall `backend_clients` hit with `net_connections()`. `/usr/bin/footprint -p <pid>`
  works as the ordinary user at ~50 ms, hence a 60 s TTL rather than a probe on
  every 5 s heartbeat.
- Visible three ways, deliberately: the periodic heartbeat log line (`self=…GB
  (peak …)`) gives greppable long-run history in the file that already has it;
  `/fleet/status` exposes it unconditionally, not only once it is a problem, because
  the *trend* is the point; and the health check fires on a threshold.

Verified end-to-end rather than asserted: baseline 0.112 GB → 3.684 GB after 1,920
embeddings through the router, with the two mlx children correctly labelled and
correctly excluded from the threshold. A probe that reports a constant is
indistinguishable from a broken one, which is why the test suite also includes an
unmocked read of the live process.

**Still missing:** the router's own number is checked (it probes itself, since no
heartbeat describes it) but is not persisted anywhere, so it has no history. And
nothing writes these figures to `request_traces` or any time series — the heartbeat
log line is the only durable record, and it rotates daily.

---

### macOS memory pressure is always reported `normal` `FIXED` (2026-10-02)

**Severity:** high. The safety logic built on it has never run on macOS, herd's
primary platform.

`common/system_metrics.py::_get_memory_pressure_darwin` runs `memory_pressure -Q` and
looks for "critical" or "warn" in the output. `-Q` prints only `System-wide memory free
percentage: N%`, so the function returns NORMAL unconditionally. On 2026-10-02 the Mac
mini reported `normal` with swap at 50.9/51.2 GB and load 340. The scorer's CRITICAL
elimination (`scorer.py`, `routes/routing.py`) and the health engine's pressure check
(`health_engine.py`) have therefore never fired on a Mac.

**Proposed fix:** read the kernel's level, `sysctl -n kern.memorystatus_vm_pressure_level`:
1 = normal, 2 = warn, 4 = critical. This is the signal behind Activity Monitor's pressure
graph. Map it directly and add a test that pins the mapping.

**Decide before enabling:** on a one-node fleet, CRITICAL elimination means herd refuses
every request while memory is critical. That is the designed protection, but it is
behavior no Mac has ever exhibited.

**Fixed 2026-10-02**, probe and consequence together, because shipping the probe alone
would have turned "memory is tight" into "fleet is down" on its first critical reading.

*Probe:* `_get_memory_pressure_darwin` now reads
`kern.memorystatus_vm_pressure_level` and maps 1/2/4 directly. Unknown or unreadable
values fail open to NORMAL — deliberately, because a wrong CRITICAL now withholds cold
loads, and degrading routing on the strength of a parse failure is worse than missing
one reading. `_check_memory_pressure` already existed in the health engine and fires
WARNING/WARN and CRITICAL as soon as the level is not normal, so the alarm came for free.

*Consequence — the elimination was the wrong shape, not just untested.* Blanket
elimination freed **nothing**: the memory is held by Ollama's resident weights, not by
herd's queue, so refusing a request unloads no model. What refusal does prevent is
loading something **new** — on this fleet a 66 GB `gpt-oss:120b` cold load landing on a
machine already in trouble. So critical pressure now withholds cold loads and keeps
serving what is resident. Per site:

| Site | Before | Now | Why |
|---|---|---|---|
| `scorer.score_loaded_models` | eliminate node | **no pressure check** | Only ever considers HOT models, by its own docstring. Eliminating here refused exactly the safe requests, and emptied auto-routing on a pressured one-node fleet. |
| `scorer._eliminate(model)` | eliminate node | eliminate **only if the model is not resident** | The split. Serve resident weights, withhold the cold load. |
| `routing._pick_pull_node` | eliminate node | **unchanged** | Its whole job is choosing a node to pull a model onto, so every candidate *is* a cold load. Correct already. |

Residency uses `serializers.model_resident_on_node`, moved there from
`model_preloader` (which re-exports it) so the scorer and the preloader cannot drift on
what "resident" means; it covers Ollama `models_loaded` and healthy `mlx_servers`.

`tests/test_server/test_memory_pressure_gating.py` pins the 1/2/4 mapping, pins the
*command* (a change back to `memory_pressure -Q` would silently restore "always
NORMAL"), asserts the real `-Q` text contains neither word the old code searched for,
and includes a live non-mocked read — the old version passed every mocked test while
being unconditionally wrong in production, which is how it survived this long.
`test_scorer.py` carries both branches of the split; the test that asserted the old
blanket behavior was replaced, not deleted quietly.

---

### Native embedding server kept its peak activation memory forever: 28 GB in one node agent `FIXED` (2026-10-02)

**Severity:** high. It took a 48 GB Mac out of memory together with its co-tenants.

ONNX Runtime keeps the high-water mark of the largest run a process does, and that
mark is batch x seq_len^2. The fastembed server allowed 8,192-token nomic inputs (4x
Ollama's 2048 context), flat batches of 32, and concurrent runs on one session, so the
mark was effectively unbounded.

**Fixed in `node/text_embedding_server.py`:**

- truncation to the registry's `max_tokens` (2048), with `"truncate": false` returning
  a 400 as in Ollama
- `_plan_batches` sizes batches from the longest input against `_ATTENTION_BUDGET`
- `_run_onnx` runs one job at a time per model

Measured peak with both models under the reproducing workload: 3.71 GB, previously more
than 8 GB within minutes. `prompt_eval_count` and rerank `usage.total_tokens` are now
real token counts from the model's tokenizer rather than word counts. Under word
counting, an unspaced 8K-char input recorded as 1, which bears directly on the
embed-latency gap in the entry below.
Evidence: `docs/observations.md` (2026-10-02).

---

### Embed traces recorded no request size, making embed latency unexplainable `FIXED` (2026-10-01)

**Severity:** medium (observability). Fixed.

Every embed trace had `prompt_tokens` NULL — 3,770 rows on the reference fleet. The
backend already computes the value (`node/text_embedding_server.py`:
`prompt_eval_count = sum(len(t.split()) for t in texts)`), returns it in the response,
and `/v1/embeddings` reports it as OpenAI `usage`. It simply was never passed to
`record_trace`, which recorded only status and latency.

**Found by an investigation it blocked.** On 2026-10-01 `nomic-embed-text:latest`
averaged 324 ms while the bare `nomic-embed-text` averaged 1,624 ms. Everything
checkable came back identical: both names are in `TEXT_EMBEDDING_MODELS` so both route
to the native fastembed server on :11439, both on node `bb`, both tagged
`["embed","text-embed"]` from `127.0.0.1`, and median concurrent LLM load was **0.0 for
both**, ruling out contention. Direct measurement showed fastembed is fast and barely
text-length sensitive (19 ms for 200 chars, 22 ms for 8,000; 2.4 ms/text at batch 64),
so batch size was the only remaining explanation — **and there was no recorded way to
test it.** The debug-body capture does not cover the embed path either.

Now recorded, verified live: batch 1 → 5 tokens / 42 ms, batch 25 → 125 / 77 ms,
batch 100 → 500 / 233 ms. The same question will answer itself from traces next time.

**Followed up 2026-10-02 with the recorded data. The batch-size inference above was
wrong — it is inverted.** Over an 8 h window:

| spelling | n | mean latency | mean `prompt_tokens` | range |
|---|---|---|---|---|
| `nomic-embed-text` | 115 | **1,441 ms** | **1.0** | 1–1 |
| `nomic-embed-text:latest` | 66 | **127 ms** | 267.4 | 47–1,295 |

The *slow* spelling sends **one token** and the fast one sends up to 1,295. Within
`:latest`, latency scales sensibly with size (80 ms at ≤100 tokens, 178 ms at 101–500,
323 ms above 500), so batching behaves exactly as measured — it just is not what
separates the two names.

**Routing is now positively ruled out, not merely "checked".** Sending identical input
under both spellings returns identical results on the same path: 27–74 ms, 768 dims,
`prompt_eval_count=45` for both. A 1-token input by hand takes 33–42 ms. So the names
are interchangeable and the native server is fast for both.

**What the gap tracks is *when* each workload arrives**, which the earlier "median
concurrent LLM load was 0.0 for both" reading missed by using the median:

- plain: **105 of 115 (91%)** arrived with an LLM request in flight → mean 1,508 ms;
  the 10 that arrived idle → 738 ms.
- `:latest`: only **11 of 66 (17%)** overlapped an LLM request.

So these are two different callers with different timing, not two routing paths.
**But co-occurrence is not yet causation, and the counter-evidence is in the same
table:** the 11 `:latest` requests that *did* overlap an LLM ran in **83 ms** — faster
than its idle ones. LLM concurrency alone therefore does not produce 1,508 ms, and a
1-token embed by hand does not either. The residual is unexplained; the plain-name
caller also self-overlaps more (mean 1.73 concurrent vs 1.11), so the leading
hypothesis is now that specific caller's burst pattern (threes, ~15 s apart) rather
than anything about the model name. **Do not close this as "batch size" or as "LLM
contention" — both have now been measured and neither holds alone.**

**2026-10-02, later: `prompt_tokens = 1` was a *word* count.** Until the memory fix of
the same day, `prompt_eval_count` was `len(t.split())`. A single long string with no
whitespace (base64, minified JSON, a joined list) therefore recorded **1**, however
many thousand real tokens it held. That fits this table better than arrival timing
does:

- **Every** plain-name request is exactly 1.
- Its *idle* requests still took 738 ms, against 33–42 ms for a true 1-token embed.
  That is the cost of a long sequence.
- The Mac mini shows the same signature on a different fleet: 147 embeds recorded as
  `prompt_tokens = 1` averaged 1,466 ms.
- On that machine, before truncation, one 8K-char unspaced input measured 2.8 s.

Such input used to run at up to 8,192 tokens, with quadratic attention cost; it now
truncates at 2,048 (see the 28 GB entry above). `prompt_tokens` is now the tokenizer's
real count, so post-fix traces settle this. If the plain-name caller shows hundreds to
thousands of tokens, the gap is input size. **Leading hypothesis, not yet confirmed.**

---


### Requests rejected before a node is chosen left no trace `FIXED` (2026-09-29)

**Severity:** was high (observability). Fixed.

`record_trace` only runs after a routing winner is selected, so every rejection path
returned an HTTP error to the client and recorded **nothing**. On 2026-09-28 four
requests hit the 30s holding-queue timeout and got 503s, while that day's traces held
**zero** non-completed rows out of 8,140. The dashboard reported 100% success, and
several status reports in this repo repeated that figure — they were measuring only
requests that reached a node.

Five sites across four route files (`ollama_compat` x2, `openai_compat`,
`anthropic_compat`, `responses_compat`). Fixed with one shared
`record_routing_rejection()` in `routes/routing.py` rather than five copies, writing
`status="rejected"` — distinct from `"failed"` (a node *was* chosen and the backend
errored) so the two causes stay separable, and outside `completed`/`retried` so it
counts toward the error rate, which is correct: the client did get an error. Trace-write
failures are swallowed, because a trace problem must never turn a 503 into a 500.

**Wiring it in immediately produced a second, subtler bug — worth recording.** The
per-node error-rate check groups by `node_id`, and rejections carry `node_id=''`
because no node was chosen. So a single rejection invented a phantom node:
`High error rate on ␣ — 100.0% error rate (1/1 requests failed)`, advising *"check
connectivity and Ollama health on ␣"*. That is precisely the wrong diagnosis — a
request nothing could be routed to is a placement or availability problem, not a node
fault. `get_error_rates_24h` now excludes `node_id = ''`.

Covered by `tests/test_server/test_routing_rejection_traces.py`, including a guard that
fails if a route module grows a `if not results:` block without a matching
`record_routing_rejection()` call, so a new API surface cannot silently reintroduce the
blind spot.

**Consequence for reported numbers:** success rates will no longer read 100% when
rejections occur. That is the point — the previous figure was flattering rather than
accurate.

---


### A `num_ctx` override that can never apply is visible, and deliberately not enforced `FIXED` (2026-09-29)

**Severity:** medium — silently wastes KV memory and defeats `FLEET_NUM_CTX_OVERRIDES`.

Observed repeatedly on 2026-09-28. `gemma3:27b` is configured for 32768, and the
router injects that correctly. But it intermittently ends up **resident at 131072**
(the Ollama default), and once that happens the router does not self-correct:

```
Dynamic num_ctx: override num_ctx=32768 for gemma3:27b cannot apply
  -- already resident at 32768. Shrinking would force an unload/reload...
```

while the backend is demonstrably at 131072:

```
ps -Ao args | grep llama-server   ->  -c 524288 -np 4   (= 131072 per slot)
```

So the router refuses to re-apply the override *because of a stale belief about the
current value*. It stayed wrong across several requests, and cost 28 GB of
unnecessary KV (139.3 GB resident vs 112.0 GB after a manual reload).

**Two separate defects here:**

1. **Something loads the model at the Ollama default.** The preloader passes
   `num_ctx` correctly (`pre_warm` was verified to send it), and the streaming path
   injects it — but some path still reaches Ollama without it, most likely a request
   arriving for a cold model outside the dynamic-num_ctx injection path. Worth
   instrumenting which caller wins the load.
2. **The "already resident at X" check trusts a cached value.** It should compare
   against what the node last actually reported, and the node should report the
   *per-slot* context. Note `ollama ps` reports a number that has already been
   divided differently than the launch args: gemma3 showed `CONTEXT 131072` while
   running `-c 524288 -np 4`. **Verify per-slot context from the launch args, not
   `ollama ps`** — see the `OLLAMA_CONTEXT_LENGTH` gotcha in CLAUDE.md.

**Workaround until fixed:** `ollama stop <model>` then send one request through the
router, which reloads it with the override applied. Confirm with the launch args.

**Why it matters beyond memory:** an oversized resident model is exactly what made
Ollama predict 341.7 GiB and deadlock on 2026-09-22. This bug can recreate the
precondition for that hang on its own.

**Resolution.** The diagnosis in the paragraph above was partly wrong and is corrected
here for the record: there was no stale *cache*. `_get_loaded_context` reads the
heartbeat, which was accurate. The misleading part was `_log_override_inert_once`
deduping by **model name alone** — it fired once when the model genuinely was at 32768,
then went permanently silent when the model later reloaded at 131072. The newest line
in the log therefore reported a context that had not been true for hours, which reads
exactly like a stale cache and cost real debugging time.

Two changes:

1. **`streaming.py`** — dedupe by `(model, loaded_ctx)`, so a context change produces a
   fresh line with correct values. The message now quantifies the waste
   (`4.0x the configured context`) and gives the actual remedy (`ollama stop <model>`,
   then one request through the router) rather than "deferred to the next cold load"
   with no way to cause one. It also records an `override_inert` event.
2. **`health_engine.py`** — new `num_ctx_override_inert` check (41 distinct checks now).
   A log line is not a state: while the override is inert the fleet runs with the wrong
   KV allocation *indefinitely*, and nothing triggers the cold load that would fix it.
   The check reads live node state so the card clears once corrected, and rates
   oversized as WARNING (wastes KV, inflates Ollama's memory prediction, can wedge a
   later load) versus undersized as INFO.

**Root cause (2026-09-29), from captured request bodies.** `FLEET_DEBUG_REQUEST_BODIES`
showed it exactly: of 203 gemma3 requests over four days, **199 reached Ollama with no
`num_ctx` at all** — only the 4 manual fixes carried 32768. Two individually-correct
branches of `_apply_context_protection` interlock into a loop:

1. Injection is skipped when `override <= already_loaded_ctx` (avoids emitting a value
   the strip branch would remove — it once produced 393 injected/393 stripped pairs in
   9 hours).
2. The strip branch removes any `num_ctx <= loaded_ctx`, to avoid forcing a reload.

The emergent behaviour: the override is "deferred to the next cold load", but the
request that *causes* the next cold load carries no `num_ctx` by rule 1 — so it cold-loads
at Ollama's default again. **Once a model lands at the wrong context it stays there
permanently.** The entry point is a ~5s race: `models_loaded` refreshes on the heartbeat,
so if Ollama evicts a model and a request arrives inside that window, herd still believes
it is resident and skips injection on precisely the request that will reload it.

**Decision: do not enforce it.** Forcing the override on a resident model means an
unload/reload, which is the multi-minute stall context protection exists to prevent, and
the trade is not worth it — serving at a *larger* context is functionally identical (gemma3's
prompts average 495 tokens; 32K vs 131K is indistinguishable), and on this fleet there is
81 GB free with zero swap. The cost is KV cache only.

**So the fix was to the reporting, not the behaviour.** Severity now follows actual impact:
INFO when there is memory headroom, WARNING only when the waste threatens capacity
(`_free_memory_gb(nodes) < _OVERSIZE_HEADROOM_GB`). The first version warned
unconditionally, which reported a deliberate engineering trade as breakage.

**Residual risk, accepted and recorded:** each cold load at the larger context makes Ollama
predict `context x OLLAMA_NUM_PARALLEL` of memory and take its evict-first path —
observed three times on 2026-09-28 as `predicted="341.7 GiB" ... evicting`. That is the same
path that hung instead of erroring on 2026-09-22, when the peer model was `KEEP_ALIVE=-1`
and could not be evicted. It succeeds while there is headroom; it is not free.

Covered by `tests/test_server/test_num_ctx_override_inert.py`. Worth noting how the
first version of those tests failed: they invented `_settings` and `_registry`
attributes on `HealthEngine` and passed green while the production path raised
`AttributeError` — the engine is stateless and `analyze()` takes registry and
trace_store as arguments. 30 unrelated tests caught it. Settings now come from env,
matching `_check_anthropic_map_targets`.

---

## Performance

### `context_waste` recommended the change that caused a six-day regression, and the optimizer could apply it `FIXED` (2026-10-02)

**Severity:** high — the automation half was one dashboard toggle from
re-running a known incident, with no operator action beyond flipping it.

Found while auditing why `context_waste` had been a standing WARNING on this
fleet. Three separate findings, in the order they came out:

**1. The premise I started from was wrong.** CLAUDE.md's co-tenancy gotcha said
`prompt_tokens` records `prompt_eval_count`, "i.e. cache misses, NOT full
context", so a 77K request "can look like 2K in traces". If true, every
context-sizing number herd computes is deflated by however well prefix caching
is working, and `context_waste` would be systematically inventing waste. It is
**not** true. Sending an identical prompt twice to Ollama 0.34.4:

```
run 1 (cold): prompt_eval_count=4074  prompt_eval_duration=2.235s
run 2 (warm): prompt_eval_count=4074  prompt_eval_duration=0.021s
```

A 106x drop in prefill duration — the cache was definitively hit — and the count
did not move. `prompt_eval_count` is the full prompt length. The original note
appears to have conflated "absent from traces" (a bypassing client's request,
which never reaches `request_traces` at all) with "deflated in traces" (herd's
own). Corrected in CLAUDE.md with the reproduction, because believing it makes
trace data look useless for the thing it is actually good for.

A second claim in the same gotcha — that fully-cached prompts may emit no `new
prompt` line — is also wrong on 0.34.4: both runs logged one. That makes the
backend/router reconciliation tripwire *tighter* than documented, and explains
why 674 traces reconciled against 679 backend lines over the same 2.4 h window.

**2. So the measurement is sound, and the recommendation is still wrong.** With
`prompt_tokens` correct, gpt-oss:120b genuinely does see p99 ~5.3K prompts
against 131,072 allocated, and the check genuinely does compute 16,384. But that
reduction was *already tried* on this fleet. On 2026-09-22 the per-slot context
went 131072 -> 32768 — still 23x the p99 prompt, comfortably fitting by this
check's own arithmetic — and prefix-cache reuse collapsed (5,772 -> 770 hits),
TTFT went 1.0s -> 6.3s and total latency 5.3s -> 10.5s for six days, on the
model serving 99% of traffic, with **decode throughput completely unchanged**,
which is why nobody noticed. The router spent those six days requesting 131072
against a 32768-resident model and losing, so the operative hazard looks like
the requested/resident *mismatch* rather than the absolute size — but that
mechanism is inferred from logs, not proven, and that uncertainty is itself the
argument for not acting on prompt-size arithmetic alone.

**3. The automation would have reverted the fix for that incident.**
`context_optimizer._check_and_optimize` runs every 5 minutes when
`num_ctx_auto_calculate` is on (the "Auto-Calculate Context" dashboard toggle)
and overwrote **any** override, including one the operator set by hand. Simulated
against live trace data:

```
recommended      = 16384
alloc > rec*4    = 131072 > 65536  -> True
override > rec*2 = 131072 > 32768  -> True
=> would overwrite the explicit 131072 with 16384 and queue an Ollama restart
```

That override is the *fix* for the regression above, set on 2026-09-28. The
asymmetry is what makes this a bug and not a design decision:
`_auto_initialize_overrides` has always skipped a model that already has an
override ("keeping existing override"); only the periodic path did not.

**Fixes:**

- `ContextOptimizer` tracks `_auto_set` — the models whose override herd itself
  computed — and `_check_and_optimize` leaves everything else alone. herd manages
  what it created; it does not manage what it was told. An override herd set may
  still be revised, so the feature keeps working.
- `context_waste` reads `FLEET_NUM_CTX_OVERRIDES` (via a shared
  `_operator_num_ctx_overrides` helper, so it and `num_ctx_override_inert` cannot
  name different targets from the same data), marks pinned models
  `operator_pinned`, and recommends no change for them — stating the memory cost
  and the prefix-cache caveat instead.
- Severity now follows what is *actionable*: INFO when every oversized model is
  pinned. A standing WARNING with no available action is how a board stops being
  read, which is how both the 32768 regression and the trace-write failures hid.
- The fix text says to verify prefix-cache hits and TTFT after a context change,
  **not** decode throughput — decode is the one metric a bad context change
  leaves untouched.

On this fleet `context_waste` went WARNING -> INFO on restart, with both models
correctly identified as deliberately pinned.

**Not changed:** the 4x/8x thresholds, and gemma3:27b's 178x ratio. Those
numbers are real; the problem was never the measurement.
### Native embedding models never unload, so herd-node keeps ~3.7 GB after first use `OPEN`

**Severity:** medium on small nodes (48 GB and under), negligible on the Mac Studio.

After the 2026-10-02 fix, the native text server's memory is *bounded* but not
*released*. Once nomic and the default reranker have each served one long input,
`herd-node` holds about 3.7 GB until it restarts: about 1.9 GB for nomic, 1 GB for the
reranker, plus memory-pattern copies. Measured live through the router: 0.09 GB → 3.71
GB, flat thereafter. That is the designed ceiling, set by `_ATTENTION_BUDGET` and
ONNX Runtime keeping its high-water mark. On a node that also runs a 27B model plus
ordinary apps, it is a meaningful share of RAM, spent on a model that may serve a
handful of requests a day (about 200 in 11 hours on the Mac mini).

**Now measurable from telemetry (2026-10-02).** The node reports its own process
memory in the heartbeat (`HeartbeatPayload.process_memory`, from
`common/process_memory.py`), so this retention no longer needs a manual `footprint`
invocation to see. Confirmed independently through the router: the agent went
**0.112 GB → 3.684 GB** over 1,920 embeddings (60 requests, batch 32, long inputs)
and stayed there — matching the 3.71 GB measured by hand, from a different direction.

`peak_gb` is reported alongside current because ONNX Runtime keeps the high-water
mark of the largest run a process ever does: one oversized request raises the floor
permanently, and *current* then reads innocent. A `herd_process_memory` health check
fires WARNING above 8 GB and CRITICAL above 16 GB for the agent's own process —
children are excluded from that comparison on purpose, since two `mlx_lm.server`
processes at 17 GB each are legitimate model weights and would trip any useful
threshold. 8 GB is roughly double the designed ceiling and is also the figure from
the incident, which passed "more than 8 GB within minutes" on its way to 28 GB.

**Proposed fix:** evict an idle model from `_slots` after a few minutes without
requests, and let the next request lazy-load it again. It is about 130 MB from the
local cache, and the load cost is already paid today on the first request after a
start. **Verify before relying on it** that dropping the fastembed object actually
returns the arena to the OS on macOS. Measure with `footprint`, not RSS: that is the
mistake that hid the original 28 GB. The vision embedding server (`embedding_server.py`)
has the same lifetime and would take the same treatment.

---

### Node memory oversubscription is invisible: no swap signal, and co-tenants are unaccounted `FIXED` (2026-10-04)

**Severity:** high on nodes shared with other workloads.

On 2026-10-02 the Mac mini (48 GB) was committed to about 81 GB and swap reached 50.9
of 51.2 GB:

| Process | Memory |
|---|---|
| gemma3:27b `llama-server` | 29 GB |
| `herd-node`, the leak fixed above | 28 GB |
| VM | 13 GB |
| Next.js dev server | 11 GB |

Herd's picture of that node was `used 13.22 / 48 GB, available 8.82 GB,
pressure=normal`. Three gaps let that happen:

- **Pressure never left NORMAL on macOS.** Fixed in 0.10.0 (`3cc423d`, reads
  `kern.memorystatus_vm_pressure_level`). Verified 2026-10-04: herd reported `warn`
  when the kernel said level 2.
- **The heartbeat has no swap or compressor figures.** Still open in 0.10.0.
  `MemoryMetrics.compressed_gb` is hard-coded `0.0` in `common/system_metrics.py`,
  nothing reads it, and there is no swap field. On 2026-10-04 herd showed `warn` and
  `available 9.92 GB` while swap sat at **96.6% (30.1 of 31.1 GB)**, and nothing in
  herd could say so.
- **The model's real footprint is about 12 GB larger than Ollama reports.** Explained:
  see the prompt-cache entry below.

**Sources, verified on macOS 26 (2026-10-04):**

- Swap: `psutil.swap_memory()` works on macOS, Linux and Windows. It returned total
  31.14 GB, used 30.08 GB, 96.6%, which agrees with `sysctl vm.swapusage`.
- Compressor: `sysctl -n vm.compressor_bytes_used` gives the RAM the compressor
  occupies (15.5 GB), matching `vm_stat`'s "occupied by compressor".
  `vm.compressor.pages_compressed` x page size gives what it holds logically (58.8 GB).
  Use the same subprocess pattern as `_get_memory_pressure_darwin`. psutil exposes
  neither.

**Fixed 2026-10-04:**

- `MemoryMetrics` gained `swap_used_gb` / `swap_total_gb`, defaulting to 0.0 so older
  agents validate. `compressed_gb` is now populated on macOS from
  `vm.compressor_bytes_used`; `_run_sysctl` is shared with the pressure probe.
- New `swap_usage` check: WARNING when swap in use reaches **half of physical RAM**.
  It does not use ~80% of swap *total*, as first proposed: macOS grows swap on demand,
  so used/total was 96.6% here at 30 GB and would read ~100% with 1 GB. Calibration:
  2026-10-02 after recovery was 36% (quiet, 77% of memory free), 2026-10-04 was 62%
  (fires; a 29 GB model fully paged out).
- The node card shows `swap · compressed` under the memory bar.
- **Also fixed: the card's memory-pressure outline could never appear.** The JS
  compared against `'warning'`, but the enum value is `"warn"`. That was invisible while
  pressure was always `normal` on macOS, and became a live bug once 0.10.0 made the
  signal real.

Live on the Mac mini after deploy, the heartbeat matched the OS: swap 11.19 / 12.0 GB
(`vm.swapusage` 11,457 MiB) and compressed 6.72 GB (`vm.compressor_bytes_used` 7.1 GB).
`swap_usage` was correctly quiet at 23% of RAM. Swap had fallen from 28 GB when
Ollama's restart released the paged-out gemma3. Firing is covered by
`tests/test_server/test_swap_visibility.py`.
psutil's `sin`/`sout` are deliberately unused: on macOS they are vm_stat's file-backed
pageins/pageouts, not swap. Not done, deliberately: naming the largest non-herd
processes, which needs a process scan per heartbeat. Add it only if the swap check
alone proves insufficient.

---

### llama-server's prompt cache holds up to 8 GiB per loaded model, unseen by Ollama and herd `OPEN`

**Severity:** high on small nodes, and it scales with the number of loaded models
everywhere.

Since Ollama shells out to upstream `llama-server`, every loaded model also runs
llama.cpp's **host-RAM prompt cache** (llama.cpp PR #16391). Its default limit is
`--cache-ram 8192` MiB *per process*, and Ollama does not pass `--cache-ram` at all.
gemma3:27b's server logged `prompt cache is enabled, size limit: 8192 MiB` at load.

**This is the "29 GB vs 17.7 GB" gap**, accounted for line by line from that
`llama-server`'s own log (`-c 32768 -np 1`, `load_mode = none`, so the weights are
anonymous memory and fully compressible):

| Part | Size |
|---|---|
| Weights, MTL0 + CPU | 15,768 + 1,103 MiB |
| KV, full + sliding-window | 2,560 + 624 MiB |
| Compute, text + vision | 556 MiB |
| **Prompt cache** | **~7,800 MiB** (15 prompts; peak 8,178 MiB) |
| **Total** | **≈ 28,400 MiB**, matching the observed 29 GB |

Ollama's `/api/ps` reported 17.7 GB. herd's own estimate was ~21 GB "@ 32768 ctx".
Neither includes the cache. The vision projector does *not* load the combined GGUF a
second time, despite `model size: 16586 MiB` in its log; it adds only compute buffers.

**On this workload the cache was pure cost.** Since load:

- 100 cache lookups and **0 hits**: `looking for better prompt` 100, `found better
  prompt` 0.
- 99 prompts saved at about 290 MiB each, for roughly 600-token prompts. gemma3's
  saved state includes the full sliding-window cache.
- The `restored context checkpoint` lines (87) are a different mechanism of 5–6 MiB
  each, unaffected.

**The lever exists without an Ollama change.** The bundled `llama-server` reads
`LLAMA_ARG_CACHE_RAM` (and `LLAMA_ARG_CTX_CHECKPOINTS`) from its environment, and
Ollama passes its environment down: the running server shows `OLLAMA_MODELS` and
`LLAMA_ARG_FIT_TARGET`. Setting `LLAMA_ARG_CACHE_RAM=0` the way the `OLLAMA_*` variables
are set (`launchctl setenv` plus `~/.zshrc`, then quit the Electron parent and relaunch)
should free about 8 GB per loaded model.

**Applied and verified on the Mac mini, 2026-10-04:**

- `LLAMA_ARG_CACHE_RAM=0` was set via `launchctl setenv` (persisted by
  `docs/examples/launchd/com.geeksaccelerator.ollama-env.plist`) and `~/.zshrc`, then
  Ollama was relaunched.
- The `ollama serve` and `llama-server` processes both carry it.
- The load log says `prompt cache is disabled`.
- Over three distinct prompts there were 0 `saving prompt` lines, and gemma3 held at
  **21.8 GiB, against 29 GiB before**.
- The Mac Studio is unchanged; measure its hit rate first.

**Reboot-proofed the same day.** It is also set in the node plist's
`EnvironmentVariables`, because at boot `herd-node` may spawn `ollama serve` with its
own environment before the env agent runs. Verified by simulation: `unsetenv`, Ollama
stopped, node agent started. The spawned `ollama serve` and gemma3's `llama-server`
both carried the value. See `docs/configuration-reference.md`.

**Proposed:**

1. Measure the hit rate per fleet before choosing a value: `grep -c "found better
   prompt"` vs `grep -c "looking for better prompt"` in `~/.ollama/logs/server.log`.
   The Mac Studio's agentic sessions may get real hits where this Mac got none; a cap
   such as 2048 is the middle ground.
2. Make herd's resident estimates include it. The preloader's memory gate and
   `measured_resident_gb` are low by up to 8 GiB per loaded Ollama model. The more
   direct fix is for `herd-node` to report each `llama-server`'s footprint, the same
   way 0.10.0 reports its own: ground truth instead of three different estimates.
3. ~~Document it in `docs/configuration-reference.md` § Ollama environment.~~ Done.

---

### herd pins every model forever, so under memory pressure an idle model goes to swap instead of unloading `OPEN`

**Severity:** medium. Wasted swap and slow first requests on constrained nodes.

herd sends `"keep_alive": -1` on every request and pre-warm (`streaming.py`:
`body.setdefault("keep_alive", -1)`, and the pre-warm body). The Mac mini has no
`OLLAMA_KEEP_ALIVE` at all, yet gemma3's `expires_at` reads **2319-01-13**. So a model
idle for hours is never unloaded. When memory gets tight, macOS cannot unload it either
and has to page it out.

That is expensive here specifically. Ollama loaded gemma3 with `load_mode = none` (nomic
got `mmap`), so its weights are anonymous memory, not clean file pages macOS could drop
and re-read from the GGUF. Measured 2026-10-04:

- gemma3's `llama-server` was **0.02 GB resident** of a ~29 GB footprint.
- Its last request was 10 h earlier (10-03 19:03).
- It sat in the compressor and swap alongside a Chrome tab (9.5 GB), Docker's VM
  (13 GB) and a dev server (8.6 GB): 58.8 GB compressed in all, with swap at 96.6%.

The system was **not** thrashing at that moment: 0 swapouts and 0 compressions in a
10 s window. The cost lands later. The next request pages ~17 GB of weights back in
from swap, where a cold load would be a sequential read of the file. Warm requests were
steady at TTFT ~6.3 s and decode ~14 tok/s.

**Proposed:** make pinning conditional on memory, using the pressure signal 0.10.0 made
real. While a node reports WARN or CRITICAL (or high swap, once reported), stop sending
`keep_alive: -1` for that node, and unload models idle past a threshold. An idle model
should leave memory by unloading, not by being swapped out. No flag (greenfield):
pinning when memory is plentiful and releasing when it is not is simply the correct
behavior. Check the interaction with priority pins and the preloader before building.

**Diagnosing swap correctly:** judge thrashing by the *live* Swapins/Swapouts and
Compressions deltas in `vm_stat` sampled seconds apart. `Pageins`/`Pageouts` count
file-backed paging, not swap, and the totals are cumulative since boot (here 24 days:
84M swapins vs 105M swapouts).

---

### Unexplained 2026-08-22 step change: p25 73.4 → 43.1, conc=3 68.0 → 50.6 `OPEN`

**Severity:** high (throughput). Present continuously since 2026-08-22.

On 2026-08-22 batched-decode throughput stepped down in a single day and has not
recovered in the five weeks since:

| | p25 | conc=3 | conc=1 |
|---|-----|--------|--------|
| Aug 21 | **73.4** | **68.0** | 76.5 |
| Aug 22 | 43.1 | 50.6 | 74.0 |
| Aug 23 – Sep 29 | 43–49 | **~52, flat** | 74–77 |

`conc=1` and `conc=2` are unaffected — they are at their seven-month best (76.8).
Only multi-stream batching lost ground.

**This was previously believed fixed, and that belief was an artifact of a bad
measurement.** A co-located client (`openclaw`) bypassing the router was found and
removed on Aug 23, and a 25-minute window (n=114 overall, **n=12** at conc=3)
showed p25 at 74.2 and conc=3 at 74.6 — reported as a full recovery. Whole-day
data contradicts it: removal moved p25 from 43.0 to 44–49 and left conc=3 flat.
**openclaw was worth roughly 5 points of p25, not 30.**

**Eliminated so far** (each by direct measurement, see `docs/observations.md`
2026-08-23 and 2026-09-28): the Ollama/llama.cpp version (0.32.13 vs 0.32.15
benchmarked identically on the real model), herd itself (direct-vs-routed A/B),
workload volume and token mix, routing, memory and swap, power/thermal, env drift,
client parallelism, co-tenancy (the tripwire now reads exactly clean daily), and
`OLLAMA_CONTEXT_LENGTH` (that was a separate, self-inflicted TTFT regression).

**NEW, 2026-10-02 — the strongest candidate yet, and it invalidates part of this
issue's framing.** Queue concurrency never limited backend in-flight requests, from the
initial commit until `24802b0`: `QueueManager._worker` called `process_fn(entry)`, which
returns an *unconsumed* async generator, handed it to the future and immediately took the
next request — the backend call happened later, when the route consumed the stream. So a
single worker dispatched the whole queue and `concurrency` bounded nothing. Verified
independently by reading the pre-fix `_worker`.

Two consequences for everything above:

1. **The `conc=N` buckets in this issue measured *unbounded* backend concurrency.** They
   are still valid as a measure of what actually reached Ollama — they come from
   overlapping trace intervals, not from herd's cap — but the implied story that herd was
   managing concurrency was never true. `conc=5`/`conc=6` rows existed precisely because
   nothing stopped them.
2. **"Co-tenancy broke the scheduling math" was partly wrong**, including in the CLAUDE.md
   gotcha (since corrected). A co-tenant could not have defeated a cap that was never
   enforced. The co-tenancy harm was real — invisible load plus 77K-token re-prefills —
   but not via the mechanism described.

**Testable prediction, now measurable.** With enforcement live (verified on this fleet:
8 concurrent requests → `in_flight` capped at exactly 4, excess visible as `pending`),
`conc` should no longer exceed `decode_parallelism_for(node, model)` = 4 for
`gpt-oss:120b`. If the Aug-22 step was caused or worsened by unbounded dispatch into a
backend that admits only 4, p25 should improve now. If p25 stays at ~43–53 with
concurrency capped, unbounded dispatch was *not* the cause and this issue needs a
different hypothesis. **Either outcome is informative — measure p25 and the `conc`
distribution over the next 24h before investigating further.**

**RESOLVED 2026-10-03 — the prediction failed. Unbounded dispatch was NOT the cause,
and it is now eliminated.** Daily p25 across the nine days spanning the change:

| day | n | median | p25 | p10 |
|---|---|---|---|---|
| 09-24 | 5,487 | 68.4 | 53.1 | 51.3 |
| 09-25 | 2,774 | 69.5 | 53.2 | 52.0 |
| 09-26 | 5,411 | 74.7 | 54.2 | 52.4 |
| 09-27 | 7,714 | 70.6 | 54.4 | 52.6 |
| 09-28 | 7,991 | 75.2 | 51.3 | 37.4 |
| 09-29 | 7,752 | 76.1 | 49.4 | 36.9 |
| 09-30 | 7,974 | 76.4 | 52.2 | 37.5 |
| 10-01 | 7,722 | 77.0 | 54.6 | 37.8 |
| **10-02** | 5,735 | 76.2 | **52.5** | 37.2 | ← enforcement live |
| **10-03** | 2,247 | 77.7 | **56.6** | 38.3 | ← enforcement live |

p25 sat in a 49–55 band for nine days and read 52.5 and 56.6 on the two enforced days —
inside the existing noise. 10-03 is the high end of the band, not a recovery, and it is
a partial day. Pre-Aug-22 p25 was **73.4** and has never returned.

So the mechanism that looked like the strongest candidate yet is not it. That still
leaves this issue's framing corrected in the two ways above — the `conc=N` buckets did
measure unbounded concurrency, and co-tenancy could not have defeated a cap that was
never enforced — but the step change itself remains **unexplained**.

**Do not re-propose** unbounded dispatch, the Ollama/llama.cpp version, co-tenancy,
`OLLAMA_CONTEXT_LENGTH`, workload mix, routing, memory/swap, thermals, or client
parallelism: every one has been measured and eliminated. A useful next hypothesis has
to explain a *step* on one specific day that persists across Ollama upgrades, reboots,
a co-tenant's removal, and now a concurrency-semantics change — while leaving `conc=1`
(70–76) and the median (76–78) untouched and only depressing the lower quartile.

**Worth knowing before investigating:** `conc=3` oscillates between ~46 and ~75
across the whole seven-month record while `conc=1` stays at 70–76. Several earlier
reports called stable ~52 an ongoing decline because they anchored on Aug 15–21,
one of the high phases. **Plot the full history first.** The question to answer is
specifically "what happened on Aug 22", not "why is it drifting" — it is not
drifting.

One untested lead: during Sep 22–27, when `OLLAMA_CONTEXT_LENGTH=32768` had
accidentally quartered gpt-oss's per-slot context, conc=3 rose to 58–60 and conc=4
to 54 — better than the ~52/~42 on either side. That suggests per-slot KV size
trades against batching efficiency, and is measurable without breaking prefill by
testing intermediate values. Note it is confounded: that period also had 6× worse
TTFT, which changes the concurrency mix.

---


### No health check detects a second client bypassing the router `FIXED` (2026-10-02)

**Severity:** high — this class of problem is invisible to every existing check.

On 2026-08-21 a co-located tool (the globally-installed `openclaw` CLI daemon,
configured in `~/.openclaw/openclaw.json`) began sending ~27% of Ollama's total
load straight to `http://127.0.0.1:11434`, bypassing herd. Nobody configured that
deliberately: its cloud provider lost its credentials and its **model fallback
chain silently redirected the workload onto the local fleet** (`reason=auth
next=ollama/gpt-oss:120b`). Any neighbouring tool with a local model in its
fallback chain can do this to a fleet at any time, without warning on either side.
Fleet decode fell 15% and **nothing in the dashboard, health engine, or traces
indicated why** — herd's own numbers all looked healthy because herd's own
requests *were* healthy. It took hours to find, after wrongly suspecting the
Ollama version, upstream llama.cpp, memory, thermals, and the clients.

The failure is structural: every scoring signal (queue depth, free slots, session
affinity, context fit) is derived from what herd itself dispatched. A second
client does not merely go unmeasured — it silently invalidates the arithmetic.
`QueueManager` caps `bb:gpt-oss:120b` at 4 to match `-np 4`, so a co-tenant
filling the same slots means herd's "conc=2" is really occupancy 3–4.

**Proposed fix:** a `backend_load_unaccounted` check. Ollama exposes no per-runner
request counter, but llama-server's `/slots` does — poll it on the node and
compare observed busy-slot occupancy against herd's own in-flight count for that
`node:model`. A sustained gap (say >15% over 10 minutes) means another client is
sharing the backend. Report it with the remediation: point that client at the
router (`:11435`), since herd is Ollama-API compatible.

Cheap manual version, worth running whenever throughput looks wrong:

```bash
grep -c "new prompt, n_ctx_slot" ~/.ollama/logs/server.log
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT date(timestamp,'unixepoch'), COUNT(*) FROM request_traces GROUP BY 1"
```

Full evidence and the six wrong turns are in `docs/observations.md` (2026-08-23).

**Fixed 2026-10-02** as `backend_bypass_clients`, but *not* the way proposed above.
The `/slots` occupancy comparison was dropped: llama-server binds a random
localhost port that has to be discovered from Ollama's log or `lsof` anyway, and a
15%-over-10-minutes threshold is a derived symptom that still leaves the operator
to go find the culprit. Two other signals were tried and rejected on evidence:

- **`/api/ps` `expires_at` drift** — would imply use herd did not dispatch, except
  the canonical fleet config sets `OLLAMA_KEEP_ALIVE=-1`, which pins `expires_at`
  to the year 2319. Measured on this box; constant, therefore useless.
- **`psutil.net_connections()`** — the portable answer, and it raises `AccessDenied`
  on macOS for any process but our own unless the agent runs as root. Verified on
  macOS 26 / psutil 7.2.2.

So the node shells out to `lsof -nP -iTCP:<port> -sTCP:ESTABLISHED`, which works as
the ordinary user, and reports any process that is not herd, a child herd spawned,
or Ollama itself (`node/backend_clients.py` → `OllamaMetrics.backend_clients`). The
router fires WARNING naming the pid, process and **full argv** — argv, not the
process name, because a Node daemon's name is only `process.title` and in the
2026-08 incident it matched an unrelated project folder and sent the investigation
the wrong way.

Design notes worth keeping:

- Probed at most once per 60 s, not per 5 s heartbeat: this spawns an `lsof` plus a
  `ps` per unknown peer, and a co-tenant that matters is one that sticks around.
- An empty list means "none seen" *or* "the probe could not run" —
  indistinguishable on purpose. The check only fires on a positive sighting, so a
  blind node is a missed detection and never a false alarm.
- Registered with the **registry-based** checks, not the trace-based ones. A fleet
  with no trace data is exactly when an unaccounted co-tenant is least explainable
  by any other means; gating it on `trace_store` would have disabled it when it
  matters most. (It was written into the trace block first and moved.)
- WARNING, not CRITICAL, and the remedy is *repoint, not kill*: the co-tenant may
  be deliberate, and herd is Ollama-API compatible so changing a base URL from
  :11434 to :11435 restores the accounting at no cost. The fix text also says to
  check whether the client arrived by **fallback** rather than configuration.
- Windows is deliberately unimplemented rather than guessed at — the signal is only
  as good as its process attribution, and `netstat -ano` + `tasklist` needs its own
  verification pass. It returns an empty list there.
- **Local and off-box clients are different shapes and both are handled.** The first
  version filtered on the client end of the socket only, which is correct for a
  loopback co-tenant and misses an off-box one entirely: that client's socket lives
  on its own machine, so all this node sees is Ollama's server end
  (`ourip:11434->theirip:52341`), whose remote port is not ours and was therefore
  filtered out. Caught by checking the parser against lsof's real output for the
  remote case rather than only the loopback case it was written for — and it matters,
  because Ollama binds `*:11434` by default (confirmed on this fleet). Off-box peers
  are reported by address with `pid=0`; the card names the address rather than
  printing "pid 0", which would read as a bug.
- **The router is excluded by address** (`node/agent.py` passes `router_url` through).
  It proxies to every node's Ollama over the LAN, so without this exclusion every
  node in a multi-node fleet would report the router as a bypasser — exactly the
  false alarm that gets a check muted.

Verified end-to-end on the live fleet, not just in tests: a foreign process holding
4 connections to :11434 produced the WARNING ~48 s after it appeared and the card
cleared ~48 s after it exited. `tests/test_node/test_backend_clients.py` drives the
probe against a real socket from a separate process for the same reason — a probe
that reports nothing is indistinguishable from one that is broken.

---

### Long-context requests starve co-resident decode with no admission control `OPEN`

**Severity:** medium-high (tail latency).

The same incident surfaced a real scheduling gap. 181 requests carried 8K–77K
token prompts that re-prefilled from scratch (`restored context checkpoint …
n_past = 1`), chunking 2048 tokens at a time for **80+ seconds**. Chunked prefill
monopolises llama-server's unified batch, so every co-resident slot's decode
stalls for the duration. Measured: requests within 60 s of one are 2.6× over-
represented in the slow quartile; within 120 s they run 43.8 vs 72.3 tok/s (−39%).
Total prefill time on this box went from 25–84 s/day to 1,795–2,471 s/day.

herd has no notion of prefill cost when admitting work. A 77K-token request and an
850-token request both consume one queue slot. Worth considering: weight queue
admission by estimated prefill tokens, or keep a separate lane for very-long-context
requests so one of them cannot stall an entire fleet's decode.

Note this hurt *tail* latency far more than typical latency — median fell 6% while
p25 fell 44%. **Dashboards that show mean or median throughput hide this entirely;**
p25/p10 would have shown it immediately.

---

### `FLEET_NUM_CTX_OVERRIDES` appeared not to apply to `gemma3:27b` `NOT A BUG` (2026-08-23)

Filed when `ollama ps` showed `gemma3:27b` at `CONTEXT 131072` (~70 GB resident)
despite `~/.fleet-manager/env` pinning it to 32768. **The override was fine.**

The model had been loaded by a client that bypassed the router entirely (see the
bypass issue above) — and a request that skips herd also skips `num_ctx`
resolution, so Ollama applied its own default. Once that client was removed and
herd loaded the model itself, it came back at `CONTEXT 32768` / 23 GB as configured.

Keep as a symptom, not a defect: **an unexpectedly large `CONTEXT` in `ollama ps`
is an early signal that something is loading models behind the router's back.**
Whatever backend-load check gets built should compare loaded-model context sizes
against `FLEET_NUM_CTX_OVERRIDES`, not just request counts — it is a cheaper
signal than task-count reconciliation and needs no log parsing.

---

### Stray `homebrew.mxcl.ollama` can win the port race and serve a stale Ollama `FIXED` (2026-09-28)

**Severity:** was filed as low/noise on 2026-08-23. It is not noise — on
2026-09-28, during an Ollama restart, it grabbed `:11434` first and
`/api/version` reported **`0.16.3`** instead of `0.34.4`. A six-month-old
inference engine silently serving the fleet is a correctness and performance
hazard, not a log annoyance.

Resolved: `launchctl bootout gui/$UID/homebrew.mxcl.ollama` + `launchctl disable`,
so it cannot return on boot. Kept here because the *class* of problem recurs —
anything that can bind `:11434` before the Mac app does will be served
transparently. **Always re-check `curl -s localhost:11434/api/version` after an
Ollama restart** (the release checklist does this). Note `/opt/homebrew/opt/ollama/bin/ollama --version`
reports its own stale client version and is NOT what is serving — check the API.

Original symptom, for reference: it races the app for port 11434 at boot —
`~/.ollama/logs/server.log` opens with `bind: address already in use`.

A Homebrew ollama service is still registered with launchd (`homebrew.mxcl.ollama`,
last exit status 1) alongside the Mac app. It races the app for port 11434 at boot —
`~/.ollama/logs/server.log` opens with `bind: address already in use` — and respawns
---

## Routing Safety

### MLX proxy records failed requests with an empty `error_message` `FIXED` (2026-10-02)

**Severity:** low (observability), but it degrades two things at once.

A request that fails on the MLX path is written to `request_traces` with
`status='failed'` and an **empty** `error_message`. Found via telemetry: the
first real community payload carried `errors: {"unknown": 1}`, and `unknown` is
what `_categorize_error()` returns for a falsy message. Tracked back to the
GLM-5 load that was SIGKILLed on 2026-08-15 — the trace recorded the failure but
not the reason.

Impact: the dashboard shows a failure with no cause, and the anonymous telemetry
error histogram gets an `unknown` bucket that hides what actually broke. A
persistent `unknown` in the published community stats would look like a
categorisation gap when it is really a missing message at the source.

**Fix:** record a reason wherever `mlx_proxy` marks a request failed — even
`"process exited"` or `"connection closed"` beats an empty string. Then confirm
`_categorize_error()` has a bucket for it rather than falling through to
`unknown`.

**Fixed 2026-10-02.** The root cause was not a missing call site — all 15 already
passed `error_message=str(exc)`. It is that `str(exc)` is **empty** for whole
classes of exception this path hits constantly: `httpx.ReadTimeout`,
`ConnectTimeout`, `WriteTimeout`, `PoolTimeout`, `RemoteProtocolError` and
`asyncio.CancelledError` all stringify to `""`. So the obvious-looking code was
the bug.

Three layers, outermost first:

1. `common/errors.py::describe_exception` — one definition of how an exception
   becomes a trace message. Deliberately a **no-op when the message is non-empty**
   (returns `str(exc)` byte for byte), so dropping it into the Ollama path cannot
   shift any category that already worked in the published telemetry histogram.
   Only the empty case changes, to the class name. The Ollama path's existing
   `str(e) or repr(e)` is replaced by it too — same behaviour for real messages,
   without `repr`'s `ReadTimeout('')` noise.
2. `_categorize_error` gained rules for the bare class names, so `ReadTimeout` →
   `timeout`, `ConnectError`/`RemoteProtocolError`/`ReadError` →
   `connection_error`, `CancelledError` → `client_disconnected`. Previously only
   the `timeout` substring happened to match; the rest fell to `other`.
   `CancelledError` matters most: Starlette ends a disconnected streaming response
   by cancelling the task, so it is the *common* disconnect shape and it is one of
   the empty-`str` classes.
3. `record_trace_mlx` refuses to persist `status='failed'` with a blank message at
   all, substituting `"unspecified MLX backend failure"`. A future call site cannot
   reintroduce the bug. `"other"` is a truthful bucket; `"unknown"` is a lie that
   reads as a categorisation gap in public stats.

`unknown` is now reserved for a genuinely absent message, which is what it should
have meant. Pinned by `tests/test_server/test_error_descriptions.py`, including a
guard that no call site has drifted back to bare `str(exc)`.

### Docker nodes unreachable + every node reported `apple_silicon` `FIXED` (0.9.3)

**Reported by an external user** ([issue #1](https://github.com/geeks-accelerator/ollama-herd/issues/1)), running `herd-node` in containers on Linux/NVIDIA hosts. Two independent bugs, both of which made the fleet unusable off Apple Silicon.

**1. `FLEET_NODE_OLLAMA_HOST` was discarded.** `registry._build_ollama_url()` kept only the *port* from `ollama_host` and rebuilt the URL from `payload.lan_ip`. Inside a container that is the bridge address (`172.17.0.x`), which the router cannot reach, so every routed request failed `ConnectError` → HTTP 500 — while `/fleet/status` cheerfully displayed the unreachable URL.

Fixed by ordering the sources by how much we actually trust them: an explicitly-configured non-loopback `ollama_host` wins (the operator knows their topology); then `request_ip`, which is reachable *by construction* because the heartbeat arrived from it; then self-reported `lan_ip` as a last resort. The middle step is the real insight — the router always had a proven-good address and was preferring a guess over it.

**2. `arch` was hardcoded.** It defaulted to `"apple_silicon"` on both `HeartbeatPayload` and `HardwareProfile`, and `collect_heartbeat()` never set it, so *every* node claimed Apple hardware. The router uses `arch` for device-aware scoring, so this was not cosmetic. Now detected in `collector._detect_arch()`, with `"unknown"` as the honest fallback.

**Why we never saw it:** the dev fleet is one Mac with the router and node co-located, so the `request_ip == 127.0.0.1` branch always ran and `lan_ip` was never consulted. Every non-Apple, non-co-located deployment hit this immediately. A test in `test_models.py` asserted `arch == "apple_silicon"` — it was codifying the bug.

### Ollama watchdog cascade-restarted `ollama serve` and wiped pinned models `FIXED` (removed)

**File:** `src/fleet_manager/node/ollama_watchdog.py` (deleted 2026-04-23)
**Severity:** High

The node-side watchdog periodically sent a chat probe to Ollama to detect stuck runners. Its probe-model picker (`_pick_probe_model`) chose the **smallest currently-loaded model** as the probe target. When `nomic-embed-text` (an embedding-only model, ~274 MB) was hot — which is common — it was picked. `/api/chat` on an embed-only model returns HTTP 400 every time; the watchdog interpreted the 400 as "runner stuck," kicked runner processes via `pkill`, and the counters never reset on successful kicks. Result:

1. Every ~2 min, 2 consecutive 400s → KICK (kill runner processes).
2. Repeated 13 times over 13 min without resetting the counter.
3. Escalated to a full `ollama serve` restart (`launchctl kickstart -k`), wiping **all** hot models.
4. Subsequent preloader re-loads of pinned models timed out at 120s because the watchdog kept kicking runners mid-cold-load.
5. During the window, 20 `gemma3:27b` requests were silently routed to `gpt-oss:120b` via VRAM fallback — a cross-category substitution (vision → reasoning) that silently dropped image inputs.

**Observed:** 2026-04-23 21:50–23:10 UTC. User request for `claude-sonnet-4-5 → gemma3:27b` (vision path) got answered by `gpt-oss:120b` silently.

**Fix shipped:**
- `src/fleet_manager/node/ollama_watchdog.py` deleted entirely. The user's original fleet ran fine without it — the watchdog existed to smooth over intermittent Claude Code CLI issues that are now handled by other layers (admission control, streaming retry, context protection).
- `src/fleet_manager/node/agent.py` — all `_ensure_ollama_watchdog` / shutdown hooks removed.
- `src/fleet_manager/models/config.py` — 5 `ollama_watchdog_*` settings removed. Comment left in place describing why (so nobody re-adds it without the two fixes the original lacked: explicit probe-model allowlist, per-cause cooldowns).
- `src/fleet_manager/server/routes/routing.py` — cross-category VRAM fallback now logs at **ERROR** level (was INFO) with an explicit "QUALITY RISK" warning when vision → non-vision substitutions happen. Event record carries `cross_category` + `fallback_category` for dashboard filtering. Existing `X-Fleet-Fallback` response header continues to flag substitutions to clients.
- `tests/test_node/test_ollama_watchdog.py` deleted alongside the module.

If stuck-runner detection is ever needed again, re-add it with: (a) an **explicit allowlist** of chat-capable probe models, never size-based selection; (b) per-cause cooldowns so a guaranteed-failing probe can't escalate; (c) hard cap on serve-restart escalations per hour.

### Ollama native image models can evict LLMs from memory `PARTIAL`

**File:** `src/fleet_manager/server/routes/ollama_compat.py`
**Severity:** High

When an Ollama native image model (e.g., `x/z-image-turbo` at 12GB) is requested via `/api/generate`, Ollama may evict the resident LLM to make room. On a single-node fleet, this means ALL text inference fails with 500 errors until the LLM is reloaded.

**Observed:** 2026-03-30. After generating images with `x/z-image-turbo`, `gpt-oss:120b` was evicted. All DriftsBot text requests failed with 500 for several minutes.

**Proposed fixes (in order of complexity):**
1. **Prefer mflux over Ollama native** — when both mflux `z-image-turbo` and Ollama `x/z-image-turbo` are available, prefer mflux since it doesn't compete for Ollama VRAM
2. **Guard single-LLM nodes** — don't route Ollama native image requests to a node if it's the only node serving text LLM requests and the image model isn't already loaded
3. **Memory budget check** — before routing, verify that loading the image model won't push total VRAM past available memory (Ollama reports `size_vram` per model)
4. **Auto-unload after generation** — send `keep_alive: 0` after image generation completes to immediately free VRAM for the LLM

**Fix #1 implemented:** The router now prefers mflux over Ollama native when both are available. If a client requests `x/z-image-turbo` via `/api/generate` and mflux has `z-image-turbo` on any node, the router redirects to the mflux image server automatically. Ollama native is only used as a fallback when mflux isn't installed. This prevents LLM eviction because mflux runs as a separate subprocess outside Ollama's VRAM.

**Remaining:** Fixes #2 (guard single-LLM nodes) and #3 (memory budget check) are not yet implemented. These would protect against Ollama native image models on multi-node fleets where some nodes have mflux and others don't.

---

### Ollama watchdog can't escalate to `ollama serve` restart `OPEN`

**File:** `src/fleet_manager/node/ollama_watchdog.py`
**Severity:** High (root cause of multi-hour gpt-oss outages)

The watchdog detects stuck `/api/chat` and kills `ollama runner` processes via `pkill -9`. That recovers the case where `ollama serve` is healthy but a runner is wedged. It does **not** recover the more pernicious case where `ollama serve` itself has accumulated state corruption — `/api/tags` keeps answering, runners keep getting kicked, but each respawn wedges immediately under load.

**Observed:** 2026-04-22, ~5h `ollama serve` uptime under sustained load (Claude Code + dashboard briefing + concurrent `hf download`):

- 28 consecutive `gpt-oss:120b` requests in debug log, all `status=retried`, all `err=ReadError('')`
- Latencies climbing **monotonically** 50s → 130s → 190s → 250s → 310s → 370s → 452s → 458s → 512s → 572s
  - That growing-tail pattern means requests stack serially behind a stuck runner; each one waits longer than the last for a slot that never opens
- Watchdog log shows `KICKING stuck runner` firing on schedule (18:10:18) — kicks landing fine
- `ollama serve` log: repeated `"llama runner process no longer running" sys=9 string="signal: killed"` — runners die before serving anything
- `/api/tags` answers in 12ms throughout (so the watchdog's tags-probe stays green)
- `/api/chat` returns `HTTP 000` after 30s
- **Recovery only happened after manual `pkill -9 ollama serve` + relaunch** — runner kicks alone did nothing

**Why the current design is insufficient:**

1. **Treats one failure mode, not the whole space.** The watchdog assumes "runner stuck, serve healthy." It can't see the "serve healthy but every runner dies under load" mode that today's outage exhibited.
2. **No escalation.** After N kicks with no recovery, it should escalate to bouncing `ollama serve`. Today it kicked, watched the next probe still fail, kicked again on the next cooldown, and so on indefinitely.
3. **Cooldown vs probe-interval mismatch.** Probe every 60s, cooldown 120s — the watchdog is silent for 2 minutes after each kick while damage compounds. A growing-latency stack-up like today's was visible in the trace store within ~3 cycles, but the watchdog couldn't act on it.
4. **No load-shedding.** When the watchdog detects the system is in trouble, it does nothing to slow incoming traffic. The dashboard briefing kept firing `num_predict=4800` requests every ~60s (because each failure cleared the cache, triggering immediate retry on next pageview) — the watchdog couldn't see that load source, let alone throttle it.

**Proposed fixes (in order of complexity):**

1. **Add escalation path** — after 3 consecutive kicks where the next probe still fails within the cooldown window, restart `ollama serve` itself (`pkill -9 -f "ollama serve"` then `open -a Ollama` on macOS, `systemctl restart ollama` on Linux). Add a higher-level cooldown (e.g. 30 min) on serve restarts to prevent a flap loop. *This alone would have ended today's outage in ~5 minutes instead of multiple hours.*
2. **Add a third probe: per-cycle latency trend.** If the rolling p95 of `/api/chat` latency from the trace store grows monotonically over 3 cycles AND the absolute latency exceeds a threshold (e.g. 60s), treat that as a soft failure and trigger a kick BEFORE the request fully times out. Catches the stack-up pattern early.
3. **Failed briefings must update the cache.** `dashboard.py:_generate_briefing` should write a "last failure" record to the cache when the LLM call fails, so the next pageview/poll doesn't immediately re-trigger another `num_predict=4800` request. The endless 60s briefing-spam loop was a major load multiplier today.
4. **Load shedding via `/fleet/queue` 503**. When the watchdog has fired ≥1 kick in the last cycle, the router should return 503 Service Unavailable to non-critical traffic (everything except real user-facing requests) so health probes and briefings back off automatically. Hard to classify "critical" cleanly without explicit tags, but even a coarse "anything from `127.0.0.1` is internal → defer" rule would have helped.
5. **Separate watchdog from per-node agent.** Today's watchdog runs in the same process as the heartbeat/collector. If the node agent itself wedges, no watchdog. A small standalone supervisor (launchd plist on macOS, systemd unit on Linux) is a better long-term home — survives node-agent restarts, can kill `ollama serve` cleanly, can use a different binary so it doesn't share the failure mode.
6. **Stop using Ollama for what we don't need.** The briefing call could go to MLX (Qwen3-Coder-30B serves it in ~3s vs gpt-oss:120b's 50s+ when working). `nomic-embed-text` could move to an MLX-native embedding model. If Ollama's only tenant becomes "things users explicitly request via `/api/chat`," it's much harder to overload accidentally and the watchdog's blast radius shrinks proportionally.

**Recommendation:** Land #1 + #3 immediately (small surface, big impact). #2 next as additional signal. #4–#6 are larger architectural moves to tee up.

**Related:** Today's outage compounded with a `huggingface_hub` download running in parallel — disk-write saturation made runners crash even faster. Already noted in `docs/observations.md`. The watchdog has no awareness of disk I/O or other resource competition.

---

## External Dependencies

### Ollama ships a native MLX runner — our MLX subsystem's premise is stale `OPEN` (investigate first)

**Files:** `src/fleet_manager/node/mlx_supervisor.py`, `src/fleet_manager/server/mlx_proxy.py`, `scripts/setup-mlx.sh`, `CLAUDE.md`
**Severity:** Medium–High (strategic — a large subsystem may be redundant; **plus** load-bearing constants may be wrong)

Ollama now has a **first-party native MLX runner** ([ollama.com/blog/mlx](https://ollama.com/blog/mlx)). Verified on this fleet 2026-07-17: the running Ollama is **`0.24.0`** (not the `0.20.4` CLAUDE.md documents), and its binary contains **`github.com/ollama/ollama/x/mlxrunner`**, real MLX C-API bindings (`mlx_enable_compile`, `mlx_set_memory_limit`), `OLLAMA_MLX_MTP_*` + `OLLAMA_NEW_ENGINE` env knobs, a prefix-cache trie (`mlxrunner.trieNode`), and MTP speculative decoding — 6,925 `mlx` string hits in total.

**Two problems.** (1) **Stale, load-bearing facts:** the documented "Ollama 0.20.4 has a hardcoded 3-model hot cap" underpins `model_preload_max_count=3`, `OLLAMA_HOT_MODEL_CAP` / `free_slots`, the `OLLAMA_MAX_LOADED_MODELS=-1` gotcha, and the eviction/pin logic — if 0.24+ changed the cap, the herd is enforcing a limit that no longer exists. (2) **Expiring premise:** our whole `mlx_lm.server` stack (supervisor, proxy, `mlx:` prefix, the `--kv-bits` patch that breaks on every `mlx-lm` upgrade, and its recent bug tax) exists *because Ollama couldn't do MLX*.

**Not threatened:** the herd's core value is routing (scoring, queues, health, multi-node) — a faster Ollama helps it for free — and **distributed multi-Mac inference is still ours** (Ollama is single-host). The decisive unknown is **Ollama's MLX model coverage** (the preview accelerated only Qwen3.5-35B-A3B; `mlx_lm` runs arbitrary HF conversions).

**We are 8 versions behind:** latest is **v0.32.1**; `0.24.0` **predates stable MLX** (the 0.30 line). Full analysis + verified-vs-unverified split: [`issues/ollama-native-mlx-runner.md`](issues/ollama-native-mlx-runner.md). **Execution plan** (upgrade to v0.32.1 + the four measurements that decide the MLX subsystem's fate, install landmines, rollback): [`plans/ollama-0.32-upgrade-and-mlx-evaluation.md`](plans/ollama-0.32-upgrade-and-mlx-evaluation.md).

---

### GLM-4.7-Flash ~4× too slow on Ollama (glm4moelite MoE not exploited) `OPEN` (upstream)

**File:** none (upstream Ollama bug — herd serves/measures correctly)
**Severity:** Medium (affects model selection/benchmarking; no correctness impact)

`glm-4.7-flash` decodes at ~13.7 tok/s on the M3 Ultra via Ollama — the speed of the **dense** `gemma3:27b` — despite being a **30B-A3B MoE with 3B active params** that should match `qwen3-coder:30b-a3b` (~56.7 tok/s). Ollama's `glm4moelite` path doesn't exploit the sparsity and CPU-offloads the experts ([ollama/ollama#14045](https://github.com/ollama/ollama/issues/14045)). Compounded by interleaved thinking (~3,600 output tokens vs qwen's ~400) and a 202,752-token default context (51 s prefill). **Fix:** serve via MLX (verify `mlx-lm` supports `glm4_moe_lite` — [mlx-lm#806](https://github.com/ml-explore/mlx-lm/issues/806) — our pinned 0.31.3 may need an upgrade + re-patch); herd-side, cap `num_ctx` to cut the prefill. Full analysis: [`issues/glm-4.7-flash-ollama-glm4moelite-slow.md`](issues/glm-4.7-flash-ollama-glm4moelite-slow.md).

---

### DiffusionKit `argmaxtools` crashes on macOS 26+ `FIXED` (local patch)

**File:** `argmaxtools/test_utils.py` (installed dependency, not our code)
**Severity:** High (blocks all DiffusionKit image generation)

The `os_spec()` function in `argmaxtools.test_utils` parses `sw_vers` output expecting exactly 3 lines. macOS 26 added a `ProductVersionExtra` field (4th line), causing `IndexError: list index out of range`. This crashes `diffusionkit-cli` on any image generation attempt.

**Workaround applied:** Patched the installed `test_utils.py` to parse `sw_vers` output as a key-value dict instead of positional list. See [image generation guide](guides/image-generation.md) for the patch instructions.

**Upstream status:** No fix as of `argmaxtools` v0.1.23 (2026-03-30). The `argmaxtools` repo appears to be private — no way to submit a PR directly. Filed on DiffusionKit GitHub as the integration surface.

**Note:** This patch must be re-applied after any `uv tool upgrade diffusionkit` or `pip install --upgrade diffusionkit`.

---

### DiffusionKit SD3.5 Large — Python crash on cleanup `OPEN`

**File:** `diffusionkit/mlx/__init__.py` (installed dependency)
**Severity:** Low (image generates successfully, crash is post-generation)

SD3.5 Large (11.6GB peak memory) occasionally triggers a "Python quit unexpectedly" crash dialog on macOS after the image has been written to disk. The image is valid — the crash happens during post-generation telemetry/cleanup. SD3 Medium (3.5GB peak) does not exhibit this behavior.

**Workaround:** Use SD3 Medium for production workloads. SD3.5 Large works but may show the macOS crash dialog to users.

**Root cause:** Likely a memory-related segfault in the MLX/Metal cleanup path when using system Python 3.9. May resolve with a newer Python version or future DiffusionKit update.

---

## Performance (Will Bite at Scale)

### `available_gb` is the wrong ceiling for "can this model fit?" `OPEN` (needs a design decision)

**Files:** `src/fleet_manager/server/scorer.py`, `src/fleet_manager/server/model_preloader.py`
**Severity:** Medium (real request failures, narrow + self-healing window)

The scorer/preloader gate model loads on psutil's `available_gb`, which on macOS is **volatile** (sampled 17 GB → 445 GB on an idle 512 GB box; -97 GB in 3 s) because resident models are **wired** and wired isn't counted as available. Worse, it's the **wrong question**: Ollama evicts its own LRU model to make room, so resident-model memory *is* usable for a new model. After the 2026-07-17 reboot this produced `All 1 nodes eliminated for gpt-oss:120b` ×100+ and 30 s holding-queue timeouts on a machine with ~358 GB free.

**Narrow:** the scorer already skips the memory check for *resident* models ([`scorer.py:203`](../src/fleet_manager/server/scorer.py)), so it only bites at cold start / just after a restart. **Left open deliberately** — picking the wrong metric fails in the dangerous direction (over-reporting capacity is what produced the 290 GB thrash loop + kernel panic). Full analysis, four options, and a suggested instrument-first step: [`issues/available-gb-is-the-wrong-ceiling-for-model-fit.md`](issues/available-gb-is-the-wrong-ceiling-for-model-fit.md).

---

### `TraceStore` write-storm under WAL contention `FIXED` (0.6.2)

**Files:** `src/fleet_manager/server/trace_store.py`, `src/fleet_manager/server/latency_store.py`, `src/fleet_manager/server/health_engine.py`
**Severity:** High (observability outage, not request outage)
**Observed:** 2026-05-10 14:21 PDT → 2026-05-15 00:58 PDT (4.5 days)

A long-running read held off SQLite WAL checkpoints on `latency.db`. The WAL grew to 2.5 GB and writers couldn't acquire the lock within the 5-second `busy_timeout`. Result: ~40,000 background `record_trace` tasks failed with `database is locked` across May 10–15 (peak ~9,650/day). Requests themselves succeeded end-to-end (trace writes are fire-and-forget) but the dashboard's `reqs_24h` quietly dropped to 0 because it queries the same DB. The failure was missed by four consecutive soak checks because the grep pattern used to scan logs was wrong (no space after the JSON colon — see `docs/observations.md` 2026-05-15 for the full process post-mortem).

**Fix shipped in 0.6.2 — two rounds:**

Round 1 (2026-05-15, "longer timeout + retry"):
- `PRAGMA busy_timeout` 5s → 30s in both `TraceStore` and `LatencyStore`.
- `TraceStore.record_trace` retries on locked errors (200ms → 800ms → 2s, 3 attempts) before declaring the trace lost.
- `PRAGMA wal_autocheckpoint=100` in both stores bounds WAL growth even when a reader is slow.
- New `trace_store_write_failures` health check (WARNING at 1+, CRITICAL at 50+ failures in last 5 min) — makes the failure mode dashboard-visible instead of requiring operators to grep logs.
- Per-process JSONL log files (`herd.jsonl` + `herd-node.jsonl`) eliminate a cross-process daily-rotation race that left one log day growing to 131 MB while peers stayed at 6 MB.
- `CLAUDE.md` "Gotchas" entry documenting the correct JSONL grep pattern (`'"level": "ERROR"'` with space) so future scanners don't repeat the false-clean miss.

Round 2 (2026-05-16, "structural fix" — round 1 reduced amplitude but didn't eliminate the failure under sustained traffic; ~4,470 errors recurred in 12 hours of soak):
- **Dedicated `_read_db` connection per store** (`PRAGMA query_only=1` for defense-in-depth). aiosqlite serializes operations per-connection through a single background thread; with one connection serving both reads and writes, a slow dashboard read blocked queued writes for the read's duration. Worse, reader snapshots pinned the WAL checkpoint barrier on the writer's view of the same connection. Two connections = two threads, independent snapshots, no serialization.
- **Periodic `PRAGMA wal_checkpoint(PASSIVE)`** every 10s from a background task in `app.py` lifespan. Wall-clock-triggered checkpoints fire in the gaps between dashboard read snapshots; autocheckpoint alone is volume-triggered and can sit at 99 pages while readers accumulate snapshots that block the eventual checkpoint when the 100th write lands.
- Plan: `docs/plans/trace-store-read-connection-and-checkpoint.md`. Verified on the local fleet 2026-05-16 — a 30-concurrent-write + 120-dashboard-poll burst held the WAL at 410 KB peak vs 103 MB on the same workload pre-round-2.

Operator runbook for "database is locked" recovery: `docs/troubleshooting.md` § "Trace DB write failures."

---

### An idle MLX server gets externally SIGKILLed on a saturated box (likely OS memory pressure) `OPEN` (mitigated)

**File:** `src/fleet_manager/node/mlx_supervisor.py`
**Severity:** Low–Medium (churn + wasted VRAM/reloads; auto-recovers, no user-facing request failure)
**Observed:** 2026-07-16 — port 11440 (`mlx-community/Qwen3-Coder-Next-4bit`)

Over an 8 h benchmark window, port 11440 exited `rc=-9` (SIGKILL) **6×**, clustered in the load peak (06:24–06:43); the monitor caught each dead child and restarted it, and the port wasn't re-bindable for ~10 s (`_wait_port_free` "port still occupied … spawning anyway"), re-mmap'ing the 30B model each time.

**Corrected mechanism (an earlier draft of this issue was wrong — worth recording why).** The first diagnosis blamed a "false-positive health kill": that the runtime health poll (3 s timeout) marked the server unhealthy and the supervisor SIGKILLed it. **That is not how the supervisor works.** `_monitor` only restarts on an *actual* process exit (`rc = self._proc.poll(); if rc is None: continue` — L888-907); `poll_health`/`refresh_health` (L984, L1189) only *update the status string* for the dashboard — nothing kills or restarts a running-but-unhealthy server. So the `rc=-9` came from **outside** the supervisor entirely.

The signature points at **macOS memory pressure (jetsam / memorystatus)**: the kill was **selective** (only the idle 35 GB 11440 died; the actively-served 11441/11442 and the small supervisor parent all survived), clustered in load peaks, and left **zero** app-level markers (jetsam is silent to the victim — the 131 MB log has no `out of memory` / `Metal` / `allocate`; its 12k tracebacks are restart-race noise: `cannot schedule new futures after interpreter shutdown` from the dying process + `Address already in use` from the respawn racing the port). 11440 was **essentially idle** — 19,904 health pings vs **3** real inference requests (nothing routes to Qwen3-Coder-Next; coding load went to Ollama `qwen3-coder:30b`), which makes it the lowest-priority, highest-footprint jetsam target. (`log show` for jetsam events was inconclusive without `sudo`, so "jetsam" is strong inference, not a captured kernel line.)

**There is no clean code fix** — a health-check debounce fixes nothing here (the health check doesn't cause the kill). The real lever is operational:
- **Don't keep an unused large model resident.** A model with zero routed traffic holds ~35 GB and becomes the jetsam target under pressure. **Mitigation applied 2026-07-16:** dropped Qwen3-Coder-Next from `FLEET_NODE_MLX_SERVERS`.
- If a genuinely-used MLX model is being jetsam'd, that's a real memory-headroom problem — surface it via the memory-pressure gate rather than absorbing repeated reloads.
- Minor hardening still worth doing: the `poll_health` comment "monitor will restart" (L1007) is misleading (the monitor does not restart on health status) and should be corrected so the next reader doesn't repeat this misdiagnosis.

---

### `/fleet/pin` reported "not on disk" for a resident, serving model `FIXED` (0.8.2)

**Files:** `src/fleet_manager/server/routes/fleet.py`, `src/fleet_manager/server/model_preloader.py`
**Severity:** Medium (factually false error; intermittent — depends on free memory at the instant of the call)
**Observed:** 2026-07-17 02:41 — reported by a client agent

`POST /fleet/pin {"model":"gpt-oss:120b","node_id":"bb"}` returned:

> `{"ok":false,"error":"'gpt-oss:120b' is not on disk on any online node — run 'ollama pull gpt-oss:120b' first."}`

…while gpt-oss:120b was **on disk, loaded (70.96 GB), and served 30/30 requests seconds later**. No restart, no traffic gap, and **not reproducible** on retry.

**Root cause — two compounding bugs.** The router log carried the real reason:

```
2026-07-17T02:41:01  Preloader: skipping gpt-oss:120b — need 72GB but only 49GB free on bb (fleet-pin)
```

1. **The error message conflated three causes.** `_load_model_on_best_node` returns a bare `False` for *not-on-disk*, *memory-gate refusal*, **and** *pre_warm error* — and `/fleet/pin` hardcoded the "not on disk … run `ollama pull`" message for all of them. The caller was told to pull a model that was already resident and serving.
2. **The memory gate ran against an already-resident model.** `_estimate_model_size("gpt-oss:120b")` = 72 GB, so the gate demands `72 × 1.2 = 86.4 GB` free. gpt-oss:120b was **already loaded**, and its own ~71 GB footprint is subtracted from the node's free memory — so the gate saw 49 GB free and refused to "load" a model that was **already in memory**. Pinning a hot model could fail *because it was hot*. (The preloader dodges this by checking `_model_is_loaded_anywhere` before calling the loader; the pin route called it directly.) The intermittency is explained by free memory fluctuating — the same call succeeded at 03:02 once memory recovered (`Preloader: loading gpt-oss:120b (~72GB) on bb (fleet-pin)`).

**Fix shipped in 0.8.2:**
- `_load_model_on_best_node` skips the memory gate when the model is **already resident** on the chosen node (reusing `_model_resident_on_node`); `pre_warm` still runs so `keep_alive=-1` is re-applied — the actual point of pinning a loaded model. Safe for the preloader, which never reaches that branch.
- `/fleet/pin` now checks on-disk explicitly (`_nodes_with_model_on_disk`) and reports each cause truthfully: genuine not-on-disk → `404` with the pull hint; on-disk-but-wouldn't-load → **`503`** naming insufficient free memory and pointing at `/fleet/status` + the router log's exact need-vs-free numbers.

---

### Failed-request traces get garbage-collected before they persist `FIXED` (0.8.2)

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Medium (observability — success rate reads higher than reality)
**Observed:** 2026-07-16

Over an 8 h window, **242** inbound OpenAI requests for `glm-4.7-flash:latest` produced **211** Ollama 503 `"maximum pending requests exceeded"` responses and **0** trace records — `glm` under no `original_model`, not even as a fallback — while 4,634 *completed* requests traced fine. The dashboard's "99.98 % success (4,669 requests)" was therefore computed over *traced* traffic only; the 211 GLM failures weren't in the denominator. (Context, not a herd bug: the client sent the *Ollama* model name to `/v1/chat/completions` instead of the resident `mlx:` model, so every request hit Ollama's saturated queue.)

**Corrected mechanism (a first draft of this issue said the error path "never calls `record_trace`" — that was wrong; the call is there).** The non-retryable branch *does* call `_record_trace(..., "failed")` (streaming.py L455). The real bug was in **`_create_logged_task`**: it did `asyncio.create_task(coro)` **without keeping a strong reference**. asyncio only holds a *weak* reference to a task, so a fire-and-forget task with no other reference can be GC'd mid-flight. Completed traces survived because the route keeps `await`-ing after recording (the loop runs the task); the **error path records then `raise`s on the very next line** with no further `await`, so the loop never ran the weakly-referenced trace task before the request tore down and GC collected it. Failed traces vanished; completed ones didn't — exactly the observed asymmetry.

**Fix shipped in 0.8.2 (two parts):**
- `_create_logged_task` now holds each task in a module-level `_background_tasks` set until its done-callback fires — the documented fix for the create_task weak-reference footgun. This makes *all* fire-and-forget writes (traces, latency records, client closes) reliable, not just the error path.
- The exhausted-retry branch (`_stream_with_retry`, `attempt > max_retries`) now records a terminal `"failed"` trace instead of leaving only per-attempt `"retried"` rows, so a request that burns every retry has a terminal outcome in the DB.

Complements the existing `trace_store_write_failures` health check: that catches "the write was attempted and failed"; this fixes "the write was scheduled and then GC'd before running."

---

### 1. `LatencyStore.get_percentile()` — Unbounded Memory Growth `FIXED`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** High

`get_percentile()` loaded ALL historical latency rows into memory every time a latency observation was recorded. For a high-traffic deployment with thousands of observations per `(node, model)` pair, this grew without bound.

**Fix:** Capped to the most recent 500 observations per `(node, model)` pair using a subquery with `ORDER BY timestamp DESC LIMIT 500`. Memory usage is now bounded regardless of history size.

---

### 2. `_refresh_cache()` — N+1 Query Pattern `FIXED`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** Medium

On startup, `_refresh_cache()` first queried all distinct `(node_id, model_name)` pairs, then issued a separate `get_percentile()` call for each pair. For a fleet with many node/model combinations, this meant dozens of sequential SQLite round-trips.

**Fix:** Replaced with a single SQL query using `ROW_NUMBER()` and `PERCENT_RANK()` window functions to compute all p75 values at once. Also caps to the latest 500 observations per pair. Startup is now one query regardless of fleet size.

---

### 3. `in_flight` List — O(n) Membership and Removal `FIXED`

**File:** `src/fleet_manager/server/queue_manager.py`
**Severity:** Low–Medium

The `in_flight` field on each queue was a `list`. Both `in` checks and `.remove()` were O(n). Under high concurrency with deep queues, this was a bottleneck.

**Fix:** Changed to `dict[str, QueueEntry]` keyed by `request_id`. All operations (`__contains__`, `pop`, `[]`) are now O(1). The reaper, `mark_completed`, `mark_failed`, and worker all use dict operations.

---

## Code Quality

### 4. `_request_tokens` Dict — Leaking Internal State `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Low

Route handlers in `openai_compat.py` and `ollama_compat.py` accessed the private `proxy._request_tokens` and `proxy._request_meta` dicts directly via `.pop()`. This broke encapsulation and coupled route logic to internal implementation details.

**Fix:** Added public methods `pop_token_counts(request_id)` and `pop_request_meta(request_id)` on `StreamingProxy`. All route handler access updated to use the public API.

---

### 5. `asyncio.ensure_future` — Deprecated API `FIXED`

**File:** `src/fleet_manager/common/discovery.py` (line ~65)
**Severity:** Low

`asyncio.ensure_future()` has been deprecated since Python 3.10 in favor of `asyncio.create_task()`. The project requires Python 3.11+, so this should be updated.

**Fix:** Replaced `asyncio.ensure_future(...)` with `asyncio.create_task(...)`.

---

### 6. Unused Dependencies and Imports `OPEN`

**Files:** `pyproject.toml`, `src/fleet_manager/server/app.py`
**Severity:** Low

- `sse-starlette` is listed in `pyproject.toml` but never imported in the source code.
- `pyyaml` is listed in `pyproject.toml` but never imported in the source code.
- `StaticFiles` is imported in `app.py` but never used.

**Fix:** Remove unused dependencies from `pyproject.toml` and the dead import from `app.py`.

---

### 7. `HeartbeatPayload.arch` — Hardcoded Default `OPEN`

**File:** `src/fleet_manager/models/` (HeartbeatPayload definition)
**Severity:** Low

The `arch` field defaults to `"apple_silicon"`, which is incorrect for non-Mac nodes (e.g., Linux/x86 or Linux/ARM).

**Fix:** Default to `platform.machine()` or similar runtime detection.

---

### 8. `event_stream()` Re-fetches State Every Tick `OPEN`

**File:** `src/fleet_manager/server/routes/dashboard.py`
**Severity:** Low

The SSE `event_stream()` function re-fetches `request.app.state` on every tick (every 2 seconds). The references should be captured once before the loop starts.

**Fix:** Capture `registry = request.app.state.registry` etc. before entering the `while True` loop.

---

### 9. Dashboard Inline HTML/CSS/JS — Growing Maintenance Burden `OPEN`

**File:** `src/fleet_manager/server/routes/dashboard.py`
**Severity:** Low (for now)

The dashboard is a large amount of inline HTML/CSS/JS in Python strings across 5 pages (Fleet Overview, Trends, Model Insights, Apps, Benchmarks). This is pragmatic for a single-file deployment but will become painful as more dashboard features are added (e.g., tag filtering on Trends/Models views).

**Fix:** When the dashboard grows further, extract to Jinja2 templates or a separate frontend build.

---

## Test Coverage Gaps

### 10. Untested Modules `PARTIAL`

**Severity:** Medium

The following modules still have zero test coverage:

- `server/rebalancer.py` — pre-warm trigger and queue move logic
- `common/discovery.py` — mDNS advertise and browse
- `common/system_metrics.py` — psutil metric collection
- `common/ollama_client.py` — Ollama HTTP client

Previously untested, now covered:
- ~~`node/agent.py`~~ — now has 6 tests in `tests/test_node/test_agent.py`

The rebalancer in particular has meaningful logic (deciding when to move pending requests, triggering pre-warm) that warrants unit tests.

---

### 11. `test_move_pending` — Tautological Assertion `OPEN`

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test asserts `moved >= 0`, which is always true for a non-negative integer. This assertion provides no verification that entries were actually moved.

**Fix:** Assert `moved >= 1` or verify the target queue received the expected entries.

---

### 12. `test_shutdown` — Vacuous Test `OPEN`

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test body is `pass  # No assertion needed`. It only verifies that no exception is raised, which provides minimal confidence.

**Fix:** Assert post-shutdown state — e.g., that worker tasks are cancelled, queues are empty, or new enqueues are rejected.

---

## Known Limitations

### 13. Meeting Detector False Positives on Dev Machines

**Severity:** Low

The macOS meeting detector (`node/meeting_detector.py`) detects active camera/microphone as "in meeting" and triggers a hard pause. Developers using webcam-based tools (video calls, streaming, screen sharing) during development will get false positives, causing the node to stop accepting work.

**Workaround:** Set `FLEET_NODE_ENABLE_CAPACITY_LEARNING=false` (the default) to disable meeting detection entirely. Tests use `@patch.object(MeetingDetector, "is_in_meeting", return_value=False)` to work around this.

---

### 14. Capacity Learning 7-Day Bootstrap Period

**Severity:** Low

The capacity learner requires 7 days of real observations to graduate from "bootstrapping" to "learned" mode. During the bootstrap period, the learner contributes less confidence to routing decisions. This cannot be validated in automated tests — it requires a week of real usage.

**Workaround:** Pre-seed the capacity learner JSON file with synthetic data if faster convergence is needed.

---

### 15. Tag Filtering Not Yet on Trends/Models Views

**Severity:** Low (feature gap)

The tagging system records tags on every trace and provides a dedicated Apps dashboard tab. However, the existing Trends and Model Insights views cannot yet be filtered by tag. Adding tag-based filtering to these views is a natural next step.

---

### 16. OLLAMA_NUM_PARALLEL Auto-Calculation Causes KV Cache Bloat and Model Thrashing `PARTIAL`

**Severity:** High

On high-memory machines (e.g., 512GB Mac Studio), Ollama's `auto` setting for `OLLAMA_NUM_PARALLEL` calculates a high slot count (e.g., 16). Each parallel slot pre-allocates KV cache for the full context window. With 16 slots and `default_num_ctx=262144`:

```
KV cache per model = 262144 ctx × 16 parallel = 4,194,304 KvSize → 384 GB
```

A single model consumes ~413 GB (17 GB weights + 384 GB KV cache + 12 GB compute), leaving no room for other models on a 464 GB VRAM machine. When a second model is requested, Ollama evicts the first — and vice versa — creating a thrashing loop that freezes the machine for 10-60 seconds per swap.

**Symptoms:**
- Models drop to 0 loaded at regular intervals (visible in herd dashboard and heartbeat data)
- Ollama logs show `"model requires more gpu memory than is currently available, evicting a model to make space"` repeatedly
- Machine freezes during model swaps (loading 88-151 GB models saturates memory bandwidth)
- `OLLAMA_KEEP_ALIVE=-1` alone does NOT fix this — eviction is space-based, not time-based

**Evidence:** Ollama server logs (`~/.ollama/logs/server-*.log`) showed eviction cascades at hourly intervals coinciding with bot-simulation model rotation. KV cache sizes confirmed via `load request` log entries showing `KvSize:4194304` with `Parallel:16`.

**Fix (user-side):** Set `OLLAMA_NUM_PARALLEL=2` (or 3-4). KV cache drops to ~20 GB per model, allowing 3-4 large models to coexist.

```bash
launchctl setenv OLLAMA_NUM_PARALLEL 2
# Restart Ollama
```

**Herd-side detection (implemented):** The health engine's `_check_kv_cache_bloat()` detects this by comparing VRAM used vs expected weight sizes. When overhead exceeds 50%, it reports CRITICAL severity with cross-platform fix instructions (macOS launchctl, Linux systemd, Windows env var). The model thrashing check (`_check_model_thrashing()`) catches the downstream symptom — frequent cold loads from eviction cascades. Both checks surface in the dashboard Health tab and `/dashboard/api/health` API.

**Remaining:** Could inject `num_ctx` overrides in proxied requests to cap context windows, but this risks changing model behavior. Current approach (detect + recommend) is safer.

---

### 21. Dynamic `num_ctx` Management Based on Actual Usage `PARTIAL`

**Severity:** Medium
**Files:** New module + `server/streaming.py`, `server/routes/dashboard.py` (settings)

Ollama allocates KV cache for the full `default_num_ctx` per model, even if most requests only use a fraction of it. A model with 131K default context uses ~67GB, but if 95% of requests only need 8K-16K context, the fleet is wasting 50+GB of memory per model on unused KV cache. This prevents loading additional models.

**Proposed approach — 3 phases:**

**Phase 1: Observe** — Track actual `num_ctx` usage per model from request traces.
- Log `prompt_eval_count` (prompt tokens) from every completed request
- Compute p50, p95, p99 of actual prompt sizes per model
- Surface in dashboard settings: "gpt-oss:120b: avg context 2K, p95 8K, p99 16K, allocated 131K"
- No behavior change — just visibility

**Phase 2: Recommend** — Use observed data to suggest optimal `num_ctx` per model.
- Dashboard shows: "Recommended: set num_ctx=32768 for gpt-oss:120b (covers p99 of your usage, saves ~50GB)"
- Health engine warns when allocated context >> actual usage by 4x+
- Settings page has a slider or input per model to set recommended `num_ctx`

**Phase 3: Auto-adjust** — Dynamically manage `num_ctx` via Ollama settings.
- Herd injects `num_ctx` in proxied requests based on learned optimal value
- If a request arrives that exceeds the current setting, Herd either:
  - a) Queues it and triggers an Ollama restart with higher `num_ctx` (slow but correct)
  - b) Passes it through with an explicit higher `num_ctx` (triggers model reload in Ollama)
  - c) Returns a warning header and serves at the current context limit
- Auto-restart Ollama if error rate spikes due to context truncation
- Settings toggle: `FLEET_DYNAMIC_NUM_CTX=true` (off by default)
- Settings page shows current vs recommended vs actual usage with toggle

**Key data already available:**
- `request_traces.prompt_tokens` in SQLite — has actual prompt sizes for every request
- Health engine already detects KV cache bloat (`_check_kv_cache_bloat()`)
- Context protection (`streaming.py`) already intercepts `num_ctx` in requests
- Dashboard settings page already has runtime toggles

**Why this matters:** On the 512GB Mac Studio, gpt-oss:120b with 131K context uses ~67GB. If actual usage is 16K context, it could use ~12GB — freeing 55GB for 2-3 additional models. This directly fixes the smart benchmark's inability to load multiple models.

---

### 17. Zombie In-Flight Queue Entries Block Concurrency Slots `FIXED`

**File:** `src/fleet_manager/server/queue_manager.py`
**Severity:** High

The queue worker adds entries to `in_flight` then hands an async generator to the route handler via a Future. If the client disconnects mid-stream or the generator is never fully consumed, `mark_completed`/`mark_failed` in the `_tracked_stream` finally block never executes. The entry stays in `in_flight` forever, permanently consuming a concurrency slot.

In production, 5 of 8 slots became zombied, causing the router to accept new connections but never process them (0 bytes returned after 2 minutes).

**Fix:** Added a background reaper task that runs every 60s and removes any in-flight entries older than 15 minutes (past the 10-minute Ollama read timeout). Reaped entries are marked as failed. The reaper starts automatically via `queue_mgr.start_reaper()` during app lifespan.

---

### 18. mDNS `NonUniqueNameException` Prevents Router Restart `FIXED`

**File:** `src/fleet_manager/common/discovery.py`
**Severity:** High

When the router crashes or is killed without clean shutdown, the zeroconf mDNS service registration persists in the network. On restart, `async_register_service()` raises `NonUniqueNameException` because the stale service name is still registered by the OS, causing the router to fail to start entirely.

**Fix:** Wrapped registration in try/except. On `NonUniqueNameException`, close the zeroconf instance, create a fresh one, and re-register with `allow_name_change=True`. This handles both stale registrations and concurrent instances gracefully.

---

### 19. Duplicate Queues from Unnormalized Model Names `FIXED`

**File:** `src/fleet_manager/models/request.py`
**Severity:** Medium

Ollama returns model names with explicit tags (e.g., `qwen3-coder:latest`) but client requests often omit the tag (e.g., `qwen3-coder`). This caused duplicate queues (`node:qwen3-coder` and `node:qwen3-coder:latest`), scoring mismatches, latency cache misses, and broken pre-warm tracking. Dashboard showed two separate queue cards for the same model with split stats (20 done vs 4520 done).

**Fix:** Added a Pydantic `model_validator` on `InferenceRequest` that appends `:latest` to model names (and fallback_models) that lack a tag. Normalization happens at construction time so all downstream code sees consistent names.

---

### 20. Client `num_ctx` Triggers Full Model Reload and Hang in Ollama `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Critical

When a client sends `num_ctx` in request options that differs from the loaded model's context window, Ollama's scheduler calls `needsReload()` and triggers a full model unload+reload. For large models (89GB `gpt-oss:120b`), this causes multi-minute hangs or complete deadlocks — 0 bytes returned. Reproduced: `num_ctx: 4096` on a model loaded at 32768 hangs indefinitely; without `num_ctx` works in 3 seconds. Confirmed directly against Ollama (bypassing Herd) — Ollama itself hangs.

Root causes compound: GPT-OSS minimum context override (Ollama bumps `num_ctx < 8192` to 8192), runner startup timeout exceeded during 89GB reload, and KV cache fill loop on small context values.

**Fix:** Added context-size protection (`FLEET_CONTEXT_PROTECTION=strip` by default) in `_build_ollama_body()`. Strips `num_ctx` when ≤ loaded context (prevents needless reload). When `num_ctx` > loaded context, searches fleet for a loaded model with sufficient context and more parameters, and auto-switches. Logged for operator visibility.

---

### 21. Stream Error Messages Are Empty Strings `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Medium

Failed request traces in the trace store have empty `error_message` fields. The `logger.error()` calls in `_stream_with_tracking` and `_stream_with_retry` format the exception with `{e}` but the exception objects sometimes stringify to empty strings (e.g., `httpx.RemoteProtocolError` with no message). This makes post-mortem debugging blind — you can see a request failed but not why.

**Fix:** Changed all `str(e)` to `f"{type(e).__name__}: {e}"` in stream error paths. Now error messages always include the exception class (e.g., `RemoteProtocolError:` instead of empty string). Applied in both `_stream_with_tracking` and `_stream_with_retry`.

---

### 22. Client Disconnects Recorded as "completed" `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** High

When a client disconnects mid-stream (HTTP timeout, connection drop), FastAPI sends `GeneratorExit` to the streaming generator. Both `_stream_with_tracking` and `_stream_with_retry` caught this but marked the request as **completed** — silently hiding failures from the dashboard and trace store.

**Observed:** 2026-04-01. Another agent reported "4 fetch failed — Ollama connection drops on large payloads" but the dashboard showed only 1 failed request out of 24,650. The disconnect failures were all recorded as successful completions.

**Fix:** `GeneratorExit` now records status `"client_disconnected"` and calls `mark_failed` instead of `mark_completed`. The trace store gets the real status so the dashboard accurately reflects failure rates.

---

### 23. Incomplete Streams (No done:true) Recorded as "completed" `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** High

If Ollama drops the TCP connection after sending partial data but without raising an exception, httpx's `aiter_lines()` stops iterating cleanly. The `finally` block saw `error_occurred = False` and marked it "completed" — even though the response was truncated and Ollama never sent the final `done: true` chunk.

**Fix:** After the stream loop completes without error, check if `_request_tokens` has an entry for this request (only populated when `done: true` is parsed in `stream_from_node`). If missing, record as `"incomplete"` and call `mark_failed`. This catches Ollama process deaths, OOM kills, and silent connection drops.

---

## Future Considerations

- **Extract dashboard frontend** — see issue #9 above
- **`event_stream()` optimization** — see issue #8 above
- **Tag filtering on Trends/Models** — see issue #15 above
- **`collector.py` catch-all** — silently returns empty metrics when Ollama is unreachable, which could mask bugs during development. Consider logging at `WARNING` level.

### #21 — Empty error messages on timeout failures `FIXED`
**File:** `server/streaming.py`
**Severity:** Low
**Problem:** httpx timeout exceptions have empty `str(e)`, so `f"{type(e).__name__}: {e}"` produces `ReadTimeout: ` with no details.
**Fix:** Use `repr(e)` as fallback when `str(e)` is empty: `f"{type(e).__name__}: {repr(e)}"`. Now captures the exception args (timeout value, URL, etc.) even when the string representation is empty.

---

### 22. Custom Date Range Selector for Dashboard Pages `FIXED`

**Files:** `src/fleet_manager/server/routes/dashboard.py`, `src/fleet_manager/server/trace_store.py`
**Severity:** Low (feature enhancement)

The Trends page has preset time buttons (24h, 48h, 72h, 7d) but no custom date/time range selector. The Model Insights and Apps pages have a `days` parameter but no time range UI at all.

**Proposed fix:**

1. **Shared date range component** — reusable across Trends, Model Insights, and Apps pages:
   - Preset buttons: 24h, 48h, 72h, 7d, 30d
   - Custom range: two datetime-local inputs (start, end)
   - All times in user's local timezone (JS `Date` handles this natively)
   - Component stores selection in URL params for shareability

2. **Backend changes:**
   - Add `start_ts` and `end_ts` query params to `/dashboard/api/trends`, `/dashboard/api/models`, `/dashboard/api/apps`
   - TraceStore queries already filter by timestamp — just expose the params
   - Timezone conversion: frontend sends UTC timestamps, backend uses them directly (traces are stored as Unix timestamps)

3. **Pages to update:**
   - Trends: replace current time buttons with shared component
   - Model Insights: add time range component (currently hardcoded to `days` param)
   - Apps: add time range component (currently hardcoded to `days` param)

---

## Model Management

### No priority/pinned model concept — restarts can evict primary models `FIXED`

**Severity:** High
**Discovered:** 2026-04-16 — during vision embedding testing, repeated fleet restarts (`pkill -9`) caused `gpt-oss:120b` (89GB, primary reasoning model) to be unloaded. VRAM fallback then routed requests to `gemma3:27b` (42GB), which loaded and consumed the memory `gpt-oss:120b` needed. Result: primary model evicted, replaced by a less capable one.

**Root cause:** No concept of "this model must always be loaded." VRAM fallback picks whatever is loaded without considering model importance. Ollama's `OLLAMA_KEEP_ALIVE=-1` keeps models loaded but can't prevent eviction when memory is consumed by other models loading first after a restart.

**Proposed fix:**
1. Add `FLEET_PINNED_MODELS` config — comma-separated list of models that must always be loaded (e.g., `gpt-oss:120b,nomic-embed-text`)
2. After node restart, load pinned models first before accepting other requests
3. VRAM fallback should never route to a non-pinned model if a pinned model exists for that category
4. Health check: WARNING if a pinned model is not loaded
5. Dashboard Settings: UI to manage pinned models

**Files:** `server/streaming.py` (VRAM fallback), `node/agent.py` (startup model loading), `models/config.py` (pinned models config), `server/health_engine.py` (health check)

### CoreML provider triggers macOS TCC dialog that freezes the node agent `FIXED`

**Severity:** Critical
**Discovered:** 2026-04-19 — after adding the vision embedding service, the node agent began freezing overnight. User reported a macOS permission dialog appearing asking Python for access. The dialog blocks the Python process indefinitely, causing heartbeats to stop, router marks node offline, all inference fails until someone dismisses the dialog.

**Pattern:** Consistent 120-130 errors/hour from midnight through 8 AM. Not random drops — a steady multi-hour outage until the user returns to the machine and dismisses the dialog. Happened twice in 5 days (April 14 and April 19).

**Root cause:** `CoreMLExecutionProvider` in `ONNXBackend.__init__` was enabled automatically on macOS. On first inference, CoreML compiles the ONNX model for the Neural Engine, which can trigger macOS TCC permission dialogs (Neural Engine access, Desktop folder access if cache scans adjacent paths). Once the dialog appears, the subprocess and the entire Python process block waiting for user interaction.

**Fix (commit d61d3cb+):** Default to CPU-only inference. CPU is fast enough on M-series chips (~60ms/image for DINOv2). Users can opt-in to CoreML via `FLEET_EMBEDDING_USE_COREML=true` with a warning in the logs.

**Files:** `src/fleet_manager/node/embedding_models.py`

---

### Queue concurrency ignores OLLAMA_NUM_PARALLEL — allows 8 in-flight but Ollama only runs 2 `FIXED` (2026-10-02, enforced)

**Fixed 2026-10-02, verified live.** A worker now holds its slot until the request leaves the queue. Every exit releases it: `mark_completed`, `mark_failed`, the reaper, and `_settle_on_exit`, a wrapper around every dispatched stream that settles the request however the stream ends. Four concurrent requests to `gemma3:27b` went from `in_flight=4, pending=0` to **`in_flight=1, pending=3`**, stepping `(1,3)→(1,2)→(1,1)→(1,0)`, all answered.

Building it found a regression of its own, now fixed. Starlette ends a disconnected streaming response by cancelling the task, and mid-generation that arrives as `CancelledError`. `_stream_with_retry` caught only `GeneratorExit` and `Exception`, so the request never left `in_flight`. With slots now held, a client disconnecting mid-stream froze the model's queue until the reaper: **measured 641.2 s** before the next request ran. After the fix: **3.5 s** on `/api/chat`, and **11.3 s** on `/v1/chat/completions`, where the remaining delay is herd taking ~10 s to *notice* the disconnect (Ollama's own log shows it generating for 10.39 s; pre-existing, separate). Disconnects mid-generation are also now traced as `client_disconnected`; before, most left no trace.


**Re-opened 2026-10-02.** This was marked `FIXED` earlier the same day on the assumption that `decode_parallelism_for()` caps each queue. It computes the right number, now per model (post-0.35 Phase 1), but **nothing enforces it**. Measured live on the Mac mini: four concurrent `/api/chat` requests to `qwen3.8:27b-mlx` showed herd's queue at `concurrency=1` with **`in_flight=4`, `pending=0`**. Completion times stepped 3.6 → 6.8 → 9.5 → 12.8 s, so the backend ran them one at a time while herd reported all four as in flight.

**Root cause** (`server/queue_manager.py` `_worker`, unchanged since the 2026-03-07 initial commit): the worker pulls an entry, calls `process_fn(entry)`, and gets back an **unconsumed async generator**, since every `process()` in `streaming.py` returns `AsyncIterator[str]`. It hands that to the route via the future and immediately loops for the next entry. The request to Ollama runs later, while the route consumes the generator, so a worker never holds its slot for a request's duration. One worker dispatches the whole pending queue at once. `concurrency` bounds how fast generators are handed out, not how many requests are at the backend.

**Impact:** the original symptom (surplus requests queue *inside Ollama*, invisible to herd's depth and wait estimates) has been present since day one, for every model. The per-model limits from Phase 1 (MLX serial, the `sched.go` families) are correct values with no effect until this is fixed. `_score_wait_time` uses `depth × p75`, so wait estimates are still roughly right in aggregate, but herd can't reorder or reject work that has already left its queue.

**Proposed fix (needs its own design and soak, since it's the hot path for every request):** a worker holds its slot until the entry completes. For example, it awaits an event that `mark_completed` / `mark_failed` set, with the zombie reaper (`_reap_stale_in_flight`) also releasing it, so a route that never reports completion can't wedge a worker forever. Expect visible behavior changes: requests over the limit wait in herd's queue (where `client_max_in_flight`, holding-queue timeouts and the dashboard now see them) instead of inside Ollama.

**Original report (2026-04-16):**

**Severity:** Medium
**Discovered:** 2026-04-16 — dashboard always shows "1/8 in-flight" regardless of model or node. On a 512GB machine the concurrency formula always hits the `_MAX_CONCURRENCY=8` cap because headroom is massive (436GB / 2GB per slot = 218, clamped to 8).

**Root cause:** `compute_concurrency()` in `queue_manager.py` calculates slots from memory headroom divided by estimated KV cache cost (2GB), then clamps to `[1, 8]`. It has no knowledge of `OLLAMA_NUM_PARALLEL`, which controls how many requests Ollama actually processes simultaneously. With `OLLAMA_NUM_PARALLEL=2`, the queue allows 8 in-flight but Ollama queues anything beyond 2 internally, adding unnecessary latency.

**Impact:** On a 512GB machine with `OLLAMA_NUM_PARALLEL=2`:
- Queue reports 8 concurrency slots
- Ollama processes 2 at a time
- 6 requests sit in Ollama's internal queue, invisible to Herd's scoring
- Scoring engine thinks the node has capacity when it's actually backed up
- Wait time estimates are wrong

**Proposed fix:**
1. Node agent reads `OLLAMA_NUM_PARALLEL` from environment or Ollama's config and reports it in the heartbeat
2. `compute_concurrency()` uses `min(memory_slots, ollama_num_parallel)` instead of just memory slots
3. If `OLLAMA_NUM_PARALLEL` is not reported, fall back to current memory-based calculation
4. Dashboard shows actual concurrency (e.g., "1/2" not "1/8")

**Files:** `server/queue_manager.py` (compute_concurrency), `node/collector.py` (read OLLAMA_NUM_PARALLEL), `models/node.py` (add to heartbeat)

**Files:** `server/streaming.py` (VRAM fallback), `node/agent.py` (startup model loading), `models/config.py` (pinned models config), `server/health_engine.py` (health check)

---

### Ollama 3-model concurrent-load cap unconfigurable on macOS (upstream) `OPEN`

**Severity:** Medium
**Discovered:** 2026-04-22 — during Claude Code + ollama-herd setup on a 512GB M3 Ultra Mac Studio
**Upstream:** [ollama/ollama#7041](https://github.com/ollama/ollama/issues/7041), [#4855](https://github.com/ollama/ollama/issues/4855), [#5722](https://github.com/ollama/ollama/issues/5722), [#14953](https://github.com/ollama/ollama/issues/14953)

**Symptom:** Ollama 0.20.4 on macOS refuses to keep more than 3 models concurrently hot in VRAM, regardless of `OLLAMA_MAX_LOADED_MODELS` configuration. Loading a 4th model always evicts one of the existing three (LRU). Silently causes herd's VRAM fallback to fire and degrades Claude Code tool-use quality when mapped models get evicted.

**Root cause (partial):** From Ollama source (`envconfig/config.go`):

```go
MaxRunners = Uint("OLLAMA_MAX_LOADED_MODELS", 0)
```

- `Uint` parses the env value as **unsigned integer**
- `-1` cannot be parsed as unsigned → silently falls through to default `0`
- `0` resolves to `defaultModelsPerGPU = 3` in the scheduler

So setting `OLLAMA_MAX_LOADED_MODELS=-1` (a common "I want unlimited" pattern that propagated via our shell init files) was silently invalid. **But setting a positive integer doesn't fix it either** — see test table below.

**Test evidence (all on Ollama 0.20.4 / macOS 15.x / M3 Ultra 512GB):**

| Attempt | Process env (`ps eww`) | Cap behavior |
|---|---|---|
| `launchctl setenv OLLAMA_MAX_LOADED_MODELS 10` (confirmed via `launchctl getenv`) | shows `-1` | still 3 |
| Plist `EnvironmentVariables.OLLAMA_MAX_LOADED_MODELS=10` at `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` | regenerated by brew on restart, stripped | still 3 |
| `~/.zshrc` `export OLLAMA_MAX_LOADED_MODELS=10` | only affects new shells, not GUI Mac App | still 3 |
| Direct CLI: `OLLAMA_MAX_LOADED_MODELS=10 /Applications/Ollama.app/Contents/Resources/ollama serve` | process still shows `-1` | still 3 |
| Full kill (Mac App + all runners) + fixed launchctl + clean relaunch via `open -a Ollama` | process still shows `-1` | still 3 |
| Load 4 **distinct** models (different weight blobs) to rule out shared-blob conflict | — | 4th evicts LRU; memory not the constraint |

**Memory was never the issue.** During the 4-distinct-model test: 358 GB of RAM available, 149 GB hot, Ollama still refused the 4th load. The dashboard shows ~292 GB used / 512 GB — plenty of headroom.

**Impact on ollama-herd:**

- **VRAM fallback fires silently** — when a mapped model in `FLEET_ANTHROPIC_MODEL_MAP` isn't hot, requests fall back to nearest available model. Debugged via `x-fleet-fallback` response header. For Claude Code specifically, this means tool-heavy requests hit weaker models (e.g. gemma3:4b) that can't emit `tool_calls` cleanly, breaking the agent loop.
- **Typical Claude Code fleet needs 4+ hot models** — 1 for haiku, 1 for sonnet, 1 for opus (or pull 480B), 1 for vision, plus user's non-Claude-Code scripts. Currently forced to cap at 3 and accept reload cost on the rest.
- **Captures of this failure mode are invisible** until you check the trace DB — no health check currently surfaces it.

**Proposed workarounds:**

1. **Accept the 3-cap** (current behavior) — pick the 3 most critical models per node, accept reload cost on others
2. **Second Ollama instance on another port** — each daemon has its own 3-slot budget; register both as separate nodes or route-target in herd. Doubles capacity to 6 hot.
3. **`mlx-lm.server` for specific heavyweight models** — bypass Ollama entirely; no cap, plus potential 2× decode speed and native prefix caching. Moderate engineering.
4. **Wait for upstream fix** — the pattern of `Uint` + silent `-1` failure is reported across multiple open issues; unclear if anyone is working on it.

**Proposed fix in herd (already planned):** see `docs/plans/hot-fleet-health-checks.md` — adds six health checks that would surface this failure mode within one heartbeat interval instead of requiring trace DB archaeology. Specifically check #3 (`ollama_max_loaded_models_observed`) infers the effective cap from observed behavior rather than reading the unreliable env var.

**Files (herd-side mitigation only):** `server/health_engine.py`, `node/collector.py` (optional: report observed cap in heartbeat payload)

---

## OPEN — Codex `/v1/models` schema is an unbounded decode chain

**Severity:** low (cosmetic for the CLI; empties the Desktop model picker)
**Found:** 2026-07-18 driving a real `codex-cli 0.145.0-alpha.18`

Codex decodes `/v1/models` against its own **undocumented, strictly-typed**
schema and fails the *entire* decode on the first problem. Each field added
reveals exactly one more. We currently emit 20 fields discovered this way,
including two closed enums (`shell_type`, `visibility`) and a nested
`truncation_policy` struct. **Next known requirement:
`experimental_supported_tools`.**

Not converged, and treated as maintenance rather than a milestone. The CLI is
unaffected (`-m` bypasses the picker), but an undecodable payload leaves the
Desktop picker empty, which pushes Desktop onto its built-in `sol`/`luna`/`terra`
Lite slugs — a materially different code path.

**Discovery loop:** add the field → restart herd → run `codex exec` **three**
times → `grep -o 'missing field \`[a-z_]*\`'`. A *single* run after a restart
reports a false clean (the models refresh hasn't fired yet); so does a dead
server (no response, no decode error). Assert liveness in the same breath.
Wrong enum values are useful — the error names the valid variants.

**Files:** `server/routes/openai_compat.py` (`list_models`)

---

## OPEN — `write_stdin` round-trips but local models can't drive it

**Severity:** low
**Found:** 2026-07-18

The protocol path works — Codex accepts the call and returns output. But
`qwen3-coder:30b` could not drive an interactive session: it started `python3 -i`
via `exec_command`, sent input with `write_stdin`, received the echo rather than
the evaluated result, retried several times with different framings, and gave
up. In an earlier run it then *claimed* the session printed `42` — a
confabulation (`6*7` is derivable without executing anything).

No herd-side defect identified. Recorded so the next person doesn't re-derive
it, and as a caution: verify interactive-session claims against the tool blocks,
never the model's summary.

---

## OPEN — one `apply_patch` call bypassed the redirect, unreproduced

**Severity:** low
**Found:** 2026-07-18

During tool-coverage testing, `error=unsupported call: apply_patch` fired once
while the redirect fired 4 times in the same window — so a single call had a
shape `_patch_text_from_args` did not recognise. Not reproduced since.

The decline path is now audible (`no recognisable patch envelope (keys=…)`),
so a recurrence names the arg keys and is fixable in one pass instead of
requiring another debugging round.

**Files:** `server/responses_translator.py` (`_patch_text_from_args`)

---

## RESOLVED (not a bug) — `glm-4.7-flash` slowness is contention, not the model or its context

**Severity:** none for herd — the behaviour is a scheduling consequence, not a defect
**Found:** 2026-07-19 · **Root-caused:** 2026-07-19

**Symptom:** requests to `glm-4.7-flash:latest` took 500–640s, then failed at 1,204–5,205s.

**Answer:** decode throughput tracks concurrent fleet traffic almost monotonically. Measured across 22 production calls by counting requests that overlapped each one:

| overlapping non-glm requests | glm decode |
|---|---|
| 0 | **45.3 tok/s** |
| 2–9 | 34.8–35.9 |
| 15–22 | 22.7–32.0 |
| 27–34 | 10.0–17.7 |
| 38–66 | **6.4–7.8** |

A clean ~7× dose-response curve. These MoE models are memory-bandwidth-bound on Apple Silicon, and bandwidth is shared.

**Two wrong theories, and how they died** — worth recording because both were plausible and one nearly got "confirmed":

1. *Prefill regression* (this model has a **fixed** prefill issue on record, so it is the natural suspect). Killed by `time_to_first_token_ms`: prefill is a healthy 13–35s even on the failures; all degradation is decode.
2. *Residency at ctx=202752 / 63.2 GB, with the 32768 override never applied.* Killed by reproducing the **exact** production shape (14,322-token prompt → 3,365 generated) in isolation **at that same residency**: **105 seconds**, versus 500–640s in production. Same context, same model, 5–6× faster with an idle fleet.

   The proposed fix was to unload the model so it would cold-load at 32768. That would have appeared to work — the unload also drains the queued backlog — and the recovery would have been credited to the context change. A fix that works for the wrong reason is worse than no fix.

**A measurement error worth not repeating:** the "78.5 tok/s in isolation" figure that framed this whole investigation used a **40-token prompt**. At the real ~14K prompt it is **32 tok/s**; decode degrades with used context (79.9 → 40.2 → 31.9 as the prompt grows). Benchmark the workload's shape, not a convenient one.

**Why it escalates to failure:** an external caller polls this model about every 10 minutes. Under load a call takes 8–10 minutes, which exceeds the interval, so the next request stacks behind it — and each queued request slows the others further. Self-reinforcing. The four failures share one timestamp with decode times in a ~600s ladder (1,170 / 3,981 / 4,587 / 5,192), which is one pile-up, not four slow requests.

**Mitigations** (caller-side or scheduler-side, not model-side):
- Cap per-model concurrency so requests queue cleanly instead of degrading each other — see the bandwidth-aware concurrency issue below.
- Lengthen the poll interval past p99-under-load (~15 min) so calls cannot overlap.
- Reduce output length; 3,500–5,000 tokens at a 10-minute cadence is the real driver.

---

## PARTLY RESOLVED — `compute_concurrency` sized queues by memory capacity, and the bottleneck is neither capacity nor bandwidth

**Severity:** Medium — costs latency under load on every large-model queue; no correctness impact
**Found:** 2026-07-19, root-causing the `glm-4.7-flash` slowdown above
**Files:** `server/queue_manager.py` (`compute_concurrency`, `_compute_queue_concurrency`), `server/hardware_lookup.py`, `server/scorer.py`

### The mismatch

```python
# server/queue_manager.py
def compute_concurrency(available_memory_gb: float, model_size_gb: float) -> int:
    headroom = available_memory_gb - model_size_gb
    slots = int(headroom / _KV_CACHE_PER_REQUEST_GB)
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, slots))   # [1, 8]
```

Purely capacity-driven: "how many KV caches fit in RAM?" On a 512 GB M3 Ultra the headroom is always vast, so it returns the ceiling of **8** for every queue — confirmed live on both `bb:gpt-oss:120b` and `bb:qwen3-coder:30b`.

But nothing about this hardware is capacity-limited during inference. MoE decode on Apple Silicon is **memory-bandwidth-bound**, and bandwidth is shared across every concurrently-decoding model. Admitting 8 concurrent requests doesn't use idle capacity; it splits a fixed bandwidth budget 8 ways.

### Evidence

Measured on this fleet (see the glm entry above): decode throughput vs. overlapping requests — 45.3 tok/s at zero overlap, 6.4 tok/s at 38–66. A ~7× swing driven entirely by concurrency.

And from the 0.32.1 upgrade research, per-request vs aggregate on qwen3-coder:

| concurrent | tok/s per request | aggregate |
|---|---|---|
| 1 | 107.3 | 107 |
| 2 | 80.2 | 160 |
| 4 | 52.7 | 211 |

Aggregate genuinely rises, so this is a **latency/throughput trade, not free loss** — an earlier claim that concurrency bought "only ~10% aggregate" came from contention-polluted traces and was wrong. The point is that the trade is currently being made *blind*: nothing in the decision knows bandwidth exists.

### Why this is cheap to fix

The data is already present and already trusted elsewhere. `hardware_lookup.resolve_bandwidth()` returns 819 GB/s for this node's `Apple M3 Ultra`, it is populated on the live heartbeat (`node.hardware.memory_bandwidth_gbps`), and `scorer.py` already consumes it for signals 3/4/5. `compute_concurrency` is the one place that ignores it.

### Prototype sketch

Derive slots from bandwidth per in-flight stream rather than RAM per KV cache, keeping capacity as an upper bound:

```
bytes_per_token ≈ active_params × bytes_per_weight        # MoE: active experts, not total
streams_at_target ≈ (bandwidth_gbps × utilisation) / (bytes_per_token × target_tok_s)
slots = clamp(min(capacity_slots, streams_at_target), 1, 8)
```

Open questions the design has to answer:
- **What is the target?** A fleet tuned for interactive coding wants per-request latency; a batch benchmark wants aggregate. This probably needs to be a policy knob, not a constant — and per-model, since a compaction model and an interactive model want opposite answers.
- **Where does `active_params` come from?** `model_knowledge` has expert counts for some models; Ollama's `/api/show` exposes `expert_used_count` and `expert_count`. Prefer measured over declared, consistent with how KV cost per token is already learned from heartbeat data.
- **Does it need to be adaptive?** The honest version measures achieved tok/s per queue from the trace store (that data exists) and closes the loop, rather than predicting from a static table.
- **Interaction with `OLLAMA_NUM_PARALLEL`.** Ollama has its own admission limit (currently 4 on this fleet). Herd handing 8 workers to a backend that runs 4 means the extra just queue inside Ollama, where herd cannot see or reorder them. The two limits should be reconciled, and the node already reports its cap in the heartbeat.
- **Multi-model contention is the actual case.** The glm collapse was caused by *other models'* traffic, so a per-queue cap alone does not solve it. A node-level bandwidth budget shared across queues is the more correct model, and considerably more invasive.

### Controlled sweep (2026-07-19, glm-4.7-flash, idle fleet, ~1.5K prompt, 300 tok out)

| N | per-stream tok/s | aggregate tok/s |
|---|---|---|
| 1 | 74.7 | 63.0 |
| 2 | 56.4 | 91.6 |
| 3 | 43.7 | 125.8 |
| **4** | 35.5 | **137.5** |
| 6 | 42.6 | 127.8 |
| 8 | 35.3 | 137.8 |

**Aggregate saturates at N=4 and never improves.** Per-stream halves from 1→4. So slots 5-8 buy *nothing* in throughput and cost latency — herd's current ceiling of 8 is strictly worse than 4 on this hardware, under any policy.

**But the plateau is almost certainly not the bandwidth knee — it is `OLLAMA_NUM_PARALLEL=4`.** Ollama admits 4; requests 5-8 queue *inside* Ollama where herd cannot see, reorder or reject them. That fully explains why N=6 and N=8 match N=4 aggregate, and it makes the N=6 per-stream reading (42.6, higher than N=4's 35.5) measurement noise from uneven queue draining rather than signal.

Two consequences:

1. **The cheapest correct fix is not a bandwidth model at all** — it is to stop handing a backend more concurrent work than it will admit. The node already reports its cap in the heartbeat (`OllamaMetrics.max_loaded_models` exists; `num_parallel` should join it), and `hot_model_cap_for(node)` is the established pattern for consuming such a value. Capping queue concurrency at the backend's own parallelism converts invisible in-Ollama queueing into visible herd queueing, which is schedulable.
2. **The real bandwidth knee is still unmeasured.** Finding it requires sweeping `OLLAMA_NUM_PARALLEL` itself (1, 2, 4, 8) with an Ollama restart per step, and repeating per model class. Until then, any bandwidth formula would be fitted to a curve that is really an admission limit.

### What shipped (2026-07-19)

**Concurrency is now capped at the backend's admission limit** — `min(memory_slots, OLLAMA_NUM_PARALLEL)`, commit `075348e`. Queues on this fleet went 8 → 4, verified live. That is the change nothing in the research argued against, and the N=8 retrograde measurement below is its justification.

**Not shipped, deliberately:** the bandwidth-aware model this issue was opened to build. The premise did not survive (see below). Also not shipped: queue reordering (measured no-op at this fleet's queue depths), chunked prefill (already optimal), and USL fitting (unfittable at `num_parallel=4`).

**Still open:** whether a *node-level* budget shared across model queues is worth building. The original failure was cross-model contention, which a per-queue cap cannot see. Nothing in the LLM-serving literature does this; the template would be Heracles (ISCA'15), which enforced DRAM bandwidth indirectly by throttling the co-runner's concurrency.

### ⚠️ Premise correction (2026-07-19, later) — it is NOT bandwidth-bound

This issue was opened as "bandwidth-aware concurrency." **That framing is wrong**, and the evidence needs no byte estimates:

- **gpt-oss-20b decodes at 116.08 t/s on M2 Ultra and 115.52 t/s on M3 Ultra** — flat, despite the Ultra's extra bandwidth ([llama.cpp #15396](https://github.com/ggml-org/llama.cpp/discussions/15396), maintainer's own numbers).
- **Qwen3-30B-A3B q4 hits 113.33 t/s on a 546 GB/s M4 Max**, versus our ~107 t/s on an **819 GB/s** M3 Ultra.

More bandwidth buys nothing at batch 1. Arithmetic on achieved traffic agrees: a single stream reaches only **~25%** of 819 GB/s on qwen3-coder and **~10–18%** on glm-4.7-flash.

**The likely real constraint is GPU occupancy.** At decode, `mul_mat_id` dispatches a grid of `n_expert_used × n_tokens` independent *small* mat-vecs — each too small to fill an 80-core GPU. That explains why the wider Ultra gains nothing over the Max. (Structural cost read from [`ggml-metal-ops.cpp`](https://github.com/ggml-org/llama.cpp/blob/master/ggml/src/ggml-metal/ggml-metal-ops.cpp); no maintainer asserts it is *the* measured bottleneck, and there is **no upstream issue tracking MoE decode on Metal** — a GitHub search for issues titled `metal`+`moe` returns zero.)

Also worth knowing: **`mul_mat_id` has no scattered row-gather at decode** — it does pointer arithmetic into a contiguous expert block, then a dense mat-vec. The "uncoalesced reads" hypothesis does not describe the implementation. And every merged Metal MoE optimisation PR (#12612, #13388, #15541) improves **prefill only**; none claims a decode win.

### The number that should drive `OLLAMA_NUM_PARALLEL`

From `llama-batched-bench` on M2 Ultra, gpt-oss-20b, npp=1024/ntg=32 ([#18308](https://github.com/ggml-org/llama.cpp/discussions/18308)):

| B | prefill t/s | decode aggregate | per-stream |
|---|---|---|---|
| 1 | 2361 | 128.8 | 128.8 |
| 4 | 2409 | 211.1 | 52.8 |
| 8 | 2420 | 245.7 | 30.7 |
| 24 | 2420 | 361.1 | 15.0 |

**Prefill throughput is dead flat at every batch size.** It is already compute-saturated at B=1. For agentic coding — high prompt:generation ratio — *total* throughput moves only **1.41×** across the entire B=1→32 sweep. Batching helps decode and does nothing for the phase that dominates our traffic.

Corroborating on Ollama specifically: with the default `num_parallel=1`, aggregate is flat from concurrency 1→8; setting 4 raises aggregate ~1.8× **but is slower at concurrency 1** (18.4 vs 21.7 t/s). Enabling slots costs single-stream latency.

### What the literature does (and does not) do

Two independent audits of ~20 SLO-aware serving systems (vLLM, SGLang, Dynamo, SCORPIO, SOLA, TaiChi, BucketServe, VoltanaLLM, CONCUR, SLOs-Serve, …):

- **Nobody targets p99 as a *control objective*.** Percentiles appear only as evaluation statistics. Every system uses per-request deadlines aggregated to a fraction-satisfied rate.
- **Nobody measures memory bandwidth as a live signal.** Several invoke memory-boundedness rhetorically, then substitute analytical proxies — token counts, KV lengths, batch sizes. Confirmed by full-text search on the three most likely candidates.
- **The dominant pattern is certainty-equivalence feedforward:** predict latency → solve a constraint → set the knob, with no corrective path for prediction error. `vLLM`'s `max_num_seqs` and `max_num_batched_tokens` are confirmed **static config**.
- The technique we want — sample completed-request latency, estimate safe concurrency, shed — **exists and is mature outside LLM serving**: Envoy's adaptive concurrency filter (gradient controller on p90 `sampleRTT`, 100ms windows), Netflix's gradient concurrency-limits. **But Envoy is structurally blind to streaming**: it measures whole-request RTT, where a long response and a slow response look identical. That is precisely why TPOT — not latency — has to be the controlled variable here. The pattern needs adapting, not copying.

**The one genuine feedback loop in production LLM serving is SGLang's, and it is worth studying.** `new_token_ratio` is a discount factor on each request's declared `max_new_tokens`, AIMD-shaped: on KV exhaustion the scheduler retracts requests and jumps the ratio up based on *observed* decode progress; on healthy steps it decays down over ~600 steps. The signal is **behavioural, not memory-physical** — it learns that most requests hit EOS well before `max_new_tokens`, which is the single most predictive admission signal anyone has shipped. The knob is a KV-consumption estimate rather than a concurrency cap, and the objective is avoiding retraction rather than any latency SLO, but the shape is right.

**Correction to the entry above:** Sarathi-Serve's token budget is **static**, from the paper's own text — *"We leverage Vidur, a LLM inference profiler and simulator to determine the token budget"* and *"dynamically varying the token budget… We leave this exploration for future work."* Concrete values in its eval: 2048 relaxed, 512 strict. It also does **no admission control** — it only composes batches. Chunked prefill remains a real and relevant lever for us; it is just not an adaptive one, and I previously implied otherwise.

**Everyone else sizes admission from static memory arithmetic.** vLLM's real defaults come from a hand-maintained lookup table keyed on `device_memory >= 70 GiB` — including an A100 carve-out that is empirical folklore encoded in source (`"Setting large max_num_batched_tokens for A100 reduces throughput, see PR #17885"`). TGI profiles once at startup, and on flash-attention models **ignores** `--max-batch-total-tokens` entirely. TensorRT-LLM's are build-time. Its `AutoTuner`, routinely cited as runtime batch tuning, is a **kernel tactic selector** — citing it for admission is a category error.

### Research findings (2026-07-19) — and they argue against leading with a cap

**MoE batching does not amortise like dense, which is the whole story.** Dense decode reads the weight set once per step regardless of batch, so batching is near-free throughput. With top-k-of-N routing, batching B streams activates the *union* of their experts — expected unique experts `N·(1-(1-k/N)^B)` — so weight bytes grow with B. A two-term model (dense bytes once + expert bytes by that fan-out) fitted on **only** our B=2 point predicted B=4 within **3.8%** (219 predicted vs 211 observed). A dense 30B would have gone 107→214→428; we got 211. Roughly half the dense benefit, and the tax is expert fan-out.

**Decode across llama.cpp slots IS genuinely batched.** Our own aggregate rising 107→160→211 proves it behaviourally — pure interleaving would have stayed flat at ~107. So concurrency is a real throughput win here, just a sharply diminishing one. (Source-level confirmation of a single `llama_decode` over a multi-slot batch: unverified.)

**Apple Silicon offers no hardware lever.** Verified from the macOS SDK headers: `MTLCommandQueue` exposes only `label` and `device`; the sole `priority` in Metal is `MTLIOPriority` on *asset-loading* queues. No MPS/MIG equivalent, no GPU compute QoS. Any budget must be enforced by admission control above the runtime — which makes the router the only possible enforcement point.

**A node-level bandwidth budget across model queues is novel in LLM serving, but has strong precedent in datacenter QoS.** Checked Clockwork, AlpaServe, MuxServe, Prism, ServerlessLLM, Salus — all budget capacity and/or compute time; none budget bandwidth. MuxServe is closest and partitions SMs, after measuring that decode latency is flat from 30%→100% SM allocation (i.e. it detects decode is bandwidth-bound, then budgets the resource that isn't the bottleneck). The template to copy is **Heracles** (ISCA'15), which had the same problem for DRAM — no hardware mechanism existed, so it measured aggregate bandwidth and throttled the co-runner's *concurrency*. That validates concurrency as the actuator even when bandwidth is the resource.

### The metric: TPOT, validated on our own traces

```
TPOT = (latency_ms - time_to_first_token_ms) / (completion_tokens - 1)
```

TTFT absorbs queue wait and prefill; the remainder is near-pure decode, so TPOT degrades if and only if decode is genuinely contended. Every column already exists. Measured across 10,686 completed traces, bucketed by node-wide overlapping requests:

| node-wide concurrency | n | median TPOT | implied tok/s |
|---|---|---|---|
| 1–2 | 4,330 | 19.6 ms | 51.1 |
| 3–5 | 4,483 | 31.0 ms | 32.2 |
| 6–10 | 1,625 | 44.8 ms | 22.3 |
| 11–25 | 234 | 36.3 ms | 27.6 |
| 26+ | 14 | 87.3 ms | 11.5 |

Monotonic apart from the 11–25 bucket (n=234 against 1,625 — likely sampling, not signal). **Control against p99 TPOT, not a memory number.**

### Counter-evidence: capping is probably not the highest-value lever

Three lines of published work address the same latency degradation *without* sacrificing throughput, and they should be evaluated before a cap ships:

- **Prefill stalls, not decode batching** — Sarathi-Serve (OSDI'24) attributes much of the damage to prefill iterations interrupting ongoing decodes, and fixes it with chunked prefill: 2.6× capacity for Mistral-7B on 1×A100 under tail-latency constraints. **Cheap diagnostic for us:** check whether inter-token latency spikes coincide with *new request arrivals* (long prompts landing mid-generation) rather than with steady queue depth. Must be measured per-backend — llama.cpp and `mlx_lm.server` schedule differently.
- **Head-of-line blocking** — FCFS is the default everywhere and causes HOL blocking; SJF-approximating schedulers recover latency at *zero* throughput cost (NeurIPS'24 learning-to-rank shows relative rank is predictable even though exact output length isn't). **Reordering costs nothing; capping costs throughput by construction.**
- **Fit the USL before picking a number** — `X(N) = γN / (1 + α(N−1) + βN(N−1))`, `N_max = √((1−α)/β)`. Fitting α (contention) and β (coherency) to fleet data gives a *derived* cap and, more importantly, distinguishes a plateau (α-dominated: a cap trades throughput for latency) from genuinely retrograde throughput (β>0: a cap recovers both). Our sweep plateaus rather than going retrograde — which weakens the case for capping as a throughput measure.

The strongest pro-capping number in the literature is Clockwork's (OSDI'20): concurrency bought ≤25% throughput while inflating tail latency **100×**, and its whole design is "execute one request at a time." Worth citing for the *mechanism* — but it is 2020-era fixed-shape DNN inference with no KV cache, and transferring the magnitude to autoregressive decode would be folklore.

### Measured on THIS fleet, 2026-07-19 — no published M3 Ultra multi-stream table exists

Unique random prompts per stream (prefix caching defeated — a first attempt with
near-identical prompts showed aggregate rising past the admission limit, which
was cache hits, not scaling). `decode t/s` is from Ollama's own `eval_duration`,
so it excludes prefill and queue wait. `OLLAMA_NUM_PARALLEL=4`.

**MoE — gpt-oss:120b** (128 experts, 4 active), our production model:

| N | decode t/s per stream | aggregate t/s |
|---|---|---|
| 1 | 36.2 | 28.9 |
| 2 | 26.2 | 39.1 |
| **4** | **27.1** | **60.7** |
| 8 | 28.9 | 58.2 ← retrograde |

**Dense — gemma3:27b**, same box, same session:

| N | decode t/s per stream | aggregate t/s |
|---|---|---|
| 1 | 21.3 | 7.1 |
| 2 | 13.6 | 16.1 |
| 4 | 7.4 | 18.1 |
| 8 | 6.6 | 18.3 |

**Four conclusions, and one of them refutes an earlier recommendation in this issue.**

1. **`NUM_PARALLEL=4` is a good setting; my suggestion to try 2 was wrong.** At N=4 both aggregate *and* per-stream beat N=2 (60.7 vs 39.1 aggregate; 27.1 vs 26.2 per stream). There is no latency argument for 2 here.
2. **N=8 is genuinely retrograde** (58.2 < 60.7) — throughput *decreases* with added load, which is `β > 0` in USL terms and the strongest possible justification for a cap. The cap shipped in `075348e` lands exactly on the peak.
3. **Per-stream decode is flat from N=2 onward** (26.2 / 27.1 / 28.9). The cost of going concurrent is a one-time ~25% hit at N=1→2, not a progressive collapse. The 7× degradation seen in production traces is therefore *not* decode contention within one model — it is cross-model contention plus queue wait, which is why per-request `tok/s` misleads and TPOT does not.
4. **Dense scales worse than MoE here, which is the opposite of the theory.** gemma3:27b flattens at ~18 t/s aggregate while per-stream collapses 21.3 → 6.6 (3.2×); gpt-oss holds per-stream flat and doubles aggregate. The expert-fan-out model predicted MoE should batch *worse*. It doesn't — at least not for these two models. Confounded by different model sizes and quantisations, so treat as a strong hint rather than a result, but it is direct evidence against the mechanism this issue previously leaned on.

### Three levers evaluated and closed (2026-07-19)

**Chunked prefill — already optimal, nothing to build.** The premise that llama.cpp naively stalls decode for prefill is **false**. `update_slots()` assembles decode tokens first, unconditionally, then fills remaining budget with prompt chunks capped at `n_batch` — structurally Sarathi-Serve's hybrid batch. A generating slot is never denied admission by a prefill. And Ollama sets `NumBatch=512`, landing exactly on Sarathi's recommended 256–512 chunk size. **Confirmed live on this fleet: `-b 512 -ub 512` in the running llama-server args.** We already have near-optimal chunking and did not choose it.

What chunking *does not* do is make prefill free — it spreads the cost across `ceil(P/n_batch)` inflated inter-token gaps rather than one long stall. Total interference is unchanged.

**Queue reordering (SJF) — measured no-op, do not build.** Reordering pending work can only help when pending depth exceeds what the backend admits concurrently. Measured on this fleet:

| enqueue depth | median | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| observed | 1 | 3 | 4 | 4 | 4 |

**Zero of 91 enqueues ever exceeded depth 4.** With `OLLAMA_NUM_PARALLEL=4`, reordering is the identity function on every request this fleet has ever seen. It is also structurally wrong for the workload: an agentic session issues a *serial chain* of dependent requests, so there is rarely more than one in the queue, and request-level SJF can actively harm session completion time by letting short requests from one session repeatedly jump a long generation from another.

**USL — cannot be fitted here, and the alternative is better anyway.** A 3-parameter model needs points either side of the throughput peak; `OLLAMA_NUM_PARALLEL=4` gives exactly four achievable concurrency values. Raising it to generate data would change `-c = num_ctx × num_parallel` and therefore the thing being measured. Also flagged as folklore: no peer-reviewed application of USL to LLM decode exists, and β (coherency) has no physical referent on a single Metal device — a positive β here would most plausibly be **thermal drift aliased in by a monotonic N ramp**.

Fit **step time linear in N** instead: `step_time(N) = a + b·N`, two parameters, ordinary least squares, both interpretable. `a ≈ W / BW_effective` — so with the model's quantised size known, **`a` is a live effective-memory-bandwidth measurement derived from traces we already collect**, which is precisely the signal no serving system measures.

### The one lever worth building: session affinity

Not concurrency at all. llama.cpp skips already-cached prompt tokens via `get_common_prefix` — so turn N+1 of a coding session, which shares nearly all of turn N's prompt, can cost ~200 tokens of prefill instead of 30,000. Since prefill is the *sole* source of decode interference (above), pinning a session to the node holding its warm prefix **eliminates** the interference term rather than redistributing it.

**Status on this fleet: a real gap, currently latent.** The scorer's `affinity` signal is `_score_role_affinity` — model size ↔ node capability. There is no session or conversation stickiness anywhere in the routing path. It costs nothing today because only one node is online (70,748 of 70,775 recent requests went to `bb`), but a returning second node makes multi-turn sessions bounce and re-prefill from cold.

Prior art: Continuum/CacheTTL attaches a `program_id` and orders by program arrival; SMetric routes a session's *first* request for load balance and all follow-ups cache-aware. HexAGenT reports workflow-level FCFS beating per-call FCFS by 23–31% on tail SLO — i.e. **preserving session order beats reordering it**.

### Do not start by writing code

Start by reproducing the curve deliberately: drive N concurrent streams at fixed N against one model on an idle fleet, record per-request and aggregate tok/s, and find the knee. The numbers above are observational — gathered from production traffic that happened to vary — not a controlled sweep. A controlled sweep is what tells you whether the knee is at 2, 3, or 4, and whether it moves with model size.

---

## RESOLVED (keep the draft model) — the MLX compactor's `--draft-model` disables batching, and that is the right trade here

**Severity:** Medium — every compaction request serialises; invisible from config
**Found:** 2026-07-19, verified locally against the installed mlx-lm
**Files:** `~/.fleet-manager/env` (`FLEET_NODE_MLX_SERVERS`), `CLAUDE.md` (MLX gotchas)

`mlx_lm/server.py:371` (v0.31.3, installed):

```python
is_batchable = draft_model is None
```

Unconditional. Our port-11441 compactor is configured with
`"draft_model":"mlx-community/Qwen3-1.7B-4bit"`, so **`is_batchable` is `False` for
every request** — continuous batching (default `--decode-concurrency 32`, added in
mlx-lm 0.28.4) never engages, and concurrent compaction requests serialise.

This is a genuine either/or that our docs present as a pure win. CLAUDE.md
describes the draft model as giving "~94 tok/s on M3 Ultra" for the compactor; it
does, **for one request at a time**. Maintainer-measured batching on comparable
hardware (M2 Ultra, Qwen 30B/3B 4-bit, mlx-lm PR #626) gives batch 1 → 89 t/s,
batch 2 → 141, batch 4 → 204. So the trade is roughly *94 t/s serialised* versus
*~204 t/s aggregate across 4 concurrent requests, at ~51 t/s each*.

Which is correct depends on whether compaction requests arrive concurrently. With
a single Claude Code session they do not, and speculative decoding wins. With
several sessions compacting at once, the draft model is actively harmful.

### Decision (2026-07-19): keep it — measured, not assumed

The trade only matters if compactions overlap. **They essentially never do:
84 of 2,813 mlx-routed requests on record ever overlapped another — 3%.**
Compaction is a serial workload by nature; one coding session compacts at a
time, spaced by whole conversations. The 97% case is precisely where
speculative decoding wins, so the current config is correct.

A live A/B across the two MLX servers is directionally consistent — the
batching-enabled server's aggregate keeps climbing with concurrency (33 → 93 →
101 t/s at N=1/2/4) while the draft-model server peaks at N=2 and then declines
(33 → 74 → 63.5) — but it is **confounded**: different models on each port
(Qwen3-Coder-30B vs GLM-4.7-Flash). It is corroboration, not proof. A decisive
test needs the same model with and without the flag, which costs a duplicate
~19GB resident and is not worth it given the overlap rate.

**Revisit if** the fleet ever serves several concurrent coding sessions — the
overlap rate is the trigger, and it is one query:
`SELECT COUNT(*) FROM request_traces WHERE model LIKE 'mlx:%'` with an overlap
join. At meaningful overlap the trade inverts and the draft model becomes a
liability.

Two related facts worth recording while here:
- Requests that set a `seed` are also non-batchable and force a batch drain.
- mlx-lm #965 (KV-cache cross-contamination between concurrent requests on M3
  Ultra at 16+ concurrency) was fixed in v0.31.2 — we run 0.31.3, so we have the
  fix. Worth knowing before raising concurrency anywhere near that range.

---

## OPEN — herd's HTTP accept path stalled for 206s under heavy load (root cause unproven)

**Severity:** High when it happens — the router serves nothing — but observed once, cause not conclusively identified
**Found:** 2026-07-25
**Files:** `server/routes/dashboard.py` (SSE — a *contributing* defect fixed; not proven to be the trigger), `server/app.py` (lifespan / uvicorn)

**Symptom:** `curl localhost:11435/*` returned connection-refused (`HTTP 000`, instant) while the herd process was alive at ~1% CPU. `lsof -iTCP:11435 -sTCP:LISTEN` showed **no listener**, but one **ESTABLISHED connection to Chrome** on the dashboard port lingered.

**Evidence it was a real outage, not a misread:** heartbeats are HTTP POSTs *into* herd, so a gap in them means the accept path stopped. Normal cadence is 5s; there was a **206-second gap starting 02:59:19**, ending when the process was restarted ~03:02:45. The observed `HTTP 000` at 03:00:59 falls squarely inside it.

**Why the general log looked healthy** (and briefly misled the diagnosis): the WAL-checkpoint loop and other `asyncio.create_task` background tasks kept running and logging every ~10s throughout — they don't depend on the listener. Total log activity had no gap; only the *inbound HTTP* did. **Lesson: to prove a listener outage, check a signal that requires the listener (heartbeats), not the process's self-generated chatter.**

**Correlation:** it stalled during punishing load — `qwen3:235b`, `deepseek-r1:70b`, and thinking-model budgets inflated to **65,536 tokens** (`num_predict` ×4), with request latencies up to 286s. The listener stopped ~2.5 min after a 65,536-token generation was admitted.

**The contributing defect that was fixed:** `/dashboard/events` had `while True` with **no `request.is_disconnected()` check** and **no exception guard**. A client that walked away without a clean close (sleeping laptop, backgrounded tab) left the generator looping forever, rebuilding full fleet state every 2s — one stuck stream per stale tab. The lingering Chrome ESTABLISHED connection fits. Fixed: loop now exits on disconnect, and a `guarded_stream` wrapper ends one bad iteration cleanly instead of letting it propagate through the ASGI transport. **This is a genuine bug regardless of whether it caused the incident** — a browser tab should never be able to accumulate work on the router.

**Why root cause is not proven, and the process failure behind it:** the wedged process was **killed before a stack dump was taken**, destroying the one piece of evidence that would have been conclusive — with the loop alive but the listener dead, the blocking coroutine would have been visible in a dump. `uvicorn`'s stderr was also going to `/dev/null`, so any server-level crash left no trace.

### Runbook — if `/*` returns connection-refused but the herd process is alive

Do this **before** restarting, or the evidence is gone again:

```bash
HERD=$(pgrep -f "bin/herd$" | head -1)
# 1. Is it the listener or the whole loop? Heartbeats prove the accept path:
python3 - <<'PY'
import json,datetime
last=[datetime.datetime.fromisoformat(json.loads(l)["ts"]) for l in open('~/.fleet-manager/logs/herd.jsonl'.replace('~',__import__('os').path.expanduser('~'))) if '"Heartbeat from"' in l][-3:]
print("last heartbeats:", [t.astimezone().strftime('%H:%M:%S') for t in last])
PY
# 2. THE decisive capture — what coroutine is blocking:
uvx py-spy dump --pid $HERD          # native stack of every thread
uvx py-spy dump --pid $HERD --locals # + local vars in each frame
# 3. Only then recover, capturing stderr this time:
pkill -9 -f "bin/herd|mlx_lm.server"
nohup uv run herd >/tmp/herd.out 2>/tmp/herd.err & disown
```

**Still to do:** run the local fleet with `py-spy` available and stderr captured (done for the current process), and reproduce under a synthetic 65,536-token load with a stale SSE tab open, to determine whether the SSE fix alone prevents recurrence or whether there is a second event-loop-blocking path.

---

## RESOLVED — image requests fell back to a blind text model and 400'd (detector gap)

**Severity:** High — vision requests failed; the 0.9.0 loud-fail guard was silently bypassed
**Found + fixed:** 2026-07-27

**Symptom:** 41 requests to `gemma3:27b` failed over ~3 hours with Ollama 400s:
`Multimodal data provided, but model does not support multimodal requests.`

**Chain:** `gemma3:27b` (vision) was evicted by heavy-model benchmark churn → a vision request arrived → herd cross-category fell back to `gpt-oss:120b` (text) → Ollama rejected the multimodal payload with a 400.

**Root cause — a hole in a guard, not a missing guard.** 0.9.0 added a check that refuses to fall an image request back onto a blind model (`routing.py`: `if inference_req.has_images and fallback_cat != "vision"`). It didn't fire because `has_images` was `False`. `_detect_images` recognised Ollama `images[]` and OpenAI `image_url` parts, but **not** `type:"image"` or `type:"input_image"` content parts — the shape these requests used. Ollama's own handler saw the images (hence the 400); herd's detector didn't, so the protection sat inert and the request fell through.

**Fix:** `_detect_images` now matches a superset of image content-part types (`image_url`, `image`, `input_image`) plus the Ollama `images[]` sibling array, and skips non-dict messages defensively. Because this detector gates the guard, a type it misses silently disables the protection — so the set is deliberately broad. Verified end-to-end after the fix: a vision request now serves via `gemma3:27b` ("Red." on a red PNG) instead of falling back to gpt-oss.

**Also, as immediate mitigation:** `gemma3:27b` pinned (`POST /fleet/pin`) so the benchmark churn stops evicting it. The eviction is what exposed the detector gap — with the vision model always warm, the fallback path isn't reached; the fix ensures that when it *is* reached, it fails cleanly rather than as a raw Ollama 400.
