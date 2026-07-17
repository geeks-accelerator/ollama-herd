"""Tests for the fleet read + pin API (client-ergonomics batch 2)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from fleet_manager.server.serializers import OLLAMA_HOT_MODEL_CAP
from tests.conftest import make_heartbeat
from tests.test_server.test_routes import create_test_app

# ---------------------------------------------------------------------------
# serialize_node via /fleet/status — derived slot fields on a real node
# ---------------------------------------------------------------------------


def test_fleet_status_reports_free_slots_per_node():
    with TestClient(create_test_app()) as c:
        c.post("/heartbeat", json=make_heartbeat(
            node_id="bb",
            loaded_models=[("gpt-oss:120b", 71.0), ("gemma3:27b", 39.0)],
            available_models=["gpt-oss:120b", "gemma3:27b"],
        ).model_dump())
        node = c.get("/fleet/status").json()["nodes"][0]
        assert node["models_loaded_count"] == 2
        assert node["free_slots"] == OLLAMA_HOT_MODEL_CAP - 2


# ---------------------------------------------------------------------------
# /fleet/limits + /fleet/status — read API
# ---------------------------------------------------------------------------


def test_fleet_limits_reports_constraints():
    with TestClient(create_test_app()) as c:
        r = c.get("/fleet/limits")
        assert r.status_code == 200
        body = r.json()
        assert body["hot_model_cap"] == OLLAMA_HOT_MODEL_CAP  # fleet-level fallback
        assert "max_retries" in body
        assert "mlx_max_inflight_per_model" in body
        assert isinstance(body["nodes"], list)


def test_fleet_status_serializes_without_error():
    with TestClient(create_test_app()) as c:
        r = c.get("/fleet/status")
        assert r.status_code == 200
        body = r.json()
        assert "fleet" in body and "nodes" in body and "queues" in body


# ---------------------------------------------------------------------------
# /fleet/pin + /fleet/pin/{model} — model management API
# ---------------------------------------------------------------------------


def test_fleet_pin_requires_model():
    with TestClient(create_test_app()) as c:
        r = c.post("/fleet/pin", json={})
        assert r.status_code == 400
        assert r.json()["ok"] is False


def test_fleet_pin_404_when_model_not_on_disk():
    # Empty fleet → the model is nowhere on disk → explicit 404, not a substitute.
    with TestClient(create_test_app()) as c:
        r = c.post("/fleet/pin", json={"model": "nonexistent:999b"})
        assert r.status_code == 404
        assert "not on disk" in r.json()["error"]


def test_fleet_unpin_accepts_slashed_model_names():
    # DELETE with a name containing '/' and ':' must route (model:path).
    with TestClient(create_test_app()) as c:
        r = c.delete("/fleet/pin/mlx-community/Qwen3-Coder-30B:latest")
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# /fleet/pin readiness (fleet-pin-readiness.md) — wait mode + mlx no-op
# ---------------------------------------------------------------------------


def test_fleet_pin_mlx_model_is_noop_and_reports_readiness():
    # mlx: models are always-resident subprocesses — pin must NOT warm (no
    # Ollama call), NOT 404, and report readiness from mlx_servers health.
    # Empty fleet → no healthy server → ready False, with an explanatory note.
    with TestClient(create_test_app()) as c:
        r = c.post("/fleet/pin", json={"model": "mlx:some/Model-4bit"})
        assert r.status_code == 200
        b = r.json()
        assert b["ok"] is True
        assert b["ready"] is False
        assert "note" in b


def test_fleet_pin_wait_returns_ready_fields_and_persists(monkeypatch):
    # wait=true blocks until residency is confirmed, then returns ready +
    # ready_after_ms and (finding #2) persists the pin on the resident node.
    async def _fake_load(*_a, **_k):
        return True  # avoid a real Ollama pre_warm; residency comes from heartbeat

    monkeypatch.setattr(
        "fleet_manager.server.model_preloader._load_model_on_best_node", _fake_load,
    )
    with TestClient(create_test_app()) as c:
        c.post("/heartbeat", json=make_heartbeat(
            node_id="bb",
            loaded_models=[("foo:1b", 2.0)],
            available_models=["foo:1b"],
        ).model_dump())
        r = c.post("/fleet/pin", json={"model": "foo:1b", "wait": True, "timeout_s": 5})
        assert r.status_code == 200
        b = r.json()
        assert b["ready"] is True
        assert "ready_after_ms" in b
        assert b["pinned_node"] == "bb"


def test_fleet_pin_no_wait_omits_ready_fields(monkeypatch):
    async def _fake_load(*_a, **_k):
        return True

    monkeypatch.setattr(
        "fleet_manager.server.model_preloader._load_model_on_best_node", _fake_load,
    )
    with TestClient(create_test_app()) as c:
        c.post("/heartbeat", json=make_heartbeat(
            node_id="bb",
            loaded_models=[("foo:1b", 2.0)],
            available_models=["foo:1b"],
        ).model_dump())
        r = c.post("/fleet/pin", json={"model": "foo:1b"})
        assert r.status_code == 200
        b = r.json()
        assert b["ok"] is True
        assert "ready" not in b
        assert "ready_after_ms" not in b


def test_fleet_pin_on_disk_but_wont_fit_reports_truthfully_not_not_on_disk():
    """Regression (2026-07-17): a pin for a model that IS on disk was refused
    with "'X' is not on disk — run 'ollama pull X'" when the real cause was the
    memory gate.  _load_model_on_best_node returns a bare False for several
    causes; the route must not report them all as not-on-disk."""
    with TestClient(create_test_app()) as c:
        # Model on disk, NOT loaded, and the node has far too little free RAM
        # for a 120B (gate needs ~86GB free).
        # Big node (so pin admission passes) but almost no FREE memory, so the
        # preloader's memory gate is what refuses — the path under test.
        c.post("/heartbeat", json=make_heartbeat(
            node_id="bb",
            memory_total=1000.0,
            memory_used=990.0,  # → 10GB available
            loaded_models=[],
            available_models=["gpt-oss:120b"],
        ).model_dump())
        r = c.post("/fleet/pin", json={"model": "gpt-oss:120b", "node_id": "bb"})
        assert r.status_code == 503  # not 404, not 409
        err = r.json()["error"]
        assert "not on disk" not in err  # the false claim is gone
        assert "could not be loaded" in err
        assert "memory" in err.lower()


# ---------------------------------------------------------------------------
# Hot-model cap comes from the node, not a hardcoded guess (2026-07-17)
# ---------------------------------------------------------------------------


def test_hot_model_cap_uses_node_reported_value():
    """Regression: the cap was hardcoded to 3 on the claim that macOS Ollama
    ignores OLLAMA_MAX_LOADED_MODELS. Disproven on 0.32.1 (10 set → 4 residents
    observed). Nodes now report their real cap and free_slots must follow it."""
    from types import SimpleNamespace

    from fleet_manager.server.serializers import (
        OLLAMA_HOT_MODEL_CAP,
        hot_model_cap_for,
    )

    node = SimpleNamespace(ollama=SimpleNamespace(max_loaded_models=10))
    assert hot_model_cap_for(node) == 10  # node's truth wins

    # Older agents report 0 → fall back to Ollama's documented default.
    assert hot_model_cap_for(SimpleNamespace(ollama=SimpleNamespace(max_loaded_models=0))) == \
        OLLAMA_HOT_MODEL_CAP
    assert hot_model_cap_for(SimpleNamespace(ollama=None)) == OLLAMA_HOT_MODEL_CAP
    assert hot_model_cap_for(None) == OLLAMA_HOT_MODEL_CAP


def test_fleet_pin_rejects_unknown_node_id():
    """A pin to a node that doesn't exist used to be accepted and rot in the
    store forever (found: gemma3:27b pinned to 'Neons-Mac-Studio' long after
    that node became 'bb')."""
    with TestClient(create_test_app()) as c:
        c.post("/heartbeat", json=make_heartbeat(
            node_id="bb", loaded_models=[], available_models=["foo:1b"],
        ).model_dump())
        r = c.post("/fleet/pin", json={"model": "foo:1b", "node_id": "Neons-Mac-Studio"})
        assert r.status_code == 400
        b = r.json()
        assert "not in the fleet" in b["error"]
        assert b["known_nodes"] == ["bb"]


def test_fleet_pin_refuses_overcommitting_the_node():
    """Admission control: pins must physically co-reside. A 290GB model on a
    100GB node is the 2026-07-17 thrash loop in miniature."""
    with TestClient(create_test_app()) as c:
        hb = make_heartbeat(
            node_id="bb",
            memory_total=100.0,
            memory_used=10.0,
            loaded_models=[],
            available_models=["huge:480b"],
        ).model_dump()
        # Node reports the real on-disk size — the whole point of the fix.
        hb["ollama"]["models_available_sizes"] = {"huge:480b": 290.0}
        c.post("/heartbeat", json=hb)

        r = c.post("/fleet/pin", json={"model": "huge:480b", "node_id": "bb"})
        assert r.status_code == 409
        b = r.json()
        assert b["ok"] is False
        assert "would bring the pinned set" in b["error"]
        assert b["requested_gb"] == 348.0  # resident cost: 290 * 1.2, not disk size
        assert "unpin" in b["error"].lower()


def test_fleet_pin_force_overrides_admission():
    """Operators can override when they know better."""
    with TestClient(create_test_app()) as c:
        hb = make_heartbeat(
            node_id="bb", memory_total=100.0, memory_used=10.0,
            loaded_models=[], available_models=["huge:480b"],
        ).model_dump()
        hb["ollama"]["models_available_sizes"] = {"huge:480b": 290.0}
        c.post("/heartbeat", json=hb)
        r = c.post("/fleet/pin", json={"model": "huge:480b", "node_id": "bb", "force": True})
        assert r.status_code != 409  # admission bypassed (load may still fail)


def test_fleet_pin_refuses_the_real_world_512gb_overcommit(monkeypatch):
    """The exact case live testing caught: on a 512GB box, gpt-oss (65GB) is
    already pinned and a 290GB model is requested. Summing raw DISK sizes gives
    355GB < 410GB budget → wrongly allowed. Resident cost (x1.2) is 427GB >
    410GB → correctly refused. This is the 2026-07-17 thrash loop's exact shape.
    """
    async def _fake_load(*_a, **_k):
        return True  # don't pre_warm against the real Ollama on this box

    monkeypatch.setattr(
        "fleet_manager.server.model_preloader._load_model_on_best_node", _fake_load,
    )
    with TestClient(create_test_app()) as c:
        hb = make_heartbeat(
            node_id="bb",
            memory_total=512.0,
            memory_used=100.0,
            loaded_models=[("gpt-oss:120b", 65.4)],
            available_models=["gpt-oss:120b", "qwen3-coder:480b-a35b-q4_K_M"],
        ).model_dump()
        hb["ollama"]["models_available_sizes"] = {
            "gpt-oss:120b": 65.4,
            "qwen3-coder:480b-a35b-q4_K_M": 290.1,
        }
        c.post("/heartbeat", json=hb)
        c.post("/fleet/pin", json={"model": "gpt-oss:120b", "node_id": "bb", "force": True})

        r = c.post("/fleet/pin", json={
            "model": "qwen3-coder:480b-a35b-q4_K_M", "node_id": "bb",
        })
        assert r.status_code == 409, "355GB of raw weights 'fits' 512GB; resident cost does not"
        assert "evict and reload them in a loop" in r.json()["error"]
