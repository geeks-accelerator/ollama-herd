"""The benchmark's workload shape: prompt length, reply cap, embedding models.

The harness sent ~10-token prompts with no reply cap, while real requests
average ~1,245 prompt tokens and ~196 completion tokens (ollamaherd.com/stats,
30 days to 2026-11-20), so a run measured almost no prefill. And the CLI sent
chat requests to embedding models, which fail.
"""

from __future__ import annotations

import json
import random

import pytest
import tiktoken

from fleet_manager.server.benchmark_engine import (
    LONG_TOPICS,
    PROMPTS,
    make_prompt,
    send_request,
)
from fleet_manager.server.model_knowledge import is_embedding_model

ENC = tiktoken.get_encoding("cl100k_base")


def test_default_prompt_is_unchanged():
    assert make_prompt(None, random.Random(1)) in PROMPTS


@pytest.mark.parametrize("target", [500, 1250, 4000])
def test_prompt_is_padded_to_about_the_target(target):
    n = len(ENC.encode(make_prompt(target, random.Random(7))))
    assert abs(n - target) <= target * 0.03, n


def test_padding_differs_from_the_first_tokens():
    """A shared prefix would let Ollama's prompt cache skip the prefill."""
    a = ENC.encode(make_prompt(1250, random.Random(1)))
    b = ENC.encode(make_prompt(1250, random.Random(2)))
    assert a[:20] != b[:20]


def test_padded_prompt_asks_for_a_long_answer():
    """PROMPTS ask for one sentence, and a model obeys that over "write more":
    qwen2.5-coder:7b stopped at ~40 tokens on half of them, so the padded
    workload uses open topics that run to the reply cap."""
    for seed in range(20):
        prompt = make_prompt(1250, random.Random(seed))
        assert "sentence:" not in prompt and "One sentence" not in prompt
        assert any(t in prompt for t in LONG_TOPICS)
        assert "at least 40 points" in prompt


@pytest.mark.parametrize(
    ("name", "embed"),
    [
        ("nomic-embed-text", True),
        ("qwen3-embedding:8b", True),
        ("bge-m3", True),
        ("qwen3-coder:30b", False),
    ],
)
def test_embedding_models_are_recognised(name, embed):
    assert is_embedding_model(name) is embed


@pytest.mark.asyncio
async def test_send_request_carries_the_workload(httpx_mock):
    import httpx

    httpx_mock.add_response(
        url="http://router.test/api/chat",
        text=json.dumps(
            {
                "message": {"content": "hi"},
                "done": True,
                "prompt_eval_count": 1240,
                "eval_count": 200,
            }
        ),
    )
    async with httpx.AsyncClient(base_url="http://router.test") as client:
        result = await send_request(client, "qwen3-coder:30b", prompt_tokens=1250, max_tokens=200)
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["options"] == {"num_predict": 200}
    assert len(ENC.encode(body["messages"][0]["content"])) > 1200
    assert (result.prompt_tokens, result.completion_tokens) == (1240, 200)


@pytest.mark.asyncio
async def test_send_request_defaults_send_no_options(httpx_mock):
    import httpx

    httpx_mock.add_response(url="http://router.test/api/chat", text=json.dumps({"done": True}))
    async with httpx.AsyncClient(base_url="http://router.test") as client:
        await send_request(client, "qwen3-coder:30b")
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert "options" not in body


@pytest.mark.asyncio
async def test_thinking_tokens_count_as_the_first_token(httpx_mock):
    """A thinking model streams "thinking" before "content"; TTFT read 0 ms
    for qwen3.5:4b when only content counted."""
    import httpx

    lines = [
        {"message": {"thinking": "Let me"}, "done": False},
        {"message": {"thinking": " think"}, "done": False},
        {"done": True, "prompt_eval_count": 10, "eval_count": 2},
    ]
    httpx_mock.add_response(
        url="http://router.test/api/chat", text="\n".join(json.dumps(x) for x in lines)
    )
    async with httpx.AsyncClient(base_url="http://router.test") as client:
        result = await send_request(client, "qwen3.5:4b")
    assert result.success
    assert result.ttft_ms > 0
