"""MLX proxy — forwards requests directly to `mlx_lm.server` (OpenAI-compat API).

Opt-in path that bypasses Ollama entirely for specific model names.  When an
inference request's resolved model starts with ``mlx:``, herd routes the
request here instead of through the normal fleet scoring + queue pipeline.
This lets us serve 4+ models hot on macOS despite Ollama's hardcoded 3-model
cap — each ``mlx_lm.server`` is an independent process with its own budget.

Why a separate proxy and not just a node backend:
  Phase 1 of the MLX backend plan (see
  ``docs/plans/mlx-backend-for-large-models.md``) ships this minimal proxy
  so we can prove the routing path without touching ``StreamingProxy``,
  ``NodeRegistry``, or the scoring pipeline.  Phase 2 refactors into a
  ``backends/`` abstraction where each node advertises multiple backends
  via heartbeat; Phase 1 is the functional MVP.

Protocol translation:
  - Anthropic Messages → OpenAI chat.completions (native for mlx_lm.server)
  - OpenAI SSE (data: {...}) → Anthropic SSE (event: ...\\ndata: {...})
  - Non-streaming Anthropic response ← OpenAI non-streaming response

Trace store still gets the request recorded via ``record_trace`` just like
Ollama-served requests, so dashboards and health checks see MLX traffic
alongside Ollama traffic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import httpx

from fleet_manager.models.request import InferenceRequest, RequestFormat

logger = logging.getLogger(__name__)

# Per-request token counts captured from mlx_lm.server's usage field.
# Keyed by request_id.  Tuple shape: (prompt_tokens, completion_tokens,
# cached_tokens).  cached_tokens is the slice of prompt_tokens that hit
# mlx_lm.server's prompt cache (i.e. were skipped during prompt processing
# because the prefix matched a previously-cached prompt).
#
# Cache-hit-rate computation (per request): cached_tokens / prompt_tokens.
# The proxy aggregates these into rolling per-model stats so the dashboard
# can show "cache hit: 87%" alongside the queue depth.
#
# See docs/plans/mlx-prompt-cache-optimization.md for why this matters
# (10-50× turn 2+ speedup when cache hits work end-to-end).
_mlx_request_tokens: dict[str, tuple[int | None, int | None, int | None]] = {}


def strip_mlx_prefix(model: str) -> str:
    """Return the model name without the ``mlx:`` prefix, if present.

    The prefix is the herd-side routing marker; mlx_lm.server itself doesn't
    expect it.  ``mlx:Qwen3-Coder-480B-A35B-4bit`` → ``Qwen3-Coder-480B-A35B-4bit``.
    """
    if model.startswith("mlx:"):
        return model[4:]
    return model


def is_mlx_model(model: str) -> bool:
    """True iff the model name is routed to the MLX backend."""
    return model.startswith("mlx:")


def _ollama_messages_to_openai(messages: list[dict]) -> list[dict]:
    """Convert Ollama-shaped messages to strict OpenAI chat.completions shape.

    The Anthropic translator (anthropic_to_ollama_messages) produces
    Ollama-friendly output that the Ollama HTTP API accepts but mlx_lm.server
    rejects with cryptic 404s:

      - tool_calls[].function.arguments is a *dict* in Ollama; OpenAI/mlx
        requires it to be a JSON-stringified *string*.  Symptom from mlx:
        ``"the JSON object must be str, bytes or bytearray, not dict"``.
      - tool_calls items lack ``id`` and ``type:"function"`` wrappers in
        the Ollama form; OpenAI requires them.
      - Ollama tolerates extra fields like ``images: [...]`` on user
        messages; OpenAI doesn't expect them.  Drop quietly.

    Pure passthrough for the common case (string content, no tool_calls).
    """
    import uuid
    out: list[dict] = []
    for m in messages:
        new_m = dict(m)  # shallow copy — we'll only rewrite specific fields
        # Drop Ollama-specific fields mlx ignores or chokes on
        new_m.pop("images", None)
        # Translate tool_calls if present
        tcs = new_m.get("tool_calls")
        if tcs and isinstance(tcs, list):
            new_tcs = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                # OpenAI expects arguments as a JSON-encoded string
                if isinstance(args, dict):
                    args_str = json.dumps(args)
                elif isinstance(args, str):
                    args_str = args
                else:
                    args_str = "{}"
                new_tcs.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": args_str,
                    },
                })
            new_m["tool_calls"] = new_tcs
            # OpenAI: assistant messages with tool_calls may have null content
            if new_m.get("content") is None:
                new_m["content"] = ""
        out.append(new_m)
    return out


class MlxModelMissingError(ValueError):
    """Raised when the proxy would send an empty/missing model name to mlx_lm.server.

    Defends against the historical 33-failure incident where ``model=""`` was
    being sent and mlx_lm.server returned a confusing 404 with
    ``"[Errno 2] No such file or directory: 'config.json'"``.  Catching this
    upstream lets the route return a clear 500 telling operators to check the
    model map / inference_req.model.
    """


class MlxQueueFullError(Exception):
    """Raised when the MLX admission queue is saturated.

    mlx_lm.server is single-threaded per process.  Without admission control,
    a Claude Code retry storm stacks requests inside mlx's HTTP queue faster
    than it can drain them, and the whole backend wedges — requests sit in
    that queue for tens of seconds or time out entirely.

    Instead, the proxy bounds the queue at ``max_queue_depth`` pending +
    1 in-flight.  Overflow raises this exception, which the route handler
    translates to an HTTP 503 with a ``Retry-After`` header.  Clients (and
    Claude Code in particular) respect that signal and back off rather than
    piling on more retries.

    Attributes:
        model_key: Which MLX model rejected the request (for logs + metrics)
        queued:    Current pending count at rejection time
        in_flight: Current in-flight count at rejection time (usually 1)
        retry_after: Suggested retry delay in seconds
    """

    def __init__(
        self,
        model_key: str,
        queued: int,
        in_flight: int,
        retry_after: int,
    ):
        self.model_key = model_key
        self.queued = queued
        self.in_flight = in_flight
        self.retry_after = retry_after
        super().__init__(
            f"MLX backend busy for model {model_key!r}: "
            f"{queued} queued + {in_flight} in-flight "
            f"(cap reached). Retry in {retry_after}s."
        )


class MlxProxy:
    """Minimal OpenAI-compat → Anthropic SSE bridge for a single mlx_lm.server."""

    def __init__(
        self,
        base_url: str,
        trace_store=None,
        *,
        max_queue_depth: int = 3,
        retry_after_seconds: int = 10,
    ):
        self._base_url = base_url.rstrip("/")
        self._trace_store = trace_store
        self._client: httpx.AsyncClient | None = None
        # Admission control config (see MlxQueueFullError docstring).
        self.max_queue_depth = max_queue_depth
        self.retry_after_seconds = retry_after_seconds
        # Per-model asyncio.Semaphore(1) enforces mlx_lm.server's real
        # concurrency limit at the herd boundary.  Lazily created so we
        # don't depend on an event loop existing at __init__ time (tests).
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # Accurate counters for dashboard / /fleet/queue.  _queued counts
        # coroutines waiting on the semaphore; _inflight counts those past
        # it (i.e. actively executing against mlx_lm.server).
        self._queued: dict[str, int] = {}
        self._inflight: dict[str, int] = {}
        self._completed: dict[str, int] = {}
        self._failed: dict[str, int] = {}
        self._rejected: dict[str, int] = {}  # queue-full 503s per model
        # Running sums for dashboard averages (per-model, since-start lifecycle
        # matching ``_completed`` above).  ``_stats_samples`` is the denominator;
        # may be ≤ _completed[model] if a completion didn't yield token counts.
        self._sum_latency_ms: dict[str, float] = {}
        self._sum_prompt_tokens: dict[str, int] = {}
        self._sum_completion_tokens: dict[str, int] = {}
        self._stats_samples: dict[str, int] = {}

    def _record_stats(
        self,
        model_key: str,
        latency_ms: float,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Accumulate per-model stats for dashboard averages.  Safe to call
        with None tokens — stored as 0 for that sample."""
        self._sum_latency_ms[model_key] = (
            self._sum_latency_ms.get(model_key, 0.0) + latency_ms
        )
        self._sum_prompt_tokens[model_key] = (
            self._sum_prompt_tokens.get(model_key, 0) + (prompt_tokens or 0)
        )
        self._sum_completion_tokens[model_key] = (
            self._sum_completion_tokens.get(model_key, 0) + (completion_tokens or 0)
        )
        self._stats_samples[model_key] = self._stats_samples.get(model_key, 0) + 1

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def is_healthy(self) -> bool:
        """True iff mlx_lm.server answers ``GET /v1/models`` with 200."""
        try:
            client = await self._get_client()
            resp = await client.get("/v1/models", timeout=3.0)
            return resp.status_code == 200
        except Exception as exc:
            logger.debug(f"MLX health check failed: {type(exc).__name__}: {exc}")
            return False

    async def list_models(self) -> list[str]:
        """Return model IDs from mlx_lm.server's /v1/models (no ``mlx:`` prefix)."""
        try:
            client = await self._get_client()
            resp = await client.get("/v1/models")
            resp.raise_for_status()
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception as exc:
            logger.debug(f"MLX list_models failed: {type(exc).__name__}: {exc}")
            return []

    def pop_token_counts(
        self, request_id: str
    ) -> tuple[int | None, int | None, int | None]:
        """Drain captured token counts for a finished request.

        Returns (prompt_tokens, completion_tokens, cached_tokens).  Any
        component may be None if mlx_lm.server didn't report it (older
        versions don't expose ``prompt_tokens_details.cached_tokens``).

        Side effect: also folds the cache_hit observation into the
        per-model rolling stats so the dashboard can show hit rate.
        """
        result = _mlx_request_tokens.pop(request_id, (None, None, None))
        prompt, _completion, cached = result
        if prompt is not None and cached is not None:
            self._record_cache_observation(prompt, cached)
        return result

    def _record_cache_observation(self, prompt_tokens: int, cached_tokens: int) -> None:
        """Append a cache hit observation to the per-model rolling window.

        Stored as (prompt_tokens, cached_tokens) tuples; the rolling
        window is fixed size (50 most recent observations) so old data
        doesn't dominate the displayed rate after a workload shift.
        Per-model so MLX deployments serving multiple models report
        hit rate correctly per-model, not averaged.
        """
        # Lazy init the per-proxy ring buffer
        if not hasattr(self, "_cache_observations"):
            self._cache_observations: dict[str, list[tuple[int, int]]] = {}
        # Single-tenant for now (one model per mlx_lm.server process), so
        # use a static key.  Expand to per-model when we run multi-model.
        key = "_default"
        obs = self._cache_observations.setdefault(key, [])
        obs.append((prompt_tokens, cached_tokens))
        # Cap at 50 observations — recent enough to reflect current state,
        # numerous enough to smooth single-request noise.
        if len(obs) > 50:
            obs.pop(0)

    def get_cache_hit_rate(self) -> float | None:
        """Return rolling cache hit rate over recent requests, or None.

        Computed as sum(cached) / sum(prompt) over the rolling window —
        weighted by request size so big requests dominate the rate (which
        is what we care about for latency).  Returns None when no
        observations are available yet.
        """
        if not hasattr(self, "_cache_observations"):
            return None
        obs = self._cache_observations.get("_default") or []
        if not obs:
            return None
        total_prompt = sum(p for p, _ in obs)
        total_cached = sum(c for _, c in obs)
        if total_prompt == 0:
            return None
        return total_cached / total_prompt

    def _get_semaphore(self, model_key: str) -> asyncio.Semaphore:
        """Return (creating if needed) the per-model admission semaphore."""
        sem = self._semaphores.get(model_key)
        if sem is None:
            sem = asyncio.Semaphore(1)
            self._semaphores[model_key] = sem
        return sem

    async def _acquire_slot(self, model_key: str) -> None:
        """Admission control — wait for mlx_lm.server slot or reject.

        Enforces the design's core invariant: at most 1 request is
        executing against mlx_lm.server at a time, with at most
        ``max_queue_depth`` additional requests waiting their turn.
        Raises :class:`MlxQueueFullError` immediately if the queue would
        exceed the cap, so clients get a fast 503 instead of an indefinite
        queue wait that just multiplies the problem.

        Counter semantics:
            _queued[m]   — coroutines waiting on the semaphore
            _inflight[m] — coroutines that have acquired and are running

        Transition happens inside this method: we increment _queued, wait
        for the semaphore, then move the count to _inflight.
        """
        current_queued = self._queued.get(model_key, 0)
        current_inflight = self._inflight.get(model_key, 0)
        # Hard cap: reject anything that would exceed max_queue_depth pending.
        # ``+1`` accounts for this request being the one that would overflow.
        if current_queued + 1 > self.max_queue_depth:
            self._rejected[model_key] = self._rejected.get(model_key, 0) + 1
            raise MlxQueueFullError(
                model_key=model_key,
                queued=current_queued,
                in_flight=current_inflight,
                retry_after=self.retry_after_seconds,
            )
        # Claim the queue slot atomically (single-threaded asyncio — no race)
        self._queued[model_key] = current_queued + 1
        sem = self._get_semaphore(model_key)
        try:
            await sem.acquire()
        except BaseException:
            # Cancellation / shutdown — release the queue slot so counters
            # stay honest even if we never actually run.
            self._queued[model_key] = max(0, self._queued.get(model_key, 1) - 1)
            raise
        # Move from queued → in-flight
        self._queued[model_key] = max(0, self._queued.get(model_key, 1) - 1)
        self._inflight[model_key] = self._inflight.get(model_key, 0) + 1

    def _release_slot(self, model_key: str) -> None:
        """Release the slot after request completion (or failure)."""
        self._inflight[model_key] = max(0, self._inflight.get(model_key, 1) - 1)
        sem = self._semaphores.get(model_key)
        if sem is not None:
            sem.release()

    async def stream_openai(
        self, request: InferenceRequest, *, already_admitted: bool = False,
    ) -> AsyncIterator[bytes]:
        """Forward request to mlx_lm.server's /v1/chat/completions, streaming.

        The body in ``request.raw_body`` is Ollama-shaped (produced by the
        Anthropic translator).  We convert it to OpenAI chat.completions
        format for MLX.  Yields raw SSE chunks from mlx_lm.server — the
        caller is responsible for translating them to Anthropic SSE.

        Args:
            request: inference request
            already_admitted: when True, the caller has already acquired a
                slot via :meth:`_acquire_slot` and is responsible for calling
                :meth:`_release_slot` after iteration completes.  Used by the
                streaming route so admission failures surface as a proper
                HTTP 503 *before* StreamingResponse locks in the 200 status.
        """
        client = await self._get_client()
        mlx_body = self._to_openai_body(request)
        model_key = strip_mlx_prefix(request.model)
        # Admission control — skipped when the caller pre-admitted (streaming
        # route).  May raise MlxQueueFullError, which the route translates
        # to HTTP 503 + Retry-After.  Otherwise this blocks until a slot
        # opens (previous request finishes).
        if not already_admitted:
            await self._acquire_slot(model_key)
        start_time = time.time()
        # Parse usage from the final SSE chunk if present; mlx_lm.server emits
        # {"usage": {"prompt_tokens": N, "completion_tokens": M}} in the last
        # "data:" line before [DONE].  Missing is fine — stats still records
        # latency with 0 tokens.
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        try:
            async with client.stream(
                "POST", "/v1/chat/completions", json=mlx_body
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    logger.error(
                        f"MLX server returned {response.status_code} for "
                        f"{request.model}: {body.decode(errors='replace')[:500]}"
                    )
                    response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    # Opportunistic usage sniff — don't break on malformed JSON.
                    if line.startswith("data:") and '"usage"' in line:
                        try:
                            payload = json.loads(line[5:].strip())
                            usage = payload.get("usage") if isinstance(payload, dict) else None
                            if isinstance(usage, dict):
                                prompt_tokens = usage.get("prompt_tokens")
                                completion_tokens = usage.get("completion_tokens")
                        except (ValueError, TypeError):
                            pass
                    yield line.encode()
            self._completed[model_key] = self._completed.get(model_key, 0) + 1
            self._record_stats(
                model_key,
                (time.time() - start_time) * 1000,
                prompt_tokens,
                completion_tokens,
            )
        except Exception:
            self._failed[model_key] = self._failed.get(model_key, 0) + 1
            raise
        finally:
            # Only release if we acquired here; the streaming-route case
            # releases on its own after the StreamingResponse completes.
            if not already_admitted:
                self._release_slot(model_key)

    async def completions_non_streaming(
        self, request: InferenceRequest
    ) -> dict:
        """Non-streaming fallback — returns the full OpenAI response dict.

        Only used when the client explicitly sets ``stream=false``; most
        Claude Code traffic streams.
        """
        client = await self._get_client()
        mlx_body = self._to_openai_body(request)
        mlx_body["stream"] = False
        model_key = strip_mlx_prefix(request.model)
        # Same admission control as streaming path — see _acquire_slot.
        await self._acquire_slot(model_key)
        start_time = time.time()
        try:
            resp = await client.post("/v1/chat/completions", json=mlx_body)
            resp.raise_for_status()
            self._completed[model_key] = self._completed.get(model_key, 0) + 1
            data = resp.json()
            # Capture usage for both observability surfaces:
            #   - cached_tokens → rolling cache-hit-rate stat (Phase 2 of
            #     docs/plans/mlx-prompt-cache-optimization.md)
            #   - prompt/completion → running-average stats for dashboard
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict):
                details = usage.get("prompt_tokens_details") or {}
                _mlx_request_tokens[request.request_id] = (
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    details.get("cached_tokens"),
                )
                self._record_stats(
                    model_key,
                    (time.time() - start_time) * 1000,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                )
            else:
                self._record_stats(
                    model_key, (time.time() - start_time) * 1000, None, None,
                )
            return data
        except Exception:
            self._failed[model_key] = self._failed.get(model_key, 0) + 1
            raise
        finally:
            self._release_slot(model_key)

    def get_queue_info(self) -> dict[str, dict]:
        """Return a queue-shaped dict so MLX shows up alongside Ollama queues.

        With admission control in place, the counters are now honest:
        ``_queued`` = waiting on the semaphore, ``_inflight`` = executing
        against mlx_lm.server.  Earlier version synthesized ``pending`` from
        ``inflight - 1`` which only worked by accident.
        """
        out: dict[str, dict] = {}
        all_models = (
            set(self._inflight)
            | set(self._queued)
            | set(self._completed)
            | set(self._failed)
            | set(self._rejected)
        )
        # Cache hit rate is currently single-tenant (mlx_lm.server runs one
        # model per process), so the same rate applies to every entry until
        # we go multi-model.  Reported as a fraction in [0, 1] or None.
        hit_rate = self.get_cache_hit_rate()
        for model_key in all_models:
            samples = self._stats_samples.get(model_key, 0)
            avg_latency_ms = (
                self._sum_latency_ms.get(model_key, 0.0) / samples if samples else 0.0
            )
            avg_prompt_tokens = (
                self._sum_prompt_tokens.get(model_key, 0) / samples if samples else 0.0
            )
            avg_completion_tokens = (
                self._sum_completion_tokens.get(model_key, 0) / samples if samples else 0.0
            )
            out[f"mlx-local:mlx:{model_key}"] = {
                "node_id": "mlx-local",
                "model": f"mlx:{model_key}",
                "pending": self._queued.get(model_key, 0),
                "in_flight": self._inflight.get(model_key, 0),
                "concurrency": 1,
                "max_queue_depth": self.max_queue_depth,
                "completed": self._completed.get(model_key, 0),
                "failed": self._failed.get(model_key, 0),
                # Requests rejected with 503 due to admission control —
                # distinct from `failed` (real errors) so operators can
                # tell "backend is overloaded" from "backend is broken".
                "rejected": self._rejected.get(model_key, 0),
                # Rolling prompt-cache hit rate (cached / total prompt
                # tokens, weighted by request size).  None until we have
                # observations.  See docs/plans/mlx-prompt-cache-optimization.md
                # for why this matters (target: ≥80% on cached turns).
                "cache_hit_rate": hit_rate,
                "backend": "mlx",
                "avg_latency_ms": round(avg_latency_ms, 1),
                "avg_prompt_tokens": round(avg_prompt_tokens, 1),
                "avg_completion_tokens": round(avg_completion_tokens, 1),
                "stats_samples": samples,
            }
        return out

    @staticmethod
    def _to_openai_body(request: InferenceRequest) -> dict:
        """Convert herd's Ollama-shaped raw_body into OpenAI chat.completions.

        Key differences:
          - Drop Ollama-specific fields (keep_alive, options wrapper)
          - Flatten options.{num_predict,temperature,top_p,...} to top level
          - Options names: num_predict → max_tokens, top_k stays
          - Strip any MLX prefix from the model name (mlx_lm doesn't expect it)
          - Tool schemas: Ollama uses {function: {name, parameters}};
            OpenAI uses {type: "function", function: {...}} — convert

        Raises:
            MlxModelMissingError: if ``request.model`` is empty/missing after
                stripping the ``mlx:`` prefix.  mlx_lm.server would otherwise
                respond with a confusing 404 (``[Errno 2] config.json``); we
                fail fast with a clear message instead.
        """
        raw = request.raw_body or {}
        options = raw.get("options", {}) or {}

        outbound_model = strip_mlx_prefix(request.model)
        if not outbound_model:
            # Defensive: 33 historical failures (2026-04-22) all came from
            # this exact case — empty model string sent to mlx_lm.server.
            # Root cause was elusive; this guard surfaces it loudly if it
            # ever recurs.  See docs/issues.md for the original incident.
            raise MlxModelMissingError(
                f"MlxProxy would send empty model name to mlx_lm.server. "
                f"InferenceRequest.model={request.model!r} request_id="
                f"{request.request_id} — check FLEET_ANTHROPIC_MODEL_MAP "
                f"and the route's local_model resolution."
            )
        # One INFO line per outbound request — invaluable when diagnosing
        # mismatched model names between herd and mlx_lm.server.  Cheap.
        logger.info(
            f"MLX proxy: forwarding request_id={request.request_id} "
            f"model={outbound_model} stream={raw.get('stream', True)} "
            f"tools={len(raw.get('tools') or [])} "
            f"messages={len(raw.get('messages') or [])}"
        )

        out: dict = {
            "model": outbound_model,
            # Translate Ollama-shaped messages → OpenAI shape mlx_lm.server
            # accepts.  The Anthropic translator (which built raw_body) emits
            # Ollama-friendly forms that Ollama accepts but mlx_lm.server
            # rejects with cryptic 404s — see the historical "tool_calls
            # arguments must be string" + "Only 'text' content type
            # supported" failures in docs/issues.md.
            "messages": _ollama_messages_to_openai(raw.get("messages", [])),
            "stream": raw.get("stream", True),
        }
        # Flatten Ollama options to OpenAI top-level params
        if "num_predict" in options:
            out["max_tokens"] = options["num_predict"]
        if "temperature" in options:
            out["temperature"] = options["temperature"]
        if "top_p" in options:
            out["top_p"] = options["top_p"]
        if "stop" in options:
            out["stop"] = options["stop"]

        # Tools — OpenAI spec wraps each function in {type:"function", function:{...}}
        if raw.get("tools"):
            openai_tools = []
            for t in raw["tools"]:
                if not isinstance(t, dict):
                    continue
                # Ollama shape: {type:"function", function:{name, description, parameters}}
                # OR older: {name, description, parameters} at top level
                if t.get("type") == "function" and "function" in t:
                    openai_tools.append(t)
                elif "name" in t:
                    openai_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": t.get("name"),
                                "description": t.get("description", ""),
                                "parameters": t.get(
                                    "parameters", t.get("input_schema", {}),
                                ),
                            },
                        }
                    )
            if openai_tools:
                out["tools"] = openai_tools

        return out


class _MlxToolState:
    """Per-tool-call state accumulator for streaming OpenAI → Anthropic translation.

    OpenAI streams tool-call arguments as JSON-string fragments across many
    chunks; we need to track which tool index has been opened, remember its
    block index + id, and accumulate the arguments so we can emit Anthropic
    ``input_json_delta`` events incrementally.
    """

    __slots__ = ("block_index", "id", "name", "started", "args_buffer")

    def __init__(self) -> None:
        self.block_index: int | None = None
        self.id: str = ""
        self.name: str = ""
        self.started: bool = False
        self.args_buffer: str = ""


def openai_sse_to_anthropic_events(
    raw_line: str,
    state,
    tools_state: dict[int, _MlxToolState],
    request_id: str,
) -> list[str]:
    """Translate one OpenAI SSE line into Anthropic SSE events.

    ``state`` is an ``AnthropicSSEState`` (same object the Ollama path uses —
    we reuse it so the Anthropic client sees identical events regardless of
    backend).  ``tools_state`` is per-call scratch for tool-call accumulation
    because ``AnthropicSSEState`` only tracks emitted tools as a flat list.

    Returns a list of fully-formatted SSE strings.

    OpenAI stream shape (one chunk per line):
        data: {"id":"...","choices":[{"delta":{"content":"Hi"},"index":0}]}
        data: {"id":"...","choices":[{"delta":{"tool_calls":[
                    {"index":0,"function":{"name":"x","arguments":"..."}}
                ]}}]}
        data: {"id":"...","choices":[{"finish_reason":"stop","delta":{}}]}
        data: [DONE]
    """
    line = raw_line.strip()
    if not line:
        return []
    if line.startswith("data: "):
        line = line[6:].strip()
    if line == "[DONE]":
        # Completion marker — the translator may already have emitted
        # message_stop on finish_reason; nothing to do here.
        return []
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return []

    def _capture_usage(usage: dict) -> None:
        """Pull (prompt, completion, cached) tokens out of a usage dict.

        ``cached_tokens`` lives at ``prompt_tokens_details.cached_tokens``
        in OpenAI-spec responses (which mlx_lm.server now follows).  Older
        mlx versions or other backends may omit it; treat as None.
        """
        details = usage.get("prompt_tokens_details") or {}
        _mlx_request_tokens[request_id] = (
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            details.get("cached_tokens"),
        )

    choices = chunk.get("choices") or []
    if not choices:
        # mlx_lm occasionally emits a final usage-only chunk with choices=[]
        usage = chunk.get("usage")
        if usage:
            _capture_usage(usage)
        return []
    choice = choices[0]
    delta = choice.get("delta") or {}
    events: list[str] = []

    # Usage on any chunk (mlx_lm sometimes includes it alongside deltas)
    usage = chunk.get("usage")
    if usage:
        _capture_usage(usage)

    def _alloc_block_index() -> int:
        idx = state.next_block_index
        state.next_block_index = idx + 1
        return idx

    # --- First chunk: emit message_start ---
    if not state.started:
        state.started = True
        if chunk.get("id"):
            state.message_id = chunk["id"]
        events.append(
            "event: message_start\ndata: "
            + json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": state.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": state.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": state.input_tokens or 0,
                            "output_tokens": 0,
                        },
                    },
                }
            )
            + "\n\n"
        )

    # --- Text content ---
    text = delta.get("content")
    if text:
        if not state.text_open:
            state.text_block_index = _alloc_block_index()
            state.text_open = True
            events.append(
                "event: content_block_start\ndata: "
                + json.dumps(
                    {
                        "type": "content_block_start",
                        "index": state.text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    }
                )
                + "\n\n"
            )
        state.output_tokens += max(1, len(text) // 4)
        events.append(
            "event: content_block_delta\ndata: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": state.text_block_index,
                    "delta": {"type": "text_delta", "text": text},
                }
            )
            + "\n\n"
        )

    # --- Tool call deltas ---
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        if idx not in tools_state:
            tools_state[idx] = _MlxToolState()
        tstate = tools_state[idx]
        fn = tc.get("function") or {}
        name = fn.get("name")
        if name and not tstate.started:
            # Close any open text block first
            if state.text_open:
                events.append(
                    "event: content_block_stop\ndata: "
                    + json.dumps(
                        {
                            "type": "content_block_stop",
                            "index": state.text_block_index,
                        }
                    )
                    + "\n\n"
                )
                state.text_open = False
            tstate.block_index = _alloc_block_index()
            tstate.id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
            tstate.name = name
            tstate.started = True
            events.append(
                "event: content_block_start\ndata: "
                + json.dumps(
                    {
                        "type": "content_block_start",
                        "index": tstate.block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tstate.id,
                            "name": name,
                            "input": {},
                        },
                    }
                )
                + "\n\n"
            )
        args_fragment = fn.get("arguments")
        if args_fragment is not None and tstate.started:
            tstate.args_buffer += args_fragment
            events.append(
                "event: content_block_delta\ndata: "
                + json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": tstate.block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_fragment,
                        },
                    }
                )
                + "\n\n"
            )

    # --- finish_reason → close all blocks + emit message_delta + message_stop ---
    finish = choice.get("finish_reason")
    if finish and not state.finished:
        # Close any open tool_use blocks (reverse-order, though any order is fine)
        for tstate in tools_state.values():
            if tstate.started:
                events.append(
                    "event: content_block_stop\ndata: "
                    + json.dumps(
                        {
                            "type": "content_block_stop",
                            "index": tstate.block_index,
                        }
                    )
                    + "\n\n"
                )
                # Add to emitted_tools list for downstream logging/trace
                state.emitted_tools.append({"id": tstate.id, "name": tstate.name})
                tstate.started = False
        if state.text_open:
            events.append(
                "event: content_block_stop\ndata: "
                + json.dumps(
                    {
                        "type": "content_block_stop",
                        "index": state.text_block_index,
                    }
                )
                + "\n\n"
            )
            state.text_open = False
        # Map OpenAI finish_reason → Anthropic stop_reason
        if finish == "tool_calls":
            state.stop_reason = "tool_use"
        elif finish == "length":
            state.stop_reason = "max_tokens"
        elif finish == "stop":
            state.stop_reason = "end_turn"
        else:
            state.stop_reason = "end_turn"
        state.finished = True
        events.append(
            "event: message_delta\ndata: "
            + json.dumps(
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": state.stop_reason,
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": state.output_tokens},
                }
            )
            + "\n\n"
        )
        events.append(
            "event: message_stop\ndata: "
            + json.dumps({"type": "message_stop"})
            + "\n\n"
        )

    return events


def build_anthropic_non_streaming_response(
    openai_response: dict,
    anthropic_model_name: str,
) -> dict:
    """Convert an OpenAI non-streaming completion to Anthropic Messages shape."""
    choice = (openai_response.get("choices") or [{}])[0]
    message = choice.get("message", {}) or {}
    content_blocks: list[dict] = []

    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {"_raw": raw_args}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                "name": fn.get("name", "unknown"),
                "input": args,
            }
        )

    finish = choice.get("finish_reason", "stop")
    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }.get(finish, "end_turn")

    usage = openai_response.get("usage", {}) or {}
    return {
        "id": openai_response.get("id", f"msg_{uuid.uuid4().hex[:12]}"),
        "type": "message",
        "role": "assistant",
        "model": anthropic_model_name,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def record_trace_mlx(
    trace_store,
    request: InferenceRequest,
    start_time: float,
    first_token_time: float | None,
    status: str,
    error_message: str | None = None,
) -> None:
    """Record a trace for an MLX-served request, matching StreamingProxy format."""
    if not trace_store:
        return
    import asyncio

    elapsed_ms = (time.time() - start_time) * 1000
    ttft_ms = (first_token_time - start_time) * 1000 if first_token_time else None
    # Tuple is (prompt, completion, cached) — cached_tokens is consumed by
    # the route via pop_token_counts (which folds it into rolling stats);
    # we just need prompt/completion for the trace record here.
    tok_entry = _mlx_request_tokens.get(request.request_id, (None, None, None))
    prompt_tokens = tok_entry[0]
    completion_tokens = tok_entry[1]

    async def _record():
        try:
            await trace_store.record_trace(
                request_id=request.request_id,
                model=request.model,
                original_model=request.original_model or request.model,
                node_id="mlx-local",  # pseudo-node id for MLX backend
                score=None,
                scores_breakdown=None,
                status=status,
                latency_ms=elapsed_ms,
                time_to_first_token_ms=ttft_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                retry_count=0,
                fallback_used=False,
                excluded_nodes=None,
                original_format=request.original_format.value
                if isinstance(request.original_format, RequestFormat)
                else "anthropic",
                error_message=error_message,
                tags=request.tags if request.tags else None,
            )
        except Exception as exc:
            logger.error(f"MLX trace record failed: {exc}")

    asyncio.create_task(_record())
