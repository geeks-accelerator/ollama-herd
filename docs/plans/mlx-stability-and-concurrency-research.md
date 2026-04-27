# MLX stability + concurrency research

**Status:** Proposed — not yet started
**Filed:** 2026-04-27
**Owner:** unassigned (initial research executed by AI agent)
**Risk:** Low code risk; the work is primarily reading, testing, and documenting. Any code changes the research recommends ship in a separate follow-up commit so the research findings don't entangle with implementation choices.

## Motivation

Two assumptions in our MLX backend code haven't been independently verified:

1. **`mlx-lm v0.31.3` is the best version we can pin to.** Our `setup-mlx.sh` declares `0.31.3` as known-good, but that's the version that produced the [`load_default → snapshot_download → thread_map` crash](https://github.com/ml-explore/mlx-lm/issues/1208) we filed two days ago. Maybe an earlier version of mlx-lm doesn't have this code path. Maybe the fix is already in a candidate PR. Maybe we need to carry a local patch like we already do for `--kv-bits`.

2. **`mlx_lm.server` is single-threaded per process.** Our `MlxProxy._acquire_slot(model_key)` enforces 1 in-flight request per model based on this assumption — every other concurrent request blocks on a per-model `asyncio.Semaphore(1)` until the current one finishes. If `mlx_lm.server` actually handles N concurrent requests internally, we're under-utilizing the model. If it can't handle even 2 (e.g., shared model state corrupts), our admission control is correct but might need to be even stricter (e.g., serialize across the whole process, not per-model).

Both assumptions affect reliability + throughput in ways we haven't measured. This plan answers them with focused, time-boxed research and a concrete artifact at the end.

## Scope

**In scope:**
- Audit of mlx-lm GitHub issues + closed PRs for the relevant failure modes
- Diff of `mlx_lm/server.py` across the v0.30.x and v0.31.x release lines
- Read mlx_lm.server's request-handler source to confirm/refute the threading model
- Live concurrent-request test against the running local MLX server
- Written findings + recommendations
- Implement the recommendations (downgrade, patch, or "no change" with rationale)

**Out of scope:**
- Replacing `mlx_lm.server` with direct mlx-lm Python API (big refactor; defer until forced)
- Evaluating `mlx-engine` (LM Studio's MLX backend) as an alternative — separate plan if anything from this research suggests we should
- Performance tuning (quantization choices, prompt cache sizing, etc.) — reliability first
- Multi-machine MLX coordination concerns — already shipped, no reported issues

## Phase 1 — Stable version analysis (~1 hour)

**Goal:** Determine whether to downgrade mlx-lm, carry a local patch, or accept v0.31.3 as best-of-bad-options with mitigations.

**Inputs:**
- `gh` CLI access to `ml-explore/mlx-lm`
- PyPI release history for `mlx-lm`
- Locally installed `mlx_lm/server.py` at `~/.local/share/uv/tools/mlx-lm/lib/python3.14/site-packages/`

**Method:**

1. **Issue audit.** Search closed + open mlx-lm issues with these terms:
   - `"load_default"`
   - `"snapshot_download"`
   - `"thread_map"`
   - `"cannot schedule new futures"`
   - `"interpreter shutdown"`
   - `"server" + "crash"` (filter by recent)

   For each match, capture: which version it affects, what fixed it (if anything), whether it's the same root cause as #1208.

2. **Version-diff analysis.** Pull source for the last 4 minor releases (v0.30.4, v0.30.7, v0.31.0, v0.31.3) and diff each `server.py` against the next. Identify when `load_default` was introduced, when `_model_map["default_model"]` was added, when `snapshot_download` started being called on every chat-completion request. Read the commit messages associated with each change.

3. **Patchability assessment.** If we can identify the bad commit, evaluate three options:
   - (a) Pin to the version BEFORE the bad commit — may lack other features we use
   - (b) Pin to v0.31.3 + carry a local patch reverting just the bad change
   - (c) Stay with v0.31.3 unmodified — accept the bug, rely on our quarantine guard

**Decision criteria:**
- If a clean pre-bug version exists and has all features we use → recommend downgrade (Option a)
- If the bug is tangled with features we need → recommend candidate patch (Option b)
- If the bad code path is unavoidable → recommend acceptance (Option c) and document why

**Output:** A "Version stability" section in `docs/research/mlx-lm-stability-and-concurrency.md` with:
- Timeline of when the bug was introduced
- Each candidate version and its trade-offs
- Recommendation with one-line rationale
- If recommending a patch: the patch text, ready to add to `setup-mlx.sh`

## Phase 2 — Concurrency model verification (~1 hour)

**Goal:** Confirm or correct our `MlxProxy._acquire_slot(model_key)` assumption that mlx_lm.server handles requests serially per model.

**Inputs:**
- Locally installed `mlx_lm/server.py`
- Running local MLX servers on ports 11440 + 11441 (Mac Studio fleet)
- A small test script that sends N concurrent requests directly to mlx_lm.server (bypassing our proxy)

**Method:**

1. **Source read.** Trace the request path in `mlx_lm/server.py`:
   - Where does a chat-completion request enter? (HTTPRequestHandler.do_POST)
   - Is it dispatched to a thread pool? Single thread per request? Queue?
   - What's shared state between requests? (model weights, KV cache, batch generator)
   - When can two requests be in flight simultaneously?

2. **Live test.** Write a tiny script that fires 3 chat-completion requests concurrently against `:11440` (bypassing our proxy and queueing). Observe via:
   - HTTP timing (does request 2 wait for request 1 to finish?)
   - mlx-server-11440.log progress messages (do they interleave or serialize?)
   - psutil thread count during the burst

   Run with **small** payloads (under 1K tokens, max_tokens=10) so the test completes in seconds. Single test against the lighter 30B server (port 11441) so we don't disrupt real coding sessions.

3. **Assumption verification.** Compare observed behavior to our admission control:
   - If mlx_lm.server serializes within a model: our `Semaphore(1)` per `model_key` is correct.
   - If mlx_lm.server can interleave: we're under-utilizing — but the safer move would be to confirm correctness before relaxing the admission gate.
   - If mlx_lm.server crashes / corrupts under concurrent load: our admission control is correct AND we should add a hard guard at the proxy that drops concurrent requests with 503 instead of silently queueing forever.

**Decision criteria:**
- Source + live behavior agree → no code change, just document the model
- Source + live behavior disagree → trust live behavior, file an upstream issue, document
- Live behavior reveals new failure mode under concurrency → plan a follow-up code change

**Output:** A "Concurrency model" section in the research doc with:
- The actual threading/dispatch model used by mlx_lm.server
- Whether our admission control matches it
- Any new failure modes the test surfaced (if any)
- Recommendation: keep, tighten, or relax `_acquire_slot`

## Phase 3 — Findings + recommendations doc (~30 min)

**Goal:** Produce a single research artifact that captures both phases and ends in concrete actions.

**Output:** `docs/research/mlx-lm-stability-and-concurrency.md` with:

1. Version stability findings (Phase 1)
2. Concurrency model findings (Phase 2)
3. Recommendations table — column "what to change" / "why" / "estimated risk"
4. References — links to upstream issues, commits, PRs we cited
5. Update or no-update decision for each affected file in our codebase

## Phase 4 — Implementation (~30 min — half day, depending on findings)

**Goal:** Apply the research's recommendations as a follow-up commit.

Likely shapes (depends on Phase 1/2 outcome):

- **If recommending downgrade:** edit `scripts/setup-mlx.sh` to bump `PINNED_VERSION` to the recommended version. Re-run setup-mlx, restart MLX servers, smoke-test. Update CLAUDE.md and `mlx-setup.md` if the version reference appears there.

- **If recommending a local patch:** add a new patch hunk to `setup-mlx.sh`'s patch chain. Verify the patched server runs correctly. Update the upstream-issue doc to note we're carrying the patch.

- **If recommending no change:** add a new section to `docs/observations.md` capturing what we learned and why we chose to live with v0.31.3.

- **If concurrency findings differ from our assumption:** edit `MlxProxy._acquire_slot` accordingly + write tests that verify the new model.

Each implementation gets its own tests + the usual commit-message-with-rationale pattern.

## Success criteria

By the time this plan is complete:

1. We have a written, defensible answer to "is mlx-lm v0.31.3 our best pin?" — yes/no with citations
2. We have a verified, written answer to "does mlx_lm.server really serialize requests per model?" — yes/no with empirical evidence
3. Our `setup-mlx.sh` reflects the version recommendation (whether that's a change or a confirmation)
4. Our `MlxProxy` reflects the concurrency recommendation (whether that's a change or a confirmation)
5. Future agents reading the research doc don't have to redo the work to make a related decision

## Non-goals

- Not solving #1208 ourselves — that's an upstream task
- Not building automated tests that exercise mlx_lm.server's internal threading — too brittle, depends on upstream behavior we can't pin
- Not benchmarking throughput — different problem, different research

## Time budget

| Phase | Estimate |
|---|---|
| 1. Stable version analysis | ~1 hour |
| 2. Concurrency model verification | ~1 hour |
| 3. Write up findings | ~30 min |
| 4. Implementation | ~30 min – half day |
| **Total** | **~3 hours – half day** |

The wide range on phase 4 reflects that we don't know what we'll find. If the answer is "stay on 0.31.3, our concurrency is correct," it's a doc-only commit. If we need to carry a patch and tighten admission control, it's more work — but the risk is bounded because we already have quarantine + orphan-reap safety nets.

## Related

- [Upstream issue ml-explore/mlx-lm#1208](https://github.com/ml-explore/mlx-lm/issues/1208) — the bug that triggered this research
- [`docs/upstream-issues/mlx-lm-load-default-crashloop.md`](../upstream-issues/mlx-lm-load-default-crashloop.md) — local copy of the issue body
- [`docs/observations.md`](../observations.md) — 2026-04-26 (quarantine guard) + 2026-04-27 (orphan reap)
- [`scripts/setup-mlx.sh`](../../scripts/setup-mlx.sh) — current pin + patch infrastructure
- [`src/fleet_manager/server/mlx_proxy.py`](../../src/fleet_manager/server/mlx_proxy.py) — `_acquire_slot` admission control
