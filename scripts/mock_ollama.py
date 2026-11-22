#!/usr/bin/env python3
"""A stand-in Ollama for measuring and breaking the router without GPUs.

Against an upstream whose latency is known and constant, whatever a request
costs on top of it is the router's (how gateway vendors measure overhead).  It
speaks enough of Ollama's API for herd-node or ``scripts/fake_fleet.py`` to
register it, for the router to route to it, and for a test to make it fail the
way Ollama fails:

* ``/``, ``/api/version``, ``/api/tags``, ``/api/ps``, ``/api/show``
* ``/api/chat`` and ``/api/generate``, streamed as ``--chunks`` lines after
  ``--delay-ms`` (then ``--chunk-delay-ms`` apart) or as one JSON body;
  ``eval_count`` is the number of chunks
* ``/api/embed``, ``--embed-dim`` vectors (deterministic per input)
* several models (``--model name:size_gb``, repeatable), with an optional
  hot-model cap: ``--max-loaded N`` evicts the least recently used idle model,
  and a cold model takes ``--load-ms`` to load (reported as ``load_duration``).
  Loads are scheduled as Ollama schedules them (``Backend``): one at a time, a
  load waits while every resident model is serving, and a request's
  ``keep_alive`` re-arms a resident model an eviction is waiting for.
  ``--memory-gb G`` caps resident model sizes instead of (or as well as) the
  count: a load evicts until the models left plus the new one fit in G, with
  the same wait for a busy victim.  A model bigger than G alone still loads
  into an empty backend, as Ollama spills it partly to the CPU.
  ``GET /_mock/state`` adds the model being loaded, which ``/api/ps`` omits
* prompt processing: ``--prefill-ms-per-token`` charges each prompt token
  (chars / 4) not already in the model's prompt cache, before the first chunk.
  The cache keeps the last ``PROMPT_CACHE_SLOTS`` prompts per model, like
  llama-server's slots: a prompt sharing a prefix with one of them pays only
  for the rest, and an unload empties it.  The done line reports the full
  ``prompt_eval_count`` (cached tokens included, as Ollama does) and
  ``prompt_eval_duration`` (``--delay-ms`` plus the prefill time)
* faults, at startup by flag or at runtime with ``POST /_mock/fault`` (a JSON
  object that *replaces* the current fault; ``{}`` clears it) and read back
  with ``GET /_mock/fault``:

  ``error_rate``        0-1, share of inference requests answered with ``status``
  ``status``            404 (Ollama's model-not-found text), 500 (runner died),
                        503 (Ollama's queue-full text, which the router does not
                        retry), or any other code; ``message`` overrides the text
  ``latency_ms``        added before anything is answered (slow but alive)
  ``die_after_chunks``  close the connection after N content chunks (0 = before
                        the first); -1 is off
  ``context_limit``     prompt-token estimate (chars / 4) above which Ollama's
                        context-too-long error is returned; 0 is off
  ``stream_error``      error text a streamed request gets as the only line of a
                        200 stream (non-streamed: a 500 with that text), e.g.
                        ``llama runner process has terminated: exit status 2``,
                        which is not a client error; "" is off
  ``models``            restrict the fault to these models; empty = all

    python scripts/mock_ollama.py --port 11500 --delay-ms 20
    python scripts/mock_ollama.py --model llama3.2:3b:2.0 --model nomic-embed-text:0.3 \\
        --chunks 20 --chunk-delay-ms 10 --max-loaded 1 --load-ms 2000
    curl -s localhost:11500/_mock/fault -d '{"die_after_chunks": 5, "models": ["llama3.2:3b"]}'
    python scripts/mock_ollama.py --model a:7b:15 --model b:7b:12 --memory-gb 24 \\
        --load-ms 3000 --prefill-ms-per-token 0.5
    FLEET_NODE_OLLAMA_HOST=http://127.0.0.1:11500 herd-node   # registers the models

One process tops out around 200 requests/s, below what the router it measures
can take; ``--workers`` runs several, so the stand-in is never the bottleneck.
With ``--workers`` the fault is kept in a temp file every worker reads, so a
runtime fault reaches all of them; loaded-model (LRU) state and the prompt
cache stay per worker, so run load, eviction and prefix-cache experiments with
one worker.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import tempfile
import time
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field, fields

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from fleet_manager.server.model_knowledge import ModelType, model_type

DEFAULT_MODEL = "mock:latest"
DEFAULT_SIZE_GB = 0.001  # the 1,000,000-byte model the router-overhead runs used
# Prompts remembered per model for prefix reuse: llama-server keeps one per slot,
# and Ollama's default OLLAMA_NUM_PARALLEL on a large machine is 4.
PROMPT_CACHE_SLOTS = 4

# Ollama's own error texts: the router classifies failures by these
# (``streaming._is_queue_full_error``, ``_is_model_not_found_error``,
# ``common.errors.categorize_error``), so a mock that paraphrased them would test
# a different router.
QUEUE_FULL = "server busy, please try again.  maximum pending requests exceeded"
RUNNER_DIED = "llama runner process has terminated: exit status 2"


def model_not_found(name: str) -> str:
    return f"model '{name}' not found"


def context_too_long(tokens: int, limit: int) -> str:
    return (
        f"input length ({tokens} tokens) exceeds the model's maximum context length "
        f"({limit} tokens)"
    )


def canonical(name: str) -> str:
    """Ollama's name for ``name``: an untagged name means ``:latest``."""
    return name if ":" in name else f"{name}:latest"


@dataclass(frozen=True)
class MockModel:
    name: str
    size_gb: float

    @classmethod
    def parse(cls, spec: str) -> MockModel:
        """``name:size_gb`` (``llama3.2:3b:2.0``), or a bare name at the default size."""
        name, sep, size = spec.rpartition(":")
        if sep:
            try:
                return cls(canonical(name), float(size))
            except ValueError:
                pass
        return cls(canonical(spec), DEFAULT_SIZE_GB)

    @property
    def kind(self) -> ModelType:
        return model_type(self.name)

    @property
    def capabilities(self) -> list[str]:
        if self.kind == ModelType.EMBED:
            return ["embedding"]
        if self.kind == ModelType.VISION:
            return ["completion", "vision", "tools"]
        return ["completion", "tools"]

    @property
    def family(self) -> str:
        match = re.match(r"[a-z]+", self.name.lower())
        return match.group(0) if match else "mock"

    @property
    def size_bytes(self) -> int:
        return int(self.size_gb * 1e9)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.name.encode()).hexdigest()

    @property
    def details(self) -> dict:
        tag = self.name.partition(":")[2]
        params = tag.upper() if re.fullmatch(r"\d+(\.\d+)?[bm]", tag) else ""
        return {
            "parent_model": "",
            "format": "gguf",
            "family": self.family,
            "families": [self.family],
            "parameter_size": params,
            "quantization_level": "Q4_K_M",
        }


@dataclass
class Fault:
    error_rate: float = 0.0
    status: int = 500
    message: str = ""
    latency_ms: float = 0.0
    die_after_chunks: int = -1
    context_limit: int = 0
    stream_error: str = ""
    models: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> Fault:
        """Validate a fault from JSON; raises ``ValueError`` naming the problem."""
        if not isinstance(data, dict):
            raise ValueError("fault must be a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown fault field(s) {unknown}; known: {sorted(known)}")
        fault = cls(**data)
        fault.models = [canonical(m) for m in fault.models]
        if not 0.0 <= fault.error_rate <= 1.0:
            raise ValueError("error_rate must be between 0 and 1")
        if not 400 <= fault.status <= 599:
            raise ValueError("status must be an HTTP error status (400-599)")
        return fault

    def applies_to(self, model: str) -> bool:
        return not self.models or model in self.models

    def error_text(self, model: str) -> str:
        if self.message:
            return self.message
        if self.status == 404:
            return model_not_found(model)
        if self.status == 503:
            return QUEUE_FULL
        if self.status == 500:
            return RUNNER_DIED
        return f"mock fault: HTTP {self.status}"


class FaultStore:
    """The current fault.  With ``path``, read from a file every worker shares."""

    def __init__(self, fault: Fault, path: str | None = None):
        self._fault = fault
        self._path = path
        self._version: tuple[int, int, int] | None = None

    def get(self) -> Fault:
        if self._path:
            try:
                # Inode too, not only the mtime: two writes inside one timestamp
                # tick share an mtime, but ``write_fault_file`` replaces the file,
                # so each write is a new inode.
                st = os.stat(self._path)
                version = (st.st_ino, st.st_mtime_ns, st.st_size)
                if version != self._version:
                    with open(self._path) as f:
                        self._fault = Fault.from_dict(json.load(f))
                    self._version = version
            except (OSError, ValueError):
                pass  # keep the last fault that parsed
        return self._fault

    def set(self, fault: Fault) -> None:
        self._fault = fault
        if self._path:
            write_fault_file(self._path, fault)


def write_fault_file(path: str, fault: Fault) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(fault), f)
    os.replace(tmp, path)


@dataclass
class MockConfig:
    models: list[str] = field(default_factory=lambda: [DEFAULT_MODEL])
    delay_ms: float = 20.0
    chunks: int = 1
    chunk_delay_ms: float = 0.0
    max_loaded: int = 0
    memory_gb: float = 0.0
    load_ms: float = 0.0
    prefill_ms_per_token: float = 0.0
    context_length: int = 4096
    embed_dim: int = 768
    seed: int | None = None
    fault: dict = field(default_factory=dict)
    fault_file: str | None = None


def keep_alive_is_zero(value) -> bool:
    """``keep_alive: 0`` (or ``"0"``, ``"0s"``, ``"0m"``): unload once idle."""
    return value is not None and str(value).strip() in ("0", "0s", "0m", "0h", "0.0")


class Backend:
    """Which models are resident, in least-recently-used order, and which are busy,
    scheduled the way Ollama's ``server/sched.go`` schedules them:

    * A request for a resident model is served at once (``getRunner``'s fast
      path), and a ``keep_alive`` it carries becomes the runner's
      (``useLoadedRunner``).  So any ``keep_alive`` but 0 cancels an eviction
      under way.
    * Every other request waits its turn in one queue (``processPending``) that
      loads one model at a time.  A load that needs a slot evicts the least
      recently used idle model; with every model busy it marks the least
      recently used one to unload when its requests end (``sessionDuration =
      0``) and waits for that, with no timeout, while the queue behind it waits
      too.  A client that leaves does not end the wait.

    "Needs a slot" is the count cap (``max_loaded``) or the memory budget
    (``memory_gb``, the sizes of the resident models plus the new one), or
    both; 0 turns either off, and with both off nothing is ever evicted for a
    load.  Eviction stops once nothing is left, so a model larger than the
    whole budget still loads into an empty backend.

    ``/api/ps`` lists a model until it is really gone, and an unload empties
    the model's prompt cache (``prompts``).
    """

    def __init__(
        self,
        models: list[MockModel],
        max_loaded: int,
        load_ms: float,
        memory_gb: float = 0.0,
    ):
        self.models = {m.name: m for m in models}
        self.max_loaded = max_loaded
        self.memory_gb = memory_gb
        self.load_ms = load_ms
        self.loaded: OrderedDict[str, None] = OrderedDict()
        for name in self.models:  # the first models that fit start resident
            if self._needs_room(name):
                break
            self.loaded[name] = None
        self.prompts: dict[str, list[str]] = {}  # most recent last
        self.busy: Counter[str] = Counter()
        self.expiring: set[str] = set()  # sessionDuration 0: unloads when idle
        self.loading: str | None = None  # in memory, not yet in /api/ps
        self._pending: asyncio.Lock | None = None
        self._waiters: list[asyncio.Future] = []

    async def acquire(self, name: str, keep_alive=None) -> float:
        """Make ``name`` resident and count one request on it; returns the
        seconds spent waiting for and doing the load.  Every ``acquire`` needs a
        ``release``."""
        if name in self.loaded:
            self._use(name, keep_alive)
            return 0.0
        self._pending = self._pending or asyncio.Lock()
        task = asyncio.ensure_future(self._schedule(name, keep_alive))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The client left; the scheduler finishes this request's turn anyway.
            task.add_done_callback(
                lambda t: t.cancelled() or t.exception() is not None or self.release(name)
            )
            raise

    async def _schedule(self, name: str, keep_alive) -> float:
        start = time.perf_counter()
        async with self._pending:
            while name not in self.loaded and self.loaded and self._needs_room(name):
                idle = next((n for n in self.loaded if not self.busy[n]), None)
                victim = idle or next(iter(self.loaded))
                self.expiring.add(victim)
                self._unload_if_idle(victim)
                while victim in self.loaded:
                    waiter = asyncio.get_running_loop().create_future()
                    self._waiters.append(waiter)
                    await waiter
            if name not in self.loaded:
                self.loading = name
                try:
                    await asyncio.sleep(self.load_ms / 1000)
                finally:
                    self.loading = None
                self.loaded[name] = None
                self.expiring.discard(name)
            self._use(name, keep_alive)
        return time.perf_counter() - start

    def _needs_room(self, name: str) -> bool:
        """Loading ``name`` now would exceed the count cap or the memory budget."""
        if self.max_loaded and len(self.loaded) >= self.max_loaded:
            return True
        if self.memory_gb:
            resident = sum(self.models[n].size_gb for n in self.loaded)
            return resident + self.models[name].size_gb > self.memory_gb + 1e-9
        return False

    def cached_tokens(self, name: str, prompt: str) -> int:
        """Tokens of ``prompt`` already in ``name``'s prompt cache: the longest
        prefix it shares with a remembered prompt, in chars / 4."""
        cache = self.prompts.get(name, [])
        shared = max((len(os.path.commonprefix([prompt, p])) for p in cache), default=0)
        return shared // 4

    def remember_prompt(self, name: str, prompt: str) -> None:
        if name not in self.loaded:
            return  # unloaded meanwhile: its cache is gone
        cache = self.prompts.setdefault(name, [])
        if prompt in cache:
            cache.remove(prompt)
        cache.append(prompt)
        del cache[:-PROMPT_CACHE_SLOTS]

    def _use(self, name: str, keep_alive) -> None:
        self.loaded.move_to_end(name)
        self.busy[name] += 1
        if keep_alive is not None:
            if keep_alive_is_zero(keep_alive):
                self.expiring.add(name)
            else:
                self.expiring.discard(name)

    def release(self, name: str) -> None:
        self.busy[name] -= 1
        self._unload_if_idle(name)

    def unload(self, name: str) -> None:
        """``keep_alive: 0`` on a load-only request: unload now, or when its
        last request ends."""
        self.expiring.add(name)
        self._unload_if_idle(name)

    def _unload_if_idle(self, name: str) -> None:
        if name in self.expiring and not self.busy[name]:
            self.expiring.discard(name)
            self.loaded.pop(name, None)
            self.prompts.pop(name, None)
            waiters, self._waiters = self._waiters, []
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)


class _NdjsonStream(StreamingResponse):
    """An NDJSON stream that can end without completing, like a node that died.

    Returning without the final ``more_body: False`` makes uvicorn drop the
    connection, so the client reads an incomplete chunked body: the same
    ``RemoteProtocolError`` a crashed Ollama produces.
    """

    def __init__(
        self, parts: AsyncIterator[bytes], drop: bool, on_close: Callable[[], None] | None = None
    ):
        super().__init__(parts, media_type="application/x-ndjson")
        self.drop = drop
        self.on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        # Here, not in the body iterator: a client gone before the first chunk
        # never starts the iterator, and its finally would never run.
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self.on_close:
                self.on_close()

    async def stream_response(self, send) -> None:
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        async for part in self.body_iterator:
            await send({"type": "http.response.body", "body": part, "more_body": True})
        if not self.drop:
            await send({"type": "http.response.body", "body": b"", "more_body": False})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def prompt_text(body: dict) -> str:
    """The prompt as one string, in order: what prefix caching compares."""
    parts = [_text_of(body.get("system")), _text_of(body.get("prompt"))]
    for message in body.get("messages") or []:
        if isinstance(message, dict):
            parts.append(_text_of(message.get("content")))
    return "".join(parts)


def estimate_prompt_tokens(body: dict) -> int:
    """Characters / 4, the estimate the router itself uses for context checks."""
    return max(1, len(prompt_text(body)) // 4)


def _embedding(text: str, dim: int) -> list[float]:
    rng = random.Random(hashlib.sha256(text.encode()).digest())
    vec = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _stream_error(stream: bool, status: int, message: str):
    """A failure Ollama reports mid-request: on a 200 stream, as the stream's only
    line (``{"error": ...}``); unstreamed, as ``status`` with the text."""
    if not stream:
        return _error(status, message)

    async def error_line():
        yield (json.dumps({"error": message}) + "\n").encode()

    return _NdjsonStream(error_line(), drop=False)


async def _no_parts() -> AsyncIterator[bytes]:
    return
    yield


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def build_app(config: MockConfig | None = None) -> FastAPI:
    config = config or MockConfig()
    models = [MockModel.parse(spec) for spec in config.models]
    backend = Backend(models, config.max_loaded, config.load_ms, config.memory_gb)
    faults = FaultStore(Fault.from_dict(config.fault), config.fault_file)
    rng = random.Random(config.seed)
    app = FastAPI()
    app.state.backend = backend
    app.state.faults = faults

    def tag(m: MockModel) -> dict:
        return {
            "name": m.name,
            "model": m.name,
            "modified_at": _now(),
            "size": m.size_bytes,
            "digest": m.digest,
            "details": m.details,
            "capabilities": m.capabilities,
        }

    async def admit(model: str) -> tuple[Fault | None, JSONResponse | None]:
        """Apply the fault's latency and error; returns (active fault, error response)."""
        fault = faults.get()
        if not fault.applies_to(model):
            return None, None
        if fault.latency_ms:
            await asyncio.sleep(fault.latency_ms / 1000)
        if fault.error_rate and rng.random() < fault.error_rate:
            return fault, _error(fault.status, fault.error_text(model))
        return fault, None

    @app.get("/")
    async def root():
        return PlainTextResponse("Ollama is running")

    @app.get("/api/version")
    async def version():
        return {"version": "0.0.0-mock"}

    @app.get("/api/tags")
    async def tags():
        return {"models": [tag(m) for m in backend.models.values()]}

    @app.get("/api/ps")
    async def ps():
        resident = [backend.models[name] for name in reversed(backend.loaded)]
        return {
            "models": [
                {
                    **tag(m),
                    "expires_at": "2318-01-01T00:00:00Z",
                    "size_vram": m.size_bytes,
                    "context_length": config.context_length,
                }
                for m in resident
            ]
        }

    @app.post("/api/show")
    async def show(request: Request):
        body = await request.json()
        requested = body.get("model") or body.get("name") or ""
        m = backend.models.get(canonical(requested))
        if m is None:
            return _error(404, model_not_found(requested))
        return {
            "license": "",
            "modelfile": f"FROM {m.name}",
            "parameters": "",
            "template": "{{ .Prompt }}",
            "details": m.details,
            "model_info": {
                "general.architecture": m.family,
                f"{m.family}.context_length": config.context_length,
                f"{m.family}.embedding_length": config.embed_dim,
            },
            "capabilities": m.capabilities,
            "modified_at": _now(),
        }

    @app.post("/api/embed")
    async def embed(request: Request):
        body = await request.json()
        requested = body.get("model", "")
        name = canonical(requested)
        if name not in backend.models:
            return _error(404, model_not_found(requested))
        start = time.perf_counter()
        fault, failure = await admit(name)
        if failure:
            return failure
        inputs = body.get("input", "")
        inputs = [inputs] if isinstance(inputs, str) else list(inputs)
        tokens = [max(1, len(str(text)) // 4) for text in inputs]
        if fault and fault.context_limit and max(tokens, default=0) > fault.context_limit:
            return _error(400, context_too_long(max(tokens), fault.context_limit))
        load_s = await backend.acquire(name, body.get("keep_alive"))
        try:
            await asyncio.sleep(config.delay_ms / 1000)
        finally:
            backend.release(name)
        return {
            "model": name,
            "embeddings": [_embedding(str(text), config.embed_dim) for text in inputs],
            "total_duration": int((time.perf_counter() - start) * 1e9),
            "load_duration": int(load_s * 1e9),
            "prompt_eval_count": sum(tokens),
        }

    async def infer(request: Request, kind: str):
        body = await request.json()
        requested = body.get("model", "")
        name = canonical(requested)
        if name not in backend.models:
            return _error(404, model_not_found(requested))
        start = time.perf_counter()
        stream = body.get("stream", True) is not False
        keep_alive = body.get("keep_alive")
        unload_after = keep_alive_is_zero(keep_alive)

        if kind == "generate" and not body.get("prompt") and "messages" not in body:
            # Ollama's load-only request (the router's pre-warm): no output.
            if unload_after:
                backend.unload(name)
            else:
                await backend.acquire(name, keep_alive)
                backend.release(name)
            return {
                "model": name,
                "created_at": _now(),
                "response": "",
                "done": True,
                "done_reason": "unload" if unload_after else "load",
            }

        fault, failure = await admit(name)
        if failure:
            return failure
        prompt = prompt_text(body)
        prompt_tokens = estimate_prompt_tokens(body)
        if fault and fault.context_limit and prompt_tokens > fault.context_limit:
            return _stream_error(stream, 400, context_too_long(prompt_tokens, fault.context_limit))
        if fault and fault.stream_error:
            return _stream_error(stream, 500, fault.stream_error)

        load_s = await backend.acquire(name, keep_alive)
        uncached = max(1, prompt_tokens - backend.cached_tokens(name, prompt))
        first_ms = config.delay_ms + uncached * config.prefill_ms_per_token  # before chunk 1
        die_after = fault.die_after_chunks if fault else -1
        chunks = max(1, config.chunks)
        texts = ["ok" if i == 0 else " ok" for i in range(chunks)]

        def piece(text: str) -> dict:
            if kind == "chat":
                return {"message": {"role": "assistant", "content": text}}
            return {"response": text}

        def done_line() -> dict:
            total = time.perf_counter() - start
            return {
                "model": name,
                "created_at": _now(),
                **piece(""),
                "done": True,
                "done_reason": "stop",
                "total_duration": int(total * 1e9),
                "load_duration": int(load_s * 1e9),
                "prompt_eval_count": prompt_tokens,
                "prompt_eval_duration": int(first_ms * 1e6),
                "eval_count": chunks,
                "eval_duration": int(max(0.0, total - load_s - first_ms / 1000) * 1e9),
            }

        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                backend.release(name)

        if not stream:
            if die_after >= 0:
                return _NdjsonStream(_no_parts(), drop=True, on_close=release)
            try:
                await asyncio.sleep(first_ms / 1000)
                backend.remember_prompt(name, prompt)
                await asyncio.sleep(chunks * config.chunk_delay_ms / 1000)
            finally:
                release()
            return {**done_line(), **piece("".join(texts))}

        async def lines():
            await asyncio.sleep(first_ms / 1000)
            backend.remember_prompt(name, prompt)
            for i, text in enumerate(texts):
                if i == die_after:
                    return
                if i and config.chunk_delay_ms:
                    await asyncio.sleep(config.chunk_delay_ms / 1000)
                line = {"model": name, "created_at": _now(), **piece(text), "done": False}
                yield (json.dumps(line) + "\n").encode()
            if die_after >= chunks:
                return  # every chunk sent, the done line never arrives
            yield (json.dumps(done_line()) + "\n").encode()

        return _NdjsonStream(lines(), drop=die_after >= 0, on_close=release)

    @app.post("/api/chat")
    async def chat(request: Request):
        return await infer(request, "chat")

    @app.post("/api/generate")
    async def generate(request: Request):
        return await infer(request, "generate")

    @app.get("/_mock/state")
    async def state():
        """What the GPU holds, for a fake node standing in for nvidia-smi: the
        resident models and the one loading, which ``/api/ps`` does not list."""
        return {
            "loaded": list(backend.loaded),
            "loading": backend.loading,
            "busy": {n: c for n, c in backend.busy.items() if c},
            "expiring": sorted(backend.expiring),
        }

    @app.get("/_mock/fault")
    async def get_fault():
        return asdict(faults.get())

    @app.post("/_mock/fault")
    async def set_fault(request: Request):
        try:
            fault = Fault.from_dict(await request.json())
        except (ValueError, TypeError) as e:
            return _error(422, str(e))
        faults.set(fault)
        return asdict(fault)

    return app


def app_from_env() -> FastAPI:
    """For ``--workers``: each worker process builds the app from the environment."""
    return build_app(MockConfig(**json.loads(os.environ["MOCK_OLLAMA_CONFIG"])))


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=11500)
    p.add_argument("--workers", type=int, default=1, help="Server processes (default 1)")
    p.add_argument(
        "--model",
        action="append",
        dest="models",
        metavar="NAME:SIZE_GB",
        help=f"A model to serve; repeatable (default {DEFAULT_MODEL})",
    )
    p.add_argument("--delay-ms", type=float, default=20.0, help="Before the first chunk")
    p.add_argument("--chunks", type=int, default=1, help="Content chunks per response")
    p.add_argument("--chunk-delay-ms", type=float, default=0.0, help="Between chunks")
    p.add_argument("--max-loaded", type=int, default=0, help="Hot-model cap, LRU (0 = none)")
    p.add_argument(
        "--memory-gb", type=float, default=0.0, help="Budget for resident model sizes (0 = none)"
    )
    p.add_argument("--load-ms", type=float, default=0.0, help="Cold-load time of a model")
    p.add_argument(
        "--prefill-ms-per-token",
        type=float,
        default=0.0,
        help="Prompt processing per uncached token (0 = free)",
    )
    p.add_argument("--context-length", type=int, default=4096, help="Reported context")
    p.add_argument("--embed-dim", type=int, default=768)
    p.add_argument("--seed", type=int, default=None, help="Seed for error_rate draws")
    f = p.add_argument_group("faults (also settable at runtime: POST /_mock/fault)")
    f.add_argument("--error-rate", type=float, default=0.0)
    f.add_argument("--error-status", type=int, default=500)
    f.add_argument("--error-message", default="")
    f.add_argument("--fault-latency-ms", type=float, default=0.0)
    f.add_argument("--die-after-chunks", type=int, default=-1)
    f.add_argument("--context-limit", type=int, default=0)
    f.add_argument("--stream-error", default="", help="Error text sent as a 200 stream's only line")
    f.add_argument("--fault-model", action="append", default=[], dest="fault_models")
    args = p.parse_args()

    fault = Fault.from_dict(
        {
            "error_rate": args.error_rate,
            "status": args.error_status,
            "message": args.error_message,
            "latency_ms": args.fault_latency_ms,
            "die_after_chunks": args.die_after_chunks,
            "context_limit": args.context_limit,
            "stream_error": args.stream_error,
            "models": args.fault_models,
        }
    )
    config = MockConfig(
        models=args.models or [DEFAULT_MODEL],
        delay_ms=args.delay_ms,
        chunks=args.chunks,
        chunk_delay_ms=args.chunk_delay_ms,
        max_loaded=args.max_loaded,
        memory_gb=args.memory_gb,
        load_ms=args.load_ms,
        prefill_ms_per_token=args.prefill_ms_per_token,
        context_length=args.context_length,
        embed_dim=args.embed_dim,
        seed=args.seed,
        fault=asdict(fault),
    )
    if args.workers <= 1:
        uvicorn.run(build_app(config), host=args.host, port=args.port, log_level="warning")
        return
    fd, config.fault_file = tempfile.mkstemp(prefix="mock-ollama-fault-", suffix=".json")
    os.close(fd)
    write_fault_file(config.fault_file, fault)
    os.environ["MOCK_OLLAMA_CONFIG"] = json.dumps(asdict(config))
    try:
        uvicorn.run(
            "mock_ollama:app_from_env",
            factory=True,
            app_dir=os.path.dirname(os.path.abspath(__file__)),
            workers=args.workers,
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    finally:
        os.unlink(config.fault_file)


if __name__ == "__main__":
    main()
