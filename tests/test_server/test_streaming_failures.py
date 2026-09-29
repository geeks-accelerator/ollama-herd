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


class TestPreWarmDiagnostics:
    """Pre-warm must say WHY it failed, and must outlive a cold model load.

    Both of these were real 2026-09-22 failures: httpx timeout exceptions
    stringify to "", so the log line read "Pre-warm gemma3:27b on bb error: "
    with nothing after the colon, and the 120s timeout aborted a load that was
    still progressing — a 12-minute retry loop that never explained itself.
    """

    async def test_pre_warm_logs_exception_type_when_message_is_empty(self, caplog):
        """An empty-stringifying exception must still name itself in the log."""
        import logging

        from fleet_manager.server.streaming import StreamingProxy

        proxy = StreamingProxy.__new__(StreamingProxy)

        class _EmptyMessageError(Exception):
            def __str__(self) -> str:  # what httpx.ReadTimeout() does in practice
                return ""

        class _Client:
            async def post(self, *a, **kw):
                raise _EmptyMessageError()

        proxy._get_client = lambda node_id: _Client()

        with caplog.at_level(logging.WARNING):
            await proxy.pre_warm("bb", "gemma3:27b", num_ctx=32768)

        assert caplog.records, "pre-warm failure must be logged"
        msg = caplog.records[-1].getMessage()
        assert "_EmptyMessageError" in msg, f"exception type missing from: {msg!r}"
        # the bug was a message that ended at the colon with nothing after it
        assert not msg.rstrip().endswith("error:"), f"empty reason: {msg!r}"

    def test_pre_warm_timeout_covers_a_cold_large_model_load(self):
        """gpt-oss:120b (65 GB) takes 45-60s warm-cache and minutes cold."""
        from fleet_manager.server.streaming import PRE_WARM_TIMEOUT_S

        assert PRE_WARM_TIMEOUT_S >= 600, (
            "pre-warm timeout must cover a cold multi-GB load; 120s aborted "
            "mid-load and made a progressing load look like a failure"
        )

    async def test_pre_warm_sends_num_ctx_when_given(self):
        """Warming without the override loads at the model's own default.

        The router then either reloads at the right size (churn) or, worse,
        strips num_ctx on every subsequent request because the resident context
        is smaller than requested.
        """
        from fleet_manager.server.streaming import StreamingProxy

        proxy = StreamingProxy.__new__(StreamingProxy)
        captured: dict = {}

        class _Resp:
            status_code = 200

        class _Client:
            async def post(self, path, json=None, timeout=None):
                captured["json"] = json
                return _Resp()

        proxy._get_client = lambda node_id: _Client()
        await proxy.pre_warm("bb", "gpt-oss:120b", num_ctx=131072)

        assert captured["json"]["options"]["num_ctx"] == 131072


class TestPreWarmResolvesNumCtx:
    """Pre-warm must load a model at its configured context without being told.

    "Callers pass num_ctx" is a contract that breaks silently.
    `rebalancer._do_pre_warm` omitted it for as long as it existed, which warms the
    runner-up at Ollama's default — and because an override only applies on a cold
    load, and pre-warming IS the cold load, the model then stays mis-sized until
    something unloads it.  On the reference fleet that meant 4x the intended KV.
    """

    def _proxy(self, overrides, dynamic=True):
        from types import SimpleNamespace

        from fleet_manager.server.streaming import StreamingProxy

        p = StreamingProxy.__new__(StreamingProxy)
        p._settings = SimpleNamespace(
            dynamic_num_ctx=dynamic, num_ctx_overrides=overrides
        )
        return p

    async def _capture(self, proxy, **kw):
        captured: dict = {}

        class _Resp:
            status_code = 200

        class _Client:
            async def post(self, path, json=None, timeout=None):
                captured["json"] = json
                return _Resp()

        proxy._get_client = lambda node_id: _Client()
        await proxy.pre_warm("bb", "gemma3:27b", **kw)
        return captured["json"]

    async def test_resolves_the_override_when_caller_omits_it(self):
        body = await self._capture(self._proxy({"gemma3:27b": 32768}))
        assert body["options"]["num_ctx"] == 32768, (
            "pre-warm must not fall back to Ollama's default when an override exists"
        )

    async def test_explicit_argument_still_wins(self):
        body = await self._capture(self._proxy({"gemma3:27b": 32768}), num_ctx=8192)
        assert body["options"]["num_ctx"] == 8192

    async def test_no_override_configured_sends_none(self):
        """Unconfigured models must keep Ollama's own default, not get a guess."""
        body = await self._capture(self._proxy({"other:7b": 4096}))
        assert "options" not in body

    async def test_respects_dynamic_num_ctx_disabled(self):
        body = await self._capture(self._proxy({"gemma3:27b": 32768}, dynamic=False))
        assert "options" not in body

    async def test_rebalancer_pre_warm_gets_the_override(self):
        """End-to-end for the call site that was actually wrong."""
        from types import SimpleNamespace

        from fleet_manager.server.rebalancer import Rebalancer

        captured: dict = {}

        class _Proxy:
            async def pre_warm(self, node_id, model, num_ctx=None):
                captured["num_ctx"] = num_ctx

        rb = Rebalancer.__new__(Rebalancer)
        rb._proxy = _Proxy()
        rb._pre_warm_locks = set()
        await rb._do_pre_warm("bb:gemma3:27b", "bb", "gemma3:27b")
        # It passes None on purpose — pre_warm resolves it. What must NOT happen is
        # a hardcoded value here that drifts from FLEET_NUM_CTX_OVERRIDES.
        assert captured["num_ctx"] is None
        assert rb._pre_warm_locks == set(), "lock must be released"
