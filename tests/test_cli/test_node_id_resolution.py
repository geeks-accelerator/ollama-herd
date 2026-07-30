"""Node identity must come from FLEET_NODE_NODE_ID, not the volatile hostname.

2026-07-28: after travelling, the node re-registered under a different name and
orphaned its pin. Two causes: (1) macOS derives the hostname from the network
when the static HostName is unset, so `gethostname()` changed from "bb" at home
to "Neons-Mac-Studio" on the road; (2) node_cli passed its `node_id` CLI default
of "" *explicitly* into NodeSettings, and an explicit kwarg shadows the env var
in pydantic — so FLEET_NODE_NODE_ID could never take effect and the node always
fell through to that volatile hostname.
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import NodeSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FLEET_NODE_NODE_ID", raising=False)


def test_env_var_sets_node_id_when_no_cli_override(monkeypatch):
    """The whole point: with the env var set and no CLI value, it wins."""
    monkeypatch.setenv("FLEET_NODE_NODE_ID", "bb")
    # The FIXED node_cli omits node_id from kwargs when the CLI value is empty.
    assert NodeSettings().node_id == "bb"


def test_explicit_empty_node_id_shadows_env_var_the_bug(monkeypatch):
    """Documents the trap: passing node_id="" explicitly (the old node_cli
    behaviour) overrides the env var, which is exactly why it silently broke."""
    monkeypatch.setenv("FLEET_NODE_NODE_ID", "bb")
    assert NodeSettings(node_id="").node_id == ""  # env var lost


def test_explicit_cli_value_still_wins_over_env(monkeypatch):
    """A user who passes --node-id must still override the env var."""
    monkeypatch.setenv("FLEET_NODE_NODE_ID", "bb")
    assert NodeSettings(node_id="explicit").node_id == "explicit"


def test_unset_everywhere_leaves_node_id_empty_for_hostname_fallback():
    """With neither CLI nor env, node_id is "" and the agent falls back to the
    hostname — the documented default, and the only case where drift is expected."""
    assert NodeSettings().node_id == ""


def test_node_cli_only_includes_node_id_when_provided():
    """Lock the actual fix in node_cli: an empty CLI node_id must NOT be added to
    settings_kwargs, or it shadows the env var again.

    Read the source as text rather than importing node_cli — importing it runs
    ``load_env_file()`` at module import, which sets FLEET_NODE_NODE_ID from the
    real ~/.fleet-manager/env into the process env and pollutes every later test.
    """
    from pathlib import Path

    import fleet_manager

    src = (Path(fleet_manager.__file__).parent / "cli" / "node_cli.py").read_text()
    # The unconditional `"node_id": node_id,` in the kwargs dict was the bug.
    assert '"node_id": node_id,' not in src
    assert "if node_id:" in src
