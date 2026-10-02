"""A model that cannot be preloaded must not be reported as failing to preload.

Preloading warms a model by posting to `/api/generate`, which Ollama refuses outright
for an embedding model ("does not support generate"). So `priority_model_not_loaded`
was describing something that was never going to happen: high embed volume made
`nomic-embed-text` a priority model, the preloader correctly declined to warm it, and
the dashboard carried a standing WARNING about the decline.
"""

from types import SimpleNamespace

import pytest

from fleet_manager.server.health_engine import HealthEngine


def _node(loaded=(), available=(), caps=None):
    caps = caps or {}
    return SimpleNamespace(
        node_id="bb",
        ollama=SimpleNamespace(
            models_loaded=[SimpleNamespace(name=n) for n in loaded],
            models_available=list(available),
            models_available_meta={
                n: SimpleNamespace(capabilities=c) for n, c in caps.items()
            },
        ),
    )


@pytest.fixture(autouse=True)
def _clear_learned_set():
    from fleet_manager.server import streaming

    streaming._non_generatable_models.clear()
    yield
    streaming._non_generatable_models.clear()


def _check(nodes, priorities):
    eng = HealthEngine()
    return eng._check_priority_models(priorities, nodes)


class TestPriorityPreloadCandidates:
    def test_embedding_model_is_not_reported_as_failing_to_preload(self):
        """Signal 1: what Ollama reports. Immediate, no failed attempt needed.

        Note the keys: Ollama keys its metadata `name:tag`, while a priority list
        carries whatever name the client used. `model_has_capability` normalizes,
        so a bare priority name must still resolve against `:latest` metadata —
        the first version of this test keyed the fixture bare and silently missed.
        """
        nodes = [_node(
            loaded=["gpt-oss:120b"],
            available=["gpt-oss:120b", "nomic-embed-text"],
            caps={
                "nomic-embed-text:latest": ["embedding"],
                "gpt-oss:120b": ["completion"],
            },
        )]
        # bare name in the priority list, `:latest` in the node metadata
        recs = _check(nodes, [{"model": "nomic-embed-text", "priority_score": 90.0}])
        assert recs == [], f"embedding model should not be flagged: {recs}"

    def test_explicitly_tagged_embedding_model_is_also_suppressed(self):
        nodes = [_node(
            loaded=[], available=["nomic-embed-text:latest"],
            caps={"nomic-embed-text:latest": ["embedding"]},
        )]
        recs = _check(nodes, [{"model": "nomic-embed-text:latest", "priority_score": 90.0}])
        assert recs == []

    def test_learned_refusal_also_suppresses_it(self):
        """Signal 2: what a backend actually refused — covers nodes reporting no caps."""
        from fleet_manager.server import streaming

        streaming._non_generatable_models.add("mystery-embedder")
        nodes = [_node(loaded=[], available=["mystery-embedder"])]
        recs = _check(nodes, [{"model": "mystery-embedder", "priority_score": 90.0}])
        assert recs == []

    def test_a_real_chat_model_is_still_flagged(self):
        """The check must keep doing its job — this is the case it exists for."""
        nodes = [_node(
            loaded=[], available=["gpt-oss:120b"],
            caps={"gpt-oss:120b": ["completion", "tools"]},
        )]
        recs = _check(nodes, [{"model": "gpt-oss:120b", "priority_score": 90.0}])
        assert len(recs) == 1
        assert recs[0].check_id == "priority_model_not_loaded"
        assert "gpt-oss:120b" in recs[0].title

    def test_unreported_capabilities_do_not_silence_a_chat_model(self):
        """`model_has_capability` is presence-only: absence must not mean 'embedding'.

        Older Ollama under-reports capabilities, so an unknown model has to keep
        being flagged or this fix would hide every genuine miss on those nodes.
        """
        nodes = [_node(loaded=[], available=["some-chat-model"])]  # no caps at all
        recs = _check(nodes, [{"model": "some-chat-model", "priority_score": 90.0}])
        assert len(recs) == 1, "an unknown model must still be reported"
