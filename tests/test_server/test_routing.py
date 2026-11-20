"""Tests for the shared routing logic with model fallback support."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import (
    InferenceRequest,
    RequestFormat,
    RoutingResult,
)
from fleet_manager.server import model_preloader
from fleet_manager.server.routes.routing import (
    _try_vram_fallback,
    _vram_fallback_events,
    score_with_fallbacks,
)
from tests.conftest import make_inference_request, make_node


def _mock_scorer(model_results: dict[str, list[RoutingResult]]):
    """Create a mock scorer that returns specific results per model."""
    scorer = MagicMock()

    def score_fn(model, queue_depths, estimated_tokens=0, **kwargs):
        return model_results.get(model, [])

    scorer.score_request.side_effect = score_fn
    return scorer


def _mock_queue_mgr():
    mgr = MagicMock()
    mgr.get_queue_depths.return_value = {}
    return mgr


def _mock_registry_with_models(models: list[str]):
    """Registry where one node has all listed models available.

    Model names are normalized with :latest tag (matching Ollama behavior).
    """
    from fleet_manager.models.request import normalize_model_name

    tagged = [normalize_model_name(m) for m in models]
    node = make_node(
        node_id="studio",
        loaded_models=[(m, 5.0) for m in tagged],
        available_models=tagged,
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

        def tracking_score(model, depths, estimated_tokens=0, **kwargs):
            call_order.append(model)
            if model == "fallback-2:latest":
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

        req = make_inference_request(
            model="primary", fallback_models=["fallback-1", "fallback-2"]
        )
        results, actual_model = await score_with_fallbacks(
            req, scorer, queue_mgr, registry
        )

        assert actual_model == "fallback-2:latest"
        # The first cycle should try primary, fallback-1, fallback-2 in order
        # Model names are normalized with :latest tag
        assert call_order[:3] == ["primary:latest", "fallback-1:latest", "fallback-2:latest"]

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

        def score_fn(model, queue_depths, estimated_tokens=0, **kwargs):
            nonlocal call_count
            call_count += 1
            # Need > 2 because: first pass (1 call) + holding queue (1 call)
            # then auto-pull, then retry scoring (call 3+)
            if call_count > 2 and model == "new-model:7b":
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
        scorer._model_fit_cost_gb.return_value = 5.0
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
        scorer.score_request.side_effect = lambda m, d, e=0, **kw: []
        scorer._model_fit_cost_gb.return_value = 40.0  # needs 40GB
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
        scorer.score_request.side_effect = lambda m, d, e=0, **kw: []
        scorer._model_fit_cost_gb.return_value = 5.0
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


def _hot_result(node_id: str, model: str, score: float = 85.0) -> RoutingResult:
    """Create a RoutingResult with HOT thermal score."""
    return RoutingResult(
        node_id=node_id,
        queue_key=f"{node_id}:{model}",
        score=score,
        scores_breakdown={"thermal": 50.0, "total": score},
    )


def _cold_result(
    node_id: str, model: str, score: float = 45.0, *, evicts: bool = True
) -> RoutingResult:
    """Create a RoutingResult with COLD thermal score; ``evicts`` says whether
    loading it there would push a resident model out."""
    return RoutingResult(
        node_id=node_id,
        queue_key=f"{node_id}:{model}",
        score=score,
        scores_breakdown={"thermal": 10.0, "eviction": -20.0 if evicts else 0.0, "total": score},
    )


class TestVramFallback:
    """Substitution only when every load would evict, and only within a
    category.  With defaults on 2026-11-20 every request for qwen2.5 was
    answered by phi4 while an empty node could have loaded qwen2.5."""

    @pytest.fixture(autouse=True)
    def clear_events(self):
        """Clear fallback events between tests."""
        _vram_fallback_events.clear()
        yield
        _vram_fallback_events.clear()

    async def _route(self, scorer, models, *, allow_fallback=None, model="qwen3-coder:latest"):
        return await score_with_fallbacks(
            make_inference_request(model=model),
            scorer,
            _mock_queue_mgr(),
            _mock_registry_with_models(models),
            settings=ServerSettings(),
            allow_fallback=allow_fallback,
        )

    @pytest.mark.asyncio
    async def test_no_fallback_when_hot(self):
        result = _hot_result("studio", "qwen2.5-coder:32b")
        scorer = _mock_scorer({"qwen2.5-coder:32b": [result]})
        _, actual_model = await self._route(scorer, ["qwen2.5-coder:32b"], model="qwen2.5-coder:32b")
        assert actual_model == "qwen2.5-coder:32b"
        assert len(_vram_fallback_events) == 0

    @pytest.mark.asyncio
    async def test_a_node_with_room_loads_the_requested_model(self):
        cold = _cold_result("empty", "qwen3-coder:latest", evicts=False)
        scorer = _mock_scorer({"qwen3-coder:latest": [cold]})
        scorer.score_loaded_models = MagicMock()
        results, actual_model = await self._route(scorer, ["qwen3-coder:latest"])
        assert (actual_model, results[0].node_id) == ("qwen3-coder:latest", "empty")
        scorer.score_loaded_models.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_category_when_every_load_evicts(self):
        cold = _cold_result("studio", "qwen3-coder:latest")
        scorer = _mock_scorer({"qwen3-coder:latest": [cold]})
        loaded_result = _hot_result("studio", "qwen2.5-coder:32b", score=90.0)
        scorer.score_loaded_models = MagicMock(return_value=[(loaded_result, "qwen2.5-coder:32b")])
        _, actual_model = await self._route(scorer, ["qwen3-coder:latest", "qwen2.5-coder:32b"])
        assert actual_model == "qwen2.5-coder:32b"
        assert len(_vram_fallback_events) == 1
        assert _vram_fallback_events[0]["requested_model"] == "qwen3-coder:latest"
        assert _vram_fallback_events[0]["actual_model"] == "qwen2.5-coder:32b"

    @pytest.mark.asyncio
    async def test_never_another_category(self):
        """No coding model loaded: load the requested one, never a general model."""
        cold = _cold_result("studio", "qwen3-coder:latest")
        scorer = _mock_scorer({"qwen3-coder:latest": [cold]})
        general = _hot_result("studio", "llama3.3:70b", score=80.0)
        scorer.score_loaded_models = MagicMock(
            side_effect=lambda cat, *a, **k: [] if cat else [(general, "llama3.3:70b")]
        )
        _, actual_model = await self._route(scorer, ["qwen3-coder:latest", "llama3.3:70b"])
        assert actual_model == "qwen3-coder:latest"
        assert len(_vram_fallback_events) == 0

    @pytest.mark.asyncio
    async def test_strict_mode_never_substitutes(self):
        cold = _cold_result("studio", "qwen3-coder:latest")
        scorer = _mock_scorer({"qwen3-coder:latest": [cold]})
        scorer.score_loaded_models = MagicMock()
        results, actual_model = await self._route(
            scorer, ["qwen3-coder:latest"], allow_fallback=False
        )
        assert actual_model == "qwen3-coder:latest"
        assert results[0].scores_breakdown["thermal"] == 10.0
        scorer.score_loaded_models.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_loaded_models_falls_through_to_the_cold_load(self):
        cold = _cold_result("studio", "qwen3-coder:latest")
        scorer = _mock_scorer({"qwen3-coder:latest": [cold]})
        scorer.score_loaded_models = MagicMock(return_value=[])
        results, actual_model = await self._route(scorer, ["qwen3-coder:latest"])
        assert actual_model == "qwen3-coder:latest"
        assert results[0].scores_breakdown["thermal"] == 10.0


class TestClientErrorPassthrough:
    """A backend 4xx must reach the caller as that 4xx, not an opaque 500.

    Ollama returns e.g. 400 {"error":"gemma3:4b does not support tools"}; letting
    it propagate made FastAPI answer 500 Internal Server Error, telling the client
    the herd broke when their request was simply invalid (2026-07-17).
    """

    @staticmethod
    def _http_error(status: int, body: dict | None = None, text: str = ""):
        import httpx
        resp = MagicMock()
        resp.status_code = status
        resp.json.side_effect = (lambda: body) if body is not None else ValueError("no json")
        resp.text = text
        return httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)

    def test_400_passes_through_with_backend_reason(self):
        from fleet_manager.server.routes.routing import client_error_passthrough
        err = self._http_error(400, {"error": "gemma3:4b does not support tools"})
        r = client_error_passthrough(err, model="gemma3:4b")
        assert r is not None
        assert r.status_code == 400  # not 500
        assert b"does not support tools" in bytes(r.body)

    def test_anthropic_shape(self):
        from fleet_manager.server.routes.routing import client_error_passthrough
        err = self._http_error(400, {"error": "nope"})
        r = client_error_passthrough(err, model="m", anthropic=True)
        assert r.status_code == 400
        assert b'"type":"error"' in bytes(r.body)  # JSONResponse serializes compactly

    def test_5xx_is_not_swallowed(self):
        """A real backend failure must keep its existing retry/500 handling."""
        from fleet_manager.server.routes.routing import client_error_passthrough
        assert client_error_passthrough(self._http_error(500, {"error": "boom"})) is None
        assert client_error_passthrough(self._http_error(503, {"error": "busy"})) is None

    def test_non_http_errors_ignored(self):
        from fleet_manager.server.routes.routing import client_error_passthrough
        assert client_error_passthrough(ValueError("x")) is None
        assert client_error_passthrough(ConnectionError("x")) is None

    def test_non_json_body_still_yields_a_message(self):
        from fleet_manager.server.routes.routing import client_error_passthrough
        r = client_error_passthrough(self._http_error(422, None, text="plain text boom"))
        assert r is not None and r.status_code == 422


class TestImageFallbackGuard:
    """Never answer an image-bearing request with a model that cannot see images.

    The 2026-04-23 incident: 20 gemma3:27b vision requests were silently
    answered by gpt-oss:120b with the images dropped. The risk was logged as a
    QUALITY RISK for months while ``InferenceRequest.has_images`` — computed on
    every request — went unread by the fallback decision.
    """

    @staticmethod
    def _req(model: str, *, images: bool) -> InferenceRequest:
        msg = {"role": "user", "content": "what is in this picture?"}
        if images:
            msg["images"] = ["<base64-image-data>"]
        return InferenceRequest(
            model=model, original_model=model, messages=[msg],
            original_format=RequestFormat.OPENAI, raw_body={"model": model},
        )

    @staticmethod
    def _scorer_with_only(model: str):
        """Nothing in-category loaded; only `model` is loaded fleet-wide."""
        scorer = MagicMock()
        scorer.score_request.return_value = []
        scorer.score_loaded_models.side_effect = lambda cat, *a, **k: (
            [] if cat is not None else
            [(RoutingResult(node_id="bb", queue_key=f"bb:{model}", score=90.0), model)]
        )
        return scorer

    def test_has_images_is_autodetected(self):
        assert self._req("gemma3:4b", images=True).has_images is True
        assert self._req("gemma3:4b", images=False).has_images is False

    def test_image_request_is_never_given_a_blind_model(self, monkeypatch):
        """gemma3:4b + images, only gpt-oss (reasoning) loaded → refuse to substitute."""
        monkeypatch.setattr(model_preloader, "_priority_cache", [], raising=False)
        out = _try_vram_fallback(
            self._req("gemma3:4b", images=True),
            self._scorer_with_only("gpt-oss:120b"), {}, 10, [],
        )
        assert out is None, "a blind model must never answer an image request"

    @staticmethod
    def _scorer_in_category(model: str):
        """`model` is what the category search returns."""
        scorer = MagicMock()
        scorer.score_loaded_models.return_value = [
            (RoutingResult(node_id="bb", queue_key=f"bb:{model}", score=90.0), model)
        ]
        return scorer

    def test_same_category_is_still_checked_for_images(self, monkeypatch):
        """Category matching alone does not prove the substitute can see."""
        monkeypatch.setattr(model_preloader, "_priority_cache", [], raising=False)
        blind = self._scorer_in_category("gpt-oss:120b")
        assert _try_vram_fallback(self._req("gemma3:4b", images=True), blind, {}, 10, []) is None
        out = _try_vram_fallback(self._req("gemma3:4b", images=False), blind, {}, 10, [])
        assert out is not None and out[1] == "gpt-oss:120b"
