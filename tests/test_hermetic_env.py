"""The suite must not read the operator's environment.

Every other test file tests the code.  This one tests the *conditions* the
others run under, because on 2026-10-22 three of them went red on a machine
that runs a herd and green in CI: ``~/.zshrc`` exported
``FLEET_ANTHROPIC_MODEL_MAP``, ``_check_mapped_model_missing`` reads it
straight from ``os.environ``, and ``mapped_model_missing`` fired CRITICAL
inside ``test_empty_fleet``.  Nothing was wrong with the code -- the suite was
asserting against the operator's configuration.

Three modules had already met this and each cleared the variables its own
author remembered, so the fourth caller was exposed.  ``_hermetic_env`` in
``conftest.py`` replaced those three.  These tests fail if it is removed or
narrowed, which is the whole point: the failure it prevents is invisible to
whoever has a clean shell.
"""

from __future__ import annotations

import os

from fleet_manager.models.config import NodeSettings, ServerSettings


def test_no_fleet_vars_are_visible():
    """Covers both settings prefixes at once -- ``FLEET_NODE_`` starts with
    ``FLEET_`` -- and every direct ``os.environ`` read in ``src/``."""
    leaked = sorted(k for k in os.environ if k.startswith("FLEET_"))
    assert leaked == [], f"operator env reached the suite: {leaked}"


def test_settings_come_from_defaults_not_the_machine():
    """The specific variable that broke the three tests, and the two settings
    objects that would otherwise be built from whatever the shell holds."""
    assert os.environ.get("FLEET_ANTHROPIC_MODEL_MAP") is None
    assert ServerSettings().anthropic_model_map == {}
    assert NodeSettings().node_id == ""


def test_a_test_can_still_set_one(monkeypatch):
    """Clearing must not defeat the 23 places that set a var deliberately:
    ``monkeypatch.setenv`` runs after the autouse fixture, not before it."""
    monkeypatch.setenv("FLEET_ANTHROPIC_MODEL_MAP", '{"claude-x":"gpt-oss:120b"}')
    assert ServerSettings().anthropic_model_map == {"claude-x": "gpt-oss:120b"}
