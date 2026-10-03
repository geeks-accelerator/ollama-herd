"""A CLI option default must not shadow the env var it documents.

`herd` built `ServerSettings(host=host, port=port)` from typer options that
always *have* a value, so the explicit kwargs always won and `FLEET_HOST` /
`FLEET_PORT` were silent no-ops — while
`docs/configuration-reference.md` documented both as working.

This is the same pydantic shadowing already fixed on the node side for
`--node-id` / `FLEET_NODE_ROUTER_URL` (see the node-identity gotcha in
CLAUDE.md); the router kept it. Found by trying to start a second router on
another port with `FLEET_PORT=11455` and watching it bind 11435 instead.

Precedence must be: CLI flag > env var > default.
"""


import pathlib

from fleet_manager.models.config import NodeSettings, ServerSettings


class TestServerSettingsPrecedence:
    def test_env_is_honoured_when_no_flag_is_given(self, monkeypatch):
        monkeypatch.setenv("FLEET_PORT", "11455")
        monkeypatch.setenv("FLEET_HOST", "127.0.0.1")
        s = ServerSettings()
        assert s.port == 11455
        assert s.host == "127.0.0.1"

    def test_an_explicit_kwarg_still_wins(self, monkeypatch):
        """The CLI flag must override the env var, not the other way round."""
        monkeypatch.setenv("FLEET_PORT", "11455")
        assert ServerSettings(port=12345).port == 12345

    def test_the_default_applies_when_neither_is_set(self, monkeypatch):
        monkeypatch.delenv("FLEET_PORT", raising=False)
        monkeypatch.delenv("FLEET_HOST", raising=False)
        s = ServerSettings()
        assert s.port == 11435
        assert s.host == "0.0.0.0"


# Read the CLI sources from disk rather than importing them. Importing
# `fleet_manager.cli.server_cli` calls `load_env_file()` at module scope, which
# injects the operator's real ~/.fleet-manager/env into the test process and
# stays there for the rest of the session -- it broke four unrelated tests on
# this machine (FLEET_DYNAMIC_NUM_CTX and FLEET_NUM_CTX_OVERRIDES leaking into
# context-protection and settings-API assertions) while every one of them passed
# in isolation. On a machine with no env file it would have looked fine, which is
# the worst version of this bug. See docs/issues.md.
_CLI = pathlib.Path(__file__).resolve().parents[2] / "src/fleet_manager/cli"


class TestTheCliDoesNotShadow:
    """Pins the call shape, since the bug lived in the caller, not the model.

    ``ServerSettings`` was always correct; the regression was `herd` passing
    option defaults into it unconditionally. A test on the settings class alone
    would have stayed green through the entire bug.
    """

    def test_server_cli_builds_kwargs_conditionally(self):
        src = (_CLI / "server_cli.py").read_text()
        assert "ServerSettings(host=host, port=port)" not in src, (
            "passing option defaults unconditionally re-breaks FLEET_HOST/FLEET_PORT"
        )
        assert "settings_kwargs" in src
        assert "if host is not None" in src
        assert "if port is not None" in src

    def test_the_options_default_to_none(self):
        """A non-None default is what makes the kwarg unconditional."""
        src = (_CLI / "server_cli.py").read_text()
        for name in ("host", "port"):
            assert f'{name}: ' in src
        assert 'typer.Option(None, help="Bind address' in src
        assert 'typer.Option(None, help="Listen port' in src

    def test_resolved_values_are_read_back_off_settings(self):
        """The banner and uvicorn must bind what settings resolved, not None."""
        src = (_CLI / "server_cli.py").read_text()
        assert "host, port = settings.host, settings.port" in src


class TestNodeSideStaysFixed:
    """Regression guard for the original instance of this bug."""

    def test_node_env_is_honoured(self, monkeypatch):
        monkeypatch.setenv("FLEET_NODE_NODE_ID", "pinned-id")
        monkeypatch.setenv("FLEET_NODE_ROUTER_URL", "http://10.0.0.5:11435")
        s = NodeSettings()
        assert s.node_id == "pinned-id"
        assert s.router_url == "http://10.0.0.5:11435"

    def test_node_cli_still_builds_kwargs_conditionally(self):
        src = (_CLI / "node_cli.py").read_text()
        assert "settings_kwargs" in src
        assert "NodeSettings(node_id=" not in src
