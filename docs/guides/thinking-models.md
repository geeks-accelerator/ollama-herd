# Working with Thinking Models

Thinking models like `gpt-oss:120b`, `deepseek-r1`, and `qwq` use chain-of-thought reasoning — they "think" before responding. This guide explains how Herd handles them and how to avoid common pitfalls.

## The problem: thinking eats your token budget

Thinking models split `num_predict` (max output tokens) between internal reasoning and visible output:

```
num_predict = 200
├── thinking tokens: 187  (invisible chain-of-thought)
└── output tokens:    13  (what the user sees)
```

If the thinking phase consumes the entire budget, you get:
- `message.content` = empty string
- `done_reason` = `"length"` (budget exhausted)
- No error — Ollama considers this a successful completion

This is the #1 cause of empty responses from thinking models.

## How Herd helps

### 1. Automatic budget inflation

Herd detects thinking models and automatically inflates `num_predict` before forwarding to Ollama:

```
Client sends:    num_predict = 200
Herd forwards:   num_predict = 1024  (max of 200 × 4.0, minimum 1024)
```

This ensures enough budget for both reasoning and output. Configure via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_THINKING_OVERHEAD` | `4.0` | Multiply client's `num_predict` by this factor |
| `FLEET_THINKING_MIN_PREDICT` | `1024` | Floor — never send less than this to Ollama |

Only applies when the client explicitly sets `num_predict` / `max_tokens`. If omitted, Ollama uses the model's default (usually large enough).

### 2. Thinking-aware response headers

Non-streaming responses include diagnostic headers:

```
X-Thinking-Tokens: 187      # Estimated tokens spent on reasoning
X-Output-Tokens: 13         # Estimated tokens of visible output
X-Budget-Used: 200/1024     # completion_tokens / num_predict sent to Ollama
X-Done-Reason: stop         # "stop" (natural end) or "length" (budget hit)
```

These make debugging instant — if you see `X-Thinking-Tokens: 200, X-Output-Tokens: 0, X-Done-Reason: length`, the thinking phase consumed the entire budget.

### 3. Known thinking models

Herd auto-detects these model families as thinking models:

- `deepseek-r1` (all sizes: 8b, 14b, 32b, 70b, 671b)
- `gpt-oss` (120b)
- `qwq` (32b)
- `phi-4-reasoning` / `phi4-reasoning`
- Any model with `reasoning` in the name

## Recommended settings by use case

| Use case | Minimum `num_predict` | Why |
|----------|----------------------|-----|
| Structured JSON (short) | 400+ | Thinking models reason about schema compliance |
| Free text (1-2 paragraphs) | 800+ | Reasoning about tone, structure, then generating |
| Long-form content | 2000+ | Extended reasoning + extended output |
| Code generation | 1500+ | Reasoning about architecture, then writing code |

With Herd's default `thinking_overhead: 4.0` and `thinking_min_predict: 1024`, most clients don't need to worry about this — the router handles it automatically.

## Client-side tips

### Don't set num_predict too low

```python
# BAD — thinking model will likely return empty content
client.chat.completions.create(model="gpt-oss:120b", max_tokens=100, ...)

# GOOD — let Herd inflate, or set a generous budget
client.chat.completions.create(model="gpt-oss:120b", max_tokens=500, ...)

# BEST — omit max_tokens entirely, let the model use its default
client.chat.completions.create(model="gpt-oss:120b", ...)
```

### Check for the empty-content pattern

When using thinking models, always handle the case where content is empty:

```python
response = client.chat.completions.create(model="gpt-oss:120b", ...)
content = response.choices[0].message.content

if not content and response.choices[0].finish_reason == "length":
    # Budget exhausted by thinking — retry with higher max_tokens
    response = client.chat.completions.create(
        model="gpt-oss:120b",
        max_tokens=2000,  # Much more generous
        ...
    )
```

### Use response headers for diagnostics

```python
# Non-streaming responses include thinking diagnostics
response = httpx.post("http://router:11435/api/chat", json={...})
print(response.headers.get("X-Thinking-Tokens"))  # e.g., "187"
print(response.headers.get("X-Done-Reason"))       # "stop" or "length"
```

## Queue depth for load planning

If you have multiple bots/agents sharing the fleet, use `/fleet/queue` to make smart backoff decisions:

```bash
curl http://router:11435/fleet/queue
```

```json
{
  "queue_depth": 5,
  "pending": 2,
  "in_flight": 3,
  "estimated_wait_ms": 15000,
  "nodes_online": 1,
  "queues": {
    "Studio:gpt-oss:120b": {
      "pending": 2,
      "in_flight": 3,
      "concurrency": 2,
      "model": "gpt-oss:120b"
    }
  }
}
```

Use `estimated_wait_ms` to decide whether to send a request now or wait. This is especially important with thinking models that can take minutes for large generations.
