# Claude Code Proxy Techniques: What the Field Actually Does (2026-04-24)

**Created**: 2026-04-24
**Status**: Deep-dive research — expanded from the original landscape survey with code-level reads of the dominant competitors and cross-referenced with our own operational findings
**Related**:
- [`claude-code-local-ecosystem-landscape.md`](./claude-code-local-ecosystem-landscape.md) — prior landscape scan (2026-04-24 morning)
- [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md) — our primary reliability research
- [`claude-code-local-models.md`](./claude-code-local-models.md) — model-selection guide

---

## TL;DR

The earlier landscape scan identified that the Claude-Code proxy space is crowded; this deep-dive reads the actual code of the top competitors and extracts **specific, named techniques** that are worth adopting or explicitly rejecting. The scan turned up four classes of insight:

1. **System-prompt compression** (nicedreamzapp) — swap Claude Code's 10K-token harness for ~100-token slim prompt. High reward, **high risk** for agentic flows; reject for us in general but expose as an opt-in setting for users who want it.
2. **Prompt-cache trim-based reuse** (nicedreamzapp) — explicit token-by-token prefix matching + cache trim. Their claimed 7.5× speedup matches our own observed 0% → 99% cache-hit delta when bytes stabilise. Implicitly already achieved by our stable-cut clearing + mlx_lm.server's native cache, but worth verifying.
3. **Expanded tool-call JSON repair patterns** (nicedreamzapp) — richer regex catalog for XML-in-JSON hybrids, `<parameter=key>value</parameter>` blocks, single-arg tool inference. **Adopt**: adds recovery breadth with zero risk (our schema-validation guard prevents bad repairs).
4. **Router-level tool filtering via `permissions.deny`** (community tip, blog) — removing tool descriptions from the system prompt saves ~40% of the tool-docs budget. Server-side version: a `FLEET_ANTHROPIC_TOOLS_DENY` setting. **Adopt** as opt-in.

Also surfaced two well-known upstream MLX bugs that explain behavior we've observed:
- **mlx-lm #1081** — ArraysCache trim() missing — blocks speculative decoding. Still open.
- **lmstudio-ai/mlx-engine #314** — MLX multi-round memory leak — open, no fix. Explains why long Claude Code sessions creep toward memory pressure over time.

And a **correction** on the landscape scan: `musistudio/claude-code-router` has 32.8k stars (not unmeasured), not 2.1k. `nicedreamzapp/claude-code-local` is the #2 Claude-Code-MLX-specific proxy, not the dominant generic one.

---

## 1. Methodology

For each candidate proxy, I fetched the GitHub README at minimum. For the top two (`musistudio/claude-code-router`, `nicedreamzapp/claude-code-local`) I also pulled the actual source code where accessible. For techniques that crossed projects (like system-prompt compression), I looked at the community blog posts and forum threads that document them at the "how-to" level rather than just describe outcomes.

The focus questions:
- What specific architectural patterns exist beyond generic translation?
- Which ones target our specific failure mode (long-context + tool-heavy Claude Code sessions)?
- What's the demonstrated impact (benchmarks, before/after)?
- What are the risks or caveats that would make it unsafe to port blindly?

---

## 2. The field, re-scored

Updated tallies with star counts and notable features:

| Project | Stars | Key techniques worth studying |
|---|---|---|
| **musistudio/claude-code-router** | **32.8k** | Transformer pipeline; `longContext` router role; `<CCR-SUBAGENT-MODEL>` prompt-embedded model selection; 32 configurable transformers (deepseek, gemini, openrouter, groq, tooluse, enhancetool, maxtoken, cleancache, reasoning, sampling) |
| **1rgs/claude-code-proxy** | **3.5k** | LiteLLM-based five-step conversion pipeline. Simple/clean baseline. |
| **nicedreamzapp/claude-code-local** | **2.1k** | "Code mode" system-prompt swap (~10K → ~100 tokens); explicit prompt-cache trim reuse; recover_garbled_tool_json with 4-pattern catalog; KV-bits config + `MLX_KV_QUANT_START` tuning |
| **waybarrios/vllm-mlx** | — | Continuous batching; SSD-tiered KV cache (`--ssd-cache-dir`); trie-based prefix cache; warm-prompts preload for 1.3–2.25× TTFT; 12 tool-call parsers; sparse prefill attention |
| **vibheksoni/UniClaudeProxy** | 19 | **ReAct XML fallback** — inject tool descs as XML into system prompt, parse `<tool_call>` blocks. For models without native function calling. |
| **chand1012/claude-code-mlx-proxy** | 40 | Basic MLX wrapper, no reliability features |
| **gglucass/headroom-desktop** | — | Client-side optimization via Claude Code `PreToolUse` hooks; compression ratios: JSON 86–100%, logs 82–95%, multi-turn 56–81% |
| **nielspeter/claude-code-proxy** | — | Clean OpenRouter / OpenAI / Ollama translation |
| **fuergaosi233/claude-code-proxy** | — | Similar, multi-provider |
| **raine/claude-code-proxy** | — | ChatGPT/Kimi subscription focus |
| **vibheksoni/UniClaudeProxy** | 19 | Tool-calling strategies include ReAct XML for non-native models |
| **MadAppGang/claudish** | — | Multi-provider CLI wrapper |
| **drbarq/Claude-Connect** | — | Universal Anthropic-to-anything |
| **LM Studio 0.4.1+** | — | Native Anthropic endpoint in the product |
| **Ollama 0.14.0+** | — | Native Anthropic endpoint in Ollama |
| **LiteLLM** | — | Generic enterprise gateway |

Two stats to sit with:

- **musistudio/claude-code-router** has 15× more stars than the MLX-focused competition combined. The mindshare for "Claude Code with local models" mostly flows through the generic multi-provider router, not the MLX-native servers.
- **The two MLX-native projects (nicedreamzapp, chand1012) together don't outstar the one generic router.** That suggests the *practical* answer for most users is "route Claude Code to a cloud-provider alternative," not "run a local MLX model." We're a niche within a niche — which matches our earlier positioning that the value proposition is reliability engineering on a fleet, not single-machine MLX.

---

## 3. Techniques that appear in top competitors

### 3a. Transformer pipeline pattern (claude-code-router)

Their architecture is **declarative composition of transformers** — each transformer is a small module that modifies the request or response payload. Users wire them in config rather than code:

```yaml
providers:
  - name: deepseek
    transformers:
      - deepseek     # API format adapter
      - tooluse      # optimize tool_choice
      - maxtoken:    # parametric — token cap
          maxtoken: 4096
```

Built-in transformers covered:
- **Provider adapters**: deepseek, gemini, openrouter, groq
- **Tool handling**: `tooluse` (optimize via tool_choice), `enhancetool` (error tolerance)
- **Token management**: `maxtoken`, `cleancache` (strips cache_control markers)
- **Reasoning**: `reasoning` (processes `reasoning_content`), `sampling` (temperature/top_p/top_k)

And a **`longContext` router role** that sends requests exceeding `longContextThreshold` (default 60K) to a specialized model. Claude Code lives below the threshold; longer contexts route to Gemini.

**Assessment for us**: This is essentially a different architectural philosophy — composition over specialization. It's a better fit for "one proxy, many providers," and our stack is more specialized (MLX + Qwen + our fleet). The **`longContext` routing idea** is worth adopting, though — we already have per-tier routing in `FLEET_ANTHROPIC_MODEL_MAP` but it's tier-based (haiku/sonnet/opus), not size-based. A size-based escalation ("requests over 100K go to a higher-context model") could be a useful addition.

### 3b. "Code mode" system-prompt swap (nicedreamzapp)

Concrete code extracted:

```python
CODE_SYSTEM_PROMPT = """You are a local coding assistant running on the user's Mac via MLX...
- Bash: run a shell command
- Read: read a file from disk (use absolute paths)
- Edit: replace exact text in an existing file
- Write: create a new file
- Grep: search file contents (ripgrep)
- Glob: find files by name pattern

RULES:
- Be concise. Skip preamble...
- NEVER say "I am not able to execute this task"...
"""

def optimize_for_code(body):
    body["system"] = CODE_SYSTEM_PROMPT
    tools = body.get("tools", [])
    code_tools = [t for t in tools if t.get("name", "") in CODE_TOOLS_ALLOW]
    if code_tools:
        body["tools"] = code_tools
    return body
```

Their impact claim: **"7.5× speedup (133s → 17.6s per Claude Code task)"** — but this is their headline from eliminating the proxy layer AND using the code-mode prompt AND other tricks. Not isolated.

**Assessment**: this is a genuine-to-consider trick but high-risk because:
1. Claude Code's real system prompt (10K tokens) is carefully crafted by Anthropic to instruct Claude Sonnet/Opus on specific agentic behaviors — stripping it may make the model *behave differently* in hard-to-predict ways.
2. Filtering tools to "the core 6" breaks Claude Code's agentic flow — if the client sends a `TodoWrite` call and the tool isn't in the schema, the model doesn't know what to do.

I'd adopt this as **opt-in only** for users who explicitly want a slim mode. Not a default. Expose as `FLEET_ANTHROPIC_SLIM_SYSTEM_PROMPT=true` with a safe fallback.

### 3c. Explicit prompt-cache trim-based reuse (nicedreamzapp)

This is the highest-signal finding. Their Python code:

```python
# Token-by-token prefix match against previous request
cache_hit_len = 0
if cache_is_safe and _cached_token_prefix is not None:
    max_check = min(len(token_ids), len(_cached_token_prefix))
    for i in range(max_check):
        if token_ids[i] == _cached_token_prefix[i]:
            cache_hit_len = i + 1
        else:
            break

# Trim cache back to shared prefix, only prefill the delta
if cache_hit_len > 0:
    trim_amount = cache_offset - cache_hit_len
    if trim_amount > 0:
        for c in _prompt_cache:
            c.trim(trim_amount)
    delta_tokens = token_ids[cache_hit_len:]
    prompt_for_gen = delta_tokens
else:
    _prompt_cache = None  # fresh cache on miss
    prompt_for_gen = token_ids

_cached_token_prefix = token_ids
```

The logic: on each request, find the longest shared prefix with the previous request's tokens, trim the KV cache back to that length, prefill only the delta. Same mechanism as our `ClearingStore` achieves indirectly — by keeping cleared bytes stable across turns, we enable the mlx_lm.server's own prefix cache to hit.

**Assessment**: this is essentially what mlx_lm.server's built-in `--prompt-cache-*` flags do — maintain a small pool of recent prompts and reuse prefixes. Their version is more explicit because they're serving Anthropic API natively; for us (translating Anthropic → OpenAI → mlx_lm.server), the upstream server does the work if we give it byte-stable prompts. **Fix B (stable-cut clearing)** we shipped 2026-04-24 does exactly this.

Worth verifying: on our next real Claude Code turn, do we actually see `cached_tok > 0`? The earlier observation of 0% cache hit suggests the cache machinery is working but byte-instability was invalidating it. Fix B should close that gap. Need empirical confirmation.

### 3d. Expanded JSON repair patterns (nicedreamzapp)

Their `recover_garbled_tool_json` handles four distinct patterns that we don't:

- **Pattern A**: `parameter=key>value` (equals-sign delimited, no quotes)
- **Pattern B**: `<parameter_key>value` or `<parameter_key>["value"]` (XML-ish)
- **Pattern C**: `"arguments": {key-value pairs with escaped quotes}` (malformed JSON inside quotes)
- **Pattern D**: Single-arg tool with leftover text — infers the arg name from a table (`Bash→command`, `Read→file_path`, `Grep→pattern`, etc.)

Our current `tool_call_repair.py` uses `json-repair` library for generic JSON syntax fixes (trailing commas, missing brackets, unescaped quotes). nicedreamzapp's patterns address a different class: XML-in-JSON hybrids where the model drops out of JSON mode partway and emits XML or free-text.

**Assessment**: port their patterns. Our schema-validation guard ensures we never produce a wrong-schema result, so even if a regex pattern matches spuriously, the result fails the schema check and we fall back to the original. Low risk, non-trivial gain.

### 3e. ReAct XML fallback (UniClaudeProxy)

For models that lack native function calling, they:

1. Inject tool descriptions as XML into the system prompt
2. Parse `<tool_call>` XML blocks from output
3. Convert back to Anthropic `tool_use` format

**Assessment**: not relevant for Qwen3-Coder-Next or gpt-oss:120b — both have solid native tool-calling. But if we ever route to a weaker model (smaller Qwens, Llama), this would be the unlock. Defer until we route to such models.

### 3f. vllm-mlx's warm-prompts + SSD-tiered cache

vllm-mlx is a **different server entirely** from mlx_lm.server. It's built on vLLM's continuous batching engine with an MLX backend. Features worth noting:

- **Warm prompts**: preload common prefixes at startup for 1.3–2.25× TTFT
- **SSD-tiered KV cache**: `--ssd-cache-dir` spills to disk, allowing much more than 16GB of effective cache
- **Prefix-trie cache**: shared across requests
- **Continuous batching**: multiple concurrent requests

**Assessment**: switching from mlx_lm.server to vllm-mlx is a backend migration — a multi-day project. The SSD-tiered cache is genuinely attractive because we currently cap at 16 GB of prompt cache; spilling to the Mac's NVMe would let us hold much more. But it's upstream code we don't control, and we'd lose our kv-bits patch (which we'd have to re-port).

Worth revisiting IF prompt-cache pressure becomes the bottleneck. Currently the bottleneck is byte-instability, which Fix B addressed. Keep on the radar.

### 3g. Headroom's client-side compression pipeline

A different design point entirely — **client-side hooks**, not a proxy server. Installs into `~/.claude/settings.json` as a `PreToolUse` hook, plus RTK for CLI output compression. Compression ratios reported: JSON 86–100%, logs 82–95%, multi-turn 56–81%.

**Assessment**: operates on a layer below us (it rewrites tool results BEFORE Claude Code sends them to our router). Complementary, not competing. The compression techniques could inspire server-side additions:

- **Structured-data detection** — if a tool_result is large JSON, pretty-print → compact → often 50%+ savings
- **Log error preservation** — keep error lines verbatim, compress progress/INFO noise
- **Short-content skip** — we already do this via `min_bloat_tokens`

The "skip low-value content" pattern matches our strategy. The "targeted JSON/log compression" is a potential addition — but the `json-repair` library compresses invalid JSON; compressing *valid* JSON for context savings is different territory and risks changing semantics.

---

## 4. User-side / config techniques (community)

These don't require any code change from us — they're Claude Code CLI configurations users can apply.

### 4a. `permissions.deny` to strip tool descriptions

Adding tool names to `~/.claude/settings.json`'s `permissions.deny` array prevents Claude Code from loading their instruction text into the system prompt. Example from the community:

```json
{
  "permissions": {
    "deny": ["NotebookEdit", "Edit", "Glob", "Grep", "Read", "Write"]
  }
}
```

Reported savings: **16K → 9.8K tokens** for the tools section (40% reduction). The author was disabling tools they'd replaced with an MCP (serena) that duplicated the built-in capabilities.

**For us**: document this in our Claude Code integration guide. Also consider implementing a **server-side equivalent**: `FLEET_ANTHROPIC_TOOLS_DENY="NotebookEdit,Agent"` would strip matching tool definitions from the outbound body before forwarding. Lets ops decide tool subset without requiring Claude-Code-side config changes on every developer machine.

### 4b. The 80/20 rule

From claudefa.st expert guide: **never use the final 20% of your context window for complex multi-file tasks.** Exit and restart at 80%. Use `/compact` at natural breakpoints (after major features), not during active debugging.

**For us**: document. Our pre-inference 413 cap at 180K is the hard ceiling; soft-suggest at 80% via response header warning could be useful. Lower priority.

### 4c. Compaction timing

Community consensus (across multiple blog posts): run `/compact` at 60% usage, not 95%. Auto-compact kicks in around 95% in hosted Claude Code; by then the model is already degraded. Proactive `/compact` at task boundaries is higher quality.

**For us**: our layered context management already fires at 100K. That's about 50% of a 200K window — aligned with the advice. No change needed.

---

## 5. Upstream bugs that affect us

### 5a. mlx-lm #1081 — ArraysCache trim() missing

Blocks speculative decoding. Still open. We already filed our own issue tracking this (`docs/issues/mlx-speculative-decoding-blocked.md`). Infrastructure ready; waiting on upstream fix.

### 5b. lmstudio-ai/mlx-engine #314 — MLX multi-round memory leak

The MLX version of Qwen3.6 35B consumes significantly more memory than the GGUF equivalent during multi-round inference, ultimately crashing with `[METAL] Command buffer execution failed: Insufficient Memory` on a 32GB M4. Still open. No documented workaround.

**Explains behavior we've observed**: long Claude Code sessions (2,700+ messages, hours of use) tend to creep toward memory pressure even though our MLX RSS plateaus at ~45GB. The bug is that KV cache state isn't fully freed between inference rounds — each multi-round session accumulates GPU memory until the process OOMs or is restarted.

**Mitigations on our side**: we already kv-quantize (saves some KV memory) and restart MLX when needed. Could add automated "restart MLX after N requests" as a safety net, but that would kill the prompt cache — bad trade.

Watch the upstream issue.

---

## 6. What this research changes about our strategy

### Confirmed — keep doing what we're doing

- **Layered context management** (mechanical clearing + LLM compactor + pre-inference 413) is the right core architecture. Nobody else does the full four-layer stack.
- **Stable-cut clearing via ClearingStore** (shipped 2026-04-24 in Fix B) is the right approach to byte-stability. nicedreamzapp's explicit trim-based reuse is the same idea implemented one layer deeper (inside their server). Both work.
- **Per-tier model routing** (`FLEET_ANTHROPIC_MODEL_MAP` with haiku→gpt-oss, sonnet→MLX) matches musistudio/claude-code-router's approach. They have `longContext` routing as a third axis; we could add it.
- **Multi-node fleet + observability + benchmark tooling** — no one else does this depth. Maintain the lead.
- **Conservative on wrapping** — we resisted extracting the Claude-Code-reliability layer into its own repo, and that remains correct; the market has many Claude-Code-proxy projects but no one doing what we do at the fleet level.

### Actionable improvements ranked by ROI × risk

**A. Expanded JSON repair patterns (HIGH value, LOW risk).** Port nicedreamzapp's Pattern A–D regex catalog into our `tool_call_repair.py`. Adds XML-in-JSON recovery, single-arg inference table. Schema-validation guard makes it safe. **~2 hours.**

**B. `FLEET_ANTHROPIC_TOOLS_DENY` server-side tool filtering (MEDIUM value, LOW risk).** Let operators strip specified tools from outbound schemas without each Claude Code instance needing config changes. Saves 40% of tool-section tokens in typical setups. **~1 hour.**

**C. Opt-in slim system prompt for Claude Code (MEDIUM value, HIGH risk).** Expose `FLEET_ANTHROPIC_SLIM_SYSTEM_PROMPT=true`. When set, replace Claude Code's 10K-token system prompt with our compact version. Default off because the risk to agentic behavior is real. **~2 hours, careful testing required.**

**D. Size-based routing escalation (MEDIUM value, LOW risk).** Currently tier-based (`claude-haiku-*` / `claude-sonnet-*`). Add a size-based override: requests over `FLEET_ANTHROPIC_SIZE_ESCALATION_TOKENS` get routed to a different model regardless of tier. Matches musistudio's `longContext` pattern. **~2 hours.**

**E. Warm-prompt preload on MLX startup (LOW value, LOW risk).** Kick off a single request with Claude Code's typical system prompt at supervisor startup so the cache is warm for the first real request. Saves ~30–60s on first-request latency after a restart. Modest win, but free. **~30 min.**

**F. Document user-side techniques in our integration guide.** `permissions.deny`, /compact timing, 80/20 rule, session restart pattern. **~30 min, no code change.**

### Not worth doing

- **Switch to vllm-mlx backend** (migration risk, loses our kv-bits patch)
- **Implement ReAct XML fallback** (our models have native tool-calling; we'd use it for a use case we don't have)
- **Emulate native Anthropic-on-MLX server** (nicedreamzapp's approach — huge rewrite, claimed speedups include many confounds)

---

## 7. Sources

Deep-read:
- [`musistudio/claude-code-router`](https://github.com/musistudio/claude-code-router) — 32.8k stars, transformer pipeline
- [`nicedreamzapp/claude-code-local`](https://github.com/nicedreamzapp/claude-code-local) — 2.1k stars, code + README scraped
- [`1rgs/claude-code-proxy`](https://github.com/1rgs/claude-code-proxy) — 3.5k stars, LiteLLM-based

Reviewed but not deep-read:
- [`vibheksoni/UniClaudeProxy`](https://github.com/vibheksoni/UniClaudeProxy) — ReAct XML fallback technique
- [`waybarrios/vllm-mlx`](https://github.com/waybarrios/vllm-mlx) — continuous batching + SSD cache
- [`gglucass/headroom-desktop`](https://github.com/gglucass/headroom-desktop) — client-side compression
- [`chand1012/claude-code-mlx-proxy`](https://github.com/chand1012/claude-code-mlx-proxy), [`nielspeter/claude-code-proxy`](https://github.com/nielspeter/claude-code-proxy), [`fuergaosi233/claude-code-proxy`](https://github.com/fuergaosi233/claude-code-proxy), [`raine/claude-code-proxy`](https://github.com/raine/claude-code-proxy), [`drbarq/Claude-Connect`](https://github.com/drbarq/Claude-Connect), [`MadAppGang/claudish`](https://github.com/MadAppGang/claudish)

Upstream bug reports:
- [`ml-explore/mlx-lm#1081`](https://github.com/ml-explore/mlx-lm/issues/1081) — ArraysCache.trim missing (blocks speculative)
- [`lmstudio-ai/mlx-engine#314`](https://github.com/lmstudio-ai/mlx-engine/issues/314) — MLX multi-round memory leak

Community write-ups:
- [claudefa.st context management](https://claudefa.st/blog/guide/mechanics/context-management) — 80/20 rule, /compact timing
- [zenn.dev: reducing system prompt via permissions.deny](https://zenn.dev/sqer/articles/5c52615eeabce0?locale=en) — 40% token reduction technique
- [Justin3go: shedding heavy memories](https://justin3go.com/en/posts/2026/04/09-context-compaction-in-codex-claude-code-and-opencode) — three-tier compaction comparison
- [LM Studio × Claude Code integration](https://lmstudio.ai/blog/claudecode) — native Anthropic endpoint in LM Studio
- [Ollama × Claude Code docs](https://docs.ollama.com/integrations/claude-code) — native integration, admits "edge cases still being patched"

Ollama-herd internal:
- [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md)
- [`claude-code-local-ecosystem-landscape.md`](./claude-code-local-ecosystem-landscape.md)
- [`claude-code-ollama-ecosystem-2026.md`](./claude-code-ollama-ecosystem-2026.md)
