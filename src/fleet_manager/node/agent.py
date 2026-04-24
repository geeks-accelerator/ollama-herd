"""Node agent — runs on each device, collects metrics, sends heartbeats."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import socket
import subprocess
import sys

import httpx
import psutil

from fleet_manager.common.discovery import FleetServiceDiscoverer
from fleet_manager.common.ollama_client import OllamaClient
from fleet_manager.common.system_metrics import get_local_ip
from fleet_manager.models.config import NodeSettings
from fleet_manager.node.collector import collect_heartbeat
from fleet_manager.node.ollama_proxy import OllamaProxy

logger = logging.getLogger(__name__)

# How long to wait for Ollama to become healthy after starting it.
_OLLAMA_START_TIMEOUT = 30
_OLLAMA_POLL_INTERVAL = 1.0


class NodeAgent:
    def __init__(self, settings: NodeSettings):
        self.settings = settings
        self.node_id = settings.node_id or socket.gethostname().split(".")[0]
        self.ollama = OllamaClient(settings.ollama_host)
        self.router_url: str | None = settings.router_url or None
        self._http: httpx.AsyncClient | None = None
        self._running = False
        self._capacity_learner = None
        self._ollama_process: subprocess.Popen | None = None
        self._ollama_proxy: OllamaProxy | None = None
        self._image_server_task: asyncio.Task | None = None
        self._image_port: int = 0
        self._transcription_process: subprocess.Popen | None = None
        self._transcription_port: int = 0
        self._embedding_server_task: asyncio.Task | None = None
        self._embedding_port: int = 0
        self._telemetry_task: asyncio.Task | None = None
        self._platform_heartbeat_task: asyncio.Task | None = None

        # MLX backend (Phase 2 of docs/plans/mlx-backend-for-large-models.md).
        # When FLEET_NODE_MLX_ENABLED is true the node agent advertises models
        # from mlx_lm.server alongside Ollama's in each heartbeat.  Phase 3
        # will add auto-start subprocess lifecycle; for now we assume the
        # user has started mlx_lm.server manually at FLEET_NODE_MLX_URL.
        self.mlx = None
        if getattr(settings, "mlx_enabled", False):
            from fleet_manager.node.mlx_client import MlxClient

            self.mlx = MlxClient(settings.mlx_url)
            logger.info(f"MLX backend enabled on node — polling {settings.mlx_url}")
        self._mlx_process: subprocess.Popen | None = None
        self._mlx_supervisor = None  # Legacy single-server; kept for back-compat
        self._mlx_supervisor_set = None  # MlxSupervisorSet | None; new multi-server path

    async def _ensure_ollama(self) -> bool:
        """Check if Ollama is running; if not, try to start it.

        Returns True if Ollama is healthy, False if we couldn't start it.
        """
        if await self.ollama.is_healthy():
            logger.info(f"Ollama is healthy at {self.settings.ollama_host}")
            return True

        logger.warning(
            f"Ollama is not reachable at {self.settings.ollama_host}, attempting to start it..."
        )

        ollama_bin = shutil.which("ollama")
        if not ollama_bin:
            logger.error(
                "Ollama binary not found in PATH. "
                "Install Ollama from https://ollama.com and try again."
            )
            return False

        # Start 'ollama serve' as a detached background process.
        # Bind to 0.0.0.0 so the router can reach us over the LAN.
        import os

        env = os.environ.copy()
        env.setdefault("OLLAMA_HOST", "0.0.0.0:11434")
        try:
            # Detach child so it survives parent exit
            popen_kwargs: dict = {}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            self._ollama_process = subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                **popen_kwargs,
            )
            logger.info(
                f"Started ollama serve (pid={self._ollama_process.pid}, "
                f"OLLAMA_HOST={env['OLLAMA_HOST']})"
            )
        except Exception as e:
            logger.error(f"Failed to start Ollama: {e}")
            return False

        # Wait for Ollama to become healthy
        for _i in range(int(_OLLAMA_START_TIMEOUT / _OLLAMA_POLL_INTERVAL)):
            await asyncio.sleep(_OLLAMA_POLL_INTERVAL)
            if await self.ollama.is_healthy():
                logger.info("Ollama is now running and healthy")
                return True

        logger.error(
            f"Ollama did not become healthy within {_OLLAMA_START_TIMEOUT}s. "
            "Check if another instance is already running or if port is in use."
        )
        return False

    async def start(self):
        """Main entry point. Discovers router, registers, starts polling."""
        self._running = True
        self._http = httpx.AsyncClient(timeout=10.0)

        # Ensure Ollama is running before we begin
        if not await self._ensure_ollama():
            logger.error("Cannot proceed without Ollama. Exiting.")
            return

        # Initialize capacity learner if enabled
        if self.settings.enable_capacity_learning:
            mem = psutil.virtual_memory()
            total_gb = mem.total / (1024**3)
            from fleet_manager.node.capacity_learner import AdaptiveCapacityLearner

            self._capacity_learner = AdaptiveCapacityLearner(
                total_memory_gb=total_gb,
                data_dir=self.settings.data_dir,
                node_id=self.node_id,
            )
            logger.info(
                f"Capacity learning enabled: {total_gb:.0f}GB total memory, "
                f"{self._capacity_learner.days_observed} days observed"
            )

        # Discover router if not configured
        if not self.router_url:
            logger.info("Discovering Fleet Manager router via mDNS...")
            discoverer = FleetServiceDiscoverer()
            ip, port = await discoverer.discover()
            self.router_url = f"http://{ip}:{port}"
            logger.info(f"Found router at {self.router_url}")
        else:
            logger.info(f"Using configured router at {self.router_url}")

        # Auto-start LAN proxy if Ollama is only on localhost
        await self._ensure_lan_proxy()

        # Start image generation server if mflux is available
        await self._ensure_image_server()

        # Start transcription server if mlx-qwen3-asr is available
        await self._ensure_transcription_server()

        # Start vision embedding server if models are downloaded
        await self._ensure_embedding_server()

        # Phase 3: MLX subprocess auto-start (docs/plans/mlx-backend-for-large-models.md)
        await self._ensure_mlx_server()

        # Auto-connect to platform if token is configured and we're not
        # already connected — makes `--platform-token` / env var behave
        # the same as pasting the token in the dashboard's Settings tab.
        await self._ensure_platform_connection()

        # Start telemetry scheduler if opted in (requires platform connection)
        self._telemetry_task = await self._ensure_telemetry_scheduler()

        # Start platform heartbeat sender if connected — sends every 60s
        self._platform_heartbeat_task = await self._ensure_platform_heartbeat()

        # Install signal handlers for graceful drain
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._drain()))

        # Prime psutil cpu_percent (first call always returns 0.0)
        psutil.cpu_percent(interval=None)

        logger.info(f"Node agent started: {self.node_id}")
        logger.info(f"Ollama: {self.settings.ollama_host}")
        logger.info(f"Router: {self.router_url}")
        logger.info(f"Heartbeat interval: {self.settings.heartbeat_interval}s")

        self._ollama_failures = 0
        self._connection_failures = 0  # Since last successful heartbeat
        self._connection_failures_total = 0  # Since agent start
        heartbeat_count = 0

        while self._running:
            try:
                payload = await collect_heartbeat(
                    self.node_id,
                    self.ollama,
                    self.settings.ollama_host,
                    capacity_learner=self._capacity_learner,
                    mlx=self.mlx,
                    mlx_supervisor_set=self._mlx_supervisor_set,
                    mlx_bind_host=getattr(
                        self.settings, "mlx_bind_host", "127.0.0.1",
                    ),
                )
                if self._image_port:
                    payload.image_port = self._image_port
                if self._transcription_port:
                    payload.transcription_port = self._transcription_port
                if self._embedding_port:
                    payload.vision_embedding_port = self._embedding_port
                payload.connection_failures = self._connection_failures
                payload.connection_failures_total = self._connection_failures_total
                await self._send_heartbeat(payload)
                # Reset recent failures on successful heartbeat
                if self._connection_failures > 0:
                    logger.info(
                        f"Router reconnected after {self._connection_failures} "
                        f"failed attempts ({self._connection_failures_total} total)"
                    )
                self._connection_failures = 0
                self._ollama_failures = 0
                heartbeat_count += 1

                # Log summary every 60 heartbeats (~5 min at 5s interval)
                if heartbeat_count % 60 == 0:
                    models_loaded = len(payload.ollama.models_loaded) if payload.ollama else 0
                    models_available = len(payload.ollama.models_available) if payload.ollama else 0
                    logger.info(
                        f"Heartbeat #{heartbeat_count}: "
                        f"cpu={payload.cpu.utilization_pct:.0f}%, "
                        f"mem={payload.memory.used_gb:.1f}/{payload.memory.total_gb:.0f}GB "
                        f"({payload.memory.pressure.value}), "
                        f"models={models_loaded} loaded/{models_available} available"
                    )

            except httpx.ConnectError as e:
                self._connection_failures += 1
                self._connection_failures_total += 1
                logger.warning(f"Cannot reach router at {self.router_url}: {e}")
            except httpx.ConnectTimeout:
                self._connection_failures += 1
                self._connection_failures_total += 1
                logger.warning(
                    f"Connection to router timed out at {self.router_url} "
                    f"(is the firewall blocking port?)"
                )
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Router rejected heartbeat: HTTP {e.response.status_code} "
                    f"from {self.router_url}"
                )
            except Exception as e:
                logger.warning(f"Heartbeat cycle failed: {type(e).__name__}: {e}")

                # If Ollama keeps failing, try to restart it
                if not await self.ollama.is_healthy():
                    self._ollama_failures += 1
                    logger.warning(
                        f"Ollama health check failed "
                        f"(consecutive failures: {self._ollama_failures})"
                    )
                    if self._ollama_failures >= 3:
                        logger.warning("Ollama appears down, attempting restart...")
                        await self._ensure_ollama()
                        self._ollama_failures = 0

            await asyncio.sleep(self.settings.heartbeat_interval)

    async def _ensure_lan_proxy(self):
        """Start a LAN proxy if Ollama is only listening on localhost.

        This makes Ollama reachable by the router over the network without
        requiring manual OLLAMA_HOST configuration on each node.
        """
        from urllib.parse import urlparse

        parsed = urlparse(self.settings.ollama_host)
        ollama_port = parsed.port or 11434

        lan_ip = get_local_ip()
        if not lan_ip or lan_ip == "127.0.0.1":
            return  # Can't determine LAN IP

        # Test if Ollama is already reachable on the LAN IP
        try:
            async with httpx.AsyncClient() as test:
                resp = await test.get(f"http://{lan_ip}:{ollama_port}/", timeout=2)
                if resp.status_code == 200:
                    logger.info(
                        f"Ollama already reachable on LAN at "
                        f"{lan_ip}:{ollama_port}, no proxy needed"
                    )
                    return
        except Exception:
            pass  # Not reachable — need the proxy

        self._ollama_proxy = OllamaProxy(
            listen_host=lan_ip,
            listen_port=ollama_port,
            target_host=parsed.hostname or "127.0.0.1",
            target_port=ollama_port,
        )
        if await self._ollama_proxy.start():
            logger.info(
                f"LAN proxy active: router can reach Ollama at http://{lan_ip}:{ollama_port}"
            )
        else:
            self._ollama_proxy = None

    async def _ensure_image_server(self):
        """Start a lightweight HTTP server for image generation if mflux is available."""
        from fleet_manager.node.collector import _detect_image_models

        image_metrics = _detect_image_models()
        if not image_metrics or not image_metrics.models_available:
            logger.debug("No mflux binaries found, skipping image server")
            return

        from urllib.parse import urlparse

        parsed = urlparse(self.settings.ollama_host)
        ollama_port = parsed.port or 11434
        image_port = ollama_port + 2  # 11434 → 11436

        try:
            import uvicorn
            from fastapi import FastAPI

            from fleet_manager.node.image_server import router as image_router

            app = FastAPI(title="Herd Image Server")
            app.include_router(image_router)

            config = uvicorn.Config(
                app, host="0.0.0.0", port=image_port, log_level="warning"
            )
            server = uvicorn.Server(config)
            self._image_server_task = asyncio.create_task(server.serve())
            self._image_port = image_port

            models = ", ".join(m.name for m in image_metrics.models_available)
            logger.info(
                f"Image server started on 0.0.0.0:{image_port} "
                f"(models: {models})"
            )
        except Exception as e:
            logger.warning(f"Failed to start image server: {repr(e)}")
            self._image_port = 0

    async def _ensure_transcription_server(self):
        """Start mlx-qwen3-asr serve if available."""
        from fleet_manager.node.collector import _detect_transcription_models

        stt_metrics = _detect_transcription_models()
        if not stt_metrics or not stt_metrics.models_available:
            logger.debug("No mlx-qwen3-asr found, skipping transcription server")
            return

        from urllib.parse import urlparse

        parsed = urlparse(self.settings.ollama_host)
        ollama_port = parsed.port or 11434
        stt_port = ollama_port + 3  # 11434 → 11437

        binary = stt_metrics.models_available[0].binary
        try:
            import os

            env = os.environ.copy()
            env["MLX_ASR_API_KEY"] = "herd-internal"
            popen_kwargs: dict = {}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            self._transcription_process = subprocess.Popen(
                [binary, "serve", "--port", str(stt_port), "--host", "0.0.0.0"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                **popen_kwargs,
            )
            self._transcription_port = stt_port

            models = ", ".join(m.name for m in stt_metrics.models_available)
            logger.info(
                f"Transcription server started on 0.0.0.0:{stt_port} "
                f"(models: {models}, pid={self._transcription_process.pid})"
            )
        except Exception as e:
            logger.warning(f"Failed to start transcription server: {repr(e)}")
            self._transcription_port = 0

    async def _ensure_embedding_server(self):
        """Start a vision embedding server if models are downloaded."""
        from fleet_manager.node.collector import _detect_vision_embedding_models

        embedding_metrics = _detect_vision_embedding_models()
        if not embedding_metrics or not embedding_metrics.models_available:
            logger.debug("No vision embedding models found, skipping embedding server")
            return

        from urllib.parse import urlparse

        parsed = urlparse(self.settings.ollama_host)
        ollama_port = parsed.port or 11434
        embedding_port = ollama_port + 4  # 11434 → 11438

        try:
            import uvicorn
            from fastapi import FastAPI

            from fleet_manager.node.embedding_server import router as embed_router

            app = FastAPI(title="Herd Vision Embedding Server")
            app.include_router(embed_router)

            config = uvicorn.Config(
                app, host="0.0.0.0", port=embedding_port, log_level="warning"
            )
            server = uvicorn.Server(config)
            self._embedding_server_task = asyncio.create_task(server.serve())
            self._embedding_port = embedding_port

            models = ", ".join(m.name for m in embedding_metrics.models_available)
            logger.info(
                f"Vision embedding server started on 0.0.0.0:{embedding_port} "
                f"(models: {models})"
            )
        except Exception as e:
            logger.warning(f"Failed to start embedding server: {repr(e)}")
            self._embedding_port = 0

    async def _ensure_mlx_server(self):
        """Spawn mlx_lm.server process(es) if auto-start is configured.

        Supports two config paths:
          1. Multi-server (new): FLEET_NODE_MLX_SERVERS='[{...},{...}]'
             → spawns one subprocess per entry via MlxSupervisorSet
          2. Single-server (legacy): FLEET_NODE_MLX_AUTO_START_MODEL=...
             → synthesized as a single-entry list, same path

        No-op when MLX isn't enabled or no servers are configured.
        One failing subprocess doesn't block the others — the set
        reports per-server status in the heartbeat so the dashboard
        surfaces which URLs are healthy.
        """
        if not getattr(self.settings, "mlx_enabled", False):
            return

        specs = self._parse_mlx_specs()
        if not specs:
            # Honor the old warning for folks with MLX_AUTO_START=true but
            # no configured model — they hit this path via the empty
            # synthesized list.
            if getattr(self.settings, "mlx_auto_start", False):
                logger.warning(
                    "FLEET_NODE_MLX_AUTO_START=true but no MLX servers "
                    "configured (neither FLEET_NODE_MLX_SERVERS nor "
                    "FLEET_NODE_MLX_AUTO_START_MODEL set).  Skipping MLX."
                )
            return

        from fleet_manager.node.mlx_supervisor import MlxSupervisorSet

        bind_host = getattr(self.settings, "mlx_bind_host", "127.0.0.1")
        headroom = float(getattr(self.settings, "mlx_memory_headroom_gb", 10.0))
        self._mlx_supervisor_set = MlxSupervisorSet(
            specs,
            bind_host=bind_host,
            memory_headroom_gb=headroom,
        )
        logger.info(
            f"Starting {len(specs)} MLX server(s): "
            f"{', '.join(f'{s.model}@{s.port}' for s in specs)} "
            f"(bind_host={bind_host}, headroom={headroom}GB)"
        )
        results = await self._mlx_supervisor_set.start_all()
        ok_count = sum(1 for v in results.values() if v)
        if ok_count == 0:
            logger.warning(
                "All MLX servers failed to start — continuing with Ollama only. "
                "Check ~/.fleet-manager/logs/mlx-server-<port>.log for details "
                "(one log file per configured port)."
            )
        elif ok_count < len(specs):
            logger.warning(
                f"MLX supervisor set: {ok_count}/{len(specs)} servers started. "
                f"See heartbeat mlx_servers field for per-server status."
            )
        else:
            logger.info(f"All {ok_count} MLX servers healthy")

    def _parse_mlx_specs(self):
        """Translate settings into list[MlxServerSpec].

        Priority: FLEET_NODE_MLX_SERVERS (JSON list) wins; falls back to
        the legacy single-server fields.  Both empty ⇒ empty list.
        """
        import json as _json

        from fleet_manager.node.mlx_supervisor import MlxServerSpec

        raw = (getattr(self.settings, "mlx_servers", "") or "").strip()
        if raw:
            try:
                data = _json.loads(raw)
            except _json.JSONDecodeError as exc:
                logger.error(
                    f"FLEET_NODE_MLX_SERVERS is not valid JSON ({exc}); "
                    "ignoring.  Check for trailing commas or mismatched quotes."
                )
                return []
            if not isinstance(data, list):
                logger.error(
                    f"FLEET_NODE_MLX_SERVERS must be a JSON array, got "
                    f"{type(data).__name__}; ignoring."
                )
                return []
            specs = []
            for entry in data:
                if not isinstance(entry, dict):
                    logger.error(
                        f"FLEET_NODE_MLX_SERVERS entry must be a JSON "
                        f"object, got {type(entry).__name__}; skipping: {entry!r}"
                    )
                    continue
                try:
                    specs.append(MlxServerSpec.from_dict(entry))
                except ValueError as exc:
                    logger.error(f"FLEET_NODE_MLX_SERVERS bad entry: {exc}")
            return specs

        # Legacy single-server fallback
        if (getattr(self.settings, "mlx_auto_start", False)
                and getattr(self.settings, "mlx_auto_start_model", "")):
            from urllib.parse import urlparse
            parsed = urlparse(self.settings.mlx_url)
            port = parsed.port or 11440
            return [MlxServerSpec(
                model=self.settings.mlx_auto_start_model,
                port=port,
                kv_bits=self.settings.mlx_kv_bits,
                prompt_cache_size=self.settings.mlx_prompt_cache_size,
                prompt_cache_bytes=self.settings.mlx_prompt_cache_bytes,
                draft_model=getattr(self.settings, "mlx_draft_model", ""),
                num_draft_tokens=getattr(self.settings, "mlx_num_draft_tokens", 4),
            )]
        return []

    async def _ensure_platform_connection(self):
        """Auto-connect to the platform if a token is configured.

        Respects existing connection state: if already connected with a
        matching token, no-op.  If token changed, reconnect.  If no
        token configured, check for saved state from a prior Connect.
        """
        from fleet_manager.node import platform_connection

        # Read token from settings (env var or CLI flag)
        token_secret = getattr(self.settings, "platform_token", None)
        token = token_secret.get_secret_value() if token_secret else ""
        platform_url = (
            getattr(self.settings, "platform_url", None)
            or platform_connection.DEFAULT_PLATFORM_URL
        )

        existing = platform_connection.load_state()

        # Case 1: no env/CLI token, no saved state → nothing to do
        if not token and not existing:
            logger.debug("Platform: no token configured, skipping auto-connect")
            return

        # Case 2: no env/CLI token, but saved state exists → stay connected
        if not token and existing:
            logger.info(
                f"Platform: using saved connection to {existing.platform_url} "
                f"(node_id={existing.node_id})"
            )
            return

        # Case 3: env/CLI token matches saved state → no-op
        if existing and existing.operator_token == token:
            logger.info(
                f"Platform: already connected to {existing.platform_url} "
                f"(node_id={existing.node_id})"
            )
            return

        # Case 4: token configured but not connected (or token changed) → connect
        logger.info(
            f"Platform: connecting to {platform_url} via configured token…"
        )
        try:
            state = await platform_connection.connect_to_platform(
                token=token,
                platform_url=platform_url,
                node_name=self.settings.node_id or None,
            )
            logger.info(
                f"Platform: connected as "
                f"{state.user_display_name or state.user_email} "
                f"(node_id={state.node_id})"
            )
        except platform_connection.InvalidTokenError as exc:
            logger.warning(f"Platform: invalid token — {exc}")
        except platform_connection.PlatformUnreachableError as exc:
            logger.warning(f"Platform: unreachable — {exc}. "
                           f"Will retry on next restart.")
        except Exception as exc:
            logger.warning(f"Platform: connection failed — {exc}")

    async def _ensure_telemetry_scheduler(self) -> asyncio.Task | None:
        """Start the daily telemetry scheduler if opted in.

        Requires:
          - telemetry_local_summary == True on NodeSettings
          - Platform connection exists (check saved state)

        Returns the task so it can be cancelled on shutdown.
        """
        telemetry_on = getattr(self.settings, "telemetry_local_summary", False)
        if not telemetry_on:
            logger.debug(
                "telemetry: disabled "
                "(set FLEET_NODE_TELEMETRY_LOCAL_SUMMARY=true to enable)"
            )
            return None

        from fleet_manager.node import platform_connection

        if not platform_connection.is_connected():
            logger.warning(
                "telemetry: enabled but not connected to platform — "
                "scheduler will NOT start. Connect via the dashboard "
                "Settings tab or restart with --platform-token set."
            )
            return None

        include_tags = getattr(self.settings, "telemetry_include_tags", False)
        logger.info(
            f"telemetry: scheduler starting "
            f"(include_tags={include_tags}, retention: 90 days rolling)"
        )

        from fleet_manager.node.telemetry_scheduler import run_scheduler

        return asyncio.create_task(
            run_scheduler(include_tags=include_tags),
            name="telemetry-scheduler",
        )

    async def _ensure_platform_heartbeat(self) -> asyncio.Task | None:
        """Start the platform heartbeat sender if connected.

        Unlike telemetry (opt-in), platform heartbeats are automatic
        when the node is connected — they power the platform's
        Nodes-detail dashboard with live utilization and loaded models.
        """
        from fleet_manager.node import platform_connection

        if not platform_connection.is_connected():
            logger.debug(
                "platform heartbeat: not connected to platform, skipping"
            )
            return None

        from fleet_manager.node.platform_heartbeat import run_scheduler

        return asyncio.create_task(
            run_scheduler(),
            name="platform-heartbeat",
        )

    async def _send_heartbeat(self, payload):
        resp = await self._http.post(
            f"{self.router_url}/heartbeat",
            json=payload.model_dump(),
        )
        if resp.status_code != 200:
            logger.warning(f"Heartbeat response: HTTP {resp.status_code} body={resp.text[:200]}")
            resp.raise_for_status()

        # Process commands from the router
        try:
            data = resp.json()
            for cmd in data.get("commands", []):
                await self._handle_command(cmd)
        except Exception:
            pass  # Don't fail heartbeat on command processing errors

    async def _handle_command(self, cmd: dict):
        """Process a command from the router (e.g., restart Ollama)."""
        cmd_type = cmd.get("type", "")
        if cmd_type == "restart_ollama":
            env = cmd.get("env", {})
            reason = cmd.get("reason", "router command")
            logger.info(f"Command: restart Ollama (reason: {reason}, env: {env})")
            await self._restart_ollama(env)
        else:
            logger.warning(f"Unknown command type: {cmd_type}")

    async def _restart_ollama(self, env_overrides: dict[str, str] | None = None):
        """Restart the local Ollama process with optional env var changes."""
        import os
        import signal

        logger.info("Restarting Ollama...")

        # Kill current Ollama process
        if hasattr(self, "_ollama_process") and self._ollama_process:
            try:
                self._ollama_process.send_signal(signal.SIGTERM)
                self._ollama_process.wait(timeout=15)
            except Exception as e:
                logger.warning(f"Error stopping Ollama: {e}")
                import contextlib
                with contextlib.suppress(Exception):
                    self._ollama_process.kill()
            self._ollama_process = None

        # Apply env overrides
        if env_overrides:
            for key, value in env_overrides.items():
                os.environ[key] = value
                logger.info(f"Set env: {key}={value}")

        # Wait a moment for port to free
        await asyncio.sleep(2)

        # Restart via the existing startup mechanism
        await self._ensure_ollama()
        logger.info("Ollama restarted successfully")

    async def _drain(self):
        """Graceful shutdown: signal the router, then stop."""
        logger.info("Drain signal received, shutting down...")
        if self._capacity_learner:
            self._capacity_learner.save()
        if self._http and self.router_url:
            try:
                await self._http.post(
                    f"{self.router_url}/heartbeat",
                    json={"node_id": self.node_id, "draining": True},
                )
            except Exception as e:
                logger.warning(f"Failed to send drain signal to router: {e}")
        self._running = False
        if self._image_server_task:
            self._image_server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._image_server_task
        if self._telemetry_task:
            self._telemetry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._telemetry_task
        if self._platform_heartbeat_task:
            self._platform_heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._platform_heartbeat_task
        if self._transcription_process:
            self._transcription_process.terminate()
            with contextlib.suppress(Exception):
                self._transcription_process.wait(timeout=5)
        if self._ollama_proxy:
            await self._ollama_proxy.stop()
        await self.ollama.close()
        if self.mlx is not None:
            await self.mlx.close()
        # MLX subprocess(es) — graceful shutdown if we spawned them.
        # Supervisor set is the new multi-server path; the single-supervisor
        # path is kept for back-compat with any direct constructors.
        if self._mlx_supervisor_set is not None:
            with contextlib.suppress(Exception):
                await self._mlx_supervisor_set.stop_all()
        if self._mlx_supervisor is not None:
            with contextlib.suppress(Exception):
                await self._mlx_supervisor.stop()
        # Legacy fallback path (no supervisor, raw Popen) — defensive
        if self._mlx_process is not None:
            with contextlib.suppress(Exception):
                self._mlx_process.terminate()
                self._mlx_process.wait(timeout=5)
        if self._http:
            await self._http.aclose()
