"""Runtime pinned-models store — per-node persistence.

The model preloader (``src/fleet_manager/server/model_preloader.py``) reads
two sources of "pinned" declarations:

  1. ``FLEET_PINNED_MODELS`` env var — a comma-separated list applied to
     every node.  Fine for single-node fleets; coarse for multi-node.

  2. This file — JSON at ``<data_dir>/pinned_models.json`` with per-node
     granularity.  Managed via ``/dashboard/api/pinned-models`` so users
     can pin/unpin from the dashboard without editing env + restarting.

The preloader merges both: env pins are always in effect; per-node pins
ADD to them for the specified node.  Unions only — we never downgrade
an env-pinned model just because it's absent from the JSON file.

File format (pretty-printed for grep-ability):

    {
      "nodes": {
        "Neons-Mac-Studio": ["gpt-oss:120b", "gemma3:27b"],
        "Lucass-MacBook-Pro-2": ["qwen3-coder:30b-agent"]
      },
      "updated_at": 1776900000.0
    }

Concurrency: JSON writes are atomic via tempfile + rename.  Reads tolerate
a partially-written file by returning an empty state (fail-open).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class PinnedModelsStore:
    """Persistent per-node pinned-models state.

    Thread-safe via a single module-level lock.  Cheap enough to re-read
    on every preloader cycle (KB-sized JSON, read maybe 6x/hour).
    """

    def __init__(self, file_path: Path):
        self._path = file_path
        self._lock = Lock()
        # Ensure parent dir exists
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, list[str]]:
        """Return {node_id: [model_names]} map, empty if file doesn't exist."""
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, list[str]]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            # Fail-open: partially-written file or missing → return empty.
            # Never crash the caller; the preloader needs to keep working.
            logger.warning(
                f"PinnedModelsStore: failed to read {self._path} "
                f"({type(exc).__name__}); returning empty state"
            )
            return {}
        nodes = data.get("nodes") or {}
        # Sanitize: only keep non-empty string lists
        out: dict[str, list[str]] = {}
        for node_id, models in nodes.items():
            if not isinstance(node_id, str) or not isinstance(models, list):
                continue
            clean = [m for m in models if isinstance(m, str) and m.strip()]
            if clean:
                out[node_id] = sorted(set(clean))  # dedup + stable order
        return out

    def set_pin(self, node_id: str, model: str, pinned: bool) -> dict[str, list[str]]:
        """Add or remove a pin for a (node_id, model) pair.

        Returns the full updated state after the change.  Persists
        atomically via tempfile + os.replace.
        """
        if not node_id or not model:
            raise ValueError("node_id and model must be non-empty")
        with self._lock:
            state = self._load_unlocked()
            current = set(state.get(node_id, []))
            if pinned:
                current.add(model)
            else:
                current.discard(model)
            if current:
                state[node_id] = sorted(current)
            elif node_id in state:
                # No pins left for this node — drop the key
                del state[node_id]
            self._write_atomic(state)
            return state

    def get_for_node(self, node_id: str) -> list[str]:
        """Convenience: return pinned model names for a specific node."""
        return self.load().get(node_id, [])

    def _write_atomic(self, state: dict[str, list[str]]) -> None:
        """Write via tempfile + replace so readers never see a partial file."""
        payload = {
            "nodes": state,
            "updated_at": time.time(),
        }
        # Temp file in same dir to guarantee rename stays on the same FS
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=str(self._path.parent),
            delete=False,
            prefix=".pinned-",
            suffix=".json.tmp",
        ) as tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp_name = tmp.name
        # Atomic on POSIX: mv replaces in one syscall.
        try:
            os.replace(tmp_name, self._path)
        except OSError as exc:
            # Clean up tmpfile if replace failed
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise RuntimeError(
                f"Failed to persist pinned models to {self._path}: {exc}",
            ) from exc


def merge_pins(
    env_pins: list[str], per_node_pins: list[str],
) -> list[str]:
    """Union env-level + per-node pins, preserving order (env first).

    Ensures env-declared pins always take effect even if absent from the
    runtime JSON.  Per-node pins add node-specific extras on top.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in env_pins + per_node_pins:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out
