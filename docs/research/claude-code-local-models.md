# Best Local OSS Models for Claude Code CLI

**Created**: 2026-04-22
**Status**: Evidence-based — web research + local trace data from a 512GB M3 Ultra Mac Studio
**Related**:
- [`why-claude-code-degrades-at-30k.md`](./why-claude-code-degrades-at-30k.md) — companion post-mortem on the tool-call looping failure at 30K tokens and why Qwen3-Coder-Next is the recommended swap (filed 2026-04-23 after the `<function=TaskUpdate>` hallucination regression on the 480B)
- [`claude-code-ollama-ecosystem-2026.md`](./claude-code-ollama-ecosystem-2026.md) — ecosystem state / symptom-to-upstream-issue mapping
- [`docs/guides/claude-code-integration.md`](../guides/claude-code-integration.md)

---

## Hard constraint

Claude Code turns must complete in **under 30 seconds** or the experience breaks. A typical turn generates 300–500 tokens after prefilling a 2–5K token prompt. That sets a floor of roughly **15 tok/s generation + fast prefill**. Anything slower is unacceptable for interactive agentic use, regardless of quality.

Two architectural facts drive every recommendation in this doc:

1. **Claude Code is tool-use-first.** Every turn is a function call (Read, Edit, Bash, Grep, …). A model that produces beautiful prose but drops or malforms `tool_calls` breaks the loop. Benchmarks that matter: **BFCL V4** (Berkeley Function-Calling Leaderboard, [live at gorilla.cs.berkeley.edu](https://gorilla.cs.berkeley.edu/leaderboard.html), updated 2026-04-12), **τ-bench / τ²-bench**, **SWE-bench Verified**.
2. **M3 Ultra is memory-bandwidth-bound** (819 GB/s). Token generation speed ≈ bandwidth ÷ active-parameter-bytes. **MoE with small active-params wins**; dense large models lose.

---

## TL;DR recommendation

For **512GB M3 Ultra with ~335GB free** (user's current config):

```bash
export FLEET_ANTHROPIC_MODEL_MAP='{
  "default":"qwen3-coder:30b",
  "claude-haiku-4-5":"qwen3-coder:30b",
  "claude-haiku-4-5-20251001":"qwen3-coder:30b",
  "claude-sonnet-4-5":"qwen3-coder:30b",
  "claude-sonnet-4-6":"qwen3-coder:30b",
  "claude-opus-4-7":"qwen3-coder:480b-a35b-q4_K_M"
}'
```

| Claude tier | Local OSS model | Size on disk | Expected tok/s on M3 Ultra | Why |
|---|---|---|---|---|
| **haiku** | `qwen3-coder:30b` | 18 GB | 120–170 | Same model as sonnet — no swap cost. 3B active MoE is absurdly fast. |
| **sonnet** | `qwen3-coder:30b` | 18 GB | 120–170 | Top open coder for tool use at its class, per [Galaxy.ai 2026 comparison](https://blog.galaxy.ai/compare/deepseek-v3-2-vs-qwen3-coder-30b-a3b-instruct). BFCL-leading for coding-specific tasks. |
| **opus** | `qwen3-coder:480b-a35b-q4_K_M` | ~270 GB | 40–55 | SOTA open agentic coding, *comparable to Claude Sonnet 4* ([Qwen blog](https://qwenlm.github.io/blog/qwen3-coder/)). 35B active MoE fits the M3 Ultra bandwidth sweet spot. |

This comfortably hits the 30-second ceiling on every tier:
- Sonnet turn (500 tokens @ 150 tok/s) = **~3 seconds**
- Opus turn (500 tokens @ 50 tok/s) = **~10 seconds**

**If you don't want to pull 270GB right now**, substitute `qwen3:235b-a22b-q4_K_M` for opus (142GB, already pulled on this machine, 25–35 tok/s, 500-token turn = ~17s). Still comfortable inside the budget.

---

## The real bottleneck is prefill, not decode

Picking the right model is necessary but not sufficient. The latency killer in long Claude Code sessions is not generation — it's **prefill**. Every turn re-encodes the entire conversation history from scratch, because neither Ollama nor ollama-herd implements prompt caching (real Anthropic Claude does).

Measured pattern from a Claude Code session on an M5 Pro 48GB laptop running qwen3-coder:30b (actual trace data):

| Turn | Input tokens | Output tokens | Elapsed |
|---|---|---|---|
| 16 | 15.7K | 34 | 46s |
| 17 | 16.5K | 54 | 51s |
| 20 | 19.3K | 51 | 64s |
| 21 | 21.9K | 54 | 78s |
| 22 | 23.7K | 54 | 89s |
| 23 | 24.3K | 57 | 93s |
| 24 | 26.4K | 53 | 109s |
| 25 | 28.8K | 58 | 130s |

**By turn 25, 99% of the elapsed time is prefill.** Generating 58 tokens takes ~1 second; re-encoding 28.8K tokens of conversation takes ~129 seconds. Latency grows linearly in context length.

Switching to M3 Ultra helps by ~4× (more GPU cores → faster prefill, more bandwidth → faster decode), which pushes turn 25 from 130s to ~32s — right at the pain threshold. The architectural fix is **prefix caching** (vLLM has it; Ollama doesn't), which cuts prefill on continuations by 70–90%. Until that ships in herd, every long Claude Code session asymptotically approaches unusability.

### Context-management enhancements in priority order

#### Tier 1 — Behavioral (immediate, no engineering)

1. **Trim CLAUDE.md.** Your project's `CLAUDE.md` is injected into the system prompt on *every turn*. This repo's CLAUDE.md is **3,022 tokens (197 lines, measured with cl100k_base)**. Over a 30-turn session, that's 90,600 tokens of redundant prefill. Keep only the load-bearing "how to build/test/commit" in CLAUDE.md; move gotchas, release process, and conventions into `docs/*.md` that Claude Code reads on demand. Target: under 1,500 tokens.
2. **Use `/compact` aggressively.** Claude Code's built-in history compressor. Fires on demand, drops context from 28K back to ~4K. Rule of thumb: every 15 turns or whenever a turn feels sluggish.
3. **Use `/clear` between distinct tasks.** Full reset. When you finish a feature and move to a bug, `/clear` > continuing.
4. **Prefer `Grep` over `Read`.** `Read` dumps full files into history permanently. `Grep` with `output_mode: files_with_matches` returns ~100 tokens vs Read's ~15,000. For exploration, always Grep first.
5. **Use `Read` with `offset` + `limit`** when the target file is large. Don't read 2,000 lines end-to-end.
6. **Prefer Claude Code's Task tool for exploration** — spawns a fresh context; exploration history doesn't bloat the main session.

#### Tier 2 — Herd configuration (no code, 5 minutes)

7. **`FLEET_DYNAMIC_NUM_CTX=true`** on the router. Watches trace-DB actual usage and auto-sizes Ollama's `num_ctx` allocation. Without this, qwen3-coder:30b allocates its full 262K native context by default, wasting KV cache RAM and slowing prefill on unused attention surface. See [`docs/plans/dynamic-num-ctx.md`](../plans/dynamic-num-ctx.md).
8. **`FLEET_ANTHROPIC_DEFAULT_MAX_TOKENS=4096`.** Claude Code sends `max_tokens=32000` per turn; caps runaway output. Typical Claude Code responses are 50–500 tokens; anything over 4K is the model looping.
9. **`OLLAMA_KEEP_ALIVE=-1`.** Already set on this Mac Studio per CLAUDE.md. Keeps models hot in VRAM; avoids cold-load penalty between sessions.

#### Tier 3 — Herd architectural (engineering effort, biggest single win)

10. **Prefix-hash cache in the streaming proxy.** The real fix. Approach:
    - Hash the canonical prefix of each inbound prompt (system + stable message prefix)
    - If the first N messages match the previous request on the same node+model, signal Ollama to reuse cached KV
    - Ollama's `/api/chat` doesn't expose this directly, but llama.cpp (its backend) supports `cache_prompt: true`
    - Estimated effort: 2–3 engineering days. Expected impact: 70–90% reduction in prefill time on long Claude Code sessions.
11. **Route sonnet-tier traffic to vLLM instead of Ollama.** vLLM has production-grade prefix caching. Hybrid: qwen3-coder:30b served by vLLM for Claude Code agentic traffic, Ollama for everything else. Larger integration cost, significant payoff.
12. **Session affinity routing.** Detect request continuations; prefer routing warm sessions to the node with matching KV. Moderate effort.

### What Claude Code CLI does NOT give you

- No `--context` flag or `CLAUDE_CODE_MAX_CONTEXT_TOKENS` env var. The server decides the context window based on model capabilities.
- No manual KV cache control from the client side.
- Auto-compact threshold is undocumented and sits around ~200K tokens — too high to protect a local-model session.
- Capping `max_tokens` on the client doesn't reduce input prefill cost (the 32K default is an output cap).

So the context-management wins live in two places: **what you send** (trim CLAUDE.md, compact/clear, prefer Grep) and **what the server does with it** (dynamic num_ctx, prefix caching).

---

## Running on smaller Macs (laptops, <128GB unified memory)

For machines like an M5 Pro 48GB or M3 Max 36GB — common developer laptops — the answer changes. These machines **cannot run Claude Code comfortably against a 30B-class model locally**, for one reason: prefill.

### Why local 30B fails on a 48GB laptop

Measured on an M5 Pro 48GB laptop (20 GPU cores, ~250 GB/s bandwidth) running qwen3-coder:30b alongside qwen3:8b:

- **Wired memory: 28 GB of 36 GB ceiling consumed** (78% of the wired GPU budget)
- **Compressor pressure: 12–13 GB** (macOS squeezing other apps to make room)
- **Decode: ~10–15 tok/s** for 30B Q4 — acceptable on its own
- **Prefill on 28K tokens: ~100 seconds** — this is the failure mode
- **Observed turn latency at turn 25: 130 seconds** — 4× over the 30-second ceiling

The chip isn't the problem for decode. The problem is that Claude Code's hot-loop is re-encoding the full conversation on every turn, and 20 GPU cores take 4× longer to do that matmul than the Mac Studio's 80.

### Recommended strategy for laptops

**Online (on LAN):** route Claude Code to a Mac Studio via ollama-herd. Zero local inference. No VRAM pressure, no compressor swap, full sonnet-class quality at M3 Ultra speed.

```bash
# On the laptop
export ANTHROPIC_BASE_URL=http://<mac-studio-ip>:11435
export ANTHROPIC_AUTH_TOKEN=dummy
claude
```

**Offline (traveling):** run ollama-herd locally on the laptop with `qwen3:8b` mapped to *all* Claude tiers. Accept the quality floor — 8B can't handle hard multi-file tool-use, but it works for small edits, classification, and quick dispatch.

```bash
export FLEET_ANTHROPIC_MODEL_MAP='{"default":"qwen3:8b","claude-haiku-4-5":"qwen3:8b","claude-sonnet-4-5":"qwen3:8b","claude-opus-4-7":"qwen3:8b"}'
```

**Unload qwen3-coder:30b on laptops.** It fits in memory but isn't usable in Claude Code at long contexts on this hardware. `ollama stop qwen3-coder:30b` (or `ollama rm` if you're sure) reclaims 18GB and eliminates the compressor pressure.

Two shell aliases make the mode switch one command:

```bash
# ~/.zshrc on the laptop
alias claude-home='export ANTHROPIC_BASE_URL=http://10.0.0.10:11435 ANTHROPIC_AUTH_TOKEN=dummy; claude'
alias claude-offline='export ANTHROPIC_BASE_URL=http://localhost:11435 ANTHROPIC_AUTH_TOKEN=dummy; claude'
```

### Honest floor for 48GB laptops

With 48GB unified and ~36GB wired ceiling:

| Model class | Fits? | Usable for Claude Code? |
|---|---|---|
| 30B MoE A3B (qwen3-coder:30b) | Yes (20GB) | **No** — prefill too slow at long context |
| 14B dense (qwen3:14b) | Yes (~9GB) | Borderline; still slow on 28K prefill |
| 8B dense (qwen3:8b) | Yes (~5GB) | **Yes** — usable for haiku-class tasks |
| 4B dense (gemma3:4b) | Yes (~3GB) | Fast but tool-use reliability drops |

Laptops are haiku-class machines. Let the Mac Studio carry the sonnet and opus tiers.

---

## Operational observations from production Claude Code use

### Zombie requests on huge-context turns

From the trace DB on the M5 Pro laptop case:

```
WARNING queue_manager  Reaped stale in-flight 9e32b01b from
                       Twins-MacBook-Pro:qwen3-coder:30b (stuck for 625s)
```

One Claude Code turn — almost certainly one of the 28K-context turns where Ollama hit an internal timeout or memory wall — hung for 10+ minutes before the queue reaper killed it. The reaper did its job (no leak, queue stayed clean), but that request returned an error to Claude Code. Worth checking how Claude Code surfaces this to the user (hanging wait vs clean error).

Monitor with:
```bash
curl -s http://localhost:11435/fleet/queue | python3 -m json.tool
```

Repeated zombies on the same model+node combination indicate context pressure; lowering `num_ctx` or enabling `FLEET_DYNAMIC_NUM_CTX` usually fixes it.

### Things that ARE working (qwen3-coder:30b in Claude Code)

From live trace data:

- **Zero synthesized-stop warnings** → Ollama is not dropping streams mid-response. Flash attention + Q8 KV config is stable on qwen3-coder:30b.
- **Zero tool-arg parse warnings** → qwen3-coder:30b produces clean JSON tool arguments every turn. This is the thing that matters most and it's working.
- **Zero exceptions in `anthropic_compat` route** → the Anthropic → Ollama → Anthropic translation is stable.
- **Routing scoring healthy** (82–93, above the 80 "good-fit" threshold).
- **Haiku → qwen3:8b routes consistently** when mapped; no VRAM-fallback hijacks.

This validates the core architecture. The pain is purely context growth and prefill latency, not model quality or integration bugs.

---

## Why tool use dominates the decision

The Ollama-herd integration guide calls this out directly: *"If a turn comes back with no tool call when one was needed, it's almost always a model-quality issue, not an integration bug."*

Reddit, HN, and 2026 blog reports converge on three model failure modes for Claude Code:

- **Drops the tool call** — model replies with prose ("To read that file, you would use…") instead of emitting a `tool_calls` block.
- **Malformed args** — wrong parameter name, wrong type, missing required field.
- **Infinite loop** — re-reads the same file forever instead of editing it.

Real evidence these matter in practice:

- **GLM-4.7 stress-tested against Claude Opus 4.5 in Claude Code**: "required multiple debug cycles," "was unable to figure out why I was getting no response," ultimately only produced a basic proxy missing Claude Code's system instructions. [Code Miners, 2026](https://blog.codeminer42.com/claude-code-ollama-stress-testing-opus-4-5-vs-glm-4-7/)
- **"Testing qwen3-coder (32B) on a Mac was very slow. Getting down to devstral-small-2 (24B) resulted in acceptable speed."** — a 2026 report from a smaller Mac. On M3 Ultra this reverses: qwen3-coder (MoE A3B) is *faster* than devstral because MoE wins on Apple Silicon bandwidth. Hardware-sensitive.
- **"A task that took cloud Claude 73 seconds took a local model 82 minutes"** on long multi-file operations — [XDA Developers, 2026](https://www.xda-developers.com/claude-code-local-llm-ollama-capable-costs-nothing/). The gap is worst on sustained sequential tool calls, which is exactly Claude Code's hot path.

## Why MoE + small active-params on M3 Ultra

M3 Ultra has 819 GB/s unified memory bandwidth. Token generation is bandwidth-bound, so:

```
tok/s ≈ memory_bandwidth / (active_params × bytes_per_param)
```

For Q4_K_M quantization (~0.5 bytes/param):

| Architecture | Active params | Theoretical ceiling | Observed |
|---|---|---|---|
| Dense 70B | 70B | ~23 tok/s | 15–25 tok/s |
| Dense 30B | 30B | ~55 tok/s | 20–35 tok/s |
| MoE 235B / 22B active (Qwen3:235b-a22b) | 22B | ~74 tok/s | **25–35 tok/s** confirmed ([MacStories benchmark](https://www.macstories.net/notes/notes-on-early-mac-studio-ai-benchmarks-with-qwen3-235b-a22b-and-qwen2-5-vl-72b/)) |
| MoE 480B / 35B active (Qwen3-Coder 480B) | 35B | ~47 tok/s | **40–55 tok/s** projected |
| MoE 30B / 3B active (Qwen3-Coder 30B) | 3B | ~500+ tok/s (decode-overhead-limited) | **120–172 tok/s** observed on M4 Max; M3 Ultra likely similar or higher |
| MoE 671B / 37B active (DeepSeek-V3) | 37B | ~44 tok/s | 20–30 tok/s (slower due to model size overhead) |

**Observed throughput on this specific Mac Studio from trace DB** (`~/.fleet-manager/latency.db`, 30-day window):

| Model | Completions observed | Observed tok/s (trace-derived) |
|---|---|---|
| `gpt-oss:120b` | **214,133** | **60.7** |
| `gemma3:4b` | 86 | 95.8 |
| `gemma3:27b` | 574 | 23.7 |
| `llama3.2:1b` | 1,015 | 48.6 |

(Trace-derived tok/s = `completion_tokens / (latency_ms − ttft_ms)` averaged across completed requests. Approximation; actual per-request tok/s varies with prompt length and model load state.)

Note: no meaningful agentic-use sample for `qwen3-coder:30b` yet (only 2 requests via the Anthropic route), so its M3 Ultra throughput is projected from M4 Max public benchmarks. Real numbers will show up in the trace DB as Claude Code usage ramps.

---

## Tier-by-tier recommendations

### claude-sonnet-4-5 / 4-6 → qwen3-coder:30b (the daily driver)

This is the most important tier. The candidate pool in Q2 2026:

| Model | Tool use | Speed on M3 Ultra | Coding quality | Pick? |
|---|---|---|---|---|
| **qwen3-coder:30b** (MoE A3B) | **Top of BFCL for coding** | 120–170 tok/s | SOTA open at 30B | ✅ **primary** |
| qwen3.5:27b-coding | Good (2026 release) | ~30 tok/s (dense) | 80.7% LiveCodeBench v6 | Slower; no clear advantage |
| qwen2.5-coder:32b | Good | ~20 tok/s (dense) | Solid but older | Fallback |
| devstral:24b | Reported OK | ~30 tok/s (dense) | Decent | Smaller Mac alternative |
| codestral:22b | **Poor** (drops tool format) | ~25 tok/s | N/A | ❌ integration guide flags it |
| glm-4.6 / 4.7 | Variable | N/A locally (`:cloud` only on Ollama) | Competitive with Sonnet 4 on benchmarks | Cloud route only |

Why qwen3-coder:30b specifically:

- Released by Alibaba as the **dedicated agentic coder** in the Qwen3 line
- 30.5B params, MoE with ~3B active (A3B architecture)
- 262K native context
- **`tools` listed as a first-class capability** in `ollama show qwen3-coder:30b`
- Per [Galaxy.ai comparison](https://blog.galaxy.ai/compare/deepseek-v3-2-vs-qwen3-coder-30b-a3b-instruct): *"Qwen3-Coder-30B-A3B-Instruct scores highest on the Berkeley Function Calling Leaderboard for coding-specific tasks"*
- At 120–170 tok/s generation, a 500-token Claude Code turn completes in 3–4 seconds

### claude-haiku-4-5 → qwen3-coder:30b (same model)

Conventional wisdom: "map haiku to a small fast model." On M3 Ultra with 335GB free, don't bother. Map haiku to the same model as sonnet. Three reasons:

1. **No model-swap latency.** Swapping between 30B and 8B models forces Ollama to evict and reload between agent and subagent turns.
2. **Consistent tool behavior.** Two models = two different tool-use failure modes. Debugging is easier when the whole loop speaks the same language.
3. **qwen3-coder:30b is already faster than you can read.** The speed win from an 8B model is invisible.

If you have **<128GB available memory**, swap haiku to `qwen3:8b` (pull it) or `gemma3:4b` (already on this machine).

### claude-opus-4-7 → qwen3-coder:480b-a35b-q4_K_M (SOTA open agentic)

Opus is the "hard task, willing to wait" tier — architectural decisions, multi-file refactors, complex debugging.

**Qwen3-Coder 480B A35B** was released July 2025 and is the current state-of-the-art open model for agentic coding:

> *"Qwen3-Coder-480B-A35B-Instruct sets new state-of-the-art results among open models on Agentic Coding, Agentic Browser-Use, and Agentic Tool-Use, **comparable to Claude Sonnet 4**."* — [Qwen blog](https://qwenlm.github.io/blog/qwen3-coder/)

- 480B total params, 35B active (MoE)
- Native 256K context, extrapolates to 1M
- Q4_K_M on disk: ~270GB (fits comfortably on 512GB Mac Studio)
- M3 Ultra projected throughput: 40–55 tok/s. A 500-token opus turn = ~10 seconds.
- [Artificial Analysis](https://artificialanalysis.ai/models/qwen3-coder-480b-a35b-instruct): cloud-hosted throughput 60 tok/s (above median of 52 for comparable open models)

**Pull command:**
```bash
ollama pull qwen3-coder:480b-a35b-q4_K_M
```

Expect ~30–60 minutes depending on connection.

**Fallback if you don't want to pull 270GB:**

- **`qwen3:235b-a22b-q4_K_M`** — 142GB, already pulled on this machine. Non-coder-specific but strong general reasoner + tool use. 25–35 tok/s confirmed on M3 Ultra (MacStories, 2026). 500-token turn = ~17s. Inside the 30s budget.

**Do NOT map opus to:**
- `gpt-oss:120b` — thinking model. 60.7 tok/s observed on this machine is actually fine speed-wise, but the architecture burns tokens on internal reasoning before tool calls. Works for chat; hurts agent loops.
- `deepseek-r1:70b` — same thinking-model failure mode. Integration guide explicitly flags it as "Poor" for tool use.
- `llama3.3:70b` — dense 70B, ~20 tok/s. Not coding-tuned. qwen3-coder:30b beats it on coding benchmarks at 6× the speed.

---

## Thinking vs non-thinking: the honest rule

A subtle trap: some of the smartest open models in 2026 (R1, gpt-oss:120b, DeepSeek-V3.2's thinking mode) perform **worse** in Claude Code than "less smart" non-thinking coders.

- Thinking models emit chain-of-thought tokens before their answer. For one-shot hard problems, this is a win.
- Claude Code is *many small decisions*, not one hard problem. Each turn should produce a tool call in <100 tokens, not 1000 tokens of internal deliberation.
- Ollama-herd's router auto-inflates `num_predict` by 4× for known thinking models, but that only prevents truncation — it doesn't make the loop faster.

[DeepSeek-V3.2](https://blog.galaxy.ai/compare/deepseek-v3-2-vs-qwen3-coder-30b-a3b-instruct) is the interesting exception: it *"supports tool calls in both thinking and non-thinking modes."* If Ollama exposes non-thinking mode cleanly, V3.2 becomes a viable opus candidate. As of this writing, the tag behavior needs verification — test before committing.

**Rule of thumb:** map Claude Code tiers to non-thinking coder models. Keep thinking models loaded on other routes (`/api/chat`, `/v1/chat/completions`) for pure reasoning.

---

## Cloud-hosted fallback tier (optional)

Ollama now proxies several models via their cloud:

- `qwen3.5:cloud` — frontier Alibaba
- `glm-4.7:cloud` / `glm-4.6:cloud` — Z.ai (competitive with Claude Sonnet 4 on some benchmarks, though stress-tested as weaker in actual Claude Code use per [Code Miners](https://blog.codeminer42.com/claude-code-ollama-stress-testing-opus-4-5-vs-glm-4-7/))
- `kimi-k2.5:cloud` — Moonshot
- `minimax-m2.5:cloud` — MiniMax

These show up in `ollama list` with size "—". They go through Ollama's API but inference happens remote. Useful when you want frontier quality without 270GB of local weights, but **defeats the privacy-local-first premise** of running Claude Code locally. Use deliberately, not by default.

---

## Setup commands (M3 Ultra 512GB configuration)

```bash
# Already pulled on this machine — verify:
ollama list | grep -E "qwen3-coder:30b|qwen3:235b"

# Pull the opus tier (one-time, ~270GB):
ollama pull qwen3-coder:480b-a35b-q4_K_M &

# Set the map
export FLEET_ANTHROPIC_MODEL_MAP='{"default":"qwen3-coder:30b","claude-haiku-4-5":"qwen3-coder:30b","claude-haiku-4-5-20251001":"qwen3-coder:30b","claude-sonnet-4-5":"qwen3-coder:30b","claude-sonnet-4-6":"qwen3-coder:30b","claude-opus-4-7":"qwen3-coder:480b-a35b-q4_K_M"}'

# Persist for future shells
echo "export FLEET_ANTHROPIC_MODEL_MAP='$FLEET_ANTHROPIC_MODEL_MAP'" >> ~/.zshrc

# Restart herd to pick up the env var (canonical recipe — see CLAUDE.md
# § Local deployment for why mlx_lm.server has to be in the pkill list)
pkill -9 -f "bin/herd|mlx_lm.server" && sleep 3
uv run herd &>/dev/null & disown
sleep 3
uv run herd-node &>/dev/null & disown

# On your laptop (or this machine):
export ANTHROPIC_BASE_URL=http://10.0.0.10:11435
export ANTHROPIC_AUTH_TOKEN=dummy
claude
```

### Interim map (until 480B finishes pulling)

Use qwen3:235b for opus in the meantime:

```bash
export FLEET_ANTHROPIC_MODEL_MAP='{"default":"qwen3-coder:30b","claude-haiku-4-5":"qwen3-coder:30b","claude-haiku-4-5-20251001":"qwen3-coder:30b","claude-sonnet-4-5":"qwen3-coder:30b","claude-sonnet-4-6":"qwen3-coder:30b","claude-opus-4-7":"qwen3:235b-a22b-q4_K_M"}'
```

---

## Verification workflow

After setting the map, verify tool use actually works end-to-end, not just that responses come back:

```bash
# Sonnet + tool use (the most important test)
curl -s http://localhost:11435/v1/messages -H "Content-Type: application/json" \
  -d '{
    "model":"claude-sonnet-4-5","max_tokens":300,
    "messages":[{"role":"user","content":"List files in /tmp. Use the list_dir tool."}],
    "tools":[{"name":"list_dir","description":"List files","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}]
  }' | python3 -m json.tool
```

Expect `content` to contain a `{"type":"tool_use","name":"list_dir","input":{"path":"/tmp"}}` block and `stop_reason: "tool_use"`. **If you get a text block describing what the tool would do, the model failed the integration.**

Then run Claude Code against a real small repo and watch for:
- Dropped tool calls (prose instead of edits)
- Schema errors (wrong param names)
- Infinite read loops

All three are model-quality symptoms, not bugs in the integration. If they happen on qwen3-coder:30b, the model is genuinely struggling — try the 480B tier.

---

## Benchmark sources and honest caveats

### What we know with confidence

- **BFCL V4 is the current function-calling benchmark** (V3 superseded, live as of 2026-04-12) — [gorilla.cs.berkeley.edu/leaderboard.html](https://gorilla.cs.berkeley.edu/leaderboard.html)
- **Qwen3-Coder 30B A3B tops BFCL for coding-specific function calling** per [Galaxy.ai 2026 comparison](https://blog.galaxy.ai/compare/deepseek-v3-2-vs-qwen3-coder-30b-a3b-instruct)
- **Qwen3-Coder 480B A35B is "comparable to Claude Sonnet 4"** per [Qwen's own release blog](https://qwenlm.github.io/blog/qwen3-coder/) — self-reported but consistent with third-party coverage
- **Qwen3 235B A22B hits 25–35 tok/s on M3 Ultra** per [MacStories 2026 benchmark](https://www.macstories.net/notes/notes-on-early-mac-studio-ai-benchmarks-with-qwen3-235b-a22b-and-qwen2-5-vl-72b/)
- **gpt-oss:120b hits 60.7 tok/s on this specific Mac Studio** — from 214k real completed requests in the local trace DB

### What's projection, not measurement

- **qwen3-coder:30b on M3 Ultra** — no public M3 Ultra benchmark found; projecting from M4 Max's 172 tok/s (M3 Ultra has higher bandwidth than M4 Max, so ≥ that number is reasonable). Will be confirmed by trace data once Claude Code usage ramps on this machine.
- **qwen3-coder:480b on M3 Ultra** — projecting 40–55 tok/s from the 22B-active point (Qwen3:235b-a22b at 25–35) and the 37B-active point (DeepSeek-V3 at 20–30). 35B active sits between, and coder-specific optimization may add a few tok/s.

### What's genuinely uncertain

- **DeepSeek-V3.2 non-thinking mode tool-use reliability** in Claude Code — no public evidence of someone running it in anger through Claude Code's agentic loop. The shape is right (MoE, small active); the behavior needs testing.
- **Community consensus on "the" Claude Code model** — no consensus exists. Reddit r/LocalLLaMA leans qwen3-coder:30b for anyone with <100GB memory and qwen3-coder:480b for those with more. GLM-4.6/4.7 gets mentioned but stress-tests [poorly](https://blog.codeminer42.com/claude-code-ollama-stress-testing-opus-4-5-vs-glm-4-7/) in actual Claude Code use despite strong benchmark scores.
- **How long this recommendation holds** — the open-weights landscape moves fast. DeepSeek-V4, Qwen3.5-Coder, Kimi K3 could land and shift the picture by mid-2026. Re-verify before relying on this doc past July 2026.

---

## Summary

### Model choice
1. **Claude Code is tool-use-first.** Optimize for BFCL performance, not general chat.
2. **M3 Ultra rewards MoE with small active-params.** Dense 70B = slow; MoE with 3–35B active = fast.
3. **Haiku and sonnet both map to `qwen3-coder:30b`** — 120+ tok/s, no model-swap cost, same tool behavior.
4. **Opus maps to `qwen3-coder:480b-a35b-q4_K_M`** — 270GB, 40–55 tok/s, SOTA open agentic coding, comparable to Claude Sonnet 4.
5. **Don't map thinking models** (`gpt-oss:120b`, `deepseek-r1:70b`, DeepSeek-V3.2 thinking mode) to Claude tiers. They break agentic loops.

### Context management (equally important)
6. **Prefill, not decode, is the bottleneck** on long Claude Code sessions. Latency grows linearly with context length.
7. **Trim CLAUDE.md** — it's sent every turn. Target under 1,500 tokens; this repo's current 3,022 is 2× too big.
8. **Use `/compact` every 15 turns; `/clear` between distinct tasks.** Zero engineering, biggest behavioral lever.
9. **Prefer `Grep` over `Read`** for exploration. Read dumps 15K tokens; Grep returns 100.
10. **Set `FLEET_DYNAMIC_NUM_CTX=true`** — right-sizes KV cache to actual usage.
11. **Set `FLEET_ANTHROPIC_DEFAULT_MAX_TOKENS=4096`** — caps runaway output.
12. **Prefix caching in herd is the architectural fix** (post-hackathon). vLLM has it; Ollama doesn't; herd could wrap it. 70–90% prefill reduction.

### Hardware-specific
13. **<128GB Macs are haiku-class.** Don't run 30B locally for Claude Code; offload to a Mac Studio via ollama-herd LAN route. Keep only `qwen3:8b` locally for offline use.
14. **The Mac Studio swap (over LAN) is ~4× not 10×** — but 4× crosses the pain threshold from unusable to usable.

### Process
15. **Under 30s per turn is achievable** on every tier with this config *if* context management is also in place.
16. **Validate with real Claude Code sessions**, not just curl. Tool-use failure modes show up in multi-turn loops.

---

## Sources

- [Berkeley Function-Calling Leaderboard (BFCL V4)](https://gorilla.cs.berkeley.edu/leaderboard.html) — live benchmark, updated 2026-04-12
- [Qwen3-Coder release blog](https://qwenlm.github.io/blog/qwen3-coder/) — 480B A35B SOTA claims
- [Artificial Analysis: Qwen3 Coder 480B](https://artificialanalysis.ai/models/qwen3-coder-480b-a35b-instruct) — 60 tok/s cloud throughput, benchmark scores
- [Galaxy.ai: DeepSeek V3.2 vs Qwen3 Coder 30B A3B Instruct](https://blog.galaxy.ai/compare/deepseek-v3-2-vs-qwen3-coder-30b-a3b-instruct) — BFCL positioning, thinking/non-thinking modes
- [MacStories: Early Mac Studio AI Benchmarks with Qwen3-235B-A22B](https://www.macstories.net/notes/notes-on-early-mac-studio-ai-benchmarks-with-qwen3-235b-a22b-and-qwen2-5-vl-72b/) — M3 Ultra 235B benchmark
- [Code Miners: Claude Code + Ollama: Stress Testing Opus 4.5 vs GLM 4.7](https://blog.codeminer42.com/claude-code-ollama-stress-testing-opus-4-5-vs-glm-4-7/) — GLM-4.7 agentic failure modes
- [Ollama: Claude Code integration](https://docs.ollama.com/integrations/claude-code) — official integration guide
- [Ollama: Run Claude Code/Codex with local models (2026)](https://medium.com/@luongnv89/how-to-run-claude-code-codex-with-local-models-via-llamacpp-ollama-lmstudio-and-vllm-2026-7d00ba7e63a4) — Qwen3.5-27B coding recommendations
- [XDA Developers: Claude Code with local LLM via Ollama](https://www.xda-developers.com/claude-code-local-llm-ollama-capable-costs-nothing/) — real-world latency gap
- [Ollama library: qwen3-coder tags](https://ollama.com/library/qwen3-coder) — available variants including 480b-a35b-q4_K_M
- Local trace DB (`~/.fleet-manager/latency.db`) — 214k real completions on this M3 Ultra Mac Studio
- Local session trace on M5 Pro 48GB laptop — turn-by-turn prefill/decode measurement showing linear latency growth
- ollama-herd internal docs: [`docs/plans/dynamic-num-ctx.md`](../plans/dynamic-num-ctx.md), [`docs/guides/claude-code-integration.md`](../guides/claude-code-integration.md), `CLAUDE.md` operational notes
- CLAUDE.md token measurement: 3,022 tokens (cl100k_base tokenizer, measured 2026-04-22)
