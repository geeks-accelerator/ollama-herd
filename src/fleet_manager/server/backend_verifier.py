"""Does herd talk to the Ollama each node describes?

A node reports its models from its own ``/api/tags``; herd dispatches to a URL
it builds itself (``NodeRegistry._build_ollama_url``).  Nothing checked that the
two are the same Ollama.  When they are not -- a remote node's heartbeat
forwarded over loopback used to be routed to the router's own ``localhost``,
and a Docker or proxy topology can still mislead -- every request either lands
on the wrong machine, silently, or comes back 404 "model not found" from a
backend that never had the model.  Community telemetry recorded 1,440 of those
in a row on one install (docs/plans/backend-model-not-found.md).

So the router asks the URL it actually uses for ``/api/tags`` and compares
the answer with what the node reported.  That also separates the two causes
of a node listing a model its backend 404s: a different Ollama (they
disagree) from a store whose manifest outlived its blobs (they agree).

**What does it fire on when everything works?**  Nothing:

* the comparison is between two snapshots taken together -- the probe and
  the heartbeat the registry holds at that moment -- so a model pulled or
  deleted between probes cannot appear missing;
* only models still in the node's *current* list are reported, so a deleted
  model drops out with the next heartbeat;
* an unreachable backend is not a finding (``query_node_models`` returns
  ``None``); ``connection_failures`` covers that, and a blind probe must be a
  missed detection, never a false alarm.

State is module level, like ``streaming.get_non_generatable_models``, because
``HealthEngine`` is built fresh for each report.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Re-ask each backend this often even when nothing changed.  A URL change is
# probed on the next tick, which is what catches a misrouted node quickly.
PROBE_INTERVAL_S = 300.0
TICK_S = 15.0


@dataclass(frozen=True)
class BackendCheck:
    """One probe: what herd's URL serves vs what the node said, at one moment."""

    node_id: str
    url: str
    checked_at: float
    # Reported by the node, absent from the backend herd dispatches to.
    missing: frozenset[str] = field(default_factory=frozenset)
    # Served at herd's URL, never reported by the node.
    extra: frozenset[str] = field(default_factory=frozenset)


_checks: dict[str, BackendCheck] = {}


def get_backend_checks() -> dict[str, BackendCheck]:
    """The latest probe per node.  Empty until a probe has run."""
    return dict(_checks)


def _ollama_models(names) -> set[str]:
    """The node's list without what never came from Ollama's ``/api/tags``."""
    return {n for n in names if n and not n.startswith("mlx:")}


class BackendVerifier:
    def __init__(self, registry, proxy, interval_s: float = PROBE_INTERVAL_S):
        self._registry = registry
        self._proxy = proxy
        self.interval_s = interval_s

    async def probe(self, node) -> BackendCheck | None:
        reported = _ollama_models(node.ollama.models_available if node.ollama else [])
        url = node.ollama_base_url
        served_rows = await self._proxy.query_node_models(node.node_id)
        if served_rows is None:
            return None
        served = {row["name"] for row in served_rows if row.get("name")}
        check = BackendCheck(
            node_id=node.node_id,
            url=url,
            checked_at=time.time(),
            missing=frozenset(reported - served),
            extra=frozenset(served - reported),
        )
        previous = _checks.get(node.node_id)
        _checks[node.node_id] = check
        if check.missing and (previous is None or previous.missing != check.missing):
            logger.warning(
                f"Backend mismatch on {node.node_id}: {url} does not have "
                f"{sorted(check.missing)}, which the node reports"
            )
        elif previous is not None and previous.missing and not check.missing:
            logger.info(f"Backend mismatch on {node.node_id} cleared ({url})")
        elif not check.missing:
            # Silent agreement must still be provably a probe that ran.
            logger.debug(
                f"Backend check on {node.node_id}: {url} has all {len(reported)} reported model(s)"
            )
        return check

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        live = set()
        for node in self._registry.get_all_nodes():
            if node.status.value != "online" or not node.ollama:
                continue
            live.add(node.node_id)
            last = _checks.get(node.node_id)
            if (
                last is None
                or last.url != node.ollama_base_url
                or now - last.checked_at >= self.interval_s
            ):
                await self.probe(node)
        for node_id in set(_checks) - live:
            _checks.pop(node_id, None)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a probe must never stop the loop
                logger.exception("Backend verification tick failed")
            await asyncio.sleep(TICK_S)
