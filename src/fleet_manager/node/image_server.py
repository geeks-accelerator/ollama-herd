"""Image generation server — wraps mflux CLI as an HTTP endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter()

# Model name → binary mapping
_MODEL_BINARIES: dict[str, list[str]] = {
    # mflux models
    "z-image-turbo": ["mflux-generate-z-image-turbo"],
    "flux-dev": ["mflux-generate", "--model", "dev"],
    "flux-schnell": ["mflux-generate", "--model", "schnell"],
    # DiffusionKit models (Stable Diffusion 3.x via MLX)
    "sd3-medium": [
        "diffusionkit-cli",
        "--model-version",
        "argmaxinc/mlx-stable-diffusion-3-medium",
    ],
    "sd3.5-large": [
        "diffusionkit-cli",
        "--model-version",
        "argmaxinc/mlx-stable-diffusion-3.5-large",
        "--t5",
    ],
}


def _is_diffusionkit(cmd_parts: list[str]) -> bool:
    """Check if a command uses the DiffusionKit backend."""
    return cmd_parts[0] == "diffusionkit-cli"


def _resolve_binary(model: str) -> list[str] | None:
    """Resolve a model name to the mflux CLI command."""
    parts = _MODEL_BINARIES.get(model)
    if not parts:
        return None
    # Verify the binary exists
    if not shutil.which(parts[0]):
        return None
    return parts


@router.post("/api/generate-image")
async def generate_image(request: Request):
    """Generate an image using mflux and return PNG bytes."""
    body = await request.json()

    model = body.get("model", "z-image-turbo")
    prompt = body.get("prompt", "")
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "prompt is required"})

    cmd_parts = _resolve_binary(model)
    if not cmd_parts:
        return JSONResponse(
            status_code=404,
            content={"error": f"Image model '{model}' not available on this node"},
        )

    # Build CLI arguments
    width = body.get("width", 1024)
    height = body.get("height", 1024)
    steps = body.get("steps", 4)
    guidance = body.get("guidance")
    seed = body.get("seed")
    quantize = body.get("quantize", 8)
    negative_prompt = body.get("negative_prompt", "")

    output_path = os.path.join(tempfile.gettempdir(), f"herd-image-{uuid.uuid4().hex}.png")

    if _is_diffusionkit(cmd_parts):
        # DiffusionKit CLI flags
        cmd = [
            *cmd_parts,
            "--prompt",
            prompt,
            "--width",
            str(width),
            "--height",
            str(height),
            "--steps",
            str(steps),
            "--output-path",
            output_path,
        ]
        if guidance is not None:
            cmd += ["--cfg", str(guidance)]
        if seed is not None:
            cmd += ["--seed", str(seed)]
        if negative_prompt:
            cmd += ["--negative_prompt", negative_prompt]
    else:
        # mflux CLI flags (default)
        cmd = [
            *cmd_parts,
            "--prompt",
            prompt,
            "--width",
            str(width),
            "--height",
            str(height),
            "--steps",
            str(steps),
            "--quantize",
            str(quantize),
            "--output",
            output_path,
        ]
        if guidance is not None:
            cmd += ["--guidance", str(guidance)]
        if seed is not None:
            cmd += ["--seed", str(seed)]
        if negative_prompt:
            cmd += ["--negative-prompt", negative_prompt]

    logger.info(f"Image generation: model={model} size={width}x{height} steps={steps}")
    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if proc.returncode != 0:
            error_msg = stderr.decode(errors="replace").strip()
            logger.error(f"Image generation failed: {error_msg}")
            return JSONResponse(
                status_code=500,
                content={"error": f"mflux failed: {error_msg}"},
            )

        if not os.path.exists(output_path):
            return JSONResponse(
                status_code=500,
                content={"error": "mflux completed but no output file was produced"},
            )

        with open(output_path, "rb") as f:
            png_bytes = f.read()

        logger.info(f"Image generated: {len(png_bytes)} bytes in {elapsed_ms}ms")

        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "X-Generation-Time": str(elapsed_ms),
                "X-Image-Model": model,
                "X-Image-Size": f"{width}x{height}",
            },
        )
    except TimeoutError:
        logger.error("Image generation timed out after 180s")
        return JSONResponse(status_code=504, content={"error": "Image generation timed out"})
    except Exception as e:
        logger.error(f"Image generation error: {repr(e)}")
        return JSONResponse(status_code=500, content={"error": repr(e)})
    finally:
        # Clean up temp file
        if os.path.exists(output_path):
            with contextlib.suppress(OSError):
                os.unlink(output_path)
