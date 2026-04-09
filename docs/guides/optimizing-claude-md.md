# Optimizing CLAUDE.md for Token Efficiency

CLAUDE.md is loaded into context on **every Claude Code turn**. Unlike files read on demand, CLAUDE.md is always present — part of the system prompt sent with every API call. Every line must earn its place by being useful on most turns, not just occasionally.

Uses Ollama Herd as a worked example, but the principles apply to any project.

---

## Why It Matters

**The math:**
- ~4 tokens per line, ~1.33 tokens per word
- 200-line CLAUDE.md = ~800-2,000 tokens per turn (depending on density)
- Over 20-turn session: 16,000-40,000 tokens on project context alone
- Multi-agent tasks (9 dispatches): multiply by 9

**Cost impact (Sonnet 4.6 at $3/MTok input):**

| CLAUDE.md Size | Tokens/Turn | Cost/Session (20 turns) | Cost/Multi-Agent Task |
|----------------|-------------|-------------------------|------------------------|
| 100 lines | ~400 | ~$0.024 | ~$0.22 |
| 200 lines | ~800 | ~$0.048 | ~$0.43 |
| 400 lines | ~1,600 | ~$0.096 | ~$0.86 |

### Prompt Caching

Anthropic caches the static prefix of each API request. Cached tokens cost 10x less:

| Model | Input | Output | Cache Hit |
|-------|-------|--------|-----------|
| Opus 4.6 | $5/MTok | $25/MTok | $0.50/MTok |
| Sonnet 4.6 | $3/MTok | $15/MTok | $0.30/MTok |
| Haiku 4.5 | $1/MTok | $5/MTok | $0.10/MTok |

CLAUDE.md benefits from caching (it's static), but it still **consumes context window space**. A 400-line CLAUDE.md cached at $0.30/MTok is cheap per-token, but those ~1,600 tokens are context the model can't use for your actual code. Cache expires after 5 minutes of inactivity.

---

## The Budget

**Target: under 200 lines.** Anthropic's recommendation.

```bash
wc -l CLAUDE.md        # line count (target: <200)
wc -w CLAUDE.md        # word count (target: <1,500)
# Approximate tokens: word count x 1.33
```

---

## Critical: Avoid @ Import Notation

**Do NOT use `@file.md` notation in CLAUDE.md.** This auto-loads referenced files into context on EVERY turn.

```markdown
# BAD — loads full file contents every turn
@docs/api-reference.md
@docs/configuration-reference.md

# GOOD — plain paths, loaded on demand when needed
See `docs/api-reference.md` for endpoint schemas.
See `docs/configuration-reference.md` for all 47+ env vars.
```

One project reduced ~15,000 tokens/turn to ~2,600 (83% savings) just by converting `@` references to plain paths.

---

## What Belongs in CLAUDE.md

Content needed on **most turns:**

| Category | Example | Lines |
|----------|---------|:---:|
| **Project summary** | What this is, one line | 1-2 |
| **Commands** | Build, test, lint, dev server | 3-5 |
| **Stack** | Language, framework, DB, key deps | 5-8 |
| **Architecture** | Core patterns, key decisions | 8-12 |
| **Code conventions** | Project-specific deviations from defaults | 5-10 |
| **Structure** | Top-level directories only | 6-10 |
| **Testing** | What to verify before pushing | 4-6 |
| **Key gotchas** | Things Claude will get wrong without guidance | 3-5 |
| **Deployment** | Where it runs, how to deploy | 3-6 |

**Total: 40-65 lines of core content.** Rest of budget for project-specific sections genuinely needed every turn.

---

## What Doesn't Belong in CLAUDE.md

Move these to separate docs and add a one-line reference:

- **Detailed directory trees** — Claude can `ls` or Glob. Keep top-level only.
- **Full code examples** — Claude doesn't need a 20-line sample every turn.
- **Historical context** — "We migrated from X to Y in March" is useful once, not every turn.
- **API reference** — belongs in `docs/api-reference.md`, not per-turn context.
- **Feature documentation** — subsystem mechanics, workflow details.
- **Long example lists** — 2 examples establish tone. 5 examples waste tokens.
- **README-style content** — installation guides, feature lists, marketing copy.
- **Things Claude already knows** — Python conventions, FastAPI patterns, Git usage.
- **Stable subsystem details** — if unchanged for weeks and well-documented in code, one line + reference.

**Only include project-specific deviations from defaults:**
- "Never use git worktrees — work directly on main."
- "Never store knowledge in `.claude/` memory files — use committed docs."
- "Thinking models eat `num_predict` budgets — the router auto-inflates by 4x."

These save Claude from mistakes it can't catch itself. Generic advice ("write tests") doesn't.

---

## Optimization Techniques

### 1. One line per concept

Bad (3 lines):
```
The scoring engine evaluates nodes on 7 signals including thermal state,
memory fit, queue depth, wait time, role affinity, availability trend,
and context fit.
```

Good (1 line):
```
ScoringEngine: 7 signals (thermal, memory, queue, wait, affinity, availability, context fit)
```

### 2. Reference instead of inline

Bad (20 lines of module details). Good (1 line):
```
Key modules: see Architecture section — `server/` for routing, `node/` for agents, `common/` for discovery
```

### 3. Tables over paragraphs

Bad (8 lines of prose describing when to use each doc). Good (8-row table with file + purpose in 10 lines).

### 4. Skip what Claude already knows

Don't tell Claude:
- "Use proper error handling" — default behavior
- "Write clean Python" — it knows Python
- "Follow FastAPI conventions" — it knows FastAPI

Only include project-specific deviations.

### 5. Extract, don't delete

When reducing tokens, move content to `docs/` and add a one-line reference. Content preserved, CLAUDE.md stays lean. The content still exists — it's just not loaded every turn.

---

## Decision Framework

### Add to CLAUDE.md when:
- Needed on nearly every turn (build commands, architecture patterns)
- Getting it wrong causes bugs hard to catch (context protection quirks, model naming)
- Project-specific deviation from defaults (no worktrees, no memory files)

### Extract to docs/ when:
- Only needed when working on one specific feature
- Stable and well-documented in code
- Process documentation, not code context
- Detailed reference (all 47+ env vars, full API docs, endpoint schemas)

### Never compress:
- **Build/test commands** — must be complete and runnable
- **Key gotchas** — the whole point is preventing mistakes
- **Release checklist** — safety-critical process

**The test:** If a section is only relevant to 1 in 10 turns, it belongs in a doc. Claude can read docs on demand. It can't un-read CLAUDE.md.

---

## Case Study: Ollama Herd

| Metric | Before | After |
|--------|--------|-------|
| Lines | 246 | ~185 |
| Words | 2,506 | ~1,500 |
| Tokens (est.) | ~3,340 | ~2,000 |
| **Savings** | | **~40%** |

**What was extracted:**

| Section | Technique |
|---------|-----------|
| Key modules table (20 rows) | Compressed to 8 key modules, rest discoverable |
| Documentation table (20 rows) | Compressed to 6 key docs with one-line descriptions |
| Full request flow (7 steps) | Compressed to 3-line summary with doc reference |
| Design principles (6 paragraphs) | Compressed to 6 one-liners |
| Release checklist detail | Kept (safety-critical, can't compress) |

**What stayed:** Build/test commands, architecture overview, key modules, conventions, gotchas, commit format, current deployment state, issues/observations references.

---

## When to Re-Optimize

CLAUDE.md grows naturally. Re-audit when:
- Line count exceeds 200
- You add a major feature (new module, new API surface)
- Multiple contributors add "just one more section"

```bash
wc -l CLAUDE.md
# If over 200: extract the least-frequently-needed section into docs/
```

---

## Related Resources

- [Claude Code costs docs](https://docs.anthropic.com/en/docs/claude-code/costs) — model pricing and token optimization
- [Prompt caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching) — how caching affects CLAUDE.md costs
- [Claude Code memory](https://docs.anthropic.com/en/docs/claude-code/memory) — `@` import behavior and subdirectory CLAUDE.md files
