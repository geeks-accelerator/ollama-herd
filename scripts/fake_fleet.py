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
``--models-per-node K`` gives each node a rotating K of them.

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


@dataclass
class FakeNode:
    node_id: str
    ollama_host: str
    models: list[tuple[str, float]]
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
        nodes.append(
            FakeNode(
                node_id=f"{config.prefix}-{i:03d}",
                ollama_host=config.ollama[i % len(config.ollama)],
                models=held,
            )
        )
    return nodes


def heartbeat_for(node: FakeNode, config: FleetConfig, lan_ip: str) -> dict:
    loaded = sum(size for _, size in node.models)
    return make_heartbeat(
        node_id=node.node_id,
        memory_total=config.memory_gb,
        memory_used=min(config.memory_gb * 0.95, 8.0 + loaded),
        cores=config.cores,
        cpu_pct=10.0,
        loaded_models=list(node.models),
        available_models=[name for name, _ in node.models],
        lan_ip=lan_ip,
        ollama_host=node.ollama_host,
        agent_version=__version__,
        chip=config.chip,
        memory_bandwidth_gbps=resolve_bandwidth(config.chip) or 0.0,
        num_parallel=config.num_parallel,
    ).model_dump()


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
    )
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
