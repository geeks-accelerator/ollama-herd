# Context-Size Protection

**Status**: Implemented
**Date**: March 2026
**Config**: `FLEET_CONTEXT_PROTECTION=strip` (default)

## Problem

When a client sends `num_ctx` in Ollama request options that differs from the loaded model's context window, Ollama's scheduler calls `needsReload()` and triggers a full model unload+reload. For large models (89GB `gpt-oss:120b`), this causes multi-minute hangs or complete deadlocks — 0 bytes returned indefinitely.

Reproduced directly against Ollama (bypassing Herd): `num_ctx: 4096` on a model loaded at 32768 hangs forever. Without `num_ctx`, same request completes in 3 seconds.

Root causes compound: GPT-OSS minimum context override (Ollama bumps `num_ctx < 8192` to 8192), runner startup timeout exceeded during 89GB reload, and KV cache fill loop on small context values. Related Ollama issues: #9749, #11711, #3583, #13461.

## Solution

The router intercepts `num_ctx` in `_build_ollama_body()` (the last transform before the request goes to Ollama) and applies context protection:

### When `num_ctx` <= loaded context (strip)

The model already supports that context window. Strip `num_ctx` from the request to prevent the unnecessary reload. Log the action.

### When `num_ctx` > loaded context (upgrade or warn)

The client genuinely needs more context. Search all loaded models across the fleet for one with:
1. Sufficient context (`context_length >= num_ctx`)
2. More parameters (larger `size_gb` — bigger model)

If found: switch the model name in the request body, strip `num_ctx`, and log the upgrade. If not found: preserve `num_ctx` and log a warning.

### Configuration

Three modes via `FLEET_CONTEXT_PROTECTION` env var:
- **`strip`** (default): Strip `num_ctx` when safe, upgrade when possible
- **`warn`**: Log warnings but don't modify requests
- **`passthrough`**: No intervention

## Implementation

### Files modified

| File | Change |
|------|--------|
| `models/config.py` | Added `context_protection: str = "strip"` to `ServerSettings` |
| `server/app.py` | Pass `settings` to `StreamingProxy` constructor |
| `server/streaming.py` | Added `_get_loaded_context()`, `_find_context_upgrade()`, `_apply_context_protection()`. Changed `_build_ollama_body()` signature to accept `node_id`. |
| `server/routes/dashboard.py` | Added `context_protection` to settings API and UI |
| `docs/configuration-reference.md` | Documented `FLEET_CONTEXT_PROTECTION` |
| `docs/operations-guide.md` | Added "Context-Size Protection" section |
| `docs/troubleshooting.md` | Added num_ctx hang diagnosis section |
| `tests/test_server/test_streaming.py` | 9 new tests (strips small, strips equal, keeps larger, upgrade switches model, upgrade no suitable model, passthrough, warn, no num_ctx, unknown model) |

### Key design decisions

1. **Intercept at `_build_ollama_body()`, not at routing time**: Context protection is a proxy-layer concern (what we send to Ollama), not a routing concern (where we send it). The `StreamingProxy` already has `self._registry` for looking up loaded models.

2. **Search all nodes for upgrades, not just the assigned node**: A bigger model with sufficient context might be loaded on a different node. The search checks the assigned node first, then all others.

3. **Prefer the smallest adequate upgrade**: When multiple models qualify (sufficient context + more params), pick the smallest one that's still bigger than the current model. Avoids unnecessarily jumping to the largest model.

4. **Only applies to Ollama-format requests**: OpenAI-format requests don't have a `num_ctx` equivalent. The `_build_ollama_body()` method builds OpenAI bodies from scratch and never includes context-size parameters.
