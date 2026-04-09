"""Streaming proxy — forwards requests to Ollama instances and converts formats."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable

import httpx

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server.registry import NodeRegistry

logger = logging.getLogger(__name__)

# Context protection event tracking (same pattern as VRAM fallbacks in routing.py)
_context_protection_events: list[dict] = []


def _record_context_protection(
    action: str,
    model: str,
    node_id: str,
    client_num_ctx: int,
    loaded_ctx: int,
    upgrade_model: str | None = None,
) -> None:
    """Record a context protection event for health visibility."""
    _context_protection_events.append({
        "timestamp": time.time(),
        "action": action,  # "stripped", "upgraded", "warning"
        "model": model,
        "node_id": node_id,
        "client_num_ctx": client_num_ctx,
        "loaded_ctx": loaded_ctx,
        "upgrade_model": upgrade_model,
    })
    if len(_context_protection_events) > 200:
        _context_protection_events.pop(0)


def get_context_protection_events(hours: float = 24) -> list[dict]:
    """Return context protection events from the last N hours."""
    cutoff = time.time() - (hours * 3600)
    return [e for e in _context_protection_events if e["timestamp"] >= cutoff]


def _create_logged_task(coro, *, name: str = "background"):
    """Create an asyncio task that logs exceptions instead of silently dropping them."""
    task = asyncio.create_task(coro, name=name)

    def _on_done(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error(f"Background task '{name}' failed: {exc}", exc_info=exc)

    task.add_done_callback(_on_done)
    return task


class StreamingProxy:
    def __init__(self, registry: NodeRegistry, latency_store=None, trace_store=None, settings=None):
        self._registry = registry
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._client_urls: dict[str, str] = {}  # Track URL used to create each client
        self._latency_store = latency_store
        self._trace_store = trace_store
        self._settings = settings
        # Token counts extracted from Ollama final chunks, keyed by request_id
        self._request_tokens: dict[str, tuple[int | None, int | None]] = {}
        # Extended request metadata: thinking tokens, done_reason, num_predict budget
        self._request_meta: dict[str, dict] = {}

    def pop_token_counts(
        self, request_id: str
    ) -> tuple[int | None, int | None]:
        """Remove and return (prompt_tokens, completion_tokens) for a request."""
        return self._request_tokens.pop(request_id, (None, None))

    def pop_request_meta(self, request_id: str) -> dict | None:
        """Remove and return extended request metadata for a request."""
        return self._request_meta.pop(request_id, None)

    def _get_client(self, node_id: str) -> httpx.AsyncClient:
        node = self._registry.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found in registry")

        # Recreate client if URL changed (e.g., node got a new LAN IP)
        current_url = node.ollama_base_url
        if node_id in self._clients and self._client_urls.get(node_id) != current_url:
            logger.info(
                f"Node {node_id} URL changed from {self._client_urls[node_id]} "
                f"to {current_url}, recreating HTTP client"
            )
            _create_logged_task(
                self._clients[node_id].aclose(),
                name=f"close-stale-client-{node_id}",
            )
            del self._clients[node_id]

        if node_id not in self._clients:
            self._clients[node_id] = httpx.AsyncClient(
                base_url=current_url,
                timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
            )
            self._client_urls[node_id] = current_url
        return self._clients[node_id]

    def _invalidate_client(self, node_id: str):
        """Discard a cached HTTP client so the next request creates a fresh one.

        Called after connection errors to recover from stale connection pools.
        """
        if node_id in self._clients:
            logger.info(f"Invalidating HTTP client for {node_id} after connection error")
            _create_logged_task(
                self._clients[node_id].aclose(),
                name=f"close-failed-client-{node_id}",
            )
            del self._clients[node_id]
            self._client_urls.pop(node_id, None)

    def make_process_fn(self, queue_key: str, queue_manager, scorer=None, settings=None):
        """Create a process function for the queue worker.

        If scorer and settings are provided, enables auto-retry on node failure.
        """
        proxy = self

        def process(entry: QueueEntry) -> AsyncIterator[str]:
            if scorer and settings and getattr(settings, "max_retries", 0) > 0:
                return proxy._stream_with_retry(entry, queue_key, queue_manager, scorer, settings)
            return proxy._stream_with_tracking(entry, queue_key, queue_manager)

        return process

    async def _stream_with_tracking(
        self, entry: QueueEntry, queue_key: str, queue_manager
    ) -> AsyncIterator[str]:
        """Stream from Ollama with latency tracking and queue cleanup."""
        start_time = time.time()
        first_token_time = None
        error_occurred = False
        try:
            async for chunk in self.stream_from_node(entry.assigned_node, entry.request):
                if first_token_time is None:
                    first_token_time = time.time()
                yield chunk
        except GeneratorExit:
            # Client disconnected (HTTP timeout, connection drop, etc.)
            # This is NOT a successful completion — the client never got the full response.
            error_occurred = True
            queue_manager.mark_failed(queue_key, entry)
            logger.warning(
                f"Client disconnected for {entry.request.request_id[:8]} "
                f"on {entry.assigned_node}"
            )
            self._record_trace(
                entry,
                entry.assigned_node,
                start_time,
                first_token_time,
                "client_disconnected",
                error_message="Client disconnected before stream completed",
            )
            self._request_tokens.pop(entry.request.request_id, None)
        except Exception as e:
            error_occurred = True
            # Invalidate stale client on connection errors so next request gets a fresh one
            if self._is_retryable_error(e):
                self._invalidate_client(entry.assigned_node)
            queue_manager.mark_failed(queue_key, entry)
            logger.error(
                f"Stream error for {entry.request.request_id[:8]}: {type(e).__name__}: {e}"
            )
            # Record failed trace
            self._record_trace(
                entry,
                entry.assigned_node,
                start_time,
                first_token_time,
                "failed",
                error_message=str(e) or repr(e),
            )
            raise
        finally:
            if not error_occurred:
                # Check if Ollama sent the final done:true chunk.
                # _request_tokens is only populated when done:true is parsed in stream_from_node.
                # If missing, the stream ended without completing (Ollama dropped connection).
                got_done = entry.request.request_id in self._request_tokens

                if got_done:
                    queue_manager.mark_completed(queue_key, entry)
                    elapsed_ms = (time.time() - start_time) * 1000
                    # Read (don't pop) token counts — the route handler may still
                    # need them for the OpenAI-compat usage response.
                    prompt_tokens, completion_tokens = self._request_tokens.get(
                        entry.request.request_id, (None, None)
                    )
                    logger.info(
                        f"Request {entry.request.request_id[:8]} completed on "
                        f"{entry.assigned_node} in {elapsed_ms / 1000:.1f}s "
                        f"(prompt={prompt_tokens}, completion={completion_tokens})"
                    )
                    # Record latency + tokens for dashboard and Signal 4
                    if self._latency_store:
                        _create_logged_task(
                            self._latency_store.record(
                                entry.assigned_node,
                                entry.request.model,
                                elapsed_ms,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                            ),
                            name=f"latency-record-{entry.request.request_id[:8]}",
                        )
                    # Record completed trace
                    self._record_trace(
                        entry,
                        entry.assigned_node,
                        start_time,
                        first_token_time,
                        "completed",
                    )
                else:
                    # Stream ended without done:true — Ollama dropped the connection
                    queue_manager.mark_failed(queue_key, entry)
                    elapsed_ms = (time.time() - start_time) * 1000
                    logger.warning(
                        f"Incomplete stream for {entry.request.request_id[:8]} on "
                        f"{entry.assigned_node} — no done:true received after "
                        f"{elapsed_ms / 1000:.1f}s"
                    )
                    self._record_trace(
                        entry,
                        entry.assigned_node,
                        start_time,
                        first_token_time,
                        "incomplete",
                        error_message="Stream ended without done:true from Ollama",
                    )
            else:
                # Clean up token tracking on error
                self._request_tokens.pop(entry.request.request_id, None)

    def _record_trace(
        self,
        entry: QueueEntry,
        node_id: str,
        start_time: float,
        first_token_time: float | None,
        status: str,
        error_message: str | None = None,
    ):
        """Fire-and-forget trace recording."""
        if not self._trace_store:
            return
        elapsed_ms = (time.time() - start_time) * 1000
        ttft_ms = (first_token_time - start_time) * 1000 if first_token_time else None
        prompt_tokens, completion_tokens = self._request_tokens.get(
            entry.request.request_id, (None, None)
        )
        _create_logged_task(
            self._trace_store.record_trace(
                request_id=entry.request.request_id,
                model=entry.request.model,
                original_model=entry.request.original_model or entry.request.model,
                node_id=node_id,
                score=entry.routing_score,
                scores_breakdown=entry.routing_breakdown,
                status=status,
                latency_ms=elapsed_ms,
                time_to_first_token_ms=ttft_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                retry_count=entry.retry_count,
                fallback_used=entry.fallback_used,
                excluded_nodes=entry.excluded_nodes if entry.excluded_nodes else None,
                original_format=entry.request.original_format.value,
                error_message=error_message,
                tags=entry.request.tags if entry.request.tags else None,
            ),
            name=f"trace-record-{entry.request.request_id[:8]}",
        )

    @staticmethod
    def _is_retryable_error(e: Exception) -> bool:
        """Return True if the error suggests a node infrastructure failure worth retrying."""
        if isinstance(
            e,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.ReadError,
            ),
        ):
            return True
        return isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500

    async def _stream_with_retry(
        self,
        entry: QueueEntry,
        queue_key: str,
        queue_manager,
        scorer,
        settings,
    ) -> AsyncIterator[str]:
        """Stream with automatic retry on pre-first-chunk infrastructure failures."""
        max_retries = settings.max_retries
        excluded_nodes = list(entry.excluded_nodes)
        current_node = entry.assigned_node
        current_queue_key = queue_key
        attempt = 0

        while attempt <= max_retries:
            first_chunk_sent = False
            first_token_time = None
            start_time = time.time()
            try:
                async for chunk in self.stream_from_node(current_node, entry.request):
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        first_token_time = time.time()
                    yield chunk
            except GeneratorExit:
                # Client disconnected — not a successful completion
                queue_manager.mark_failed(current_queue_key, entry)
                logger.warning(
                    f"Client disconnected for {entry.request.request_id[:8]} "
                    f"on {current_node}"
                )
                self._record_trace(
                    entry, current_node, start_time, first_token_time,
                    "client_disconnected",
                    error_message="Client disconnected before stream completed",
                )
                self._request_tokens.pop(entry.request.request_id, None)
                return
            except Exception as e:
                # Invalidate stale client on connection errors
                if self._is_retryable_error(e):
                    self._invalidate_client(current_node)

                if first_chunk_sent or not self._is_retryable_error(e):
                    # Cannot retry after chunks sent, or non-retryable error
                    queue_manager.mark_failed(current_queue_key, entry)
                    self._record_trace(
                        entry,
                        current_node,
                        start_time,
                        first_token_time,
                        "failed",
                        error_message=str(e) or repr(e),
                    )
                    if first_chunk_sent:
                        logger.error(
                            f"Stream error (after first chunk, cannot retry) "
                            f"for {entry.request.request_id[:8]}: {type(e).__name__}: {e}"
                        )
                    else:
                        logger.error(
                            f"Non-retryable error for {entry.request.request_id[:8]}: "
                            f"{type(e).__name__}: {e}"
                        )
                    raise

                # Retryable pre-first-chunk failure
                attempt += 1
                excluded_nodes.append(current_node)
                entry.excluded_nodes = excluded_nodes
                entry.retry_count = attempt

                logger.warning(
                    f"Node {current_node} failed for {entry.request.request_id[:8]} "
                    f"(attempt {attempt}/{max_retries + 1}): {type(e).__name__}: {e}"
                )

                # Record "retried" trace for the failed attempt
                self._record_trace(
                    entry,
                    current_node,
                    start_time,
                    None,
                    "retried",
                    error_message=str(e) or repr(e),
                )

                if attempt > max_retries:
                    queue_manager.mark_failed(current_queue_key, entry)
                    raise

                # Re-score excluding failed nodes
                queue_depths = queue_manager.get_queue_depths()
                results = scorer.score_request(entry.request.model, queue_depths)
                results = [r for r in results if r.node_id not in excluded_nodes]

                if not results:
                    queue_manager.mark_failed(current_queue_key, entry)
                    raise RuntimeError(
                        f"No available nodes after excluding {excluded_nodes}"
                    ) from e

                next_winner = results[0]
                current_node = next_winner.node_id
                current_queue_key = next_winner.queue_key
                entry.assigned_node = current_node
                entry.routing_score = next_winner.score
                entry.routing_breakdown = next_winner.scores_breakdown

                logger.info(
                    f"Retrying {entry.request.request_id[:8]} on {current_node} "
                    f"(attempt {attempt + 1})"
                )
                continue
            else:
                # Stream ended — check if Ollama sent the final done:true chunk
                got_done = entry.request.request_id in self._request_tokens
                elapsed_ms = (time.time() - start_time) * 1000

                if got_done:
                    queue_manager.mark_completed(current_queue_key, entry)
                    prompt_tokens, completion_tokens = self._request_tokens.get(
                        entry.request.request_id, (None, None)
                    )
                    logger.info(
                        f"Request {entry.request.request_id[:8]} completed on {current_node} "
                        f"in {elapsed_ms / 1000:.1f}s "
                        f"(prompt={prompt_tokens}, completion={completion_tokens})"
                    )
                    if self._latency_store:
                        _create_logged_task(
                            self._latency_store.record(
                                current_node,
                                entry.request.model,
                                elapsed_ms,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                            ),
                            name=f"latency-record-{entry.request.request_id[:8]}",
                        )
                    self._record_trace(
                        entry, current_node, start_time, first_token_time, "completed",
                    )
                else:
                    # Stream ended without done:true — Ollama dropped the connection
                    queue_manager.mark_failed(current_queue_key, entry)
                    logger.warning(
                        f"Incomplete stream for {entry.request.request_id[:8]} on "
                        f"{current_node} — no done:true received after "
                        f"{elapsed_ms / 1000:.1f}s"
                    )
                    self._record_trace(
                        entry, current_node, start_time, first_token_time,
                        "incomplete",
                        error_message="Stream ended without done:true from Ollama",
                    )
                return

    async def stream_from_node(self, node_id: str, request: InferenceRequest) -> AsyncIterator[str]:
        """Stream response from a node's Ollama, converting format if needed."""
        client = self._get_client(node_id)
        ollama_body = self._build_ollama_body(request, node_id)

        # Determine endpoint based on original request shape
        endpoint = "/api/chat"
        if request.raw_body.get("prompt") is not None and "messages" not in request.raw_body:
            endpoint = "/api/generate"

        # Track thinking vs output tokens for thinking-model awareness
        thinking_token_count = 0
        output_token_count = 0

        async with client.stream("POST", endpoint, json=ollama_body) as response:
            if response.status_code >= 400:
                body = await response.aread()
                logger.error(
                    f"Ollama {node_id} returned {response.status_code} for "
                    f"{request.model}: {body.decode(errors='replace')[:500]}"
                )
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                # Extract token counts and thinking/output breakdown
                try:
                    parsed = json.loads(line)
                    # Count thinking vs output tokens from streaming chunks.
                    # Ollama sends thinking content in message.thinking and
                    # regular content in message.content.
                    msg = parsed.get("message", {})
                    thinking_text = msg.get("thinking", "")
                    content_text = msg.get("content", "") or parsed.get("response", "")
                    if thinking_text:
                        # Rough token estimate: ~4 chars per token
                        thinking_token_count += max(1, len(thinking_text) // 4)
                    if content_text and not parsed.get("done", False):
                        output_token_count += max(1, len(content_text) // 4)

                    if parsed.get("done", False):
                        prompt_tok = parsed.get("prompt_eval_count")
                        completion_tok = parsed.get("eval_count")
                        done_reason = parsed.get("done_reason", "")
                        self._request_tokens[request.request_id] = (
                            prompt_tok,
                            completion_tok,
                        )
                        # Store extended metadata for headers
                        self._request_meta[request.request_id] = {
                            "thinking_tokens": thinking_token_count,
                            "output_tokens": output_token_count,
                            "done_reason": done_reason,
                            "prompt_tokens": prompt_tok,
                            "completion_tokens": completion_tok,
                            "num_predict": ollama_body.get("options", {}).get(
                                "num_predict"
                            ),
                        }
                except json.JSONDecodeError:
                    logger.warning(f"Malformed JSON from Ollama on {node_id}: {line[:200]}")
                # Yield in the appropriate format
                if request.original_format == RequestFormat.OPENAI:
                    yield self._ollama_to_openai_sse(line, request.model)
                else:
                    yield line + "\n"

        if request.original_format == RequestFormat.OPENAI:
            yield "data: [DONE]\n\n"

    async def pre_warm(self, node_id: str, model: str):
        """Send a load-only request to pre-warm a model on a node."""
        try:
            client = self._get_client(node_id)
            resp = await client.post(
                "/api/generate",
                json={"model": model, "prompt": "", "keep_alive": -1},
                timeout=120.0,
            )
            if resp.status_code == 200:
                logger.info(f"Pre-warmed {model} on {node_id}")
            else:
                logger.warning(f"Pre-warm {model} on {node_id} failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Pre-warm {model} on {node_id} error: {e}")

    async def pull_model(
        self,
        node_id: str,
        model: str,
        progress_cb: Callable[[int, int, int, str], None] | None = None,
    ) -> bool:
        """Pull a model onto a node via Ollama /api/pull. Returns True on success.

        progress_cb(pct, completed_bytes, total_bytes, status) is called on each
        progress update if provided.
        """
        try:
            client = self._get_client(node_id)
            async with client.stream(
                "POST", "/api/pull", json={"name": model}
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        logger.error(
                            f"Pull {model} on {node_id} failed: {data['error']}"
                        )
                        return False
                    status = data.get("status", "")
                    if "completed" in data and "total" in data and data["total"] > 0:
                        pct = int(data["completed"] / data["total"] * 100)
                        logger.info(
                            f"Pulling {model} on {node_id}: {status} {pct}%"
                        )
                        if progress_cb:
                            progress_cb(pct, data["completed"], data["total"], status)
                    elif status == "success":
                        logger.info(f"Pull {model} on {node_id}: success")
                        if progress_cb:
                            progress_cb(100, 0, 0, "success")
                    elif progress_cb:
                        progress_cb(-1, 0, 0, status)
            return True
        except Exception as e:
            logger.error(f"Pull {model} on {node_id} error: {type(e).__name__}: {e}")
            return False

    async def pull_model_streaming(
        self, node_id: str, model: str
    ) -> AsyncIterator[bytes]:
        """Pull a model onto a node, yielding raw NDJSON bytes for the client.

        Yields each line from Ollama's /api/pull response verbatim, preserving
        the exact wire format (status, digest, total, completed).
        """
        client = self._get_client(node_id)
        async with client.stream(
            "POST", "/api/pull", json={"name": model}
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    logger.error(
                        f"Pull {model} on {node_id} failed: {data['error']}"
                    )
                status = data.get("status", "")
                if "completed" in data and "total" in data and data["total"] > 0:
                    pct = int(data["completed"] / data["total"] * 100)
                    logger.info(f"Pulling {model} on {node_id}: {status} {pct}%")
                elif status == "success":
                    logger.info(f"Pull {model} on {node_id}: success")
                yield line.encode() + b"\n"

    async def delete_model(self, node_id: str, model: str) -> bool:
        """Delete a model from a node via Ollama DELETE /api/delete."""
        try:
            client = self._get_client(node_id)
            resp = await client.request(
                "DELETE", "/api/delete", json={"name": model}, timeout=30.0
            )
            if resp.status_code == 200:
                logger.info(f"Deleted {model} from {node_id}")
                return True
            else:
                logger.warning(
                    f"Delete {model} on {node_id} failed: HTTP {resp.status_code}"
                )
                return False
        except Exception as e:
            logger.error(
                f"Delete {model} on {node_id} error: {type(e).__name__}: {e}"
            )
            return False

    def make_image_process_fn(self, queue_key: str, queue_manager, timeout: float = 120.0):
        """Create a process function for image generation queue entries.

        Unlike LLM process functions that return async generators, this wraps
        the image generation in a single-yield generator that marks completion
        immediately after the image bytes are produced.
        """
        proxy = self

        async def _generate_and_yield(entry: QueueEntry):
            start_time = time.time()
            try:
                png_bytes = await proxy.generate_image_on_node(
                    entry.assigned_node, entry.request.raw_body, timeout
                )
                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"Image {entry.request.request_id[:8]} completed "
                    f"on {entry.assigned_node} in {elapsed_ms:.0f}ms"
                )
                queue_manager.mark_completed(queue_key, entry)
                yield png_bytes
            except Exception as e:
                queue_manager.mark_failed(queue_key, entry)
                logger.error(
                    f"Image {entry.request.request_id[:8]} failed "
                    f"on {entry.assigned_node}: {repr(e)}"
                )
                raise

        def process(entry: QueueEntry):
            return _generate_and_yield(entry)

        return process

    async def transcribe_on_node(
        self, node_id: str, audio_bytes: bytes, filename: str, timeout: float = 300.0
    ) -> dict:
        """Proxy a transcription request to a node's STT server.

        The mlx-qwen3-asr server uses an async job API: submit → poll.
        This method handles both steps and returns the final result.
        """
        import asyncio

        node = self._registry.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        from urllib.parse import urlparse

        parsed = urlparse(node.ollama_base_url)
        host = parsed.hostname or "localhost"
        stt_port = node.transcription_port or 11437
        stt_url = f"http://{host}:{stt_port}"
        auth = {"Authorization": "Bearer herd-internal"}

        async with httpx.AsyncClient(
            base_url=stt_url,
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0),
        ) as client:
            # Submit job
            resp = await client.post(
                "/transcribe",
                files={"audio": (filename, audio_bytes)},
                headers=auth,
            )
            resp.raise_for_status()
            job = resp.json()
            job_id = job["job_id"]

            # Poll for completion
            deadline = time.time() + timeout
            while time.time() < deadline:
                resp = await client.get(f"/jobs/{job_id}", headers=auth)
                resp.raise_for_status()
                status = resp.json()
                if status["status"] == "completed":
                    return status.get("result", status)
                if status["status"] == "failed":
                    raise RuntimeError(
                        f"Transcription job {job_id} failed: "
                        f"{status.get('error', 'unknown')}"
                    )
                await asyncio.sleep(0.5)

            raise TimeoutError(f"Transcription job {job_id} timed out")

    def make_transcription_process_fn(
        self, queue_key: str, queue_manager, timeout: float = 300.0
    ):
        """Create a process function for transcription queue entries."""
        proxy = self

        async def _transcribe_and_yield(entry: QueueEntry):
            start_time = time.time()
            try:
                audio_bytes = entry.request.raw_body.get("_audio_bytes", b"")
                filename = entry.request.raw_body.get("_filename", "audio.wav")
                result = await proxy.transcribe_on_node(
                    entry.assigned_node, audio_bytes, filename, timeout
                )
                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"Transcription {entry.request.request_id[:8]} completed "
                    f"on {entry.assigned_node} in {elapsed_ms:.0f}ms"
                )
                queue_manager.mark_completed(queue_key, entry)
                import json

                yield json.dumps(result)
            except Exception as e:
                queue_manager.mark_failed(queue_key, entry)
                logger.error(
                    f"Transcription {entry.request.request_id[:8]} failed "
                    f"on {entry.assigned_node}: {repr(e)}"
                )
                raise

        def process(entry: QueueEntry):
            return _transcribe_and_yield(entry)

        return process

    async def generate_image_on_node(
        self, node_id: str, body: dict, timeout: float = 120.0
    ) -> bytes:
        """Proxy an image generation request to a node's image server.

        Returns raw PNG bytes on success, raises on failure.
        """
        node = self._registry.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        # Build the image server URL from the node's Ollama URL + image port
        from urllib.parse import urlparse

        parsed = urlparse(node.ollama_base_url)
        host = parsed.hostname or "localhost"
        image_port = node.image_port or 11436
        image_url = f"http://{host}:{image_port}"

        async with httpx.AsyncClient(
            base_url=image_url,
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0),
        ) as client:
            resp = await client.post("/api/generate-image", json=body)
            resp.raise_for_status()
            return resp.content

    async def query_node_models(self, node_id: str) -> list[dict]:
        """Query a node's Ollama /api/tags for model details including disk size."""
        try:
            client = self._get_client(node_id)
            resp = await client.get("/api/tags", timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            result = []
            for m in data.get("models", []):
                size_bytes = m.get("size", 0)
                result.append({
                    "name": m.get("model", m.get("name", "")),
                    "size_gb": round(size_bytes / (1024**3), 2),
                    "parameter_size": m.get("details", {}).get(
                        "parameter_size", ""
                    ),
                    "quantization": m.get("details", {}).get(
                        "quantization_level", ""
                    ),
                    "family": m.get("details", {}).get("family", ""),
                })
            return result
        except Exception as e:
            logger.warning(
                f"Query models on {node_id} error: {type(e).__name__}: {e}"
            )
            return []

    def _get_loaded_context(self, model: str, node_id: str) -> int:
        """Look up the context length of a loaded model on a node. Returns 0 if unknown."""
        node = self._registry.get_node(node_id)
        if not node or not node.ollama:
            return 0
        for loaded in node.ollama.models_loaded:
            if loaded.name == model:
                return loaded.context_length or 0
        return 0

    def _find_context_upgrade(
        self, model: str, required_ctx: int, node_id: str
    ) -> str | None:
        """Find a loaded model with sufficient context and more parameters.

        Searches the assigned node first, then all other nodes. Returns the model
        name if a suitable upgrade is found, or None.
        """
        node = self._registry.get_node(node_id)
        if not node or not node.ollama:
            return None

        # Find the current model's size for comparison
        current_size = 0.0
        for loaded in node.ollama.models_loaded:
            if loaded.name == model:
                current_size = loaded.size_gb
                break

        # Search this node first, then all nodes
        all_nodes = [node]
        for other in self._registry.get_all_nodes():
            if other.node_id != node_id and other.ollama:
                all_nodes.append(other)

        best_candidate = None
        best_size = current_size
        for n in all_nodes:
            if not n.ollama:
                continue
            for loaded in n.ollama.models_loaded:
                if loaded.name == model:
                    continue  # Skip the same model
                if (
                    (loaded.context_length or 0) >= required_ctx
                    and loaded.size_gb > best_size
                    and (best_candidate is None or loaded.size_gb < best_candidate[1])
                ):
                    best_candidate = (loaded.name, loaded.size_gb, n.node_id)

        if best_candidate:
            return best_candidate[0]  # Return model name
        return None

    def _apply_context_protection(self, body: dict, model: str, node_id: str) -> None:
        """Strip or warn about num_ctx values that would trigger Ollama model reloads.

        When a client sends num_ctx different from the loaded model's context window,
        Ollama unloads and reloads the entire model. For large models this causes
        multi-minute hangs or deadlocks. This method intercepts and strips num_ctx
        when it's unnecessary (≤ loaded context), preventing the reload.

        When num_ctx exceeds the loaded context, searches for a loaded model with
        sufficient context and more parameters, and switches to it if found.
        """
        if not self._settings:
            return
        mode = getattr(self._settings, "context_protection", "strip")
        if mode == "passthrough":
            return

        # Dynamic num_ctx: inject override when client didn't specify num_ctx
        # This ensures cold-loaded models use the optimized context size
        if getattr(self._settings, "dynamic_num_ctx", False):
            overrides = getattr(self._settings, "num_ctx_overrides", {})
            override = overrides.get(model, 0)
            if override > 0:
                options = body.get("options")
                if not options or "num_ctx" not in options:
                    if "options" not in body:
                        body["options"] = {}
                    body["options"]["num_ctx"] = override
                    logger.info(
                        f"Dynamic num_ctx: injected num_ctx={override} for {model}"
                    )
                    _record_context_protection(
                        "dynamic_override", model, node_id, override, 0
                    )

        options = body.get("options")
        if not options or "num_ctx" not in options:
            return

        client_num_ctx = options["num_ctx"]
        loaded_ctx = self._get_loaded_context(model, node_id)
        if loaded_ctx == 0:
            # Unknown context — can't protect, pass through
            return

        if client_num_ctx <= loaded_ctx:
            if mode == "strip":
                del options["num_ctx"]
                # Clean up empty options dict
                if not options:
                    body.pop("options", None)
                logger.info(
                    f"Context protection: stripped num_ctx={client_num_ctx} for {model} on "
                    f"{node_id} (loaded context={loaded_ctx})"
                )
                _record_context_protection("stripped", model, node_id, client_num_ctx, loaded_ctx)
            else:
                logger.warning(
                    f"Context protection: client sent num_ctx={client_num_ctx} for {model} on "
                    f"{node_id} (loaded context={loaded_ctx}) — would trigger reload"
                )
                _record_context_protection("warning", model, node_id, client_num_ctx, loaded_ctx)
        else:
            # Client needs more context than loaded — try to find a bigger loaded model
            if mode == "strip":
                upgrade = self._find_context_upgrade(model, client_num_ctx, node_id)
                if upgrade:
                    body["model"] = upgrade
                    del options["num_ctx"]
                    if not options:
                        body.pop("options", None)
                    logger.info(
                        f"Context protection: switched {model} → {upgrade} for "
                        f"num_ctx={client_num_ctx} on {node_id} (original context={loaded_ctx})"
                    )
                    _record_context_protection(
                        "upgraded", model, node_id, client_num_ctx, loaded_ctx, upgrade
                    )
                    return

            logger.warning(
                f"Context protection: client wants num_ctx={client_num_ctx} but {model} on "
                f"{node_id} only has context={loaded_ctx}"
            )
            _record_context_protection("warning", model, node_id, client_num_ctx, loaded_ctx)

    def _build_ollama_body(self, request: InferenceRequest, node_id: str) -> dict:
        """Convert normalized request to Ollama API format."""
        if request.original_format == RequestFormat.OLLAMA and request.raw_body:
            body = dict(request.raw_body)
            body["stream"] = True
            # Strip tagging fields that Ollama doesn't understand
            body.pop("metadata", None)
            body.pop("fallback_models", None)
            # Keep models loaded permanently — the router manages model lifecycle,
            # not Ollama's idle timeout. Prevents costly cold loads on high-memory machines.
            body.setdefault("keep_alive", -1)
            # Protect against num_ctx triggering expensive model reloads
            self._apply_context_protection(body, request.model, node_id)
            # Auto-inflate num_predict for thinking models
            self._apply_thinking_overhead(body, request.model)
            return body

        body = {
            "model": request.model,
            "stream": True,
            "keep_alive": -1,
        }

        if request.messages:
            body["messages"] = request.messages
        elif request.raw_body.get("prompt") is not None:
            body["prompt"] = request.raw_body["prompt"]

        options = {}
        if request.temperature != 0.7:
            options["temperature"] = request.temperature
        if request.max_tokens:
            options["num_predict"] = request.max_tokens
        if options:
            body["options"] = options

        # Auto-inflate num_predict for thinking models
        self._apply_thinking_overhead(body, request.model)

        return body

    def _apply_thinking_overhead(self, body: dict, model: str) -> None:
        """Auto-inflate num_predict for thinking models.

        Thinking models (deepseek-r1, gpt-oss, qwq) split their token budget
        between internal chain-of-thought reasoning and visible output. Small
        num_predict values result in empty responses because the entire budget
        is consumed by thinking. This inflates the budget to ensure enough
        tokens for both reasoning and output.

        Only applies when num_predict is explicitly set by the client. If the
        client doesn't set num_predict, Ollama uses the model's default (usually
        large enough), so no inflation is needed.
        """
        from fleet_manager.server.model_knowledge import is_thinking_model

        if not is_thinking_model(model):
            return

        options = body.get("options", {})
        num_predict = options.get("num_predict")
        if num_predict is None:
            return  # Client didn't set a limit, Ollama will use model default

        # Get overhead settings
        overhead = 4.0
        min_predict = 1024
        if self._settings:
            overhead = getattr(self._settings, "thinking_overhead", 4.0)
            min_predict = getattr(self._settings, "thinking_min_predict", 1024)

        # Apply: inflate by multiplier, enforce minimum
        original = num_predict
        inflated = max(int(num_predict * overhead), min_predict)
        if inflated != original:
            if "options" not in body:
                body["options"] = {}
            body["options"]["num_predict"] = inflated
            logger.info(
                f"Thinking overhead: inflated num_predict {original} → {inflated} "
                f"for {model} (×{overhead}, min={min_predict})"
            )

    def _ollama_to_openai_sse(self, ollama_json_line: str, model: str) -> str:
        """Convert a single Ollama NDJSON line to OpenAI SSE format."""
        try:
            data = json.loads(ollama_json_line)
        except json.JSONDecodeError:
            logger.warning(f"Malformed JSON in OpenAI SSE conversion: {ollama_json_line[:200]}")
            return ""

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if data.get("done", False):
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        else:
            content = data.get("message", {}).get("content", "")
            if not content:
                content = data.get("response", "")
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": None,
                    }
                ],
            }

        return f"data: {json.dumps(chunk)}\n\n"

    async def close(self):
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        logger.debug("StreamingProxy closed all HTTP clients")
