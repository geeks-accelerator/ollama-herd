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
    def __init__(self, registry: NodeRegistry, latency_store=None):
        self._registry = registry
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._latency_store = latency_store
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

    def make_process_fn(self, queue_key: str, queue_manager):
        """Create a process function for the queue worker."""

        def process(entry: QueueEntry) -> AsyncIterator[str]:
            return self._stream_with_tracking(entry, queue_key, queue_manager)

        return process

    async def _stream_with_tracking(
        self, entry: QueueEntry, queue_key: str, queue_manager
    ) -> AsyncIterator[str]:
        """Stream from Ollama with latency tracking and queue cleanup."""
        start_time = time.time()
        error_occurred = False
        try:
            async for chunk in self.stream_from_node(entry.assigned_node, entry.request):
                yield chunk
        except GeneratorExit:
            # Consumer stopped consuming (e.g., non-streaming accumulated enough)
            pass
        except Exception as e:
            error_occurred = True
            queue_manager.mark_failed(queue_key, entry)
            logger.error(f"Stream error for {entry.request.request_id[:8]}: {e}")
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
            else:
                # Clean up token tracking on error
                self._request_tokens.pop(entry.request.request_id, None)

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
