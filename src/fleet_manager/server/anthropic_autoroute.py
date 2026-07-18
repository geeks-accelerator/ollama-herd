"""Best-loaded routing for the Anthropic Messages API.

Claude Code sends model ids like ``claude-sonnet-4-5``.  Historically the herd
required a hand-written ``FLEET_ANTHROPIC_MODEL_MAP`` translating each id to a
specific local model — which meant every deployment had to maintain a map that
matched exactly what it had pulled, and a map entry pointing at a model that
isn't downloaded is a guaranteed 404 (as our own box demonstrated: it mapped
four aliases at ``mlx:...Qwen3-Coder-Next-4bit`` long after that server was
dropped).

This module makes the map **optional**.  When a Claude id has no explicit
mapping, we resolve it to the *best currently-loaded* local model for its tier —
so a fresh install with whatever the user pulled Just Works, and an explicit map
entry is a per-alias override rather than a requirement.

The resolver is deliberately pure: it takes the loaded/on-disk model *names* and
returns a name plus a reason string.  The route layer supplies the names from
the registry and does the I/O.  Everything here is unit-testable without a
running fleet.
"""

from __future__ import annotations

from fleet_manager.models.request import normalize_model_name
from fleet_manager.server.model_knowledge import (
    ModelCategory,
    classify_model,
    is_vision_model,
    lookup_model,
)

# Tiers we infer from the Claude model id.  Claude Code's three families map
# onto a speed/quality axis; the tier only changes how we weigh size against
# quality, not which models are *eligible*.
TIER_FAST = "fast"  # haiku — smallest capable model, latency over peak quality
TIER_BALANCED = "balanced"  # sonnet (and any unrecognised claude-*) — the workhorse
TIER_PREMIUM = "premium"  # opus — best quality, size is a plus not a cost

# Categories that can serve a chat/agentic request at all.  Embedding and image
# models never can; we filter them out before ranking.
_CHAT_CATEGORIES = frozenset(
    {
        ModelCategory.CODING,
        ModelCategory.GENERAL,
        ModelCategory.REASONING,
        ModelCategory.CREATIVE,
        ModelCategory.FAST_CHAT,
        ModelCategory.VISION,  # vision models handle text too
    }
)
_NEVER_CHAT = frozenset(
    {
        ModelCategory.VISION_EMBEDDING,
        ModelCategory.IMAGE,
    }
)

# How much each category is worth for an *agentic coding* workload (Claude
# Code's actual use).  Coder models win; reasoning models are capable but their
# thinking tokens make agentic loops slow, so they rank below general chat;
# vision models are a last resort for text.  Applied on top of quality_score.
_CATEGORY_BONUS = {
    ModelCategory.CODING: 15.0,
    ModelCategory.GENERAL: 5.0,
    ModelCategory.FAST_CHAT: 0.0,
    ModelCategory.CREATIVE: -5.0,
    ModelCategory.REASONING: -5.0,
    ModelCategory.VISION: -10.0,
}

# Quality assumed for a loaded model we have no catalog entry for.  Mid-scale:
# below anything we actually know is good, above nothing — so a known-good model
# outranks a mystery one, but a mystery one still beats routing to nothing.
_UNKNOWN_QUALITY = 50.0
# Params assumed for an unknown model, in billions — only affects the size
# tiebreak, and a neutral mid value keeps unknowns from dominating either tier.
_UNKNOWN_PARAMS_B = 30.0

# Text-embedding models must never serve a chat request.  The catalog has no
# text-embedding category, so ``classify_model`` returns GENERAL for them (e.g.
# nomic-embed-text) — which would make an embedding model a chat candidate and
# route a Claude Code turn straight into a 500.  Exclude by name; these
# substrings cover the common families (nomic-embed, bge, gte, e5, arctic-embed,
# all-minilm, snowflake).
_EMBEDDING_HINTS = ("embed", "bge-", "gte-", "e5-", "minilm", "arctic")


def _is_embedding_name(name: str) -> bool:
    lower = name.lower()
    return any(h in lower for h in _EMBEDDING_HINTS)


def _tier_of(claude_model: str) -> str:
    """Infer the speed/quality tier from a Claude model id."""
    m = (claude_model or "").lower()
    if "haiku" in m:
        return TIER_FAST
    if "opus" in m:
        return TIER_PREMIUM
    return TIER_BALANCED  # sonnet, and anything unrecognised → the safe middle


def _is_on_fleet(model: str, ondisk_names) -> bool:
    """True if ``model`` names a model the fleet actually has.

    This is the passthrough gate: a caller may send a real Ollama/MLX name
    (``qwen3-coder:30b``, ``mlx:...``) straight through a foreign-API endpoint,
    and we must not rewrite it.

    It replaces an older heuristic (``":" in model or not
    model.startswith("claude")``) that asked "does this *look* local?" rather
    than "is it *here*?".  That worked while Anthropic was the only foreign API
    — a ``claude-*`` alias was the only non-local thing we saw — but it breaks
    the moment another provider's ids arrive: Codex sends e.g. ``gpt-5-codex``,
    which has no colon and doesn't start with ``claude``, so the old rule
    passed it straight through to a guaranteed 404 instead of auto-routing.

    Asking about presence instead is both correct and *less* provider-specific:
    a name that's here passes through, a name that isn't (``claude-sonnet-4-5``,
    ``gpt-5-codex``, or a typo) falls through to auto-routing.  Tag-tolerant,
    since clients often omit the ``:latest`` Ollama always reports.
    """
    if not model:
        return False
    names = set(ondisk_names or ())
    return model in names or normalize_model_name(model) in names


def _candidate_score(name: str, tier: str, *, want_vision: bool) -> float | None:
    """Rank score for loading ``name`` to serve ``tier``; None if unusable.

    Higher is better.  Unusable = an embedding/image model, or (for an image
    request) a model that can't see images.
    """
    if _is_embedding_name(name):
        return None  # embedding models can't chat, and classify as GENERAL

    spec = lookup_model(name)
    category = spec.category if spec else classify_model(name)

    if category in _NEVER_CHAT:
        return None
    if category not in _CHAT_CATEGORIES:
        return None
    if want_vision and not is_vision_model(name):
        return None

    if spec and spec.benchmarks.quality_score > 0:
        quality = spec.benchmarks.quality_score
    else:
        quality = _UNKNOWN_QUALITY
    params = spec.params_b if spec else _UNKNOWN_PARAMS_B

    base = quality + _CATEGORY_BONUS.get(category, 0.0)

    if tier == TIER_PREMIUM:
        # Best quality; when close, prefer the larger (more capable) model.
        return base + params * 0.1
    if tier == TIER_FAST:
        # Latency tier: quality still matters, but bias hard toward small.
        return base - params * 0.5
    return base  # balanced: quality + category, size-neutral


def rank_candidates(
    names, claude_model: str, *, want_vision: bool = False
) -> list[str]:
    """Return ``names`` that can serve ``claude_model``, best first.

    De-duplicates, drops unusable models, and orders by tier-appropriate score.
    Ties break deterministically by name so the choice is stable across calls.
    """
    tier = _tier_of(claude_model)
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        score = _candidate_score(name, tier, want_vision=want_vision)
        if score is None:
            continue
        scored.append((score, name))
    # Sort by score desc, then name asc for a stable tiebreak.
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [name for _, name in scored]


def resolve_model(
    model: str,
    model_map: dict[str, str],
    loaded_names,
    ondisk_names,
    *,
    auto_route: bool,
    has_images: bool = False,
) -> tuple[str | None, str]:
    """Resolve a Claude model id to a local model name.

    Returns ``(model, reason)``.  ``model`` is None only when nothing can serve
    the request — the caller turns that into a helpful 404.  ``reason`` is a
    short tag for logging/observability.

    Precedence (an explicit map entry always wins, so per-alias pinning stays
    available — it's just no longer required):

    1. exact entry in ``model_map`` → ``explicit-map``
    2. caller sent a real local name → ``passthrough``
    3. best loaded model for the tier → ``auto-loaded`` (auto_route only)
    4. best on-disk model for the tier → ``auto-ondisk`` (auto_route only)
    5. ``model_map["default"]`` → ``default``
    6. nothing → ``(None, "unresolved")``
    """
    model_map = model_map or {}

    # 1. Explicit per-alias mapping — the override, honoured first.
    explicit = model_map.get(model)
    if explicit:
        return explicit, "explicit-map"

    # 2. A real local model name sent straight through — never rewrite it.
    if _is_on_fleet(model, ondisk_names):
        return model, "passthrough"

    if auto_route:
        # 3. Best model already resident anywhere on the fleet — no cold load.
        ranked = rank_candidates(loaded_names, model, want_vision=has_images)
        if ranked:
            return ranked[0], "auto-loaded"
        # 4. Nothing suitable loaded — best on-disk, accepting a cold load.
        ranked = rank_candidates(ondisk_names, model, want_vision=has_images)
        if ranked:
            return ranked[0], "auto-ondisk"

    # 5. Configured catch-all, if any (also the escape hatch when auto is off).
    default = model_map.get("default")
    if default:
        return default, "default"

    # 6. Genuinely nothing to route to.
    return None, "unresolved"
