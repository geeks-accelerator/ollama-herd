# MLX Prompt Pruning — Strip Unused Tools from Forwarded Prompts

**Status**: ⚠️ DEFERRED INDEFINITELY (2026-04-22) — see "Why this is deprioritized" below.
**Created**: 2026-04-22
**Updated**: 2026-04-22 — research into Claude Code internals showed pruning's win is much smaller than the original analysis assumed.
**Related**:
- `docs/plans/mlx-prompt-cache-optimization.md` — the work that subsumed this plan's purpose; see its "How Anthropic does this" section
- `src/fleet_manager/server/anthropic_translator.py` — where pruning would integrate, if revived
- `docs/observations.md` — discovery write-up (tools=27 but only 3 ever called)

---

## Why this is deprioritized

This plan was written under the assumption that **tool schemas were a per-turn cost worth fighting**. Subsequent research into how Anthropic's API and Claude Code's request-building work showed that's only true when prompt caching is broken:

- **Hosted Claude users** get tools cached server-side via `cache_control` markers — tools cost ~10% of nominal rate after turn 1, essentially free.
- **Our MLX setup** with the cch normalization fix (Phase 1+2 of `mlx-prompt-cache-optimization.md`) puts tools in the byte-stable cached prefix → 0% additional cost on cached turns.

So the pruning win shrinks to:

- **Turn 1 (cold cache):** save 5-7K tokens of dead-weight tools = ~15s faster cold start. **Real, but bounded.**
- **Turn 2+ (warm cache):** save ~0 tokens — those tokens were already free. **Marginal.**

A better answer for the cold-start problem is **prompt cache warming** (Phase 4 of the cache plan): send a synthetic warm-up request when a session is first established so turn 1 is also cached. Cheaper to build, no risk of stripping a tool the model wanted to call, no cache-stability concerns.

**Reconsider this plan only if:** real Claude Code cache hit rate stays low (<50%) even after Phase 3 of the cache plan finds and normalizes all per-request fingerprints. In that scenario, mlx's cache is fundamentally not helping us and we'd need this plan's per-request strip-down. Until then, leave the work undone.

---

> _The original plan content follows for reference — it remains internally consistent under its original assumptions._

---

---

## Motivation

Analysis of 80 real Claude Code requests during a TypeScript build session revealed Claude Code advertises **27 tools** in every request but **only 3 are ever called**:

| Tool | Calls observed | % of total tool use |
|---|---:|---:|
| `Write` | 271 | 54.5% |
| `Bash` | 222 | 44.6% |
| `Read` | 6 | 1.2% |
| (24 other tools) | 0 | 0% |

**Per-request token cost of the dead-weight 24 tools: ~6.5K tokens.** Each tool's JSON schema averages ~300 tokens (name + description + parameters JSON Schema with property types and descriptions).

At ~2 ms/token MLX prompt processing, that's **~13 seconds per request** spent processing tool definitions the model will never invoke. Multiplied across a session: **~5 minutes of wall-clock time per hour of Claude Code use**.

### Why the data is trustworthy

The 80-request sample is from a **real user building a real TypeScript project**, not synthetic load. The tool-use distribution is what Claude Code actually does on a coding workload: heavy Write/Bash/Read, ignore everything else. Different workloads (research, web automation) would surface different tools, but coding is the dominant Claude Code workload through this fleet.

### Interaction with prompt caching

This plan **must be designed alongside `mlx-prompt-cache-optimization.md`** — they pull in opposite directions:

- Pruning saves tokens **per request** (smaller prompt = faster prompt processing)
- Caching saves tokens **per session** (subsequent turns skip the unchanged prefix)

If we prune dynamically per-request, the prefix changes every turn → cache misses → we **lose more from cache busts than we gain from fewer tokens**. The combined design must:

1. Decide tool set ONCE per session (after a few turns of observation)
2. Lock it for the rest of the session so the cache prefix stays stable
3. Re-evaluate only on session boundary (router restart, idle timeout, or explicit reset)

---

## Design: session-stable tool set selection

### Architecture

Add a `ToolSetSelector` class living alongside the Anthropic translator. Stateful per-session (keyed by some session id), tracks which tools have been used and decides what to forward.

```
┌────────────────────────────┐
│ Anthropic request          │
│  - 27 advertised tools     │
│  - history with tool_use   │
└─────────────┬──────────────┘
              │
              ▼
┌────────────────────────────┐
│ ToolSetSelector            │
│  - look up session profile │
│  - pick tools to forward   │
└─────────────┬──────────────┘
              │
              ▼
┌────────────────────────────┐
│ Translated prompt          │
│  - pruned tools subset     │
│  - rest unchanged          │
└────────────────────────────┘
```

### Session identity

Three options, ranked by reliability:

1. **`metadata.user_id` + tool-set hash** (preferred). Claude Code passes a `metadata.user_id` field; combine with hash of the advertised tool set to form a session key. Stable for a given user's repeat sessions, distinguishes test scripts from real usage.
2. **Anthropic request_id prefix** (fragile). Some clients reuse a session prefix in request IDs.
3. **Conversation hash** (fallback). SHA256 of the first user message + tool set. Stable for the same conversation but treats a new conversation as a new session.

Combine: prefer 1, fall back to 3. Never solely rely on transient values.

### Selection algorithm

```python
def select_tools(session_id, advertised_tools, history):
    profile = get_session_profile(session_id)

    # Cold start: keep everything until we have enough signal
    if profile.turn_count < 3:
        return advertised_tools  # full set, build observations

    # Tools the model has actually used in this session
    used = profile.tools_called  # set built from observed assistant tool_use blocks

    # Always-include essentials (covers 95% of coding workloads per data)
    essentials = {"Read", "Write", "Bash", "Edit", "TodoWrite"}

    # Keyword-hint additions from the current user message
    last_user_text = extract_last_user_text(history)
    hinted = match_keyword_hints(last_user_text)
    # e.g., "search for X" → add Grep; "fetch URL" → add WebFetch

    keep = used | essentials | hinted
    return [t for t in advertised_tools if t["name"] in keep]
```

### Session profile structure

```python
@dataclass
class SessionProfile:
    session_id: str
    created_at: float
    turn_count: int = 0
    tools_called: set[str] = field(default_factory=set)
    locked_tool_set: list[str] | None = None  # set after stabilization
    last_seen: float = 0.0

    def observe_request(self, history):
        self.turn_count += 1
        self.last_seen = time.time()
        for msg in history:
            if msg.get("role") != "assistant": continue
            for block in coerce_blocks(msg.get("content")):
                if block.get("type") == "tool_use":
                    self.tools_called.add(block.get("name", ""))
```

Stored in an in-memory dict on the router, evicted after 1 hour of inactivity.

### Lock-in for cache stability

After turn N (default 5), freeze the tool set. Never change it for the rest of the session (until idle timeout). This preserves cache-prefix stability — the tool set is part of the prompt, so it must not flip between turns.

```python
if profile.turn_count >= LOCK_IN_TURN and not profile.locked_tool_set:
    profile.locked_tool_set = sorted(keep)
    logger.info(f"Session {session_id}: locked tool set to {profile.locked_tool_set}")

return profile.locked_tool_set or [t for t in advertised_tools if t["name"] in keep]
```

### Keyword hints (additive)

Even after lock-in, certain keywords in the current user message could promote a tool back into the set IF the lock prevents that — but doing so would invalidate the cache. So keyword hints only matter PRE-LOCK. After lock, the user gets whatever tools the lock-in captured.

```python
KEYWORD_HINTS = {
    "Grep":           ["grep", "search for", "find string", "look for"],
    "Glob":           ["glob", "list files matching", "find files"],
    "WebFetch":       ["fetch", "download", "http://", "https://", "url"],
    "WebSearch":      ["search the web", "google", "look up online"],
    "TodoWrite":      ["todo", "track progress", "checklist"],
    "ScheduleWakeup": ["schedule", "wake up", "remind me later", "in N minutes"],
    "AskUserQuestion": ["ask me", "clarify", "do you want"],
    "Skill":          ["use the skill", "invoke skill"],
    "Task":           ["delegate", "spawn agent", "subagent"],
    # Map every advertised tool name to its likely-trigger phrases
}
```

---

## Implementation phases

### Phase 1 — Pruning module + observation only (1 day)

Build `tool_set_selector.py` with the `SessionProfile` machinery and the selection algorithm. **Don't actually prune yet** — log what WOULD be pruned for analysis.

```
INFO  tool_pruner: session=abc123 turn=4 advertised=27 would_keep=5 would_drop=22
       (drop: WebFetch WebSearch ScheduleWakeup Agent ...)
```

Run for 1-2 days under real Claude Code load. Verify the algorithm picks reasonable sets across diverse sessions.

### Phase 2 — Enable pruning behind env flag (1 day)

`FLEET_MLX_TOOL_PRUNING=true` (default false initially). When on, actually strip tools. Add `--enable-pruning-from-turn=N` to control the lock-in turn.

Add unit tests:
- Cold start returns all tools
- After lock, returns the locked set
- Keyword hints promote tools pre-lock
- Session eviction after timeout

### Phase 3 — Validate with real workloads (2 days)

A/B test:
- 50% of sessions: pruning on
- 50% of sessions: pruning off (control)

Compare:
- Average prompt token count
- p50/p95 latency
- Tool-call success rate (model attempts to call a stripped tool → bad)
- User-reported friction (subjective; Claude Code session abandonment rate as proxy)

If stripped-tool-call rate > 1%, tighten the keyword heuristics or expand essentials.

### Phase 4 — Default on + observability (1 day)

Once Phase 3 validates safety, flip default to `FLEET_MLX_TOOL_PRUNING=true`. Add:
- Dashboard panel: per-session tool-set decisions
- Debug log capture of pruning events (so we can replay analysis later)
- Health check: alert if stripped-tool-call rate spikes (indicates new workload type)

### Phase 5 — Per-tool-category templating (2-3 days, optional)

Beyond binary include/exclude, compress tool DESCRIPTIONS for tools we keep. Some descriptions are 100+ tokens and verbose; could be 30 tokens without losing model utility.

```
Before: "WebFetch — Fetches content from a URL using a markdown converter and
         returns it as text. Use when you need to access information from web pages
         or APIs that return structured documents..."
After:  "WebFetch — Fetch URL content as text/markdown."
```

Risk: too aggressive compression hurts model's ability to call the tool correctly. Validate with model self-correction rate (model tries → fails → retries differently).

---

## Testing strategy

### Unit tests (Phase 1)

- `SessionProfile.observe_request()` correctly extracts tool_use blocks
- `select_tools()` returns full set before lock-in turn
- `select_tools()` returns essentials + used set after lock-in
- Keyword hints add tools pre-lock, ignored post-lock
- Session profile timeout eviction works

### Integration tests (Phase 2)

- Mock 5 sequential requests with different tool usage patterns
- Verify lock-in happens at turn N
- Verify pruned prompt is ≤ baseline minus expected tokens

### A/B regression test (Phase 3)

- Replay captured production traffic with and without pruning
- Compare model output: should be functionally identical for any prompt that doesn't actually need a stripped tool
- Capture: prompts where output diverges, investigate why

---

## Operational impact

### Positive

- **Save ~5K tokens per request** after lock-in = ~10s faster prompt processing on cached turns (but only if cache is also working — see companion plan)
- **Smaller KV cache footprint** = more headroom = less wedge risk = more concurrent sessions per node
- **Cleaner traces** for debugging (fewer dead tools cluttering logs)

### Negative

- **Stripped-tool hallucination risk:** model tries to call a tool we removed. Severity depends on how Claude Code handles "unknown tool" responses. **Mitigation:** essentials list + keyword hints + cold-start full-set.
- **Session profile state on the router:** memory cost is negligible (~1KB per session), but if the router restarts, all sessions are cold again. **Mitigation:** persist profiles to SQLite (low priority, only matters if router restarts often).
- **Privacy:** session profiles record which tools each user calls. Already captured in trace store, but a new lookup surface. Document in privacy notes.

### Combined-with-cache-fix scenarios

| Cache fix | Tool pruning | Outcome |
|---|---|---|
| ❌ | ❌ | Today's baseline: 50s/turn forever |
| ✅ | ❌ | Turn 1: 50s. Turns 2+: ~5s each. **10× win on subsequent turns** |
| ❌ | ✅ | Every turn: ~37s (saves the 13s of dead-tool processing). **27% win, no compounding** |
| ✅ | ✅ | Turn 1: ~37s. Turns 2+: ~3s each (smaller prompt, cached). **15× win on subsequent turns** |

Cache fix is the bigger lever. Pruning is the cherry on top. Build cache fix first; pruning second.

---

## Open questions

### How does Claude Code handle "model called a tool that doesn't exist"?

Doesn't seem to be documented. Likely options:
1. Claude Code rejects the tool call locally, the model retries with a different approach
2. Claude Code returns an error to the model, which gets included in the next turn
3. Claude Code crashes (unlikely but worth checking)

We need to know before turning pruning on by default. Phase 1's observation mode lets us see "would have stripped X, model tried to call X" before any user-facing impact.

### Should we also prune tools the model already used but not recently?

Example: model used `WebFetch` in turn 3, hasn't used it in turns 4-10. Keep it? Drop it?

**Default decision: keep forever once used.** Tool need is sticky in coding workloads (if you needed grep once, you'll likely need it again). Avoid the complexity of a usage-recency model in v1.

### Does the data sample bias matter?

The 80-request sample was a TS build. Different sessions (research, doc generation, debugging) might use AskUserQuestion, WebFetch, Skill heavily. We won't know until we observe diverse sessions. Phase 1's observation-only mode lets us collect that data safely.

### What about Skills (slash commands)?

Skills are listed in the `<system-reminder>` block, separate from the tools array. Same pruning idea applies but they're already opt-in by name — the model only sees skills the client included, and the model uses `Skill(name)` to invoke them. Less wasteful than tools, but ~5K tokens of skill descriptions can still be pruned. Future phase.

---

## Success metrics

- **Average prompt token count drops by ≥4K** for sessions past the lock-in turn (5+ turns)
- **Stripped-tool hallucination rate < 1%** of pruned-session requests
- **No measurable quality regression** in model output (subjective; tracked via session abandonment rate as proxy)
- **Combined with cache fix:** p50 turn latency on turn 6+ of a session drops to **<5s** (vs current ~50s)
