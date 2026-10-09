"""An absent num_ctx is not a no-op, and three code paths assumed it was.

Ollama fills a missing ``num_ctx`` from ``OLLAMA_CONTEXT_LENGTH`` (or, unset,
its GPU-memory heuristic).  When that differs from the context the model is
resident at, Ollama unloads and reloads it -- the multi-minute stall that
context protection exists to prevent.  So "send nothing" is a decision, and it
was the wrong one.

Measured on the live fleet 2026-10-09, reproducible in two requests with
``OLLAMA_CONTEXT_LENGTH=131072`` and ``gemma3:27b`` pinned to 32768 in
``FLEET_NUM_CTX_OVERRIDES``:

1. ``ollama stop gemma3:27b`` then one request through the router: herd injects
   32768, Ollama launches ``-c 131072 -np 4`` = 32768 per slot.  Correct.
2. one more request: resident now *equals* the override, so herd declined to
   inject and sent no num_ctx -> Ollama applied 131072 -> the model reloaded at
   ``-c 524288 -np 4``, evicting ``gpt-oss:120b`` on the way.

The override could therefore never survive a second request, which is the real
reason gemma3:27b kept being found at 4x its configured context.  CLAUDE.md had
recorded that as "the override only applies on a cold load and nothing ever
triggers one" -- the cold load was in fact happening, correctly, and then being
reverted immediately.
"""

import pytest

from fleet_manager.models.request import InferenceRequest, RequestFormat
from tests.test_server.test_streaming import _make_proxy_with_loaded_model


def _body(proxy, model="gpt-oss:120b", options=None, node="test-node"):
    raw = {"model": model, "messages": [{"role": "user", "content": "Hi"}]}
    if options is not None:
        raw["options"] = options
    req = InferenceRequest(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        original_format=RequestFormat.OLLAMA,
        raw_body=raw,
    )
    return proxy._build_ollama_body(req, node)


class TestNoPathLeavesNumCtxAbsent:
    """The invariant: if the resident context is known, it goes on the wire."""

    @pytest.mark.parametrize(
        "options",
        [
            None,                              # client sent nothing at all
            {},                                # empty options
            {"temperature": 0.5},              # other options, no num_ctx
            {"num_ctx": 4096},                 # below resident
            {"num_ctx": 32768},                # exactly resident
        ],
        ids=["absent", "empty", "other-only", "below", "equal"],
    )
    def test_resident_context_is_always_sent(self, options):
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="strip"
        )
        body = _body(proxy, options=options)
        assert body["options"]["num_ctx"] == 32768

    def test_other_options_survive_the_pin(self):
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="strip"
        )
        body = _body(proxy, options={"temperature": 0.5, "top_p": 0.9})
        assert body["options"]["temperature"] == 0.5
        assert body["options"]["top_p"] == 0.9
        assert body["options"]["num_ctx"] == 32768


class TestTheMeasuredSequence:
    """Step 2 of the live reproduction, as a unit test."""

    def test_second_request_still_carries_the_configured_context(self):
        proxy = _make_proxy_with_loaded_model(
            model_name="gemma3:27b", context_length=32768, context_protection="strip"
        )
        proxy._settings.dynamic_num_ctx = True
        proxy._settings.num_ctx_overrides = {"gemma3:27b": 32768}
        # Resident == configured, which is precisely when the old code went
        # quiet and let OLLAMA_CONTEXT_LENGTH reload the model.
        body = _body(proxy, model="gemma3:27b")
        assert body["options"]["num_ctx"] == 32768

    def test_override_larger_than_resident_is_not_silently_pinned_down(self):
        """A client/override needing MORE context must not be clamped to resident.

        Pinning is for holding a model where it is, never for shrinking a
        request's ask -- that case is the overflow warning and the model-upgrade
        search, both unchanged.
        """
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="strip"
        )
        body = _body(proxy, options={"num_ctx": 65536})
        assert body["options"]["num_ctx"] == 65536


class TestUnknownResidentContext:
    """Nothing can be asserted about a model with no heartbeat, so assert nothing."""

    def test_unknown_model_is_left_alone(self):
        proxy = _make_proxy_with_loaded_model(
            model_name="something-else:latest",
            context_length=32768,
            context_protection="strip",
        )
        body = _body(proxy, model="gpt-oss:120b")
        assert "num_ctx" not in body.get("options", {})

    def test_unknown_model_keeps_a_client_value(self):
        proxy = _make_proxy_with_loaded_model(
            model_name="something-else:latest",
            context_length=32768,
            context_protection="strip",
        )
        body = _body(proxy, model="gpt-oss:120b", options={"num_ctx": 8192})
        assert body["options"]["num_ctx"] == 8192


class TestPassthroughModeUntouched:
    def test_passthrough_does_not_pin(self):
        """`passthrough` means the operator asked herd not to intervene."""
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="passthrough"
        )
        body = _body(proxy, options={"temperature": 0.5})
        assert "num_ctx" not in body["options"]
