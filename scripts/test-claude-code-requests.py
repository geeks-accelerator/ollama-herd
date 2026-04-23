#!/usr/bin/env python3
"""Stress test Claude Code-style requests against Ollama + Herd.

Replays the request shapes that were failing (qwen3-coder:30b-agent with
tools, anthropic format) to verify they now complete. Useful after tuning
OLLAMA_NUM_PARALLEL or upgrading Ollama.

Usage:
    # Test local Ollama only (fastest, confirms Jetsam/OOM fix)
    python3 scripts/test-claude-code-requests.py --mode direct

    # Test via Herd router (end-to-end, tests routing + translation)
    python3 scripts/test-claude-code-requests.py --mode fleet \
        --router http://10.0.0.10:11435

    # Both, with custom iterations
    python3 scripts/test-claude-code-requests.py --mode both -n 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures — varied prompt sizes and tool configurations
# ─────────────────────────────────────────────────────────────────────────────

def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required or list(props.keys()),
            },
        },
    }


# Match the real Claude Code tool set — 18 tools with verbose schemas
CLAUDE_CODE_TOOLS = [
    _tool("Bash", "Execute a bash command in a persistent shell session. "
          "Supports timeout, background execution, and working directory.",
          {"command": {"type": "string"}, "timeout": {"type": "integer"},
           "description": {"type": "string"}, "run_in_background": {"type": "boolean"}}),
    _tool("Read", "Read a file from the local filesystem. Supports text, images, "
          "PDFs, and Jupyter notebooks. Returns content with line numbers.",
          {"file_path": {"type": "string"}, "limit": {"type": "integer"},
           "offset": {"type": "integer"}, "pages": {"type": "string"}},
          required=["file_path"]),
    _tool("Edit", "Edit a file with exact string replacement. Preserves "
          "indentation. Fails if old_string is not unique unless replace_all.",
          {"file_path": {"type": "string"}, "old_string": {"type": "string"},
           "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
          required=["file_path", "old_string", "new_string"]),
    _tool("Write", "Write content to a file, overwriting if it exists.",
          {"file_path": {"type": "string"}, "content": {"type": "string"}}),
    _tool("Grep", "Search for regex patterns across files using ripgrep.",
          {"pattern": {"type": "string"}, "path": {"type": "string"},
           "glob": {"type": "string"}, "type": {"type": "string"},
           "output_mode": {"type": "string"}, "-i": {"type": "boolean"},
           "-n": {"type": "boolean"}, "context": {"type": "integer"},
           "multiline": {"type": "boolean"}, "head_limit": {"type": "integer"}},
          required=["pattern"]),
    _tool("Glob", "Fast file pattern matching.",
          {"pattern": {"type": "string"}, "path": {"type": "string"}},
          required=["pattern"]),
    _tool("TodoWrite", "Create/update a structured task list for the session.",
          {"todos": {"type": "array", "items": {"type": "object"}}}),
    _tool("WebFetch", "Fetch a URL and process with a prompt.",
          {"url": {"type": "string"}, "prompt": {"type": "string"}}),
    _tool("WebSearch", "Search the web and return results.",
          {"query": {"type": "string"}, "allowed_domains": {"type": "array"},
           "blocked_domains": {"type": "array"}}, required=["query"]),
    _tool("NotebookEdit", "Edit a Jupyter notebook cell.",
          {"notebook_path": {"type": "string"}, "new_source": {"type": "string"},
           "cell_id": {"type": "string"}, "cell_type": {"type": "string"},
           "edit_mode": {"type": "string"}},
          required=["notebook_path", "new_source"]),
    _tool("Task", "Delegate to a specialized sub-agent.",
          {"description": {"type": "string"}, "prompt": {"type": "string"},
           "subagent_type": {"type": "string"}, "model": {"type": "string"}},
          required=["description", "prompt"]),
    _tool("ExitPlanMode", "Exit plan mode with a finalized plan.",
          {"plan": {"type": "string"}}),
    _tool("BashOutput", "Get output from a background bash process.",
          {"bash_id": {"type": "string"}, "filter": {"type": "string"}},
          required=["bash_id"]),
    _tool("KillBash", "Kill a background bash process.",
          {"shell_id": {"type": "string"}}),
    _tool("SlashCommand", "Invoke a slash command.",
          {"command": {"type": "string"}}),
    _tool("AskUserQuestion", "Ask the user a disambiguating question.",
          {"questions": {"type": "array", "items": {"type": "object"}}}),
    _tool("Skill", "Execute a skill by name.",
          {"skill": {"type": "string"}, "args": {"type": "string"}},
          required=["skill"]),
    _tool("Monitor", "Monitor a long-running process.",
          {"process_id": {"type": "string"}}),
]


# Realistic Claude Code system prompt fragment (~2K tokens of preamble)
CLAUDE_CODE_SYSTEM = """You are Claude Code, Anthropic's official CLI for coding tasks.

IMPORTANT: Refuse to write code or explain code that may be used maliciously.
Refuse to create, modify, or improve code that appears malicious.

When the user asks about Claude Code (CLI tool), use the WebFetch tool to gather
information from Claude Code docs at https://docs.claude.com/en/docs/claude-code.

Tone and style: You should be concise, direct, and to the point. When you run a
non-trivial bash command, you should explain what the command does and why you
are running it. Your output will be displayed on a command line interface.

Proactiveness: You are allowed to be proactive, but only when the user asks you
to do something. Strike a balance between doing the right thing when asked,
including taking actions and follow-up actions, and not surprising the user.

Following conventions: When making changes to files, first understand the file's
code conventions. Mimic code style, use existing libraries and utilities, and
follow existing patterns.

Code style: Do NOT add comments unless asked. Follow these conventions for
Python: use type hints, prefer pydantic v2 models, async/await over sync, pep8
formatting via ruff, use pathlib over os.path.

Task Management: You have access to the TodoWrite tool to help you manage and
plan tasks. Use this tool VERY frequently to track progress and demonstrate
thoroughness. Use it proactively for complex tasks (3+ steps).

Doing tasks: The user will primarily request you perform software engineering
tasks. For these tasks, the following steps are recommended:
1. Use available search tools to understand the codebase and the user's query
2. Implement the solution using all tools available to you
3. Verify the solution if possible with tests. NEVER assume specific test
   framework or test script. Check the README or search codebase to determine
   the testing approach.
4. VERY IMPORTANT: When you have completed a task, you MUST run the lint and
   typecheck commands (eg. npm run lint, npm run typecheck, ruff, etc.) if they
   were provided. If you are unable to find the correct command, ask the user
   for it and then suggest writing it to CLAUDE.md.

Tool usage policy: When doing file search, prefer to use the Task tool to
reduce context usage. VERY IMPORTANT: You MUST avoid using search commands like
`find` and `grep`. Instead use Grep, Glob, or Task to search.
""" * 1  # 1x ≈ 500 words ≈ 700 tokens


def _big_code_context(num_files: int) -> str:
    """Build a realistic chunk of 'codebase context' — simulates what Claude Code
    pastes into prompts when working on a repo."""
    blocks = []
    for i in range(num_files):
        blocks.append(
            f"=== src/module_{i}.py ===\n"
            f"\"\"\"Module {i}: handles data processing for pipeline stage {i % 5}.\"\"\"\n\n"
            f"from __future__ import annotations\n"
            f"import asyncio\nimport logging\nfrom dataclasses import dataclass\n\n"
            f"logger = logging.getLogger(__name__)\n\n"
            f"@dataclass\nclass Config{i}:\n    name: str\n    timeout: float = 30.0\n"
            f"    retries: int = 3\n\n"
            f"async def process_{i}(data: dict, config: Config{i}) -> list[dict]:\n"
            f"    \"\"\"Process items for stage {i}.\"\"\"\n"
            f"    logger.info(f\"Processing {{len(data)}} items\")\n"
            f"    results = []\n"
            f"    for item in data.get('items', []):\n"
            f"        # Step {i}: validate, transform, enrich\n"
            f"        if item.get('valid'):\n"
            f"            results.append({{'id': item['id'], 'stage': {i}}})\n"
            f"    return results\n\n"
        )
    return "\n".join(blocks)


def make_fixtures(model: str) -> list[dict]:
    """Return realistic Claude Code-style test requests at varying sizes.

    Validation criteria encoded per fixture:
    - `must_contain`: substrings the response should include (case-insensitive)
    - `must_tool_call`: name of tool the model should call (or None)
    - `min_tokens` / `max_tokens`: sanity range for completion length
    """
    fixtures = [
        {
            "name": "tiny-hello",
            "body": {
                "model": model,
                "messages": [
                    {"role": "system", "content": CLAUDE_CODE_SYSTEM},
                    {"role": "user", "content": "Reply with exactly: ok"},
                ],
                "tools": CLAUDE_CODE_TOOLS,
                "stream": False,
                "keep_alive": -1,
            },
            "expect": {"must_contain": ["ok"], "min_tokens": 1, "max_tokens": 20},
        },
        {
            "name": "short-tool-bash",
            "body": {
                "model": model,
                "messages": [
                    {"role": "system", "content": CLAUDE_CODE_SYSTEM},
                    {
                        "role": "user",
                        "content": "Run `ls /tmp` using the Bash tool. Don't explain anything.",
                    },
                ],
                "tools": CLAUDE_CODE_TOOLS,
                "stream": False,
                "keep_alive": -1,
            },
            "expect": {"must_tool_call": "Bash", "min_tokens": 5, "max_tokens": 200},
        },
        {
            "name": "medium-grep-tool",
            "body": {
                "model": model,
                "messages": [
                    {"role": "system", "content": CLAUDE_CODE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            "Search the codebase for all uses of 'FastAPI'. "
                            "Use the Grep tool. Respond with only the tool call."
                        ),
                    },
                ],
                "tools": CLAUDE_CODE_TOOLS,
                "stream": False,
                "keep_alive": -1,
            },
            "expect": {"must_tool_call": "Grep", "min_tokens": 5, "max_tokens": 200},
        },
        {
            "name": "multi-turn-tool-result",
            "body": {
                "model": model,
                "messages": [
                    {"role": "system", "content": CLAUDE_CODE_SYSTEM},
                    {"role": "user", "content": "Read /tmp/config.json"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {
                                "name": "Read",
                                "arguments": {"file_path": "/tmp/config.json"},
                            }}
                        ],
                    },
                    {
                        "role": "tool",
                        "content": '{"name": "fleet-test", "port": 11435, "nodes": 2}',
                    },
                    {
                        "role": "user",
                        "content": "What port is configured? Answer with just the number.",
                    },
                ],
                "tools": CLAUDE_CODE_TOOLS,
                "stream": False,
                "keep_alive": -1,
            },
            "expect": {"must_contain": ["11435"], "min_tokens": 1, "max_tokens": 50},
        },
        {
            "name": "large-10k-prompt",
            "body": {
                "model": model,
                "messages": [
                    {"role": "system", "content": CLAUDE_CODE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            "Here's my codebase context:\n\n"
                            + _big_code_context(num_files=40)  # ~10K tokens
                            + "\n\nHow many files did I share? "
                            "Answer with just the number."
                        ),
                    },
                ],
                "tools": CLAUDE_CODE_TOOLS,
                "stream": False,
                "keep_alive": -1,
            },
            "expect": {"must_contain": ["40"], "min_tokens": 1, "max_tokens": 50},
        },
        {
            "name": "xlarge-30k-prompt-streaming",
            "body": {
                "model": model,
                "messages": [
                    {"role": "system", "content": CLAUDE_CODE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            "Here's my codebase context:\n\n"
                            + _big_code_context(num_files=120)  # ~30K tokens
                            + "\n\nList 3 modules you see. Use concise output."
                        ),
                    },
                ],
                "tools": CLAUDE_CODE_TOOLS,
                "stream": True,   # <<< streaming mode — matches Claude Code
                "keep_alive": -1,
            },
            "expect": {"must_contain": ["module"], "min_tokens": 5, "max_tokens": 500},
        },
    ]
    return fixtures


# ─────────────────────────────────────────────────────────────────────────────
# Request execution
# ─────────────────────────────────────────────────────────────────────────────


def post_and_parse(url: str, body: dict, timeout: float = 600) -> tuple[int, dict | str, float]:
    """POST JSON and return (status, parsed_body_or_text, elapsed_seconds).
    Handles both JSON and NDJSON streaming responses."""
    t0 = time.time()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        elapsed = time.time() - t0

        # Streaming request → NDJSON response
        if body.get("stream"):
            lines = raw.decode(errors="replace").splitlines()
            return resp.status, collect_streamed_ndjson(lines), elapsed

        try:
            return resp.status, json.loads(raw), elapsed
        except json.JSONDecodeError:
            return resp.status, raw.decode(errors="replace"), elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body_text = e.read().decode(errors="replace") if hasattr(e, "read") else str(e)
        return e.code, body_text, elapsed
    except Exception as e:
        elapsed = time.time() - t0
        return 0, f"{type(e).__name__}: {e}", elapsed


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────


def parse_response(status: int, body: dict | str) -> dict:
    """Parse a response into a dict with:
       content (str), tool_call_name (str|None), prompt_tokens, completion_tokens
    """
    out = {"content": "", "tool_call_name": None, "prompt_tokens": None,
           "completion_tokens": None, "raw_preview": ""}

    if not isinstance(body, dict):
        out["raw_preview"] = str(body)[:200]
        return out

    out["raw_preview"] = json.dumps(body)[:200]

    # Ollama native format
    if "message" in body and isinstance(body["message"], dict):
        out["content"] = body["message"].get("content") or ""
        tc = body["message"].get("tool_calls")
        if tc:
            out["tool_call_name"] = tc[0].get("function", {}).get("name")
        out["prompt_tokens"] = body.get("prompt_eval_count")
        out["completion_tokens"] = body.get("eval_count")
        return out

    # OpenAI format
    if "choices" in body:
        msg = body["choices"][0].get("message", {})
        out["content"] = msg.get("content") or ""
        tc = msg.get("tool_calls")
        if tc:
            fn = tc[0].get("function", {})
            out["tool_call_name"] = fn.get("name")
        out["prompt_tokens"] = body.get("usage", {}).get("prompt_tokens")
        out["completion_tokens"] = body.get("usage", {}).get("completion_tokens")
        return out

    # Anthropic Messages format
    if "content" in body and isinstance(body["content"], list):
        for block in body["content"]:
            if block.get("type") == "text":
                out["content"] += block.get("text", "")
            elif block.get("type") == "tool_use":
                out["tool_call_name"] = block.get("name")
        out["prompt_tokens"] = body.get("usage", {}).get("input_tokens")
        out["completion_tokens"] = body.get("usage", {}).get("output_tokens")
        return out

    return out


def collect_streamed_ndjson(body_chunks: list[str]) -> dict:
    """Reconstruct a final Ollama-like body from NDJSON stream chunks."""
    out = {"message": {"content": "", "tool_calls": []},
           "prompt_eval_count": None, "eval_count": None}
    for chunk in body_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message", {})
        if msg.get("content"):
            out["message"]["content"] += msg["content"]
        if msg.get("tool_calls"):
            out["message"]["tool_calls"].extend(msg["tool_calls"])
        if obj.get("prompt_eval_count") is not None:
            out["prompt_eval_count"] = obj["prompt_eval_count"]
        if obj.get("eval_count") is not None:
            out["eval_count"] = obj["eval_count"]
    return out


def validate(parsed: dict, expect: dict) -> tuple[bool, str]:
    """Return (ok, reason). Checks content + tool_call + token range."""
    content_lower = (parsed["content"] or "").lower()
    tc = parsed["tool_call_name"]

    if "must_tool_call" in expect:
        want = expect["must_tool_call"]
        if tc is None:
            return False, f"expected tool_call={want}, got none"
        if tc.lower() != want.lower():
            return False, f"expected tool_call={want}, got {tc}"

    for needle in expect.get("must_contain", []):
        if needle.lower() not in content_lower and (
            tc is None or needle.lower() not in (tc or "").lower()
        ):
            return False, f"content missing '{needle}' (got: {parsed['content'][:80]!r})"

    ct = parsed.get("completion_tokens")
    if ct is not None:
        if "min_tokens" in expect and ct < expect["min_tokens"]:
            return False, f"too few tokens: {ct} < {expect['min_tokens']}"
        if "max_tokens" in expect and ct > expect["max_tokens"]:
            return False, f"too many tokens: {ct} > {expect['max_tokens']}"

    return True, "ok"


def run_suite(url: str, label: str, fixtures: list[dict], iterations: int = 1) -> None:
    """Run all fixtures iterations times, validate responses, print a report."""
    print()
    print(f"══════ {label} ══════")
    print(f"URL: {url}")
    print(f"Iterations per fixture: {iterations}")
    print()

    results = []
    for fixture in fixtures:
        expect = fixture.get("expect", {})
        for it in range(iterations):
            name = f"{fixture['name']}#{it + 1}" if iterations > 1 else fixture["name"]
            status, body, elapsed = post_and_parse(url, fixture["body"])

            if status != 200:
                # HTTP-level failure
                err_preview = (body if isinstance(body, str) else json.dumps(body))[:180]
                print(f"  ✗ {name:35s} {elapsed:6.1f}s  HTTP {status}")
                print(f"      {err_preview}")
                results.append({"name": name, "ok": False, "elapsed": elapsed})
                continue

            parsed = parse_response(status, body)
            ok, reason = validate(parsed, expect)

            pt = parsed.get("prompt_tokens")
            ct = parsed.get("completion_tokens")
            tc = parsed.get("tool_call_name")
            tok_s = f" p={pt} c={ct}" if pt or ct else ""
            tc_s = f" tool={tc}" if tc else ""

            status_char = "✓" if ok else "✗"
            print(f"  {status_char} {name:35s} {elapsed:6.1f}s{tok_s}{tc_s}")

            # Show response preview
            preview = parsed["content"][:150].replace("\n", " ")
            if preview:
                print(f"      > {preview}")
            if not ok:
                print(f"      VALIDATION FAILED: {reason}")

            results.append({"name": name, "ok": ok, "elapsed": elapsed,
                            "prompt_tokens": pt, "completion_tokens": ct})

    passed = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    elapsed_ok = [r["elapsed"] for r in passed]

    print()
    print(f"  {len(passed)}/{len(results)} passed  ({len(failed)} failed)")
    if elapsed_ok:
        print(
            f"  latency: min={min(elapsed_ok):.1f}s  "
            f"p50={statistics.median(elapsed_ok):.1f}s  "
            f"max={max(elapsed_ok):.1f}s"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--mode",
        choices=["direct", "fleet", "both"],
        default="both",
        help="direct = hit Ollama on this node; fleet = hit Herd router; both = run both",
    )
    parser.add_argument(
        "--ollama",
        default="http://localhost:11434",
        help="Local Ollama URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--router",
        default="http://localhost:11435",
        help="Herd router URL (default: http://localhost:11435)",
    )
    parser.add_argument(
        "--model",
        default="qwen3-coder:30b-agent",
        help="Model to test (default: qwen3-coder:30b-agent)",
    )
    parser.add_argument("-n", "--iterations", type=int, default=1, help="Iterations per fixture")
    args = parser.parse_args()

    fixtures = make_fixtures(args.model)

    print(f"Testing {args.model} with {len(fixtures)} fixture(s) × {args.iterations} iteration(s)")

    if args.mode in ("direct", "both"):
        run_suite(
            url=f"{args.ollama}/api/chat",
            label="Direct Ollama (bypasses Herd — tests OS/Ollama stability)",
            fixtures=fixtures,
            iterations=args.iterations,
        )

    if args.mode in ("fleet", "both"):
        run_suite(
            url=f"{args.router}/api/chat",
            label="Via Herd router (tests routing + node selection)",
            fixtures=fixtures,
            iterations=args.iterations,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
