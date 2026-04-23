"""Tests for server/debug_log.py — JSONL request capture."""

from __future__ import annotations

import json
import time

from fleet_manager.server import debug_log


def _make_record(request_id: str = "abc-123", **overrides) -> dict:
    base = {
        "request_id": request_id,
        "timestamp": time.time(),
        "model": "qwen3-coder:30b-agent",
        "status": "completed",
        "latency_ms": 1234.0,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "client_body": {"model": "qwen3-coder:30b-agent", "messages": []},
        "ollama_body": {"model": "qwen3-coder:30b-agent", "stream": True},
        "response_chunks": ['{"done":true}'],
        "error": None,
    }
    base.update(overrides)
    return base


class TestDebugLog:
    def test_disabled_is_noop(self, tmp_path):
        debug_log.append_request(enabled=False, data_dir=str(tmp_path), record=_make_record())
        # No file should be created
        assert list(tmp_path.glob("**/*")) == []

    def test_enabled_writes_jsonl(self, tmp_path):
        record = _make_record()
        debug_log.append_request(enabled=True, data_dir=str(tmp_path), record=record)
        files = list((tmp_path / "debug").glob("requests.*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["request_id"] == "abc-123"
        assert parsed["model"] == "qwen3-coder:30b-agent"

    def test_append_multiple_records(self, tmp_path):
        for i in range(5):
            rec = _make_record(request_id=f"req-{i}")
            debug_log.append_request(enabled=True, data_dir=str(tmp_path), record=rec)
        files = list((tmp_path / "debug").glob("requests.*.jsonl"))
        assert len(files) == 1  # Same day → same file
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 5
        ids = [json.loads(line)["request_id"] for line in lines]
        assert ids == ["req-0", "req-1", "req-2", "req-3", "req-4"]

    def test_failure_to_write_never_raises(self, tmp_path):
        """A broken data_dir path must not propagate exceptions."""
        # Point at a file that exists (not a directory) — write will fail
        bad = tmp_path / "not-a-dir"
        bad.write_text("blocker")
        # Should not raise — just swallowed and logged at DEBUG
        debug_log.append_request(
            enabled=True,
            data_dir=str(bad),
            record=_make_record(),
        )

    def test_iter_records_newest_last(self, tmp_path):
        # Write today's records
        for i in range(3):
            debug_log.append_request(
                enabled=True,
                data_dir=str(tmp_path),
                record=_make_record(request_id=f"a-{i}"),
            )
        records = debug_log.iter_records(str(tmp_path))
        assert [r["request_id"] for r in records] == ["a-0", "a-1", "a-2"]

    def test_find_by_request_id(self, tmp_path):
        for rid in ("alpha", "beta", "gamma"):
            debug_log.append_request(
                enabled=True, data_dir=str(tmp_path),
                record=_make_record(request_id=rid),
            )
        found = debug_log.find_by_request_id(str(tmp_path), "beta")
        assert found is not None
        assert found["request_id"] == "beta"

    def test_find_by_request_id_missing(self, tmp_path):
        debug_log.append_request(
            enabled=True, data_dir=str(tmp_path),
            record=_make_record(request_id="only"),
        )
        assert debug_log.find_by_request_id(str(tmp_path), "not-here") is None

    def test_find_failures_filters_correctly(self, tmp_path):
        debug_log.append_request(
            enabled=True, data_dir=str(tmp_path),
            record=_make_record(request_id="ok", status="completed"),
        )
        debug_log.append_request(
            enabled=True, data_dir=str(tmp_path),
            record=_make_record(
                request_id="bad", status="failed", error="Server error 500"
            ),
        )
        debug_log.append_request(
            enabled=True, data_dir=str(tmp_path),
            record=_make_record(
                request_id="dropped", status="client_disconnected"
            ),
        )
        failures = debug_log.find_failures(str(tmp_path))
        ids = {r["request_id"] for r in failures}
        assert ids == {"bad", "dropped"}

    def test_prune_old_files(self, tmp_path):
        # Create a fake old log
        old_file = tmp_path / "debug" / "requests.2020-01-01.jsonl"
        old_file.parent.mkdir(parents=True)
        old_file.write_text('{"request_id":"ancient"}\n')
        # Force its mtime far in the past
        old_time = time.time() - (30 * 86400)  # 30 days ago
        import os
        os.utime(old_file, (old_time, old_time))
        # Now write a fresh record with 7-day retention
        debug_log.append_request(
            enabled=True, data_dir=str(tmp_path),
            record=_make_record(request_id="fresh"),
            retention_days=7,
        )
        # Old file should be gone, new one should exist
        assert not old_file.exists()
        assert list((tmp_path / "debug").glob("requests.*.jsonl"))

    def test_since_filter(self, tmp_path):
        cutoff = time.time()
        debug_log.append_request(
            enabled=True, data_dir=str(tmp_path),
            record=_make_record(request_id="old", timestamp=cutoff - 100),
        )
        debug_log.append_request(
            enabled=True, data_dir=str(tmp_path),
            record=_make_record(request_id="new", timestamp=cutoff + 100),
        )
        records = debug_log.iter_records(str(tmp_path), since=cutoff)
        assert [r["request_id"] for r in records] == ["new"]

    def test_missing_dir_returns_empty_lists(self, tmp_path):
        # Nothing written — should return empty without error
        nonexistent = tmp_path / "not-created"
        assert debug_log.iter_records(str(nonexistent)) == []
        assert debug_log.find_failures(str(nonexistent)) == []
        assert debug_log.find_by_request_id(str(nonexistent), "x") is None
