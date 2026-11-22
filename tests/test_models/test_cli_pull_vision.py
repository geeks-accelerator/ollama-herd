"""`herd-node pull-vision`: the way to put a vision embedding model on a node.

Run 6: the router's 404 for a fleet with no vision model said to POST
`/api/pull {"model": "dinov2-vit-s14"}`; that went to Ollama, failed with
"pull model manifest: file does not exist" and logged an ERROR.  Vision models
run in herd-node's own server, so they are fetched there.
"""

from pathlib import Path

from typer.testing import CliRunner

from fleet_manager.cli import node_cli
from fleet_manager.node import embedding_models
from fleet_manager.server.routes.ollama_compat import _check_non_ollama_model

runner = CliRunner()


def test_pull_vision_downloads_and_does_not_start_the_agent(monkeypatch):
    fetched = []
    monkeypatch.setattr(node_cli, "load_env_file", lambda: None)
    monkeypatch.setattr(
        embedding_models, "download_model", lambda m: fetched.append(m) or Path("/m") / m
    )
    monkeypatch.setattr(
        "fleet_manager.node.agent.NodeAgent.start",
        lambda self: (_ for _ in ()).throw(AssertionError("agent started")),
    )

    result = runner.invoke(node_cli.app, ["pull-vision", "dinov2-vit-s14"])

    assert result.exit_code == 0, result.output
    assert fetched == ["dinov2-vit-s14"]
    assert "Restart herd-node" in result.output


def test_pull_vision_refuses_an_unknown_model(monkeypatch):
    monkeypatch.setattr(node_cli, "load_env_file", lambda: None)
    result = runner.invoke(node_cli.app, ["pull-vision", "llava:7b"])
    assert result.exit_code == 1
    assert "dinov2-vit-s14" in result.output


def test_api_pull_answers_every_vision_model_with_the_command():
    for model in embedding_models.VISION_EMBEDDING_MODELS:
        hint = _check_non_ollama_model(f"{model}:latest")
        assert hint and f"herd-node pull-vision {model}" in hint
