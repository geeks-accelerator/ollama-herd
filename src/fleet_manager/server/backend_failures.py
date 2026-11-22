"""Short memory of backends that keep failing before they produce anything.

A backend that rejects every request at once has an empty queue and no slow
traces, so every queue- and latency-based signal scores it as the idlest node
in the fleet.  Run 6 met exactly that: Metal ran out of memory on a 24 GB M4,
Ollama's ``llama-server`` went into "backend is in error state ... recreate the
backend to recover", and answered every request with an error in milliseconds
while ``/api/ps`` still listed the model.  The retry moved each request to the
other Mac, but every new request asked the broken one first.

The memory is per ``node:model`` because the runner is: another model on the
same node has its own ``llama-server`` and may be fine.  It counts consecutive
attempts that failed before the first chunk with an infrastructure error (the
ones the retry treats as retryable), and forgets on the first success.  Once a
backend reaches ``THRESHOLD`` it is *failing* for ``COOL_OFF_S`` after its last
failure: long enough that a burst of traffic goes elsewhere, short enough that
one request probes it again soon and a recovered backend gets its traffic back.
It is a penalty in the scorer, never an elimination, so a fleet whose only node
is failing still tries it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _State:
    consecutive: int = 0
    last_failure: float = 0.0
    last_error: str = ""


class BackendFailureMemory:
    THRESHOLD = 3
    COOL_OFF_S = 30.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._states: dict[tuple[str, str], _State] = {}

    def record_failure(self, node_id: str, model: str, error: str = "") -> None:
        state = self._states.setdefault((node_id, model), _State())
        state.consecutive += 1
        state.last_failure = self._clock()
        state.last_error = error

    def record_success(self, node_id: str, model: str) -> None:
        self._states.pop((node_id, model), None)

    def is_failing(self, node_id: str, model: str) -> bool:
        state = self._states.get((node_id, model))
        return (
            state is not None
            and state.consecutive >= self.THRESHOLD
            and self._clock() - state.last_failure < self.COOL_OFF_S
        )

    def failing(self) -> list[dict]:
        """Backends failing right now, for logs and the status page."""
        return [
            {
                "node_id": node,
                "model": model,
                "consecutive_failures": s.consecutive,
                "last_error": s.last_error,
            }
            for (node, model), s in self._states.items()
            if self.is_failing(node, model)
        ]
