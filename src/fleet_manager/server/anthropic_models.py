"""Pydantic models for the Anthropic Messages API surface.

Used by `routes/anthropic_compat.py` to validate `POST /v1/messages` and
`POST /v1/messages/count_tokens` request bodies, and to shape the
non-streaming response.

Permissive: unknown fields (cache_control, computer use blocks, etc.)
are ignored rather than rejected so we don't 422 future Claude features.

# EXTRACTION SEAM (recorded 2026-04-24):
# - Fleet-manager dependencies: NONE.
# - External dependencies: pydantic (which any Anthropic proxy would need).
# - Public surface: all of the BaseModel classes below.  Stable contract
#   with Anthropic's Messages API spec; worth keeping untouched if extracted.
# - ``AnthropicMessage.content`` is deliberately ``str | list[dict[str, Any]]``
#   rather than a discriminated union so new block types (cache_edits,
#   cache_control, thinking, etc.) pass through without validation errors.
#   That laxness is a feature, not a bug — see
#   ``docs/research/why-claude-code-degrades-at-30k.md`` §7 for why.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Content blocks ----------------------------------------------------------


class _Block(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TextBlock(_Block):
    type: Literal["text"]
    text: str


class ImageSource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["base64", "url"]
    media_type: str | None = None
    data: str | None = None
    url: str | None = None


class ImageBlock(_Block):
    type: Literal["image"]
    source: ImageSource


class ToolUseBlock(_Block):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(_Block):
    type: Literal["tool_result"]
    tool_use_id: str
    # Per Anthropic spec, content can be a string or a list of text/image blocks
    content: str | list[dict[str, Any]] = ""
    is_error: bool = False


class ThinkingBlock(_Block):
    type: Literal["thinking"]
    thinking: str = ""
    signature: str | None = None


# Discriminated union — pydantic validates by `type` field
ContentBlock = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock


# --- Messages ----------------------------------------------------------------


class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]  # accept raw dicts; we parse downstream


# --- Tools -------------------------------------------------------------------


class AnthropicTool(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["auto", "any", "tool", "none"] = "auto"
    name: str | None = None  # required when type == "tool"


# --- System prompt -----------------------------------------------------------


class SystemBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["text"] = "text"
    text: str


# --- Request -----------------------------------------------------------------


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    max_tokens: int = 4096
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: ToolChoice | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None
    # Accept and ignore: thinking config, betas, anthropic_version, etc.


# --- Response ----------------------------------------------------------------


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AnthropicMessageResponse(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: list[dict[str, Any]]  # text + tool_use blocks
    stop_reason: Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"] | None = None
    stop_sequence: str | None = None
    usage: Usage = Field(default_factory=Usage)
