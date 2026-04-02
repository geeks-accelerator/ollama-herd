"""Tests for auto-retry on node failure during streaming."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import (
    InferenceRequest,
    QueueEntry,
    RequestFormat,
    RoutingResult,
)
from fleet_manager.server.streaming import StreamingProxy


def _make_entry(model="phi4:14b", node_id="node-a"):
    req = InferenceRequest(
        model=model,
        original_model=model,
        messages=[{"role": "user", "content": "hi"}],
        original_format=RequestFormat.OPENAI,
        raw_body={"model": model},
    )
    return QueueEntry(
        request=req,
        assigned_node=node_id,
        routing_score=85.0,
        routing_breakdown={"thermal": 50, "memory_fit": 20},
    )


def _mock_scorer(score_fn=None):
    scorer = MagicMock()
    if score_fn:
        scorer.score_request.side_effect = score_fn
    else:
        scorer.score_request.return_value = []
    return scorer


def _mock_queue_mgr():
    mgr = MagicMock()
    mgr.get_queue_depths.return_value = {}
    mgr.mark_completed = MagicMock()
    mgr.mark_failed = MagicMock()
    return mgr


class TestIsRetryableError:
    def test_connect_error_is_retryable(self):
        assert StreamingProxy._is_retryable_error(httpx.ConnectError("refused"))

    def test_connect_timeout_is_retryable(self):
        assert StreamingProxy._is_retryable_error(httpx.ConnectTimeout("timeout"))

    def test_read_timeout_is_retryable(self):
        assert StreamingProxy._is_retryable_error(httpx.ReadTimeout("timeout"))

    def test_500_is_retryable(self):
        response = MagicMock()
        response.status_code = 500
        err = httpx.HTTPStatusError("server error", request=MagicMock(), response=response)
        assert StreamingProxy._is_retryable_error(err)

    def test_400_is_not_retryable(self):
        response = MagicMock()
        response.status_code = 400
        err = httpx.HTTPStatusError("bad request", request=MagicMock(), response=response)
        assert not StreamingProxy._is_retryable_error(err)

    def test_value_error_is_not_retryable(self):
        assert not StreamingProxy._is_retryable_error(ValueError("something"))


class TestStreamWithRetry:
    @pytest.mark.asyncio
    async def test_successful_stream_no_retry(self):
        """Normal successful streaming — no retry needed."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)
        scorer = _mock_scorer()
        entry = _make_entry()

        async def fake_stream(node_id, request):
            yield "data: {}\n\n"
            yield "data: [DONE]\n\n"
            # Simulate done:true token extraction (normally done in stream_from_node)
            proxy._request_tokens[request.request_id] = (10, 20)

        proxy.stream_from_node = fake_stream

        chunks = []
        async for chunk in proxy._stream_with_retry(
            entry, "node-a:phi4:14b", queue_mgr, scorer, settings
        ):
            chunks.append(chunk)

        assert len(chunks) == 2
        queue_mgr.mark_completed.assert_called_once()
        queue_mgr.mark_failed.assert_not_called()
        assert entry.retry_count == 0

    @pytest.mark.asyncio
    async def test_retry_on_connect_error(self):
        """First node fails with ConnectError, retry succeeds on second node."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)

        node_b_result = RoutingResult(
            node_id="node-b", queue_key="node-b:phi4:14b", score=70.0,
            scores_breakdown={"thermal": 30},
        )
        scorer = _mock_scorer(score_fn=lambda model, depths: [node_b_result])
        entry = _make_entry(node_id="node-a")

        call_count = 0

        async def failing_then_succeeding(node_id, request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("Connection refused")
            yield "data: {}\n\n"
            yield "data: [DONE]\n\n"
            # Simulate done:true token extraction (normally done in stream_from_node)
            proxy._request_tokens[request.request_id] = (10, 20)

        proxy.stream_from_node = failing_then_succeeding

        chunks = []
        async for chunk in proxy._stream_with_retry(
            entry, "node-a:phi4:14b", queue_mgr, scorer, settings
        ):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert entry.retry_count == 1
        assert entry.assigned_node == "node-b"
        assert "node-a" in entry.excluded_nodes
        queue_mgr.mark_completed.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_retry_after_first_chunk(self):
        """Error after first chunk sent — should propagate, not retry."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)
        scorer = _mock_scorer()
        entry = _make_entry()

        async def fail_after_chunk(node_id, request):
            yield "data: partial\n\n"
            raise httpx.ReadTimeout("timeout mid-stream")

        proxy.stream_from_node = fail_after_chunk

        chunks = []
        with pytest.raises(httpx.ReadTimeout):
            async for chunk in proxy._stream_with_retry(
                entry, "node-a:phi4:14b", queue_mgr, scorer, settings
            ):
                chunks.append(chunk)

        assert len(chunks) == 1
        queue_mgr.mark_failed.assert_called_once()
        assert entry.retry_count == 0

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """After max_retries, error propagates."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=1)

        node_b_result = RoutingResult(
            node_id="node-b", queue_key="node-b:phi4:14b", score=70.0,
        )
        scorer = _mock_scorer(score_fn=lambda model, depths: [node_b_result])
        entry = _make_entry(node_id="node-a")

        async def always_fail(node_id, request):
            raise httpx.ConnectError("refused")
            yield  # makes this an async generator

        proxy.stream_from_node = always_fail

        with pytest.raises(httpx.ConnectError):
            async for _ in proxy._stream_with_retry(
                entry, "node-a:phi4:14b", queue_mgr, scorer, settings
            ):
                pass

        assert entry.retry_count == 2  # attempt 1 (original) + attempt 2 (retry)
        queue_mgr.mark_failed.assert_called()

    @pytest.mark.asyncio
    async def test_no_retry_on_client_error(self):
        """4xx errors should not trigger retry."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)
        scorer = _mock_scorer()
        entry = _make_entry()

        response = MagicMock()
        response.status_code = 400

        async def client_error(node_id, request):
            raise httpx.HTTPStatusError(
                "bad request", request=MagicMock(), response=response
            )
            yield  # makes this an async generator

        proxy.stream_from_node = client_error

        with pytest.raises(httpx.HTTPStatusError):
            async for _ in proxy._stream_with_retry(
                entry, "node-a:phi4:14b", queue_mgr, scorer, settings
            ):
                pass

        assert entry.retry_count == 0
        queue_mgr.mark_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_excluded_nodes_grow(self):
        """Each failed node gets added to the exclusion list."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)

        call_num = 0

        def scoring(model, depths):
            nonlocal call_num
            call_num += 1
            node_id = f"node-{chr(97 + call_num)}"  # node-b, node-c
            return [RoutingResult(
                node_id=node_id, queue_key=f"{node_id}:{model}", score=50.0,
            )]

        scorer = _mock_scorer(score_fn=scoring)
        entry = _make_entry(node_id="node-a")

        fail_count = 0

        async def fail_then_succeed(node_id, request):
            nonlocal fail_count
            fail_count += 1
            if fail_count <= 2:
                raise httpx.ConnectError("refused")
            yield "data: ok\n\n"

        proxy.stream_from_node = fail_then_succeed

        chunks = []
        async for chunk in proxy._stream_with_retry(
            entry, "node-a:phi4:14b", queue_mgr, scorer, settings
        ):
            chunks.append(chunk)

        assert "node-a" in entry.excluded_nodes
        assert "node-b" in entry.excluded_nodes
        assert entry.retry_count == 2

    @pytest.mark.asyncio
    async def test_no_nodes_left_after_exclusion(self):
        """When re-scoring returns empty (all excluded), error propagates."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)

        scorer = _mock_scorer()
        scorer.score_request.return_value = []
        entry = _make_entry(node_id="node-a")

        async def fail(node_id, request):
            raise httpx.ConnectError("refused")
            yield  # makes this an async generator

        proxy.stream_from_node = fail

        with pytest.raises(RuntimeError, match="No available nodes"):
            async for _ in proxy._stream_with_retry(
                entry, "node-a:phi4:14b", queue_mgr, scorer, settings
            ):
                pass
