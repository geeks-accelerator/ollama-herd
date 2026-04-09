"""
Server-side benchmark runner with smart mode and progress tracking.

Supports two modes:
- default: benchmark currently loaded models
- smart: use model recommender to fill available memory, then benchmark everything
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from fleet_manager.server.benchmark_engine import (
    ModelTarget,
    NodeInfo,
    RequestResult,
    build_report,
    discover_embed_models,
    discover_fleet,
    discover_image_models,
    embed_worker,
    image_worker,
    poll_fleet_status,
    warmup,
    worker,
)

logger = logging.getLogger(__name__)


class BenchmarkRunner:
    """Manages server-side benchmark execution with progress tracking."""

    def __init__(self, base_url: str = "http://localhost:11435"):
        self._base_url = base_url
        self._status: str = "idle"  # idle|pulling|warming_up|running|complete|error|cancelled
        self._phase: str = ""
        self._start_time: float = 0
        self._duration: float = 0
        self._results: list[RequestResult] = []
        self._targets: list[ModelTarget] = []
        self._nodes_info: list[NodeInfo] = []
        self._models_pulled: list[str] = []
        self._pull_progress: dict = {}
        self._report: dict | None = None
        self._error: str | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._status in ("pulling", "warming_up", "running")

    def get_progress(self) -> dict:
        """Return current benchmark status and progress."""
        elapsed = time.time() - self._start_time if self._start_time else 0
        ok = sum(1 for r in self._results if r.success)
        fail = sum(1 for r in self._results if not r.success)
        tok = sum(r.completion_tokens for r in self._results if r.success)
        tok_s = tok / elapsed if elapsed > 1 else 0

        progress = {
            "status": self._status,
            "phase": self._phase,
            "elapsed": round(elapsed, 1),
            "duration": self._duration,
            "requests_completed": ok,
            "requests_failed": fail,
            "tok_per_sec": round(tok_s, 1),
            "models": [t.name for t in self._targets],
            "models_pulled": self._models_pulled,
            "pull_progress": self._pull_progress,
            "error": self._error,
        }
        if self._report:
            progress["run_id"] = self._report.get("run_id")
        return progress

    async def start(
        self,
        mode: str = "default",
        duration: float = 300,
        model_types: list[str] | None = None,
        registry=None,
        trace_store=None,
        streaming_proxy=None,
        scorer=None,
    ):
        """Launch benchmark in a background task. Returns run_id.

        model_types: list of types to benchmark. Default: ["llm"].
        Options: "llm", "embed", "image".
        """
        if self.is_running:
            raise RuntimeError("Benchmark already running")

        # Reset state
        self._status = "idle"
        self._phase = ""
        self._results = []
        self._targets = []
        self._nodes_info = []
        self._models_pulled = []
        self._pull_progress = {}
        self._report = None
        self._error = None
        self._stop_event = asyncio.Event()
        self._duration = duration
        self._start_time = time.time()
        self._model_types = model_types or ["llm"]

        self._task = asyncio.create_task(
            self._run(
                mode=mode,
                duration=duration,
                model_types=self._model_types,
                registry=registry,
                trace_store=trace_store,
                streaming_proxy=streaming_proxy,
                scorer=scorer,
            )
        )
        return f"bench-{int(self._start_time)}"

    def cancel(self):
        """Cancel a running benchmark."""
        if self.is_running:
            self._stop_event.set()
            self._status = "cancelled"
            self._phase = "Cancelled by user"

    async def _run(
        self,
        mode: str,
        duration: float,
        model_types: list[str],
        registry,
        trace_store,
        streaming_proxy,
        scorer,
    ):
        """Main benchmark execution flow."""
        try:
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                # Smart mode: pull recommended models first
                if mode == "smart":
                    await self._smart_pull(
                        client, registry, streaming_proxy, scorer
                    )
                    if self._stop_event.is_set():
                        return

                # Wait for pulled models to appear in /api/ps (hot in memory)
                if mode == "smart" and self._models_pulled:
                    self._phase = "Verifying models are loaded..."
                    loaded_set: set[str] = set()
                    expected = set(self._models_pulled)
                    for _attempt in range(12):  # Up to 60s (12 x 5s)
                        try:
                            ps_resp = await client.get("/api/ps", timeout=5)
                            if ps_resp.status_code == 200:
                                ps_models = ps_resp.json().get("models", [])
                                loaded_set = {m["name"] for m in ps_models}
                                missing = expected - loaded_set
                                if not missing:
                                    logger.info(
                                        "Smart benchmark: all pulled models confirmed loaded"
                                    )
                                    break
                                self._phase = (
                                    f"Waiting for {len(missing)} model(s) to load: "
                                    f"{', '.join(sorted(missing))}"
                                )
                        except Exception:
                            pass
                        await asyncio.sleep(5)

                # Discover fleet (includes any newly loaded models)
                self._status = "warming_up"
                self._phase = "Discovering fleet..."

                all_targets: list[ModelTarget] = []
                nodes_info: list[NodeInfo] = []

                # Discover LLM models
                if "llm" in model_types:
                    targets, nodes_info = await discover_fleet(client)
                    all_targets.extend(targets)

                # Discover embedding models
                if "embed" in model_types:
                    embed_targets = await discover_embed_models(client)
                    all_targets.extend(embed_targets)

                # Discover image models
                if "image" in model_types:
                    image_targets = await discover_image_models(client)
                    all_targets.extend(image_targets)

                if not all_targets:
                    self._status = "error"
                    self._error = "No models found for selected types"
                    return

                # Get nodes_info if we didn't discover LLM (need it for report)
                if not nodes_info:
                    try:
                        resp = await client.get("/fleet/status")
                        if resp.status_code == 200:
                            for node in resp.json().get("nodes", []):
                                if node.get("status") == "online":
                                    nodes_info.append(NodeInfo(
                                        node_id=node["node_id"],
                                        cores=node.get("cpu", {}).get("cores_physical", 0),
                                        memory_total_gb=node.get("memory", {}).get("total_gb", 0),
                                        memory_used_gb=node.get("memory", {}).get("used_gb", 0),
                                        memory_available_gb=node.get("memory", {}).get(
                                            "available_gb", 0
                                        ),
                                    ))
                    except Exception:
                        pass

                self._targets = all_targets
                self._nodes_info = nodes_info

                # Warmup LLM models only (embed/image don't need warmup)
                llm_targets = [t for t in all_targets if t.model_type == "llm"]
                other_targets = [t for t in all_targets if t.model_type != "llm"]

                if llm_targets:
                    self._phase = "Warming up LLM models..."
                    llm_targets = await warmup(
                        client, llm_targets,
                        log_fn=lambda msg: logger.info(f"Benchmark warmup: {msg}"),
                    )

                all_targets = llm_targets + other_targets
                if not all_targets:
                    self._status = "error"
                    self._error = "All models failed warmup"
                    return

                self._targets = all_targets

                if self._stop_event.is_set():
                    return

                # Run benchmark
                type_summary = ", ".join(sorted(set(t.model_type for t in all_targets)))
                self._status = "running"
                self._phase = f"Running {duration:.0f}s benchmark ({type_summary})..."
                self._start_time = time.time()  # Reset for accurate elapsed
                self._results = []

                fleet_snapshots: list[dict] = []

                # Create worker tasks — use appropriate worker per model type
                tasks = []
                for target in all_targets:
                    for _ in range(target.concurrency):
                        if target.model_type == "embed":
                            tasks.append(asyncio.create_task(
                                embed_worker(client, target.name, self._results, self._stop_event)
                            ))
                        elif target.model_type == "image":
                            tasks.append(asyncio.create_task(
                                image_worker(client, target.name, self._results, self._stop_event)
                            ))
                        else:
                            tasks.append(asyncio.create_task(
                                worker(client, target.name, self._results, self._stop_event)
                            ))

                # Fleet status poller
                poller_task = asyncio.create_task(
                    poll_fleet_status(client, self._stop_event, fleet_snapshots)
                )

                # Wait for duration
                import contextlib

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=duration
                    )

                self._stop_event.set()

                # Wait for in-flight to finish
                self._phase = "Draining in-flight requests..."
                done, pending = await asyncio.wait(tasks, timeout=30)
                for t in pending:
                    t.cancel()
                poller_task.cancel()

                # Build report
                actual_duration = time.time() - self._start_time
                report_duration = min(actual_duration, duration * 2)
                self._report = build_report(
                    self._results, report_duration, targets, nodes_info, fleet_snapshots
                )
                self._report["mode"] = mode
                if self._models_pulled:
                    self._report["smart_pulled_models"] = self._models_pulled

                # Save results
                if trace_store:
                    await trace_store.save_benchmark_run(self._report)

                self._status = "complete"
                self._phase = "Benchmark complete"
                logger.info(
                    f"Benchmark complete: {self._report['total_requests']} requests, "
                    f"{self._report['tokens_per_sec']} tok/s"
                )

        except asyncio.CancelledError:
            self._status = "cancelled"
            self._phase = "Cancelled"
        except Exception as e:
            self._status = "error"
            self._error = str(e)
            self._phase = f"Error: {e}"
            logger.error(f"Benchmark failed: {e}", exc_info=True)

    async def _smart_pull(self, client, registry, streaming_proxy, scorer):
        """Use model recommender to fill available memory with optimal models.

        Strategy (in priority order):
        1. Models already on disk but not loaded — just need warmup, instant
        2. Models from catalog matching uncovered categories — need download
        Cap individual models at 50GB, total at 50% of available memory.
        """
        self._status = "pulling"
        self._phase = "Analyzing fleet for smart model selection..."

        try:
            from fleet_manager.server.model_knowledge import (
                ModelCategory,
                best_for_category,
                classify_model,
                is_image_model,
                lookup_model,
            )

            # Get current fleet state
            resp = await client.get("/fleet/status")
            resp.raise_for_status()
            fleet_data = resp.json()

            nodes = fleet_data.get("nodes", [])
            if not nodes:
                self._phase = "No nodes online, skipping smart pull"
                return

            MAX_MODEL_SIZE_GB = 50
            MAX_PULL_PCT = 0.5

            # (model, node_id, ram_gb, on_disk) — on_disk=True means no download needed
            models_to_load: list[tuple[str, str, float, bool]] = []

            for node in nodes:
                if node.get("status") != "online":
                    continue
                node_id = node["node_id"]
                mem = node.get("memory", {})
                available_gb = mem.get("available_gb", 0)
                ollama = node.get("ollama") or {}
                loaded_names = {m["name"] for m in ollama.get("models_loaded", [])}
                on_disk_names = set(ollama.get("models_available", []))
                already_planned = {m for m, _, _, _ in models_to_load}

                pullable_gb = min(
                    available_gb - 20,
                    available_gb * MAX_PULL_PCT,
                )
                if pullable_gb < 4:
                    continue

                # Categories we want to cover
                target_categories = [
                    ModelCategory.GENERAL,
                    ModelCategory.CODING,
                    ModelCategory.REASONING,
                    ModelCategory.FAST_CHAT,
                ]

                # Track which categories are already covered by loaded LLM models
                # (skip embedding models and image models — they can't serve chat)
                EMBEDDING_PATTERNS = ("embed", "nomic", "bge", "e5-")
                covered = set()
                for name in loaded_names:
                    lower = name.lower()
                    if any(p in lower for p in EMBEDDING_PATTERNS):
                        continue
                    if is_image_model(name):
                        continue
                    cat = classify_model(name)
                    if cat:
                        covered.add(cat)

                # Phase 1: Prefer LLM models already on disk but not loaded
                for name in sorted(on_disk_names - loaded_names):
                    if pullable_gb < 2:
                        break
                    if name in already_planned:
                        continue
                    lower = name.lower()
                    if any(p in lower for p in EMBEDDING_PATTERNS):
                        continue
                    if is_image_model(name):
                        continue
                    spec = lookup_model(name)
                    cat = classify_model(name)
                    if cat in covered:
                        continue
                    ram = spec.ram_gb if spec else 8.0  # estimate if unknown
                    if ram > pullable_gb or ram > MAX_MODEL_SIZE_GB:
                        continue
                    models_to_load.append((name, node_id, ram, True))
                    pullable_gb -= ram
                    already_planned.add(name)
                    if cat:
                        covered.add(cat)

                # Phase 2: Fill remaining categories from catalog (requires download)
                for cat in target_categories:
                    if cat in covered or pullable_gb < 4:
                        continue
                    cap = min(pullable_gb, MAX_MODEL_SIZE_GB)
                    spec = best_for_category(cat, cap)
                    if spec is None:
                        continue
                    name = spec.ollama_name
                    if name in loaded_names or name in already_planned:
                        continue
                    models_to_load.append((name, node_id, spec.ram_gb, False))
                    pullable_gb -= spec.ram_gb
                    already_planned.add(name)
                    covered.add(cat)

            if not models_to_load:
                self._phase = "No additional models fit in available memory"
                logger.info("Smart benchmark: no additional models to pull")
                return

            # Load/pull models
            on_disk = [m for m in models_to_load if m[3]]
            to_download = [m for m in models_to_load if not m[3]]
            total = len(models_to_load)

            disk_names = ", ".join(m[0] for m in on_disk)
            dl_names = ", ".join(m[0] for m in to_download)
            logger.info(
                f"Smart benchmark: {len(on_disk)} on-disk ({disk_names}), "
                f"{len(to_download)} to download ({dl_names})"
            )

            for i, (model, node_id, ram_gb, is_on_disk) in enumerate(models_to_load, 1):
                if self._stop_event.is_set():
                    return

                action = "Loading" if is_on_disk else "Pulling"
                self._phase = f"{action} {model} ({i}/{total})..."
                self._pull_progress = {
                    "model": model,
                    "node_id": node_id,
                    "current": i,
                    "total": total,
                    "ram_gb": ram_gb,
                    "on_disk": is_on_disk,
                }
                logger.info(
                    f"Smart benchmark: {action.lower()} {model} "
                    f"{'from disk' if is_on_disk else 'from registry'} "
                    f"to {node_id} ({ram_gb}GB)"
                )

                def _on_pull_progress(pct, completed, total_bytes, status):
                    """Update pull progress for the UI."""
                    self._pull_progress["pct"] = pct
                    self._pull_progress["status"] = status
                    if total_bytes > 0:
                        self._pull_progress["completed_gb"] = round(completed / 1e9, 1)
                        self._pull_progress["total_gb"] = round(total_bytes / 1e9, 1)

                try:
                    if is_on_disk:
                        # Model is on disk — send a non-streaming request to force
                        # Ollama to load it into GPU memory. Block until complete.
                        warmup_resp = await client.post(
                            "/api/chat",
                            json={
                                "model": model,
                                "messages": [{"role": "user", "content": "hi"}],
                                "stream": False,
                                "options": {"num_predict": 1},
                            },
                            timeout=180,
                        )
                        success = warmup_resp.status_code == 200
                        if success:
                            logger.info(
                                f"Smart benchmark: {model} loaded into GPU memory"
                            )
                    elif streaming_proxy:
                        success = await streaming_proxy.pull_model(
                            node_id, model, progress_cb=_on_pull_progress,
                        )
                    else:
                        pull_resp = await client.post(
                            "/api/pull",
                            json={"name": model, "node_id": node_id, "stream": False},
                            timeout=600,
                        )
                        success = pull_resp.status_code == 200

                    if success:
                        self._models_pulled.append(model)
                        logger.info(f"Smart benchmark: {action.lower()} {model} succeeded")
                    else:
                        logger.warning(f"Smart benchmark: {action.lower()} {model} failed")
                except Exception as e:
                    logger.warning(f"Smart benchmark: error with {model}: {e}")

            self._pull_progress = {}
            loaded_count = len(self._models_pulled)
            self._phase = f"Loaded {loaded_count}/{total} models"

        except Exception as e:
            logger.warning(f"Smart pull failed, continuing with loaded models: {e}")
            self._phase = f"Smart pull error: {e}, continuing with loaded models"
