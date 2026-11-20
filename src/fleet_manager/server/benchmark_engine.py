"""
Core benchmark engine — shared by CLI (scripts/benchmark.py) and server-side runner.

Provides fleet discovery, concurrent request generation, and report building.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import statistics
import time
from dataclasses import dataclass, field

import httpx

from fleet_manager.common.stats import percentile

logger = logging.getLogger(__name__)

# KV cache estimate per concurrent request (same as queue manager)
KV_CACHE_PER_REQUEST_GB = 2.0
MIN_CONCURRENCY = 1
MAX_CONCURRENCY_PER_MODEL = 6
MAX_TOTAL_PER_NODE = 8  # Ollama can't efficiently handle more

# Short prompts that produce quick responses — maximizes request throughput
PROMPTS = [
    "In exactly one sentence, what is distributed computing?",
    "Name three benefits of load balancing in one sentence.",
    "What is a GPU cluster? One sentence only.",
    "Define inference routing in one sentence.",
    "What is model sharding? One sentence.",
    "Explain KV cache in one sentence.",
    "What is tensor parallelism? One sentence.",
    "Define throughput in computing. One sentence.",
    "What is latency? One sentence answer.",
    "Explain horizontal scaling in one sentence.",
]


# Words for padding prompts to a target length.  Common English words are
# about one token each in most tokenizers, so a count of words is close to a
# count of tokens; make_prompt() measures with tiktoken to be sure.
_FILLER_TEXT = (
    "the router sends each request to a machine that has the model ready and memory to "
    "spare while the queue on every node is watched so that no single box falls behind "
    "a coding agent reads files runs tests and asks for changes many times an hour and "
    "each turn carries the history of the session along with the tools it may call "
    "embeddings turn documents into vectors so that search can find the right passage "
    "small models answer quick questions while large ones handle the hard reasoning work"
)
_FILLER_WORDS = _FILLER_TEXT.split()

EMBED_PATTERNS = ("embed", "nomic", "bge", "e5-")


def is_embed_model(name: str) -> bool:
    """True for embedding models, judged by name (how discover_fleet labels them)."""
    return any(p in name.lower() for p in EMBED_PATTERNS)


# Topics for the padded workload.  PROMPTS ask for one sentence, which a model
# obeys over any instruction to write more, so they cannot drive long replies.
LONG_TOPICS = [
    "how a load balancer decides where to send each request",
    "what limits token generation speed on a GPU",
    "how a KV cache works and what it costs in memory",
    "trade-offs of quantizing a language model",
    "how service discovery works on a local network",
    "ways to make a small cluster of machines fault tolerant",
    "how tensor parallelism and pipeline parallelism differ",
    "what to monitor on a machine serving language models",
]


def make_prompt(prompt_tokens: int | None = None, rng: random.Random | None = None) -> str:
    """A chat prompt: one of PROMPTS, or about ``prompt_tokens`` long.

    The short PROMPTS are ~10 tokens, while real requests average ~1,245 prompt
    tokens (ollamaherd.com/stats, 30 days to 2026-11-20), so a benchmark with
    them measures almost no prefill.  With ``prompt_tokens`` the prompt is
    padded with words in a random order, different on every call, so no two
    requests share a prefix Ollama's cache could skip.  It asks for a long answer
    so that ``max_tokens`` (not the model's brevity) decides the reply length:
    asked for "about 150 words" or "at least 400 words", qwen2.5-coder:7b still
    stopped near 40-60 tokens; a list of 40 points on an open topic runs to the cap.
    """
    rng = rng or random
    if not prompt_tokens:
        return rng.choice(PROMPTS)
    topic = rng.choice(LONG_TOPICS)
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    tail = (
        f"\n\nUsing the notes above where relevant, explain {topic} as a numbered list "
        f"of at least 40 points, each a full sentence."
    )
    budget = max(0, prompt_tokens - len(enc.encode(tail)))
    words: list[str] = []
    while len(enc.encode(" ".join(words))) < budget:
        words.extend(rng.sample(_FILLER_WORDS, k=min(40, len(_FILLER_WORDS))))
    text = " ".join(words)
    tokens = enc.encode(text)[:budget]
    return "Notes: " + enc.decode(tokens) + tail


EMBED_PROMPTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Distributed computing enables horizontal scaling across machines.",
    "Local inference preserves data privacy without cloud dependencies.",
    "GPU memory bandwidth determines token generation throughput.",
    "Model quantization trades precision for lower memory requirements.",
]

IMAGE_PROMPTS = [
    "a serene mountain landscape at sunset, photorealistic",
    "a futuristic robot reading a book in a library",
    "a cat wearing a tiny astronaut helmet floating in space",
    "an abstract painting of neural network connections",
    "a cozy coffee shop interior with warm lighting",
]


@dataclass
class RequestResult:
    model: str
    node_id: str
    latency_ms: float
    ttft_ms: float
    prompt_tokens: int
    completion_tokens: int
    success: bool
    error: str | None = None
    model_type: str = "llm"  # llm, embed, image, stt
    served_model: str = ""  # X-Fleet-Served-Model: what actually answered


@dataclass
class ModelTarget:
    name: str
    size_gb: float
    concurrency: int
    nodes: list[str] = field(default_factory=list)
    model_type: str = "llm"  # llm, embed, image, stt


@dataclass
class NodeInfo:
    node_id: str
    cores: int
    memory_total_gb: float
    memory_used_gb: float
    memory_available_gb: float
    models: list[str] = field(default_factory=list)
    model_sizes: dict[str, float] = field(default_factory=dict)
    ollama_url: str = ""  # the node's Ollama, for warming it directly
    model_contexts: dict[str, int] = field(default_factory=dict)  # resident num_ctx


async def discover_fleet(
    client: httpx.AsyncClient,
    include_on_disk: tuple[str, ...] | list[str] = (),
) -> tuple[list[ModelTarget], list[NodeInfo]]:
    """Discover nodes and the models to benchmark, with concurrency per model.

    Targets are the models loaded on any online node, labelled ``embed`` or
    ``llm`` by name, plus any model in ``include_on_disk`` that is not loaded
    but is on some node's disk (its first request then includes the load).
    Concurrency is calculated per-node:
    1. Total node budget = min(available_gb / KV_CACHE, MAX_TOTAL_PER_NODE)
    2. Budget is split evenly across models on that node
    3. Each model's share is capped at MAX_CONCURRENCY_PER_MODEL
    """
    resp = await client.get("/fleet/status")
    resp.raise_for_status()
    data = resp.json()

    nodes_info: list[NodeInfo] = []
    model_map: dict[str, ModelTarget] = {}

    def target(name: str, size_gb: float, concurrency: int) -> ModelTarget:
        if name not in model_map:
            model_map[name] = ModelTarget(
                name=name,
                size_gb=size_gb,
                concurrency=concurrency,
                model_type="embed" if is_embed_model(name) else "llm",
            )
        else:
            model_map[name].concurrency = max(model_map[name].concurrency, concurrency)
        return model_map[name]

    for node in data.get("nodes", []):
        if node.get("status") != "online":
            continue
        node_id = node["node_id"]
        ollama = node.get("ollama") or {}
        mem = node.get("memory", {})
        cpu = node.get("cpu", {})

        loaded = ollama.get("models_loaded", [])
        # What a model can load into: RAM plus GPU memory the OS doesn't count.
        available_gb = node.get("model_memory_available_gb", mem.get("available_gb", 0))

        nodes_info.append(
            NodeInfo(
                node_id=node_id,
                cores=cpu.get("cores_physical", 0),
                memory_total_gb=mem.get("total_gb", 0),
                memory_used_gb=mem.get("used_gb", 0),
                memory_available_gb=available_gb,
                models=[m["name"] for m in loaded],
                model_sizes={m["name"]: m.get("size_gb", 0) for m in loaded},
                ollama_url=node.get("ollama_url", ""),
                model_contexts={
                    m["name"]: m["context_length"] for m in loaded if m.get("context_length")
                },
            )
        )

        if loaded:
            # Available memory is already free (models are loaded and accounted for).
            mem_budget = int(available_gb / KV_CACHE_PER_REQUEST_GB)
            node_budget = max(MIN_CONCURRENCY, min(mem_budget, MAX_TOTAL_PER_NODE))
            base_per_model = max(1, node_budget // len(loaded))
            per_model_conc = min(base_per_model, MAX_CONCURRENCY_PER_MODEL)
            for m in loaded:
                target(m["name"], m.get("size_gb", 0), per_model_conc).nodes.append(node_id)

        loaded_names = {m["name"] for m in loaded}
        on_disk = set(ollama.get("models_available", []))
        sizes = ollama.get("models_available_sizes", {}) or {}
        for name in include_on_disk:
            if name in on_disk and name not in loaded_names:
                target(name, sizes.get(name, 0.0), MIN_CONCURRENCY).nodes.append(node_id)

    return list(model_map.values()), nodes_info


async def discover_image_models(
    client: httpx.AsyncClient,
) -> list[ModelTarget]:
    """Find image generation models available on the fleet."""
    try:
        resp = await client.get("/api/image-models")
        if resp.status_code != 200:
            return []
        data = resp.json()
        targets = []
        for m in data.get("models", []):
            name = m["name"]
            # Only benchmark one image model to avoid long runs
            if not targets:
                targets.append(
                    ModelTarget(
                        name=name,
                        size_gb=0,
                        concurrency=1,  # Image gen is slow, 1 at a time
                        nodes=m.get("fleet_nodes", []),
                        model_type="image",
                    )
                )
        return targets
    except Exception:
        return []


# Every benchmark request asks for exactly the model it names: a router that
# substitutes another model would otherwise be measured as that model.
EXACT_MODEL_HEADERS = {"X-Fleet-No-Fallback": "true"}


def served_mismatch(model: str, resp: httpx.Response) -> str | None:
    """Why ``resp`` is not an answer from ``model``, or None if it is."""
    served = resp.headers.get("x-fleet-served-model", "")
    if not served or served == model or served == f"{model}:latest":
        return None
    return f"served by {served}, not {model}"


async def send_embed_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 60.0,
) -> RequestResult:
    """Send an embedding request and measure latency."""
    body = {
        "model": model,
        "input": random.choice(EMBED_PROMPTS),
        "metadata": {"tags": ["benchmark"]},
    }
    start = time.monotonic()
    try:
        resp = await client.post(
            "/api/embed", json=body, timeout=timeout, headers=EXACT_MODEL_HEADERS
        )
        latency_ms = (time.monotonic() - start) * 1000
        mismatch = served_mismatch(model, resp)
        if resp.status_code != 200 or mismatch:
            return RequestResult(
                model=model,
                node_id="unknown",
                latency_ms=latency_ms,
                ttft_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                success=False,
                error=mismatch or f"HTTP {resp.status_code}",
                model_type="embed",
                served_model=resp.headers.get("x-fleet-served-model", ""),
            )
        node_id = resp.headers.get("x-fleet-node", "unknown")
        return RequestResult(
            model=model,
            node_id=node_id,
            latency_ms=latency_ms,
            ttft_ms=latency_ms,
            prompt_tokens=0,
            completion_tokens=1,
            success=True,
            model_type="embed",
        )
    except Exception as e:
        return RequestResult(
            model=model,
            node_id="unknown",
            latency_ms=(time.monotonic() - start) * 1000,
            ttft_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error=str(e)[:120],
            model_type="embed",
        )


async def send_image_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 120.0,
) -> RequestResult:
    """Send an image generation request and measure latency."""
    body = {
        "model": model,
        "prompt": random.choice(IMAGE_PROMPTS),
        "size": "512x512",
        "steps": 4,  # Fast for benchmarking
        "metadata": {"tags": ["benchmark"]},
    }
    start = time.monotonic()
    try:
        resp = await client.post("/api/generate-image", json=body, timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        node_id = resp.headers.get("x-fleet-node", "unknown")
        if resp.status_code != 200:
            return RequestResult(
                model=model,
                node_id=node_id,
                latency_ms=latency_ms,
                ttft_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                success=False,
                error=f"HTTP {resp.status_code}",
                model_type="image",
            )
        return RequestResult(
            model=model,
            node_id=node_id,
            latency_ms=latency_ms,
            ttft_ms=latency_ms,
            prompt_tokens=0,
            completion_tokens=1,
            success=True,
            model_type="image",
        )
    except Exception as e:
        return RequestResult(
            model=model,
            node_id="unknown",
            latency_ms=(time.monotonic() - start) * 1000,
            ttft_ms=0,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error=str(e)[:120],
            model_type="image",
        )


async def embed_worker(
    client: httpx.AsyncClient,
    model: str,
    results: list[RequestResult],
    stop_event: asyncio.Event,
    error_backoff: float = 1.0,
):
    """Continuously send embedding requests until stop_event is set."""
    while not stop_event.is_set():
        result = await send_embed_request(client, model)
        results.append(result)
        if not result.success:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=error_backoff)
                break
            except TimeoutError:
                pass


async def image_worker(
    client: httpx.AsyncClient,
    model: str,
    results: list[RequestResult],
    stop_event: asyncio.Event,
    error_backoff: float = 2.0,
):
    """Continuously send image generation requests until stop_event is set."""
    while not stop_event.is_set():
        result = await send_image_request(client, model)
        results.append(result)
        if not result.success:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=error_backoff)
                break
            except TimeoutError:
                pass


async def send_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 300.0,
    prompt_tokens: int | None = None,
    max_tokens: int | None = None,
    think: bool | None = None,
) -> RequestResult:
    """Send a streaming request via Ollama format to get token counts.

    ``prompt_tokens`` pads the prompt to about that length (``make_prompt``);
    ``max_tokens`` caps the reply (Ollama's ``num_predict``).  Both default to
    the original short prompt with no cap.  ``think=False`` turns a thinking
    model's reasoning off, which also stops the router raising a capped
    ``num_predict`` to its thinking minimum.  A reply from any model other than
    ``model`` is a failure (``EXACT_MODEL_HEADERS``).
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": make_prompt(prompt_tokens)}],
        "stream": True,
        "metadata": {"tags": ["benchmark"]},
    }
    if max_tokens:
        body["options"] = {"num_predict": max_tokens}
    if think is not None:
        body["think"] = think

    start = time.monotonic()
    ttft = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    node_id = ""
    first_content = True

    try:
        async with client.stream(
            "POST",
            "/api/chat",
            json=body,
            timeout=timeout,
            headers=EXACT_MODEL_HEADERS,
        ) as resp:
            node_id = resp.headers.get("x-fleet-node", "unknown")
            served = resp.headers.get("x-fleet-served-model", "")
            mismatch = served_mismatch(model, resp)

            if resp.status_code != 200 or mismatch:
                raw = await resp.aread()
                return RequestResult(
                    model=model,
                    node_id=node_id,
                    latency_ms=(time.monotonic() - start) * 1000,
                    ttft_ms=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    success=False,
                    error=mismatch or f"HTTP {resp.status_code}: {raw.decode()[:200]}",
                    served_model=served,
                )

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = chunk.get("message", {})
                # A thinking model streams its reasoning in "thinking" before any
                # "content"; its first token is the first of either, or TTFT reads
                # 0 for every request that is still reasoning at the cap.
                if first_content and (msg.get("content") or msg.get("thinking")):
                    ttft = (time.monotonic() - start) * 1000
                    first_content = False

                if chunk.get("done"):
                    prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                    completion_tokens = chunk.get("eval_count", 0) or 0

        latency_ms = (time.monotonic() - start) * 1000
        return RequestResult(
            model=model,
            node_id=node_id,
            latency_ms=latency_ms,
            ttft_ms=ttft,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            success=True,
            served_model=served,
        )

    except Exception as e:
        return RequestResult(
            model=model,
            node_id=node_id or "unknown",
            latency_ms=(time.monotonic() - start) * 1000,
            ttft_ms=ttft,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
            error=str(e)[:120],
        )


async def worker(
    client: httpx.AsyncClient,
    model: str,
    results: list[RequestResult],
    stop_event: asyncio.Event,
    error_backoff: float = 1.0,
    prompt_tokens: int | None = None,
    max_tokens: int | None = None,
    think: bool | None = None,
):
    """Continuously send requests until stop_event is set, with error backoff."""
    while not stop_event.is_set():
        result = await send_request(
            client, model, prompt_tokens=prompt_tokens, max_tokens=max_tokens, think=think
        )
        results.append(result)
        if not result.success:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=error_backoff)
                break
            except TimeoutError:
                pass


async def warm_node_model(
    ollama_url: str,
    model: str,
    *,
    embed: bool = False,
    num_ctx: int | None = None,
    timeout: float = 900.0,
) -> bool:
    """One real request straight to a node's Ollama: loads ``model`` if needed and
    runs it once.

    Straight to the node, not through the router, so every node gets warmed and
    not only the one the router picks.  A real computation, not an empty
    prompt: the first compute on a fresh runner took ~39 s on Turing GPUs in the
    2026-11-20 fleet benchmark, which an empty-prompt load does not trigger.
    ``keep_alive: -1`` as the router sends, and the resident ``num_ctx`` so
    Ollama doesn't reload the model at a different context.
    """
    if embed:
        path, body = "/api/embed", {"model": model, "input": "warm", "keep_alive": -1}
    else:
        options: dict = {"num_predict": 1}
        if num_ctx:
            options["num_ctx"] = num_ctx
        path = "/api/chat"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "options": options,
            "keep_alive": -1,
        }
    try:
        async with httpx.AsyncClient(base_url=ollama_url, timeout=timeout) as node:
            return (await node.post(path, json=body)).status_code == 200
    except httpx.HTTPError as exc:
        logger.warning(f"Warming {model} at {ollama_url} failed: {exc}")
        return False


async def warmup(
    targets: list[ModelTarget],
    nodes_info: list[NodeInfo],
    log_fn=None,
) -> list[ModelTarget]:
    """Warm every node that serves each target, directly; drop targets no node
    could warm.  Image targets are not warmed.

    log_fn: optional callback(message: str) for progress reporting.
    """
    if log_fn:
        log_fn("Warming up models on every node...")
    by_id = {ni.node_id: ni for ni in nodes_info}
    jobs: list[tuple[ModelTarget, str]] = []
    coros = []
    for t in targets:
        if t.model_type not in ("llm", "embed"):
            continue
        for node_id in t.nodes:
            ni = by_id.get(node_id)
            if ni is None or not ni.ollama_url:
                continue
            jobs.append((t, node_id))
            coros.append(
                warm_node_model(
                    ni.ollama_url,
                    t.name,
                    embed=t.model_type == "embed",
                    num_ctx=ni.model_contexts.get(t.name),
                )
            )
    outcomes = await asyncio.gather(*coros)

    warmed: dict[str, list[str]] = {}
    for (t, node_id), ok in zip(jobs, outcomes, strict=True):
        if ok:
            warmed.setdefault(t.name, []).append(node_id)
        if log_fn:
            log_fn(f"  {t.name} on {node_id}: {'ok' if ok else 'FAILED'}")
    return [t for t in targets if t.model_type not in ("llm", "embed") or t.name in warmed]


async def run_load(
    client: httpx.AsyncClient,
    targets: list[ModelTarget],
    duration: float,
    stop_event: asyncio.Event,
    results: list[RequestResult],
    *,
    prompt_tokens: int | None = None,
    max_tokens: int | None = None,
    think: bool | None = None,
    on_tick=None,
) -> tuple[list[dict], float]:
    """Run each target's workers for ``duration`` (or until ``stop_event``),
    drain in-flight requests, and return (fleet snapshots, seconds elapsed).

    The one load loop the CLI and the dashboard runner share.  ``on_tick(elapsed)``
    is called every two seconds for progress output.
    """
    tasks = []
    for target in targets:
        for _ in range(target.concurrency):
            if target.model_type == "embed":
                coro = embed_worker(client, target.name, results, stop_event)
            elif target.model_type == "image":
                coro = image_worker(client, target.name, results, stop_event)
            else:
                coro = worker(
                    client,
                    target.name,
                    results,
                    stop_event,
                    prompt_tokens=prompt_tokens,
                    max_tokens=max_tokens,
                    think=think,
                )
            tasks.append(asyncio.create_task(coro))

    snapshots: list[dict] = []
    poller = asyncio.create_task(poll_fleet_status(client, stop_event, snapshots))
    start = time.monotonic()
    while not stop_event.is_set():
        elapsed = time.monotonic() - start
        if elapsed >= duration:
            break
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=min(2.0, duration - elapsed))
        if on_tick:
            on_tick(time.monotonic() - start)
    stop_event.set()

    # Drain in-flight requests, then stop whatever is still waiting.
    _, pending = await asyncio.wait(tasks, timeout=30)
    for t in pending:
        t.cancel()
    poller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poller
    return snapshots, time.monotonic() - start


async def poll_fleet_status(
    client: httpx.AsyncClient,
    stop_event: asyncio.Event,
    snapshots: list[dict],
):
    """Periodically poll /fleet/status to capture utilization during benchmark."""
    while not stop_event.is_set():
        try:
            resp = await client.get("/fleet/status", timeout=5)
            if resp.status_code == 200:
                snapshots.append(resp.json())
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
            break
        except TimeoutError:
            pass


def build_report(
    results: list[RequestResult],
    duration: float,
    targets: list[ModelTarget],
    nodes_info: list[NodeInfo],
    fleet_snapshots: list[dict],
    run: dict | None = None,
) -> dict:
    """Build report data dict. Returns data for storage.

    ``run`` (workload, mode, ...) is stored inside ``fleet_snapshot``, the one
    free-form column a saved run keeps.
    """
    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    total_prompt = sum(r.prompt_tokens for r in success)
    total_completion = sum(r.completion_tokens for r in success)

    latencies = [r.latency_ms for r in success]
    ttfts = [r.ttft_ms for r in success if r.ttft_ms > 0]

    req_s = len(success) / duration if duration > 0 else 0
    tok_s = total_completion / duration if duration > 0 else 0

    # Build per-model results
    per_model = []
    if success:
        for model in sorted(set(r.model for r in success)):
            mr = [r for r in success if r.model == model]
            m_tok = sum(r.completion_tokens for r in mr)
            m_lat = statistics.mean(r.latency_ms for r in mr)
            m_tok_s = m_tok / duration if duration > 0 else 0
            m_ttft = (
                statistics.mean(r.ttft_ms for r in mr if r.ttft_ms > 0)
                if any(r.ttft_ms > 0 for r in mr)
                else 0
            )
            # Determine model type from results
            m_type = mr[0].model_type if mr else "llm"
            node_counts: dict[str, int] = {}
            for r in mr:
                node_counts[r.node_id] = node_counts.get(r.node_id, 0) + 1
            per_model.append(
                {
                    "model": model,
                    "model_type": m_type,
                    "requests": len(mr),
                    "nodes": node_counts,  # where the router placed it
                    "tok_s": round(m_tok_s, 1),
                    "avg_latency_ms": round(m_lat, 1),
                    "avg_ttft_ms": round(m_ttft, 1),
                }
            )

    # Build per-node results
    per_node = []
    if success:
        for node in sorted(set(r.node_id for r in success)):
            nr = [r for r in success if r.node_id == node]
            n_pct = len(nr) / len(success) * 100
            n_tok = sum(r.completion_tokens for r in nr)
            n_tok_s = n_tok / duration if duration > 0 else 0
            per_node.append(
                {
                    "node_id": node,
                    "requests": len(nr),
                    "pct": round(n_pct, 1),
                    "tok_s": round(n_tok_s, 1),
                    "tokens": n_tok,
                }
            )

    # Build peak utilization
    peak_util = []
    if fleet_snapshots:
        node_cpu_max: dict[str, float] = {}
        node_mem_max: dict[str, float] = {}
        node_active_max: dict[str, int] = {}
        node_cpu_samples: dict[str, list[float]] = {}
        node_mem_samples: dict[str, list[float]] = {}
        for snap in fleet_snapshots:
            for node in snap.get("nodes", []):
                nid = node["node_id"]
                cpu = node.get("cpu", {}).get("utilization_pct", 0)
                mem_total = node.get("memory", {}).get("total_gb", 1)
                mem_used = node.get("memory", {}).get("used_gb", 0)
                mem_pct = (mem_used / mem_total) * 100 if mem_total > 0 else 0
                active = node.get("ollama", {}).get("requests_active", 0)
                node_cpu_max[nid] = max(node_cpu_max.get(nid, 0), cpu)
                node_mem_max[nid] = max(node_mem_max.get(nid, 0), mem_pct)
                node_active_max[nid] = max(node_active_max.get(nid, 0), active)
                node_cpu_samples.setdefault(nid, []).append(cpu)
                node_mem_samples.setdefault(nid, []).append(mem_pct)

        for nid in sorted(node_cpu_max.keys()):
            peak_util.append(
                {
                    "node_id": nid,
                    "cpu_avg": round(statistics.mean(node_cpu_samples.get(nid, [0])), 1),
                    "cpu_peak": round(node_cpu_max[nid], 1),
                    "mem_avg": round(statistics.mean(node_mem_samples.get(nid, [0])), 1),
                    "mem_peak": round(node_mem_max[nid], 1),
                    "active_peak": node_active_max[nid],
                }
            )

    # Fleet snapshot for storage
    fleet_snapshot = {
        "nodes": [
            {
                "node_id": ni.node_id,
                "cores": ni.cores,
                "memory_total_gb": ni.memory_total_gb,
                "memory_available_gb": ni.memory_available_gb,
            }
            for ni in nodes_info
        ],
        "models": [
            {
                "name": t.name,
                "size_gb": t.size_gb,
                "concurrency": t.concurrency,
                "model_type": t.model_type,
            }
            for t in targets
        ],
    }
    if run:
        fleet_snapshot["run"] = run

    return {
        "run_id": f"bench-{int(time.time())}",
        "timestamp": time.time(),
        "duration_s": round(duration, 1),
        "total_requests": len(success),
        "total_failures": len(failed),
        # Failures that were answers from another model than the one asked for.
        "served_model_mismatches": sum(1 for r in failed if r.served_model),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "requests_per_sec": round(req_s, 2),
        "tokens_per_sec": round(tok_s, 1),
        "latency_p50_ms": round(percentile(latencies, 50), 1) if latencies else None,
        "latency_p95_ms": round(percentile(latencies, 95), 1) if latencies else None,
        "latency_p99_ms": round(percentile(latencies, 99), 1) if latencies else None,
        "ttft_p50_ms": round(percentile(ttfts, 50), 1) if ttfts else None,
        "ttft_p95_ms": round(percentile(ttfts, 95), 1) if ttfts else None,
        "ttft_p99_ms": round(percentile(ttfts, 99), 1) if ttfts else None,
        "fleet_snapshot": fleet_snapshot,
        "per_model_results": per_model,
        "per_node_results": per_node,
        "peak_utilization": peak_util,
    }
