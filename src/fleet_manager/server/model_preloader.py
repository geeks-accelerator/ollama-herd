"""Priority Model Preloader — keeps the right models warm without thrashing.

Two concerns:

1.  **Pinned models** — user-declared "always keep this hot."  Configured
    via FLEET_PINNED_MODELS (comma-separated).  Loaded first on startup
    and actively reloaded if evicted.  Example: ``gpt-oss:120b,gemma3:27b``
    for projects that depend on them across sessions.

2.  **Priority models** — scored by 24h/7d request frequency.  After
    pinned models are loaded, the preloader fills remaining slots up to
    FLEET_MODEL_PRELOAD_MAX_COUNT (default 3, matches Ollama's hardcoded
    hot cap).

Critical invariant: **don't load more models than the backend can hold
concurrently.**  Ollama 0.20.4 on macOS has a hardcoded 3-model cap.
Historically the preloader ignored this and blindly pre-warmed 10+
models based on usage scoring, which caused each new load to evict an
older one — thrashing the LRU and kicking out whatever was loaded
before the restart (including pinned models).  2026-04-23 observation:
restart of the router caused gpt-oss:120b eviction because the
preloader pre-warmed 6+ models past the 3-slot cap.

Design:
  - Startup: query ``/api/ps``, load pinned models first, fill remaining
    slots up to max_count.  Never exceed max_count total loads.
  - Refresh (every 10 min): check pinned models, reload any evicted;
    separately check top N priority models with recent activity.
  - Disable: FLEET_DISABLE_MODEL_PRELOADER=true → no-op, models load
    on demand on first request.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.mlx_proxy import is_mlx_model, strip_mlx_prefix
from fleet_manager.server.model_knowledge import lookup_model
from fleet_manager.server.pinned_models import PinnedModelsStore
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.streaming import StreamingProxy
from fleet_manager.server.trace_store import TraceStore

# Pinned-model load failures, for the `pin_cannot_fit` health check.
#
# A pin the fleet can physically never satisfy retries forever: the refresh
# cycle notices the model isn't resident, tries to load it, the memory gate
# refuses, and ten minutes later it does the whole thing again.  Observed
# 2026-07-19 — `llama4:maverick` (needs ~294GB) looped 56 times over 9.5 hours,
# entirely at INFO, so it never surfaced in any error scan.  It was found only
# by tracing what had evicted three unrelated models.
#
# `POST /fleet/pin` refuses impossible pins with a 409 since 0.8.2, but that
# guards the *entry* point only: a pin made before that check existed, forced
# with `"force": true`, or made when memory was free and later starved by other
# residents, still ends up here.  A backstop at the door is not a substitute for
# noticing that the instruction keeps failing.
_pin_fit_failures: list[dict] = []


def _record_pin_fit_failure(
    model: str, node_id: str, needed_gb: float, available_gb: float
) -> None:
    _pin_fit_failures.append({
        "timestamp": time.time(),
        "model": model,
        "node_id": node_id,
        "needed_gb": needed_gb,
        "available_gb": available_gb,
    })
    if len(_pin_fit_failures) > 200:
        _pin_fit_failures.pop(0)


def get_pin_fit_failures(hours: float = 24) -> list[dict]:
    """Pinned-model load failures in the last N hours."""
    cutoff = time.time() - (hours * 3600)
    return [e for e in _pin_fit_failures if e["timestamp"] >= cutoff]


logger = logging.getLogger(__name__)

# Cache priority scores to avoid repeated DB queries
_priority_cache: list[dict] = []
_priority_cache_time: float = 0
_CACHE_TTL = 300  # 5 minutes

# Seconds between registry residency polls in _wait_until_resident.  The
# limiting factor is heartbeat_interval (~5s) — models_loaded only refreshes
# then — so a sub-second interval would just spin.
_RESIDENCY_POLL_INTERVAL = 1.0

# Assumed size for a model whose size we genuinely cannot determine (node
# reports none, not in the catalog, name carries no parameter count).  Chosen
# to be larger than anything we'd casually auto-preload: the failure mode of
# guessing too LOW is evicting the whole fleet for a model that doesn't fit,
# while guessing too HIGH just declines to preload and logs why.
_UNKNOWN_MODEL_SIZE_GB = 100.0

# Resident-cost multiplier over on-disk weights, used ONLY when we have no
# measured KV cost for a model.  It is a poor approximation and we know it —
# measured ratios span 1.0x (gpt-oss:120b) to 6.6x (qwen3-coder:30b at 262K
# context).  See _observe_kv_cost / estimate_resident_gb, and
# docs/issues/model-sizing-ignores-kv-cache.md.
_RESIDENT_OVERHEAD = 1.2

# Learned KV-cache cost per model, in MB per context token:
#   {model_name: mb_per_token}
# Populated from heartbeat data — every LoadedModel reports BOTH its real
# resident size_gb AND its context_length, so each loaded model is a free
# measurement of (resident - weights) / context_length.  Measured on the fleet
# 2026-07-17: qwen3-coder:30b = 0.387 MB/tok @32K and 0.407 MB/tok @262K —
# linear to ~5%, so one observation predicts any context.
_kv_cost_mb_per_token: dict[str, float] = {}

# Models whose learned cost we've already logged, so the discovery is auditable
# without spamming a line per heartbeat.
_kv_cost_logged: set[str] = set()

# Last context window each model was actually loaded with:
#   {model_name: context_length}
# When we DON'T override num_ctx, the model gets its own default — which we
# can't read from the manifest but can simply remember from the last time it
# was resident.  qwen3-coder:30b defaults to 262144, and that default is the
# entire difference between a 31GB model and a 122GB one.
_observed_ctx: dict[str, int] = {}

# Below this, treat a measured KV cost as noise rather than signal (rounding in
# the reported sizes can make a genuinely-tiny KV look like a small negative).
_KV_COST_MIN_MB_PER_TOKEN = 0.0


def _num(value) -> float:
    """Coerce a reported field to a float, or 0.0 if it isn't a number.

    ``_observe_kv_cost`` reads heartbeat-supplied attributes off arbitrary
    objects and runs inside routing decisions.  A malformed or absent field
    should make us decline to learn, never raise into a routing path.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _observe_kv_cost(node) -> None:
    """Learn each loaded model's KV cost per token from what ``node`` reports.

    ``LoadedModel`` already carries the real resident ``size_gb`` and the
    allocated ``context_length``; combined with the on-disk weight size from
    ``/api/tags`` that gives us::

        kv_per_token = (resident_gb - weights_gb) / context_length

    We were throwing this away and multiplying weights by a constant instead —
    which under-estimated qwen3-coder:30b by 5.4x (22.8GB assumed vs 122.9GB
    real) and twice let a load through that kernel-panicked the box.

    Cheap and idempotent: call it wherever a node is in hand.
    """
    ollama = getattr(node, "ollama", None)
    if not ollama:
        return
    sizes = getattr(ollama, "models_available_sizes", None)
    weights_of = sizes.get if isinstance(sizes, dict) else lambda _n, _d=0: 0
    for m in getattr(ollama, "models_loaded", None) or []:
        ctx = int(_num(getattr(m, "context_length", 0)))
        resident = _num(getattr(m, "size_gb", 0))
        weight = _num(weights_of(getattr(m, "name", None), 0))
        if ctx > 0:
            # Worth remembering even if we can't derive a KV cost below: it's
            # the context this model gets when nobody overrides it, and that
            # default is the whole difference between 31GB and 122GB.
            _observed_ctx[m.name] = ctx
        if ctx <= 0 or resident <= 0 or weight <= 0:
            continue  # can't derive without all three
        kv_gb = resident - weight
        if kv_gb < 0:
            continue  # reported resident below weights — don't learn nonsense
        mb_per_token = (kv_gb * 1024.0) / ctx
        if mb_per_token < _KV_COST_MIN_MB_PER_TOKEN:
            continue
        _kv_cost_mb_per_token[m.name] = mb_per_token
        if m.name not in _kv_cost_logged:
            _kv_cost_logged.add(m.name)
            logger.info(
                f"Learned KV cost for {m.name}: {mb_per_token:.3f} MB/token "
                f"({weight:.1f}GB weights + {kv_gb:.1f}GB KV = {resident:.1f}GB "
                f"resident @ {ctx} ctx). Resident/weights ratio "
                f"{resident / weight:.1f}x."
            )


def measured_resident_gb(
    model: str, node, num_ctx: int | None = None, weights_gb: float | None = None
) -> float | None:
    """Resident cost of ``model`` on ``node`` — or None when we'd be guessing.

    A model's resident footprint is ``weights + KV cache``, and the KV cache
    scales with the context window; it can dwarf the weights (qwen3-coder:30b:
    18.6GB of weights, 104GB of KV at 262K context). Sizing by weights alone is
    what let a "19GB" model consume 122GB and starve the box into a kernel
    panic (2026-07-17).

    Answers only from evidence, in ground-truth-first order:

    1. **It's already loaded** → its reported resident size, KV and all.
    2. **We've measured its KV cost** → ``weights + kv_per_token * num_ctx``.
    3. **Neither** → None. Callers decide what to do without evidence; this
       function does not manufacture it.

    ``num_ctx`` should be the context the model will actually run with (see
    ``_expected_num_ctx``). ``weights_gb`` overrides the on-disk size lookup for
    callers that have a better one — the scorer can ask peer nodes.
    """
    _observe_kv_cost(node)

    # 1. Loaded → we know exactly, including whatever KV it actually allocated.
    for m in getattr(getattr(node, "ollama", None), "models_loaded", None) or []:
        if getattr(m, "name", None) == model and _num(getattr(m, "size_gb", 0)) > 0:
            return _num(m.size_gb)

    # 2. Measured KV cost + the context we intend to use.
    kv_mb = _kv_cost_mb_per_token.get(model)
    if kv_mb is not None and num_ctx and num_ctx > 0:
        weights = _estimate_model_size(model, node) if weights_gb is None else weights_gb
        return weights + (kv_mb * num_ctx) / 1024.0

    return None


def estimate_resident_gb(
    model: str, node, num_ctx: int | None = None, weights_gb: float | None = None
) -> float:
    """``measured_resident_gb``, falling back to the old ``weights * 1.2``.

    For callers that need a number rather than an admission of ignorance. The
    fallback is a poor approximation and known to be — measured ratios span
    1.0x to 6.6x — but it is the *previous* behaviour, so an un-observed model
    is sized exactly as it was before this function existed. No regression.
    """
    measured = measured_resident_gb(model, node, num_ctx, weights_gb)
    if measured is not None:
        return measured
    weights = _estimate_model_size(model, node) if weights_gb is None else weights_gb
    return weights * _RESIDENT_OVERHEAD


def _num_ctx_override(model: str, settings) -> int | None:
    """The num_ctx the router will SEND for ``model``, or None if it sends none.

    Mirrors ``StreamingProxy._apply_context_protection`` deliberately: an
    override only applies when dynamic num_ctx is on and the model has one.  If
    these two ever disagree the preloader warms a model at one size and the
    first request reloads it at another.
    """
    if not settings or not getattr(settings, "dynamic_num_ctx", False):
        return None
    overrides = getattr(settings, "num_ctx_overrides", None)
    if not isinstance(overrides, dict):
        return None
    override = int(_num(overrides.get(model, 0)))
    return override if override > 0 else None


def _expected_num_ctx(model: str, settings) -> int | None:
    """The context ``model`` will actually RUN with, override or not.

    An override is authoritative — we're about to send it.  Otherwise the model
    gets its own default, which we can't read from the manifest but have
    probably watched it use before.  That default is not a detail:
    qwen3-coder:30b defaults to 262144 and costs 122GB, versus 31GB at 32K.
    """
    return _num_ctx_override(model, settings) or _observed_ctx.get(model)


async def get_cached_priorities(trace_store: TraceStore) -> list[dict]:
    """Get priority scores, cached for 5 minutes."""
    global _priority_cache, _priority_cache_time
    if time.time() - _priority_cache_time < _CACHE_TTL and _priority_cache:
        return _priority_cache
    _priority_cache = await trace_store.get_model_priority_scores()
    _priority_cache_time = time.time()
    return _priority_cache


def get_model_priority(model: str, priorities: list[dict]) -> float:
    """Look up the priority score for a model name."""
    for entry in priorities:
        if entry["model"] == model:
            return entry["priority_score"]
    return 0.0


def _model_size_from_node(model: str, node) -> float | None:
    """Real size in GB for ``model`` on ``node``, or None if unknown.

    Prefers ground truth over guessing, in order:
      1. ``models_available_sizes`` — the on-disk size straight from
         Ollama's ``/api/tags`` (works even when the model isn't loaded,
         which is exactly the preloader's case).
      2. ``models_loaded[].size_gb`` — resident size, if it's already up.

    Returns None for older node agents that don't report sizes, so the caller
    can fall back to the name heuristic.
    """
    if node is None:
        return None
    ollama = getattr(node, "ollama", None)
    if not ollama:
        return None
    sizes = getattr(ollama, "models_available_sizes", None) or {}
    real = sizes.get(model)
    if isinstance(real, (int, float)) and real > 0:
        return float(real)
    for m in getattr(ollama, "models_loaded", None) or []:
        if m.name == model and m.size_gb:
            return float(m.size_gb)
    return None


def _estimate_model_size(model: str, node=None) -> float:
    """Model RAM in GB — real size when the node reports one, else a guess.

    **Always pass ``node`` when you have one.** The name heuristic below cannot
    know the size of a model whose name carries no parameter count
    (``llama4:maverick``, ``MichelRosselli/GLM-4.6:Q4_K_M``) and silently
    returned a 10 GB "conservative default" for them.  That default was not
    conservative — it was catastrophic: ``qwen3-coder:480b-a35b-q4_K_M`` is
    **290 GB** on disk and estimated at 10 GB, so the memory gate computed
    "need 12 GB, have 355 GB → load it" and Ollama evicted the entire fleet to
    make room.  A pinned model doing that on every preloader cycle produced a
    ~300 GB disk→memory thrash loop (2026-07-17; see docs/issues.md).
    """
    # 1. Ground truth from the node (on-disk via /api/tags, or resident size).
    real = _model_size_from_node(model, node)
    if real is not None:
        return real

    # 2. Curated catalog.
    spec = lookup_model(model)
    if spec:
        return spec.ram_gb

    # 3. Name heuristic — last resort. Ordered biggest-first so that a name
    #    like "480b-a35b" matches 480b, not the "35b" hiding inside it.
    lower = model.lower()
    if "embed" in lower or "nomic" in lower:
        return 0.5
    for token, gb in (
        ("671b", 400.0), ("480b", 290.0), ("405b", 230.0), ("235b", 140.0),
        ("122b", 75.0), ("120b", 72.0), ("70b", 45.0), ("72b", 45.0),
        ("32b", 20.0), ("30b", 19.0), ("27b", 19.0), ("22b", 14.0),
        ("14b", 10.0), ("13b", 10.0), ("8b", 5.0), ("7b", 5.0),
        ("4b", 3.0), ("3b", 3.0), ("1b", 1.0), ("0.6b", 1.0), ("0.5b", 1.0),
    ):
        if token in lower:
            return gb

    # 4. Unknown. Deliberately pessimistic: an under-estimate lets an
    #    unloadable model past the memory gate and thrashes the fleet, while an
    #    over-estimate merely declines to preload it — which the operator sees
    #    in the log. Fail toward "don't load", not "evict everything".
    logger.info(
        f"Preloader: unknown size for {model!r} and the node reported none — "
        f"assuming {_UNKNOWN_MODEL_SIZE_GB:.0f}GB (pessimistic). If this model "
        f"is small, it may not preload; check the node reports "
        f"models_available_sizes."
    )
    return _UNKNOWN_MODEL_SIZE_GB


def _parse_pinned_models(setting: str) -> list[str]:
    """Parse FLEET_PINNED_MODELS into a clean list of model names."""
    return [m.strip() for m in (setting or "").split(",") if m.strip()]


def _model_resident_on_node(model: str, node) -> bool:
    """True if ``node`` currently has ``model`` resident and serving.

    Covers both backends: Ollama (``models_loaded``) and MLX (``mlx_servers``
    entry with a ``healthy`` status — MLX names carry the ``mlx:`` prefix,
    which the server list stores stripped).  This is the SAME residency the
    scorer gates on ([scorer.py](scorer.py) reads ``models_loaded``), so
    "resident here" ⇒ routing won't fall back for it.
    """
    if is_mlx_model(model):
        target = strip_mlx_prefix(model)
        return any(
            s.model == target and s.status == "healthy"
            for s in (node.mlx_servers or [])
        )
    return bool(
        node.ollama and model in [m.name for m in node.ollama.models_loaded]
    )


def _model_is_loaded_anywhere(model: str, nodes) -> bool:
    """True if any online node has the model currently resident."""
    return any(_model_resident_on_node(model, n) for n in nodes)


async def _wait_until_resident(
    registry, node_id: str | None, model: str, timeout_s: float,
) -> tuple[bool, int]:
    """Poll the registry until ``model`` is resident, or ``timeout_s`` elapses.

    Mirrors the deadline-poll idiom in ``mlx_supervisor`` (``_wait_healthy``).
    The signal watched is the same ``models_loaded`` / healthy ``mlx_servers``
    the scorer gates on, so once this returns ``True`` the router won't fall
    back for the model.  ``pre_warm`` already blocks through the actual Ollama
    load, so this only waits out the heartbeat-reflection lag
    (``heartbeat_interval``, ~5s) — hence a short default timeout upstream.

    ``node_id=None`` waits for the model to appear on ANY online node (used
    when the pin didn't target a specific node); otherwise it polls that node.
    Returns ``(ready, elapsed_ms)``.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + timeout_s
    while True:
        if node_id is None:
            resident = _model_is_loaded_anywhere(model, registry.get_online_nodes())
        else:
            node = registry.get_node(node_id)
            resident = node is not None and _model_resident_on_node(model, node)
        if resident:
            return True, int((loop.time() - start) * 1000)
        if loop.time() >= deadline:
            return False, int((loop.time() - start) * 1000)
        await asyncio.sleep(_RESIDENCY_POLL_INTERVAL)


def _nodes_with_model_on_disk(model: str, nodes):
    """Nodes that have the model available on disk (pullable, not necessarily hot)."""
    return [n for n in nodes if n.ollama and model in n.ollama.models_available]


async def _load_model_on_best_node(
    model: str, nodes, proxy: StreamingProxy, *,
    why: str = "preload", target_node_id: str | None = None,
    settings=None,
) -> bool:
    """Pre-warm model; prefer ``target_node_id`` if given, else pick best-mem node.

    Returns True if load was attempted.  When ``target_node_id`` is set but
    that node doesn't have the model on disk or is offline, we fall back to
    the best-memory node (so a per-node pin still warms the fleet).

    ``settings`` supplies the num_ctx override, which decides both what we warm
    with and what we predict the model will cost.  Without it the gate falls
    back to the model's observed default context, or — never having seen it —
    to the old weights-only approximation.
    """
    available_nodes = _nodes_with_model_on_disk(model, nodes)
    if not available_nodes:
        logger.info(f"Preloader: {model} not on disk anywhere — skipping ({why})")
        return False
    best = None
    if target_node_id:
        for n in available_nodes:
            if n.node_id == target_node_id:
                best = n
                break
        if best is None:
            logger.info(
                f"Preloader: target node {target_node_id} doesn't have {model} "
                f"on disk; falling back to best-mem node ({why})"
            )
    if best is None:
        best = max(
            available_nodes,
            key=lambda n: n.memory.available_gb if n.memory else 0,
        )
    # What this model will actually COST resident — weights + KV cache for the
    # context we're about to request — not just its on-disk weights.  Sizing by
    # weights alone under-estimated qwen3-coder:30b by 5.4x (22.8GB assumed vs
    # 122.9GB real) and twice let a load through that panicked the box.  See
    # docs/issues/model-sizing-ignores-kv-cache.md.
    send_ctx = _num_ctx_override(model, settings)  # what we'll ask Ollama for
    num_ctx = _expected_num_ctx(model, settings)  # what it'll actually run at
    resident_gb = estimate_resident_gb(model, best, num_ctx)
    available = best.memory.available_gb if best.memory else 0
    # Skip the memory gate when the model is ALREADY resident on this node — it
    # is in memory by definition, so there is nothing left to "fit".  Without
    # this, warming a hot model can fail *because it's hot*: its own footprint
    # is already subtracted from the free memory the gate inspects.  Observed
    # 2026-07-17 — gpt-oss:120b was resident (71GB) and serving, yet a pin was
    # refused because the gate saw "need 72GB but only 49GB free".  pre_warm
    # still runs below so keep_alive=-1 is (re)applied, which is the whole
    # point of pinning an already-loaded model.
    if not _model_resident_on_node(model, best) and available < resident_gb:
        ctx_note = f" @ {num_ctx} ctx" if num_ctx else ""
        logger.info(
            f"Preloader: skipping {model} — needs ~{resident_gb:.0f}GB resident"
            f"{ctx_note} but only {available:.0f}GB free on {best.node_id} ({why})"
        )
        # Only pinned models are worth surfacing.  An ordinary preload that
        # doesn't fit is the gate working as intended; a *pin* that doesn't fit
        # is a standing instruction the fleet cannot carry out, and it will be
        # retried on every refresh until a human intervenes.
        if "pinned" in (why or ""):
            _record_pin_fit_failure(model, best.node_id, resident_gb, available)
        return False
    ctx_note = f" @ {num_ctx} ctx" if num_ctx else ""
    logger.info(
        f"Preloader: loading {model} (~{resident_gb:.0f}GB resident{ctx_note}) "
        f"on {best.node_id} ({why})"
    )
    try:
        await proxy.pre_warm(best.node_id, model, num_ctx=send_ctx)
        return True
    except Exception as exc:
        logger.warning(f"Preloader: failed to load {model}: {exc}")
        return False


def _build_pinned_plan(
    env_pins: list[str],
    per_node_map: dict[str, list[str]],
) -> list[tuple[str, str | None]]:
    """Return ordered (model, target_node_id) list for pin loading.

    Env pins come first with ``None`` target (load on best-mem node).  Per-node
    pins follow with their node id set.  Duplicates within the same target
    bucket are collapsed.  A model pinned both env-wide and per-node will
    appear twice — once as fleet-wide, once targeted — and the preloader's
    "already hot anywhere" check skips the second if the first succeeded.
    """
    plan: list[tuple[str, str | None]] = [(m, None) for m in env_pins if m]
    seen_per_node: set[tuple[str, str]] = set()
    for node_id, models in per_node_map.items():
        for m in models:
            key = (node_id, m)
            if m and key not in seen_per_node:
                seen_per_node.add(key)
                plan.append((m, node_id))
    return plan


async def preload_priority_models(
    registry: NodeRegistry,
    trace_store: TraceStore,
    proxy: StreamingProxy,
    settings: ServerSettings,
    *,
    pinned_store: PinnedModelsStore | None = None,
) -> None:
    """Startup: load pinned models, then fill remaining slots up to cap.

    Refresh loop: every 10 min, reload any pinned model that got evicted,
    plus top priority models with recent activity.  Respects
    ``model_preload_max_count`` as the total-slots budget so the
    preloader never thrashes the Ollama hot cap.
    """
    if getattr(settings, "disable_model_preloader", False):
        logger.info("Preloader disabled via FLEET_DISABLE_MODEL_PRELOADER")
        return

    env_pins = _parse_pinned_models(getattr(settings, "pinned_models", ""))
    per_node_map = pinned_store.load() if pinned_store else {}
    pinned_plan = _build_pinned_plan(env_pins, per_node_map)
    # Flat list preserved for step-2 priority fill exclusions + logging
    pinned = list(dict.fromkeys([m for m, _ in pinned_plan]))
    max_count = getattr(settings, "model_preload_max_count", 3)

    # Wait for at least one node to come online
    for _ in range(60):
        nodes = registry.get_online_nodes()
        if nodes:
            break
        await asyncio.sleep(1)
    else:
        logger.info("Preloader: no nodes registered after 60s, skipping startup")
        return

    # Brief delay to let the node's heartbeat fully populate
    await asyncio.sleep(3)
    nodes = registry.get_online_nodes()

    # --- Step 1: load pinned models ---------------------------------------
    loaded_count = 0
    for model, target in pinned_plan:
        if loaded_count >= max_count:
            logger.warning(
                f"Preloader: pinned-models plan ({len(pinned_plan)}) exceeds "
                f"max_count ({max_count}); truncating at {loaded_count}.  "
                f"Raise FLEET_MODEL_PRELOAD_MAX_COUNT if your backend can "
                f"handle more concurrent models."
            )
            break
        if _model_is_loaded_anywhere(model, nodes):
            logger.info(f"Preloader: {model} already hot (pinned)")
            loaded_count += 1
            continue
        why = f"pinned:{target}" if target else "pinned"
        if await _load_model_on_best_node(
            model, nodes, proxy, why=why, target_node_id=target,
            settings=settings,
        ):
            loaded_count += 1
            await asyncio.sleep(2)  # let Ollama update /api/ps
            nodes = registry.get_online_nodes()  # refresh after load

    # --- Step 2: fill remaining slots with priority models ----------------
    priorities = await get_cached_priorities(trace_store)
    if priorities:
        logger.info(
            f"Preloader: {len(priorities)} model(s) in usage history; "
            f"will fill up to {max_count - loaded_count} more slot(s)"
        )
        for entry in priorities:
            if loaded_count >= max_count:
                logger.info(
                    f"Preloader: reached max_count ({max_count}) — "
                    f"stopping to avoid Ollama LRU thrash"
                )
                break
            model = entry["model"]
            score = entry["priority_score"]
            if score < 1.0:
                break  # rarely-used models not worth warming
            if model in pinned:
                continue  # already handled above
            if _model_is_loaded_anywhere(model, nodes):
                continue  # already hot, no need to load
            if await _load_model_on_best_node(
                model, nodes, proxy, why=f"priority score={score}",
                settings=settings,
            ):
                loaded_count += 1
                await asyncio.sleep(2)
                nodes = registry.get_online_nodes()

    logger.info(
        f"Preloader startup complete: {loaded_count}/{max_count} models warm "
        f"({len(pinned)} pinned configured)"
    )

    # --- Step 3: refresh loop ---------------------------------------------
    # Every 10 min, ensure pinned models stay hot + top priorities with
    # recent activity stay hot.  Respects max_count as the overall budget.
    while True:
        await asyncio.sleep(600)
        try:
            # Re-read per-node pins so dashboard toggles land within a cycle
            if pinned_store is not None:
                refreshed_plan = _build_pinned_plan(
                    env_pins, pinned_store.load(),
                )
            else:
                refreshed_plan = pinned_plan
            await _refresh_priority_models(
                registry, trace_store, proxy,
                pinned_plan=refreshed_plan, max_count=max_count,
                settings=settings,
            )
        except Exception as exc:
            logger.warning(f"Preloader refresh failed: {exc}")


async def _refresh_priority_models(
    registry: NodeRegistry,
    trace_store: TraceStore,
    proxy: StreamingProxy,
    *,
    pinned: list[str] | None = None,
    pinned_plan: list[tuple[str, str | None]] | None = None,
    max_count: int = 3,
    settings=None,
) -> None:
    """Keep pinned models hot + top priorities with recent activity hot.

    Ordering matters:
      1. Pinned models FIRST — reload any that were evicted (regardless
         of recent activity; if the user pinned them, they stay hot)
      2. Top priority models with recent activity — fill remaining slots
         after pinned models

    Budget: total post-refresh hot-count stays ≤ max_count.  Pinned
    models get their slots first; priority models only fill what's left.
    """
    if pinned_plan is None:
        pinned_plan = [(m, None) for m in (pinned or [])]
    # Flat de-duped name list for priority-exclusion in step 2
    pinned_names = list(dict.fromkeys([m for m, _ in pinned_plan]))

    nodes = registry.get_online_nodes()
    if not nodes:
        return

    # --- Count currently-hot models, reserving slots for pinned -----------
    hot_models: set[str] = set()
    for n in nodes:
        if n.ollama:
            for m in n.ollama.models_loaded:
                hot_models.add(m.name)
    currently_hot_count = len(hot_models)

    # --- Step 1: ensure pinned models are hot -----------------------------
    loaded_this_cycle = 0
    for model, target in pinned_plan:
        if loaded_this_cycle >= max_count:
            break  # already filled the budget with pins alone
        if model in hot_models:
            continue  # already loaded, nothing to do
        # Pinned-but-missing: ALWAYS reload (no recency check — user pinned it)
        logger.info(
            f"Preloader refresh: pinned model {model} was evicted — reloading"
            + (f" on {target}" if target else "")
        )
        why = f"pinned-refresh:{target}" if target else "pinned-refresh"
        if await _load_model_on_best_node(
            model, nodes, proxy, why=why, target_node_id=target,
            settings=settings,
        ):
            loaded_this_cycle += 1
            await asyncio.sleep(2)
            nodes = registry.get_online_nodes()
            # Update hot_models snapshot after successful load
            for n in nodes:
                if n.ollama:
                    for m in n.ollama.models_loaded:
                        hot_models.add(m.name)

    # --- Step 2: fill remaining slots with top priority models ------------
    priorities = await get_cached_priorities(trace_store)
    if not priorities:
        return

    # Respect user intent: only reload priorities with recent activity.
    # Pinned models bypass this — they're reloaded regardless.
    recent_models = await trace_store.get_recently_used_models(seconds=3600)

    # Budget: total hot + loaded-this-cycle must stay ≤ max_count
    remaining_budget = max_count - max(currently_hot_count, loaded_this_cycle)
    if remaining_budget <= 0:
        return

    top_priorities = [
        p for p in priorities
        if p["priority_score"] >= 10 and p["model"] not in pinned_names
    ][: max_count]  # cap search scope

    for entry in top_priorities:
        if remaining_budget <= 0:
            break
        model = entry["model"]
        if model in hot_models:
            continue
        if model not in recent_models:
            logger.info(
                f"Preloader refresh: skipping {model} — no requests in last hour"
            )
            continue
        if await _load_model_on_best_node(
            model, nodes, proxy, why=f"priority-refresh score={entry['priority_score']}",
            settings=settings,
        ):
            remaining_budget -= 1
            await asyncio.sleep(2)
            nodes = registry.get_online_nodes()
