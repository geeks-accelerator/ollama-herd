"""Data models for node state, heartbeats, and hardware profiles."""

from __future__ import annotations

import time
from enum import StrEnum

from pydantic import BaseModel, Field


class MemoryPressure(StrEnum):
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"


class ThermalState(StrEnum):
    """Platform-aware thermal signal.

    Populating this on every platform is genuinely hard:
      - Apple Silicon doesn't expose cpu_thermal_state (Intel-only sysctl)
        and powermetrics requires sudo. ``pmset -g therm`` reports past
        events, not current state.
      - Linux exposes ``psutil.sensors_temperatures()`` with real numbers.
      - Windows support via psutil is driver-dependent; often unavailable.

    So the detection is best-effort with explicit "unknown" when we can't
    tell. The dashboard renders the CPU>=95% proxy when state is "unknown"
    so the warning overlay still fires on genuinely hot machines even
    when we lack a real thermal sensor.
    """

    NOMINAL = "nominal"   # thermal signal available and within range
    WARNING = "warning"   # thermal signal available and concerning
    UNKNOWN = "unknown"   # no reliable thermal signal for this platform


class NodeStatus(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class CpuMetrics(BaseModel):
    cores_physical: int
    utilization_pct: float


class MemoryMetrics(BaseModel):
    total_gb: float
    used_gb: float
    available_gb: float
    pressure: MemoryPressure = MemoryPressure.NORMAL
    wired_gb: float = 0.0
    compressed_gb: float = 0.0


class ThermalMetrics(BaseModel):
    """Thermal state reported by the node agent. See ``ThermalState``."""

    state: ThermalState = ThermalState.UNKNOWN
    # When ``state == NOMINAL`` or ``WARNING`` on Linux we can include the
    # peak package/core temp that drove the classification. None elsewhere.
    temperature_c: float | None = None
    # Brief human-readable hint: what driver / source produced this reading.
    # Used by the dashboard tooltip + operator diagnostics. Empty when
    # state == UNKNOWN.
    source: str = ""


class DiskMetrics(BaseModel):
    total_gb: float
    used_gb: float
    available_gb: float


class LoadedModel(BaseModel):
    name: str
    size_gb: float
    requests_active: int = 0
    parameter_size: str = ""  # e.g. "30.5B"
    quantization: str = ""  # e.g. "Q4_K_M"
    context_length: int = 0  # allocated context window


class OllamaMetrics(BaseModel):
    models_loaded: list[LoadedModel] = Field(default_factory=list)
    models_available: list[str] = Field(default_factory=list)
    requests_active: int = 0


class CapacityMetrics(BaseModel):
    """Adaptive capacity state from the node's capacity learner."""

    mode: str = "full"
    ceiling_gb: float = 0.0
    availability_score: float = 1.0
    reason: str = ""
    override_active: bool = False
    learning_confidence: float = 0.0
    days_observed: int = 0


class ImageModel(BaseModel):
    """An image generation model available on a node."""

    name: str  # e.g. "z-image-turbo", "flux-dev"
    binary: str  # e.g. "mflux-generate-z-image-turbo"


class ImageMetrics(BaseModel):
    """Image generation capabilities reported by a node."""

    models_available: list[ImageModel] = Field(default_factory=list)
    generating: bool = False


class TranscriptionModel(BaseModel):
    """A speech-to-text model available on a node."""

    name: str  # e.g. "qwen3-asr-0.6b", "qwen3-asr-1.7b"
    binary: str  # e.g. "mlx-qwen3-asr"


class TranscriptionMetrics(BaseModel):
    """Speech-to-text capabilities reported by a node."""

    models_available: list[TranscriptionModel] = Field(default_factory=list)
    transcribing: bool = False


class VisionEmbeddingModel(BaseModel):
    """A vision embedding model available on a node (CLIP, DINOv2, SigLIP)."""

    name: str       # e.g. "dinov2-vit-s14", "clip-vit-b32"
    runtime: str    # "mlx" or "onnx"
    dimensions: int  # 384, 768, or 512


class VisionEmbeddingMetrics(BaseModel):
    """Vision embedding capabilities reported by a node."""

    models_available: list[VisionEmbeddingModel] = Field(default_factory=list)
    processing: bool = False


class MlxServerInfo(BaseModel):
    """One mlx_lm.server subprocess on a node, reported in the heartbeat.

    The router uses this to build a ``{model_id: node_url+port}`` map for
    the MLX proxy so multi-server multi-node routing Just Works.

    ``status`` values:
      - "healthy"         — /v1/models returned 200 at the last check
      - "starting"        — spawned, waiting for first healthy response
      - "unhealthy"       — running but not responding (restart in progress)
      - "memory_blocked"  — skipped at start due to psutil memory gate
      - "stopped"         — terminated or never started
    """

    port: int
    model: str
    status: str
    status_reason: str = ""
    kv_bits: int = 0
    model_size_gb: float = 0.0
    last_ok_ts: float = 0.0


class HeartbeatPayload(BaseModel):
    node_id: str
    arch: str = "apple_silicon"
    timestamp: float = Field(default_factory=time.time)
    cpu: CpuMetrics
    memory: MemoryMetrics
    # Thermal signal. Default UNKNOWN so older node agents that predate
    # this field round-trip cleanly; the dashboard falls back to the
    # CPU>=95% proxy when state is unknown.
    thermal: ThermalMetrics = Field(default_factory=ThermalMetrics)
    disk: DiskMetrics | None = None
    ollama: OllamaMetrics
    ollama_host: str = "http://localhost:11434"
    lan_ip: str = ""
    draining: bool = False
    capacity: CapacityMetrics | None = None
    agent_version: str = ""
    image: ImageMetrics | None = None
    image_port: int = 0
    transcription: TranscriptionMetrics | None = None
    transcription_port: int = 0
    # Vision embedding capabilities (CLIP, DINOv2, SigLIP)
    vision_embedding: VisionEmbeddingMetrics | None = None
    vision_embedding_port: int = 0
    # Connection health: failures since last successful heartbeat
    connection_failures: int = 0
    connection_failures_total: int = 0  # Total since agent start
    # Hardware identity — used for device-aware scoring.  ``chip`` is the raw
    # string from the OS (e.g. "Apple M3 Ultra"); ``memory_bandwidth_gbps``
    # comes from hardware_lookup.resolve_bandwidth().  Both are optional —
    # older agents will report them as empty / 0, and the router falls back
    # to memory-tier heuristics.  See docs/plans/device-aware-scoring.md.
    chip: str = ""
    memory_bandwidth_gbps: float = 0.0
    # Multi-MLX-server support.  Empty list ⇒ node has no MLX servers
    # configured, or is running an older agent.  See
    # ``docs/issues/multi-mlx-server-support.md``.  The bind host that the
    # servers listen on is reported separately so the router can construct
    # LAN-reachable URLs even when the node's local bind differs from its
    # LAN IP.
    mlx_servers: list[MlxServerInfo] = Field(default_factory=list)
    mlx_bind_host: str = "127.0.0.1"


class HardwareProfile(BaseModel):
    node_id: str
    arch: str = "apple_silicon"
    chip: str = ""
    cores_physical: int = 0
    memory_total_gb: float = 0.0
    # Unified-memory / GPU memory bandwidth in GB/s.  0 means unknown — the
    # scorer falls back to memory-tier heuristics.  Populated by the collector
    # via ``hardware_lookup.resolve_bandwidth(chip)`` on startup.
    memory_bandwidth_gbps: float = 0.0
    ollama_host: str = "http://localhost:11434"


class NodeState(BaseModel):
    node_id: str
    status: NodeStatus = NodeStatus.ONLINE
    hardware: HardwareProfile
    last_heartbeat: float = 0.0
    missed_heartbeats: int = 0
    cpu: CpuMetrics | None = None
    memory: MemoryMetrics | None = None
    thermal: ThermalMetrics | None = None
    disk: DiskMetrics | None = None
    ollama: OllamaMetrics | None = None
    ollama_base_url: str = "http://localhost:11434"
    # Track when models were last unloaded for warm-tier scoring
    model_unloaded_at: dict[str, float] = Field(default_factory=dict)
    # Adaptive capacity from the node's capacity learner
    capacity: CapacityMetrics | None = None
    # Software version reported by the node agent
    agent_version: str = ""
    # Image generation capabilities
    image: ImageMetrics | None = None
    # Port for image generation server on this node
    image_port: int = 0
    # Speech-to-text capabilities
    transcription: TranscriptionMetrics | None = None
    # Port for transcription server on this node
    transcription_port: int = 0
    # Vision embedding capabilities (CLIP, DINOv2, SigLIP)
    vision_embedding: VisionEmbeddingMetrics | None = None
    # Port for vision embedding server on this node
    vision_embedding_port: int = 0
    # Connection health from node agent
    connection_failures: int = 0  # Failures since last successful heartbeat
    connection_failures_total: int = 0  # Total since agent start
    # Multi-MLX-server state mirrored from heartbeat.  Empty list for nodes
    # without MLX configured (or older agents that predate this field).
    mlx_servers: list[MlxServerInfo] = Field(default_factory=list)
    mlx_bind_host: str = "127.0.0.1"
