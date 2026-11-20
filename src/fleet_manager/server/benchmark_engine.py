"""
Core benchmark engine — shared by CLI (scripts/benchmark.py) and server-side runner.

Provides fleet discovery, concurrent request generation, and report building.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
import math
import random
import statistics
import struct
import subprocess
import time
import zlib
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import httpx

import fleet_manager
from fleet_manager.common.errors import ollama_stream_error
from fleet_manager.common.stats import percentile
from fleet_manager.models.request import normalize_model_name
from fleet_manager.server.model_knowledge import model_type

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
    # Every filler word is at least one token, so ``budget`` words are enough:
    # encode once and cut, instead of re-encoding the growing text per batch.
    words: list[str] = []
    while len(words) < budget:
        words.extend(rng.sample(_FILLER_WORDS, k=min(40, len(_FILLER_WORDS))))
    tokens = enc.encode(" ".join(words))[:budget]
    return "Notes: " + enc.decode(tokens) + tail


def session_system_prompt(tokens: int) -> str:
    """One long system prompt every session shares, like an agent's tool prompt.

    Deterministic, so every session (and every node) sees the same head: what a
    backend's prefix cache, and herd's prefix affinity, can reuse.
    """
    rng = random.Random(0)
    return (
        "You are a coding agent working in a large repository. Reference notes "
        "follow; use them when relevant.\n" + make_prompt(tokens, rng)
    )


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

# For vision models: questions about the image the request carries.
VISION_PROMPTS = [
    "Describe this image in one sentence.",
    "What colours are in this image? One sentence.",
    "Is this a photo or a drawing? One sentence.",
]


def _test_png(size: int = 64) -> bytes:
    """A small RGB gradient PNG, drawn here so no binary file is committed.

    64 px, not 1: vision encoders resize to their own input (224 for DINOv2 and
    CLIP), and some (Qwen-VL) refuse images under 28 px a side.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    step = 255 // (size - 1)
    rows = b"".join(
        b"\x00" + bytes(c for x in range(size) for c in (x * step, y * step, 128))
        for y in range(size)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


TEST_IMAGE_B64 = base64.b64encode(_test_png()).decode()

# Workloads the engine can send.  STT has none yet (it needs audio).
WORKLOAD_TYPES = ("llm", "vision", "embed", "embed-image", "image")
# What a run measures unless asked otherwise: everything but the image
# workload for vision-capable chat models, which is opt-in (``select_workloads``).
DEFAULT_TYPES = ("llm", "embed", "embed-image", "image")
# Chat workloads: the ones that stream tokens, so the ones TTFT and token rates
# are about.
CHAT_WORKLOADS = ("llm", "vision")
# Workloads warmed straight at a node's Ollama; image generation and vision
# embeddings run in node sidecars, so their first request includes the load.
WARMED_TYPES = ("llm", "vision", "embed")
# A request still in flight when the drain ran out: recorded as a failure, never
# dropped, because under overload it is the slowest requests the drain cuts off.
CANCELLED_AT_DRAIN = "cancelled at drain"

# No connection cap.  httpx's default pool holds 100 connections, so in an open
# loop the 101st concurrent request waited inside the client: timed as server
# latency while the schedule lag read ~1 ms.
UNLIMITED_POOL = httpx.Limits(max_connections=None, max_keepalive_connections=None)


def bench_client(base_url: str, timeout: float = 300.0) -> httpx.AsyncClient:
    """The client every benchmark request goes through: no pool limit."""
    return httpx.AsyncClient(base_url=base_url, timeout=timeout, limits=UNLIMITED_POOL)


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
    model_type: str = "llm"  # llm, vision, embed, embed-image, image
    served_model: str = ""  # X-Fleet-Served-Model: what actually answered
    turn: int = -1  # turn within a multi-turn session (session workload), else -1
    reply: str = ""  # the reply text, kept only where a session needs its history
    started_ms: float = 0.0  # when it was sent, ms after the run began (the run clock)
    request_id: str = ""  # X-Request-Id, joins it to the router's trace; "" if absent
    schedule_lag_ms: float | None = None  # open loop only: how late it started
    # Why the reply ended: Ollama's done_reason / OpenAI's finish_reason
    # ("stop", "length", ...); None when the stream did not say.
    finish_reason: str | None = None


@dataclass
class ModelTarget:
    name: str
    size_gb: float
    concurrency: int
    nodes: list[str] = field(default_factory=list)
    model_type: str = "llm"  # what the model is: llm, vision, embed, embed-image, image, stt
    # What is sent to it.  A vision-capable chat model is a chat model: it gets
    # the text workload unless the run asks for vision (``select_workloads``).
    workload: str = ""
    # Loaded on no node, only on disk: not warmed, so its first request
    # includes the load wherever the router puts it.
    on_disk_only: bool = False

    def __post_init__(self) -> None:
        if not self.workload:
            self.workload = "llm" if self.model_type == "vision" else self.model_type


def select_workloads(targets: Iterable[ModelTarget], types: Collection[str]) -> list[ModelTarget]:
    """The targets the requested ``types`` cover, each set to the workload it runs.

    A vision-capable model (gemma3, llava, qwen-vl) runs the image workload when
    ``"vision"`` is asked for, else the text workload under ``"llm"``: never
    both, so one model is not measured twice at double the concurrency.  Every
    other target runs as its type, when that type is asked for.
    """
    selected = []
    for t in targets:
        if t.model_type == "vision":
            workload = "vision" if "vision" in types else "llm" if "llm" in types else None
        else:
            workload = t.model_type if t.model_type in types else None
        if workload:
            t.workload = workload
            selected.append(t)
    return selected


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
    # For the run manifest; None where /fleet/status doesn't report it.
    chip: str | None = None
    agent_version: str | None = None
    ollama_version: str | None = None
    num_parallel: int | None = None
    # Per loaded model: digest, quantization, parameter_size, size_gb, context_length.
    model_meta: dict[str, dict] = field(default_factory=dict)


def _loaded_meta(m: dict) -> dict:
    return {
        "digest": m.get("digest") or None,
        "quantization": m.get("quantization") or None,
        "parameter_size": m.get("parameter_size") or None,
        "size_gb": m.get("size_gb"),
        "context_length": m.get("context_length") or None,
    }


async def reported_capabilities(client: httpx.AsyncClient) -> dict[str, list[str]]:
    """Ollama's own capabilities per model, from the router's ``/api/tags``
    (``/fleet/status`` leaves them out); ``{}`` from an endpoint without it.

    Presence-only, like ``serializers.model_has_capability``: an older Ollama
    under-reports, so a missing capability only means the name decides.
    """
    try:
        resp = await client.get("/api/tags")
        if resp.status_code != 200:
            return {}
        return {m["name"]: list(m.get("capabilities") or []) for m in resp.json()["models"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {}


def match_requested(
    targets: list[ModelTarget], names: list[str], weights: dict[str, float]
) -> tuple[list[ModelTarget], dict[str, float], list[str]]:
    """The discovered targets for the ``--models`` asked for, in that order.

    Ollama names a loaded or on-disk model with its tag ("nomic-embed-text:latest");
    people ask without one.  Matching exactly dropped every untagged model from
    a run without a word (fleet benchmark run 4, 2026-11-20: the embedding and
    vision halves of two tests never ran).  Returns the targets, the weights
    keyed by the targets' own names, and the names nothing matched.
    """
    by_name = {normalize_model_name(t.name): t for t in targets}
    found = [by_name[normalize_model_name(n)] for n in names if normalize_model_name(n) in by_name]
    remapped = {
        by_name[normalize_model_name(n)].name: w
        for n, w in weights.items()
        if normalize_model_name(n) in by_name
    }
    missing = [n for n in names if normalize_model_name(n) not in by_name]
    return found, remapped, missing


async def discover_fleet(
    client: httpx.AsyncClient,
    include_on_disk: Iterable[str] = (),
) -> tuple[list[ModelTarget], list[NodeInfo]]:
    """Discover nodes and the models to benchmark, with concurrency per model.

    Targets are the models loaded on any online node, typed by
    ``model_knowledge.model_type`` (with the capabilities Ollama reports), plus
    any model in ``include_on_disk`` that is not loaded but is on some node's
    disk (``on_disk_only``: its first request includes the load) or served by a
    node's vision-embedding sidecar (``embed-image``).  Each target's
    ``workload`` is its type's default; ``select_workloads`` sets the run's.
    Concurrency is calculated per-node:
    1. Total node budget = min(available_gb / KV_CACHE, MAX_TOTAL_PER_NODE)
    2. Budget is split evenly across models on that node
    3. Each model's share is capped at MAX_CONCURRENCY_PER_MODEL
    """
    resp = await client.get("/fleet/status")
    resp.raise_for_status()
    data = resp.json()
    capabilities = await reported_capabilities(client)

    nodes_info: list[NodeInfo] = []
    model_map: dict[str, ModelTarget] = {}
    on_disk_by_node: dict[str, tuple[set[str], dict]] = {}
    vision_embed_by_node: dict[str, set[str]] = {}

    def target(name: str, size_gb: float, concurrency: int, **kw) -> ModelTarget:
        if name not in model_map:
            kw.setdefault("model_type", model_type(name, capabilities.get(name, ())).value)
            model_map[name] = ModelTarget(name=name, size_gb=size_gb, concurrency=concurrency, **kw)
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
                chip=(node.get("hardware") or {}).get("chip") or None,
                agent_version=node.get("agent_version") or None,
                ollama_version=ollama.get("version") or None,
                num_parallel=ollama.get("num_parallel") or None,  # 0 = not reported
                model_meta={m["name"]: _loaded_meta(m) for m in loaded},
            )
        )
        vision_embed_by_node[node_id] = {
            m["name"] for m in (node.get("vision_embedding") or {}).get("models_available", [])
        }

        if loaded:
            # Available memory is already free (models are loaded and accounted for).
            mem_budget = int(available_gb / KV_CACHE_PER_REQUEST_GB)
            node_budget = max(MIN_CONCURRENCY, min(mem_budget, MAX_TOTAL_PER_NODE))
            base_per_model = max(1, node_budget // len(loaded))
            per_model_conc = min(base_per_model, MAX_CONCURRENCY_PER_MODEL)
            for m in loaded:
                target(m["name"], m.get("size_gb", 0), per_model_conc).nodes.append(node_id)

        on_disk_by_node[node_id] = (
            set(ollama.get("models_available", [])),
            ollama.get("models_available_sizes", {}) or {},
        )

    # A requested model that is loaded nowhere: every node with it on disk is a
    # candidate, and none is warmed (``on_disk_only``): its first request
    # includes the load, on the node the router picks.  Warming it on each node
    # loaded it everywhere, evicting resident models before the router decided
    # anything; for the same reason a model loaded somewhere keeps only the
    # nodes it is loaded on (fleet benchmark run 2, 2026-11-20).
    # Requested names match with or without ":latest", as Ollama reports a
    # model loaded or on disk with its tag ("nomic-embed-text:latest") while
    # people ask for it without one.
    known = {normalize_model_name(k) for k in model_map}
    for name in include_on_disk:
        if normalize_model_name(name) in known:
            continue
        for node_id, (on_disk, sizes) in on_disk_by_node.items():
            actual = next(
                (d for d in on_disk if normalize_model_name(d) == normalize_model_name(name)), None
            )
            if actual:
                t = target(actual, sizes.get(actual, 0.0), MIN_CONCURRENCY, on_disk_only=True)
                t.nodes.append(node_id)
        # Vision embeddings live in the node's sidecar, never in Ollama.
        for node_id, served in vision_embed_by_node.items():
            if name in served:
                target(name, 0.0, MIN_CONCURRENCY, model_type="embed-image").nodes.append(node_id)

    return list(model_map.values()), nodes_info


def explicit_targets(names: Iterable[str], concurrency: int = 1) -> list[ModelTarget]:
    """Targets for an endpoint with no ``/fleet/status`` (plain Ollama,
    llama-swap, llama.cpp's router): the named models, typed by name, no nodes."""
    return [
        ModelTarget(name=n, size_gb=0.0, concurrency=concurrency, model_type=model_type(n).value)
        for n in names
    ]


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


def _same_model(model: str) -> tuple[str, str]:
    """The names an answer from ``model`` may carry: ``:latest`` is the same model."""
    return model, f"{model}:latest"


def served_mismatch(model: str, resp: httpx.Response) -> str | None:
    """Why ``resp`` is not an answer from ``model``, or None if it is."""
    served = resp.headers.get("x-fleet-served-model", "")
    if not served or served in _same_model(model):
        return None
    return f"served by {served}, not {model}"


def _failure(model: str, model_type: str, start: float, error: str, **kw) -> RequestResult:
    return RequestResult(
        model=model,
        node_id=kw.pop("node_id", "unknown"),
        latency_ms=(time.monotonic() - start) * 1000,
        ttft_ms=kw.pop("ttft_ms", 0),
        prompt_tokens=0,
        completion_tokens=0,
        success=False,
        error=error,
        model_type=model_type,
        **kw,
    )


def _exception_text(e: Exception) -> str:
    """A failed request's error: the exception's class too, since httpx's
    timeouts have no message (a ReadTimeout was recorded as "")."""
    return f"{type(e).__name__}: {e}"[:120] if str(e) else type(e).__name__


def _ids(resp: httpx.Response) -> dict:
    """The response headers every result keeps: what answered, and the id that
    joins it to the router's trace (absent from a backend that isn't herd)."""
    return {
        "served_model": resp.headers.get("x-fleet-served-model", ""),
        "request_id": resp.headers.get("x-request-id", ""),
    }


async def _send_unary(
    client: httpx.AsyncClient,
    model: str,
    model_type: str,
    path: str,
    body: dict,
    timeout: float,
) -> RequestResult:
    """One non-streaming request (embeddings, images): latency is the whole answer."""
    start = time.monotonic()
    try:
        resp = await client.post(path, json=body, timeout=timeout, headers=EXACT_MODEL_HEADERS)
        latency_ms = (time.monotonic() - start) * 1000
        node_id = resp.headers.get("x-fleet-node", "unknown")
        mismatch = served_mismatch(model, resp)
        if resp.status_code != 200 or mismatch:
            return _failure(
                model,
                model_type,
                start,
                mismatch or f"HTTP {resp.status_code}",
                node_id=node_id,
                **_ids(resp),
            )
        return RequestResult(
            model=model,
            node_id=node_id,
            latency_ms=latency_ms,
            ttft_ms=latency_ms,
            prompt_tokens=0,
            completion_tokens=1,
            success=True,
            model_type=model_type,
            **_ids(resp),
        )
    except Exception as e:
        return _failure(model, model_type, start, _exception_text(e))


async def send_embed_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 60.0,
    rng: random.Random | None = None,
) -> RequestResult:
    """One text embedding request (``/api/embed``)."""
    body = {
        "model": model,
        "input": (rng or random).choice(EMBED_PROMPTS),
        "metadata": {"tags": ["benchmark"]},
    }
    return await _send_unary(client, model, "embed", "/api/embed", body, timeout)


async def send_embed_image_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 60.0,
) -> RequestResult:
    """One vision embedding request (``/api/embed-image``: DINOv2, CLIP, SigLIP)
    with the engine's test image."""
    body = {"model": model, "images": [TEST_IMAGE_B64]}
    return await _send_unary(client, model, "embed-image", "/api/embed-image", body, timeout)


async def send_image_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 120.0,
    rng: random.Random | None = None,
) -> RequestResult:
    """One image generation request (``/api/generate-image``)."""
    body = {
        "model": model,
        "prompt": (rng or random).choice(IMAGE_PROMPTS),
        "size": "512x512",
        "steps": 4,  # Fast for benchmarking
        "metadata": {"tags": ["benchmark"]},
    }
    return await _send_unary(client, model, "image", "/api/generate-image", body, timeout)


def _with_image(message: dict, api: str) -> dict:
    """``message`` carrying the test image, in the API's own shape."""
    if api == "openai":
        return {
            "role": message["role"],
            "content": [
                {"type": "text", "text": message["content"]},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{TEST_IMAGE_B64}"},
                },
            ],
        }
    return {**message, "images": [TEST_IMAGE_B64]}


def _chat_body(
    api: str,
    model: str,
    messages: list[dict],
    tags: list[str],
    max_tokens: int | None,
    temperature: float | None,
    think: bool | None,
) -> tuple[str, dict]:
    """(path, body) of a streaming chat request in Ollama's or OpenAI's format."""
    body: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "metadata": {"tags": tags},
    }
    if api == "openai":
        body["stream_options"] = {"include_usage": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        return "/v1/chat/completions", body
    options: dict = {}
    if max_tokens:
        options["num_predict"] = max_tokens
    if temperature is not None:
        options["temperature"] = temperature
    if options:
        body["options"] = options
    if think is not None:
        body["think"] = think
    return "/api/chat", body


class _Chunk(NamedTuple):
    """One streamed line, as ``send_request`` reads it."""

    content: str = ""
    token: bool = False  # a first token: content or reasoning
    counts: tuple[int, int] | None = None  # (prompt, completion) on the final chunk
    error: str | None = None  # the stream's in-band error
    end: bool = False  # Ollama's done:true, or OpenAI's [DONE]
    finish_reason: str | None = None


def _parse_chunk(api: str, line: str) -> _Chunk | None:
    """One streamed line, or None for a line that carries nothing (blank,
    keep-alive, not JSON).

    A thinking model streams its reasoning (Ollama ``thinking``, OpenAI-style
    ``reasoning_content`` / ``reasoning``) before any content; its first token
    is the first of either, or TTFT reads 0 for every request still reasoning
    at the cap.  Counts are Ollama's ``prompt_eval_count`` / ``eval_count`` on
    the ``done`` chunk, or the OpenAI ``usage`` object.  An error is Ollama's
    ``{"error": ...}`` line or an OpenAI ``data: {"error": {...}}`` event: what
    a backend or the router sends when a stream fails after it began (HTTP 200).
    """
    line = line.strip()
    if api == "openai":
        if not line.startswith("data:"):
            return None
        line = line[5:].strip()
        if line == "[DONE]":
            return _Chunk(end=True)
    if not line:
        return None
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(chunk, dict):
        return None
    error = ollama_stream_error(chunk)
    if error:
        return _Chunk(error=error)
    if api == "openai":
        content, token, finish = "", False, None
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content += delta.get("content") or ""
            token = token or bool(
                delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
            )
            finish = choice.get("finish_reason") or finish
        usage = chunk.get("usage")
        counts = (
            (usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0)
            if usage
            else None
        )
        return _Chunk(content, token, counts, finish_reason=finish)
    msg = chunk.get("message") or {}
    token = bool(msg.get("content") or msg.get("thinking"))
    if not chunk.get("done"):
        return _Chunk(msg.get("content") or "", token)
    counts = (chunk.get("prompt_eval_count") or 0, chunk.get("eval_count") or 0)
    return _Chunk(
        msg.get("content") or "", token, counts, end=True, finish_reason=chunk.get("done_reason")
    )


async def send_request(
    client: httpx.AsyncClient,
    model: str,
    timeout: float = 300.0,
    prompt_tokens: int | None = None,
    max_tokens: int | None = None,
    think: bool | None = None,
    messages: list[dict] | None = None,
    tags: list[str] | None = None,
    node_label: str = "",
    *,
    rng: random.Random | None = None,
    prompt: str | None = None,
    temperature: float | None = None,
    image: bool = False,
    api: str = "ollama",
) -> RequestResult:
    """Send one streaming chat request and measure TTFT, latency and tokens.

    ``api`` is ``"ollama"`` (``/api/chat``, the default) or ``"openai"``
    (``/v1/chat/completions``, for endpoints that speak only that: llama-swap,
    llama-server).  ``prompt`` is the user message as given; otherwise
    ``prompt_tokens`` pads it to about that length (``make_prompt``, drawing
    from ``rng``).  ``max_tokens`` caps the reply (Ollama's ``num_predict``);
    ``temperature`` is sent only when set.  ``think=False`` turns a thinking
    model's reasoning off (Ollama API only), which also stops the router raising
    a capped ``num_predict`` to its thinking minimum.  ``image`` attaches the
    engine's test PNG, for vision models.  A reply from any model other than
    ``model`` is a failure (``EXACT_MODEL_HEADERS``).
    """
    rng = rng or random
    keep_reply = messages is not None  # a session's history needs the reply
    if messages is None:
        text = prompt or (rng.choice(VISION_PROMPTS) if image else make_prompt(prompt_tokens, rng))
        user = {"role": "user", "content": text}
        messages = [_with_image(user, api) if image else user]
    path, body = _chat_body(
        api, model, messages, ["benchmark", *(tags or [])], max_tokens, temperature, think
    )
    kind = "vision" if image else "llm"

    start = time.monotonic()
    ttft = 0.0
    counts: tuple[int, int] | None = None
    node_id = node_label
    ids: dict = {}
    reply_parts: list[str] = []

    try:
        async with client.stream(
            "POST", path, json=body, timeout=timeout, headers=EXACT_MODEL_HEADERS
        ) as resp:
            node_id = resp.headers.get("x-fleet-node", node_label or "unknown")
            ids = _ids(resp)
            mismatch = served_mismatch(model, resp)

            if resp.status_code != 200 or mismatch:
                raw = await resp.aread()
                return _failure(
                    model,
                    kind,
                    start,
                    mismatch or f"HTTP {resp.status_code}: {raw.decode()[:200]}",
                    node_id=node_id,
                    **ids,
                )

            chunks = 0
            ended = False
            finish: str | None = None
            async for line in resp.aiter_lines():
                chunk = _parse_chunk(api, line)
                if chunk is None:
                    continue
                if chunk.error:
                    # HTTP 200, then the stream failed: the node died mid-reply,
                    # the context overflowed.  A failure, with the reason given.
                    return _failure(
                        model,
                        kind,
                        start,
                        f"stream error: {chunk.error[:200]}",
                        node_id=node_id,
                        ttft_ms=ttft,
                        **ids,
                    )
                if chunk.token:
                    chunks += 1
                    if not ttft:
                        ttft = (time.monotonic() - start) * 1000
                if keep_reply and chunk.content:
                    reply_parts.append(chunk.content)
                if chunk.counts:
                    counts = chunk.counts
                finish = chunk.finish_reason or finish
                ended = ended or chunk.end
        # A stream that just stops is not an answer: Ollama ends every complete
        # one with done:true, an OpenAI server with [DONE] or a finish_reason.
        if not (ended or (api == "openai" and finish)):
            marker = "done" if api == "ollama" else "[DONE] or a finish_reason"
            return _failure(
                model,
                kind,
                start,
                f"stream ended without {marker}",
                node_id=node_id,
                ttft_ms=ttft,
                **ids,
            )
        prompt_n, completion_n = counts or (0, chunks)
        return RequestResult(
            model=model,
            node_id=node_id,
            latency_ms=(time.monotonic() - start) * 1000,
            ttft_ms=ttft,
            prompt_tokens=prompt_n,
            completion_tokens=completion_n,
            success=True,
            model_type=kind,
            reply="".join(reply_parts),
            finish_reason=finish,
            **ids,
        )

    except Exception as e:
        return _failure(
            model,
            kind,
            start,
            _exception_text(e),
            node_id=node_id or "unknown",
            ttft_ms=ttft,
            **ids,
        )


async def _recorded(
    request: Awaitable[RequestResult],
    model: str,
    kind: str,
    origin: float,
    results: list[RequestResult] | None,
    **fields,
) -> RequestResult:
    """Await one request; its result stamped with when it started on the run
    clock (``origin``) and ``fields`` (turn, schedule lag), and appended to
    ``results``.

    A request cancelled in flight (the drain ran out) is appended as a
    ``CANCELLED_AT_DRAIN`` failure before the cancellation goes on: dropping it
    lost exactly the slowest requests of an overloaded run.
    """
    t0 = time.monotonic()

    def keep(result: RequestResult) -> RequestResult:
        result.started_ms = round((t0 - origin) * 1000, 1)
        for name, value in fields.items():
            setattr(result, name, value)
        if results is not None:
            results.append(result)
        return result

    try:
        result = await request
    except asyncio.CancelledError:
        keep(_failure(model, kind, t0, CANCELLED_AT_DRAIN))
        raise
    return keep(result)


async def send_one(
    client: httpx.AsyncClient,
    target: ModelTarget,
    *,
    rng: random.Random,
    origin: float,
    chat: dict | None = None,
    results: list[RequestResult] | None = None,
    **fields,
) -> RequestResult:
    """One request of ``target``'s workload, stamped with when it started on
    the run clock (``origin`` is the run's ``time.monotonic()`` start) and
    appended to ``results`` (see ``_recorded``; ``fields`` are set on it).

    ``chat`` is ``send_request``'s workload keywords (prompt_tokens, max_tokens,
    think, prompt, temperature, api, timeout), used for chat and vision targets.
    """
    kind = target.workload
    if kind == "embed":
        request = send_embed_request(client, target.name, rng=rng)
    elif kind == "embed-image":
        request = send_embed_image_request(client, target.name)
    elif kind == "image":
        request = send_image_request(client, target.name, rng=rng)
    else:
        request = send_request(client, target.name, rng=rng, image=kind == "vision", **(chat or {}))
    return await _recorded(request, target.name, kind, origin, results, **fields)


class RequestBudget:
    """``--requests N``: a total shared by every worker; ``None`` is unlimited."""

    def __init__(self, total: int | None = None):
        self.remaining = total

    def take(self) -> bool:
        if self.remaining is None:
            return True
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


class ModelSequence:
    """Which target each request goes to, when workers share the targets.

    ``alternate`` cycles through the targets in order, each repeated by its
    (whole-number) weight: ``a=2,b=1`` gives a, a, b, a, a, b.  ``random`` draws
    each request's target by weight from ``rng``, so a seeded run asks for the
    same models in the same order every time.
    """

    def __init__(
        self,
        targets: list[ModelTarget],
        mode: str,
        weights: dict[str, float] | None = None,
        rng: random.Random | None = None,
    ):
        if not targets:
            raise ValueError("a model sequence needs at least one model")
        weights = weights or {}
        unknown = set(weights) - {t.name for t in targets}
        if unknown:
            raise ValueError(f"weights for models not in the run: {sorted(unknown)}")
        self.targets = targets
        self.weights = [float(weights.get(t.name, 1)) for t in targets]
        if any(w <= 0 for w in self.weights):
            raise ValueError("model weights must be positive")
        self.mode = mode
        self.rng = rng or random.Random()
        if mode == "alternate":
            if any(w != int(w) for w in self.weights):
                raise ValueError("--sequence alternate needs whole-number weights")
            order = [t for t, w in zip(targets, self.weights, strict=True) for _ in range(int(w))]
            self._cycle = itertools.cycle(order)
        elif mode != "random":
            raise ValueError(f"unknown sequence {mode!r} (alternate or random)")

    def next(self) -> ModelTarget:
        if self.mode == "alternate":
            return next(self._cycle)
        return self.rng.choices(self.targets, weights=self.weights)[0]


def parse_models(values: Iterable[str]) -> tuple[list[str], dict[str, float]]:
    """``--models`` as (names in order, weights): ``a b``, ``a=3,b=1`` or ``a=3 b``.

    Model names never contain ``=`` or ``,``; a name without a weight has none
    in the returned dict (weight 1 where weights apply).
    """
    names: list[str] = []
    weights: dict[str, float] = {}
    for value in values:
        for item in filter(None, (p.strip() for p in value.split(","))):
            name, sep, weight = item.partition("=")
            names.append(name)
            if sep:
                try:
                    weights[name] = float(weight)
                except ValueError:
                    raise ValueError(f"bad weight in {item!r}") from None
    return names, weights


async def open_loop(
    fire: Callable[[int, float], Awaitable[None]],
    rate: float,
    count: int | None,
    *,
    stop_event: asyncio.Event | None = None,
    drain_timeout: float | None = None,
) -> list[float]:
    """Open-loop arrivals: start ``fire(i, lag_s)`` at ``i / rate`` seconds, for
    ``i < count`` (``None``: until ``stop_event``), never waiting for earlier
    calls to finish, so a slow server can't slow the arrivals and hide its
    queueing (the closed loop's blind spot).  Then waits for the calls
    (at most ``drain_timeout`` s; the rest are cancelled, and so are all of
    them if this is cancelled, and each has finished when this returns).

    Returns each call's schedule lag in seconds (how late it started): above a
    millisecond or two, the client was the limit, not the server.
    """
    if rate <= 0:
        raise ValueError("rate must be positive")
    tasks: list[asyncio.Task] = []
    lags: list[float] = []
    start = time.perf_counter()
    try:
        for i in range(count) if count is not None else itertools.count():
            due = start + i / rate
            delay = due - time.perf_counter()
            if delay > 0:
                if stop_event is None:
                    await asyncio.sleep(delay)
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
            if stop_event is not None and stop_event.is_set():
                break
            lag = max(0.0, time.perf_counter() - due)
            lags.append(lag)
            tasks.append(asyncio.create_task(fire(i, lag)))
        if tasks:
            await asyncio.wait(tasks, timeout=drain_timeout)
    finally:
        for t in tasks:
            t.cancel()
        # Let each cancelled call run its own handling (a benchmark request
        # records itself as cancelled) before anyone reads what they recorded.
        await asyncio.gather(*tasks, return_exceptions=True)
        for t in tasks:
            if not t.cancelled() and t.exception():
                logger.warning(f"open-loop request failed: {t.exception()!r}")
    return lags


async def worker(
    client: httpx.AsyncClient,
    pick: Callable[[], ModelTarget],
    results: list[RequestResult],
    stop_event: asyncio.Event,
    *,
    rng: random.Random,
    origin: float,
    budget: RequestBudget | None = None,
    chat: dict | None = None,
):
    """Send ``pick()``'s next request, one after another, until ``stop_event``
    or the budget runs out; back off after a failure (2 s for image generation,
    1 s otherwise) so a broken model doesn't spin."""
    budget = budget or RequestBudget()
    while not stop_event.is_set() and budget.take():
        target = pick()
        result = await send_one(client, target, rng=rng, origin=origin, chat=chat, results=results)
        if not result.success:
            backoff = 2.0 if target.workload == "image" else 1.0
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                break
            except TimeoutError:
                pass


async def session_worker(
    pick_client,
    model: str,
    results: list[RequestResult],
    stop_event: asyncio.Event,
    *,
    system_prompt: str,
    turns: int,
    user_tokens: int = 250,
    max_tokens: int | None = 150,
    think: bool | None = None,
    tag_sessions: bool = True,
    rng: random.Random | None = None,
    origin: float | None = None,
    budget: RequestBudget | None = None,
):
    """Multi-turn conversations, one after another, until ``stop_event``.

    Each starts from ``system_prompt`` (shared by every session) and adds a
    ~``user_tokens`` user message per turn plus the model's reply, so turn N
    resends everything before it: what coding agents do, and what a backend's
    prefix cache saves.  ``pick_client()`` returns (client, node label) per
    request: the router, or the next node in a round-robin baseline.  Sessions
    are tagged ``session:<id>`` so the router can tell them apart (they all come
    from one benchmark client, whose IP would otherwise merge them).  Session
    ids and user turns come from ``rng``, so a seeded run sends the same text.
    """
    rng = rng or random.Random()
    origin = time.monotonic() if origin is None else origin
    budget = budget or RequestBudget()
    while not stop_event.is_set():
        session = f"session:bench-{rng.getrandbits(48):012x}"
        messages = [{"role": "system", "content": system_prompt}]
        for turn in range(turns):
            if stop_event.is_set() or not budget.take():
                return
            messages.append({"role": "user", "content": make_prompt(user_tokens, rng)})
            client, label = pick_client()
            request = send_request(
                client,
                model,
                max_tokens=max_tokens,
                think=think,
                messages=list(messages),
                tags=[session] if tag_sessions else None,
                node_label=label,
            )
            result = await _recorded(request, model, "llm", origin, results, turn=turn)
            if not result.success:
                break
            messages.append({"role": "assistant", "content": result.reply})
            result.reply = ""  # history kept in `messages`; don't hold it twice


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
    could warm.  Only ``WARMED_TYPES`` workloads are warmed (vision as chat, with
    no image: that loads the model).  Not warmed, so their first request includes
    the load and shows as ``first_ttft_ms`` in the report: image generation and
    vision embeddings (node sidecars), and a model only on disk
    (``on_disk_only``), which warming would load on every node that has it.

    log_fn: optional callback(message: str) for progress reporting.
    """
    if log_fn:
        log_fn("Warming up models on every node...")
    by_id = {ni.node_id: ni for ni in nodes_info}
    jobs: list[tuple[ModelTarget, str]] = []
    coros = []

    def warmed_here(t: ModelTarget) -> bool:
        return t.workload in WARMED_TYPES and not t.on_disk_only

    for t in targets:
        if not warmed_here(t):
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
                    embed=t.workload == "embed",
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
    return [t for t in targets if not warmed_here(t) or t.name in warmed]


@dataclass
class LoadResult:
    """What ``run_load`` saw, on the run clock its requests are stamped with."""

    snapshots: list[dict]  # /fleet/status polls, each with its "t_ms" on the run clock
    elapsed_s: float  # the whole run, drain included
    started_at: str  # UTC ISO 8601: the run clock's zero
    ended_at: str
    seed: int  # what the run's random choices came from: rerun with it to repeat them
    # What rates are over: the send window in an open loop (arrivals), the load
    # phase in a closed loop.  Never the drain, when nothing new is sent.
    window_s: float


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


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
    prompt: str | None = None,
    temperature: float | None = None,
    api: str = "ollama",
    timeout: float = 300.0,
    on_tick=None,
    sessions: dict | None = None,
    fleet_client: httpx.AsyncClient | None = None,
    sequence: str | None = None,
    weights: dict[str, float] | None = None,
    concurrency: int | None = None,
    rate: float | None = None,
    max_requests: int | None = None,
    seed: int | None = None,
) -> LoadResult:
    """Send load for ``duration`` s (or until ``stop_event``, or ``max_requests``
    sent), drain in-flight requests, and return what was seen.

    The drain gives in-flight requests up to ``timeout`` (the per-request
    timeout) to finish; any still running then is cancelled and recorded as a
    ``CANCELLED_AT_DRAIN`` failure.  The fleet is polled until the drain ends,
    so model loads and evictions in it are seen.  ``LoadResult.window_s``, what
    rates are over, leaves the drain out.

    The one load loop the CLI and the dashboard runner share.  Three shapes:

    - **per model** (default): each target's ``concurrency`` workers send to it,
      one request after another (closed loop);
    - **shared** (``sequence`` or ``weights``): ``concurrency`` workers (default:
      the targets' total) each take the next model from a ``ModelSequence``:
      ``"alternate"`` or ``"random"`` (weighted; the default when weights are
      given);
    - **open loop** (``rate``): requests start ``rate`` per second on schedule,
      whatever is still in flight, each taking the next model from the sequence
      (``"alternate"`` unless weights make it ``"random"``); every result keeps
      its ``schedule_lag_ms``.

    ``sessions`` switches chat targets to multi-turn sessions (per-model shape
    only): ``session_worker`` keyword arguments plus ``clients``, a list of
    (client, label) taken in turn per request (one entry, the router; or one per
    node for a round-robin baseline).  ``fleet_client`` is polled for fleet state
    every 3 s (model load/evict events, utilization); ``None`` polls nothing, for
    an endpoint with no ``/fleet/status``.  ``seed`` seeds every random choice
    (prompts, model sequence, session ids); ``None`` picks one, returned so the
    run can be repeated.  ``timeout`` is per chat request (a cold load of a
    large model is slow).  ``on_tick(elapsed)`` is called about every two seconds.
    """
    if sessions and (sequence or weights or rate):
        raise ValueError("sessions run per model; they don't combine with a sequence or a rate")
    if seed is None:
        seed = random.SystemRandom().randrange(2**31)
    chat = {
        "prompt_tokens": prompt_tokens,
        "max_tokens": max_tokens,
        "think": think,
        "prompt": prompt,
        "temperature": temperature,
        "api": api,
        "timeout": timeout,
    }
    runnable = [t for t in targets if t.workload in WORKLOAD_TYPES]
    for t in targets:
        if t not in runnable:
            logger.warning(f"Benchmark: no workload for {t.model_type} model {t.name}; skipped")
    budget = RequestBudget(max_requests)
    # Each worker draws from its own generator, seeded from the run's, so its
    # prompts don't depend on how the others' requests interleave.
    rngs = (random.Random(f"{seed}-{k}") for k in itertools.count())
    picker = None
    if runnable and (sequence or weights or rate):
        mode = sequence or ("random" if weights else "alternate")
        picker = ModelSequence(runnable, mode, weights, random.Random(seed))

    origin = time.monotonic()
    started_at = _utc_now()
    tasks: list[asyncio.Task] = []
    last_send: float | None = None  # open loop: when the latest request started
    if rate and picker:
        count = budget.remaining
        if math.isfinite(duration):
            count = min(count, int(rate * duration)) if count is not None else int(rate * duration)
        rng = next(rngs)

        async def fire(_i: int, lag: float) -> None:
            nonlocal last_send
            last_send = time.monotonic()
            await send_one(
                client,
                picker.next(),
                rng=rng,
                origin=origin,
                chat=chat,
                results=results,
                schedule_lag_ms=round(lag * 1000, 2),
            )

        # No drain bound of its own: the drain below bounds every shape alike.
        tasks.append(asyncio.create_task(open_loop(fire, rate, count, stop_event=stop_event)))
    elif picker:
        for _ in range(concurrency or sum(t.concurrency for t in runnable)):
            coro = worker(
                client,
                picker.next,
                results,
                stop_event,
                rng=next(rngs),
                origin=origin,
                budget=budget,
                chat=chat,
            )
            tasks.append(asyncio.create_task(coro))
    else:
        rotation = None
        if sessions:
            sessions = dict(sessions)
            rotation = itertools.cycle(sessions.pop("clients", None) or [(client, "")])
        for target in runnable:
            for _ in range(target.concurrency):
                if sessions and target.workload == "llm":
                    coro = session_worker(
                        lambda: next(rotation),
                        target.name,
                        results,
                        stop_event,
                        think=think,
                        rng=next(rngs),
                        origin=origin,
                        budget=budget,
                        **sessions,
                    )
                else:
                    coro = worker(
                        client,
                        lambda target=target: target,
                        results,
                        stop_event,
                        rng=next(rngs),
                        origin=origin,
                        budget=budget,
                        chat=chat,
                    )
                tasks.append(asyncio.create_task(coro))

    snapshots: list[dict] = []
    poller = None
    polled = asyncio.Event()  # set when the drain is over, not when sending stops
    if fleet_client is not None:
        poller = asyncio.create_task(
            poll_fleet_status(fleet_client, polled, snapshots, origin=origin)
        )
    # Ends at the duration, at stop_event, or when every worker is done (the
    # request budget is spent).
    work = asyncio.gather(*tasks, return_exceptions=True)
    stopped = asyncio.create_task(stop_event.wait())
    while True:
        elapsed = time.monotonic() - origin
        if elapsed >= duration or stop_event.is_set() or work.done():
            break
        await asyncio.wait(
            {stopped, work},
            timeout=min(2.0, duration - elapsed),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if on_tick:
            on_tick(time.monotonic() - origin)
    load_s = min(time.monotonic() - origin, duration)
    stop_event.set()

    # Drain: each request in flight gets up to its own timeout; what is still
    # running then is cancelled, and records itself as a failure.
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
    await work
    await stopped
    polled.set()
    if poller is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await poller
    # Open loop: the arrivals' window, to the end of the last one's slot however
    # late it started.  Closed loop: the load phase.
    window_s = last_send - origin + 1 / rate if last_send is not None else load_s
    return LoadResult(
        snapshots=snapshots,
        elapsed_s=time.monotonic() - origin,
        started_at=started_at,
        ended_at=_utc_now(),
        seed=seed,
        window_s=window_s,
    )


async def poll_fleet_status(
    client: httpx.AsyncClient,
    stop_event: asyncio.Event,
    snapshots: list[dict],
    origin: float | None = None,
):
    """Poll /fleet/status every 3 s during a run: utilization, and which models
    each node has loaded (``model_events``).  Each snapshot gets ``t_ms``, when
    it arrived on the run clock (``origin``)."""
    origin = time.monotonic() if origin is None else origin
    while not stop_event.is_set():
        try:
            resp = await client.get("/fleet/status", timeout=5)
            if resp.status_code == 200:
                snap = resp.json()
                snap["t_ms"] = round((time.monotonic() - origin) * 1000, 1)
                snapshots.append(snap)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
            break
        except TimeoutError:
            pass


def model_events(snapshots: list[dict]) -> list[dict]:
    """Model loads and evictions seen between fleet polls:
    ``[{t_ms, node_id, model, event: "loaded" | "unloaded"}]``.

    The first poll of each node is its baseline.  ``t_ms`` is when the poll that
    saw the change arrived, so an event happened up to one poll interval (3 s)
    plus one node heartbeat (~5 s) earlier.  A node missing from a poll is not
    taken to have unloaded anything.
    """
    events: list[dict] = []
    previous: dict[str, set[str]] = {}
    for snap in snapshots:
        for node in snap.get("nodes", []):
            node_id = node["node_id"]
            loaded = {m["name"] for m in (node.get("ollama") or {}).get("models_loaded", [])}
            before = previous.get(node_id)
            if before is not None:
                for name, event in [
                    *((n, "loaded") for n in sorted(loaded - before)),
                    *((n, "unloaded") for n in sorted(before - loaded)),
                ]:
                    events.append(
                        {
                            "t_ms": snap.get("t_ms"),
                            "node_id": node_id,
                            "model": name,
                            "event": event,
                        }
                    )
            previous[node_id] = loaded
    return events


def _ttft_by_turn(results: list[RequestResult]) -> dict:
    """``{"ttft_p50_by_turn": {turn: ms}}`` for session results, else ``{}``."""
    by_turn: dict[int, list[float]] = {}
    for r in results:
        if r.turn >= 0 and r.ttft_ms > 0:
            by_turn.setdefault(r.turn, []).append(r.ttft_ms)
    if not by_turn:
        return {}
    return {
        "ttft_p50_by_turn": {
            str(t): round(percentile(v, 50), 1) for t, v in sorted(by_turn.items())
        }
    }


REPORT_PERCENTILES = (50, 90, 95, 99)


def _percentiles(values: list[float], name: str) -> dict:
    """``{name_p50_ms: ..., name_p90_ms: ...}``; ``None`` for no samples."""
    out = {}
    for p in REPORT_PERCENTILES:
        v = percentile(values, p)
        out[f"{name}_p{p}_ms"] = round(v, 1) if v is not None else None
    return out


def _first_and_after(results: list[RequestResult]) -> dict:
    """A model's first TTFT (a cold load unless it was warmed) and its TTFT
    after that: what a swap costs, per request, when models alternate."""
    ttfts = [r.ttft_ms for r in sorted(results, key=lambda r: r.started_ms) if r.ttft_ms > 0]
    if not ttfts:
        return {}
    after = ttfts[1:] or ttfts
    return {
        "first_ttft_ms": round(ttfts[0], 1),
        "ttft_p50_ms_after_first": round(percentile(after, 50), 1),
        "ttft_max_ms_after_first": round(max(after), 1),
    }


def request_record(r: RequestResult) -> dict:
    """One request as the report keeps it (``report["requests"]``)."""
    record = {
        "started_ms": r.started_ms,
        "model": r.model,
        "model_type": r.model_type,
        "node_id": r.node_id,
        "served_model": r.served_model,
        "request_id": r.request_id,
        "success": r.success,
        "error": r.error,
        "ttft_ms": round(r.ttft_ms, 1),
        "latency_ms": round(r.latency_ms, 1),
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "turn": r.turn,
    }
    if r.schedule_lag_ms is not None:
        record["schedule_lag_ms"] = r.schedule_lag_ms
    if r.finish_reason is not None:
        record["finish_reason"] = r.finish_reason
    return record


def _git_revision() -> tuple[str | None, bool | None]:
    """(short commit, dirty) of the checkout this code runs from; (None, None)
    for an installed package, or if git is missing.  Never raises.

    Only when the repository's ``src/fleet_manager`` is the package actually
    imported: a wheel installed in a virtualenv inside some other repository
    must not report that repository's commit.
    """
    package = Path(fleet_manager.__file__).resolve().parent

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=package, capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    if not top or Path(top).resolve() / "src" / "fleet_manager" != package:
        return None, None
    commit = git("rev-parse", "--short", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return commit, (bool(status) if status is not None else None)


def build_manifest(
    nodes_info: list[NodeInfo],
    run: dict | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> dict:
    """What produced a result: herd version and commit, when, the run settings
    (``run``: workload, seed, repetition, mode, ...), and per node the agent and
    Ollama versions, ``num_parallel`` and each loaded model's digest,
    quantization and size.  ``None`` wherever ``/fleet/status`` doesn't say."""
    commit, dirty = _git_revision()
    return {
        "herd_version": fleet_manager.__version__,
        "git_commit": commit,
        "git_dirty": dirty,
        "started_at": started_at,
        "ended_at": ended_at,
        **(run or {}),
        "nodes": [
            {
                "node_id": ni.node_id,
                "chip": ni.chip,
                "agent_version": ni.agent_version,
                "ollama_version": ni.ollama_version,
                "num_parallel": ni.num_parallel,
                "models": ni.model_meta,
            }
            for ni in nodes_info
        ],
    }


# Report keys the dashboard's benchmark table has no column for.
SNAPSHOT_KEYS = (
    "manifest",
    "model_events",
    "per_type_results",
    "cancelled_at_drain",
    "schedule_lag_ms",
)


def dashboard_record(report: dict) -> dict:
    """``report`` as the dashboard stores it: without the per-request records
    (megabytes on a long run, and never displayed) and with what has no column
    of its own (``SNAPSHOT_KEYS``) moved into ``fleet_snapshot``, the one
    free-form column a saved run keeps."""
    record = {k: v for k, v in report.items() if k != "requests" and k not in SNAPSHOT_KEYS}
    record["fleet_snapshot"] = {
        **report["fleet_snapshot"],
        **{k: report[k] for k in SNAPSHOT_KEYS if k in report},
    }
    return record


def build_report(
    results: list[RequestResult],
    duration: float,
    targets: list[ModelTarget],
    nodes_info: list[NodeInfo],
    fleet_snapshots: list[dict],
    run: dict | None = None,
    *,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> dict:
    """The run's report: aggregates, per model / type / node, peak utilization,
    every request (``requests``, in start order), model load/evict events and
    the run manifest (``build_manifest``; ``run`` holds the settings it records).

    ``duration`` is what rates are over (``LoadResult.window_s``).  Tokens,
    token rates and TTFT come from chat requests only (``CHAT_WORKLOADS``): an
    embedding is one "token" and its TTFT is its whole latency, so mixing them
    in inflated the one and pulled down the other.  ``per_type_results`` has
    each workload's own figures.

    Save it to the dashboard with ``dashboard_record``; write it whole for a
    published result.
    """
    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    chat = [r for r in success if r.model_type in CHAT_WORKLOADS]

    total_prompt = sum(r.prompt_tokens for r in chat)
    total_completion = sum(r.completion_tokens for r in chat)

    latencies = [r.latency_ms for r in success]
    ttfts = [r.ttft_ms for r in chat if r.ttft_ms > 0]
    lags = [r.schedule_lag_ms for r in results if r.schedule_lag_ms is not None]

    req_s = len(success) / duration if duration > 0 else 0
    tok_s = total_completion / duration if duration > 0 else 0

    per_type = []
    for kind in sorted({r.model_type for r in results}):
        ok = [r for r in success if r.model_type == kind]
        entry = {
            "model_type": kind,
            "requests": len(ok),
            "failures": sum(1 for r in failed if r.model_type == kind),
            "requests_per_sec": round(len(ok) / duration, 2) if duration > 0 else 0,
            **_percentiles([r.latency_ms for r in ok], "latency"),
        }
        if kind in CHAT_WORKLOADS:
            tokens = sum(r.completion_tokens for r in ok)
            entry["tok_s"] = round(tokens / duration, 1) if duration > 0 else 0
            entry.update(_percentiles([r.ttft_ms for r in ok if r.ttft_ms > 0], "ttft"))
        per_type.append(entry)

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
                    **_ttft_by_turn(mr),
                    **_first_and_after(mr),
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
            n_tok = sum(r.completion_tokens for r in nr if r.model_type in CHAT_WORKLOADS)
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
                "workload": t.workload,
            }
            for t in targets
        ],
    }

    report = {
        "run_id": f"bench-{int(time.time())}",
        "timestamp": time.time(),
        "duration_s": round(duration, 1),
        "total_requests": len(success),
        "total_failures": len(failed),
        # Of the failures: still in flight when the drain ran out.
        "cancelled_at_drain": sum(1 for r in failed if r.error == CANCELLED_AT_DRAIN),
        # Failures that were answers from another model than the one asked for
        # (not every failure that named the model it came from: a timeout
        # mid-stream from the right model is a failure, not a mismatch).
        "served_model_mismatches": sum(
            1 for r in failed if r.served_model and r.served_model not in _same_model(r.model)
        ),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "requests_per_sec": round(req_s, 2),
        "tokens_per_sec": round(tok_s, 1),
        **_percentiles(latencies, "latency"),
        **_percentiles(ttfts, "ttft"),
        "fleet_snapshot": fleet_snapshot,
        "per_model_results": per_model,
        "per_type_results": per_type,
        "per_node_results": per_node,
        "peak_utilization": peak_util,
        "manifest": build_manifest(nodes_info, run, started_at, ended_at),
        "model_events": model_events(fleet_snapshots),
        "requests": [request_record(r) for r in sorted(results, key=lambda r: r.started_ms)],
    }
    if lags:
        # Open loop: above a millisecond or two, the client fell behind its own
        # schedule and the arrival rate was lower than asked.
        report["schedule_lag_ms"] = {
            "p50": round(percentile(lags, 50), 2),
            "p99": round(percentile(lags, 99), 2),
            "max": round(max(lags), 2),
        }
    return report
