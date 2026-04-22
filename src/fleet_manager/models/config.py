"""Configuration models with sensible defaults for zero-config startup."""

from __future__ import annotations

from datetime import datetime

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 11435
    heartbeat_interval: float = 5.0
    heartbeat_timeout: float = 15.0
    heartbeat_offline: float = 30.0
    mdns_service_type: str = "_fleet-manager._tcp.local."
    mdns_service_name: str = "Fleet Manager Router"
    data_dir: str = "~/.fleet-manager"

    # Scoring weights
    score_model_hot: float = 50.0
    score_model_warm: float = 30.0
    score_model_cold: float = 10.0
    score_memory_fit_max: float = 20.0
    score_queue_depth_max_penalty: float = 30.0
    score_queue_depth_penalty_per: float = 6.0
    score_wait_time_max_penalty: float = 25.0
    score_role_affinity_max: float = 15.0
    score_role_large_threshold_gb: float = 20.0
    score_role_small_threshold_gb: float = 8.0
    score_availability_trend_max: float = 10.0
    score_context_fit_max: float = 15.0

    # Rebalancer
    rebalance_interval: float = 5.0
    rebalance_threshold: int = 4
    rebalance_max_per_cycle: int = 3

    # Pre-warm
    pre_warm_threshold: int = 3
    pre_warm_min_availability: float = 0.60

    # Auto-pull
    auto_pull: bool = True
    auto_pull_timeout: float = 300.0  # 5 minutes

    # VRAM-aware fallback: route to loaded model in same category instead of cold-loading
    vram_fallback: bool = True

    # Context protection: prevent clients from triggering Ollama model reloads via num_ctx
    # "strip" = remove num_ctx when ≤ loaded context (default, prevents reload hang)
    # "warn"  = keep num_ctx but log warnings
    # "passthrough" = do nothing
    context_protection: str = "strip"

    # Stale request reaper
    # Seconds before in-flight requests are considered zombied (15 min default)
    stale_timeout: float = 600.0

    # Image generation routing
    image_generation: bool = True  # Route /api/generate-image to nodes with mflux/DiffusionKit
    image_timeout: float = 120.0  # Max seconds to wait for image generation

    # Transcription routing
    transcription: bool = True  # Route /api/transcribe to nodes with Qwen3-ASR
    transcription_timeout: float = 300.0  # Max seconds for transcription

    # Vision embedding routing (CLIP, DINOv2, SigLIP)
    vision_embedding: bool = True  # Route /api/embed-image to nodes with vision embeddings
    vision_embedding_timeout: float = 30.0  # Max seconds for embedding

    # Thinking model support
    thinking_overhead: float = 4.0  # Multiply num_predict by this for thinking models
    thinking_min_predict: int = 1024  # Minimum num_predict for thinking models

    # Dynamic context management
    dynamic_num_ctx: bool = False  # Inject num_ctx overrides on cold loads
    num_ctx_overrides: dict[str, int] = {}  # Per-model: {"gpt-oss:120b": 32768}
    num_ctx_auto_calculate: bool = False  # Auto-calculate from trace data

    # Fleet Intelligence — LLM-powered dashboard briefing
    fleet_intelligence: bool = True  # Enable briefing card on dashboard
    fleet_intelligence_model: str = ""  # Empty = auto-select best loaded LLM

    # Retry
    max_retries: int = 2

    # Anthropic Messages API compat (for Claude Code etc.)
    # JSON map of claude-* model id → local Ollama model.
    # Always include a "default" key to catch unknown claude-* requests.
    anthropic_model_map: dict[str, str] = {
        "default": "qwen3-coder:30b",
        "claude-opus-4-7": "qwen3:32b",
        "claude-sonnet-4-6": "qwen3-coder:30b",
        "claude-sonnet-4-5": "qwen3-coder:30b",
        "claude-haiku-4-5": "qwen3:14b",
    }
    # Optional shared secret for /v1/messages. When require_key is true and the
    # client's x-api-key header doesn't match anthropic_api_key, return 401.
    anthropic_require_key: bool = False
    anthropic_api_key: str = ""
    anthropic_default_max_tokens: int = 4096

    model_config = {"env_prefix": "FLEET_"}


class NodeSettings(BaseSettings):
    node_id: str = ""
    ollama_host: str = "http://localhost:11434"
    router_url: str = ""
    heartbeat_interval: float = 5.0
    poll_interval: float = 5.0
    mdns_service_type: str = "_fleet-manager._tcp.local."
    enable_capacity_learning: bool = False
    data_dir: str = "~/.fleet-manager"

    # Platform connection (all None when disconnected).
    # The operator token is stored as SecretStr so it never appears in
    # repr() / str() / model_dump() without explicit get_secret_value().
    # Persisted separately to ~/.fleet-manager/platform.json with 0600
    # permissions — never written to the main config.yaml.
    platform_url: str | None = None
    platform_token: SecretStr | None = None
    platform_node_id: str | None = None
    platform_connected_at: datetime | None = None

    # Telemetry opt-ins (require platform connection to take effect)
    telemetry_local_summary: bool = False
    telemetry_include_tags: bool = False

    model_config = {"env_prefix": "FLEET_NODE_"}
