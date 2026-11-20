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
  hot-model cap: ``--max-loaded N`` evicts the least recently used model, and
  a cold model takes ``--load-ms`` to load (reported as ``load_duration``)
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
  ``models``            restrict the fault to these models; empty = all

    python scripts/mock_ollama.py --port 11500 --delay-ms 20
    python scripts/mock_ollama.py --model llama3.2:3b:2.0 --model nomic-embed-text:0.3 \\
        --chunks 20 --chunk-delay-ms 10 --max-loaded 1 --load-ms 2000
    curl -s localhost:11500/_mock/fault -d '{"die_after_chunks": 5, "models": ["llama3.2:3b"]}'
    FLEET_NODE_OLLAMA_HOST=http://127.0.0.1:11500 herd-node   # registers the models

One process tops out around 200 requests/s, below what the router it measures
can take; ``--workers`` runs several, so the stand-in is never the bottleneck.
With ``--workers`` the fault is kept in a temp file every worker reads, so a
runtime fault reaches all of them; loaded-model (LRU) state stays per worker,
so run load/eviction experiments with one worker.
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
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field, fields

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from fleet_manager.server.model_knowledge import ModelType, model_type

DEFAULT_MODEL = "mock:latest"
DEFAULT_SIZE_GB = 0.001  # the 1,000,000-byte model the router-overhead runs used

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
        self._mtime: int | None = None

    def get(self) -> Fault:
        if self._path:
            try:
                mtime = os.stat(self._path).st_mtime_ns
                if mtime != self._mtime:
                    with open(self._path) as f:
                        self._fault = Fault.from_dict(json.load(f))
                    self._mtime = mtime
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
    load_ms: float = 0.0
    context_length: int = 4096
    embed_dim: int = 768
    seed: int | None = None
    fault: dict = field(default_factory=dict)
    fault_file: str | None = None


class Backend:
    """Which models are resident, in least-recently-used order."""

    def __init__(self, models: list[MockModel], max_loaded: int, load_ms: float):
        self.models = {m.name: m for m in models}
        self.max_loaded = max_loaded
        self.load_ms = load_ms
        first = list(self.models)[: max_loaded or None]
        self.loaded: OrderedDict[str, None] = OrderedDict((name, None) for name in first)
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure_loaded(self, name: str) -> float:
        """Make ``name`` resident; returns the seconds spent loading it."""
        if name in self.loaded:
            self.loaded.move_to_end(name)
            return 0.0
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            if name in self.loaded:
                self.loaded.move_to_end(name)
                return 0.0
            start = time.perf_counter()
            await asyncio.sleep(self.load_ms / 1000)
            while self.max_loaded and len(self.loaded) >= self.max_loaded:
                self.loaded.popitem(last=False)
            self.loaded[name] = None
            return time.perf_counter() - start

    def unload(self, name: str) -> None:
        self.loaded.pop(name, None)


class _NdjsonStream(StreamingResponse):
    """An NDJSON stream that can end without completing, like a node that died.

    Returning without the final ``more_body: False`` makes uvicorn drop the
    connection, so the client reads an incomplete chunked body: the same
    ``RemoteProtocolError`` a crashed Ollama produces.
    """

    def __init__(self, parts: AsyncIterator[bytes], drop: bool):
        super().__init__(parts, media_type="application/x-ndjson")
        self.drop = drop

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


def estimate_prompt_tokens(body: dict) -> int:
    """Characters / 4, the estimate the router itself uses for context checks."""
    chars = len(_text_of(body.get("prompt"))) + len(_text_of(body.get("system")))
    for message in body.get("messages") or []:
        if isinstance(message, dict):
            chars += len(_text_of(message.get("content")))
    return max(1, chars // 4)


def _embedding(text: str, dim: int) -> list[float]:
    rng = random.Random(hashlib.sha256(text.encode()).digest())
    vec = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


async def _no_parts() -> AsyncIterator[bytes]:
    return
    yield


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def build_app(config: MockConfig | None = None) -> FastAPI:
    config = config or MockConfig()
    models = [MockModel.parse(spec) for spec in config.models]
    backend = Backend(models, config.max_loaded, config.load_ms)
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
        load_s = await backend.ensure_loaded(name)
        await asyncio.sleep(config.delay_ms / 1000)
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
        unload_after = str(body.get("keep_alive", "")) in ("0", "0s")

        if kind == "generate" and not body.get("prompt") and "messages" not in body:
            # Ollama's load-only request (the router's pre-warm): no output.
            if unload_after:
                backend.unload(name)
            else:
                await backend.ensure_loaded(name)
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
        prompt_tokens = estimate_prompt_tokens(body)
        if fault and fault.context_limit and prompt_tokens > fault.context_limit:
            message = context_too_long(prompt_tokens, fault.context_limit)
            if not stream:
                return _error(400, message)

            async def error_line():
                yield (json.dumps({"error": message}) + "\n").encode()

            # Mid-request failures arrive on a 200 stream as an error line.
            return _NdjsonStream(error_line(), drop=False)

        load_s = await backend.ensure_loaded(name)
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
                "prompt_eval_duration": int(config.delay_ms * 1e6),
                "eval_count": chunks,
                "eval_duration": int(max(0.0, total - load_s - config.delay_ms / 1000) * 1e9),
            }

        def finish() -> None:
            if unload_after:
                backend.unload(name)

        if not stream:
            if die_after >= 0:
                return _NdjsonStream(_no_parts(), drop=True)
            await asyncio.sleep((config.delay_ms + chunks * config.chunk_delay_ms) / 1000)
            finish()
            return {**done_line(), **piece("".join(texts))}

        async def lines():
            await asyncio.sleep(config.delay_ms / 1000)
            for i, text in enumerate(texts):
                if i == die_after:
                    return
                if i and config.chunk_delay_ms:
                    await asyncio.sleep(config.chunk_delay_ms / 1000)
                line = {"model": name, "created_at": _now(), **piece(text), "done": False}
                yield (json.dumps(line) + "\n").encode()
            if die_after >= chunks:
                return  # every chunk sent, the done line never arrives
            finish()
            yield (json.dumps(done_line()) + "\n").encode()

        return _NdjsonStream(lines(), drop=die_after >= 0)

    @app.post("/api/chat")
    async def chat(request: Request):
        return await infer(request, "chat")

    @app.post("/api/generate")
    async def generate(request: Request):
        return await infer(request, "generate")

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
    p.add_argument("--load-ms", type=float, default=0.0, help="Cold-load time of a model")
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
            "models": args.fault_models,
        }
    )
    config = MockConfig(
        models=args.models or [DEFAULT_MODEL],
        delay_ms=args.delay_ms,
        chunks=args.chunks,
        chunk_delay_ms=args.chunk_delay_ms,
        max_loaded=args.max_loaded,
        load_ms=args.load_ms,
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
