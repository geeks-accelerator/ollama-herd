# MLX Prompt Cache Optimization — Stop Reprocessing Identical Prefixes

**Status**: Phase 1 + 2 LANDED. Phase 3 (find remaining cache busters) is now top priority.
**Created**: 2026-04-22
**Updated**: 2026-04-22 — Phase 2 verified end-to-end (5× speedup); plan pivoted after Claude Code internals research.
**Related**:
- `src/fleet_manager/server/anthropic_translator.py` — `_normalize_cache_busting_tokens` (Phase 1)
- `src/fleet_manager/server/mlx_proxy.py` — cache hit rate tracking (Phase 2)
- `docs/observations.md` — discovery write-up (cch= fingerprint)
- `docs/plans/mlx-backend-for-large-models.md` — overall MLX integration
- `docs/plans/mlx-prompt-pruning.md` — companion plan, **deprioritized** after this insight (see "How Anthropic does this" below)

---

## Motivation

Today's measurement of 30 sequential Claude Code requests against `mlx_lm.server` showed prompt processing time scaling **linearly with prompt size at a flat ~2 ms/token across the entire range:**

```
21K tokens → 2.0 ms/token
25K tokens → 2.0 ms/token
30K tokens → 2.4 ms/token
44K tokens → 4.8 ms/token  (KV pressure at this size)
```

That flatness is conclusive evidence the prompt cache is missing on every turn. If it were hitting, the cached portion would be free and ms/token would drop sharply on subsequent turns of a growing conversation.

Root cause: Claude Code injects a **per-request fingerprint** into the system prompt:

```
x-anthropic-billing-header: cc_version=2.1.117.bc2; cc_entrypoint=cli; cch=3247f;
                                                                       ^^^^^
```

That `cch=<hex>;` 5-char hex token CHANGES on every request. mlx_lm.server's prompt cache requires byte-exact prefix match — a single byte difference at offset 86 invalidates the entire 25K-token shared prefix. Every Claude Code turn reprocesses the full prompt.

Probable purpose of `cch=` from Anthropic's perspective: anti-replay token, request fingerprint, or telemetry hash. Useful for Anthropic's billing infrastructure; useless for inference.

### Expected impact when fixed

For a typical Claude Code session (system + 27 tools + skills ≈ 25K tokens, growing by ~500 tokens per turn for new user message):

- **Without cache:** every turn reprocesses 25-50K tokens at 2-5 ms/token = **50-150s per turn**
- **With cache:** turn 1 pays the full cost, turns 2+ process only the new message (~500 tokens) = **~1-3s per turn** (cached portion free, generation cost only)

**~10-50× speedup on turns 2+.** This is the largest single available latency win for Claude Code through the herd.

---

## How Anthropic's API does this (and why our path differs)

Research into Claude Code's request-building code (insight contributed 2026-04-22) reveals that **Anthropic's API and `mlx_lm.server` use fundamentally different cache mechanisms** — and understanding the difference reshapes our priorities.

### Anthropic API: explicit `cache_control` markers

Claude Code attaches structured cache directives to specific blocks of the system prompt:

```python
cache_control: { type: 'ephemeral', ttl: '5m' }    # default
cache_control: { type: 'ephemeral', ttl: '1h' }    # extended eligibility
```

Anthropic's server reads these markers and explicitly caches the marked blocks server-side. Subsequent requests within the TTL window get **cache-read billing (~10% of nominal input rate)** for the cached portion. The system prompt isn't re-processed; the server replays it from cache.

Critically, the **tools array isn't separately cache-marked** — but it doesn't need to be. The cache key covers the entire prompt prefix (system + tools + early messages), so as long as tools don't change between turns, the cached portion includes them automatically.

Other Anthropic-side optimizations:

- **`defer_loading: true` for MCP tools** — when MCP tools would consume >10% of context window, Claude Code sends only their NAMES, not full schemas. The model uses a built-in `ToolSearch` tool to fetch a full schema on demand when it actually wants to call something. This caps tool-schema overhead even with 50+ MCP servers installed.
- **Beta header latching** — Claude Code's bootstrap explicitly latches eligibility flags at session start to prevent mid-session changes that would bust ~50-70K cached tokens.
- **TTL flip avoidance** — Claude Code never flips a session between 5m and 1h TTL mid-stream because that would invalidate the prefix cache.

The engineering effort visible in Claude Code's source is **disproportionately around protecting the cache**, not minimizing what's sent. This tells us they ran the numbers and the cache is where the real money lives.

### `mlx_lm.server`: pure prefix-byte-match cache

mlx_lm.server has no concept of `cache_control` markers. Its prompt cache is much simpler: a small ring (default 4 slots) of recently-seen prompt prefixes, keyed on **exact byte equality**. On a new request, it walks the cache slots looking for the longest matching prefix; whatever matches is reused, the rest is reprocessed from the first non-matching token.

Implications:

- **Marker-based caching from the client is invisible to mlx.** We can't translate Anthropic's `cache_control` to anything mlx understands.
- **Byte-stable prefixes are the only handle we have.** Any per-request token (timestamp, fingerprint, request id) embedded in the system prompt at byte offset N invalidates the entire cache for the rest of the prompt.
- **Tool order matters.** If our translator emits tools in different orders between turns, the tool-array bytes differ, cache misses on the entire prefix from that point.
- **No `defer_loading` equivalent.** mlx will tokenize and process every tool definition we send. If Claude Code sends 41 tools with `defer_loading: true` markers, mlx ignores the markers and processes all 41.

**This is why our Phase 1 fix (`cch=` normalization) was necessary and Anthropic's API users never had to think about it:** Anthropic's marker-based cache survived `cch=` flipping because it caches by content not bytes; mlx's byte-match cache didn't.

### What this reframes about pruning

The companion plan (`docs/plans/mlx-prompt-pruning.md`) was written assuming tool schemas were a real per-turn cost worth fighting. That's only true when the cache is broken. With Phase 1 + 2 working:

- **Hosted Claude users:** tools ride in the server-side cache after turn 1 → ~10% of nominal cost → essentially free.
- **Our MLX setup post-cch-fix:** tools ARE in the cached prefix → 0% additional cost on cached turns.

So pruning's win shrinks dramatically:

- **Turn 1 (cold cache):** save 5-7K tokens of dead-weight tools = ~15s faster cold start. **Real, but bounded.**
- **Turn 2+ (warm cache):** save ~0 tokens — those tokens were already free via cache. **Marginal.**

**Pruning is a cold-start optimization, not a per-turn one.** Better answer for cold starts: **prompt cache warming** (send a synthetic warm-up request when a session is first established so turn 1 is also cached). Cheaper to build, no side effects on cache stability.

### Concrete priority changes

What this insight de-prioritizes:
- Tool pruning (companion plan) → defer indefinitely; only reconsider if cache hit rate stays low even after Phase 3
- Tool schema description compression → defer same reason

What this insight ELEVATES (now Phase 3+):
- **Verify cache hits on REAL Claude Code traffic, not just our synthetic probes** — the dashboard cache_hit_rate metric is exactly this
- **Hunt remaining per-request fingerprints** beyond `cch=` — beta header changes, conversation IDs, message timestamps
- **Tool-array order stability** in our translator — defensive fix, cheap, prevents future regression
- **Detect & strip `defer_loading: true` tools** — Anthropic's signal that "the model can't use this without ToolSearch anyway"; we can drop them losslessly

---

## Design: stable-prefix normalization in the translator

### Architecture

The Anthropic→Ollama translator is the right place to normalize. It's the choke point through which every Claude Code request passes; downstream code (router, mlx_proxy) can stay backend-agnostic.

```
client_body (with cch=3247f)
    │
    ▼
anthropic_to_ollama_messages
  └─ anthropic_system_to_text  ← normalize cch= here
    │
    ▼
inference_req.raw_body (with cch=NORMALIZED)
    │
    ▼
mlx_proxy._to_openai_body
    │
    ▼
mlx_lm.server (sees stable prefix → cache hit)
```

### Why normalize, not strip

Stripping the `x-anthropic-billing-header` line entirely would also work — but normalization is **safer for compatibility**. Some downstream tooling (Claude Code's debugging, our trace store, future multi-tenant routing) might want to inspect the header. Replacing the volatile token with a stable placeholder preserves the schema.

### Token chosen

`cch=NORMALIZED;` — fixed-width-ish, obviously synthetic, easy to grep for in logs to spot if real cch values leak through.

---

## Implementation phases

### Phase 1 — Strip the cch= token (LANDED 2026-04-22)

- Added `_normalize_cache_busting_tokens()` regex helper to `anthropic_translator.py`
- Wired into `anthropic_system_to_text()` for both string and text-block-array forms
- 5 unit tests covering the regex + the system-text path
- Single 6-line code change behind 60 lines of tests + docs

**Status**: ✅ shipped, validated end-to-end (see Phase 2).

### Phase 2 — Validate cache hit rate (LANDED 2026-04-22)

Built full observability stack so the cache fix is no longer a hypothesis.

- **MlxProxy `_mlx_request_tokens`** grew from `(prompt, completion)` to `(prompt, completion, cached)` — captured from `usage.prompt_tokens_details.cached_tokens` on both streaming (final usage chunk) and non-streaming (response body) paths.
- **`pop_token_counts()` folds observations** into a per-proxy rolling window (last 50 requests, weighted by request size).
- **`get_cache_hit_rate()`** returns the windowed rate as a fraction in `[0, 1]` or `None` (don't fake 0% before any data).
- **`/fleet/queue`** exposes `cache_hit_rate` per MLX entry.
- **Router log** shows `MLX stream done: ... prompt_tok=20512 cached_tok=18000 cache=88% elapsed_ms=4500` per request.
- **Debug log** captures `prompt_tokens` + `cached_tokens` for replay analysis.
- **Dashboard queue cards** show `CACHE NN%` chip color-coded green ≥80 / yellow ≥40 / red <40.

**Verification (back-to-back identical-prefix probes):**

| Request | Latency | prompt_tok | cached_tok | Hit |
|---|---|---|---|---|
| 1 (cold) | 1.08 s | 1019 | 6 | 1% |
| 2 (warm) | **0.22 s** | 1019 | 1018 | **99.9%** |

5× speedup on identical prefix — exactly the pattern Phase 1 was designed to enable. 8 new tests, 769 total passing.

**Status**: ✅ shipped, awaiting validation across REAL multi-turn Claude Code sessions (vs synthetic probes — see Phase 3).

### Phase 3 — Verify in real traffic + hunt remaining fingerprints (CURRENT TOP PRIORITY)

Phase 2 proved cache works on synthetic probes. Real Claude Code traffic might have OTHER per-request fingerprints that still bust the cache. The new dashboard `cache_hit_rate` metric is the ground-truth measurement.

**Pass criterion:** rolling cache hit rate ≥ 80% during a sustained Claude Code session (5+ turns of the same conversation).

**If pass:** done with cache work. Move on to other reliability/UX work.

**If fail (rate < 50% on sustained session):** find what else is changing. Specific suspects, in order of likelihood (informed by Claude Code internals research):

1. **Beta header changes mid-session.** Claude Code's source explicitly latches eligibility flags to prevent ~50-70K cached tokens being busted per flip. If our router somehow normalizes/strips/reorders beta headers between turns, we cascade the same problem.
2. **Tool array order instability.** mlx's byte-match cache requires identical tool-array bytes. If our translator emits `tools=[...]` in a different order between turns (e.g., dict iteration order shift, sort-by-name lost), every turn is a cache miss even though Anthropic's content-aware cache would survive it. **Defensive fix: deterministic sort in `_to_openai_body`.**
3. **`defer_loading: true` markers.** Anthropic's `defer_loading: true` flag means "don't process this tool's full schema; the model will fetch via ToolSearch when needed." If Claude Code sends these and we forward them to mlx (which ignores the flag), we're shipping schemas the model literally cannot invoke. Worse, if the marker itself appears in the prompt string and changes per request, it busts cache. **Investigation: dump captured request bodies, look for `defer_loading` keys.**
4. **Conversation IDs / message IDs.** Anthropic message IDs and conversation IDs are passed in metadata fields. If they're embedded in any prompt text, they change every turn.
5. **Message timestamps.** Less common, but some clients add `<timestamp>` tags to messages.
6. **System reminder rotation.** The `<system-reminder>` block in user messages often includes per-turn state (todo list status, recent tool counts). If the format includes a turn counter, that's a busting token.

For each candidate found, extend `_normalize_cache_busting_tokens()` with another regex. Defensive: regex pipeline grows without affecting existing normalizations.

**Diagnostic tooling needed:**
- A "diff sequential turns" script that pulls 2 captured requests from the same session and shows byte-level differences in their prefixes. Already most of the way there — see the inline analysis at the bottom of `tests/test_server/test_anthropic_translator.py`.

### Phase 4 — Cold-start optimization via cache warming (NEW, replaces former pruning win)

The Anthropic-internals research showed pruning is mostly a cold-start optimization once cache works. **Cache warming is a better answer to that same problem:** when a Claude Code session is established, proactively send a synthetic warm-up request so the system+tools prefix is cached before the user's first real request arrives.

**Mechanism:**
- Detect new-session signal (request with `messages.length == 1` and a haiku probe shape, OR explicit `metadata.user_id` we haven't seen recently)
- Fire a fast no-op completion against mlx with the same system + tools prefix (`max_tokens=1`)
- The first real request now hits cache instead of paying full cold-start cost

**Win:** turn 1 latency drops from ~50s to ~5s (same as turn 2+). Whole session gets cached behavior from the start.

**Risk:** wastes one inference call per session even when the user never sends a follow-up. Mitigation: only warm if recent traffic shows the user typically does multi-turn (≥3 turns per session). Default off; opt-in via `FLEET_MLX_WARM_CACHE_ON_SESSION_START=true`.

**Effort:** 2-3 days. Lower priority than Phase 3 — only worth building if Phase 3 confirms the hot-path is fully fast.

### Phase 5 — Validate cache hit on multi-conversation fleets (1 day)

mlx_lm.server has `--prompt-cache-size 4` — only 4 cache slots. With multiple concurrent Claude Code sessions on the same node, cache thrashing could happen. Verify by:

1. Open 2-3 different Claude Code sessions in parallel
2. Measure each session's cache hit rate independently
3. If thrashing: bump `--prompt-cache-size` to 8 or 16 (memory budget allows)

### Phase 6 — Auto-tune cache size based on heartbeat memory (2 days, optional)

Adapt `--prompt-cache-size` per-node based on available RAM. Mac Studio (512GB) can hold 16-32 cached prefixes; M2 Air (16GB) might only hold 1-2. Wire into the supervisor's mlx_lm.server launch args.

---

## Testing strategy

### Unit tests (in place — Phase 1 + 2)

Phase 1:
- `_normalize_cache_busting_tokens()` regex: matches expected, leaves other text alone
- Stable across invocations (different cch values → same output)
- `anthropic_system_to_text()` normalizes both string and block-array forms

Phase 2:
- `pop_token_counts()` returns three-tuple `(prompt, completion, cached)`
- Missing-data semantics: `None` ≠ 0% (don't fake hit rate when mlx didn't report)
- Rolling window caps at 50 observations (recent enough to reflect current state)
- `get_cache_hit_rate()` weighted by request size (big requests dominate)
- `get_queue_info()` exposes `cache_hit_rate` field per MLX entry

### End-to-end validation (in place — Phase 2)

- Back-to-back identical-prefix probes against router show 5× latency drop on turn 2 (1.08s → 0.22s)
- Real request capture confirms `cached_tokens` populated in debug log records
- Dashboard renders `CACHE NN%` chip from live SSE stream

### Phase 3 validation (TODO)

- Replay 10+ sequential turns of the same Claude Code session through the router
- Expected: turns 2+ show `cached_tokens > 80% of prompt_tokens`
- If <50%, dump system prompts pairwise and find the remaining cache-busting token (see Phase 3 suspect list)

---

## Operational impact

### Positive

- **Drop p50 latency for Claude Code from ~50s to ~5s** on turns 2+ of a session. Single biggest UX win available.
- **Reduce GPU work by ~80%** on cached turns. More headroom for other work, less heat, less power.
- **Indirectly mitigate the queue-overflow risk** addressed by admission control — if requests finish in 5s instead of 50s, the queue drains 10× faster, fewer 503s.

### Negative

- **Memory cost:** mlx prompt cache grows. With 16GB cache budget and ~20K-token prefixes at ~50KB each (Q8 KV), capacity is ~300 cached prefixes. Plenty.
- **Wrong-cache-hit risk:** if our normalization is too aggressive and collapses semantically-different prompts to the same key, the model sees stale state. **Mitigation:** only normalize tokens proven to be telemetry/fingerprints, never normalize content the model needs.

### Neutral

- Cold first request still pays the full prompt-processing cost. Not addressed by Phase 1-3; addressed by Phase 4 (cache warming) when prioritized. Note: the original plan listed `mlx-prompt-pruning.md` as the cold-start fix — that plan is now deprecated for the reasons covered in "How Anthropic does this" above.

---

## Open questions

### Does mlx_lm.server's cache work on prefixes or only exact prompts?

**Resolved (Phase 2)**: confirmed prefix-byte-match. The 5× speedup on the 99.9% cache-hit second request proves it walks cached slots looking for the longest matching prefix, not exact-only. Growing conversations work as long as the prefix bytes stay stable.

### Is `cch=` Anthropic-server-side cache busting?

**Resolved (per Claude Code internals research)**: `cch=` appears to be a per-request fingerprint that Claude Code includes for Anthropic's own server-side billing/audit machinery. It changes every request by design. Anthropic's content-aware cache survives it (they don't key on bytes); mlx's byte-match cache doesn't. Normalization is the right call for our scenario. If Claude Code adds stricter fingerprints (signed tokens, request HMACs), the regex will need extension.

### What other Anthropic clients inject similar tokens?

Aider, Cline, Continue.dev, Anthropic SDK direct usage — all may have their own per-request fingerprints. As we add more client compatibility, we'll need to discover and normalize each. Phase 3's "comb the captured requests" approach extends to each new client.

### Should this be opt-in or default?

**Default on**, behind a `FLEET_NORMALIZE_FINGERPRINTS=true` env var (default true). If a user has a strange use case where the original token must reach the model, they can disable. Risk of disabling without realizing it: ~50× latency penalty. Worth the env var existing as an escape hatch.

---

## Success metrics

| Metric | Target | Phase 2 result | Phase 3 status |
|---|---|---|---|
| Synthetic-probe cache hit rate (back-to-back identical prefix) | ≥ 95% | ✅ 99.9% measured | n/a |
| Synthetic-probe latency drop on second request | ≥ 5× | ✅ 5× (1.08s → 0.22s) | n/a |
| Real Claude Code session cache hit rate (turn 2+) | ≥ 80% | TBD — needs sustained session | open |
| Real Claude Code p50 latency on cached turns | < 10s (vs ~50s baseline) | TBD | open |
| Functional regressions (model output quality unchanged) | 0 | ✅ none observed in Phase 2 | n/a |

The synthetic-probe results prove the mechanism works. The remaining open metrics (real-traffic) are exactly what Phase 3 hunts down — if they don't pass, the pass criterion drives investigation of remaining cache-busting tokens beyond `cch=`.
