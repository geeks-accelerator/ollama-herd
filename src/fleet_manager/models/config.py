"""Configuration models with sensible defaults for zero-config startup."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class ServerSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8080
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

    # Rebalancer
    rebalance_interval: float = 5.0
    rebalance_threshold: int = 4
    rebalance_max_per_cycle: int = 3

    # Pre-warm
    pre_warm_threshold: int = 3
    pre_warm_min_availability: float = 0.60

    # Retry
    max_retries: int = 2

    model_config = {"env_prefix": "FLEET_"}


class NodeSettings(BaseSettings):
    node_id: str = ""
    ollama_host: str = "http://localhost:11434"
    router_url: str = ""
    heartbeat_interval: float = 5.0
    poll_interval: float = 5.0
    mdns_service_type: str = "_fleet-manager._tcp.local."

    model_config = {"env_prefix": "FLEET_NODE_"}
