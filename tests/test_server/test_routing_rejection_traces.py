"""A request rejected before a node is chosen must still leave a trace.

`record_trace` only runs after a winner is selected, so every rejection path was
invisible to the trace store. On 2026-09-28 four requests hit the 30s holding-queue
timeout and returned 503 to their clients while that day's traces showed *zero*
non-completed rows — the dashboard reported 100% success while bots got errors, and
several status reports repeated that figure.
"""

from types import SimpleNamespace

import pytest

from fleet_manager.server.routes.routing import (
    REJECTED_STATUS,
    record_routing_rejection,
)


class _Store:
    def __init__(self, boom=False):
        self.calls: list[dict] = []
        self._boom = boom

    async def record_trace(self, **kw):
        if self._boom:
            raise RuntimeError("database is locked")
        self.calls.append(kw)


def _req(**kw):
    return SimpleNamespace(
        request_id=kw.get("request_id", "rid-1"),
        model=kw.get("model", "gemma3:27b"),
        tags=kw.get("tags", ["bot-simulation", "inbed-bot"]),
    )


class TestRejectionTrace:
    async def test_records_with_a_distinct_status_and_a_reason(self):
        st = _Store()
        await record_routing_rejection(
            st, _req(), reason="no node could serve 'gemma3:27b' within the holding timeout",
            original_format="ollama", client_ip="::1",
        )
        assert len(st.calls) == 1
        c = st.calls[0]
        assert c["status"] == REJECTED_STATUS
        # distinct from a backend failure, so the two causes stay separable
        assert c["status"] != "failed"
        assert "holding timeout" in c["error_message"], "reason must not be empty"
        assert c["model"] == "gemma3:27b"
        assert c["original_format"] == "ollama"

    async def test_node_id_is_empty_because_none_was_chosen(self):
        st = _Store()
        await record_routing_rejection(st, _req(), reason="x")
        assert st.calls[0]["node_id"] == ""

    async def test_carries_tags_so_per_bot_analytics_see_the_rejection(self):
        st = _Store()
        await record_routing_rejection(st, _req(), reason="x")
        assert st.calls[0]["tags"] == ["bot-simulation", "inbed-bot"]

    async def test_counts_as_an_error_in_the_trace_store_math(self):
        """trace_store computes errors as `status != completed AND != retried`."""
        assert REJECTED_STATUS not in ("completed", "retried")

    async def test_a_trace_write_failure_never_escalates_the_response(self):
        """A 503 must not become a 500 because the trace could not be written."""
        await record_routing_rejection(_Store(boom=True), _req(), reason="x")

    async def test_no_trace_store_is_a_no_op(self):
        await record_routing_rejection(None, _req(), reason="x")


class TestAllRejectionSitesWired:
    """Every `if not results:` path must record. A new route must not silently skip it."""

    @pytest.mark.parametrize(
        "module",
        ["ollama_compat", "openai_compat", "anthropic_compat", "responses_compat"],
    )
    def test_route_module_records_rejections(self, module):
        import importlib
        import inspect

        m = importlib.import_module(f"fleet_manager.server.routes.{module}")
        src = inspect.getsource(m)
        n_reject = src.count("if not results:")
        n_record = src.count("record_routing_rejection(")
        assert n_record >= n_reject, (
            f"{module}: {n_reject} rejection block(s) but only {n_record} "
            f"record_routing_rejection call(s) — a rejection path is invisible"
        )


class TestRejectionsDoNotFakeANodeFault:
    """A node-less rejection must not create a phantom 100%-error-rate node.

    Wiring rejection traces in immediately produced exactly that: the per-node
    error-rate check groups by node_id, saw one row with node_id='' where the single
    request had "failed", and reported `High error rate on ␣ — 100.0% (1/1)`. Its
    remediation was "check Ollama health on ␣" — the wrong diagnosis for a request
    that never reached a node.
    """

    async def test_per_node_error_rate_excludes_empty_node_id(self, tmp_path):
        import time

        from fleet_manager.server.trace_store import TraceStore

        store = TraceStore(str(tmp_path / "t.db"))
        await store.initialize()
        try:
            # one healthy node request, one node-less rejection
            await store.record_trace(
                request_id="a", model="m", original_model="m", node_id="bb",
                status="completed", latency_ms=100.0,
            )
            await store.record_trace(
                request_id="b", model="m", original_model="m", node_id="",
                status="rejected", error_message="no node could serve 'm'",
            )
            time.sleep(0.05)
            rates = await store.get_error_rates_24h(lookback_s=3600)
            nodes = {r["node_id"] for r in rates}
            assert "" not in nodes, (
                f"node-less rejection leaked into per-node error rates: {rates}"
            )
            for r in rates:
                if r["node_id"] == "bb":
                    assert r["failed"] == 0, "healthy node must not inherit the rejection"
        finally:
            await store.close()


class TestEmbedTracesRecordSize:
    """Embed traces must carry the request size, or embed latency is unexplainable.

    The backend already computes `prompt_eval_count` and `/v1/embeddings` reports it
    as OpenAI `usage`, but it was never written to the trace: all 3,770 embed rows on
    the reference fleet had prompt_tokens NULL. On 2026-10-01 two spellings of the
    same model averaged 324 ms and 1,624 ms — same node, same backend, no contention
    — and there was no recorded way to test whether batch size explained it.
    """

    def test_backend_reports_a_count_we_can_record(self):
        """Guard the field name the trace now depends on."""
        import inspect

        from fleet_manager.node import text_embedding_server as tes

        src = inspect.getsource(tes)
        assert "prompt_eval_count" in src

    def test_embed_success_path_passes_prompt_tokens(self):
        import inspect

        from fleet_manager.server.routes import text_embedding_compat as tec

        src = inspect.getsource(tec)
        # the success-path record_trace must forward the size
        assert "prompt_tokens=prompt_tokens" in src, (
            "embed traces must record request size, not just latency"
        )
        assert 'result.get("prompt_eval_count")' in src
