#!/usr/bin/env python3
"""Replay captured requests from the debug log.

Requires ``FLEET_DEBUG_REQUEST_BODIES=true`` to have been enabled on the router
when the original requests ran.  Reads ``<data_dir>/debug/requests.*.jsonl``
and re-POSTs each request (the exact ``client_body`` and path) against a
target URL, then compares.

Usage:
    # Replay all failed requests from the last hour against the same router
    python3 scripts/replay-debug-requests.py \\
        --data-dir ~/.fleet-manager \\
        --target http://localhost:11435 \\
        --failures-only --since 1h

    # Replay a specific request_id
    python3 scripts/replay-debug-requests.py \\
        --data-dir ~/.fleet-manager \\
        --target http://localhost:11435 \\
        --request-id abc-123-...

    # List captured requests (no replay)
    python3 scripts/replay-debug-requests.py --data-dir ~/.fleet-manager --list
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Allow running as a script without installing the package
SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from fleet_manager.server import debug_log  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def parse_since(text: str | None) -> float | None:
    """Parse '1h', '30m', '2d', or a unix timestamp."""
    if not text:
        return None
    m = re.fullmatch(r"(\d+)([smhd])", text.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return time.time() - (n * mult)
    try:
        return float(text)
    except ValueError:
        print(f"Invalid --since value: {text}")
        sys.exit(1)


def endpoint_path_for(record: dict) -> str:
    """Guess the router path to hit based on original_format."""
    fmt = record.get("original_format", "")
    if fmt == "anthropic":
        return "/v1/messages"
    if fmt == "openai":
        return "/v1/chat/completions"
    return "/api/chat"


def pretty_status(status: int | str, ok: bool) -> str:
    color = "\033[32m" if ok else "\033[31m"
    return f"{color}{status}\033[0m"


def post(url: str, body: dict, timeout: float) -> tuple[int, object, float]:
    """POST JSON, returning (status, parsed_or_raw, elapsed_seconds)."""
    t0 = time.time()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        elapsed = time.time() - t0
        try:
            return resp.status, json.loads(raw), elapsed
        except json.JSONDecodeError:
            return resp.status, raw.decode(errors="replace"), elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body_text = e.read().decode(errors="replace") if hasattr(e, "read") else str(e)
        return e.code, body_text, elapsed
    except Exception as e:
        elapsed = time.time() - t0
        return 0, f"{type(e).__name__}: {e}", elapsed


def summarize(status: int, body: object) -> tuple[bool, str]:
    ok = status == 200
    if isinstance(body, str):
        return ok, body[:200]
    if isinstance(body, dict):
        # error field from Ollama
        if "error" in body and status != 200:
            return False, str(body["error"])[:200]
        # Ollama/OpenAI-ish content peek
        if "message" in body and isinstance(body["message"], dict):
            content = body["message"].get("content", "")
            return ok, f"{content[:200]}"
        if "choices" in body:
            msg = body["choices"][0].get("message", {})
            return ok, f"{msg.get('content', '')[:200]}"
        if "content" in body and isinstance(body["content"], list):
            txt = "".join(
                b.get("text", "") for b in body["content"] if b.get("type") == "text"
            )
            return ok, txt[:200]
    return ok, json.dumps(body)[:200] if isinstance(body, dict) else str(body)[:200]


# ─────────────────────────────────────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────────────────────────────────────


def action_list(data_dir: str, since: float | None, failures_only: bool) -> int:
    records = (
        debug_log.find_failures(data_dir, since=since)
        if failures_only
        else debug_log.iter_records(data_dir, since=since)
    )
    if not records:
        print("No captured requests match.")
        return 0
    print(f"{'request_id':40s}  {'when':20s}  {'status':20s}  {'lat':>8s}  {'model':30s}")
    print("─" * 130)
    for r in records:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("timestamp", 0)))
        lat = r.get("latency_ms") or 0
        print(
            f"{(r.get('request_id') or '')[:40]:40s}  "
            f"{ts:20s}  "
            f"{(r.get('status') or '')[:20]:20s}  "
            f"{lat / 1000:>7.1f}s  "
            f"{(r.get('model') or '')[:30]:30s}"
        )
    print()
    print(f"{len(records)} record(s).")
    return 0


def action_replay(
    data_dir: str,
    target: str,
    since: float | None,
    failures_only: bool,
    request_id: str | None,
    limit: int | None,
    timeout: float,
) -> int:
    if request_id:
        found = debug_log.find_by_request_id(data_dir, request_id)
        records = [found] if found else []
        if not records:
            print(f"No capture found for request_id={request_id}")
            return 1
    else:
        records = (
            debug_log.find_failures(data_dir, since=since)
            if failures_only
            else debug_log.iter_records(data_dir, since=since)
        )
        if limit:
            records = records[:limit]

    if not records:
        print("No captured requests match.")
        return 0

    passed = 0
    for rec in records:
        rid = rec.get("request_id", "(no-id)")[:24]
        orig_status = rec.get("status", "?")
        orig_error = rec.get("error")
        path = endpoint_path_for(rec)
        body = rec.get("client_body") or {}
        url = target.rstrip("/") + path

        new_status, resp, elapsed = post(url, body, timeout=timeout)
        ok, summary = summarize(new_status, resp)

        marker = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
        print(
            f"{marker} {rid:26s}  orig=[{orig_status}]  "
            f"replay=[{pretty_status(new_status, ok)}]  "
            f"{elapsed:5.1f}s"
        )
        if orig_error:
            print(f"     was: {orig_error[:140]}")
        if summary:
            preview = summary.replace("\n", " ")
            print(f"     now: {preview[:140]}")
        if ok:
            passed += 1

    print()
    print(f"{passed}/{len(records)} replayed successfully")
    return 0 if passed == len(records) else 1


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data-dir",
        default="~/.fleet-manager",
        help="Herd data directory (default: ~/.fleet-manager)",
    )
    parser.add_argument(
        "--target",
        default="http://localhost:11435",
        help="Router URL to replay against (default: http://localhost:11435)",
    )
    parser.add_argument(
        "--list", action="store_true", help="Just list captured requests and exit"
    )
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="Only show/replay requests that failed or disconnected",
    )
    parser.add_argument(
        "--since",
        help="Only include records newer than this (e.g. '1h', '30m', '2d', or unix ts)",
    )
    parser.add_argument(
        "--request-id",
        help="Replay a single request by its id",
    )
    parser.add_argument(
        "--limit", type=int, help="Max number of records to replay"
    )
    parser.add_argument(
        "--timeout", type=float, default=600, help="HTTP timeout per replay (seconds)"
    )
    args = parser.parse_args()

    since = parse_since(args.since)
    data_dir = str(Path(args.data_dir).expanduser())

    if args.list:
        return action_list(data_dir, since, args.failures_only)

    return action_replay(
        data_dir=data_dir,
        target=args.target,
        since=since,
        failures_only=args.failures_only,
        request_id=args.request_id,
        limit=args.limit,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
