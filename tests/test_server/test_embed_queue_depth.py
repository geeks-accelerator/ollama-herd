"""Embeddings count in the queue depth the scorer reads.

``dispatch_embed`` sends each embedding straight to the node's Ollama rather
than through a queue worker, so until ``QueueManager.direct`` the scorer saw no
load from embeddings at all.  On the 2026-11-21 fleet benchmark (run 5) that
stranded them: the first embeddings of a run went to an A10 that also held the
coding and image models, each took about 15 s there, and every later one
followed (queue 0, affinity) while an A100 had the model loaded and idle.
See docs/plans/fleet-benchmark-findings-2026-11.md, item 31.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.server.queue_manager import QueueManager
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama
from tests.test_server.test_routes import create_test_app

MODEL = "nomic-embed-text:latest"
OK = {"model": MODEL, "embeddings": [[0.1, 0.2]], "prompt_eval_count": 2}


class TestDirect:
    def test_counted_in_depth_while_it_runs(self):
        qm = QueueManager()
        with qm.direct("a", MODEL):
            assert qm.get_queue_depths()[f"a:{MODEL}"] == 1
            with qm.direct("a", MODEL):
                assert qm.get_queue_depths()[f"a:{MODEL}"] == 2
            assert qm.get_queue_info()[f"a:{MODEL}"]["in_flight"] == 1
        assert qm.get_queue_depths()[f"a:{MODEL}"] == 0

    def test_released_when_the_request_raises(self):
        qm = QueueManager()
        with pytest.raises(RuntimeError), qm.direct("a", MODEL):
            raise RuntimeError("node went away")
        assert qm.get_queue_depths()[f"a:{MODEL}"] == 0

    def test_never_looks_like_a_load(self):
        """An embedding at a node is not a load another model's keep_alive would cancel."""
        qm = QueueManager()
        with qm.direct("a", MODEL):
            assert not qm.load_pending_on("a", "qwen3-coder:30b")


@pytest.fixture
def client(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as c:
        for node in ("a", "b"):
            hb = make_heartbeat(
                node_id=node, loaded_models=[(MODEL, 0.3)], available_models=[MODEL]
            )
            c.post("/heartbeat", json=hb.model_dump())
        yield c


def _route(client: TestClient, status: int = 200):
    """Send one embedding; while it is at its node, record that node's depth and
    where the scorer would send the next one."""
    seen: dict = {}

    def handler(request: httpx.Request):
        qm = client.app.state.queue_mgr
        depths = qm.get_queue_depths()
        seen["depths"] = {k: v for k, v in depths.items() if v}
        seen["next"] = client.app.state.scorer.score_request(MODEL, depths)[0].node_id
        return httpx.Response(status, json=OK if status == 200 else {"error": "boom"})

    for node in ("a", "b"):
        _install_mock_ollama(client, node, handler)
    resp = client.post("/api/embed", json={"model": MODEL, "input": "hello"})
    return resp, seen


class TestRouting:
    def test_an_embedding_in_flight_steers_the_next_one_away(self, client):
        resp, seen = _route(client)
        assert resp.status_code == 200, resp.text
        first = resp.headers["X-Fleet-Node"]
        assert seen["depths"] == {f"{first}:{MODEL}": 1}
        assert seen["next"] != first

    @pytest.mark.parametrize("status", [200, 500])
    def test_depth_returns_to_zero_however_it_ends(self, client, status):
        resp, _ = _route(client, status)
        assert resp.status_code == status
        assert not any(client.app.state.queue_mgr.get_queue_depths().values())
