"""Data models for inference requests, queue entries, and routing results."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, Field


class RequestFormat(StrEnum):
    OPENAI = "openai"
    OLLAMA = "ollama"


class RequestStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


class InferenceRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    model: str
    messages: list[dict] = Field(default_factory=list)
    stream: bool = True
    temperature: float = 0.7
    max_tokens: int | None = None
    original_format: RequestFormat = RequestFormat.OPENAI
    raw_body: dict = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    # Fallback & trace fields
    original_model: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    # Request tagging for per-app analytics
    tags: list[str] = Field(default_factory=list)


class QueueEntry(BaseModel):
    request: InferenceRequest
    status: RequestStatus = RequestStatus.PENDING
    assigned_node: str = ""
    enqueued_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    # Routing context for traces
    routing_score: float | None = None
    routing_breakdown: dict[str, float] | None = None
    retry_count: int = 0
    fallback_used: bool = False
    excluded_nodes: list[str] = Field(default_factory=list)


class RoutingResult(BaseModel):
    node_id: str
    queue_key: str
    score: float
    scores_breakdown: dict[str, float] = Field(default_factory=dict)
