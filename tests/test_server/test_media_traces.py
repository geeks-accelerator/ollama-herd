"""Image generation and transcription write a ``request_traces`` row.

Both are queued like LLM requests, but their process functions settled the
queue without tracing, so only a client that left while still queued left a
row (``dispatched_stream`` writes that one).  A completed, failed, or
mid-generation-cancelled job was in no trace, no dashboard traffic figure, no
error rate and no community rollup, and its ``X-Request-Id`` resolved to
nothing.  Since ``first_chunk`` cancels a job whose client leaves, that last
case also lost the in-memory event the per-type stats read.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from fleet_manager.models.request import InferenceRequest, QueueEntry, normalize_model_name
from fleet_manager.server.fleet_headers import REQUEST_ID_HEADER
from fleet_manager.server.routes import image_compat, transcription_compat
from fleet_manager.server.routes.routing import CLIENT_GONE_STATUS, first_chunk
from fleet_manager.server.streaming import StreamingProxy
from tests.conftest import make_heartbeat
from tests.test_server.test_request_id_header import (  # noqa: F401 -- fixture
    MODEL,
    _trace_rows,
    fleet,
)

IMAGE_MODEL = "z-image-turbo"
STT_MODEL = "qwen3-asr"


def _image_node(client) -> None:
    client.post("/dashboard/api/settings", json={"image_generation": True})
    hb = make_heartbeat(node_id="studio", loaded_models=[(MODEL, 18.0)]).model_dump()
    hb["image"] = {
        "models_available": [{"name": IMAGE_MODEL, "binary": "mflux-generate"}],
        "generating": False,
    }
    hb["image_port"] = 11436
    client.post("/heartbeat", json=hb)


def _stt_node(client) -> None:
    client.post("/dashboard/api/settings", json={"transcription": True})
    hb = make_heartbeat(node_id="studio", loaded_models=[(MODEL, 18.0)]).model_dump()
    hb["transcription"] = {
        "models_available": [{"name": STT_MODEL, "binary": "mlx-qwen3-asr"}],
        "transcribing": False,
    }
    hb["transcription_port"] = 11437
    client.post("/heartbeat", json=hb)


def _image(client):
    return client.post("/api/generate-image", json={"model": IMAGE_MODEL, "prompt": "a"})


def _transcribe(client):
    return client.post("/api/transcribe", files={"audio": ("a.wav", b"RIFF", "audio/wav")})


MEDIA = {
    "image": (_image_node, "generate_image_on_node", b"PNG", _image, IMAGE_MODEL),
    "stt": (_stt_node, "transcribe_on_node", {"text": "hello"}, _transcribe, STT_MODEL),
}


class TestRoutesWriteATraceRow:
    @pytest.mark.parametrize("kind", list(MEDIA))
    def test_completed(self, fleet, kind):  # noqa: F811
        setup, method, result, call, model = MEDIA[kind]
        setup(fleet)
        setattr(fleet.app.state.streaming_proxy, method, AsyncMock(return_value=result))
        resp = call(fleet)
        assert resp.status_code == 200, resp.text
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        # The queue entry's name: InferenceRequest normalizes it, as the queue key does.
        assert (row["status"], row["node_id"]) == ("completed", "studio")
        assert row["model"] == normalize_model_name(model)
        assert row["latency_ms"] >= 0

    @pytest.mark.parametrize("kind", list(MEDIA))
    def test_failed(self, fleet, kind):  # noqa: F811
        setup, method, _result, call, _model = MEDIA[kind]
        setup(fleet)
        setattr(
            fleet.app.state.streaming_proxy,
            method,
            AsyncMock(side_effect=httpx.ConnectError("refused")),
        )
        resp = call(fleet)
        assert resp.status_code == 502, resp.text
        [row] = _trace_rows(fleet, resp.headers[REQUEST_ID_HEADER])
        assert row["status"] == "failed"
        assert "refused" in row["error_message"]


def _entry(request_type: str) -> QueueEntry:
    body = {"_audio_bytes": b"RIFF", "_filename": "a.wav"} if request_type == "stt" else {}
    return QueueEntry(
        request=InferenceRequest(model="m", raw_body=body, request_type=request_type),
        assigned_node="studio",
    )


def _process(proxy: StreamingProxy, kind: str, queue_mgr):
    if kind == "image":
        return proxy.make_image_process_fn("studio:m", queue_mgr)
    return proxy.make_transcription_process_fn("studio:m", queue_mgr)


class TestClientLeavesMidGeneration:
    """``first_chunk`` cancels the job when the client leaves: traced
    ``client_disconnected``, settled, and the cancellation re-raised."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", list(MEDIA))
    async def test_traced_as_a_disconnect(self, kind):
        proxy = StreamingProxy(MagicMock())
        proxy._record_trace = MagicMock()
        queue_mgr = MagicMock()
        started = asyncio.Event()

        async def still_generating(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        setattr(proxy, MEDIA[kind][1], still_generating)
        entry = _entry(kind)

        async def is_disconnected():
            return started.is_set()

        stream = _process(proxy, kind, queue_mgr)(entry)
        request = SimpleNamespace(is_disconnected=is_disconnected)
        assert await first_chunk(request, stream, poll_s=0.01) is None

        [call] = proxy._record_trace.call_args_list
        assert call.args[4] == "client_disconnected"
        queue_mgr.mark_failed.assert_called_once_with("studio:m", entry)
        queue_mgr.mark_completed.assert_not_called()


class TestDisconnectIsAnEvent:
    """The per-type stats (``/dashboard/api/image-stats``, the health checks)
    read the in-memory events, and a disconnect is one of them too.  Its trace
    row is written where it happens (``dispatched_stream`` while queued, the
    process function mid-generation), so the route only adds the event."""

    @pytest.mark.parametrize(
        "kind,module,events",
        [
            ("image", image_compat, image_compat._image_gen_events),
            ("stt", transcription_compat, transcription_compat._transcription_events),
        ],
    )
    def test_route_records_the_disconnect(self, fleet, monkeypatch, kind, module, events):  # noqa: F811
        setup, method, result, call, model = MEDIA[kind]
        setup(fleet)
        setattr(fleet.app.state.streaming_proxy, method, AsyncMock(return_value=result))

        async def client_gone(*args, **kwargs):
            return None

        monkeypatch.setattr(module, "dispatched_stream", client_gone)
        events.clear()
        resp = call(fleet)
        assert resp.status_code == CLIENT_GONE_STATUS
        [event] = events
        assert (event["status"], event["model"], event["node_id"]) == (
            "client_disconnected",
            model,
            "studio",
        )


def test_the_debug_log_records_the_audio_size_not_the_audio(monkeypatch, tmp_path):
    """With ``FLEET_DEBUG_REQUEST_BODIES`` on, a traced transcription would
    otherwise write its whole audio file into the JSONL as a bytes repr."""
    from fleet_manager.server import streaming

    records = []
    monkeypatch.setattr(
        streaming.debug_log, "append_request", lambda **kw: records.append(kw["record"])
    )
    settings = SimpleNamespace(
        debug_request_bodies=True, data_dir=str(tmp_path), debug_request_retention_days=1
    )
    proxy = StreamingProxy(MagicMock(), settings=settings)
    proxy._record_trace(_entry("stt"), "studio", 0.0, None, "completed")
    [record] = records
    assert record["client_body"] == {"_audio_bytes": "<4 bytes>", "_filename": "a.wav"}
