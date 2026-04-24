"""Tests for MlxServerSpec, memory gate, and MlxSupervisorSet.

Covers Phase 1 of the multi-MLX-server work.  We don't actually spawn
mlx_lm.server (needs real model weights + minutes per run) — instead we
monkey-patch ``MlxSupervisor.start`` so the set's orchestration logic is
exercised without the heavy lifting.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fleet_manager.node.mlx_supervisor import (
    MlxServerSpec,
    MlxSupervisor,
    MlxSupervisorSet,
    available_memory_gb,
    estimate_model_size_gb,
    memory_gate_ok,
)

# ---------------------------------------------------------------------------
# MlxServerSpec.from_dict — required keys, defaults
# ---------------------------------------------------------------------------


def test_spec_from_dict_minimal():
    s = MlxServerSpec.from_dict({"model": "foo/bar", "port": 11440})
    assert s.model == "foo/bar"
    assert s.port == 11440
    assert s.kv_bits == 0
    assert s.draft_model == ""


def test_spec_from_dict_all_fields():
    s = MlxServerSpec.from_dict({
        "model": "foo/bar",
        "port": 11440,
        "kv_bits": 8,
        "prompt_cache_size": 8,
        "prompt_cache_bytes": 2**30,
        "draft_model": "foo/draft",
        "num_draft_tokens": 6,
    })
    assert s.kv_bits == 8
    assert s.prompt_cache_size == 8
    assert s.prompt_cache_bytes == 2**30
    assert s.draft_model == "foo/draft"
    assert s.num_draft_tokens == 6


def test_spec_from_dict_missing_model_raises():
    with pytest.raises(ValueError, match="missing 'model'"):
        MlxServerSpec.from_dict({"port": 11440})


def test_spec_from_dict_empty_model_raises():
    with pytest.raises(ValueError, match="missing 'model'"):
        MlxServerSpec.from_dict({"model": "  ", "port": 11440})


def test_spec_from_dict_bad_port_raises():
    with pytest.raises(ValueError, match="invalid 'port'"):
        MlxServerSpec.from_dict({"model": "foo/bar", "port": -1})
    with pytest.raises(ValueError, match="invalid 'port'"):
        MlxServerSpec.from_dict({"model": "foo/bar", "port": "11440"})


# ---------------------------------------------------------------------------
# Memory gate — mocked psutil + disk walk
# ---------------------------------------------------------------------------


def test_memory_gate_ok_when_fits():
    with patch(
        "fleet_manager.node.mlx_supervisor.estimate_model_size_gb",
        return_value=16.0,
    ), patch(
        "fleet_manager.node.mlx_supervisor.available_memory_gb",
        return_value=100.0,
    ):
        ok, reason = memory_gate_ok("any/model", headroom_gb=10.0)
    assert ok is True
    assert reason == ""


def test_memory_gate_blocks_when_too_tight():
    with patch(
        "fleet_manager.node.mlx_supervisor.estimate_model_size_gb",
        return_value=95.0,
    ), patch(
        "fleet_manager.node.mlx_supervisor.available_memory_gb",
        return_value=100.0,
    ):
        ok, reason = memory_gate_ok("big/model", headroom_gb=10.0)
    assert ok is False
    assert "95.0 GB" in reason
    assert "10.0 GB headroom" in reason
    assert "100.0 GB available" in reason


def test_memory_gate_passes_when_size_unknown():
    # Unknown model size (not in HF cache) → proceed
    with patch(
        "fleet_manager.node.mlx_supervisor.estimate_model_size_gb",
        return_value=0.0,
    ):
        ok, _ = memory_gate_ok("unknown/model", headroom_gb=10.0)
    assert ok is True


def test_memory_gate_passes_when_psutil_fails():
    # Available = 0.0 means psutil failed; don't block
    with patch(
        "fleet_manager.node.mlx_supervisor.estimate_model_size_gb",
        return_value=16.0,
    ), patch(
        "fleet_manager.node.mlx_supervisor.available_memory_gb",
        return_value=0.0,
    ):
        ok, _ = memory_gate_ok("any/model", headroom_gb=10.0)
    assert ok is True


def test_estimate_model_size_returns_zero_for_missing_cache(tmp_path):
    # Path-like arg that doesn't exist → 0.0 (not an error)
    assert estimate_model_size_gb(str(tmp_path / "missing")) == 0.0


def test_estimate_model_size_walks_blobs_dir(tmp_path):
    # Create the full HF cache layout the helper expects:
    #   <home>/.cache/huggingface/hub/models--org--model/blobs/<file>
    fake_cache = tmp_path / ".cache" / "huggingface" / "hub" / "models--org--model"
    blobs = fake_cache / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "weight-a").write_bytes(b"x" * (1024 * 1024 * 100))  # 100 MB
    (blobs / "weight-b").write_bytes(b"x" * (1024 * 1024 * 50))   # 50 MB
    # Monkey-patch Path.home so the helper rewrites its lookup into tmp_path
    with patch(
        "fleet_manager.node.mlx_supervisor.Path.home",
        return_value=tmp_path,
    ):
        size_gb = estimate_model_size_gb("org/model")
    # 150 MB → ~0.146 GB
    assert 0.13 < size_gb < 0.16


def test_available_memory_gb_is_positive_on_real_system():
    # Smoke test — should not raise and should return a positive number
    v = available_memory_gb()
    assert v > 0.0


# ---------------------------------------------------------------------------
# MlxSupervisorSet — orchestration with mocked child.start()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_start_all_parallel_happy_path():
    # Two specs, both children start successfully.
    async def _ok_start(self):
        self._status = "healthy"
        return True

    specs = [
        MlxServerSpec(model="a/main", port=11440),
        MlxServerSpec(model="a/compactor", port=11441),
    ]
    ss = MlxSupervisorSet(specs, bind_host="127.0.0.1", memory_headroom_gb=0.0)
    with patch.object(MlxSupervisor, "start", _ok_start):
        result = await ss.start_all()
    assert result == {11440: True, 11441: True}
    statuses = ss.statuses()
    assert len(statuses) == 2
    assert all(s.status == "healthy" for s in statuses)


@pytest.mark.asyncio
async def test_set_one_failure_does_not_block_others():
    # Compactor fails (memory gate), main succeeds — set reports both.
    async def _mixed_start(self):
        if self.port == 11441:
            self._status = "memory_blocked"
            self._status_reason = "memory gate: estimated way too much"
            return False
        self._status = "healthy"
        return True

    specs = [
        MlxServerSpec(model="a/main", port=11440),
        MlxServerSpec(model="a/compactor", port=11441),
    ]
    ss = MlxSupervisorSet(specs, bind_host="127.0.0.1", memory_headroom_gb=0.0)
    with patch.object(MlxSupervisor, "start", _mixed_start):
        result = await ss.start_all()
    assert result == {11440: True, 11441: False}
    statuses = {s.port: s for s in ss.statuses()}
    assert statuses[11440].status == "healthy"
    assert statuses[11441].status == "memory_blocked"
    assert "too much" in statuses[11441].status_reason


@pytest.mark.asyncio
async def test_set_dedups_duplicate_ports():
    async def _ok(self):
        self._status = "healthy"
        return True

    specs = [
        MlxServerSpec(model="a/first", port=11440),
        MlxServerSpec(model="a/second", port=11440),  # same port — must drop
    ]
    ss = MlxSupervisorSet(specs, bind_host="127.0.0.1", memory_headroom_gb=0.0)
    with patch.object(MlxSupervisor, "start", _ok):
        result = await ss.start_all()
    # Only the first port 11440 spec made it through
    assert set(result.keys()) == {11440}
    # statuses() still lists both specs (second shows as "stopped" since no child)
    assert len(ss.statuses()) == 2


@pytest.mark.asyncio
async def test_set_empty_specs_no_op():
    ss = MlxSupervisorSet([], bind_host="127.0.0.1", memory_headroom_gb=0.0)
    assert await ss.start_all() == {}
    await ss.stop_all()  # must not raise
    assert ss.statuses() == []


@pytest.mark.asyncio
async def test_set_healthy_models_only_includes_healthy():
    async def _mixed(self):
        if self.port == 11442:
            self._status = "unhealthy"
            return False
        self._status = "healthy"
        return True

    specs = [
        MlxServerSpec(model="a/main", port=11440),
        MlxServerSpec(model="a/helper", port=11441),
        MlxServerSpec(model="a/broken", port=11442),
    ]
    ss = MlxSupervisorSet(specs, bind_host="127.0.0.1", memory_headroom_gb=0.0)
    with patch.object(MlxSupervisor, "start", _mixed):
        await ss.start_all()
    mapping = ss.healthy_models()
    assert mapping == {"a/main": 11440, "a/helper": 11441}


@pytest.mark.asyncio
async def test_supervisor_memory_gate_blocks_start():
    # With a stringent headroom and a large estimated size, start() refuses
    # to spawn the subprocess AND sets status=memory_blocked so the heartbeat
    # surfaces the reason.
    sup = MlxSupervisor(
        model="fake/huge-model",
        port=11440,
        memory_headroom_gb=10.0,
    )
    with patch(
        "fleet_manager.node.mlx_supervisor.find_mlx_lm_binary",
        return_value="/fake/bin",
    ), patch(
        "fleet_manager.node.mlx_supervisor.estimate_model_size_gb",
        return_value=1_000_000.0,  # pretend it's 1 PB
    ), patch(
        "fleet_manager.node.mlx_supervisor.available_memory_gb",
        return_value=32.0,
    ):
        ok = await sup.start()
    assert ok is False
    assert sup.status() == "memory_blocked"
    assert "memory gate" in sup.status_reason()
