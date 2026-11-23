"""Recompute the throughput figures runs 2, 4 and 5 and the 0.12.12 check published under the
old rate definition, which divided work that finished after the load window by the window.

Writes ``legacy_rate_corrections.json`` next to this script. Each figure gets one of three bases:

- ``records``: the arm kept every request, so the in-window figure is exact, by the engine's own
  ``rate_window`` (closed loop: requests that finished inside the window, over the window; open
  loop: answers to requests sent in the window, over the span from the first send to the last
  of those answers).
- ``rule``: a closed-loop, one-model arm in which every request ran to ``max_tokens``. When the
  window closes each of the ``C`` workers has exactly one request in flight, and the drain answers
  it, so the in-window figure is ``(answered - C) x max_tokens / window``. Run 6, which kept both
  figures, has drained = C in every closed-loop cell, and the arms here that kept records agree
  (run 4's failure arm: 8 drained at C = 8, 281.7 -> 275.0 exactly as the rule gives).
- ``estimate``: a closed-loop mix without records. Each chat model's ``C`` workers are taken to
  have one request in the drain at that model's mean answer length. Treat it as approximate.

The 0.12.12 check keeps only per-run totals; its figures get an ``estimate`` from the four
coding workers' 200-token requests.

    uv run python docs/experiments/legacy-rate-corrections.py
"""

from __future__ import annotations

import json
from pathlib import Path

from fleet_manager.server.benchmark_engine import CHAT_WORKLOADS, rate_window
from fleet_manager.server.model_knowledge import model_type

HERE = Path(__file__).parent
MAX_TOKENS = 200  # every run here used --max-tokens 200 for the coder
WINDOW_RUN2 = 120.0


def is_chat(model: str) -> bool:
    return model_type(model).value in CHAT_WORKLOADS


def from_records(report: dict) -> tuple[float, int]:
    records = report["requests"]
    window = report["duration_s"]
    if (report.get("settings") or {}).get("rate") or report.get("_open_loop"):
        # The summaries drop ``schedule_lag_ms``, which is how rate_window tells an
        # open loop; restore the marker.
        records = [{**r, "schedule_lag_ms": 0.0} for r in records]
    rw = rate_window(records, window)
    tokens = sum(
        r.get("completion_tokens") or 0
        for r, c in zip(records, rw.counted, strict=True)
        if c and is_chat(r["model"])
    )
    drained = sum(1 for r, c in zip(records, rw.counted, strict=True) if r["success"] and not c)
    return round(rw.per_sec(tokens), 1), drained


def from_rule(answered: int, concurrency: int, window: float) -> float:
    return round((answered - concurrency) * MAX_TOKENS / window, 1)


def from_estimate(report: dict) -> float:
    s = report["settings"]
    window = report["duration_s"]
    drained_tokens = 0.0
    for model, m in report["per_model"].items():
        if is_chat(model) and m["answered"]:
            drained_tokens += s["concurrency"] * m["completion_tokens"] / m["answered"]
    return round(report["chat_tokens_per_sec"] - drained_tokens / window, 1)


def correct_report(report: dict, open_loop: bool = False) -> dict:
    """One saved report (an arm or a repetition) with ``chat_tokens_per_sec``."""
    old = report["chat_tokens_per_sec"]
    if report.get("requests"):
        new, drained = from_records({**report, "_open_loop": open_loop})
        return {"old": old, "in_window": new, "basis": "records", "drained": drained}
    s = report.get("settings") or {}
    chat = [m for m in report["per_model"] if is_chat(m)]
    one_model_full = len(report["per_model"]) == 1 and all(
        m["completion_tokens"] == m["answered"] * MAX_TOKENS for m in report["per_model"].values()
    )
    concurrency = s.get("concurrency")
    if one_model_full and concurrency and not s.get("rate"):
        answered = report["per_model"][chat[0]]["answered"]
        return {
            "old": old,
            "in_window": from_rule(answered, concurrency, report["duration_s"]),
            "basis": "rule",
            "drained": concurrency,
        }
    return {"old": old, "in_window": from_estimate(report), "basis": "estimate"}


def walk_arms(node, path: str, out: dict, open_loop: bool = False, settings=None):
    """Every saved report under ``node``; run 5 keeps ``settings`` on the arm, not
    on each repetition, so the nearest one above is carried down."""
    if isinstance(node, dict):
        settings = node.get("settings") or settings
        if "chat_tokens_per_sec" in node and "duration_s" in node and "per_model" in node:
            out[path] = correct_report({**node, "settings": settings or {}}, open_loop)
            return
        for k, v in node.items():
            walk_arms(v, f"{path}/{k}", out, open_loop or k.startswith("rate_"), settings)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk_arms(v, f"{path}[{i}]", out, open_loop, settings)


def main() -> None:
    result: dict = {
        "what": __doc__.split("\n\n")[0].replace("\n", " "),
        "rule_check": {},
    }

    # Run 2 (0.12.10): divided by the elapsed time, drain included; every request 200 tokens.
    run2 = json.loads((HERE / "fleet_scaling_results.json").read_text())
    r2 = {}
    for arm, cells in run2["arms"].items():
        for cell, rep in cells.items():
            c = int(cell[1:])
            r2[f"{arm}/{cell}"] = {
                "old": rep["tokens_per_sec"],
                "in_window": from_rule(rep["total_requests"], c, WINDOW_RUN2),
                "basis": "rule",
                "drained": c,
            }
    result["fleet_scaling_results.json"] = r2

    for name in ("fleet_run4_results.json", "fleet_run5_results.json"):
        run = json.loads((HERE / name).read_text())
        out: dict = {}
        for cell_i, cell in enumerate(run.get("scaling", {}).get("cells", [])):
            for rep in cell["reps"]:
                label = f"machines={cell['machines']} c={cell['concurrency']} rep={rep['rep']}"
                out[f"/scaling/cells[{cell_i}] {label}"] = {
                    "old": rep["tokens_per_sec"],
                    "in_window": from_rule(rep["requests"], cell["concurrency"], 120.0),
                    "basis": "rule",
                    "drained": cell["concurrency"],
                }
        for key, section in run.items():
            if key in ("scaling",):
                continue
            walk_arms(section, f"/{key}", out)
        result[name] = out

    # The mixed workload (0.12.10, same harness as run 2): the coder's rate divided its 200-token
    # answers by the elapsed time, drain included. Four workers per model; the rule applies to the
    # coder alone, but the other models shared its machines, so treat it as an estimate.
    mixed = json.loads((HERE / "mixed_workload_results.json").read_text())
    out = {}
    for arm, rep in mixed["arms"].items():
        for pm in rep["per_model_results"]:
            if pm["model"] == "qwen3-coder:30b":
                out[f"/arms/{arm}/qwen3-coder:30b"] = {
                    "old": pm["tok_s"],
                    "in_window": from_rule(pm["requests"], 4, 180.0),
                    "basis": "estimate",
                    "drained": 4,
                }
    result["mixed_workload_results.json"] = out

    check = json.loads((HERE / "release_0_12_12_check.json").read_text())
    window = 180.0
    coding_drain = 4 * MAX_TOKENS / window
    out = {}
    for section in ("combined", "stranding"):
        for version, v in check[section].items():
            for i, run in enumerate(v.get("runs", []) if isinstance(v, dict) else []):
                out[f"/{section}/{version}/runs[{i}]"] = {
                    "old": run["chat_tokens_per_sec"],
                    "in_window": round(run["chat_tokens_per_sec"] - coding_drain, 1),
                    "basis": "estimate",
                }
    result["release_0_12_12_check.json"] = out

    # The rule against the arms that kept records (closed loop): it must match exactly.
    for name in ("fleet_run4_results.json", "fleet_run5_results.json"):
        for path, fig in result[name].items():
            if fig["basis"] == "records" and "rate_" not in path:
                result["rule_check"][f"{name}{path}"] = {"drained": fig["drained"]}

    (HERE / "legacy_rate_corrections.json").write_text(json.dumps(result, indent=1) + "\n")
    for name, figs in result.items():
        if isinstance(figs, dict) and name.endswith(".json"):
            print(f"== {name}")
            for path, f in figs.items():
                print(f"  {f['old']:7.1f} -> {f['in_window']:7.1f}  {f['basis']:8s} {path}")
    print("rule check (closed-loop arms with records):", result["rule_check"])


if __name__ == "__main__":
    main()
