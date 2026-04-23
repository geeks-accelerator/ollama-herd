"""Optional debug capture of full request/response bodies to JSONL.

DISABLED BY DEFAULT. Enable with ``FLEET_DEBUG_REQUEST_BODIES=true``.

What it does
------------
When enabled, every inference request that flows through the streaming proxy
is appended as a single JSON line to ``<data_dir>/debug/requests.jsonl`` on
the router.  Each line captures the complete lifecycle so failures can be
replayed exactly:

    {
      "request_id":      "uuid",
      "timestamp":       1776900000.123,
      "node_id":         "Lucass-MacBook-Pro-2",
      "model":           "qwen3-coder:30b-agent",
      "original_model":  "qwen3-coder:30b-agent",
      "original_format": "anthropic" | "openai" | "ollama",
      "tags":            ["user:..."],
      "status":          "completed" | "failed" | "client_disconnected",
      "error":           "ReadError(...)" | null,
      "latency_ms":      12345,
      "ttft_ms":         500,
      "prompt_tokens":   1234,
      "completion_tokens": 56,
      "client_body":     {...},   // exact body the client sent us
      "ollama_body":     {...},   // translated body we sent to Ollama
      "response":        {...}    // reconstructed final response (or partial on failure)
    }

Append-only JSONL is used because:
    - Each line is a complete record → crash-safe (partial writes lose at most
      the in-flight record, never corrupt past ones)
    - ``grep`` / ``jq`` / ``tail -f`` Just Work
    - No per-request file explosion, no locking
    - Errors are *always* captured because the writer runs in the same
      try/finally that records the trace

Privacy
-------
This records user prompts, tool results, file contents, and responses.  **Only
enable on fleets where you own every caller** — e.g. internal agent traffic
via gotomy.ai.  Never enable on public gateways.

Retention
---------
Files rotate daily (``requests.jsonl.YYYY-MM-DD``).  Set
``FLEET_DEBUG_REQUEST_RETENTION_DAYS`` (default 7) to auto-prune older files.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level lock keeps the append atomic across asyncio tasks.  JSON lines
# writes are tiny so contention is negligible.
_WRITE_LOCK = threading.Lock()


def _debug_dir(data_dir: str) -> Path:
    """Resolve and create the debug directory."""
    root = Path(data_dir).expanduser() / "debug"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _current_log_path(data_dir: str) -> Path:
    """Today's log file.  Rotation happens naturally by date suffix."""
    date = datetime.now().strftime("%Y-%m-%d")
    return _debug_dir(data_dir) / f"requests.{date}.jsonl"


def _prune_old(dir_path: Path, max_age_days: int) -> None:
    """Delete requests.*.jsonl files older than max_age_days.  No-op on <= 0."""
    if max_age_days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=max_age_days)
    try:
        for p in dir_path.glob("requests.*.jsonl"):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError as e:
        logger.debug(f"debug_log prune failed: {e}")


def append_request(
    *,
    enabled: bool,
    data_dir: str,
    record: dict[str, Any],
    retention_days: int = 7,
) -> None:
    """Append one request lifecycle record to today's JSONL file.

    Safe to call from any code path — returns immediately when ``enabled`` is
    False.  Never raises; failure to write is logged at DEBUG and swallowed so
    production traffic is never disturbed.

    Call this ONCE per request, from the finalizer that already knows the
    outcome (e.g. the same try/finally that persists the trace).  Do not call
    mid-stream — buffer the response chunks and pass the reconstructed final
    body via ``record["response"]``.
    """
    if not enabled:
        return

    try:
        path = _current_log_path(data_dir)
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _WRITE_LOCK, path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
        _prune_old(path.parent, retention_days)
    except Exception as e:  # noqa: BLE001 — never break the request path
        logger.debug(f"debug_log append failed for {record.get('request_id')}: {e}")


def iter_records(data_dir: str, *, since: float | None = None) -> list[dict[str, Any]]:
    """Return all records from all daily files, newest last.

    Optional ``since`` filter (unix timestamp) is cheap — we skip lines with
    ``timestamp < since`` without parsing the whole record.
    """
    try:
        dir_path = _debug_dir(data_dir)
        paths = sorted(dir_path.glob("requests.*.jsonl"))
        out: list[dict[str, Any]] = []
        for p in paths:
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since is not None and rec.get("timestamp", 0) < since:
                        continue
                    out.append(rec)
            except OSError:
                continue
        return out
    except OSError:
        return []


def find_by_request_id(data_dir: str, request_id: str) -> dict[str, Any] | None:
    """Find a single record by request_id (scans all daily files)."""
    for rec in iter_records(data_dir):
        if rec.get("request_id") == request_id:
            return rec
    return None


def find_failures(data_dir: str, *, since: float | None = None) -> list[dict[str, Any]]:
    """Return only records that ended in failure or client disconnect."""
    return [
        r for r in iter_records(data_dir, since=since)
        if r.get("status") not in ("completed", None) or r.get("error")
    ]
