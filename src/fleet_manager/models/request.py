"""Data models for inference requests, queue entries, and routing results."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class RequestFormat(StrEnum):
    OPENAI = "openai"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    # OpenAI Responses API (/v1/responses) — what Codex CLI speaks since it
    # dropped chat/completions in Feb 2026.  Like ANTHROPIC, the route
    # pre-translates into an Ollama-ready body and owns response translation.
    RESPONSES = "responses"


class RequestStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


def normalize_model_name(name: str) -> str:
    """Normalize Ollama model names by appending :latest when no tag is present.

    Ollama always returns model names with explicit tags (e.g. 'qwen3-coder:latest',
    'qwen3:235b'). Client requests often omit the tag (e.g. 'qwen3-coder'), which
    causes duplicate queues, scoring mismatches, and cache misses throughout the
    pipeline. This function ensures consistent naming.
    """
    if not name:
        return name
    if ":" not in name:
        return f"{name}:latest"
    return name


# Content-part `type` values that carry an image, across the formats a client
# might send to any of our routes. This detector gates the loud-fail guard that
# stops an image request from being answered by a blind text model — so a type
# it misses is not a cosmetic gap: it silently disables that protection.
# 2026-07-27: `type:"image"` / `type:"input_image"` were absent here, so vision
# requests carrying those shapes fell back to gpt-oss and 400'd, while the guard
# that exists to prevent exactly that sat inert. Ollama recognised the images
# (hence the 400); we didn't. Keep this superset of what the runtimes accept.
_IMAGE_PART_TYPES = frozenset({"image_url", "image", "input_image"})


def _detect_images(messages: list[dict]) -> bool:
    """Check if any message carries image content, across all client formats."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Ollama format: a sibling `images` list of base64 strings.
        if msg.get("images"):
            return True
        # OpenAI / Anthropic / Responses: content is a list of typed parts.
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES:
                    return True
    return False


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
    # Model type: "text", "image", "stt", "embed"
    request_type: str = "text"
    # True when messages contain image content (vision requests)
    has_images: bool = False
    # Caller IP for per-client concurrency accounting (empty = anonymous).
    client_ip: str = ""

    @model_validator(mode="after")
    def _normalize_model_names(self) -> InferenceRequest:
        """Append :latest to model names missing a tag, matching Ollama conventions."""
        self.model = normalize_model_name(self.model)
        if self.original_model:
            self.original_model = normalize_model_name(self.original_model)
        self.fallback_models = [normalize_model_name(m) for m in self.fallback_models]
        # Auto-detect images in messages (OpenAI image_url parts or Ollama images field)
        if not self.has_images:
            self.has_images = _detect_images(self.messages)
        return self


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
