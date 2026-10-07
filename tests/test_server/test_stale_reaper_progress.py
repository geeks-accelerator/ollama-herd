"""The stale reaper must key on progress, not elapsed time.

On 2026-10-06 it reaped a request that was mid-stream and 75 s from finishing;
that request then completed normally with 9,610 tokens. Two things broke:

* its concurrency slot was released while it still held one, so herd ran 5 real
  in-flight against a cap of 4 -- newly consequential, since enforcement only
  started binding on 2026-10-02;
* the queue recorded `failed=1` for a request the trace store calls `completed`.

The reaper only had `started_at`, so it structurally could not tell a slow
stream from a dead one. That distinction matters more as load rises: gpt-oss
decode fell 76 -> 24 tok/s here, so the same 8,000-token reply went from ~106 s
to ~330 s of entirely healthy work, walking toward a fixed 600 s limit.

Both directions are pinned below. A reaper that stops false-positiving by never
firing would be worse than the bug.
"""

import time

import pytest

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestStatus
from fleet_manager.server import queue_manager as qm


def _entry(started_ago: float, progress_ago: float | None):
    now = time.time()
    e = QueueEntry(
        request=InferenceRequest(model="gpt-oss:120b", prompt="x"),
        status=RequestStatus.IN_FLIGHT,
        assigned_node="bb",
    )
    e.started_at = now - started_ago
    e.last_progress_at = None if progress_ago is None else now - progress_ago
    return e


def _is_stale(entry, timeout: float) -> bool:
    """The reaper's predicate, as the production loop evaluates it."""
    now = time.time()
    last = entry.last_progress_at or entry.started_at
    return bool(last and (now - last) > timeout)


class TestTheReportedFalsePositive:
    def test_a_long_but_actively_streaming_request_is_not_stale(self):
        """The exact shape of the 2026-10-06 incident.

        Running 645 s, last chunk 2 s ago. Under the old age-based test this was
        reaped; it went on to return 9,610 tokens.
        """
        assert _is_stale(_entry(started_ago=645, progress_ago=2), 600.0) is False

    def test_even_a_twelve_minute_generation_survives_if_it_is_producing(self):
        assert _is_stale(_entry(started_ago=720, progress_ago=5), 600.0) is False

    def test_the_old_age_based_test_would_have_killed_it(self):
        """Documents what changed, so nobody reverts it as redundant."""
        e = _entry(started_ago=645, progress_ago=2)
        old_verdict = (time.time() - e.started_at) > 600.0
        assert old_verdict is True, "the old predicate fired on this"
        assert _is_stale(e, 600.0) is False, "the new one does not"


class TestGenuineZombiesAreStillCaught:
    def test_a_stream_that_never_produced_anything_is_reaped_on_age(self):
        """The case the reaper exists for: stream never consumed, no mark_* runs.

        last_progress_at is None, so it falls back to started_at.
        """
        assert _is_stale(_entry(started_ago=700, progress_ago=None), 600.0) is True

    def test_a_stream_that_stalled_mid_flight_is_reaped(self):
        """Produced output once, then went silent. Age is irrelevant here."""
        assert _is_stale(_entry(started_ago=5000, progress_ago=900), 600.0) is True

    def test_just_under_the_threshold_is_not_reaped(self):
        assert _is_stale(_entry(started_ago=10_000, progress_ago=599), 600.0) is False


class TestWiring:
    def test_both_streaming_loops_stamp_progress(self):
        """Two `async for chunk` loops exist; the retry path is the easy one to miss."""
        import pathlib

        src = pathlib.Path("src/fleet_manager/server/streaming.py").read_text()
        assert src.count("entry.last_progress_at = time.time()") == 2, (
            "every streaming loop must stamp progress, including the retry path"
        )
        assert src.count("async for chunk in self.stream_from_node") == 2

    def test_the_reaper_reads_progress_not_just_age(self):
        import pathlib

        src = pathlib.Path("src/fleet_manager/server/queue_manager.py").read_text()
        assert "e.last_progress_at or e.started_at" in src
        assert "now - e.started_at) > self._stale_timeout" not in src, (
            "age-based staleness reintroduced"
        )

    def test_the_fallback_constant_matches_the_setting(self):
        """They drifted: the constant read 900 with a "15 minutes" comment while
        ServerSettings.stale_timeout=600 always won, so the comment was wrong."""
        from fleet_manager.models.config import ServerSettings

        assert ServerSettings().stale_timeout == qm._STALE_NO_PROGRESS_SECONDS

    def test_reaper_events_record_both_idle_and_age(self):
        """The difference between them is the diagnosis.

        idle ~= age means it never produced anything; idle << age means it
        stalled mid-stream. One number cannot say which.
        """
        import pathlib

        src = pathlib.Path("src/fleet_manager/server/queue_manager.py").read_text()
        for field in ('"stuck_seconds"', '"age_seconds"', '"produced_output"'):
            assert field in src, f"{field} missing from reaper events"


@pytest.mark.asyncio
class TestEntryDefaults:
    async def test_last_progress_at_defaults_to_none(self):
        """None must mean "nothing yet", so age-fallback applies to new entries."""
        e = QueueEntry(request=InferenceRequest(model="m", prompt="p"))
        assert e.last_progress_at is None


class TestTheWorkerUsesTheSameRule:
    """Fixing one timeout and leaving the other just moves the bug.

    The worker's belt-and-braces wait was `stale_timeout + interval`, sized to
    fire 60 s *after* the reaper so the reaper always acted first and this path
    stayed dead code. Making the reaper progress-aware removed its false
    positives and promoted this one: on 2026-10-06 20:42 and 2026-10-07 02:42 it
    released the slots of two requests still mid-stream, which went on to return
    6,557 and 12,809 tokens. Both warnings appeared only *after* the reaper fix
    deployed, which is how it was caught.
    """

    def test_the_worker_no_longer_waits_a_single_fixed_timeout(self):
        import pathlib as _p

        src = _p.Path("src/fleet_manager/server/queue_manager.py").read_text()
        assert "timeout=self._stale_timeout + _REAPER_INTERVAL_SECONDS" not in src, (
            "a fixed worker timeout re-creates the bug the reaper fix removed"
        )

    def test_the_worker_checks_progress_before_releasing(self):
        import pathlib as _p

        src = _p.Path("src/fleet_manager/server/queue_manager.py").read_text()
        worker = src[src.index("Belt and braces for a request"):]
        worker = worker[: worker.index("def mark_completed")]
        assert "entry.last_progress_at" in worker
        assert "entry.started_at" in worker, "must fall back to age when nothing produced"
        assert "idle > self._stale_timeout" in worker

    def test_both_release_paths_share_one_threshold(self):
        """Two paths with two thresholds is what let this hide for a day.

        Neither may carry its own number.
        """
        import pathlib as _p

        src = _p.Path("src/fleet_manager/server/queue_manager.py").read_text()
        # the reaper's test and the worker's test must both read _stale_timeout
        assert src.count("self._stale_timeout") >= 2
        assert "_stale_timeout +" not in src, "no path may offset the shared threshold"

    def test_the_worker_wakes_on_the_reaper_cadence(self):
        """It polls rather than sleeping through the whole budget, so a long
        generation is re-checked instead of judged once."""
        import pathlib as _p

        src = _p.Path("src/fleet_manager/server/queue_manager.py").read_text()
        worker = src[src.index("Belt and braces for a request"):]
        worker = worker[: worker.index("def mark_completed")]
        assert "timeout=_REAPER_INTERVAL_SECONDS" in worker
        assert "while True" in worker
