"""Tests for the ``~/.fleet-manager/env`` loader."""

from __future__ import annotations

import os

import pytest

from fleet_manager.common.env_file import load_env_file


@pytest.fixture
def clean_env(monkeypatch):
    """Remove any FLEET_TEST_* vars before each test."""
    for k in list(os.environ):
        if k.startswith("FLEET_TEST_"):
            monkeypatch.delenv(k, raising=False)
    yield monkeypatch


def test_missing_file_is_silent(tmp_path, clean_env):
    applied = load_env_file(tmp_path / "does-not-exist")
    assert applied == {}


def test_loads_simple_pairs(tmp_path, clean_env):
    p = tmp_path / "env"
    p.write_text("FLEET_TEST_A=hello\nFLEET_TEST_B=world\n")
    applied = load_env_file(p)
    assert applied == {"FLEET_TEST_A": "hello", "FLEET_TEST_B": "world"}
    assert os.environ["FLEET_TEST_A"] == "hello"


def test_shell_env_wins_over_file(tmp_path, clean_env):
    clean_env.setenv("FLEET_TEST_A", "from-shell")
    p = tmp_path / "env"
    p.write_text("FLEET_TEST_A=from-file\nFLEET_TEST_B=from-file\n")
    applied = load_env_file(p)
    # Shell-set key is preserved; only the new key is applied
    assert os.environ["FLEET_TEST_A"] == "from-shell"
    assert applied == {"FLEET_TEST_B": "from-file"}


def test_comments_and_blanks_skipped(tmp_path, clean_env):
    p = tmp_path / "env"
    p.write_text(
        "# comment\n"
        "\n"
        "FLEET_TEST_A=x\n"
        "   # indented comment should also be ignored\n"
        "FLEET_TEST_B=y\n",
    )
    applied = load_env_file(p)
    assert applied == {"FLEET_TEST_A": "x", "FLEET_TEST_B": "y"}


def test_export_prefix_supported(tmp_path, clean_env):
    """So the same file is ``set -a; source``-able from a shell."""
    p = tmp_path / "env"
    p.write_text("export FLEET_TEST_A=value\n")
    applied = load_env_file(p)
    assert applied == {"FLEET_TEST_A": "value"}


def test_quoted_values_stripped(tmp_path, clean_env):
    p = tmp_path / "env"
    p.write_text(
        'FLEET_TEST_DOUBLE="double-quoted value"\n'
        "FLEET_TEST_SINGLE='single-quoted value'\n"
        'FLEET_TEST_JSON=\'{"nested":"json"}\'\n',
    )
    applied = load_env_file(p)
    assert applied["FLEET_TEST_DOUBLE"] == "double-quoted value"
    assert applied["FLEET_TEST_SINGLE"] == "single-quoted value"
    assert applied["FLEET_TEST_JSON"] == '{"nested":"json"}'


def test_malformed_lines_skipped_not_fatal(tmp_path, clean_env, caplog):
    p = tmp_path / "env"
    p.write_text(
        "FLEET_TEST_OK=fine\n"
        "this line has no equals sign\n"
        "FLEET_TEST_ALSO_OK=also-fine\n",
    )
    applied = load_env_file(p)
    assert "FLEET_TEST_OK" in applied
    assert "FLEET_TEST_ALSO_OK" in applied
    # The bad line didn't nuke the whole load
    assert len(applied) == 2


def test_fleet_env_file_override(tmp_path, clean_env):
    """``FLEET_ENV_FILE`` lets ops point to a non-default location."""
    p = tmp_path / "custom.env"
    p.write_text("FLEET_TEST_CUSTOM=loaded\n")
    clean_env.setenv("FLEET_ENV_FILE", str(p))
    applied = load_env_file()  # no arg — uses env var
    assert applied == {"FLEET_TEST_CUSTOM": "loaded"}
