"""Assembles heartbeat payloads from system metrics and Ollama state."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from fleet_manager import __version__
from fleet_manager.common.ollama_client import OllamaClient
from fleet_manager.common.system_metrics import (
    get_cpu_metrics,
    get_disk_metrics,
    get_local_ip,
    get_memory_metrics,
)
from fleet_manager.models.node import (
    CapacityMetrics,
    HeartbeatPayload,
    ImageMetrics,
    ImageModel,
    OllamaMetrics,
    TranscriptionMetrics,
    TranscriptionModel,
    VisionEmbeddingMetrics,
    VisionEmbeddingModel,
)

logger = logging.getLogger(__name__)


def _make_lan_reachable_url(ollama_host: str, lan_ip: str) -> str:
    """Replace localhost in ollama_host with the LAN IP so the router can reach us."""
    parsed = urlparse(ollama_host)
    if parsed.hostname in ("localhost", "127.0.0.1", "::1") and lan_ip and lan_ip != "127.0.0.1":
        port = parsed.port or 11434
        return f"http://{lan_ip}:{port}"
    return ollama_host


# Known mflux binaries and their model names
_MFLUX_BINARIES = [
    ("mflux-generate-z-image-turbo", "z-image-turbo"),
    ("mflux-generate", "flux-dev"),
]

# DiffusionKit binary and models it provides
_DIFFUSIONKIT_BINARY = "diffusionkit-cli"
_DIFFUSIONKIT_MODELS = [
    "sd3-medium",
    "sd3.5-large",
]


def _which_extended(binary: str) -> str | None:
    """Find a binary, checking common tool install paths beyond $PATH.

    uv tool, pipx, and Homebrew install binaries in locations that may not
    be in PATH when the node agent starts (e.g., via launchd, cron, or
    Windows services).
    """
    found = shutil.which(binary)
    if found:
        return found
    # Check common tool binary locations (platform-aware)
    import sys

    extra_dirs = [
        Path.home() / ".local" / "bin",           # uv tool, pipx (Unix/Linux/macOS)
    ]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        if local:
            extra_dirs.append(Path(local) / "Programs" / "Python" / "Scripts")
        if appdata:
            extra_dirs.append(Path(appdata) / "Python" / "Scripts")
        extra_dirs.append(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links")
    else:
        extra_dirs.append(Path("/opt/homebrew/bin"))   # Homebrew (Apple Silicon)
        extra_dirs.append(Path("/usr/local/bin"))      # Homebrew (Intel), system
    for d in extra_dirs:
        candidate = d / binary
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def _detect_image_models() -> ImageMetrics | None:
    """Detect available image generation models on this system (mflux + DiffusionKit)."""
    models: list[ImageModel] = []

    # Detect mflux models
    for binary, name in _MFLUX_BINARIES:
        path = _which_extended(binary)
        if path:
            models.append(ImageModel(name=name, binary=path))

    # Detect DiffusionKit models
    if _which_extended(_DIFFUSIONKIT_BINARY):
        for name in _DIFFUSIONKIT_MODELS:
            dk_path = _which_extended(_DIFFUSIONKIT_BINARY)
            models.append(ImageModel(name=name, binary=dk_path or _DIFFUSIONKIT_BINARY))

    if not models:
        return None

    # Check if any image generation process is currently running
    generating = False
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            proc_name = proc.info.get("name", "") or ""
            if "mflux" in proc_name.lower() or "diffusionkit" in proc_name.lower():
                generating = True
                break
    except Exception:
        pass

    return ImageMetrics(models_available=models, generating=generating)


def _detect_transcription_models() -> TranscriptionMetrics | None:
    """Detect available speech-to-text models on this system."""
    models: list[TranscriptionModel] = []
    if shutil.which("mlx-qwen3-asr"):
        models.append(TranscriptionModel(name="qwen3-asr", binary="mlx-qwen3-asr"))
    if not models:
        return None

    # Check if a transcription is currently running
    transcribing = False
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            proc_name = proc.info.get("name", "") or ""
            if "qwen3-asr" in proc_name.lower() or "mlx-qwen3-asr" in proc_name.lower():
                transcribing = True
                break
    except Exception:
        pass

    return TranscriptionMetrics(models_available=models, transcribing=transcribing)


def _detect_vision_embedding_models() -> VisionEmbeddingMetrics | None:
    """Detect available vision embedding models (DINOv2, SigLIP, CLIP)."""
    from fleet_manager.node.embedding_models import (
        VISION_EMBEDDING_MODELS,
        is_model_downloaded,
    )

    models: list[VisionEmbeddingModel] = []
    for name, spec in VISION_EMBEDDING_MODELS.items():
        if not is_model_downloaded(name):
            continue
        models.append(
            VisionEmbeddingModel(
                name=name,
                runtime=spec["runtime"],
                dimensions=spec["dimensions"],
            )
        )

    if not models:
        return None
    return VisionEmbeddingMetrics(models_available=models, processing=False)


async def collect_heartbeat(
    node_id: str,
    ollama: OllamaClient,
    ollama_host: str = "http://localhost:11434",
    capacity_learner=None,
    mlx=None,  # type: ignore[no-untyped-def]
) -> HeartbeatPayload:
    """Assemble a complete heartbeat payload from local system state.

    ``mlx`` is an optional :class:`fleet_manager.node.mlx_client.MlxClient`.
    When provided, models it advertises are merged into ``models_available``
    with an ``mlx:`` prefix so the router can route requests through the MLX
    backend.  See ``docs/plans/mlx-backend-for-large-models.md``.
    """
    cpu = get_cpu_metrics()
    memory = get_memory_metrics()
    disk = get_disk_metrics()

    try:
        models_loaded = await ollama.get_running_models()
        models_available = await ollama.get_available_models()
        requests_active = sum(m.requests_active for m in models_loaded)
        logger.debug(
            f"Ollama state: {len(models_loaded)} loaded, "
            f"{len(models_available)} available, "
            f"{requests_active} active requests"
        )
    except Exception as e:
        logger.warning(f"Ollama not reachable at {ollama_host}: {type(e).__name__}: {e}")
        models_loaded = []
        models_available = []
        requests_active = 0

    # MLX backend — if enabled, merge its advertised models into the heartbeat's
    # `models_available` with an `mlx:` prefix.  The server-side Anthropic route
    # detects that prefix and forwards to `MlxProxy` instead of Ollama.
    if mlx is not None:
        try:
            mlx_models = await mlx.get_available_models()
            if mlx_models:
                from fleet_manager.node.mlx_client import (
                    get_running_mlx_model,
                    prefix_mlx,
                )

                # CRITICAL: mlx_lm.server's /v1/models returns every model it
                # can *find* on disk (HF cache scan), not what's actually
                # loaded into memory.  Only the model passed as ``--model`` to
                # the running process is resident; everything else is just
                # discoverable.  Filter so the dashboard reports truth.
                running = get_running_mlx_model()
                if running:
                    # Canonicalize so equivalent ids (full HF, snapshot path,
                    # short id) all collapse to one entry.
                    def _canon(s: str) -> str:
                        if "/" in s and "models--" in s:
                            for p in Path(s).parts:
                                if p.startswith("models--"):
                                    return p.removeprefix("models--").replace("--", "/", 1)
                        return s

                    canon_running = _canon(running)
                    found_running = any(_canon(m) == canon_running for m in mlx_models)
                    cleaned = [running] if found_running else []
                else:
                    cleaned = []
                prefixed = [prefix_mlx(m) for m in cleaned]
                models_available = list(models_available) + prefixed
                logger.debug(
                    f"MLX state: +{len(prefixed)} loaded model(s) "
                    f"({', '.join(prefixed) if prefixed else 'none — server not running'})"
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"MLX polling failed: {type(e).__name__}: {e}")

    # Run capacity learner observation if enabled
    capacity = None
    if capacity_learner is not None:
        cap_info = capacity_learner.observe(
            cpu.utilization_pct,
            memory.used_gb / memory.total_gb * 100 if memory.total_gb > 0 else 0,
        )
        capacity = CapacityMetrics(
            mode=cap_info.mode.value,
            ceiling_gb=cap_info.ceiling_gb,
            availability_score=cap_info.availability_score,
            reason=cap_info.reason,
            override_active=cap_info.override_active,
            learning_confidence=cap_info.learning_confidence,
            days_observed=cap_info.days_observed,
        )

    lan_ip = get_local_ip()
    image = _detect_image_models()
    transcription = _detect_transcription_models()
    vision_embedding = _detect_vision_embedding_models()

    return HeartbeatPayload(
        node_id=node_id,
        cpu=cpu,
        memory=memory,
        disk=disk,
        ollama=OllamaMetrics(
            models_loaded=models_loaded,
            models_available=models_available,
            requests_active=requests_active,
        ),
        ollama_host=_make_lan_reachable_url(ollama_host, lan_ip),
        lan_ip=lan_ip,
        capacity=capacity,
        agent_version=__version__,
        image=image,
        transcription=transcription,
        vision_embedding=vision_embedding,
    )
