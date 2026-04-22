"""Multi-turn TTFT benchmark: mlx-lm (with prefix cache) vs Ollama (no cache).

Simulates a Claude-Code-style session where each turn extends the prior
conversation by ~500 tokens of "tool results" and asks a new question.
Measures time-to-first-token per turn. If prefix caching works, TTFT
stays roughly flat; without caching, TTFT grows linearly with context.
"""
from __future__ import annotations
import json, time, sys, argparse
from pathlib import Path
import urllib.request


SYSTEM_PROMPT = (
    "You are an AI coding assistant. You have access to tools to read files, "
    "edit them, and run shell commands. Be concise. Always call a tool when "
    "the user asks you to do something that requires one."
)

# Fake "file content" we'll append as tool_result-style chunks each turn
FILE_CHUNK_TEMPLATE = (
    "Here is the content of file{i}.py:\n"
    "```python\n"
    "def process_items_{i}(items):\n"
    "    '''Process a list of items, returning only even integers.'''\n"
    "    result = []\n"
    "    for x in items:\n"
    "        if isinstance(x, int) and x % 2 == 0:\n"
    "            result.append(x * 2)\n"
    "    return result\n"
    "\n"
    "def validate_input_{i}(data):\n"
    "    '''Validate that input is a non-empty list of numbers.'''\n"
    "    if not isinstance(data, list):\n"
    "        raise ValueError('Expected list')\n"
    "    if not data:\n"
    "        raise ValueError('Empty list')\n"
    "    for item in data:\n"
    "        if not isinstance(item, (int, float)):\n"
    "            raise TypeError(f'Bad type: {{type(item)}}')\n"
    "    return True\n"
    "\n"
    "class DataProcessor_{i}:\n"
    "    def __init__(self, config):\n"
    "        self.config = config\n"
    "        self.cache = {{}}\n"
    "    def run(self, items):\n"
    "        validate_input_{i}(items)\n"
    "        return process_items_{i}(items)\n"
    "```\n"
)


def streaming_request(url: str, payload: dict) -> tuple[float, float, int]:
    """POST a streaming chat request, return (ttft_ms, total_ms, output_tokens).

    TTFT = wall-clock from request send to first non-empty content chunk.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t_start = time.perf_counter()
    first_token_t = None
    output_tokens = 0
    output_text_parts = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("data: "):
                line = line[6:].strip()
            if line == "[DONE]":
                break
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or choice.get("message") or {}
            txt = delta.get("content") or ""
            if txt:
                if first_token_t is None:
                    first_token_t = time.perf_counter()
                output_tokens += 1
                output_text_parts.append(txt)
    t_end = time.perf_counter()
    ttft_ms = (first_token_t - t_start) * 1000 if first_token_t else (t_end - t_start) * 1000
    total_ms = (t_end - t_start) * 1000
    return ttft_ms, total_ms, output_tokens


def run_session(label: str, url: str, model: str, n_turns: int, out_path: Path):
    """Run a simulated N-turn session; record per-turn metrics to JSON."""
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    results = []
    print(f"\n=== {label} ({model}) on {url} ===")
    print(f"{'turn':>4}  {'in_msgs':>7}  {'ttft_ms':>8}  {'total_ms':>9}  {'out_tok':>7}")

    for turn in range(1, n_turns + 1):
        # Each turn appends a growing "tool result" chunk + a new user question
        chunk = FILE_CHUNK_TEMPLATE.format(i=turn)
        user_msg = f"{chunk}\nSummarize the purpose of the DataProcessor_{turn} class in one short sentence. Just one sentence, no preamble."
        messages.append({"role": "user", "content": user_msg})

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": 60,
            "temperature": 0.2,
        }
        try:
            ttft_ms, total_ms, out_tok = streaming_request(url, payload)
        except Exception as e:
            print(f"{turn:>4}  ERROR: {e}")
            results.append({"turn": turn, "error": str(e)})
            break

        # Count cumulative input size (rough char-based proxy for token count)
        total_chars = sum(len(m["content"]) for m in messages)
        results.append({
            "turn": turn,
            "in_msgs": len(messages),
            "in_chars": total_chars,
            "ttft_ms": round(ttft_ms, 1),
            "total_ms": round(total_ms, 1),
            "out_tokens": out_tok,
        })
        print(f"{turn:>4}  {len(messages):>7}  {ttft_ms:>8.0f}  {total_ms:>9.0f}  {out_tok:>7}")

        # Append an assistant stub so next turn extends the conversation
        # (don't need the full streamed response for the benchmark — a short stub is fine)
        messages.append({"role": "assistant", "content": f"(assistant response for turn {turn})"})

    out_path.write_text(json.dumps(results, indent=2))
    print(f"saved to {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["mlx", "ollama", "both"], default="both")
    ap.add_argument("--turns", type=int, default=25)
    args = ap.parse_args()

    outdir = Path("/tmp/mlx-experiment")
    if args.target in ("mlx", "both"):
        run_session(
            "MLX (with prefix cache)",
            "http://127.0.0.1:11440/v1/chat/completions",
            "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
            args.turns,
            outdir / "mlx_results.json",
        )
    if args.target in ("ollama", "both"):
        run_session(
            "Ollama (no prefix cache)",
            "http://localhost:11434/v1/chat/completions",
            "qwen3-coder:30b",
            args.turns,
            outdir / "ollama_results.json",
        )
