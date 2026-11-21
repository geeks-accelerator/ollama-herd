"""docs/experiments/fleet-summarize.py on small synthetic reports.

The reports have the fields ``scripts/benchmark.py --output`` writes that the
summarizer reads; nothing here depends on a real run's raw files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_experiments import EXPERIMENTS, load_experiment

fs = load_experiment("fleet-summarize.py", "fleet_summarize")

RTX6000 = "AMD EPYC-Rome Processor + Quadro RTX 6000"
A6000 = "AMD EPYC-Rome Processor + NVIDIA RTX A6000"
PRICES = {"Quadro RTX 6000": 0.69, "RTX A6000": 1.09}


def req(model="coder", kind="llm", ttft=1000.0, ok=True, node="m1", started=0.0, tokens=200):
    return {
        "started_ms": started,
        "model": model,
        "model_type": kind,
        "node_id": node,
        "success": ok,
        "error": None if ok else "connection lost",
        "ttft_ms": ttft,
        "latency_ms": ttft + 5000,
        "completion_tokens": tokens if ok else 0,
    }


def report(
    requests,
    *,
    rep=None,
    seed=1,
    chips=(RTX6000,),
    concurrency=8,
    tps=100.0,
    start="2026-11-20T17:00:00.000+00:00",
    end="2026-11-20T17:04:00.000+00:00",
):
    ok = [q for q in requests if q["success"]]
    return {
        "duration_s": 240.0,
        "tokens_per_sec": tps,
        "total_requests": len(ok),
        "total_failures": len(requests) - len(ok),
        "cancelled_at_drain": 1,
        "ttft_p50_ms": 1.0,
        "ttft_p95_ms": 2.0,
        "ttft_p99_ms": 3.0,
        "per_node_results": [{"node_id": "m1", "requests": len(ok)}],
        "model_events": [{"event": "loaded", "model": "coder"}],
        "manifest": {
            "started_at": start,
            "ended_at": end,
            "seed": seed,
            "repetition": rep,
            "workload": {"concurrency": concurrency, "models": ["coder"]},
            "nodes": [{"node_id": f"m{i + 1}", "chip": c} for i, c in enumerate(chips)],
        },
        "requests": requests,
    }


def write(raw: Path, name: str, rep: dict) -> None:
    (raw / name).parent.mkdir(parents=True, exist_ok=True)
    (raw / name).write_text(json.dumps(rep))


class TestSpread:
    def test_median_and_range(self):
        assert fs.spread([3.0, 1.0, 2.0]) == {"median": 2.0, "min": 1.0, "max": 3.0}

    def test_skips_missing_samples(self):
        assert fs.spread([None, 4.0, 2.0]) == {"median": 3.0, "min": 2.0, "max": 4.0}
        assert fs.spread([None, None]) == {"median": None, "min": None, "max": None}

    def test_fields_cover_every_number(self):
        rows = [{"rep": 1, "a": 1, "b": None, "s": "x"}, {"rep": 2, "a": 3, "b": 5.0, "s": "y"}]
        assert fs.spread_fields(rows, skip=("rep",)) == {
            "a_median": 2.0,
            "a_min": 1,
            "a_max": 3,
            "b_median": 5.0,
            "b_min": 5.0,
            "b_max": 5.0,
        }


class TestArm:
    def test_first_token_counts_chat_requests_only(self):
        rows = [
            req(ttft=1000.0),
            req(ttft=2000.0),
            *[req("embed", "embed", ttft=50.0) for _ in range(20)],
            req("vision", "vision", ttft=0.0),  # answered with no first token measured
            req(ttft=9999.0, ok=False),
        ]
        a = fs.arm(report(rows), "herd", PRICES)
        assert (a["ttft_p50_ms"], a["ttft_p95_ms"]) == (1500.0, 1950.0)
        assert a["per_model"]["embed"]["ttft_p50_ms"] is None
        assert a["per_model"]["embed"]["answered"] == 20
        assert a["per_model"]["vision"]["ttft_p50_ms"] is None
        assert (a["sent"], a["answered"], a["failed"]) == (24, 23, 1)
        assert a["per_model"]["coder"]["failures"] == {"connection lost": 1}

    def test_price_and_tokens_per_dollar_from_the_machines(self):
        a = fs.arm(report([req()], chips=(RTX6000, RTX6000, A6000), tps=100.0), "herd", PRICES)
        assert a["usd_per_hour"] == 2.47
        assert a["tokens_per_usd"] == round(100.0 * 3600 / 2.47)

    @pytest.mark.parametrize("chip", ["Apple M3 Ultra", None])
    def test_unpriced_machine_fails(self, chip):
        with pytest.raises(SystemExit, match="price keys"):
            fs.arm(report([req()], chips=(chip,)), "herd", PRICES)

    def test_ambiguous_price_fails(self):
        with pytest.raises(SystemExit, match="price keys"):
            fs.arm(report([req()]), "herd", {**PRICES, "RTX 6000": 0.5})

    def test_price_key_matches_the_end_of_the_chip(self):
        """A10 and A100 share a prefix; a key names the GPU at the end of the chip string."""
        a10 = "Intel(R) Xeon(R) Platinum 8358 CPU @ 2.60GHz + NVIDIA A10"
        a100 = "AMD EPYC 7J13 64-Core Processor + NVIDIA A100-SXM4-40GB"
        prices = {"A10": 1.29, "A100-SXM4-40GB": 1.99}
        a = fs.arm(report([req()], chips=(a10, a10, a100), tps=100.0), "herd", prices)
        assert a["usd_per_hour"] == 4.57

    def test_requests_kept_only_when_asked(self):
        assert "requests" not in fs.arm(report([req()]), "herd", PRICES)
        kept = fs.arm(report([req()]), "herd", PRICES, keep_requests=True)
        assert set(kept["requests"][0]) == set(fs.REQUEST_KEYS)


def manifest(tmp_path: Path, sections: dict, **run) -> Path:
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "output": "out.json",
                "run": {"date": "2026-11-21", "prices_usd_per_hour": PRICES, **run},
                "sections": sections,
            }
        )
    )
    return path


def summarize(tmp_path: Path, raw: Path, sections: dict, **run) -> dict:
    return json.loads(fs.main(manifest(tmp_path, sections, **run), raw).read_text())


class TestRepetitions:
    def test_reports_are_ordered_by_repetition_and_spread(self, tmp_path):
        raw = tmp_path / "raw"
        # File names sort a, b, c; repetitions 3, 1, 2.
        for name, rep, tps in (("a", 3, 120.0), ("b", 1, 100.0), ("c", 2, 90.0)):
            write(raw, f"b8_{name}.json", report([req(), req("embed", "embed")], rep=rep, tps=tps))
        out = summarize(tmp_path, raw, {"vision": {"files": "b8_*.json", "router": "herd"}})
        v = out["vision"]
        assert [r["rep"] for r in v["reps"]] == [1, 2, 3]
        assert [r["chat_tokens_per_sec"] for r in v["reps"]] == [100.0, 90.0, 120.0]
        assert (v["chat_tokens_per_sec_median"], v["chat_tokens_per_sec_min"]) == (100.0, 90.0)
        assert v["chat_tokens_per_sec_max"] == 120.0
        assert v["per_model"]["embed"]["answered_median"] == 1.0
        assert v["per_model"]["embed"]["ttft_p50_ms_median"] is None
        assert v["settings"] == {"concurrency": 8, "models": ["coder"]}
        assert "settings" not in v["reps"][0] and v["reps"][0]["seed"] == 1

    def test_a_model_missing_from_a_repetition_counts_as_unasked(self, tmp_path):
        raw = tmp_path / "raw"
        write(raw, "x_r1.json", report([req(), req("img", "vision", ttft=40.0)], rep=1))
        write(raw, "x_r2.json", report([req()], rep=2))
        out = summarize(tmp_path, raw, {"x": {"files": "x_r*.json", "router": "herd"}})
        img = out["x"]["per_model"]["img"]
        assert (img["asked_min"], img["asked_max"], img["ttft_p50_ms_median"]) == (0, 1, 40.0)

    def test_one_report_keeps_bare_figures(self, tmp_path):
        raw = tmp_path / "raw"
        write(raw, "one.json", report([req()]))  # repetition None: a single run
        out = summarize(
            tmp_path, raw, {"g": {"what": "text", "a": {"files": "one.json", "router": "r"}}}
        )
        assert out["g"]["what"] == "text"
        assert out["g"]["a"]["chat_tokens_per_sec"] == 100.0
        assert "reps" not in out["g"]["a"]

    @pytest.mark.parametrize("reps", [(None, 1), (2, 2)])
    def test_several_reports_need_distinct_repetitions(self, tmp_path, reps):
        raw = tmp_path / "raw"
        for i, rep in enumerate(reps):
            write(raw, f"x{i}.json", report([req()], rep=rep))
        with pytest.raises(SystemExit, match="distinct repetitions"):
            summarize(tmp_path, raw, {"x": {"files": "x*.json", "router": "r"}})

    def test_repetitions_must_share_a_workload(self, tmp_path):
        raw = tmp_path / "raw"
        write(raw, "x1.json", report([req()], rep=1, concurrency=4))
        write(raw, "x2.json", report([req()], rep=2, concurrency=8))
        with pytest.raises(SystemExit, match="different workloads"):
            summarize(tmp_path, raw, {"x": {"files": "x*.json", "router": "r"}})

    def test_a_glob_that_matches_nothing_fails(self, tmp_path):
        (tmp_path / "raw").mkdir()
        with pytest.raises(SystemExit, match="matches no file"):
            summarize(tmp_path, tmp_path / "raw", {"x": {"files": "nope*.json", "router": "r"}})


class TestEvents:
    def spec(self, events):
        return {"failure": {"files": "b6_r*.json", "router": "herd", "events": events}}

    def raw(self, tmp_path):
        raw = tmp_path / "raw"
        write(
            raw,
            "b6_r1.json",
            report(
                [req()],
                rep=1,
                start="2026-11-21T01:00:00.000+00:00",
                end="2026-11-21T01:04:00.000+00:00",
            ),
        )
        write(
            raw,
            "b6_r2.json",
            report(
                [req()],
                rep=2,
                start="2026-11-21T01:10:00.500+00:00",
                end="2026-11-21T01:14:00.000+00:00",
            ),
        )
        return raw

    def test_kill_offsets_are_seconds_from_each_run_start(self, tmp_path):
        events = [
            {"at": "2026-11-21T01:11:00.700+00:00", "what": "stopped Ollama on m3"},
            {"at": "2026-11-21T01:01:05.123+00:00", "what": "stopped Ollama on m3"},
            {"at": "2026-11-21T01:12:30.000+00:00", "what": "m4 gone"},
        ]
        out = summarize(tmp_path, self.raw(tmp_path), self.spec(events))
        r1, r2 = out["failure"]["reps"]
        assert [e["t_s"] for e in r1["events"]] == [65.1]
        assert [(e["t_s"], e["what"]) for e in r2["events"]] == [
            (60.2, "stopped Ollama on m3"),
            (149.5, "m4 gone"),
        ]

    def test_single_run_events_sit_on_the_arm(self, tmp_path):
        raw = tmp_path / "raw"
        write(raw, "b6.json", report([req()]))
        spec = {
            "failure": {
                "files": "b6.json",
                "router": "r",
                "events": [{"at": "2026-11-20T17:01:05+00:00", "what": "kill"}],
            }
        }
        out = summarize(tmp_path, raw, spec)
        assert out["failure"]["events"] == [
            {"t_s": 65.0, "at": "2026-11-20T17:01:05+00:00", "what": "kill"}
        ]

    def test_event_outside_every_run_fails(self, tmp_path):
        events = [
            {"at": "2026-11-21T01:01:00+00:00", "what": "a"},
            {"at": "2026-11-21T01:11:00+00:00", "what": "b"},
            {"at": "2026-11-21T02:00:00+00:00", "what": "late"},
        ]
        with pytest.raises(SystemExit, match="in no report's run window"):
            summarize(tmp_path, self.raw(tmp_path), self.spec(events))

    def test_a_run_without_its_events_fails(self, tmp_path):
        with pytest.raises(SystemExit, match="b6_r1.json has no events"):
            summarize(tmp_path, self.raw(tmp_path), self.spec([]))

    def test_event_time_needs_an_offset(self, tmp_path):
        with pytest.raises(SystemExit, match="no UTC offset"):
            summarize(
                tmp_path,
                self.raw(tmp_path),
                self.spec([{"at": "2026-11-21T01:01:00", "what": "a"}]),
            )


class TestScaling:
    def test_cells_group_by_machines_and_concurrency(self, tmp_path):
        raw = tmp_path / "raw"
        for rep in (2, 1):
            rows = [req(ttft=100.0 * rep), req(ttft=300.0), req(ok=False)]
            write(raw, f"b1/r{rep}_n1.json", report(rows, rep=rep, tps=50.0 * rep))
            write(
                raw,
                f"b1/r{rep}_n2.json",
                report([req()], rep=rep, chips=(RTX6000, RTX6000), concurrency=16),
            )
        spec = {"scaling": {"router": "herd", "model": "coder", "cells": "b1/*.json"}}
        out = summarize(tmp_path, raw, spec)["scaling"]
        assert (out["router"], out["model"], out["repetitions"]) == ("herd", "coder", 2)
        one, two = out["cells"]
        assert (one["machines"], one["concurrency"], two["machines"], two["concurrency"]) == (
            1,
            8,
            2,
            16,
        )
        assert [r["rep"] for r in one["reps"]] == [1, 2]
        assert [r["ttft_ms"] for r in one["reps"]] == [[100.0, 300.0], [200.0, 300.0]]
        assert one["reps"][0]["failed"] == 1  # total_failures; drained are already in it
        assert (one["tokens_per_sec_median"], one["tokens_per_sec_max"]) == (75.0, 100.0)
        assert (one["usd_per_hour_median"], two["usd_per_hour_median"]) == (0.69, 1.38)
        assert one["reps"][0]["tokens_per_usd"] == round(50.0 * 3600 / 0.69)
        assert "rep_median" not in one


class TestMain:
    def test_run_facts_lead_and_unfilled_ones_fail(self, tmp_path):
        raw = tmp_path / "raw"
        write(raw, "a.json", report([req()]))
        sections = {"a": {"files": "a.json", "router": "r"}}
        out = summarize(tmp_path, raw, sections, ollama="0.40.16")
        assert list(out)[:3] == ["date", "prices_usd_per_hour", "ollama"]
        with pytest.raises(SystemExit, match="fill in run.ollama"):
            summarize(tmp_path, raw, sections, ollama=None)

    def test_number_lists_print_on_one_line(self):
        text = fs.dumps({"ttft_ms": [1.5, 20.0, 3.0], "nested": {"ttft_ms": []}, "s": ["a", "b"]})
        assert '"ttft_ms": [1.5, 20.0, 3.0]' in text
        assert json.loads(text)["s"] == ["a", "b"]


def arms(node):
    if isinstance(node, dict):
        if "files" in node or "cells" in node:
            yield node
        else:
            for v in node.values():
                yield from arms(v)


@pytest.mark.parametrize("name", ["fleet_run4_manifest.json", "fleet_run5_manifest.json"])
def test_committed_manifests_are_well_formed(name):
    m = json.loads((EXPERIMENTS / name).read_text())
    assert set(m) == {"output", "run", "sections"}
    assert m["run"]["prices_usd_per_hour"] == PRICES
    specs = list(arms(m["sections"]))
    assert specs
    for spec in specs:
        assert "cells" in spec or spec["router"]
        for ev in spec.get("events", []):
            fs.parse_time(ev["at"])
