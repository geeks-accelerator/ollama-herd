"""Summarize the 0.12.12 hardware checks (Lambda Cloud, 2026-11-21) into one results file.

Three sources, all raw reports from ``scripts/benchmark.py --output`` plus Ollama's own
journal on each node (``journalctl -u ollama -o short-iso-precise``):

* ``combined``: the vision workload (qwen3-coder:30b, nomic-embed-text, moondream, four
  workers each, 180 s) on two A10 24 GB and one A100 40 GB, router and harness on a fourth
  machine of their own, 0.12.11 and 0.12.12's code (``213cdb7``) alternating, three runs each.
* ``stranding``: the same workload after priming nomic-embed-text onto an A10 through the
  router and then loading it on the A100 directly, the situation run 5 hit by chance.
* ``slot_occupancy``: run 5's identical-machines arms (four A10, one model, 8 at once),
  how much of each run each node spent with 0, 1, 2, 3 or 4 requests in Ollama, read from
  the runner's ``processing task`` / ``stop processing`` lines.

    uv run python docs/experiments/release-0-12-12-summarize.py \
        <combined dir> <stranding dir> <run 5 dir>

Each dir is a bench folder with ``results/`` and ``journals/`` (phase logs, Ollama journals).
"""

from __future__ import annotations

import collections
import gzip
import json
import re
import sys
from pathlib import Path

from fleet_manager.common.stats import percentile

HERE = Path(__file__).parent
MODELS = {"qwen3-coder": "coding", "nomic-embed-text": "embeddings", "moondream": "images"}


def r1(x):
    return None if x is None else round(x, 1)


def phase_window(journals: Path, label: str) -> tuple[str, str]:
    text = (journals / f"phase_{label}.log").read_text()
    start = re.search(rf"== {re.escape(label)} start (\S+)", text).group(1)
    end = re.search(rf"== {re.escape(label)} done (\S+)", text).group(1)
    return start, end


def run_summary(report: dict) -> dict:
    ok = [q for q in report["requests"] if q["success"]]
    answered = collections.Counter()
    by_node = collections.Counter()
    for q in ok:
        kind = next(v for k, v in MODELS.items() if q["model"].startswith(k))
        answered[kind] += 1
        by_node[(kind, q["node_id"])] += 1
    return {
        "rep": (report.get("manifest") or {}).get("repetition"),
        "sent": len(report["requests"]),
        "failed": len(report["requests"]) - len(ok),
        "chat_tokens_per_sec": report["tokens_per_sec"],
        "ttft_p95_ms": report["ttft_p95_ms"],
        "answered": dict(answered),
        "answered_by_node": {f"{k}@{n}": v for (k, n), v in sorted(by_node.items())},
    }


def medians(runs: list[dict]) -> dict:
    def med(xs):
        return r1(percentile(xs, 50))

    out = {
        "chat_tokens_per_sec": med([r["chat_tokens_per_sec"] for r in runs]),
        "ttft_p95_ms": med([r["ttft_p95_ms"] for r in runs]),
        "failed": sum(r["failed"] for r in runs),
    }
    for kind in MODELS.values():
        out[f"{kind}_answered"] = med([r["answered"].get(kind, 0) for r in runs])
    return out


def versions(raw: Path, pattern: str, labels: dict[str, str], extra=None) -> dict:
    out = {}
    for key, version in labels.items():
        runs = []
        for f in sorted((raw / "results").glob(pattern.format(key=key))):
            s = run_summary(json.loads(f.read_text()))
            if extra:
                s.update(extra(s))
            runs.append(s)
        out[version] = {"runs": runs, "median": medians(runs)}
    return out


def images_on(nodes):
    def f(s):
        return {
            "images_on_" + "_".join(nodes): sum(
                v
                for k, v in s["answered_by_node"].items()
                if k.startswith("images@") and k.split("@")[1] in nodes
            )
        }

    return f


def embeddings_on(node):
    def f(s):
        return {f"embeddings_on_{node}": s["answered_by_node"].get(f"embeddings@{node}", 0)}

    return f


def occupancy(journal_lines: list[str], start: str, end: str) -> dict[int, float]:
    """Share of the window (skipping 20 s of warm-up, 5 s of tail) at each in-flight count."""

    def secs(t):
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    t0, t1 = secs(start) + 20, secs(end) - 5
    events = []
    for line in journal_lines:
        m = re.match(r"\S+T(\d\d:\d\d:\d\d\.\d+)", line)
        if not m:
            continue
        ts = secs(m.group(1))
        if not secs(start) <= ts <= secs(end):
            continue  # this run only: counting from the journal's start drifts
        if "processing task" in line and "is_child = 0" in line:
            events.append((ts, 1))
        elif "stop processing" in line:
            events.append((ts, -1))
    events.sort()
    n, last, share = 0, None, collections.Counter()
    for ts, d in events:
        if last is not None and t0 <= ts <= t1:
            share[n] += ts - max(last, t0)
        n, last = max(0, n + d), ts
    total = sum(share.values()) or 1
    return {k: round(v / total, 3) for k, v in sorted(share.items())}


def slot_occupancy(run5: Path) -> dict:
    journals = run5 / "journals"
    lines = {}
    for m in ("m1", "m2", "m3", "m4"):
        with gzip.open(journals / f"{m}-ollama.log.gz", "rt") as f:
            lines[m] = f.readlines()
    out = {}
    for arm, label in (("herd", "tie_herd"), ("haproxy_roundrobin", "tie_haproxy_rr")):
        reps = []
        for rep in (1, 2, 3):
            start, end = phase_window(journals, f"{label}_r{rep}")
            reps.append({m: occupancy(lines[m], start, end) for m in lines})
        # Mean share per in-flight count, over every node and run.
        counts = sorted({k for r in reps for node in r.values() for k in node})
        mean = {
            k: round(
                sum(node.get(k, 0) for r in reps for node in r.values()) / (len(reps) * len(lines)),
                3,
            )
            for k in counts
        }
        out[arm] = {"per_run": reps, "mean_share": mean}
    return out


def main(combined: Path, stranding: Path, run5: Path) -> None:
    out = {
        "date": "2026-11-21",
        "machines": "Lambda Cloud us-east-1: NVIDIA A10 24 GB ($1.29/h) and A100 SXM4 40 GB "
        "($1.99/h); Ubuntu 22.04; one private network",
        "ollama": "0.40.17 for combined and stranding, 0.40.16 for slot_occupancy (run 5); "
        "OLLAMA_NUM_PARALLEL=4, OLLAMA_CONTEXT_LENGTH=4096 everywhere",
        "workload": "qwen3-coder:30b (about 1,250 prompt and 200 completion tokens), "
        "nomic-embed-text and moondream (an image question per request), four workers each, "
        "180 s, --skip-warmup, started right after a reset: m1 and m2 the coder, m5 "
        "nomic-embed-text and moondream",
        "telemetry": "off on every router and node",
        "combined": {
            "what": "router and harness on a fourth A10 with no models; 0.12.11 from PyPI and "
            "OSS 213cdb7 (both 0.12.12 fixes) alternating, three runs each",
            **versions(
                combined,
                "v_{key}_std_r*.json",
                {"0.12.11": "0.12.11", "fix": "0.12.12"},
                images_on(["m1", "m2"]),
            ),
        },
        "stranding": {
            "what": "nomic-embed-text primed onto an A10 through the router (six requests while "
            "only the A10s could take it), then loaded on the A100 directly; the first version of "
            "the embedding fix (4c3b7f1, the same scorer change as 0.12.12) against 0.12.11, two "
            "runs each; the router shared m1 with a node",
            **versions(
                stranding,
                "v_{key}_forced_r*.json",
                {"0.12.11": "0.12.11", "fix": "0.12.12"},
                embeddings_on("m5"),
            ),
        },
        "slot_occupancy": {
            "what": "run 5 identical machines: four A10, qwen3-coder:30b, 8 concurrent for 120 s, "
            "Herd 0.12.11 against HAProxy 2.4 round robin, three runs each; share of each run "
            "a node spent with that many requests in Ollama",
            **slot_occupancy(run5),
        },
    }
    path = HERE / "release_0_12_12_check.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(*(Path(a) for a in sys.argv[1:4]))
