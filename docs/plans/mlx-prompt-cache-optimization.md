# MLX Prompt Cache Optimization — Stop Reprocessing Identical Prefixes

**Status**: In Progress (Phase 1 landed, validation pending)
**Created**: 2026-04-22
**Related**:
- `src/fleet_manager/server/anthropic_translator.py` — `_normalize_cache_busting_tokens` (Phase 1)
- `docs/observations.md` — discovery write-up (cch= fingerprint)
- `docs/plans/mlx-backend-for-large-models.md` — overall MLX integration
- `docs/plans/mlx-prompt-pruning.md` — companion plan, complementary not competing

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

**Status**: code shipped, awaiting validation under live Claude Code traffic.

### Phase 2 — Validate cache hit rate (1 day)

The tricky part: prove the fix actually works.

**Approach A — direct probe via mlx_lm.server response:**
The `usage.prompt_tokens_details.cached_tokens` field in OpenAI responses tells us exactly how many of the prompt's tokens hit the cache. Capture this from MLX responses (currently we only capture the Anthropic-translated response; need to also surface mlx's raw counts).

**Approach B — indirect via latency-vs-prompt-size:**
Re-run the diagnostic from today's session. If cache is hitting, sequential turns should drop from ~2 ms/token to <0.5 ms/token on the cached portion.

**Concrete steps:**
1. Add `cache_hit_tokens` field to MLX trace records (mlx_proxy already has access to the response, just plumb it through)
2. Add `cache_hit_rate` to `/fleet/queue` MLX entries (rolling p75 over last N requests)
3. Surface on dashboard: "Cache hit rate: 87%" alongside the queue depth
4. Capture before/after comparison: 10 sequential turns of a real Claude Code session, expect p50 latency to drop ~10×

**Risk:** the fix doesn't help and we discover a deeper issue (e.g., mlx_lm.server's cache requires explicit `extra_body={"cache_prompt": True}` flag, or the cache is per-connection and Claude Code uses fresh connections). In that case Phase 3 expands.

### Phase 3 — Investigate other cache-busting tokens (1-2 days)

If Phase 2 shows cache-hit rate is still low after the cch fix, comb through captured Claude Code requests for other per-request volatility:

- Anthropic message IDs / request IDs embedded in messages
- Timestamps in any system field
- Per-turn tool descriptions that change (unlikely, but possible)
- Conversation IDs that flip between requests
- Message ordering instability (rare but happens with retry logic)

Add normalization for each as discovered. Defensive: the regex pipeline can grow without breaking existing tests.

### Phase 4 — Validate cache hit on multi-conversation fleets (1 day)

mlx_lm.server has `--prompt-cache-size 4` — only 4 cache slots. With multiple concurrent Claude Code sessions on the same node, cache thrashing could happen. Verify by:

1. Open 2-3 different Claude Code sessions in parallel
2. Measure each session's cache hit rate independently
3. If thrashing: bump `--prompt-cache-size` to 8 or 16 (memory budget allows)

### Phase 5 — Auto-tune cache size based on heartbeat memory (2 days, optional)

Adapt `--prompt-cache-size` per-node based on available RAM. Mac Studio (512GB) can hold 16-32 cached prefixes; M2 Air (16GB) might only hold 1-2. Wire into the supervisor's mlx_lm.server launch args.

---

## Testing strategy

### Unit tests (in place)

- `_normalize_cache_busting_tokens()` regex: matches expected, leaves other text alone
- Stable across invocations (different cch values → same output)
- `anthropic_system_to_text()` normalizes both string and block-array forms

### Integration tests (TODO Phase 2)

- Mock mlx_lm.server response with `usage.prompt_tokens_details.cached_tokens`
- Assert proxy parses + traces it correctly
- Assert dashboard surfaces it

### End-to-end validation (TODO Phase 2)

- Replay 10 captured back-to-back Claude Code requests
- Pre-fix expected: every request shows `cached_tokens=0` or trivial
- Post-fix expected: turns 2-10 show `cached_tokens > 90% of prompt_tokens`
- Latency p50 drop from ~50s to ~5s

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

- Cold first request still pays the full prompt-processing cost. Not addressed by this plan; covered by Phase 1 of `mlx-prompt-pruning.md` which reduces the cold cost.

---

## Open questions

### Does mlx_lm.server's cache work on prefixes or only exact prompts?

If exact-only, the cache is essentially useless for growing conversations and we need a different strategy (e.g., maintain our own KV cache server-side). Phase 2 validation will tell us. Source dive into `mlx_lm/server.py` is a 30-min task to clarify before too much investment.

### Is `cch=` Anthropic-server-side cache busting?

Possibly intentional from Anthropic's side — they may explicitly want every request to be a cache miss for billing/audit reasons. We don't care server-side at the inference layer; normalizing it is the right call for our scenario. If Claude Code adds a more strict fingerprint (signed token, request HMAC), the regex will need extension.

### What other Anthropic clients inject similar tokens?

Aider, Cline, Continue.dev, Anthropic SDK direct usage — all may have their own per-request fingerprints. As we add more client compatibility, we'll need to discover and normalize each. Phase 3's "comb the captured requests" approach extends to each new client.

### Should this be opt-in or default?

**Default on**, behind a `FLEET_NORMALIZE_FINGERPRINTS=true` env var (default true). If a user has a strange use case where the original token must reach the model, they can disable. Risk of disabling without realizing it: ~50× latency penalty. Worth the env var existing as an escape hatch.

---

## Success metrics

- **Cache hit rate ≥ 80%** on turn 2+ of a Claude Code session (measured via `usage.prompt_tokens_details.cached_tokens`)
- **p50 latency for cached turns < 10s** (vs current ~50s)
- **Zero functional regressions** — model output quality unchanged for the same prompt
