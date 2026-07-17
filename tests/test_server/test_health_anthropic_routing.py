"""Tests for the Claude-Code-readiness health check.

_check_anthropic_no_chat_model warns when nothing on the fleet can serve an
Anthropic Messages request — the fresh-install case where ANTHROPIC_BASE_URL is
set but no model has been pulled — and distinguishes that from a config gap
(auto-routing off with no map covering claude-*).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from fleet_manager.server.health_engine import HealthEngine, Severity


def _node(*, status="online", loaded=None, available=None, mlx=None):
    """Stub node matching the shape the health check reads.

    ``mlx`` is a list of (model, status) tuples → mlx_servers.
    """
    return SimpleNamespace(
        node_id="n",
        status=SimpleNamespace(value=status),
        ollama=SimpleNamespace(
            models_loaded=[SimpleNamespace(name=n) for n in (loaded or [])],
            models_available=list(available or []),
        ),
        mlx_servers=[
            SimpleNamespace(model=m, status=s) for m, s in (mlx or [])
        ],
    )


def _run(nodes, env=None):
    """Run the check with a controlled env (defaults: empty map, auto on)."""
    base = {"FLEET_ANTHROPIC_MODEL_MAP": "", "FLEET_ANTHROPIC_AUTO_ROUTE": "true"}
    base.update(env or {})
    with patch.dict(os.environ, base, clear=False):
        return HealthEngine()._check_anthropic_no_chat_model(nodes)


def _ids(recs):
    return {r.check_id for r in recs}


# --- fires: nothing can serve Claude Code -----------------------------------


def test_fresh_install_no_models_warns():
    recs = _run([_node(loaded=[], available=[])])
    assert _ids(recs) == {"anthropic_no_chat_model"}
    assert recs[0].severity == Severity.WARNING


def test_embedding_only_fleet_still_warns():
    # nomic-embed-text classifies as GENERAL but can't chat — must not count.
    recs = _run([_node(loaded=["nomic-embed-text"], available=["nomic-embed-text"])])
    assert _ids(recs) == {"anthropic_no_chat_model"}


# --- silent: a chat/coding model is reachable -------------------------------


def test_loaded_coding_model_no_warning():
    assert _run([_node(loaded=["qwen3-coder:30b"], available=["qwen3-coder:30b"])]) == []


def test_ondisk_only_model_no_warning():
    # Not loaded but on disk → auto-routing cold-loads it, so no 404 risk.
    assert _run([_node(loaded=[], available=["qwen3-coder:30b"])]) == []


def test_healthy_mlx_chat_server_no_warning():
    recs = _run([_node(mlx=[("lmstudio-community/GLM-4.7-Flash-MLX-4bit", "healthy")])])
    assert recs == []


def test_unhealthy_mlx_server_does_not_count():
    recs = _run([_node(mlx=[("lmstudio-community/GLM-4.7-Flash-MLX-4bit", "starting")])])
    assert _ids(recs) == {"anthropic_no_chat_model"}


# --- config gap: models exist but routing is switched off -------------------


def test_auto_off_no_map_with_models_warns_config():
    recs = _run(
        [_node(loaded=["qwen3-coder:30b"], available=["qwen3-coder:30b"])],
        env={"FLEET_ANTHROPIC_AUTO_ROUTE": "false"},
    )
    assert _ids(recs) == {"anthropic_unrouted_config"}


def test_auto_off_with_default_map_no_warning():
    recs = _run(
        [_node(loaded=["qwen3-coder:30b"], available=["qwen3-coder:30b"])],
        env={
            "FLEET_ANTHROPIC_AUTO_ROUTE": "false",
            "FLEET_ANTHROPIC_MODEL_MAP": json.dumps({"default": "qwen3-coder:30b"}),
        },
    )
    assert recs == []


def test_explicit_map_entry_no_warning_even_auto_off():
    recs = _run(
        [_node(loaded=["qwen3-coder:30b"], available=["qwen3-coder:30b"])],
        env={
            "FLEET_ANTHROPIC_AUTO_ROUTE": "false",
            "FLEET_ANTHROPIC_MODEL_MAP": json.dumps(
                {"claude-sonnet-4-5": "qwen3-coder:30b"}
            ),
        },
    )
    assert recs == []


# --- edge cases -------------------------------------------------------------


def test_no_online_node_is_silent():
    # Offline-fleet checks own that case; this one stays quiet.
    assert _run([_node(status="offline", available=["qwen3-coder:30b"])]) == []


def test_malformed_map_env_does_not_crash():
    recs = _run(
        [_node(loaded=[], available=[])],
        env={"FLEET_ANTHROPIC_MODEL_MAP": "{not valid json"},
    )
    # Falls back to empty map → still correctly warns about the missing model.
    assert _ids(recs) == {"anthropic_no_chat_model"}
