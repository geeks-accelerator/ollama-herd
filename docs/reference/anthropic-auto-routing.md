# Anthropic auto-routing — the model map is optional

Claude Code (and any anthropic-SDK client) sends model ids like
`claude-sonnet-4-5`. The herd has to turn that into a local model. Historically
you had to hand-write `FLEET_ANTHROPIC_MODEL_MAP` translating every Claude id to
a specific local model — which meant the map had to match exactly what you'd
pulled, and an entry pointing at a model you *hadn't* pulled was a guaranteed
404.

As of 0.9.0 the map is **optional**. A `claude-*` id with no explicit mapping is
resolved to the **best currently-loaded local model** for its tier.

## Resolution order

For each incoming `claude-*` id, in order (first match wins):

1. **Explicit `anthropic_model_map` entry** for that exact id → used. *(This is
   the override — per-alias pinning still works, it's just no longer required.)*
2. **A real local model name** sent straight through (e.g. a client that sends
   `"model": "qwen3-coder:30b"`) → passed through unchanged.
3. **Best loaded model** for the tier, across all online nodes → used. *(No cold
   load — it's already resident.)*
4. **Best on-disk model** for the tier → used, accepting a cold load.
5. **`anthropic_model_map["default"]`** if you set one → used.
6. Nothing serviceable anywhere → `404` telling you to pull a model or set the
   map.

Steps 3–4 are skipped when `FLEET_ANTHROPIC_AUTO_ROUTE=false`, which restores
the pre-0.9 behaviour (explicit map or bust).

## How "best" is chosen

Tiers are inferred from the Claude id — `haiku` → fast, `opus` → premium,
`sonnet`/anything else → balanced. Within a tier, candidates are ranked by
benchmark quality (from the model catalog) plus a category bias suited to Claude
Code's agentic-coding workload:

- **Coding** models are preferred, then **general** chat, then **reasoning**
  (capable, but their thinking tokens slow agentic loops), with **vision**
  models last for text.
- **Embedding and image models are never candidates** — an embedding model that
  classifies as "general" is explicitly excluded, so a chat turn can't route
  into `nomic-embed-text`.
- The **premium** (opus) tier leans toward larger/higher-quality models; the
  **fast** (haiku) tier biases toward smaller/faster ones. The **balanced**
  (sonnet) tier optimises quality without a size preference.
- Models not in the catalog are still eligible — they're classified by name
  (e.g. anything with `coder` → coding) and given a mid-scale quality, so a
  known-good model outranks a mystery one but a mystery one still beats routing
  to nothing.

**Image requests** filter candidates to vision-capable models automatically. An
explicit `FLEET_ANTHROPIC_VISION_MODEL` still overrides everything for image
requests, as before.

## Configuration

| Var | Default | Effect |
|-----|---------|--------|
| `FLEET_ANTHROPIC_MODEL_MAP` | `{}` | Optional per-alias overrides. Entries win over auto-routing. A `default` key is the last-resort catch-all. |
| `FLEET_ANTHROPIC_AUTO_ROUTE` | `true` | When a `claude-*` id isn't explicitly mapped, resolve to the best loaded/on-disk model. Set `false` to require an explicit map. |
| `FLEET_ANTHROPIC_VISION_MODEL` | `""` | Hard override for image requests. |

### Examples

**Zero config** (recommended): pull a coding model and point Claude Code at the
herd. Everything auto-routes.

```bash
ollama pull qwen3-coder:30b
export ANTHROPIC_BASE_URL=http://localhost:11435
claude
```

**Pin one alias, auto-route the rest:**

```bash
# opus → a specific big model you keep hot; sonnet/haiku still auto-route.
export FLEET_ANTHROPIC_MODEL_MAP='{"claude-opus-4-7": "qwen3:32b"}'
```

**Old strict behaviour** (explicit map required, no auto):

```bash
export FLEET_ANTHROPIC_AUTO_ROUTE=false
export FLEET_ANTHROPIC_MODEL_MAP='{"default":"qwen3-coder:30b","claude-haiku-4-5":"qwen3:14b"}'
```

## Observability

Each resolution logs one line at INFO:

```
Anthropic resolved claude-sonnet-4-5 → qwen3-coder:30b (auto-loaded)
```

The reason tag (`explicit-map`, `passthrough`, `auto-loaded`, `auto-ondisk`,
`default`, `unresolved`) tells you which rule fired. The resolver itself lives in
`server/anthropic_autoroute.py` and is pure — see
`tests/test_server/test_anthropic_autoroute.py` for the full behaviour matrix.

## Implementation

- `server/anthropic_autoroute.py` — the pure resolver (`resolve_model`,
  `rank_candidates`).
- `server/routes/routing.py::get_fleet_loaded_and_ondisk` — gathers the
  loaded/on-disk names (including healthy `mlx:` servers) from the registry.
- `server/routes/anthropic_compat.py` — calls the resolver at request time.
- `server/model_knowledge.py` — the catalog that supplies category + quality.
