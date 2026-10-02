"""The context optimizer must never reverse an operator's own decision.

`_auto_initialize_overrides` already skipped a model that had an override, but
`_check_and_optimize` overwrote one -- and that asymmetry was a loaded gun. With
the "Auto-Calculate Context" toggle on, the deliberate gpt-oss:120b=131072
override set on 2026-09-28 (to END a six-day TTFT regression) scored as 8x
oversized against measured prompt usage, so within 5 minutes the optimizer
would have reset it to 16384 and queued an Ollama restart to apply it --
re-running the incident, automatically, from the same trace data.
"""

from types import SimpleNamespace

import pytest

from fleet_manager.server.context_optimizer import (
    ContextOptimizer,
    compute_recommended_ctx,
)


class _Store:
    def __init__(self, stats):
        self._stats = stats

    async def get_prompt_token_stats(self, days=7):
        return self._stats


def _registry(loaded):
    node = SimpleNamespace(
        node_id="bb",
        ollama=SimpleNamespace(
            models_loaded=[
                SimpleNamespace(name=n, context_length=c) for n, c in loaded
            ]
        ),
    )
    return SimpleNamespace(get_online_nodes=lambda: [node])


GPT_OSS = {
    "model": "gpt-oss:120b", "total_p99": 5309,
    "max_total_24h": 5908, "request_count": 47625,
}


@pytest.fixture
def opt():
    def _make(overrides):
        settings = SimpleNamespace(
            num_ctx_overrides=dict(overrides),
            num_ctx_auto_calculate=True,
            dynamic_num_ctx=True,
        )
        return ContextOptimizer(
            settings, _registry([("gpt-oss:120b", 131072)]), _Store([GPT_OSS])
        )

    return _make


def test_the_premise_still_holds(opt):
    """Guard the setup: this model really does score as reducible.

    If the recommendation ever stops being far below the pinned value, the
    tests below would pass for the wrong reason -- they would be asserting that
    nothing changed because nothing wanted to change.
    """
    rec = compute_recommended_ctx(GPT_OSS["total_p99"], GPT_OSS["max_total_24h"])
    assert rec == 16384
    assert rec * 4 < 131072


@pytest.mark.asyncio
async def test_an_operator_set_override_is_left_alone(opt):
    o = opt({"gpt-oss:120b": 131072})
    await o._check_and_optimize()
    assert o._settings.num_ctx_overrides["gpt-oss:120b"] == 131072


@pytest.mark.asyncio
async def test_and_queues_no_restart_to_apply_a_change_it_did_not_make(opt):
    o = opt({"gpt-oss:120b": 131072})
    await o._check_and_optimize()
    assert o.get_pending_commands("bb") == []


@pytest.mark.asyncio
async def test_a_model_with_no_override_is_still_optimized(opt):
    """The feature must keep working -- this is a scope fix, not a disabling."""
    o = opt({})
    await o._check_and_optimize()
    assert o._settings.num_ctx_overrides["gpt-oss:120b"] == 16384


@pytest.mark.asyncio
async def test_an_override_herd_set_itself_may_be_revised(opt):
    """herd manages what it created; it does not manage what it was told."""
    o = opt({})
    await o._check_and_optimize()
    assert "gpt-oss:120b" in o._auto_set
    o._settings.num_ctx_overrides["gpt-oss:120b"] = 131072
    await o._check_and_optimize()
    assert o._settings.num_ctx_overrides["gpt-oss:120b"] == 16384


@pytest.mark.asyncio
async def test_auto_init_also_marks_what_it_set(opt):
    o = opt({})
    await o._auto_initialize_overrides()
    assert o._auto_set == {"gpt-oss:120b"}


@pytest.mark.asyncio
async def test_auto_init_does_not_claim_an_operator_override(opt):
    o = opt({"gpt-oss:120b": 131072})
    await o._auto_initialize_overrides()
    assert o._auto_set == set()
    assert o._settings.num_ctx_overrides["gpt-oss:120b"] == 131072
