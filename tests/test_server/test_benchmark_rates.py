"""One in-window throughput definition (``rate_window``) and per-cell seeds.

Review finding A3: ``build_report`` divided every completed request, drain
included, by the load window at six sites, so past saturation throughput rose
with concurrency.  Findings item 35 (C2): every cell of a run shared one seed,
so a concurrency-8 cell replayed the concurrency-4 cell's first four workers.
"""

from __future__ import annotations

import pytest

from fleet_manager.server.benchmark_engine import (
    RATE_BASIS,
    RequestResult,
    build_report,
    cell_seed,
    dashboard_record,
    rate_window,
    report_chat_tokens_per_sec,
)


def _r(started, latency, *, model="m", node="a", kind="llm", tokens=100, ok=True, lag=None):
    return RequestResult(
        model,
        node,
        latency,
        latency / 2,
        10,
        tokens if ok else 0,
        ok,
        model_type=kind,
        started_ms=started,
        schedule_lag_ms=lag,
    )


class TestClosedLoop:
    # A 10 s load window: two answered inside it, one answered in the drain.
    RESULTS = [
        _r(0, 4_000, model="m", node="a"),
        _r(1_000, 9_000, model="n", node="b"),  # ends at 10 s exactly: counts
        _r(8_000, 6_000, model="m", node="b", tokens=900),  # ends at 14 s: drain
        _r(0, 500, kind="embed", model="e", node="a", tokens=1),
        _r(0, 1_000, ok=False),
    ]

    def test_every_rate_site_leaves_the_drain_out(self):
        report = build_report(self.RESULTS, 10.0, [], [], [])
        assert report["rate_basis"] == RATE_BASIS == "window"
        assert report["rate_span_s"] == 10.0
        assert report["drained_completed"] == 1
        # Counts and tokens keep every answer ...
        assert report["total_requests"] == 4
        assert report["total_completion_tokens"] == 1100
        # ... rates only what finished inside the window.
        assert report["requests_per_sec"] == 0.3
        assert report["tokens_per_sec"] == 20.0
        by_type = {t["model_type"]: t for t in report["per_type_results"]}
        assert by_type["llm"]["requests"] == 3
        assert by_type["llm"]["requests_per_sec"] == 0.2
        assert by_type["llm"]["tok_s"] == 20.0
        assert by_type["embed"]["requests_per_sec"] == 0.1
        by_model = {m["model"]: m for m in report["per_model_results"]}
        assert by_model["m"]["requests"] == 2 and by_model["m"]["tok_s"] == 10.0
        assert by_model["n"]["tok_s"] == 10.0
        by_node = {n["node_id"]: n for n in report["per_node_results"]}
        assert by_node["b"]["tokens"] == 1000 and by_node["b"]["tok_s"] == 10.0
        assert by_node["a"]["tok_s"] == 10.0

    def test_past_saturation_more_concurrency_no_longer_reads_faster(self):
        """Run 6's shape: twice the workers, the same machines, the extra
        requests all finishing in the drain.  The old figure rose; this holds."""
        base = [_r(i * 1_000, 5_000) for i in range(6)]  # all end by 10 s
        doubled = base + [_r(9_000, 20_000, tokens=100) for _ in range(6)]
        a = build_report(base, 10.0, [], [], [])["tokens_per_sec"]
        b = build_report(doubled, 10.0, [], [], [])["tokens_per_sec"]
        assert a == b == 60.0

    def test_stored_with_the_dashboard_copy(self):
        """No column for it, so it rides in ``fleet_snapshot``: a stored run
        without it is legacy."""
        snap = dashboard_record(build_report(self.RESULTS, 10.0, [], [], []))["fleet_snapshot"]
        assert snap["rate_basis"] == "window" and snap["drained_completed"] == 1
        assert snap["rate_span_s"] == 10.0


class TestOpenLoop:
    def test_counts_every_answer_sent_in_the_window_over_the_send_to_answer_span(self):
        # Arrivals over 3 s at 1/s; the last answer comes 6 s after the first send.
        results = [_r(i * 1_000, 4_000, lag=0.1) for i in range(3)]
        rw = rate_window([r.__dict__ for r in results], 3.0)
        assert rw.counted == (True, True, True)
        assert rw.span_s == 6.0
        report = build_report(results, 3.0, [], [], [])
        assert report["drained_completed"] == 0
        assert report["rate_span_s"] == 6.0
        assert report["requests_per_sec"] == 0.5  # not 3 / 3.0, not 1 / 3.0
        assert report["tokens_per_sec"] == 50.0

    def test_failures_and_nothing_answered(self):
        results = [_r(0, 1_000, ok=False, lag=0.0)]
        report = build_report(results, 2.0, [], [], [])
        assert report["requests_per_sec"] == 0 and report["rate_span_s"] == 2.0


class TestSavedReports:
    def test_a_legacy_report_gets_the_in_window_figure(self):
        report = {
            "duration_s": 10.0,
            "tokens_per_sec": 999.0,  # legacy: the drain counted
            "requests": [
                {"started_ms": 0, "latency_ms": 5_000, "success": True, "completion_tokens": 200},
                {"started_ms": 9_000, "latency_ms": 5_000, "success": True,
                 "completion_tokens": 300},
                {"started_ms": 0, "latency_ms": 1_000, "success": True, "model_type": "embed",
                 "completion_tokens": 1},
            ],
        }  # fmt: skip
        assert report_chat_tokens_per_sec(report) == 20.0

    def test_no_duration_is_none(self):
        assert report_chat_tokens_per_sec({"duration_s": 0, "requests": []}) is None

    def test_matches_build_report(self):
        report = build_report(TestClosedLoop.RESULTS, 10.0, [], [], [])
        assert report_chat_tokens_per_sec(report) == report["tokens_per_sec"]


class TestCellSeed:
    def test_cells_of_one_run_differ(self):
        seeds = {cell_seed(7, "llm", c, None, rep) for c in (1, 2, 4, 8, 16) for rep in (1, 2, 3)}
        assert len(seeds) == 15

    @pytest.mark.parametrize(
        "other",
        [
            (8, "llm", 4, None, 1),
            (7, "embed", 4, None, 1),
            (7, "llm", 8, None, 1),
            (7, "llm", 4, 2.0, 1),
            (7, "llm", 4, None, 2),
        ],
    )
    def test_every_coordinate_matters(self, other):
        assert cell_seed(7, "llm", 4, None, 1) != cell_seed(*other)

    def test_repeatable_and_in_range(self):
        """A pinned value: a change to the derivation would silently make every
        published run unrepeatable from its run seed."""
        s = cell_seed(7, "llm", 4, None, 1)
        assert s == cell_seed(7, "llm", 4, None, 1)
        assert 0 <= s < 2**31
        assert s == PINNED_CELL_SEED


PINNED_CELL_SEED = 557392763
