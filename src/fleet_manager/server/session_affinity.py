"""Remember which node last served a conversation, so its prefix cache stays warm.

llama.cpp skips prompt tokens it has already processed —
``get_common_prefix(slot.prompt.tokens, input_tokens)`` — so turn N+1 of a
coding session, which resends turn N's entire conversation, can cost a few
hundred tokens of prefill instead of thirty thousand.

That matters more than it first appears.  Prefill is the *only* thing that
stalls other requests' generation: llama.cpp admits decode tokens first and
fills the remaining batch budget with prompt chunks, so a co-resident stream
loses roughly one decode step per prefill chunk.  Cutting prefill by ~100x
therefore doesn't just speed up the session that owns the cache — it removes
the interference that session inflicts on everything else on the node.

Routing the same conversation elsewhere throws that away and re-prefills from
cold.  On a single-node fleet this is free; the moment a second node joins it is
the difference between a warm session and a cold one on every turn.

Deliberately *approximate*, and the limits are worth stating plainly:

* We cannot see Ollama's cache.  A pin is a bet that the node still holds the
  prefix, and the TTL is how long that bet stays good.  Too long and we pin to a
  cold slot; too short and we lose the benefit on slow-turning sessions.
* Slots do **not** rotate blindly — an earlier version of this note had that
  backwards.  llama.cpp selects the slot whose cached tokens share the longest
  common prefix with the incoming request (ggml-org/llama.cpp#13606), so a node
  with ``OLLAMA_NUM_PARALLEL=4`` holds *four* warm conversations at once and
  finds the right one.  The pin is a better bet than the TTL implies.

  That makes the real limit a **capacity** one, not a time one: past N
  concurrent conversations per node, a pin is a claim the backend cannot back.
  ``mlx_lm.server`` is the same shape with ``--prompt-cache-size`` (we set 10).
  The TTL below remains a coarse safety net rather than the true bound.
  Being wrong costs one cold prefill — exactly what would have happened anyway
  without affinity — so the downside is bounded either way.
* The bonus is a *preference*, never a constraint.  Elimination still runs
  first, so an offline, drained or over-full node is never chosen because a
  session once landed there.
"""

from __future__ import annotations

import time

# How long a pin stays credible.  Chosen to comfortably span the gap between
# turns in an interactive coding session (seconds to a few minutes) while
# expiring long before a model would plausibly have been evicted and reloaded.
DEFAULT_TTL_S = 900

# Bounds memory on a busy fleet.  Sessions are cheap (two short strings), and
# the oldest entries are the least likely to still be cache-warm anyway.
MAX_SESSIONS = 2000


class SessionAffinityTracker:
    """Bounded, TTL'd map of session → the node that last served it."""

    def __init__(self, ttl_s: int = DEFAULT_TTL_S, max_sessions: int = MAX_SESSIONS):
        self._ttl_s = ttl_s
        self._max = max_sessions
        self._seen: dict[str, tuple[str, float]] = {}

    def remember(self, session_key: str, node_id: str) -> None:
        if not session_key or not node_id:
            return
        self._seen[session_key] = (node_id, time.time())
        if len(self._seen) > self._max:
            self._evict()

    def preferred_node(self, session_key: str) -> str | None:
        """The node that last served this session, if the pin is still fresh."""
        if not session_key:
            return None
        entry = self._seen.get(session_key)
        if entry is None:
            return None
        node_id, seen_at = entry
        if time.time() - seen_at > self._ttl_s:
            self._seen.pop(session_key, None)
            return None
        return node_id

    def _evict(self) -> None:
        """Drop expired entries, then oldest-first until back under the cap."""
        now = time.time()
        for k in [k for k, (_, t) in self._seen.items() if now - t > self._ttl_s]:
            self._seen.pop(k, None)
        if len(self._seen) <= self._max:
            return
        for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1][1])[
            : len(self._seen) - self._max
        ]:
            self._seen.pop(k, None)


def session_key_for(request) -> str:
    """Best-effort conversation identity from what a proxy can actually see.

    An explicit ``session:<id>`` tag wins — a client that tells us its
    conversation id is giving us ground truth.  Otherwise fall back to
    client IP plus model, which is right for the common case (one agentic
    session per client) and wrong in a way that costs only a cold prefill:
    two concurrent sessions from one IP on one model share a pin and may
    collide.  That is strictly better than no affinity, which is a cold
    prefill *every* turn.
    """
    for tag in getattr(request, "tags", None) or []:
        if isinstance(tag, str) and tag.startswith("session:"):
            return tag
    ip = getattr(request, "client_ip", "") or ""
    model = getattr(request, "original_model", "") or getattr(request, "model", "")
    return f"{ip}|{model}" if ip else ""
