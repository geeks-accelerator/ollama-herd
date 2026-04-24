"""Tests for PinnedModelsStore — per-node runtime pins."""

from __future__ import annotations

import json

import pytest

from fleet_manager.server.pinned_models import PinnedModelsStore, merge_pins


@pytest.fixture
def store(tmp_path):
    return PinnedModelsStore(tmp_path / "pinned_models.json")


def test_load_empty_when_file_missing(store):
    assert store.load() == {}


def test_set_pin_creates_file(store, tmp_path):
    state = store.set_pin("node-a", "gpt-oss:120b", pinned=True)
    assert state == {"node-a": ["gpt-oss:120b"]}
    assert (tmp_path / "pinned_models.json").exists()


def test_set_pin_persists_across_instances(tmp_path):
    path = tmp_path / "pinned_models.json"
    s1 = PinnedModelsStore(path)
    s1.set_pin("node-a", "gpt-oss:120b", pinned=True)
    s1.set_pin("node-a", "gemma3:27b", pinned=True)
    s1.set_pin("node-b", "qwen3-coder:30b", pinned=True)

    s2 = PinnedModelsStore(path)
    state = s2.load()
    assert state == {
        "node-a": ["gemma3:27b", "gpt-oss:120b"],
        "node-b": ["qwen3-coder:30b"],
    }


def test_unpin_removes_model(store):
    store.set_pin("node-a", "m1", pinned=True)
    store.set_pin("node-a", "m2", pinned=True)
    state = store.set_pin("node-a", "m1", pinned=False)
    assert state == {"node-a": ["m2"]}


def test_unpin_last_removes_node_key(store):
    store.set_pin("node-a", "m1", pinned=True)
    state = store.set_pin("node-a", "m1", pinned=False)
    assert state == {}


def test_unpin_missing_is_noop(store):
    state = store.set_pin("node-a", "never-pinned", pinned=False)
    assert state == {}


def test_set_pin_dedups(store):
    store.set_pin("node-a", "m1", pinned=True)
    state = store.set_pin("node-a", "m1", pinned=True)
    assert state == {"node-a": ["m1"]}


def test_set_pin_rejects_empty(store):
    with pytest.raises(ValueError):
        store.set_pin("", "m1", pinned=True)
    with pytest.raises(ValueError):
        store.set_pin("node-a", "", pinned=True)


def test_get_for_node(store):
    store.set_pin("node-a", "m1", pinned=True)
    store.set_pin("node-b", "m2", pinned=True)
    assert store.get_for_node("node-a") == ["m1"]
    assert store.get_for_node("node-b") == ["m2"]
    assert store.get_for_node("unknown") == []


def test_corrupt_file_fails_open(tmp_path, caplog):
    path = tmp_path / "pinned_models.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = PinnedModelsStore(path)
    assert store.load() == {}


def test_malformed_nodes_are_filtered(tmp_path):
    path = tmp_path / "pinned_models.json"
    path.write_text(
        json.dumps({
            "nodes": {
                "node-a": ["m1", "m2", ""],  # empty string filtered
                "node-b": "not-a-list",  # whole entry filtered
                "node-c": [],  # empty list dropped
                "node-d": ["m3", 42, "m4"],  # non-strings filtered
            },
        }),
        encoding="utf-8",
    )
    store = PinnedModelsStore(path)
    state = store.load()
    assert state == {
        "node-a": ["m1", "m2"],
        "node-d": ["m3", "m4"],
    }


def test_write_is_atomic_no_tempfiles_left(store, tmp_path):
    store.set_pin("node-a", "m1", pinned=True)
    store.set_pin("node-a", "m2", pinned=True)
    leftover = list(tmp_path.glob(".pinned-*.tmp"))
    assert leftover == []


def test_merge_pins_union_preserves_env_first():
    assert merge_pins(["a", "b"], ["c", "a"]) == ["a", "b", "c"]


def test_merge_pins_both_empty():
    assert merge_pins([], []) == []


def test_merge_pins_skips_falsy():
    assert merge_pins(["a", ""], ["", "b"]) == ["a", "b"]
