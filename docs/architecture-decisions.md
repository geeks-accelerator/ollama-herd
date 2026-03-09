# Architecture Decisions

Key design choices made during development, with context on alternatives considered and trade-offs.

---

## Port Selection: 11435

**Decision:** Use port `11435` as the default router port.

**History:**

| Port | Rationale | Outcome |
|------|-----------|---------|
| `8080` | Common HTTP alternative port | Rejected — too many conflicts (dev servers, proxies, other tools) |
| `4373` | "HERD" on phone keypad (4-3-7-3) | Memorable but didn't signal the Ollama relationship |
| `11435` | Ollama is 11434, Herd is 11434+1 | Adopted — intuitive, signals "the thing on top of Ollama" |

The proximity to Ollama's `11434` makes it immediately obvious what Herd does: it sits right next to Ollama. Users remember "Ollama plus one."

---

## Dynamic Queue Concurrency (Not Static Config)

**Decision:** Automatically calculate worker count per `node:model` queue based on available memory, rather than exposing a `FLEET_QUEUE_CONCURRENCY` env var.

**Formula:**

```
headroom = available_memory_gb - model_size_gb
concurrency = headroom / 2.0   (2 GB estimated KV cache per request)
clamped to [1, 8]
```

**Why not a config var?**
- Different nodes have wildly different memory (16GB laptop vs 512GB studio)
- The same node's available memory changes over time (other apps, loaded models)
- A static config would either over-commit small machines or under-utilize large ones
- The 2GB-per-request KV cache estimate is conservative enough for most models

**Trade-off:** The `2GB` constant (`_KV_CACHE_PER_REQUEST_GB`) is a rough estimate. Large-context models or very long conversations may use more KV cache per request. Future work could make this model-aware.

**Example outcomes:**

| Machine | RAM | Model | Available | Concurrency |
|---------|-----|-------|-----------|-------------|
| Mac Studio | 512GB | llama3.3:70b (40GB) | ~350GB | 8 (clamped max) |
| MacBook Air | 16GB | qwen2.5:7b (5GB) | ~5GB | 2 |
| MacBook Pro | 128GB | qwen3:235b (130GB) | ~0GB | 1 (clamped min) |

Concurrency is recalculated on every enqueue, so it adapts as memory conditions change.

---

## Three-Source Tag Merging

**Decision:** Accept tags from three sources (`metadata.tags`, `X-Herd-Tags` header, `user` field), merge and deduplicate.

**Why three sources?**
- **`metadata.tags` in body** — the primary method, follows LiteLLM's convention. Works with any HTTP client.
- **`X-Herd-Tags` header** — for clients that can't modify the request body (reverse proxies, middleware, load balancers, piped commands).
- **`user` field** — already standard in OpenAI format. Captured as `user:<value>` tag so user identity shows up in the same analytics without a separate dimension.

**Why not just one?** Different integration patterns have different constraints. A coding assistant using the OpenAI SDK can easily add `extra_body`, but a reverse proxy in front of multiple apps can only add headers. Supporting all three means zero friction for any integration pattern.

**Stripping before proxy:** The `metadata` and `fallback_models` fields are removed from the request body before forwarding to Ollama. Ollama doesn't understand these fields, and while it currently ignores unknown fields, this prevents any future compatibility issues.

---

## SQLite for Everything (No External DB)

**Decision:** Use a single SQLite database (`~/.fleet-manager/latency.db`) for latency history, request traces, usage stats, and tag analytics.

**Why SQLite?**
- Zero setup — no database server to install or configure
- Single file — easy to backup, move, or inspect
- WAL mode — concurrent reads/writes without blocking
- `json_each()` — native JSON array explosion for tag queries
- Good enough for the expected scale (thousands to tens of thousands of requests per day)

**When to reconsider:** If a deployment exceeds ~100K requests/day or needs distributed query capability, consider migrating to PostgreSQL. The `json_each()` queries for tag analytics would translate directly to PostgreSQL's `jsonb_array_elements_text()`.

---

## Ollama Body Passthrough vs. Normalization

**Decision:** For Ollama-format requests (`/api/chat`, `/api/generate`), pass the raw request body through to Ollama (after stripping Herd-specific fields). For OpenAI-format requests, build the Ollama body from scratch.

**Why?**
- Ollama has many model-specific options (`num_ctx`, `num_gpu`, `repeat_penalty`, etc.) that Herd doesn't need to know about
- Passing through means Herd automatically supports any new Ollama options without code changes
- OpenAI format requires conversion anyway (different message format, different parameter names)

**What gets stripped from Ollama passthrough:**
- `fallback_models` — Herd-specific routing field
- `metadata` — Herd-specific tagging field
- `user` — captured as tag, not passed to Ollama

---

## Scoring Engine: 7 Signals with Fixed Weights

**Decision:** Use 7 weighted signals with fixed point values rather than a configurable or ML-based scoring system.

| Signal | Max Points | Purpose |
|--------|-----------|---------|
| Thermal state | +50 | Prefer hot models (already loaded) |
| Memory fit | +20 | Prefer nodes with more headroom |
| Queue depth | −30 | Penalize busy nodes |
| Latency history | −25 | Penalize historically slow nodes |
| Role affinity | +15 | Match model size to machine capability |
| Availability trend | +10 | Prefer nodes that are freeing up (capacity learning) |
| Context fit | ±15 | Prefer nodes with context windows fitting the input |

**Why fixed weights?**
- Predictable — operators can reason about why a node was chosen
- Debuggable — the `scores_breakdown` in traces shows exactly what happened
- Thermal state dominates by design — avoiding cold model loads (10-60s) is the #1 performance win

**Trade-off:** Fixed weights don't adapt to specific workload patterns. A future version could learn optimal weights per deployment, but the current system works well across diverse fleet configurations.

---

## Context Window Awareness (Signal 7)

**Decision:** Add a scoring signal that prefers nodes with larger context windows for token-heavy requests.

**Why?**
- Models with limited context windows (4K–8K) risk truncating large prompts
- Some models have 32K–128K context variants — the router can route long inputs to better-equipped nodes
- Token estimation is rough (~4 chars/token heuristic), so this is a soft scoring signal, not a hard gate

**Design:** Returns +15 max when input tokens have plenty of headroom, −15 when they exceed the window. Only applies to loaded (hot) models with known `context_length` — cold models return 0 (neutral), keeping "avoid cold loads" as the #1 priority.

**Overflow handling:** When estimated tokens exceed the context window on the winning node, the response includes an `X-Fleet-Context-Overflow` header. The router still serves the request — Ollama handles truncation.

---

## Auto-Pull: Seamless Model Acquisition

**Decision:** When a requested model doesn't exist on any fleet node, automatically pull it onto the best available node rather than returning a 404.

**Why?**
- Eliminates the most common setup friction ("Run `ollama pull <model>` first")
- Small models download in under a minute — seamless for the client
- The router already knows each node's Ollama URL and available memory
- Enabled by default — aligns with the zero-config design principle

**Node selection:** Pick the online node with the most available memory that can fit the estimated model size. Respects capacity ceilings, memory pressure, and paused state.

**Deduplication:** A module-level `_pulls_in_flight` set prevents concurrent pulls of the same model. A second request for the same model waits for the in-flight pull to complete, then retries scoring.

**Trade-off:** The client waits for the download (up to `FLEET_AUTO_PULL_TIMEOUT`, default 5 minutes). For large models this can be slow, but the alternative — returning an error and requiring manual intervention — is worse for the zero-config use case. Users who prefer explicit control can set `FLEET_AUTO_PULL=false`.

---

## Capacity Learning: 168-Slot Weekly Model

**Decision:** Use a 168-slot model (7 days × 24 hours) to learn device availability patterns.

**Why weekly?**
- Human computer usage is strongly weekly-periodic (weekday work patterns vs. weekend patterns)
- 168 slots is small enough to converge quickly (7 days to fill all slots)
- Each slot stores multiple observations, so it can detect variations within the same hour across weeks

**Availability score formula:**

```
score = (historical_baseline × 0.4) + (current_state × 0.4) + (trend × 0.2)
```

- 40% historical — what usually happens at this time on this day
- 40% current — what's happening right now (CPU, memory, app workload)
- 20% trend — is the machine getting busier or freeing up

**Bootstrap period:** The first 7 days are a "bootstrapping" phase where the learner has low confidence. After accumulating a full week of observations, it graduates to "learned" mode with higher confidence.

**Hard overrides:** Certain conditions bypass the learned model entirely:
- Camera/mic active → hard pause (meeting detection)
- Sustained high CPU → reduced availability
- Memory pressure critical → no new work
- Thermal throttling → reduced availability

---

## LAN Proxy: Automatic Ollama Network Bridging

**Decision:** Auto-start a TCP reverse proxy on the node's LAN IP when Ollama is only listening on localhost, rather than requiring users to manually set `OLLAMA_HOST=0.0.0.0`.

**Why a proxy?**
- Zero-config — users shouldn't need to know about `OLLAMA_HOST` to get multi-device routing working
- Non-invasive — doesn't modify Ollama's configuration or restart it
- Transparent — the proxy is a simple byte-pipe, no protocol awareness needed
- Automatic detection — the agent checks if Ollama is already LAN-reachable and skips the proxy if so

**Why not just set `OLLAMA_HOST=0.0.0.0` when auto-starting Ollama?**
The agent does set `OLLAMA_HOST=0.0.0.0` when it starts Ollama itself. But if Ollama is already running (the common case — started by the system or user), we can't change its bind address without restarting it. The proxy handles this case without disruption.

**Trade-off:** Adds one extra hop for LAN traffic. In practice, the TCP proxy on the same machine adds negligible latency (<1ms) since it's just piping bytes between two local sockets.

---

## Dashboard: Inline HTML in Python

**Decision:** Embed all dashboard HTML/CSS/JS directly in Python string constants in `routes/dashboard.py`.

**Why?**
- Zero build step — no webpack, no npm, no frontend toolchain
- Single-file deployment — everything is in the Python package
- Chart.js via CDN — the only external dependency
- Pragmatic for the current scope (5 pages)

**When to extract:** When the dashboard grows beyond ~6-7 pages or needs interactive features like tag-based filtering on the Trends/Models views, it should be extracted to either Jinja2 templates or a separate React/Vue frontend with its own build.

---

## Drop-in Replacement Philosophy

**Decision:** Make Herd a transparent, zero-config proxy that works with any existing Ollama or OpenAI client by just changing the URL.

**Implications:**
- Same port neighborhood (11435 vs 11434)
- Same API surface — `/v1/chat/completions`, `/api/chat`, `/api/generate`, `/api/tags`, `/api/ps`
- Same request/response formats — no proprietary extensions required
- Extra fields (`fallback_models`, `metadata.tags`) are optional additions, not requirements
- Works with every framework that supports `base_url` or `OLLAMA_HOST`

**Tested compatible with:** OpenClaw, LangChain, CrewAI, AutoGen, LlamaIndex, Haystack, smolagents, OpenHands, Aider, Cline, Continue.dev, Bolt.diy, and any OpenAI-compatible client.
