"""Queue Manager — per node:model queues with dynamic concurrent workers."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from fleet_manager.models.request import QueueEntry, RequestStatus
from fleet_manager.server.serializers import (
    decode_parallelism_for,
    distinct_resident_models,
    model_resident_on_node,
)

logger = logging.getLogger(__name__)

# Zombie reaper event tracking for health visibility
_reaper_events: list[dict] = []


class ClientConcurrencyExceeded(Exception):
    """Raised by :meth:`QueueManager.enqueue` when a client is over its
    per-client in-flight cap.  Carries ``retry_after`` so the route can return
    ``429`` + ``Retry-After`` instead of piling the request onto the queue."""

    def __init__(self, client_ip: str, limit: int, retry_after: int):
        self.client_ip = client_ip
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(f"client {client_ip or '<anon>'} exceeded max in-flight={limit}")


def get_reaper_events(hours: float = 24) -> list[dict]:
    """Return zombie reaper events from the last N hours."""
    cutoff = time.time() - (hours * 3600)
    return [e for e in _reaper_events if e["timestamp"] >= cutoff]


# Estimated KV cache memory per concurrent request (GB).
# Conservative: large models need more, small models less, but 2GB is a
# reasonable middle ground that prevents over-subscription.
_KV_CACHE_PER_REQUEST_GB = 2.0

# Bounds for auto-calculated concurrency per queue.
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 8

# Fallback only: an in-flight entry that has made no PROGRESS for this long is
# considered wedged.  `ServerSettings.stale_timeout` always wins when present,
# which it is, so this value was dead code -- and it read 900 with a "15 minutes"
# comment while the effective threshold was 600.  Kept aligned so the two cannot
# disagree again.
_STALE_NO_PROGRESS_SECONDS = 600.0

# How often to run the stale reaper (seconds).
_REAPER_INTERVAL_SECONDS = 60

# How often a request held for a load on its node checks whether it may go.
_LOAD_HOLD_POLL_SECONDS = 0.25

# The longest a load that must evict a busy model waits for one to go idle
# before it is sent anyway, and the resident models make room for it
# (``QueueManager._must_wait``).  Long enough that a node does not swap models
# on every request, short next to a client's timeout: a few cold loads.
_LOAD_WAIT_SECONDS = 30.0

# The longest requests for a resident model are held to give a load on their
# node the room (``QueueManager._must_wait``, second rule), counted from when
# the load reached the node.  Ollama evicts a busy model once its running
# requests finish, so a load past this is more likely stuck than waiting, and
# the resident model's clients should not pay for it until the stale reaper
# (600 s).  Generous next to a cold load (gpt-oss:120b, 65 GB: 45-60 s).
_LOAD_ROOM_SECONDS = 120.0

# How long after herd's last request for a model on a node a heartbeat saying
# it is not resident may be stale (``QueueManager.should_pin``).  Heartbeats
# arrive every 5 s; the margin covers a slow one.
_RESIDENCY_LAG_SECONDS = 30.0


def compute_concurrency(available_memory_gb: float, model_size_gb: float) -> int:
    """Calculate how many concurrent requests a node can handle for a model.

    Uses the memory headroom after the model is loaded divided by an estimated
    per-request KV cache cost.  Clamped to [1, 8].
    """
    headroom = available_memory_gb - model_size_gb
    if headroom <= 0:
        return _MIN_CONCURRENCY
    slots = int(headroom / _KV_CACHE_PER_REQUEST_GB)
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, slots))


class QueueDepths(dict):
    """Each ``node:model`` queue's depth, as the scorer reads it, plus
    ``loading``: the queues whose model has a load under way there (an
    in-flight request that has produced nothing yet).

    Depth alone cannot tell "a load is on its way" from "requests are queued
    behind a resident model", and the scorer's ``joins_load`` needs the first:
    it cancels a cold load's eviction charge only to send requests after a
    load already started, not to stack a churn onto a node that merely has
    work queued (findings item 30).  A plain dict reads as nothing loading.
    """

    def __init__(self, depths=(), *, loading: frozenset[str] = frozenset()):
        super().__init__(depths)
        self.loading = loading


def _load_under_way(q: DeviceModelQueue) -> bool:
    """Is a request for ``q``'s model at its node and still producing nothing?
    One definition, for the load hold and the scorer.  Held requests are not
    in flight, so a backlog waiting its turn never counts as a load."""
    return any(e.last_progress_at is None for e in q.in_flight.values())


@dataclass
class DeviceModelQueue:
    node_id: str
    model: str
    pending: asyncio.Queue = field(default_factory=asyncio.Queue)
    in_flight: dict[str, QueueEntry] = field(default_factory=dict)  # keyed by request_id
    # Set when the request leaves this queue (completed, failed, reaped).  The
    # worker that dispatched it waits on this, so it holds its slot for the
    # request's whole duration — that is what makes `concurrency` a real limit.
    slots: dict[str, asyncio.Event] = field(default_factory=dict)  # keyed by request_id
    worker_tasks: list[asyncio.Task] = field(default_factory=list)
    # Taken by a worker but held back for a load on this node
    # (``QueueManager._must_wait``); counted as pending.
    held: int = 0
    # Requests herd sends to the node itself, not through a worker (embeddings,
    # ``QueueManager.direct``): counted in the depth, never in ``in_flight``.
    direct: int = 0
    # When a request for this model on this node last finished (``should_pin``).
    last_finished_at: float = 0.0
    # Chunks streamed for this model on this node, cumulative
    # (``QueueManager.progress``): the dashboard's live rate is its change.
    streamed_chunks: int = 0
    concurrency: int = _MIN_CONCURRENCY
    completed_count: int = 0
    failed_count: int = 0
    # Running sums for dashboard averages — same lifecycle as the counts
    # above (reset on restart, no persistence).  Only incremented on
    # successful completions where stats are available; ``stats_samples``
    # is the denominator for averages and may be ≤ ``completed_count`` if a
    # completion didn't yield token counts (e.g. non-streaming image gen).
    total_latency_ms: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    stats_samples: int = 0


class QueueManager:
    def __init__(self, registry=None, settings=None):
        self._queues: dict[str, DeviceModelQueue] = {}
        self._lock = asyncio.Lock()
        self._registry = registry
        self._settings = settings
        self._stale_timeout = (
            settings.stale_timeout
            if settings and hasattr(settings, "stale_timeout")
            else _STALE_NO_PROGRESS_SECONDS
        )
        self._reaper_task: asyncio.Task | None = None
        # Per-client concurrency accounting.  ``_client_in_flight`` counts
        # reserved slots per client IP; ``_counted_requests`` makes release
        # idempotent (a request is released exactly once no matter which
        # terminal path — complete / fail / reap — fires).  All ops are
        # sync + await-free, so they're atomic under asyncio without a lock.
        self._client_in_flight: dict[str, int] = {}
        self._counted_requests: set[str] = set()

    def _acquire_client(self, entry: QueueEntry) -> None:
        """Reserve a client-concurrency slot, or raise ClientConcurrencyExceeded.

        No-op when the cap is 0 (disabled, default) or the caller is anonymous
        (no IP to bound).  Check-and-increment is a single await-free sequence,
        so it's atomic under asyncio's single-threaded scheduling.
        """
        limit = getattr(self._settings, "client_max_in_flight", 0) if self._settings else 0
        if limit <= 0:
            return
        ip = entry.request.client_ip or ""
        if not ip:
            return
        if self._client_in_flight.get(ip, 0) >= limit:
            retry_after = getattr(self._settings, "client_concurrency_retry_after", 2)
            raise ClientConcurrencyExceeded(ip, limit, retry_after)
        self._client_in_flight[ip] = self._client_in_flight.get(ip, 0) + 1
        self._counted_requests.add(entry.request.request_id)

    def _release_client(self, entry: QueueEntry) -> None:
        """Release a client-concurrency slot.  Idempotent per request."""
        rid = entry.request.request_id
        if rid not in self._counted_requests:
            return
        self._counted_requests.discard(rid)
        ip = entry.request.client_ip or ""
        remaining = self._client_in_flight.get(ip, 0) - 1
        if remaining > 0:
            self._client_in_flight[ip] = remaining
        else:
            self._client_in_flight.pop(ip, None)

    def start_reaper(self):
        """Start the background stale in-flight reaper."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_stale_in_flight())

    async def _reap_stale_in_flight(self):
        """Periodically remove in-flight entries that have been stuck too long."""
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
                now = time.time()
                for key, q in list(self._queues.items()):
                    # Stale means "produced nothing recently", NOT "has been
                    # running a long time".  The old test was
                    # `now - started_at > timeout`, which cannot distinguish a
                    # slow stream from a dead one -- and on a saturated box the
                    # two are easy to confuse: gpt-oss decode fell 76 -> 24
                    # tok/s under load here, so the same 8,000-token reply went
                    # from ~106s to ~330s of entirely healthy work.  On
                    # 2026-10-06 that reaped the slot of a request still
                    # mid-stream, which then completed normally with 9,610
                    # tokens, releasing a concurrency slot the request still
                    # held and recording a failure the trace store denied.
                    #
                    # `last_progress_at` falls back to `started_at` so a request
                    # that has produced *nothing* is still reaped on age: that is
                    # the genuine zombie this exists for -- a stream that was
                    # never consumed, so no mark_* will ever run.
                    stale = [
                        (rid, e)
                        for rid, e in q.in_flight.items()
                        if (e.last_progress_at or e.started_at)
                        and (now - (e.last_progress_at or e.started_at)) > self._stale_timeout
                    ]
                    for rid, entry in stale:
                        del q.in_flight[rid]
                        # The safety net for a request whose stream was never
                        # consumed (so no mark_* ever runs): without this the
                        # worker holding its slot would wait forever.
                        self._release_slot(q, rid)
                        self._release_client(entry)
                        entry.status = RequestStatus.FAILED
                        entry.completed_at = now
                        q.failed_count += 1
                        last = entry.last_progress_at or entry.started_at
                        idle = int(now - last)
                        age = int(now - entry.started_at) if entry.started_at else idle
                        _reaper_events.append(
                            {
                                "timestamp": now,
                                "request_id": entry.request.request_id,
                                "queue_key": key,
                                "stuck_seconds": idle,
                                # Both, because they differ and the difference is the
                                # diagnosis: idle ~= age means it never produced
                                # anything; idle << age means it stalled mid-stream.
                                "age_seconds": age,
                                "produced_output": entry.last_progress_at is not None,
                            }
                        )
                        if len(_reaper_events) > 100:
                            _reaper_events.pop(0)
                        logger.warning(
                            f"Reaped stale in-flight {entry.request.request_id[:8]} "
                            f"from {key} (no output for {idle}s, age {age}s, "
                            f"produced_output={entry.last_progress_at is not None})"
                        )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error in stale in-flight reaper")

    def get_queue_depths(self) -> QueueDepths:
        """Current depth (pending + held + in flight + direct) for all queues,
        plus which of them have a load under way (``QueueDepths.loading``)."""
        return QueueDepths(
            {
                key: q.pending.qsize() + q.held + len(q.in_flight) + q.direct
                for key, q in self._queues.items()
            },
            loading=frozenset(key for key, q in self._queues.items() if _load_under_way(q)),
        )

    def get_queue_info(self) -> dict[str, dict]:
        """Return detailed queue information for the fleet status endpoint."""
        info = {}
        for key, q in self._queues.items():
            # Infer request type from in-flight entries or model knowledge
            request_type = "text"
            if q.in_flight:
                first_entry = next(iter(q.in_flight.values()))
                request_type = getattr(first_entry.request, "request_type", "text")
            else:
                from fleet_manager.server.model_knowledge import ModelType, model_type

                request_type = {
                    ModelType.IMAGE: "image",
                    ModelType.STT: "stt",
                    ModelType.EMBED: "embed",
                }.get(model_type(q.model), "text")
            # Running averages since process start.  ``stats_samples`` is the
            # honest denominator — may be below completed_count when some
            # completions didn't carry tokens (e.g. image gen).  Expose it
            # alongside the averages so the dashboard can decide whether to
            # render or suppress (e.g. "—" when samples == 0).
            samples = q.stats_samples
            avg_latency_ms = (q.total_latency_ms / samples) if samples else 0.0
            avg_prompt_tokens = (q.total_prompt_tokens / samples) if samples else 0.0
            avg_completion_tokens = (q.total_completion_tokens / samples) if samples else 0.0
            info[key] = {
                "node_id": q.node_id,
                "model": q.model,
                "pending": q.pending.qsize() + q.held,
                "in_flight": len(q.in_flight),
                # Cumulative; the dashboard reads its change as the live rate.
                "streamed_chunks": q.streamed_chunks,
                # Embeddings at the node (``direct``): load for the scorer, but not
                # a backlog the rebalancer should pre-warm or move work for.
                "direct": q.direct,
                "completed": q.completed_count,
                "failed": q.failed_count,
                "concurrency": q.concurrency,
                "request_type": request_type,
                "avg_latency_ms": round(avg_latency_ms, 1),
                "avg_prompt_tokens": round(avg_prompt_tokens, 1),
                "avg_completion_tokens": round(avg_completion_tokens, 1),
                "stats_samples": samples,
            }
        return info

    def _compute_queue_concurrency(self, node_id: str, model: str) -> int:
        """Determine concurrency for a queue based on live node metrics."""
        if self._registry is None:
            return _MIN_CONCURRENCY

        node = self._registry.get_node(node_id)
        if node is None or node.memory is None or node.ollama is None:
            return _MIN_CONCURRENCY

        available_gb = node.memory.available_gb

        # Find the model's loaded size, fall back to 0 (small model on disk).
        # Largest entry, not the first: one name can have two runners when an
        # Ollama upgrade rewrites a resident model's digest, and the first match
        # is an arbitrary order.  This feeds concurrency/memory math, where
        # understating resident size is the unsafe direction.
        model_size_gb = max(
            (m.size_gb for m in node.ollama.models_loaded if m.name == model),
            default=0.0,
        )
        backend_limit = decode_parallelism_for(node, model)

        # A model resident in Ollama has already paid for every slot it admits:
        # llama-server allocates the weights and the KV cache for all
        # `-np` slots (`-c NumCtx x numParallel`) when it loads, and
        # `available_gb` is measured after that.  Subtracting the model again
        # and charging each request a fresh KV cache counted it twice, which
        # held a 24 GB Mac to one request against Ollama's four (run 6:
        # (8.47 - 5.22) / 2 -> 1; per-request decode flat at 22.4 tok/s while
        # time to first token grew with concurrency).  Not `mlx:` models:
        # mlx_lm.server grows its KV cache per request.
        if model_size_gb > 0 and not model.startswith("mlx:") and backend_limit > 0:
            return self._capacity_capped(node, backend_limit)

        # If capacity learning is active, respect the ceiling
        if node.capacity and node.capacity.ceiling_gb > 0:
            available_gb = min(available_gb, node.capacity.ceiling_gb)

        concurrency = compute_concurrency(available_gb, model_size_gb)

        # Never exceed what the backend will actually decode concurrently.
        # `compute_concurrency` answers "how many KV caches fit in RAM?", which
        # on a large-memory node is always the ceiling — but the binding limit
        # is Ollama's own admission cap.  Workers beyond it don't decode; they
        # block inside Ollama, invisible to the queue that is supposed to be
        # managing them.
        if backend_limit > 0:
            concurrency = min(concurrency, backend_limit)
        return self._capacity_capped(node, concurrency)

    @staticmethod
    def _capacity_capped(node, concurrency: int) -> int:
        """``concurrency``, lowered while the capacity learner says the
        machine's owner is busy (``CapacityMetrics.slot_cap``).  Applied to
        every model, resident or not, as its own rule rather than through the
        memory ceiling.  Recomputed on each enqueue (``_ensure_workers``), so a
        new band takes effect from the next request."""
        concurrency = max(_MIN_CONCURRENCY, concurrency)
        if node.capacity:
            return max(_MIN_CONCURRENCY, min(concurrency, node.capacity.slot_cap(concurrency)))
        return concurrency

    def _ensure_workers(self, q: DeviceModelQueue, queue_key: str):
        """Ensure the right number of workers are running for a queue."""
        # Recalculate concurrency from live node data
        target = self._compute_queue_concurrency(q.node_id, q.model)
        q.concurrency = target

        # Clean up finished workers
        q.worker_tasks = [t for t in q.worker_tasks if not t.done()]

        # Spawn more workers if needed
        while len(q.worker_tasks) < target:
            worker_id = len(q.worker_tasks)
            task = asyncio.create_task(self._worker(q, worker_id))
            q.worker_tasks.append(task)

        if target > 1:
            logger.debug(f"Queue {queue_key}: {len(q.worker_tasks)} workers (target={target})")

    async def enqueue(
        self,
        entry: QueueEntry,
        process_fn,
    ) -> asyncio.Future:
        """
        Add a request to the appropriate queue.
        Returns a Future that resolves to an async generator of response chunks.

        Raises :class:`ClientConcurrencyExceeded` if the caller is already at
        its per-client in-flight cap — the route turns that into 429 rather
        than queueing the request (backpressure, not amplification).
        """
        # Reserve the client-concurrency slot first — reject fast, before any
        # queue/worker work, so an over-cap caller can't grow the backlog.
        self._acquire_client(entry)

        queue_key = f"{entry.assigned_node}:{entry.request.model}"

        async with self._lock:
            if queue_key not in self._queues:
                q = DeviceModelQueue(node_id=entry.assigned_node, model=entry.request.model)
                self._queues[queue_key] = q
            else:
                q = self._queues[queue_key]

        loop = asyncio.get_running_loop()
        response_future = loop.create_future()

        await q.pending.put((entry, response_future, process_fn))
        logger.debug(
            f"Enqueued {entry.request.request_id[:8]} to {queue_key} "
            f"(depth={q.pending.qsize() + len(q.in_flight)})"
        )

        # Ensure correct number of workers are running
        self._ensure_workers(q, queue_key)

        return response_future

    async def _worker(self, q: DeviceModelQueue, worker_id: int = 0):
        """Worker loop for a single queue: one request at a time, start to finish.

        ``process_fn`` returns an *unconsumed* stream; the request only reaches
        the backend as the route reads it.  So the worker can't treat handing
        the stream off as "done" — it holds its slot until the request leaves
        this queue (``mark_completed`` / ``mark_failed`` / the reaper).  Before
        this, a worker handed off and immediately took the next request, so one
        worker dispatched the whole queue and ``concurrency`` bounded nothing.
        """
        me = asyncio.current_task()
        while True:
            # Retire if the limit dropped below the live worker count (e.g. node
            # metadata arrived after this queue was created and says the model
            # is serial).  Rank among live workers is unique and stable, so
            # exactly the excess retire — never all of them at once.
            live = [t for t in q.worker_tasks if not t.done()]
            if me in live and live.index(me) >= q.concurrency:
                logger.debug(f"Queue {q.node_id}:{q.model} worker {worker_id} retiring")
                break

            try:
                entry, future, process_fn = await asyncio.wait_for(q.pending.get(), timeout=300.0)
            except TimeoutError:
                logger.debug(f"Queue {q.node_id}:{q.model} worker {worker_id} idle, stopping")
                break

            if self._must_wait(q, entry):
                q.held += 1
                try:
                    while self._must_wait(q, entry) and not future.cancelled():
                        await asyncio.sleep(_LOAD_HOLD_POLL_SECONDS)
                finally:
                    q.held -= 1

            if future.cancelled():
                # The caller went away while queued (a client disconnect cancels
                # the route awaiting this future).  Don't start work nobody will
                # read — its stream would never be consumed, so no mark_* would
                # ever free the slot.
                self._release_client(entry)
                continue

            rid = entry.request.request_id
            entry.status = RequestStatus.IN_FLIGHT
            entry.started_at = time.time()
            q.in_flight[rid] = entry
            released = asyncio.Event()
            q.slots[rid] = released

            try:
                stream = process_fn(entry)
                if hasattr(stream, "__aiter__"):
                    stream = self._settle_on_exit(q, entry, stream)
                if not future.done():
                    future.set_result(stream)
            except Exception as e:
                # Nothing was dispatched and no stream will settle it: take it
                # out of ``in_flight`` (a phantom load otherwise, until the
                # reaper) and free its slot and client reservation here.  The
                # proxy's process_fns trace it (``StreamingProxy._traced``).
                self.mark_failed(f"{q.node_id}:{q.model}", entry)
                if not future.done():
                    future.set_exception(e)
                logger.error(f"Queue worker error for {rid}: {e}")
                continue

            # Drop our references to the stream before waiting.  The future's
            # result IS the stream, and a stream is only finalized -- the
            # process_fn's own GeneratorExit handler calls mark_failed, which
            # releases this very slot -- once nothing references it.  A streaming client that
            # disconnects mid-response leaves its stream suspended at a yield;
            # if this frame still held it, the slot would wait for the reaper
            # (~11 min) and the model's queue would freeze.  Found live.
            stream = future = None

            # Belt and braces for a request that never leaves the queue at
            # all.  This MUST use the same no-progress rule as the reaper.
            #
            # It used to be a single `wait_for(..., stale_timeout + interval)`,
            # sized to fire 60 s *after* the reaper so the reaper would always
            # act first and this path stayed dead code.  Making the reaper
            # progress-aware (2026-10-06) removed its false positives and
            # promoted this one: on 10-06 20:42 and 10-07 02:42 it released the
            # slots of two requests still mid-stream, which went on to return
            # 6,557 and 12,809 tokens.  Fixing one timeout and leaving the other
            # just moved the bug 60 s later.
            #
            # So: wake on the reaper's cadence, and only give up when the entry
            # has produced nothing for `stale_timeout`.  `last_progress_at`
            # falls back to `started_at`, so a request that never produced
            # anything is still released on age -- the genuine stuck case.
            try:
                while True:
                    try:
                        await asyncio.wait_for(released.wait(), timeout=_REAPER_INTERVAL_SECONDS)
                        break
                    except TimeoutError:
                        last = entry.last_progress_at or entry.started_at or time.time()
                        idle = time.time() - last
                        if idle > self._stale_timeout:
                            logger.warning(
                                f"Queue {q.node_id}:{q.model} worker {worker_id}: "
                                f"{rid[:8]} produced nothing for {int(idle)}s "
                                f"— releasing the slot"
                            )
                            break
            finally:
                q.slots.pop(rid, None)

    @contextmanager
    def direct(self, node_id: str, model: str):
        """Count a request herd sends to ``node_id`` itself rather than through a
        queue worker (embeddings) in that node and model's depth while it runs.

        Without it the scorer saw no load from embeddings at all: on the
        2026-11-21 fleet benchmark (run 5) the first embeddings of a run went to
        an A10 that also held the coding and image models, each then took about
        15 s there, and every later one followed them (queue 0, affinity) while
        the A100 had the model loaded and idle: 50 answered in 180 s against
        about 10,300 when the first went to the A100.  Kept out of ``in_flight``:
        an embedding is never a load to wait for or to join, and never a backlog
        for the rebalancer, which reads ``in_flight`` from ``get_queue_info``.
        Counted there, four embeddings in flight at 70 ms each looked like a
        deep queue: on the hardware check of this fix the rebalancer pre-warmed
        the embedding model 43 times onto an A10 serving the coding model, and
        coding requests fell 13%.
        """
        key = f"{node_id}:{model}"
        q = self._queues.get(key)
        if q is None:
            q = self._queues[key] = DeviceModelQueue(node_id=node_id, model=model)
        q.direct += 1
        try:
            yield
        finally:
            q.direct -= 1
            q.last_finished_at = time.time()

    @contextmanager
    def loading(self, entry: QueueEntry):
        """Count a load herd sends itself (a pre-warm) as what it is while it
        runs: an in-flight request for its model on its node that has produced
        nothing yet.

        Sent outside the queue, a pre-warm was invisible: the node kept pinning
        the model it was about to evict and kept taking that model's traffic,
        and Ollama, which evicts a busy model by waiting for it to go idle,
        loaded nothing else there for up to fifteen minutes (run 6).  In
        ``in_flight`` it is a pending load to ``load_pending_on`` /
        ``should_pin`` and the load hold, like any other.  No worker slot and no
        completed/failed counts: it is not a client request.
        """
        key = f"{entry.assigned_node}:{entry.request.model}"
        q = self._queues.get(key)
        if q is None:
            q = self._queues[key] = DeviceModelQueue(
                node_id=entry.assigned_node, model=entry.request.model
            )
        rid = entry.request.request_id
        entry.status = RequestStatus.IN_FLIGHT
        entry.started_at = time.time()
        entry.last_progress_at = None
        q.in_flight[rid] = entry
        try:
            yield
        finally:
            q.in_flight.pop(rid, None)
            q.last_finished_at = time.time()

    def speculative_load_allowed(self, entry: QueueEntry) -> bool:
        """May a load nobody asked for (a pre-warm) go to its node now?

        Not when it would have to evict a busy model with no idle room: Ollama
        would wait for that model to go idle and load nothing else on the node
        meanwhile, and cancelling the request does not end Ollama's wait.  So
        the only bound that works is not sending it.  A request that must evict
        waits its turn (``_must_wait``); a speculative one is dropped.
        """
        if self._registry is None:
            return True
        node = self._registry.get_node(entry.assigned_node)
        if node is None or node.ollama is None:
            return True
        if not self._evicting_load(entry):
            return True
        depths = self.get_queue_depths()
        if not any(depths.get(f"{node.node_id}:{m.name}") for m in node.ollama.models_loaded):
            return True  # nothing busy to wait for
        return self._idle_room_for(node, node.node_id, entry.request.model, depths)

    def progress(self, entry: QueueEntry) -> None:
        """A chunk arrived for ``entry``: stamp its progress (the stale reaper
        and every "load under way" check read it) and count it for the
        dashboard's live rate (``streamed_chunks``)."""
        entry.last_progress_at = time.time()
        q = self._queues.get(f"{entry.assigned_node}:{entry.request.model}")
        if q is not None:
            q.streamed_chunks += 1

    def free_slots(self, queue_key: str) -> int:
        """How many more requests ``queue_key``'s node would start for its
        model right now: its concurrency less what is in flight, queued or
        held there.  The rebalancer moves backlog up to this, so a node that
        frees up takes as much as it can serve at once, never more."""
        node_id, _, model = queue_key.partition(":")
        q = self._queues.get(queue_key)
        busy = len(q.in_flight) + q.pending.qsize() + q.held if q else 0
        return max(0, self._compute_queue_concurrency(node_id, model) - busy)

    def load_pending_on(self, node_id: str, model: str) -> bool:
        """Is a request for another model on ``node_id`` at the node and still
        waiting for its first token?  It may be a load waiting for a resident
        model to go idle, which a ``keep_alive`` sent to that model now would
        cancel (``StreamingProxy._build_ollama_body``).  From herd's own queue,
        not from heartbeats, which lag a load by up to one interval: deciding
        from residency let a request herd took for a load re-arm a model that
        had just loaded and that a later load was waiting to evict."""
        return any(
            e.last_progress_at is None
            for q in self._queues.values()
            if q.node_id == node_id and q.model != model
            for e in q.in_flight.values()
        )

    def should_pin(self, node_id: str, model: str) -> bool:
        """Send ``keep_alive: -1`` with a request for ``model`` on ``node_id``?

        Yes unless a load may be waiting on the node (``load_pending_on``):
        there the keep-alive would cancel that load's eviction of this model.
        Except when ``model`` is certainly not resident, because then the
        request is itself a load and its keep-alive only sets its own new
        runner.  Left out, Ollama gives that runner ``OLLAMA_KEEP_ALIVE``,
        five minutes on a default install, and the model herd just loaded
        unloads if nothing asks for it in that time.

        "Certainly" because heartbeats lag a load: a model herd loaded seconds
        ago can still be listed as absent, and pinning it then re-arms the model
        a waiting load must evict (the run 4 stall).  So herd's own record has
        to agree: nothing for it at the node producing tokens or embedding, and
        nothing finished there within ``_RESIDENCY_LAG_SECONDS``.
        """
        if not self.load_pending_on(node_id, model):
            return True
        if self._registry is None:
            return False
        node = self._registry.get_node(node_id)
        if node is None or node.ollama is None or model_resident_on_node(model, node):
            return False
        q = self._queues.get(f"{node_id}:{model}")
        if q is None:
            return True
        if q.direct or any(e.last_progress_at for e in q.in_flight.values()):
            return False
        return time.time() - q.last_finished_at > _RESIDENCY_LAG_SECONDS

    def _evicting_load(self, e: QueueEntry) -> bool:
        """Was ``e`` routed as a load that evicts?  A negative ``eviction`` score,
        or routed to its model resident (``thermal`` hot): asked about once that
        model is no longer resident, loading it back evicts what took its place."""
        b = e.routing_breakdown or {}
        hot = getattr(self._settings, "score_model_hot", 50.0)
        return b.get("eviction", 0) < 0 or b.get("thermal", 0) >= hot

    def _idle_room_for(self, node, node_id: str, model: str, depths: dict[str, int]) -> bool:
        """Would evicting the idle models on ``node`` make room to load ``model``?

        Ollama evicts an idle model at once, so a load that fits in the free
        memory plus what the idle models hold never waits for a busy one.  One
        that does not fit even then still has to evict a busy model, and waits
        its turn like any other: a small idle model beside a busy large one
        used to disable the wait outright.  False with no idle model, where the
        routing verdict (``_evicting_load``) stands.  The cost is the one the
        scorer uses (``model_preloader.fit_cost_gb``): measured resident size
        when the fleet has seen the model, else its weights.
        """
        idle = [m for m in distinct_resident_models(node) if not depths.get(f"{node_id}:{m.name}")]
        if not idle:
            return False
        from fleet_manager.server.model_preloader import fit_cost_gb

        on_gpu = bool(node.memory and node.memory.gpu_total_gb > 0)
        freed = sum((m.size_vram_gb or m.size_gb) if on_gpu else m.size_gb for m in idle)
        cost = fit_cost_gb(model, node, self._settings)
        return node.memory_free_without_evicting_gb() + freed >= cost

    def _must_wait(self, q: DeviceModelQueue, entry: QueueEntry) -> bool:
        """Should ``entry`` stay in herd's queue rather than go to its node now?

        Ollama evicts a busy model by waiting, with no timeout, until its
        requests are done, and serves new requests for it meanwhile; while it
        waits it loads nothing else on that node.  So a load sent to a node
        whose models are all busy waits for as long as herd keeps sending them
        work: on the 2026-11-20 fleet benchmark (run 4) phi4 and qwen2.5:14b,
        steadily busy on the two 24 GB cards, kept ``qwen3-coder:30b`` off both
        until 25 of its 27 requests timed out.  Two rules bound that wait
        without swapping models on every request:

        * **A load waits its turn**: a request that must evict a busy model
          stays here until a model on its node goes idle (Ollama evicts an idle
          one at once), its model is loaded, or it has waited
          ``_LOAD_WAIT_SECONDS``.  Requests for the same model that arrive
          meanwhile join it, and go with it.
        * **Then it gets the room**: while that load is at the node and has
          produced nothing, new requests for the models it will evict stay
          here, so the ones already running are the last.  For at most
          ``_LOAD_ROOM_SECONDS`` from when the load reached the node: past it
          the load is more likely stuck, and holding a healthy resident model's
          traffic until the stale reaper took up to ten minutes.

        Nothing waits for a load that fits beside the resident models, or that
        evicting the node's idle models makes room for (``_idle_room_for``).
        """
        if self._registry is None:
            return False
        node = self._registry.get_node(q.node_id)
        if node is None or node.ollama is None:
            return False
        depths = self.get_queue_depths()
        if model_resident_on_node(q.model, node):
            now = time.time()
            return any(
                e.last_progress_at is None
                and self._evicting_load(e)
                and now - (e.started_at or now) < _LOAD_ROOM_SECONDS
                and not self._idle_room_for(node, q.node_id, other.model, depths)
                for other in self._queues.values()
                if other is not q
                and other.node_id == q.node_id
                and not model_resident_on_node(other.model, node)
                for e in other.in_flight.values()
            )
        if not self._evicting_load(entry):
            return False
        if self._idle_room_for(node, q.node_id, q.model, depths):
            return False
        if _load_under_way(q):
            return False  # this model's load is under way: join it
        return time.time() - entry.enqueued_at < _LOAD_WAIT_SECONDS

    def mark_completed(
        self,
        queue_key: str,
        entry: QueueEntry,
        *,
        latency_ms: float | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ):
        """Remove an entry from in-flight and mark completed.

        Optional ``latency_ms``/``prompt_tokens``/``completion_tokens`` feed
        the per-queue running averages surfaced on the dashboard.  Pass all
        three when available — partial updates are accepted (missing values
        treated as 0 for that sample) to keep the happy path simple for
        callers that don't always have token counts (e.g. image generation).
        """
        self._release_client(entry)
        if queue_key in self._queues:
            q = self._queues[queue_key]
            entry.status = RequestStatus.COMPLETED
            entry.completed_at = time.time()
            q.in_flight.pop(entry.request.request_id, None)
            self._release_slot(q, entry.request.request_id)
            q.last_finished_at = entry.completed_at
            q.completed_count += 1
            # Accumulate stats only when at least one value is provided,
            # otherwise the averages would drift to zero for completions
            # that never reported tokens (e.g. image generation).
            if latency_ms is not None or prompt_tokens is not None or completion_tokens is not None:
                q.total_latency_ms += latency_ms or 0.0
                q.total_prompt_tokens += prompt_tokens or 0
                q.total_completion_tokens += completion_tokens or 0
                q.stats_samples += 1
            logger.debug(f"Completed {entry.request.request_id[:8]} on {queue_key}")

    def _settle_on_exit(self, q: DeviceModelQueue, entry: QueueEntry, stream):
        """``stream``, guaranteed to take the request out of this queue
        however it ends: exhausted, failed, cancelled, closed, or dropped.
        See ``_SettledStream``."""
        return _SettledStream(self, q, entry, stream)

    def _settle_abandoned(self, q: DeviceModelQueue, entry: QueueEntry) -> None:
        """Mark a request failed if nothing settled it, and free its slot.

        Where the request is now: a retry moves it to another node's queue
        (``admit``).  Idempotent.
        """
        rid = entry.request.request_id
        key = f"{entry.assigned_node}:{entry.request.model}"
        now_in = self._queues.get(key)
        if now_in is not None and rid in now_in.in_flight:  # nothing settled it
            self.mark_failed(key, entry)
        self._release_slot(q, rid)

    @staticmethod
    def _release_slot(q: DeviceModelQueue, request_id: str) -> None:
        """Free the worker holding ``request_id`` — every exit path calls this."""
        event = q.slots.pop(request_id, None)
        if event is not None:
            event.set()

    def mark_failed(self, queue_key: str, entry: QueueEntry):
        """Remove an entry from in-flight and mark failed."""
        self._release_client(entry)
        if queue_key in self._queues:
            q = self._queues[queue_key]
            entry.status = RequestStatus.FAILED
            entry.completed_at = time.time()
            q.in_flight.pop(entry.request.request_id, None)
            self._release_slot(q, entry.request.request_id)
            q.last_finished_at = entry.completed_at
            q.failed_count += 1
            logger.warning(f"Failed {entry.request.request_id[:8]} on {queue_key}")

    async def move_pending(self, source_key: str, target_key: str, count: int) -> int:
        """Move up to `count` pending requests from source queue to target queue.
        Returns the number actually moved."""
        async with self._lock:
            if source_key not in self._queues:
                return 0

            source = self._queues[source_key]
            if target_key not in self._queues:
                # Parse node_id and model from queue key
                parts = target_key.split(":", 1)
                if len(parts) != 2:
                    return 0
                self._queues[target_key] = DeviceModelQueue(node_id=parts[0], model=parts[1])
            target = self._queues[target_key]

        moved = 0
        # Drain pending items from source
        while not source.pending.empty() and moved < count:
            try:
                item = source.pending.get_nowait()
                entry, future, process_fn = item
                # Update the entry's assigned node
                new_node = target.node_id
                entry.assigned_node = new_node
                await target.pending.put((entry, future, process_fn))
                moved += 1
            except asyncio.QueueEmpty:
                break

        # Ensure target workers are running
        if moved > 0:
            self._ensure_workers(target, target_key)

        return moved

    async def admit(self, entry: QueueEntry, target_key: str) -> bool:
        """Move in-flight ``entry`` to ``target_key``'s queue the way any request
        enters it: through ``pending``, taken by one of that queue's workers.
        Returns once a worker has taken it (False if it was not in flight).

        A retry sending a request to another node uses this.  The worker
        applies the load hold (``_must_wait``) and holds one of the target's
        slots until the request settles there (``mark_*``), so a retried
        request runs inside the new node's concurrency.  It used to be placed
        straight into ``in_flight``, which counted it in the depth and the pins
        but let a node run one more request per retry than it admits.

        The old queue lets go at once: its slot is freed and the request
        leaves its ``in_flight``, so the node that failed shows no phantom load.
        ``started_at`` restarts when the target worker takes it, because the
        room a load gets (``_LOAD_ROOM_SECONDS``) counts from its new node.
        """
        rid = entry.request.request_id
        source_key = f"{entry.assigned_node}:{entry.request.model}"
        async with self._lock:
            source = self._queues.get(source_key)
            if source is None or rid not in source.in_flight:
                return False
            parts = target_key.split(":", 1)
            if len(parts) != 2:
                return False
            target = self._queues.get(target_key)
            if target is None:
                target = self._queues[target_key] = DeviceModelQueue(
                    node_id=parts[0], model=parts[1]
                )
            source.in_flight.pop(rid)
            self._release_slot(source, rid)
            entry.assigned_node = target.node_id
            entry.status = RequestStatus.PENDING
            entry.last_progress_at = None
            future = asyncio.get_running_loop().create_future()
            await target.pending.put((entry, future, _admitted))
        self._ensure_workers(target, target_key)
        try:
            await future
        except asyncio.CancelledError:
            future.cancel()  # the worker skips it
            raise
        logger.debug(f"Admitted {rid[:8]} from {source_key} to {target_key}")
        return True

    async def shutdown(self):
        """Cancel all worker tasks and the reaper."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        for q in self._queues.values():
            for task in q.worker_tasks:
                if not task.done():
                    task.cancel()


def _admitted(entry: QueueEntry) -> None:
    """The process function of a request ``QueueManager.admit`` moved here: it
    is already running, so dispatching it starts nothing.  The worker hands
    back ``None`` and holds the slot until the request settles."""
    return None


class _SettledStream:
    """A process function's stream, passed through, that always takes its
    request out of the queue.

    The process functions settle requests themselves (``mark_completed`` /
    ``mark_failed``) on completion, errors and ``GeneratorExit``, but not on
    ``CancelledError``, which is how Starlette ends a streaming response when
    the client disconnects: mid-generation it lands while the stream awaits
    the backend, and the request never left ``in_flight`` (measured: a 641 s
    freeze until the reaper).  So this settles on every exit.

    A class, not an async generator, for the stream nobody ever starts: a
    client that leaves just as the worker hands the stream over gets it
    unconsumed, and ``aclose()`` on an async generator that never started runs
    no ``finally``.  Here ``aclose()`` settles either way
    (``routing.dispatched_stream`` calls it), so the slot is freed instead of
    waiting ten minutes for the reaper while the request counts as a pending
    load.  Settling is idempotent.
    """

    def __init__(self, manager: QueueManager, q: DeviceModelQueue, entry: QueueEntry, stream):
        self._manager = manager
        self._q = q
        self._entry = entry
        self._stream = stream
        self._it = None
        self._settled = False

    def _settle(self) -> None:
        if not self._settled:
            self._settled = True
            self._manager._settle_abandoned(self._q, self._entry)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._it is None:
            self._it = aiter(self._stream)
        try:
            return await anext(self._it)
        except BaseException:  # StopAsyncIteration, errors, cancellation
            self._settle()
            raise

    async def aclose(self) -> None:
        try:
            close = getattr(self._it if self._it is not None else self._stream, "aclose", None)
            if close is not None:
                await close()
        finally:
            self._settle()
