"""Summarize one fleet benchmark run into one results file, driven by the run's manifest.

The raw reports from ``scripts/benchmark.py --output`` keep every request (46 MB for
run 4); the summary keeps what the published page and its readers need. The manifest
(``fleet_run<N>_manifest.json`` next to this script) names the output file, the run's
facts (``run``, copied to the top of the summary, ``prices_usd_per_hour`` among them)
and a tree of ``sections``. A node of that tree is one of:

- an arm, ``{"files": glob or [globs], "router": label, "keep_requests": bool,
  "what": text, "events": [{"at": UTC ISO time, "what": text}]}`` (all but ``files``
  and ``router`` optional): the reports its globs match, relative to the raw dir;
- a scaling section, ``{"cells": glob, ...}``: reports grouped into cells by machine
  count and concurrency, every other key copied;
- anything else: a dict is recursed into, a value is copied.

An arm or cell with several reports is ordered by each report's
``manifest.repetition``; a lone report may have none. Every numeric figure of a
multi-repetition arm or cell gets its ``spread``: ``<name>_median``, ``_min``,
``_max``. A one-report arm keeps its figures bare. First-token percentiles come from
successful chat requests with a first token (the engine's rule, ``CHAT_WORKLOADS``)
through the engine's own ``percentile_fields``. ``usd_per_hour`` is the machines in the
report's manifest priced by ``prices_usd_per_hour`` (keys are chip-name substrings);
``tokens_per_usd`` is chat tokens per dollar of those machines. Events are placed in the
repetition whose run window holds them, at ``t_s`` seconds from that run's
``manifest.started_at`` (the run clock's zero; request ``started_ms`` count from it).

    cd docs/experiments
    uv run python fleet-summarize.py fleet_run4_manifest.json <run 4 raw results dir>
    uv run python fleet-summarize.py fleet_run5_manifest.json <run 5 raw results dir>
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from fleet_manager.common.stats import percentile
from fleet_manager.server.benchmark_engine import CHAT_WORKLOADS, percentile_fields

SUMMARY_PERCENTILES = (50, 95)
NUMBER_LIST = re.compile(r"\[\s*((?:-?[\d.]+,\s*)*-?[\d.]+)\s*\]")
REQUEST_KEYS = (
    "started_ms",
    "model",
    "node_id",
    "success",
    "error",
    "ttft_ms",
    "latency_ms",
    "completion_tokens",
)


def is_number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def spread(values: list) -> dict:
    """Median and range of one figure across repetitions; ``None`` entries (no samples
    in that repetition) are left out, and none at all gives ``None``s.  The median is
    rounded to 0.01, which only ever trims an interpolated (even-count) median."""
    vals = [v for v in values if v is not None]
    if not vals:
        return {"median": None, "min": None, "max": None}
    return {"median": round(percentile(vals, 50), 2), "min": min(vals), "max": max(vals)}


def spread_fields(rows: list[dict], skip: tuple[str, ...] = ()) -> dict:
    """``spread`` of every figure of ``rows`` (a key whose values are all numbers or
    ``None``), as ``<key>_median/_min/_max``."""
    keys = dict.fromkeys(k for r in rows for k in r if k not in skip)
    out = {}
    for k in keys:
        values = [r.get(k) for r in rows]
        if all(v is None or is_number(v) for v in values):
            out.update({f"{k}_{s}": v for s, v in spread(values).items()})
    return out


def ttft_samples(requests: list[dict]) -> list[float]:
    """First-token times as the engine counts them: answered chat requests that had
    a first token (an embedding's "first token" is its whole latency)."""
    return [
        q["ttft_ms"]
        for q in requests
        if q["success"] and q.get("model_type") in CHAT_WORKLOADS and q["ttft_ms"] > 0
    ]


def ttft_fields(requests: list[dict]) -> dict:
    return percentile_fields(ttft_samples(requests), "ttft", SUMMARY_PERCENTILES)


def usd_per_hour(report: dict, prices: dict[str, float]) -> float:
    total = 0.0
    for node in report["manifest"]["nodes"]:
        chip = node.get("chip") or ""
        match = [k for k in prices if k in chip]
        if len(match) != 1:
            raise SystemExit(
                f"{report.get('_file', 'report')}: node {node['node_id']} chip {chip!r} matches "
                f"{match or 'no'} price keys; prices_usd_per_hour needs exactly one"
            )
        total += prices[match[0]]
    return round(total, 2)


def tokens_per_usd(tokens_per_sec, usd: float):
    return round(tokens_per_sec * 3600 / usd) if tokens_per_sec and usd else None


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise SystemExit(f"time {s!r} has no UTC offset")
    return t


def place_events(events: list[dict], reports: list[dict], where: str) -> list[list[dict]]:
    """Each event in the one report whose run window holds it, as seconds from that
    report's run clock zero; every report must get at least one."""
    placed: list[list[dict]] = [[] for _ in reports]
    for ev in events:
        at = parse_time(ev["at"])
        for i, rep in enumerate(reports):
            m = rep["manifest"]
            start, end = parse_time(m["started_at"]), parse_time(m["ended_at"])
            if start <= at <= end:
                t_s = round((at - start).total_seconds(), 1)
                placed[i].append({"t_s": t_s, "at": ev["at"], "what": ev["what"]})
                break
        else:
            raise SystemExit(f"{where}: event at {ev['at']} is in no report's run window")
    for i, evs in enumerate(placed):
        if not evs:
            raise SystemExit(f"{where}: {reports[i]['_file']} has no events; add its logged times")
    return placed


def per_model(report: dict) -> dict:
    out = {}
    for model in sorted({q["model"] for q in report["requests"]}):
        asked = [q for q in report["requests"] if q["model"] == model]
        ok = [q for q in asked if q["success"]]
        out[model] = {
            "asked": len(asked),
            "answered": len(ok),
            "completion_tokens": sum(q["completion_tokens"] for q in ok),
            **ttft_fields(asked),
            "nodes": dict(Counter(q["node_id"] for q in ok)),
            "failures": dict(
                Counter((q.get("error") or "")[:60] for q in asked if not q["success"])
            ),
        }
    return out


def arm(report: dict, router: str, prices: dict, keep_requests: bool = False) -> dict:
    """One report's figures."""
    requests = report["requests"]
    ok = [q for q in requests if q["success"]]
    events = report.get("model_events") or []
    usd = usd_per_hour(report, prices)
    out = {
        "router": router,
        "sent": len(requests),
        "answered": len(ok),
        "failed": len(requests) - len(ok),
        "chat_tokens_per_sec": report["tokens_per_sec"],
        "usd_per_hour": usd,
        "tokens_per_usd": tokens_per_usd(report["tokens_per_sec"], usd),
        **ttft_fields(requests),
        "model_loads": sum(e["event"] == "loaded" for e in events),
        "duration_s": report.get("duration_s"),
        "per_model": per_model(report),
        "settings": report["manifest"].get("workload"),
    }
    if keep_requests:
        out["model_events"] = events
        out["requests"] = [{k: q.get(k) for k in REQUEST_KEYS} for q in requests]
    return out


def by_repetition(reports: list[dict], where: str) -> list[dict]:
    """``reports`` in repetition order; one report may have no repetition, several
    must each have a distinct one."""
    if len(reports) == 1:
        return reports
    reps = [r["manifest"].get("repetition") for r in reports]
    if None in reps or len(set(reps)) != len(reps):
        files = ", ".join(f"{r['_file']}={r['manifest'].get('repetition')}" for r in reports)
        raise SystemExit(f"{where}: reports need distinct repetitions ({files})")
    return sorted(reports, key=lambda r: r["manifest"]["repetition"])


def summarize_arm(spec: dict, reports: list[dict], prices: dict, where: str) -> dict:
    reports = by_repetition(reports, where)
    router, keep = spec["router"], spec.get("keep_requests", False)
    events = (
        place_events(spec["events"], reports, where) if "events" in spec else [None] * len(reports)
    )
    extra = {k: spec[k] for k in ("what",) if k in spec}
    if len(reports) == 1:
        out = {**arm(reports[0], router, prices, keep), **extra}
        if events[0] is not None:
            out["events"] = events[0]
        return out
    runs = [arm(r, router, prices, keep) for r in reports]
    settings = runs[0]["settings"]
    if any(x["settings"] != settings for x in runs):
        raise SystemExit(f"{where}: repetitions ran different workloads")
    models = list(dict.fromkeys(m for x in runs for m in x["per_model"]))
    unasked = {"asked": 0, "answered": 0, "completion_tokens": 0}
    reps = []
    for r, x, evs in zip(reports, runs, events, strict=True):
        m = r["manifest"]
        rep = {"rep": m["repetition"], "seed": m.get("seed"), "started_at": m["started_at"]}
        rep.update({k: v for k, v in x.items() if k not in ("router", "settings")})
        if evs is not None:
            rep["events"] = evs
        reps.append(rep)
    return {
        "router": router,
        **spread_fields(runs),
        "per_model": {
            model: spread_fields([x["per_model"].get(model, unasked) for x in runs])
            for model in models
        },
        "settings": settings,
        **extra,
        "reps": reps,
    }


def scaling_rep(report: dict, prices: dict) -> dict:
    usd = usd_per_hour(report, prices)
    return {
        "rep": report["manifest"].get("repetition"),
        "tokens_per_sec": report["tokens_per_sec"],
        "usd_per_hour": usd,
        "tokens_per_usd": tokens_per_usd(report["tokens_per_sec"], usd),
        "requests": report["total_requests"],
        "failed": report["total_failures"],
        "ttft_p50_ms": report["ttft_p50_ms"],
        "ttft_p95_ms": report["ttft_p95_ms"],
        "ttft_p99_ms": report["ttft_p99_ms"],
        "per_node": {p["node_id"]: p["requests"] for p in report["per_node_results"]},
        "ttft_ms": ttft_samples(report["requests"]),
    }


def summarize_cells(spec: dict, reports: list[dict], prices: dict, where: str) -> dict:
    groups: dict[tuple[int, int], list[dict]] = {}
    for r in reports:
        key = (len(r["manifest"]["nodes"]), r["manifest"]["workload"]["concurrency"])
        groups.setdefault(key, []).append(r)
    cells = []
    for (n, c), group in sorted(groups.items()):
        reps = [scaling_rep(r, prices) for r in by_repetition(group, f"{where} n{n} c{c}")]
        cells.append(
            {"machines": n, "concurrency": c, "reps": reps, **spread_fields(reps, skip=("rep",))}
        )
    return {
        **{k: v for k, v in spec.items() if k != "cells"},
        "repetitions": max(len(cell["reps"]) for cell in cells),
        "cells": cells,
    }


def load_reports(raw: Path, globs, where: str) -> list[dict]:
    files = []
    for g in [globs] if isinstance(globs, str) else globs:
        matched = sorted(raw.glob(g))
        if not matched:
            raise SystemExit(f"{where}: {g!r} matches no file in {raw}")
        files += matched
    reports = []
    for f in files:
        report = json.loads(f.read_text())
        report["_file"] = str(f.relative_to(raw))
        reports.append(report)
    return reports


def summarize(node, raw: Path, prices: dict, where: str = "sections"):
    if not isinstance(node, dict):
        return node
    if "files" in node:
        return summarize_arm(node, load_reports(raw, node["files"], where), prices, where)
    if "cells" in node:
        return summarize_cells(node, load_reports(raw, node["cells"], where), prices, where)
    return {k: summarize(v, raw, prices, f"{where}.{k}") for k, v in node.items()}


def dumps(out: dict) -> str:
    """Indented JSON with each list of numbers (the ``ttft_ms`` samples) on one line."""
    text = json.dumps(out, indent=1)
    return NUMBER_LIST.sub(lambda m: "[" + " ".join(m.group(1).split()) + "]", text) + "\n"


def main(manifest_path: Path, raw: Path) -> Path:
    manifest = json.loads(manifest_path.read_text())
    if set(manifest) != {"output", "run", "sections"}:
        raise SystemExit(f"{manifest_path}: keys must be output, run, sections")
    run = manifest["run"]
    unset = [k for k, v in run.items() if v is None]
    if unset:
        raise SystemExit(f"{manifest_path}: fill in run.{', run.'.join(unset)}")
    out = {**run, **summarize(manifest["sections"], raw, run["prices_usd_per_hour"])}
    path = manifest_path.parent / manifest["output"]
    path.write_text(dumps(out))
    print(f"wrote {path} ({path.stat().st_size // 1024} KB)")
    return path


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
