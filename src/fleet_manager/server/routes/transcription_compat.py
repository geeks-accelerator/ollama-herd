"""Transcription routes — routes speech-to-text requests to the best available node."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat

logger = logging.getLogger(__name__)

router = APIRouter(tags=["transcription"])

# In-memory tracking of transcription events
_transcription_events: list[dict] = []
_MAX_TRANSCRIPTION_EVENTS = 200


def _record_transcription(
    model: str,
    node_id: str,
    status: str,
    processing_ms: int = 0,
    audio_duration_s: float = 0,
    error: str = "",
) -> None:
    """Record a transcription event for health monitoring."""
    _transcription_events.append({
        "timestamp": time.time(),
        "model": model,
        "node_id": node_id,
        "status": status,
        "processing_ms": processing_ms,
        "audio_duration_s": audio_duration_s,
        "error": error,
    })
    if len(_transcription_events) > _MAX_TRANSCRIPTION_EVENTS:
        del _transcription_events[: len(_transcription_events) - _MAX_TRANSCRIPTION_EVENTS]


def get_transcription_events(hours: float = 24) -> list[dict]:
    """Get transcription events from the last N hours."""
    cutoff = time.time() - hours * 3600
    return [e for e in _transcription_events if e["timestamp"] >= cutoff]


def _score_transcription_candidates(candidates) -> object:
    """Pick the best node for transcription."""
    scored = []
    for node in candidates:
        score = 0.0
        if node.transcription and node.transcription.transcribing:
            score -= 50.0
        if node.memory:
            score += node.memory.available_gb * 0.5
        if node.cpu:
            score -= node.cpu.utilization_pct * 0.2
        scored.append((score, node))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


@router.post("/api/transcribe")
async def transcribe_audio(request: Request, audio: UploadFile):
    """Transcribe an audio file on the best available node."""
    settings = request.app.state.settings

    if not settings.transcription:
        return JSONResponse(
            status_code=503,
            content={
                "error": "Transcription is disabled. "
                "Enable via FLEET_TRANSCRIPTION=true or the settings API."
            },
        )

    registry = request.app.state.registry

    # Find nodes with transcription capabilities
    candidates = [
        n
        for n in registry.get_online_nodes()
        if n.transcription
        and n.transcription_port > 0
        and n.transcription.models_available
    ]

    if not candidates:
        return JSONResponse(
            status_code=404,
            content={
                "error": "No transcription models available on any node. "
                "Install mlx-qwen3-asr: pip install 'mlx-qwen3-asr[serve]'"
            },
        )

    best = _score_transcription_candidates(candidates)
    model = candidates[0].transcription.models_available[0].name
    logger.info(f"Transcription: {audio.filename} → {best.node_id}")

    # Read audio bytes
    audio_bytes = await audio.read()
    filename = audio.filename or "audio.wav"

    # Create request for queue tracking
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        stream=False,
        original_format=RequestFormat.OLLAMA,
        raw_body={"_audio_bytes": audio_bytes, "_filename": filename},
        request_type="stt",
    )

    queue_key = f"{best.node_id}:{inference_req.model}"
    entry = QueueEntry(
        request=inference_req,
        assigned_node=best.node_id,
        routing_score=0.0,
    )

    proxy = request.app.state.streaming_proxy
    queue_mgr = request.app.state.queue_mgr
    process_fn = proxy.make_transcription_process_fn(
        queue_key, queue_mgr, timeout=settings.transcription_timeout
    )
    response_future = await queue_mgr.enqueue(entry, process_fn)

    start = time.monotonic()
    try:
        stream = await response_future
        result_json = ""
        async for chunk in stream:
            result_json = chunk

        elapsed_ms = int((time.monotonic() - start) * 1000)
        result = json.loads(result_json)

        _record_transcription(
            model, best.node_id, "completed",
            processing_ms=elapsed_ms,
        )

        return JSONResponse(
            content=result,
            headers={
                "X-Fleet-Node": best.node_id,
                "X-Fleet-Model": model,
                "X-Transcription-Time": str(elapsed_ms),
            },
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _record_transcription(
            model, best.node_id, "failed",
            processing_ms=elapsed_ms, error=repr(e),
        )
        logger.error(f"Transcription failed on {best.node_id}: {repr(e)}")
        return JSONResponse(
            status_code=502,
            content={"error": f"Transcription failed on {best.node_id}: {repr(e)}"},
        )
