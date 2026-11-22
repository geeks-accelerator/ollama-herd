#!/usr/bin/env python3
"""Fake herd-nodes: N heartbeat loops against one router, for control-plane scale.

Each fake node POSTs a heartbeat built by ``fleet_manager.testing.heartbeats``
(the builder the test suite uses) to ``{router}/heartbeat`` every
``--interval`` seconds with ``--jitter``, starting at a random offset so the
nodes are not in lockstep.  Its ``ollama_host`` is a ``--ollama`` URL (assigned
round robin), normally ``scripts/mock_ollama.py``, so requests the router sends
to a fake node are actually served.

A loopback ``--ollama`` URL works whether or not the router is on this machine:
the router rewrites it to ``localhost:<port>`` for a node on its own machine and
to ``<address the heartbeat came from>:<port>`` otherwise
(``NodeRegistry._build_ollama_url``).  Each node reports ``lan_ip`` from
``get_local_ip()`` like the real agent; a made-up address would send the router
to a host that does not exist.

Models come from ``--model name:size_gb`` (repeatable) or, without it, from the
first mock's ``/api/tags``.  Every model a node holds is reported loaded;
``--models-per-node K`` gives each node a rotating K of them.  With
``--follow-ps`` a node instead reports what its own backend says before each
heartbeat: its ``/api/tags`` as available and its ``/api/ps`` as loaded, so a
mock with ``--max-loaded`` drives real placement and eviction.  ``--gpu-gb G``
makes every node a discrete-GPU node with G GB of card memory, its loaded
models on the card.  Its use is measured, as nvidia-smi measures it: with
``--follow-ps`` a mock's model mid-load (``/_mock/state``) counts too, the
memory a real card shows in use before ``/api/ps`` lists the model.

Per-node memory state, each ``NODE=VALUE`` and repeatable, where NODE is a
node's index (``0``), its node_id (``fake-000``) or ``*`` for every node (a
named node wins over ``*``):

* ``--pressure NODE=LEVEL``: the memory pressure the node reports, ``normal``,
  ``warn`` or ``critical`` (default ``normal``).
* ``--working-set NODE=GB``: the node's Metal working set
  (``memory.gpu_working_set_gb``), the cap Ollama fits models into inside
  unified RAM.  Unset, it stays 0 ("not reported"), as from a non-Apple node.

Churn and failure:

* ``--churn-every S``: every S seconds ``--churn-count`` nodes send the drain
  message the real agent sends on shutdown (``{"node_id": ..., "draining":
  true}``), stay away ``--rejoin-after`` seconds, then heartbeat again.
* ``--kill N``: after ``--kill-after`` seconds, N nodes stop heartbeating with
  no drain (a crash or a pulled cable).  ``/fleet/status`` is then polled and
  the summary records when each was seen degraded and offline.

On exit (``--duration`` elapsed, Ctrl-C or SIGTERM) every node still up drains,
as real agents do, and a JSON summary is printed (and written to ``--output``):
heartbeats sent, failures by reason, drains, rejoins, heartbeat POST latency
p50/p90/p99 (``common.stats.percentile``) and the offline-detection times.

    uv run python scripts/fake_fleet.py --router http://127.0.0.1:11535 --nodes 8 \\
        --ollama http://127.0.0.1:11500 --duration 120 --output fleet.json
    uv run python scripts/fake_fleet.py --router http://127.0.0.1:11535 --nodes 64 \\
        --model llama3.2:3b:2.0 --model qwen3:8b:5.2 --models-per-node 1 \\
        --churn-every 20 --kill 4 --kill-after 30 --duration 120
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import signal
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

import httpx

from fleet_manager import __version__
from fleet_manager.common.stats import percentile
from fleet_manager.common.system_metrics import get_local_ip
from fleet_manager.models.node import MemoryPressure
from fleet_manager.server.hardware_lookup import resolve_bandwidth
from fleet_manager.testing.heartbeats import make_heartbeat


@dataclass
class FleetConfig:
    nodes: int = 8
    ollama: list[str] = field(default_factory=lambda: ["http://127.0.0.1:11500"])
    models: list[tuple[str, float]] = field(default_factory=list)
    models_per_node: int = 0
    memory_gb: float = 64.0
    chip: str = ""
    cores: int = 12
    num_parallel: int = 0
    interval: float = 5.0
    jitter: float = 0.1
    duration: float = 0.0
    churn_every: float = 0.0
    churn_count: int = 1
    rejoin_after: float = 10.0
    kill: int = 0
    kill_after: float = 10.0
    status_every: float = 10.0
    prefix: str = "fake"
    lan_ip: str = ""
    seed: int | None = None
    follow_ps: bool = False
    gpu_gb: float = 0.0
    pressure: dict[str, str] = field(default_factory=dict)  # NODE -> level
    working_set: dict[str, float] = field(default_factory=dict)  # NODE -> GB


@dataclass
class FakeNode:
    node_id: str
    ollama_host: str
    models: list[tuple[str, float]]
    loaded: list[tuple[str, float]] | None = None  # None: every model in ``models``
    loading_gb: float = 0.0  # a model the backend is loading (``--follow-ps``)
    pressure: MemoryPressure = MemoryPressure.NORMAL
    working_set_gb: float = 0.0  # 0: not reported
    up: asyncio.Event = field(default_factory=asyncio.Event)
    killed_at: float | None = None
    last_ok_at: float | None = None
    degraded_at: float | None = None
    offline_at: float | None = None

    @property
    def killed(self) -> bool:
        return self.killed_at is not None


def parse_model(spec: str) -> tuple[str, float]:
    """``name:size_gb``, e.g. ``llama3.2:3b:2.0``."""
    name, sep, size = spec.rpartition(":")
    try:
        if sep:
            return name, float(size)
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"expected NAME:SIZE_GB, got {spec!r}")


def parse_node_value(spec: str) -> tuple[str, str]:
    """``NODE=VALUE``: NODE is an index, a node_id or ``*``."""
    node, sep, value = spec.partition("=")
    if not sep or not node or not value:
        raise argparse.ArgumentTypeError(f"expected NODE=VALUE, got {spec!r}")
    return node, value


def parse_pressure(spec: str) -> tuple[str, str]:
    node, level = parse_node_value(spec)
    try:
        return node, MemoryPressure(level.lower()).value
    except ValueError:
        levels = ", ".join(p.value for p in MemoryPressure)
        raise argparse.ArgumentTypeError(
            f"pressure must be one of {levels}, got {level!r}"
        ) from None


def parse_working_set(spec: str) -> tuple[str, float]:
    node, gb = parse_node_value(spec)
    try:
        value = float(gb)
    except ValueError:
        value = -1.0
    if value < 0:
        raise argparse.ArgumentTypeError(f"working set must be a number of GB, got {gb!r}")
    return node, value


def for_node(per_node: dict, index: int, node_id: str, default):
    """The value ``per_node`` gives this node: by node_id, by index, then ``*``."""
    for key in (node_id, str(index), "*"):
        if key in per_node:
            return per_node[key]
    return default


async def discover_models(client: httpx.AsyncClient, ollama_url: str) -> list[tuple[str, float]]:
    """The models a mock (or a real Ollama) lists, with on-disk sizes in GB."""
    resp = await client.get(f"{ollama_url.rstrip('/')}/api/tags")
    resp.raise_for_status()
    return [
        (m.get("model") or m["name"], (m.get("size") or 0) / 1e9)
        for m in resp.json().get("models", [])
    ]


def build_nodes(config: FleetConfig) -> list[FakeNode]:
    models = config.models
    per_node = config.models_per_node or len(models)
    nodes = []
    for i in range(config.nodes):
        held = [models[(i * per_node + j) % len(models)] for j in range(min(per_node, len(models)))]
        node_id = f"{config.prefix}-{i:03d}"
        nodes.append(
            FakeNode(
                node_id=node_id,
                ollama_host=config.ollama[i % len(config.ollama)],
                models=held,
                pressure=MemoryPressure(for_node(config.pressure, i, node_id, "normal")),
                working_set_gb=for_node(config.working_set, i, node_id, 0.0),
            )
        )
    return nodes


def heartbeat_for(node: FakeNode, config: FleetConfig, lan_ip: str) -> dict:
    loaded = node.models if node.loaded is None else node.loaded
    in_ram = 0.0 if config.gpu_gb > 0 else sum(size for _, size in loaded)
    body = make_heartbeat(
        node_id=node.node_id,
        memory_total=config.memory_gb,
        memory_used=min(config.memory_gb * 0.95, 8.0 + in_ram),
        cores=config.cores,
        cpu_pct=10.0,
        pressure=node.pressure,
        loaded_models=list(loaded),
        available_models=[name for name, _ in node.models],
        lan_ip=lan_ip,
        ollama_host=node.ollama_host,
        agent_version=__version__,
        chip=config.chip,
        memory_bandwidth_gbps=resolve_bandwidth(config.chip) or 0.0,
        num_parallel=config.num_parallel,
    ).model_dump()
    body["ollama"]["models_available_sizes"] = dict(node.models)
    if node.working_set_gb > 0:
        body["memory"]["gpu_working_set_gb"] = node.working_set_gb
    if config.gpu_gb > 0:
        used = sum(size for _, size in loaded) + node.loading_gb
        body["memory"].update(
            gpu_total_gb=config.gpu_gb, gpu_used_gb=min(used, config.gpu_gb), gpu_source="detected"
        )
        for m in body["ollama"]["models_loaded"]:
            m["size_vram_gb"] = m["size_gb"]
    return body


async def follow_backend(client: httpx.AsyncClient, node: FakeNode) -> None:
    """Report what the node's backend holds: ``/api/tags`` available, ``/api/ps``
    loaded.  Left as it was when the backend does not answer."""
    base = node.ollama_host.rstrip("/")
    try:
        tags = await discover_models(client, base)
        resp = await client.get(f"{base}/api/ps")
        resp.raise_for_status()
    except httpx.HTTPError:
        return
    node.models = tags
    node.loaded = [
        (m.get("model") or m["name"], (m.get("size") or 0) / 1e9)
        for m in resp.json().get("models", [])
    ]
    with contextlib.suppress(httpx.HTTPError, ValueError):  # a real Ollama has no /_mock
        state = await client.get(f"{base}/_mock/state")
        loading = state.json().get("loading") if state.status_code == 200 else None
        node.loading_gb = dict(tags).get(loading, 0.0) if loading else 0.0


class Fleet:
    def __init__(self, config: FleetConfig, client: httpx.AsyncClient, out=sys.stderr):
        self.config = config
        self.client = client
        self.out = out
        self.rng = random.Random(config.seed)
        self.lan_ip = config.lan_ip or get_local_ip(warn=False)
        self.nodes = build_nodes(config)
        self.start = time.monotonic()
        self.latencies_ms: list[float] = []
        self.window_ms: list[float] = []
        self.failures: Counter[str] = Counter()
        self.sent = 0
        self.drains = 0
        self.rejoins = 0
        self._churn_next = 0

    def now(self) -> float:
        return time.monotonic() - self.start

    async def post(self, body: dict, what: str) -> float | None:
        """POST to ``/heartbeat``; the latency in ms, or ``None`` on failure."""
        t0 = time.perf_counter()
        try:
            resp = await self.client.post("/heartbeat", json=body)
        except httpx.HTTPError as e:
            self.failures[f"{what} {type(e).__name__}"] += 1
            return None
        if resp.status_code != 200:
            self.failures[f"{what} HTTP {resp.status_code}"] += 1
            return None
        return (time.perf_counter() - t0) * 1000

    async def node_loop(self, node: FakeNode) -> None:
        cfg = self.config
        await asyncio.sleep(self.rng.uniform(0, cfg.interval))
        while not node.killed:
            await node.up.wait()
            if node.killed:
                return
            if cfg.follow_ps:
                await follow_backend(self.client, node)
            ms = await self.post(heartbeat_for(node, cfg, self.lan_ip), "heartbeat")
            self.sent += 1  # counted once answered, so one cancelled at exit is not
            if ms is not None:
                node.last_ok_at = self.now()
                self.latencies_ms.append(ms)
                self.window_ms.append(ms)
            await asyncio.sleep(cfg.interval * self.rng.uniform(1 - cfg.jitter, 1 + cfg.jitter))

    async def drain(self, node: FakeNode) -> None:
        node.up.clear()
        self.drains += 1
        await self.post({"node_id": node.node_id, "draining": True}, "drain")

    async def rejoin(self, node: FakeNode) -> None:
        await asyncio.sleep(self.config.rejoin_after)
        if not node.killed:
            self.rejoins += 1
            node.up.set()

    async def churn_loop(self, tasks: set[asyncio.Task]) -> None:
        while True:
            await asyncio.sleep(self.config.churn_every)
            for _ in range(self.config.churn_count):
                node = self._next_churn_candidate()
                if node is None:
                    break
                await self.drain(node)
                task = asyncio.create_task(self.rejoin(node))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    def _next_churn_candidate(self) -> FakeNode | None:
        for _ in range(len(self.nodes)):
            node = self.nodes[self._churn_next % len(self.nodes)]
            self._churn_next += 1
            if node.up.is_set() and not node.killed:
                return node
        return None

    async def kill_then_watch(self) -> None:
        await asyncio.sleep(self.config.kill_after)
        # Only nodes that are up: a drained node is already offline, which
        # would read as instant detection.
        victims = [n for n in reversed(self.nodes) if n.up.is_set() and not n.killed]
        victims = victims[: self.config.kill]
        for node in victims:
            node.killed_at = self.now()
        poll = min(1.0, self.config.interval / 5)
        while any(n.offline_at is None for n in victims):
            await asyncio.sleep(poll)
            try:
                resp = await self.client.get("/fleet/status")
                status = {n["node_id"]: n["status"] for n in resp.json()["nodes"]}
            except (httpx.HTTPError, ValueError, KeyError) as e:
                self.failures[f"fleet/status {type(e).__name__}"] += 1
                continue
            t = self.now()
            for node in victims:
                seen = status.get(node.node_id)
                if seen in ("degraded", "offline") and node.degraded_at is None:
                    node.degraded_at = t
                if seen == "offline" and node.offline_at is None:
                    node.offline_at = t

    async def status_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.status_every)
            print(self.status_line(), file=self.out, flush=True)
            self.window_ms = []

    def heartbeat_failures(self) -> int:
        return sum(n for reason, n in self.failures.items() if reason.startswith("heartbeat "))

    def status_line(self) -> str:
        up = sum(1 for n in self.nodes if n.up.is_set() and not n.killed)
        killed = sum(1 for n in self.nodes if n.killed)
        p50 = percentile(self.window_ms, 50)
        p99 = percentile(self.window_ms, 99)
        lat = f"p50 {p50:.1f} ms p99 {p99:.1f} ms" if p50 is not None else "no posts"
        return (
            f"[{self.now():6.0f}s] up {up}/{len(self.nodes)} killed {killed} | "
            f"heartbeats {self.sent} failed {self.heartbeat_failures()} | {lat}"
        )

    async def run(self, stop: asyncio.Event) -> dict:
        cfg = self.config
        tasks: set[asyncio.Task] = set()
        for node in self.nodes:
            node.up.set()
            tasks.add(asyncio.create_task(self.node_loop(node)))
        if cfg.churn_every > 0:
            tasks.add(asyncio.create_task(self.churn_loop(tasks)))
        if cfg.kill > 0:
            tasks.add(asyncio.create_task(self.kill_then_watch()))
        if cfg.status_every > 0:
            tasks.add(asyncio.create_task(self.status_loop()))
        try:
            timeout = cfg.duration if cfg.duration > 0 else None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout)
        finally:
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Real agents drain on shutdown; do the same so the router is left clean.
            await asyncio.gather(
                *(self.drain(n) for n in self.nodes if n.up.is_set() and not n.killed)
            )
        return self.summary()

    def summary(self) -> dict:
        lat = self.latencies_ms
        cfg = asdict(self.config)
        cfg["models"] = [f"{name}:{size:g}" for name, size in self.config.models]

        def ms(pct: float) -> float | None:
            value = percentile(lat, pct)
            return round(value, 2) if value is not None else None

        def since(t: float | None, origin: float | None) -> float | None:
            return round(t - origin, 2) if t is not None and origin is not None else None

        return {
            "config": cfg,
            "elapsed_s": round(self.now(), 2),
            "heartbeats_sent": self.sent,
            "heartbeat_failures": self.heartbeat_failures(),
            "failure_reasons": dict(self.failures),
            "drains_sent": self.drains,
            "rejoins": self.rejoins,
            "post_ms": {
                "n": len(lat),
                "p50": ms(50),
                "p90": ms(90),
                "p99": ms(99),
                "max": round(max(lat), 2) if lat else None,
            },
            "killed": [
                {
                    "node_id": n.node_id,
                    "killed_at_s": round(n.killed_at, 2),
                    "degraded_after_kill_s": since(n.degraded_at, n.killed_at),
                    "offline_after_kill_s": since(n.offline_at, n.killed_at),
                    "offline_after_last_heartbeat_s": since(n.offline_at, n.last_ok_at),
                }
                for n in self.nodes
                if n.killed_at is not None
            ],
        }


def parse_args(argv: list[str] | None = None) -> tuple[str, FleetConfig, str | None]:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--router", required=True, help="Router base URL")
    p.add_argument("--nodes", type=int, default=8)
    p.add_argument(
        "--ollama",
        action="append",
        metavar="URL",
        help="Backend URL for the nodes, repeatable, round robin (default http://127.0.0.1:11500)",
    )
    p.add_argument(
        "--model",
        action="append",
        type=parse_model,
        dest="models",
        metavar="NAME:SIZE_GB",
        help="A model every node holds; repeatable (default: the first --ollama's /api/tags)",
    )
    p.add_argument("--models-per-node", type=int, default=0, help="Rotating subset (0 = all)")
    p.add_argument("--memory-gb", type=float, default=64.0)
    p.add_argument(
        "--follow-ps",
        action="store_true",
        help="Report each node's backend /api/tags as available and /api/ps as loaded",
    )
    p.add_argument("--gpu-gb", type=float, default=0.0, help="Card memory per node (0 = none)")
    p.add_argument(
        "--pressure",
        action="append",
        type=parse_pressure,
        default=[],
        metavar="NODE=LEVEL",
        help="Memory pressure a node reports (normal/warn/critical); NODE = index, id or *",
    )
    p.add_argument(
        "--working-set",
        action="append",
        type=parse_working_set,
        default=[],
        metavar="NODE=GB",
        help="Metal working set a node reports (gpu_working_set_gb); NODE = index, id or *",
    )
    p.add_argument("--chip", default="", help='e.g. "Apple M3 Ultra" (sets bandwidth)')
    p.add_argument("--cores", type=int, default=12)
    p.add_argument("--num-parallel", type=int, default=0, help="Reported OLLAMA_NUM_PARALLEL")
    p.add_argument("--interval", type=float, default=5.0, help="Heartbeat period, seconds")
    p.add_argument("--jitter", type=float, default=0.1, help="Fraction of the period, +/-")
    p.add_argument("--duration", type=float, default=0.0, help="Seconds (0 = until Ctrl-C)")
    p.add_argument("--churn-every", type=float, default=0.0, help="Seconds between drains")
    p.add_argument("--churn-count", type=int, default=1, help="Nodes drained each time")
    p.add_argument(
        "--rejoin-after", type=float, default=10.0, help="Seconds a drained node is away"
    )
    p.add_argument("--kill", type=int, default=0, help="Nodes that stop heartbeating, no drain")
    p.add_argument("--kill-after", type=float, default=10.0, help="Seconds before the kill")
    p.add_argument("--status-every", type=float, default=10.0, help="Status line period (0 = off)")
    p.add_argument("--prefix", default="fake", help="node_id prefix")
    p.add_argument("--lan-ip", default="", help="Reported lan_ip (default: this machine's)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--output", help="Write the JSON summary here too")
    a = p.parse_args(argv)
    if a.nodes < 1:
        p.error("--nodes must be at least 1")
    if a.kill > a.nodes:
        p.error("--kill cannot exceed --nodes")
    config = FleetConfig(
        nodes=a.nodes,
        ollama=a.ollama or ["http://127.0.0.1:11500"],
        models=a.models or [],
        models_per_node=a.models_per_node,
        memory_gb=a.memory_gb,
        chip=a.chip,
        cores=a.cores,
        num_parallel=a.num_parallel,
        interval=a.interval,
        jitter=a.jitter,
        duration=a.duration,
        churn_every=a.churn_every,
        churn_count=a.churn_count,
        rejoin_after=a.rejoin_after,
        kill=a.kill,
        kill_after=a.kill_after,
        status_every=a.status_every,
        prefix=a.prefix,
        lan_ip=a.lan_ip,
        seed=a.seed,
        follow_ps=a.follow_ps,
        gpu_gb=a.gpu_gb,
        pressure=dict(a.pressure),
        working_set=dict(a.working_set),
    )
    ids = {f"{config.prefix}-{i:03d}" for i in range(config.nodes)}
    known = {"*"} | ids | {str(i) for i in range(config.nodes)}
    for flag, per_node in (("--pressure", config.pressure), ("--working-set", config.working_set)):
        unknown = sorted(set(per_node) - known)
        if unknown:
            p.error(f"{flag} names no such node: {', '.join(unknown)}")
    return a.router.rstrip("/"), config, a.output


async def amain(argv: list[str] | None = None) -> dict:
    router, config, output = parse_args(argv)
    limits = httpx.Limits(max_connections=max(10, config.nodes), max_keepalive_connections=None)
    async with httpx.AsyncClient(base_url=router, timeout=10.0, limits=limits) as client:
        if not config.models:
            config.models = await discover_models(client, config.ollama[0])
            if not config.models:
                raise SystemExit(f"{config.ollama[0]}/api/tags lists no models; pass --model")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, stop.set)
        summary = await Fleet(config, client).run(stop)
    text = json.dumps(summary, indent=2)
    print(text)
    if output:
        with open(output, "w") as f:
            f.write(text + "\n")
    return summary


if __name__ == "__main__":
    asyncio.run(amain())
