# Pin button should hide for MLX-hosted LLMs on the Recommendations page

**Status:** Open
**Severity:** Low (UI correctness — no models affected today)
**Filed:** 2026-04-23

## Problem

The per-node pin button on the Recommendations page (dashboard) only
makes sense for models that the **Ollama preloader** can act on.  Pin
state is persisted to `<data_dir>/pinned_models.json` and consumed
exclusively by `src/fleet_manager/server/model_preloader.py`, which
calls `/api/ps` against each node's Ollama and issues `pre_warm` via
Ollama's load path.  If a model isn't served by Ollama on a given node,
a pin is a lie: the preloader will log "not on disk anywhere —
skipping" and the user's "keep this hot" intent is silently ignored.

We already handle this for the `vision-embedding` category (DINOv2,
SigLIP, CLIP — served by `node/embedding_server.py` on `:11438`, not
Ollama).  The pin button is hidden on those cards and the POST endpoint
rejects pin attempts with a 400.

The same principle applies to **MLX-hosted LLMs** — models routed via
the `mlx:` prefix in `FLEET_ANTHROPIC_MODEL_MAP`.  Those flow through
`mlx_lm.server` managed by `node/mlx_supervisor.py`, not Ollama.  Pins
on `mlx:` models would be equally meaningless.

## Why it's not a live bug today

MLX-prefixed models don't appear on the Recommendations page.  The
page is driven by `MODEL_CATALOG` in
`src/fleet_manager/server/model_knowledge.py`, which contains only
Ollama-named models.  There's no catalog entry whose `ollama_name`
starts with `mlx:`, so no recommendation card with an MLX model can
be rendered, so no one can click a misleading pin.

## What would trigger this

Any of these changes would surface the issue:

1. Adding MLX variants to `MODEL_CATALOG` (e.g. an
   `mlx:mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit` entry so
   the recommender can suggest MLX for nodes with MLX enabled).
2. Auto-registering node-reported MLX models (from `mlx_client.py`'s
   `/v1/models` poll) as synthetic catalog entries for the
   recommendation view.
3. A future "hot fleet health" view that lists all served models
   regardless of backend and offers a unified pin control.

## Proposed fix (when the time comes)

Generalise the category-based skip to a **backend-based** skip.  The
pin button should only render when the model is (or could be) Ollama-
hosted on the target node.  Concretely:

- Add a `backend` hint to `ModelRecommendation` — `"ollama" |
  "mlx" | "embedding-service"`.  For today's catalog it's always
  `"ollama"`; for vision-embedding it's `"embedding-service"`;
  MLX-prefixed entries would be `"mlx"`.
- Dashboard renders the pin button only when `rec.backend === 'ollama'`
  and the node actually runs Ollama.
- `POST /dashboard/api/pinned-models` validates the same way: reject
  pins whose target model isn't Ollama-backed on that node.
- Either extend the pin store to carry a backend tag, or add a
  separate "mlx-pin" concept if we ever want to reserve a model in
  `mlx_lm.server`'s single-slot.  The current MLX supervisor serves
  whatever model was spawned — there's nothing to "keep hot" separately.

## Related

- See `src/fleet_manager/server/routes/dashboard.py` — the
  `isVisionEmbedding` branch in the Recommendations card render and
  the VISION_EMBEDDING check in `POST /dashboard/api/pinned-models`.
- See `docs/plans/mlx-backend-for-large-models.md` for the MLX
  architecture.
- See `src/fleet_manager/server/model_preloader.py` for what a pin
  actually controls today.

## Source conversation

Raised 2026-04-23 while adding per-node pin UI.  User observation:
"if [DINOv2] can be run via mlx [instead of] ollama, [the pin is]
misleading; it's not misleading if mlx [is] not installed or on a
device that doesn't support mlx."  The same principle should apply to
MLX-hosted LLMs the moment they show up on the Recommendations page.
