"""FLEET_NODE_OLLAMA_HOST must reach the node when `herd-node` runs with no flag.

2026-11-19: the Docker image's first test found that it never did. The CLI's
--ollama-host option defaulted to "http://localhost:11434" and was always passed
into NodeSettings explicitly, and an explicit kwarg beats the env var, the same
trap test_node_id_resolution.py records for node_id. Environment variables are
how a container is configured, so a node container pointed at the host's Ollama
silently looked at its own localhost instead and exited.

These drive the real Typer command, so they test what the user runs rather
than NodeSettings alone.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fleet_manager.cli import node_cli


@pytest.fixture
def captured(monkeypatch):
    """Run node_cli up to the agent and capture the settings it built."""
    seen = {}

    class FakeAgent:
        def __init__(self, settings):
            seen["settings"] = settings
            self.node_id = "test-node"

        async def start(self):
            return None

    monkeypatch.setattr("fleet_manager.node.agent.NodeAgent", FakeAgent)
    monkeypatch.setattr("fleet_manager.common.logging_config.setup_logging", lambda **_: None)
    monkeypatch.setattr(
        "fleet_manager.common.telemetry_notice.show_first_run_notice_if_needed",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(node_cli, "load_env_file", lambda *a, **k: None, raising=False)
    monkeypatch.delenv("FLEET_NODE_OLLAMA_HOST", raising=False)
    return seen


def _run(args=()):
    result = CliRunner().invoke(node_cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result


def test_env_var_sets_ollama_host_when_no_flag(captured, monkeypatch):
    monkeypatch.setenv("FLEET_NODE_OLLAMA_HOST", "http://host.docker.internal:11434")
    result = _run()
    assert captured["settings"].ollama_host == "http://host.docker.internal:11434"
    # The banner shows what the node will actually use.
    assert "host.docker.internal:11434" in result.output


def test_flag_still_wins_over_env(captured, monkeypatch):
    monkeypatch.setenv("FLEET_NODE_OLLAMA_HOST", "http://from-env:11434")
    _run(["--ollama-host", "http://from-flag:11434"])
    assert captured["settings"].ollama_host == "http://from-flag:11434"


def test_default_without_flag_or_env(captured):
    _run()
    assert captured["settings"].ollama_host == "http://localhost:11434"
