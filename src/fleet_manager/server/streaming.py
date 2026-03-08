"""Streaming proxy — forwards requests to Ollama instances and converts formats."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server.registry import NodeRegistry

logger = logging.getLogger(__name__)


class StreamingProxy:
    def __init__(self, registry: NodeRegistry, latency_store=None, trace_store=None):
        self._registry = registry
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._latency_store = latency_store
        self._trace_store = trace_store
        # Token counts extracted from Ollama final chunks, keyed by request_id
        self._request_tokens: dict[str, tuple[int | None, int | None]] = {}

    def _get_client(self, node_id: str) -> httpx.AsyncClient:
        if node_id not in self._clients:
            node = self._registry.get_node(node_id)
            if not node:
                raise ValueError(f"Node {node_id} not found in registry")
            self._clients[node_id] = httpx.AsyncClient(
                base_url=node.ollama_base_url,
                timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
            )
        return self._clients[node_id]

    def make_process_fn(self, queue_key: str, queue_manager, scorer=None, settings=None):
        """Create a process function for the queue worker.

        If scorer and settings are provided, enables auto-retry on node failure.
        """
        proxy = self

        def process(entry: QueueEntry) -> AsyncIterator[str]:
            if scorer and settings and getattr(settings, "max_retries", 0) > 0:
                return proxy._stream_with_retry(
                    entry, queue_key, queue_manager, scorer, settings
                )
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
            # Consumer stopped consuming (e.g., non-streaming accumulated enough)
            pass
        except Exception as e:
            error_occurred = True
            queue_manager.mark_failed(queue_key, entry)
            logger.error(f"Stream error for {entry.request.request_id[:8]}: {e}")
            # Record failed trace
            elapsed_ms = (time.time() - start_time) * 1000
            self._record_trace(
                entry, entry.assigned_node, start_time, first_token_time,
                "failed", error_message=str(e),
            )
            raise
        finally:
            if not error_occurred:
                queue_manager.mark_completed(queue_key, entry)
                elapsed_ms = (time.time() - start_time) * 1000
                # Read (don't pop) token counts — the route handler may still
                # need them for the OpenAI-compat usage response.
                prompt_tokens, completion_tokens = self._request_tokens.get(
                    entry.request.request_id, (None, None)
                )
                logger.info(
                    f"Request {entry.request.request_id[:8]} completed on {entry.assigned_node} "
                    f"in {elapsed_ms / 1000:.1f}s "
                    f"(prompt={prompt_tokens}, completion={completion_tokens})"
                )
                # Record latency + tokens for dashboard and Signal 4
                if self._latency_store:
                    asyncio.create_task(
                        self._latency_store.record(
                            entry.assigned_node,
                            entry.request.model,
                            elapsed_ms,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
                    )
                # Record completed trace
                self._record_trace(
                    entry, entry.assigned_node, start_time, first_token_time,
                    "completed",
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
        ttft_ms = (
            (first_token_time - start_time) * 1000 if first_token_time else None
        )
        prompt_tokens, completion_tokens = self._request_tokens.get(
            entry.request.request_id, (None, None)
        )
        asyncio.create_task(
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
            )
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
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
            return True
        return False

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
                queue_manager.mark_completed(current_queue_key, entry)
                self._record_trace(
                    entry, current_node, start_time, first_token_time, "completed"
                )
                return
            except Exception as e:
                if first_chunk_sent or not self._is_retryable_error(e):
                    # Cannot retry after chunks sent, or non-retryable error
                    queue_manager.mark_failed(current_queue_key, entry)
                    self._record_trace(
                        entry, current_node, start_time, first_token_time,
                        "failed", error_message=str(e),
                    )
                    if first_chunk_sent:
                        logger.error(
                            f"Stream error (after first chunk, cannot retry) "
                            f"for {entry.request.request_id[:8]}: {e}"
                        )
                    else:
                        logger.error(
                            f"Non-retryable error for {entry.request.request_id[:8]}: {e}"
                        )
                    raise

                # Retryable pre-first-chunk failure
                attempt += 1
                excluded_nodes.append(current_node)
                entry.excluded_nodes = excluded_nodes
                entry.retry_count = attempt

                logger.warning(
                    f"Node {current_node} failed for {entry.request.request_id[:8]} "
                    f"(attempt {attempt}/{max_retries + 1}): {e}"
                )

                # Record "retried" trace for the failed attempt
                self._record_trace(
                    entry, current_node, start_time, None,
                    "retried", error_message=str(e),
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
                # Stream completed successfully
                queue_manager.mark_completed(current_queue_key, entry)
                elapsed_ms = (time.time() - start_time) * 1000
                prompt_tokens, completion_tokens = self._request_tokens.get(
                    entry.request.request_id, (None, None)
                )
                logger.info(
                    f"Request {entry.request.request_id[:8]} completed on {current_node} "
                    f"in {elapsed_ms / 1000:.1f}s "
                    f"(prompt={prompt_tokens}, completion={completion_tokens})"
                )
                if self._latency_store:
                    asyncio.create_task(
                        self._latency_store.record(
                            current_node,
                            entry.request.model,
                            elapsed_ms,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
                    )
                self._record_trace(
                    entry, current_node, start_time, first_token_time, "completed"
                )
                return

    async def stream_from_node(
        self, node_id: str, request: InferenceRequest
    ) -> AsyncIterator[str]:
        """Stream response from a node's Ollama, converting format if needed."""
        client = self._get_client(node_id)
        ollama_body = self._build_ollama_body(request)

        # Determine endpoint based on original request shape
        endpoint = "/api/chat"
        if request.raw_body.get("prompt") is not None and "messages" not in request.raw_body:
            endpoint = "/api/generate"

        async with client.stream("POST", endpoint, json=ollama_body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                # Extract token counts from the final Ollama chunk
                try:
                    parsed = json.loads(line)
                    if parsed.get("done", False):
                        prompt_tok = parsed.get("prompt_eval_count")
                        completion_tok = parsed.get("eval_count")
                        self._request_tokens[request.request_id] = (
                            prompt_tok,
                            completion_tok,
                        )
                except json.JSONDecodeError:
                    pass
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
                json={"model": model, "prompt": "", "keep_alive": "10m"},
                timeout=120.0,
            )
            if resp.status_code == 200:
                logger.info(f"Pre-warmed {model} on {node_id}")
            else:
                logger.warning(f"Pre-warm {model} on {node_id} failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Pre-warm {model} on {node_id} error: {e}")

    def _build_ollama_body(self, request: InferenceRequest) -> dict:
        """Convert normalized request to Ollama API format."""
        if request.original_format == RequestFormat.OLLAMA and request.raw_body:
            body = dict(request.raw_body)
            body["stream"] = True
            return body

        body = {
            "model": request.model,
            "stream": True,
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

        return body

    def _ollama_to_openai_sse(self, ollama_json_line: str, model: str) -> str:
        """Convert a single Ollama NDJSON line to OpenAI SSE format."""
        try:
            data = json.loads(ollama_json_line)
        except json.JSONDecodeError:
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
