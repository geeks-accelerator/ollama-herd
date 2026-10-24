"""Removed platform flags are accepted, ignored and warned about -- never fatal.

The gotomy.ai platform these flags connected to was never deployed, so 0.12.0
removed it (``docs/plans/remove-platform-integration.md``).  Rejecting the flags
would make Typer exit with "No such option", which under a launchd/systemd
keep-alive agent is a crash loop on upgrade.  These tests pin the tolerance and
its limits:

- every removed flag still parses, the process still starts, and one warning
  names the flag -- without echoing its value (``--platform-token`` is a
  credential);
- the tolerance is per flag, so a typo still fails;
- the removed flags are hidden from ``--help``;
- leftover ``FLEET_NODE_PLATFORM_*`` variables are inert: settings construct,
  nothing warns.

Delete the flag tests with ``cli.removed_option`` in 0.14.0; keep the env-var
test, which is about upgrades in general.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fleet_manager.cli import node_cli, server_cli

runner = CliRunner()

SECRET = "herd_s3cret-value-that-must-not-print"

SERVER_FLAGS = [["--cloud"], ["--token", SECRET], ["--cloud-url", "https://gotomy.ai"]]
NODE_FLAGS = [
    ["--platform-token", SECRET],
    ["--operator-token", SECRET],
    ["--platform-url", "https://gotomy.ai"],
    ["--telemetry-local-summary"],
    ["--telemetry-include-tags"],
]


@pytest.fixture
def started(monkeypatch):
    """Stub both start paths at their outermost side effect and record the call.

    Nothing binds a port, starts an agent, or writes under ``~/.fleet-manager``.
    """
    calls: list[str] = []

    monkeypatch.setattr(server_cli, "load_env_file", lambda *a, **k: {})
    monkeypatch.setattr(node_cli, "load_env_file", lambda *a, **k: {})
    monkeypatch.setattr("fleet_manager.server.app.create_app", lambda settings: object())
    monkeypatch.setattr(server_cli.uvicorn, "run", lambda *a, **k: calls.append("herd"))

    class FakeAgent:
        node_id = "test-node"

        def __init__(self, settings):
            self.settings = settings

        async def start(self):
            calls.append("herd-node")

    monkeypatch.setattr("fleet_manager.node.agent.NodeAgent", FakeAgent)
    monkeypatch.setattr("fleet_manager.common.logging_config.setup_logging", lambda **k: None)
    monkeypatch.setattr(
        "fleet_manager.common.telemetry_notice.show_first_run_notice_if_needed",
        lambda *a, **k: False,
    )
    return calls


def _cases():
    for args in SERVER_FLAGS:
        yield pytest.param(server_cli, "herd", args, id=f"herd {args[0]}")
    for args in NODE_FLAGS:
        yield pytest.param(node_cli, "herd-node", args, id=f"herd-node {args[0]}")


@pytest.mark.parametrize(("module", "name", "args"), list(_cases()))
def test_removed_flag_still_starts_and_warns(module, name, args, started):
    result = runner.invoke(module.app, args)

    assert result.exit_code == 0, result.output
    assert started == [name], "the process must still start"
    assert "was removed in ollama-herd 0.12.0" in result.output
    assert args[0] in result.output, "the warning must name the flag"
    assert result.output.count("WARNING:") == 1
    assert SECRET not in result.output, "a removed flag's value must never be echoed"


@pytest.mark.parametrize(("module", "name"), [(server_cli, "herd"), (node_cli, "herd-node")])
def test_without_removed_flags_there_is_no_warning(module, name, started):
    result = runner.invoke(module.app, [])

    assert result.exit_code == 0, result.output
    assert started == [name]
    assert "WARNING:" not in result.output


@pytest.mark.parametrize("module", [server_cli, node_cli], ids=["herd", "herd-node"])
def test_a_typo_still_fails(module, started):
    """The tolerance is per removed flag -- not Typer's ``ignore_unknown_options``,
    which would also swallow mistakes like this one."""
    result = runner.invoke(module.app, ["--prot", "11435"])

    assert result.exit_code != 0
    assert started == []


@pytest.mark.parametrize(
    ("module", "flags"),
    [(server_cli, SERVER_FLAGS), (node_cli, NODE_FLAGS)],
    ids=["herd", "herd-node"],
)
def test_removed_flags_are_hidden_from_help(module, flags):
    result = runner.invoke(module.app, ["--help"])

    assert result.exit_code == 0
    for args in flags:
        assert args[0] not in result.output


LEFTOVER_ENV = {
    "FLEET_NODE_PLATFORM_TOKEN": SECRET,
    "FLEET_NODE_PLATFORM_URL": "https://gotomy.ai",
    "FLEET_NODE_PLATFORM_NODE_ID": "abc",
    "FLEET_NODE_TELEMETRY_LOCAL_SUMMARY": "true",
    "FLEET_NODE_TELEMETRY_INCLUDE_TAGS": "true",
    "CLOUD_FLEET_TOKEN": SECRET,
}


def test_leftover_platform_env_vars_are_inert(monkeypatch, started):
    """An upgraded machine may still export these.  Both settings classes must
    construct (pydantic-settings ignores unknown prefixed variables), and the
    node must start without a warning -- the hidden options read no env vars."""
    from fleet_manager.models.config import NodeSettings, ServerSettings

    for key, value in LEFTOVER_ENV.items():
        monkeypatch.setenv(key, value)

    NodeSettings()
    ServerSettings()

    result = runner.invoke(node_cli.app, [])
    assert result.exit_code == 0, result.output
    assert started == ["herd-node"]
    assert "WARNING:" not in result.output
