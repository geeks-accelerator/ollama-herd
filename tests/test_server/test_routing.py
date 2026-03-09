"""Tests for the shared routing logic with model fallback support."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import RoutingResult
from fleet_manager.server.routes.routing import score_with_fallbacks

from tests.conftest import make_inference_request, make_node


def _mock_scorer(model_results: dict[str, list[RoutingResult]]):
    """Create a mock scorer that returns specific results per model."""
    scorer = MagicMock()

    def score_fn(model, queue_depths, estimated_tokens=0):
        return model_results.get(model, [])

    scorer.score_request.side_effect = score_fn
    return scorer


def _mock_queue_mgr():
    mgr = MagicMock()
    mgr.get_queue_depths.return_value = {}
    return mgr


def _mock_registry_with_models(models: list[str]):
    """Registry where one node has all listed models available."""
    node = make_node(
        node_id="studio",
        loaded_models=[(m, 5.0) for m in models],
        available_models=models,
    )
    registry = MagicMock()
    registry.get_all_nodes.return_value = [node]
    return registry


class TestScoreWithFallbacks:
    @pytest.mark.asyncio
    async def test_primary_model_found(self):
        """When primary model has nodes, return immediately."""
        result = RoutingResult(
            node_id="studio", queue_key="studio:phi4:14b", score=85.0
        )
        scorer = _mock_scorer({"phi4:14b": [result]})
        queue_mgr = _mock_queue_mgr()
        registry = _mock_registry_with_models(["phi4:14b"])

        req = make_inference_request(model="phi4:14b")
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry
        )

        assert len(results) == 1
        assert actual_model == "phi4:14b"
        assert results[0].node_id == "studio"

    @pytest.mark.asyncio
    async def test_fallback_used_when_primary_empty(self):
        """When primary model has no nodes, try fallback."""
        fallback_result = RoutingResult(
            node_id="mini", queue_key="mini:llama3:8b", score=60.0
        )
        scorer = _mock_scorer({
            "llama3:70b": [],
            "llama3:8b": [fallback_result],
        })
        queue_mgr = _mock_queue_mgr()
        registry = _mock_registry_with_models(["llama3:70b", "llama3:8b"])

        req = make_inference_request(model="llama3:70b", fallback_models=["llama3:8b"])
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry
        )

        assert len(results) == 1
        assert actual_model == "llama3:8b"

    @pytest.mark.asyncio
    async def test_all_models_exhausted(self):
        """When no model has available nodes and none exist, return empty."""
        scorer = _mock_scorer({})
        queue_mgr = _mock_queue_mgr()
        registry = MagicMock()
        registry.get_all_nodes.return_value = []  # No nodes at all

        req = make_inference_request(
            model="nonexistent:99b", fallback_models=["also-gone:99b"]
        )
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry
        )

        assert results == []
        assert actual_model == ""

    @pytest.mark.asyncio
    async def test_fallback_order_preserved(self):
        """Models are tried in order: primary, then fallbacks left-to-right."""
        call_order = []

        def tracking_score(model, depths, estimated_tokens=0):
            call_order.append(model)
            if model == "fallback-2":
                return [
                    RoutingResult(
                        node_id="n1", queue_key=f"n1:{model}", score=50.0
                    )
                ]
            return []

        scorer = MagicMock()
        scorer.score_request.side_effect = tracking_score
        queue_mgr = _mock_queue_mgr()
        registry = _mock_registry_with_models(
            ["primary", "fallback-1", "fallback-2"]
        )

        req = make_inference_request(model="primary")
        req.fallback_models = ["fallback-1", "fallback-2"]
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry
        )

        assert actual_model == "fallback-2"
        # The first cycle should try primary, fallback-1, fallback-2 in order
        assert call_order[:3] == ["primary", "fallback-1", "fallback-2"]

    @pytest.mark.asyncio
    async def test_no_fallbacks_works_like_before(self):
        """Requests without fallback_models still work."""
        result = RoutingResult(
            node_id="studio", queue_key="studio:phi4:14b", score=85.0
        )
        scorer = _mock_scorer({"phi4:14b": [result]})
        queue_mgr = _mock_queue_mgr()
        registry = _mock_registry_with_models(["phi4:14b"])

        req = make_inference_request(model="phi4:14b")
        assert req.fallback_models == []

        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry
        )
        assert actual_model == "phi4:14b"


class TestAutoPull:
    @pytest.mark.asyncio
    async def test_auto_pull_triggers_on_missing_model(self):
        """When model doesn't exist and auto_pull is on, pull and route."""
        call_count = 0

        def score_fn(model, queue_depths, estimated_tokens=0):
            nonlocal call_count
            call_count += 1
            # First call: no results. After pull: return results.
            if call_count > 1 and model == "new-model:7b":
                return [
                    RoutingResult(
                        node_id="studio",
                        queue_key="studio:new-model:7b",
                        score=70.0,
                    )
                ]
            return []

        scorer = MagicMock()
        scorer.score_request.side_effect = score_fn
        scorer._estimate_model_size.return_value = 5.0
        queue_mgr = _mock_queue_mgr()
        # Node with plenty of memory but no models
        node = make_node(
            node_id="studio", memory_total=128.0, memory_used=20.0
        )
        registry = MagicMock()
        registry.get_all_nodes.return_value = [node]
        registry.get_node.return_value = node

        proxy = MagicMock()
        proxy.pull_model = AsyncMock(return_value=True)

        settings = ServerSettings(auto_pull=True, auto_pull_timeout=60.0)

        req = make_inference_request(model="new-model:7b")
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry,
            proxy=proxy, settings=settings,
        )

        assert actual_model == "new-model:7b"
        assert len(results) == 1
        proxy.pull_model.assert_called_once_with("studio", "new-model:7b")

    @pytest.mark.asyncio
    async def test_auto_pull_disabled(self):
        """When auto_pull is False, return empty without pulling."""
        scorer = _mock_scorer({})
        queue_mgr = _mock_queue_mgr()
        node = make_node(
            node_id="studio", memory_total=128.0, memory_used=20.0
        )
        registry = MagicMock()
        registry.get_all_nodes.return_value = [node]

        proxy = MagicMock()
        proxy.pull_model = AsyncMock()

        settings = ServerSettings(auto_pull=False)

        req = make_inference_request(model="missing:7b")
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry,
            proxy=proxy, settings=settings,
        )

        assert results == []
        assert actual_model == ""
        proxy.pull_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_pull_no_node_fits(self):
        """When no node has enough memory, skip pull."""
        scorer = MagicMock()
        scorer.score_request.side_effect = lambda m, d, e=0: []
        scorer._estimate_model_size.return_value = 40.0  # needs 40GB
        queue_mgr = _mock_queue_mgr()
        # Node with only 5GB free
        node = make_node(
            node_id="tiny", memory_total=16.0, memory_used=11.0
        )
        registry = MagicMock()
        registry.get_all_nodes.return_value = [node]

        proxy = MagicMock()
        proxy.pull_model = AsyncMock()

        settings = ServerSettings(auto_pull=True)

        req = make_inference_request(model="llama3.3:70b")
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry,
            proxy=proxy, settings=settings,
        )

        assert results == []
        proxy.pull_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_pull_failure_returns_empty(self):
        """When pull fails, return empty results."""
        scorer = MagicMock()
        scorer.score_request.side_effect = lambda m, d, e=0: []
        scorer._estimate_model_size.return_value = 5.0
        queue_mgr = _mock_queue_mgr()
        node = make_node(
            node_id="studio", memory_total=128.0, memory_used=20.0
        )
        registry = MagicMock()
        registry.get_all_nodes.return_value = [node]

        proxy = MagicMock()
        proxy.pull_model = AsyncMock(return_value=False)

        settings = ServerSettings(auto_pull=True, auto_pull_timeout=60.0)

        req = make_inference_request(model="bad-model:7b")
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry,
            proxy=proxy, settings=settings,
        )

        assert results == []
        assert actual_model == ""
        proxy.pull_model.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_pull_without_proxy_no_error(self):
        """When proxy is not provided, skip auto-pull gracefully."""
        scorer = _mock_scorer({})
        queue_mgr = _mock_queue_mgr()
        registry = MagicMock()
        registry.get_all_nodes.return_value = []

        req = make_inference_request(model="missing:7b")
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry
        )

        assert results == []
        assert actual_model == ""
