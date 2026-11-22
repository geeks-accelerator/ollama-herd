"""The rebalancer's move and pre-warm *decisions*, which had no tests of their own.

`docs/issues.md` item 10 listed `server/rebalancer.py` as untested; the triage on
2026-10-13 narrowed that to "thin rather than zero" — it is exercised incidentally
by `test_streaming_failures.py` and `test_routes.py`, but nothing covered the
decisions, which is where the logic lives.

Both paths are gated by a chain of conditions that each silently skip. That shape
is why this needs direct tests: a wrong condition does not raise, it just means
nothing ever rebalances, and the symptom is a deep queue that stays deep.

Three conditions in particular are load-bearing and easy to get backwards:

* pre-warm must target the **runner-up**, not the winner — warming the winner
  does nothing, it already has the model;
* rebalance must only move to a node that already has the model **hot**,
  otherwise it trades a queue wait for a cold load;
* the pre-warm **lock** must prevent a second warm of the same `node:model`
  while one is in flight, and must be released even if the warm raises.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_manager.server.rebalancer import Rebalancer


def _settings(pre_warm_threshold=3, rebalance_threshold=5, pre_warm_min_availability=0.6):
    return SimpleNamespace(
        rebalance_interval=1,
        pre_warm_threshold=pre_warm_threshold,
        rebalance_threshold=rebalance_threshold,
        pre_warm_min_availability=pre_warm_min_availability,
    )


def _node(node_id, loaded=(), availability=None):
    return SimpleNamespace(
        node_id=node_id,
        ollama=SimpleNamespace(models_loaded=[SimpleNamespace(name=m) for m in loaded]),
        capacity=None if availability is None else SimpleNamespace(availability_score=availability),
    )


def _score(node_id, model, eviction=0.0):
    return SimpleNamespace(
        node_id=node_id,
        queue_key=f"{node_id}:{model}",
        score=1.0,
        scores_breakdown={"eviction": eviction},
    )


def _rebalancer(*, queue_info, scores, nodes, settings=None, depths=None, free=None):
    qm = MagicMock()
    # Free slots per target queue key (QueueManager.free_slots); 4 by default.
    qm.free_slots = MagicMock(side_effect=lambda key: (free or {}).get(key, 4))
    qm.get_queue_info = MagicMock(return_value=queue_info)
    qm.get_queue_depths = MagicMock(return_value=depths or {})
    qm.move_pending = AsyncMock(return_value=0)
    scorer = MagicMock()
    scorer.score_request = MagicMock(return_value=scores)
    registry = MagicMock()
    registry.get_node = MagicMock(side_effect=lambda nid: nodes.get(nid))
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock(return_value=None)
    return Rebalancer(settings or _settings(), registry, scorer, qm, proxy), qm, proxy


def _qinfo(key, model, node_id, pending, in_flight=0):
    return {key: {"model": model, "node_id": node_id, "pending": pending, "in_flight": in_flight}}


class TestPreWarmDecision:
    async def test_warms_the_runner_up_not_the_winner(self):
        """Warming the winner is a no-op: it is the winner because it has the model."""
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=5),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", [])},
        )
        await r._check_pre_warm()
        await asyncio.sleep(0.05)  # the warm runs as a logged task
        proxy.pre_warm.assert_awaited_once()
        assert proxy.pre_warm.await_args.args[0] == "b", "must warm the runner-up"

    async def test_never_evicts_to_pre_warm(self):
        """A speculative load must not push out a resident model (2026-11-20:
        qwen3.5:9b and qwen3-coder:30b were warmed over each other in turn)."""
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=5),
            scores=[_score("a", "m"), _score("b", "m", eviction=-40.0)],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["busy"])},
        )
        await r._check_pre_warm()
        await asyncio.sleep(0.05)
        proxy.pre_warm.assert_not_awaited()

    async def test_below_the_threshold_nothing_happens(self):
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=1),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", [])},
            settings=_settings(pre_warm_threshold=3),
        )
        await r._check_pre_warm()
        await asyncio.sleep(0.02)
        proxy.pre_warm.assert_not_awaited()

    async def test_depth_counts_in_flight_too(self):
        """2 pending + 2 in-flight is a depth of 4, not 2."""
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=2, in_flight=2),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", [])},
            settings=_settings(pre_warm_threshold=4),
        )
        await r._check_pre_warm()
        await asyncio.sleep(0.05)
        proxy.pre_warm.assert_awaited_once()

    async def test_skips_when_the_runner_up_already_has_it(self):
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=9),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["m"])},
        )
        await r._check_pre_warm()
        await asyncio.sleep(0.02)
        proxy.pre_warm.assert_not_awaited()

    async def test_skips_when_there_is_no_runner_up(self):
        """A single-node fleet has nowhere to pre-warm."""
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=9),
            scores=[_score("a", "m")],
            nodes={"a": _node("a", ["m"])},
        )
        await r._check_pre_warm()
        await asyncio.sleep(0.02)
        proxy.pre_warm.assert_not_awaited()

    async def test_the_lock_prevents_a_duplicate_warm(self):
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=9),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", [])},
        )
        r._pre_warm_locks.add("b:m")
        await r._check_pre_warm()
        await asyncio.sleep(0.02)
        proxy.pre_warm.assert_not_awaited()

    async def test_the_lock_is_released_even_when_the_warm_raises(self):
        """It is released in a `finally`; leaking it would block warms forever."""
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=9),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", [])},
        )
        proxy.pre_warm = AsyncMock(side_effect=RuntimeError("node went away"))
        with pytest.raises(RuntimeError):
            await r._do_pre_warm("b:m", "b", "m")
        assert "b:m" not in r._pre_warm_locks


class TestPreWarmAvailability:
    async def test_never_warms_a_machine_whose_owner_is_busy(self):
        """pre_warm_min_availability was documented and read by nothing."""
        r, _qm, proxy = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=5),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", [], availability=0.3)},
        )
        await r._check_pre_warm()
        await asyncio.sleep(0)
        proxy.pre_warm.assert_not_awaited()


class TestRebalanceDecision:
    async def test_moves_only_to_a_node_with_the_model_hot(self):
        """Otherwise it trades a queue wait for a cold load."""
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=10),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", [])},  # b is cold
            depths={"b:m": 0},
        )
        await r._check_rebalance()
        qm.move_pending.assert_not_awaited()

    async def test_moves_as_many_as_the_target_has_free_slots(self):
        """B5: a fixed few per tick drained a backlog onto a node that had just
        come back at 0.6 requests a second while it could take 2.6 (run 6)."""
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=30),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["m"])},
            free={"b:m": 4},
        )
        await r._check_rebalance()
        qm.move_pending.assert_awaited_once()
        assert qm.move_pending.await_args.args[2] == 4, "every free slot, not a fixed cap"

    async def test_never_more_than_is_pending(self):
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=5),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["m"])},
            settings=_settings(rebalance_threshold=5),
            free={"b:m": 8},
        )
        await r._check_rebalance()
        assert qm.move_pending.await_args.args[2] == 5

    async def test_a_target_with_no_free_slot_gets_nothing(self):
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=10),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["m"])},
            free={"b:m": 0},
        )
        await r._check_rebalance()
        qm.move_pending.assert_not_awaited()

    async def test_never_moves_to_itself(self):
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=10),
            scores=[_score("a", "m")],
            nodes={"a": _node("a", ["m"])},
            depths={"a:m": 10},
        )
        await r._check_rebalance()
        qm.move_pending.assert_not_awaited()

    async def test_skips_a_target_that_is_also_overloaded(self):
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=10),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["m"])},
            settings=_settings(rebalance_threshold=5),
            free={"b:m": 0},  # its own queue fills every slot
        )
        await r._check_rebalance()
        qm.move_pending.assert_not_awaited()

    async def test_below_the_threshold_nothing_moves(self):
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=2),
            scores=[_score("a", "m"), _score("b", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["m"])},
            depths={"b:m": 0},
            settings=_settings(rebalance_threshold=5),
        )
        await r._check_rebalance()
        qm.move_pending.assert_not_awaited()

    async def test_one_rebalance_per_queue_per_cycle(self):
        """The loop `break`s after a move, so a third candidate is not used."""
        r, qm, _p = _rebalancer(
            queue_info=_qinfo("a:m", "m", "a", pending=20),
            scores=[_score("a", "m"), _score("b", "m"), _score("c", "m")],
            nodes={"a": _node("a", ["m"]), "b": _node("b", ["m"]), "c": _node("c", ["m"])},
            depths={"b:m": 0, "c:m": 0},
        )
        await r._check_rebalance()
        assert qm.move_pending.await_count == 1


class TestTheLoopSurvivesErrors:
    async def test_an_exception_in_one_pass_does_not_kill_the_loop(self):
        """`run()` catches per-iteration, or one bad cycle ends rebalancing forever."""
        r, _qm, _p = _rebalancer(queue_info={}, scores=[], nodes={}, settings=_settings())
        r._check_pre_warm = AsyncMock(side_effect=RuntimeError("boom"))
        r._check_rebalance = AsyncMock(return_value=None)
        task = asyncio.create_task(r.run())
        await asyncio.sleep(0.05)
        assert not task.done(), "the loop must survive a failing pass"
        task.cancel()
