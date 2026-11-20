#!/usr/bin/env python3
"""A stand-in Ollama that answers every chat after a fixed delay.

For measuring the router's own overhead the way gateway vendors do: against an
upstream whose latency is known and constant, so whatever a request costs on
top of it is the gateway's.  It speaks just enough of Ollama's API for
herd-node to register it (``/``, ``/api/version``, ``/api/tags``, ``/api/ps``)
and answers ``/api/chat`` with a two-line stream after ``--delay-ms``,
reporting that delay as ``total_duration`` like Ollama does.

    python scripts/mock_ollama.py --port 11500 --delay-ms 20
    FLEET_NODE_OLLAMA_HOST=http://127.0.0.1:11500 herd-node   # registers "mock:latest"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

MODEL = "mock:latest"


def build_app(delay_ms: float) -> FastAPI:
    app = FastAPI()
    tag = {
        "name": MODEL,
        "model": MODEL,
        "size": 1_000_000,
        "digest": "0" * 64,
        "details": {"family": "mock", "format": "gguf", "parameter_size": "1M"},
    }

    @app.get("/")
    async def root():
        return PlainTextResponse("Ollama is running")

    @app.get("/api/version")
    async def version():
        return {"version": "0.0.0-mock"}

    @app.get("/api/tags")
    async def tags():
        return {"models": [tag]}

    @app.get("/api/ps")
    async def ps():
        return {"models": [{**tag, "size_vram": 0, "context_length": 4096}]}

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        start = time.perf_counter()
        await asyncio.sleep(delay_ms / 1000)
        done = {
            "model": body.get("model", MODEL),
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 5,
            "eval_count": 1,
            "total_duration": int((time.perf_counter() - start) * 1e9),
        }
        first = {
            "model": done["model"],
            "message": {"role": "assistant", "content": "ok"},
            "done": False,
        }
        if body.get("stream") is False:
            return JSONResponse({**done, "message": first["message"]})
        lines = json.dumps(first) + "\n" + json.dumps(done) + "\n"
        return StreamingResponse(iter([lines]), media_type="application/x-ndjson")

    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=11500)
    p.add_argument("--delay-ms", type=float, default=20.0)
    args = p.parse_args()
    uvicorn.run(build_app(args.delay_ms), host=args.host, port=args.port, log_level="warning")
