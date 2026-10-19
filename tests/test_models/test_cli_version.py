"""`herd --version` / `herd-node --version` print the version and do nothing else.

The ollamaherd.com/download page uses `herd --version` as the post-install
verify step, so it must work on a fresh install: no env file loaded, no
settings built, no server started.  The option is eager, so Typer runs it
before the group callback body that calls `load_env_file()` and starts the
router or node agent.
"""

import pytest
from typer.testing import CliRunner

from fleet_manager import __version__
from fleet_manager.cli import node_cli, server_cli

runner = CliRunner()


@pytest.mark.parametrize("module", [server_cli, node_cli], ids=["herd", "herd-node"])
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_prints_and_exits_without_starting(module, flag, monkeypatch):
    def must_not_run():
        raise AssertionError("--version reached the start path (load_env_file was called)")

    monkeypatch.setattr(module, "load_env_file", must_not_run)

    result = runner.invoke(module.app, [flag])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"ollama-herd {__version__}"


@pytest.mark.parametrize("module", [server_cli, node_cli], ids=["herd", "herd-node"])
def test_version_is_listed_in_help(module):
    result = runner.invoke(module.app, ["--help"])
    assert result.exit_code == 0
    assert "--version" in result.output
