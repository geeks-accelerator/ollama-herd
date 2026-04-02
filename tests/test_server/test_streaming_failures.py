"""Tests for streaming failure detection — client disconnects and incomplete streams."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import (
    InferenceRequest,
    QueueEntry,
    RequestFormat,
)
from fleet_manager.server.streaming import StreamingProxy


def _make_entry(model="phi4:14b", node_id="node-a"):
    req = InferenceRequest(
        model=model,
        original_model=model,
        messages=[{"role": "user", "content": "hi"}],
        original_format=RequestFormat.OLLAMA,
        raw_body={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )
    return QueueEntry(
        request=req,
        assigned_node=node_id,
        routing_score=85.0,
        routing_breakdown={"thermal": 50, "memory_fit": 20},
    )


def _mock_queue_mgr():
    mgr = MagicMock()
    mgr.get_queue_depths.return_value = {}
    mgr.mark_completed = MagicMock()
    mgr.mark_failed = MagicMock()
    return mgr


def _done_chunk(model="phi4:14b"):
    """Ollama final chunk with done:true and token counts."""
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "prompt_eval_count": 10,
        "eval_count": 20,
    })


def _content_chunk(content="Hello", model="phi4:14b"):
    """Ollama content chunk."""
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": False,
    })


def _make_fake_stream(proxy, chunks, request_id=None):
    """Create a fake stream_from_node that yields chunks and populates _request_tokens
    just like the real stream_from_node does when it parses done:true."""

    async def fake_stream(node_id, request):
        for chunk_str in chunks:
            try:
                parsed = json.loads(chunk_str)
                if parsed.get("done", False):
                    prompt_tok = parsed.get("prompt_eval_count")
                    completion_tok = parsed.get("eval_count")
                    rid = request_id or request.request_id
                    proxy._request_tokens[rid] = (prompt_tok, completion_tok)
            except json.JSONDecodeError:
                pass
            yield chunk_str + "\n"

    return fake_stream


class TestClientDisconnect:
    """Bug 1: GeneratorExit (client disconnect) must be recorded as failed, not completed."""

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_disconnect_in_tracking_records_client_disconnected(self, mock_task):
        """_stream_with_tracking: GeneratorExit marks as failed, not completed."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        entry = _make_entry()

        chunks = [_content_chunk("Hello"), _content_chunk(" world"), _done_chunk()]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        # Consume only the first chunk, then stop (simulates client disconnect)
        gen = proxy._stream_with_tracking(entry, "node-a:phi4:14b", queue_mgr)
        await gen.__anext__()  # Get first chunk
        await gen.aclose()  # Triggers GeneratorExit

        # Must be marked failed, NOT completed
        queue_mgr.mark_failed.assert_called_once()
        queue_mgr.mark_completed.assert_not_called()

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_disconnect_in_retry_records_client_disconnected(self, mock_task):
        """_stream_with_retry: GeneratorExit marks as failed, not completed."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)
        scorer = MagicMock()
        entry = _make_entry()

        chunks = [_content_chunk("Hello"), _content_chunk(" world"), _done_chunk()]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        # Consume only the first chunk, then stop
        gen = proxy._stream_with_retry(
            entry, "node-a:phi4:14b", queue_mgr, scorer, settings
        )
        await gen.__anext__()
        await gen.aclose()

        queue_mgr.mark_failed.assert_called_once()
        queue_mgr.mark_completed.assert_not_called()


class TestIncompleteStream:
    """Bug 2: Streams without done:true must be recorded as incomplete, not completed."""

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_no_done_chunk_in_tracking_records_incomplete(self, mock_task):
        """_stream_with_tracking: stream ends without done:true → incomplete."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        entry = _make_entry()

        # No done:true chunk — Ollama dropped the connection
        chunks = [_content_chunk("Hello"), _content_chunk(" world")]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        chunks_received = []
        async for chunk in proxy._stream_with_tracking(entry, "node-a:phi4:14b", queue_mgr):
            chunks_received.append(chunk)

        assert len(chunks_received) == 2
        # Must be marked failed (incomplete), NOT completed
        queue_mgr.mark_failed.assert_called_once()
        queue_mgr.mark_completed.assert_not_called()

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_no_done_chunk_in_retry_records_incomplete(self, mock_task):
        """_stream_with_retry: stream ends without done:true → incomplete."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)
        scorer = MagicMock()
        entry = _make_entry()

        chunks = [_content_chunk("Hello")]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        chunks_received = []
        async for chunk in proxy._stream_with_retry(
            entry, "node-a:phi4:14b", queue_mgr, scorer, settings
        ):
            chunks_received.append(chunk)

        assert len(chunks_received) == 1
        queue_mgr.mark_failed.assert_called_once()
        queue_mgr.mark_completed.assert_not_called()


class TestNormalCompletion:
    """Verify normal streams with done:true still work correctly."""

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_complete_stream_in_tracking_records_completed(self, mock_task):
        """_stream_with_tracking: full stream with done:true → completed."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        entry = _make_entry()

        chunks = [_content_chunk("Hello"), _done_chunk()]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        chunks_received = []
        async for chunk in proxy._stream_with_tracking(entry, "node-a:phi4:14b", queue_mgr):
            chunks_received.append(chunk)

        assert len(chunks_received) == 2
        queue_mgr.mark_completed.assert_called_once()
        queue_mgr.mark_failed.assert_not_called()

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_complete_stream_in_retry_records_completed(self, mock_task):
        """_stream_with_retry: full stream with done:true → completed."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        settings = ServerSettings(max_retries=2)
        scorer = MagicMock()
        entry = _make_entry()

        chunks = [_content_chunk("Hello"), _done_chunk()]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        chunks_received = []
        async for chunk in proxy._stream_with_retry(
            entry, "node-a:phi4:14b", queue_mgr, scorer, settings
        ):
            chunks_received.append(chunk)

        assert len(chunks_received) == 2
        queue_mgr.mark_completed.assert_called_once()
        queue_mgr.mark_failed.assert_not_called()

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_token_counts_populated_on_complete(self, mock_task):
        """Verify _request_tokens is populated when done:true is received."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        entry = _make_entry()

        chunks = [_content_chunk("Hello"), _done_chunk()]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        async for _ in proxy._stream_with_tracking(entry, "node-a:phi4:14b", queue_mgr):
            pass

        # Token counts should be present after done:true
        assert entry.request.request_id in proxy._request_tokens
        prompt, completion = proxy._request_tokens[entry.request.request_id]
        assert prompt == 10
        assert completion == 20

    @pytest.mark.asyncio
    @patch("fleet_manager.server.streaming._create_logged_task")
    async def test_token_counts_missing_on_incomplete(self, mock_task):
        """Verify _request_tokens is NOT populated when done:true is missing."""
        registry = MagicMock()
        proxy = StreamingProxy(registry)
        queue_mgr = _mock_queue_mgr()
        entry = _make_entry()

        chunks = [_content_chunk("Hello")]
        proxy.stream_from_node = _make_fake_stream(proxy, chunks)

        async for _ in proxy._stream_with_tracking(entry, "node-a:phi4:14b", queue_mgr):
            pass

        assert entry.request.request_id not in proxy._request_tokens
