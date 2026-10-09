"""Four context-window conditions that herd could not see.

From a field report by an unrelated project whose provider layer lost control of
the window by talking to Ollama over `/v1`, where an OpenAI-format request has
no field for it. That root cause doesn't reach herd — it calls `/api/chat` — but
four adjacent gaps did. See docs/plans/context-window-blindness.md.

The audit before building these halved the work: the overflow comparison already
existed and was merely unrecorded, and requested-vs-reported window was already
stored. So most of what is tested here is *persistence* of detections that were
already correct.
"""

import pathlib
from types import SimpleNamespace

import pytest

from fleet_manager.models.node import MemoryMetrics, MlxServerInfo
from fleet_manager.models.request import InferenceRequest
from fleet_manager.server.health_engine import HealthEngine, Severity


def _node(node_id="bb", loaded=(), free_gb=400.0):
    return SimpleNamespace(
        node_id=node_id,
        memory=MemoryMetrics(
            total_gb=512.0, used_gb=512.0 - free_gb, available_gb=free_gb
        ),
        ollama=SimpleNamespace(
            models_loaded=[
                SimpleNamespace(name=n, context_length=c, size_gb=1.0) for n, c in loaded
            ],
            models_available_meta={},
        ),
        mlx_servers=[],
    )


class TestPhase1EstimateIsCarried:
    """The router already computed this; it just never reached the trace."""

    def test_the_request_model_carries_it(self):
        r = InferenceRequest(model="m", messages=[{"role": "user", "content": "x"}])
        assert r.estimated_tokens is None, "None on paths that never score"

    def test_routing_stashes_the_value_it_already_computed(self):
        """Pin that no FOURTH estimator was introduced.

        Three already exist with different consumers: ScoringEngine for routing,
        _total_tokens for compaction triggers, and anthropic_translator's
        tiktoken one for the client-facing count_tokens endpoint.
        """
        src = pathlib.Path("src/fleet_manager/server/routes/routing.py").read_text()
        assert "inference_req.estimated_tokens = estimated_tokens" in src
        assert src.count("ScoringEngine.estimate_tokens(inference_req.messages)") >= 1

    def test_the_trace_records_it(self):
        src = pathlib.Path("src/fleet_manager/server/streaming.py").read_text()
        assert "estimated_tokens=entry.request.estimated_tokens" in src

    def test_the_overflow_detection_is_now_persisted(self):
        """It already logged and header'd; it never left a record."""
        src = pathlib.Path("src/fleet_manager/server/routes/routing.py").read_text()
        assert '_record_context_protection(\n        "overflow"' in src
        assert "X-Fleet-Context-Overflow" in src, "the client header must remain"


@pytest.mark.asyncio
class TestPhase1TraceRoundTrip:
    async def test_estimated_tokens_survives_a_write_and_read(self, tmp_path):
        from fleet_manager.server.trace_store import TraceStore

        store = TraceStore(str(tmp_path / "t.db"))
        await store.initialize()
        await store.record_trace(
            request_id="r1", model="m", original_model="m", node_id="bb",
            status="completed", prompt_tokens=73, completion_tokens=5,
            estimated_tokens=9,
        )
        rows = await store._read_db.execute(
            "SELECT estimated_tokens, prompt_tokens FROM request_traces"
        )
        got = await rows.fetchall()
        assert got == [(9, 73)], (
            "the estimate and the actual must sit together — the pair is what "
            "makes truncation answerable, and supplies threshold calibration"
        )
        await store.close()


class TestPhase2Unmanaged:
    @pytest.fixture
    def engine(self, monkeypatch):
        monkeypatch.setenv("FLEET_NUM_CTX_OVERRIDES", '{"gpt-oss:120b": 131072}')
        return HealthEngine()

    def test_a_model_with_traffic_and_no_override_is_flagged(self, engine):
        stats = [{"model": "qwen3.6:27b", "request_count": 400}]
        recs = engine._check_num_ctx_unmanaged([_node()], stats)
        assert len(recs) == 1
        assert recs[0].check_id == "num_ctx_unmanaged"
        assert "qwen3.6:27b" in recs[0].description

    def test_a_pinned_model_is_not_flagged(self, engine):
        stats = [{"model": "gpt-oss:120b", "request_count": 9999}]
        assert engine._check_num_ctx_unmanaged([_node()], stats) == []

    def test_an_unused_model_is_not_flagged(self, engine):
        """21 idle models on disk are not a problem; carding them is noise."""
        stats = [{"model": "qwen3.6:27b", "request_count": 0}]
        assert engine._check_num_ctx_unmanaged([_node()], stats) == []

    def test_native_embedding_models_are_excluded(self, engine):
        """The first live run fired on nomic-embed-text with 2,829 requests.

        `model_has_capability` is presence-only and nomic is served by the
        fastembed server, so Ollama reports no capability for it and the
        capability test returns False. The routing registry is authoritative.
        """
        stats = [
            {"model": "nomic-embed-text", "request_count": 2829},
            {"model": "nomic-embed-text:latest", "request_count": 183},
        ]
        assert engine._check_num_ctx_unmanaged([_node()], stats) == [], (
            "a model with no num_ctx to manage must never appear here"
        )

    def test_mlx_models_are_excluded(self, engine):
        """Their window is fixed at server launch; herd cannot set it."""
        stats = [{"model": "mlx:some/Model-4bit", "request_count": 500}]
        assert engine._check_num_ctx_unmanaged([_node()], stats) == []

    def test_severity_follows_headroom_not_count(self, engine):
        stats = [{"model": "qwen3.6:27b", "request_count": 400}]
        roomy = engine._check_num_ctx_unmanaged([_node(free_gb=400.0)], stats)
        tight = engine._check_num_ctx_unmanaged([_node(free_gb=5.0)], stats)
        assert roomy[0].severity is Severity.INFO
        assert tight[0].severity is Severity.WARNING

    def test_it_says_unmanaged_is_a_valid_choice(self, engine):
        """What does this fire on when everything is working? A deliberate
        decision. So the text must not imply breakage."""
        stats = [{"model": "qwen3.6:27b", "request_count": 400}]
        rec = engine._check_num_ctx_unmanaged([_node()], stats)[0]
        assert "deliberate state" in rec.description
        assert "valid choice" in rec.fix

    def test_no_stats_means_silence(self, engine):
        assert engine._check_num_ctx_unmanaged([_node()], None) == []


class TestPhase3MlxWindowUnknown:
    def test_none_means_cannot_be_known(self):
        """Not 0 — a zero would read as a real zero-length window."""
        srv = MlxServerInfo(port=11440, model="m", status="healthy")
        assert srv.context_length is None

    def test_it_can_be_populated_if_a_future_mlx_reports_one(self):
        srv = MlxServerInfo(port=11440, model="m", status="healthy", context_length=32768)
        assert srv.context_length == 32768


class TestPhase4EmptyGenerations:
    """`completion_tokens` is Ollama's eval_count, which includes reasoning.

    Verified live: a thinking model at num_predict=24 returned eval_count=24
    with zero content and 41 chars of thinking. So reading this field cannot
    misfire on a thinking model — reading content length would.
    """

    def _rel(self, no_output=0, total=100, by_model=None):
        return {
            "client_disconnected": 0,
            "incomplete": 0,
            "no_output": no_output,
            "total_requests": total,
            "by_model": by_model or {},
        }

    def test_one_empty_generation_is_surfaced(self):
        e = HealthEngine()
        rel = self._rel(1, by_model={"gpt-oss:120b": {"no_output": 1}})
        recs = [
            r for r in e._check_stream_reliability(rel, self._rel(1))
            if r.check_id == "empty_generations"
        ]
        assert len(recs) == 1
        assert recs[0].severity is Severity.WARNING  # still happening

    def test_historical_only_is_info_not_warning(self):
        e = HealthEngine()
        rel = self._rel(2, by_model={"m": {"no_output": 2}})
        rec = [
            r for r in e._check_stream_reliability(rel, self._rel(0))
            if r.check_id == "empty_generations"
        ][0]
        assert rec.severity is Severity.INFO

    def test_silent_when_none(self):
        e = HealthEngine()
        assert [
            r for r in e._check_stream_reliability(self._rel(0), self._rel(0))
            if r.check_id == "empty_generations"
        ] == []

    def test_a_reliability_dict_without_the_key_does_not_explode(self):
        """An older router's dict predates no_output; a KeyError here would
        take out the entire health pass."""
        e = HealthEngine()
        old = {
            "client_disconnected": 0, "incomplete": 0,
            "total_requests": 10, "by_model": {},
        }
        assert e._check_stream_reliability(old, old) == []

    def test_null_completion_tokens_is_not_zero(self):
        """48,449 completed traces on the reference fleet carry NULL counts
        (embeddings, MLX). Treating NULL as zero would classify every one."""
        src = pathlib.Path("src/fleet_manager/server/streaming.py").read_text()
        assert "completion_tokens is not None and completion_tokens == 0" in src

    def test_it_reads_eval_count_not_content_length(self):
        """The trap the plan warned about, pinned."""
        src = pathlib.Path("src/fleet_manager/server/streaming.py").read_text()
        block = src[src.index("produced_nothing ="):]
        block = block[: block.index("self._record_trace")]
        assert "output_token_count" not in block, (
            "reading content length would mislabel every thinking model"
        )

    def test_it_extends_the_existing_status_set(self):
        """A fourth value joins incomplete/client_disconnected rather than
        getting its own query and check."""
        src = pathlib.Path("src/fleet_manager/server/trace_store.py").read_text()
        assert "'client_disconnected', 'incomplete', 'no_output'" in src
