"""Data models for inference requests, queue entries, and routing results."""

from __future__ import annotations

import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field


class RequestFormat(str, Enum):
    OPENAI = "openai"
    OLLAMA = "ollama"


class RequestStatus(str, Enum):
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


class QueueEntry(BaseModel):
    request: InferenceRequest
    status: RequestStatus = RequestStatus.PENDING
    assigned_node: str = ""
    enqueued_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None


class RoutingResult(BaseModel):
    node_id: str
    queue_key: str
    score: float
    scores_breakdown: dict[str, float] = Field(default_factory=dict)
