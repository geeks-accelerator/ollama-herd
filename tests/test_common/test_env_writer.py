"""Tests for persisting settings to ~/.fleet-manager/env.

The round-trip tests are the important ones: a writer whose output the loader
cannot read back would make every dashboard toggle look like it worked and
then quietly do nothing on restart -- the exact bug this module exists to fix.
"""

from __future__ import annotations

import os

import pytest

from fleet_manager.common.env_file import load_env_file
from fleet_manager.common.env_writer import set_env_var, unset_env_var


@pytest.fixture
def env_path(tmp_path):
    return tmp_path / "env"


def _reload(path) -> dict:
    """Load the file with a clean environ so shell vars can't mask results."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("FLEET_"):
            del os.environ[key]
    try:
        load_env_file(path)
        return {k: v for k, v in os.environ.items() if k.startswith("FLEET_")}
    finally:
        os.environ.clear()
        os.environ.update(saved)


class TestRoundTrip:
    def test_written_value_loads_back(self, env_path):
        set_env_var("FLEET_NODE_TELEMETRY", False, env_path)
        assert _reload(env_path)["FLEET_NODE_TELEMETRY"] == "false"

    def test_bool_true_round_trips(self, env_path):
        set_env_var("FLEET_NODE_TELEMETRY", True, env_path)
        assert _reload(env_path)["FLEET_NODE_TELEMETRY"] == "true"

    def test_value_with_spaces_round_trips(self, env_path):
        set_env_var("FLEET_NODE_HERD_NICKNAME", "Neon's Big Herd", env_path)
        assert _reload(env_path)["FLEET_NODE_HERD_NICKNAME"] == "Neon's Big Herd"

    def test_pydantic_accepts_the_written_bool(self, env_path):
        """The loader hands strings to pydantic; 'false' must mean False."""
        set_env_var("FLEET_NODE_TELEMETRY", False, env_path)
        saved = dict(os.environ)
        try:
            load_env_file(env_path)
            from fleet_manager.models.config import NodeSettings

            assert NodeSettings().telemetry is False
        finally:
            os.environ.clear()
            os.environ.update(saved)


class TestPreservesHandEdits:
    """The real file carries comments explaining hard-won gotchas."""

    def test_comments_and_other_keys_survive(self, env_path):
        env_path.write_text(
            "# Pin the node id -- hostname is network-derived on macOS\n"
            "FLEET_NODE_NODE_ID=bb\n"
            "\n"
            "# Router pin so a DHCP change cannot orphan the node\n"
            "FLEET_NODE_ROUTER_URL=http://localhost:11435\n"
        )
        set_env_var("FLEET_NODE_TELEMETRY", False, env_path)
        text = env_path.read_text()

        assert "# Pin the node id" in text
        assert "# Router pin so a DHCP change" in text
        assert "FLEET_NODE_NODE_ID=bb" in text
        assert "FLEET_NODE_ROUTER_URL=http://localhost:11435" in text
        assert "FLEET_NODE_TELEMETRY=false" in text

    def test_existing_key_updated_in_place_not_duplicated(self, env_path):
        env_path.write_text("FLEET_NODE_TELEMETRY=true\nFLEET_NODE_NODE_ID=bb\n")
        set_env_var("FLEET_NODE_TELEMETRY", False, env_path)
        lines = [ln for ln in env_path.read_text().splitlines() if ln.strip()]
        assert lines.count("FLEET_NODE_TELEMETRY=false") == 1
        assert not any(ln == "FLEET_NODE_TELEMETRY=true" for ln in lines)
        # Position preserved: still ahead of the node id it was written before.
        assert lines.index("FLEET_NODE_TELEMETRY=false") < lines.index(
            "FLEET_NODE_NODE_ID=bb"
        )

    def test_commented_out_key_is_not_treated_as_the_value(self, env_path):
        env_path.write_text("# FLEET_NODE_TELEMETRY=true\n")
        set_env_var("FLEET_NODE_TELEMETRY", False, env_path)
        text = env_path.read_text()
        assert "# FLEET_NODE_TELEMETRY=true" in text, "comment must be left alone"
        assert "FLEET_NODE_TELEMETRY=false" in text

    def test_export_prefixed_key_is_replaced(self, env_path):
        env_path.write_text("export FLEET_NODE_TELEMETRY=true\n")
        set_env_var("FLEET_NODE_TELEMETRY", False, env_path)
        assert _reload(env_path)["FLEET_NODE_TELEMETRY"] == "false"


class TestUnset:
    def test_removes_key_so_default_applies(self, env_path):
        env_path.write_text("FLEET_NODE_TELEMETRY=false\nFLEET_NODE_NODE_ID=bb\n")
        unset_env_var("FLEET_NODE_TELEMETRY", env_path)
        loaded = _reload(env_path)
        assert "FLEET_NODE_TELEMETRY" not in loaded
        assert loaded["FLEET_NODE_NODE_ID"] == "bb"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert unset_env_var("FLEET_NODE_TELEMETRY", tmp_path / "nope") is True


class TestResilience:
    def test_unwritable_path_returns_false_without_raising(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        assert set_env_var("FLEET_NODE_TELEMETRY", False, blocker / "env") is False

    def test_creates_file_and_parents_when_absent(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "env"
        assert set_env_var("FLEET_NODE_TELEMETRY", True, target) is True
        assert target.exists()
