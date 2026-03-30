"""Data models for node state, heartbeats, and hardware profiles."""

from __future__ import annotations

import time
from enum import StrEnum

from pydantic import BaseModel, Field


class MemoryPressure(StrEnum):
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"


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


class HeartbeatPayload(BaseModel):
    node_id: str
    arch: str = "apple_silicon"
    timestamp: float = Field(default_factory=time.time)
    cpu: CpuMetrics
    memory: MemoryMetrics
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


class HardwareProfile(BaseModel):
    node_id: str
    arch: str = "apple_silicon"
    chip: str = ""
    cores_physical: int = 0
    memory_total_gb: float = 0.0
    ollama_host: str = "http://localhost:11434"


class NodeState(BaseModel):
    node_id: str
    status: NodeStatus = NodeStatus.ONLINE
    hardware: HardwareProfile
    last_heartbeat: float = 0.0
    missed_heartbeats: int = 0
    cpu: CpuMetrics | None = None
    memory: MemoryMetrics | None = None
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
