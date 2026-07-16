"""Tests for the canonical X-Fleet-* header builder + per-request strict mode."""

from __future__ import annotations

from fleet_manager.server.fleet_headers import fleet_headers
from fleet_manager.server.routes.routing import parse_allow_fallback

# ---------------------------------------------------------------------------
# fleet_headers — one canonical set on every route
# ---------------------------------------------------------------------------


def test_fleet_headers_emits_full_canonical_set():
    h = fleet_headers(
        node_id="bb", served_model="gpt-oss:120b", requested_model="gpt-oss:120b",
    )
    # Every key present and always-on (no conditional omissions).
    assert h["X-Fleet-Node"] == "bb"
    assert h["X-Fleet-Served-Model"] == "gpt-oss:120b"
    assert h["X-Fleet-Requested-Model"] == "gpt-oss:120b"
    assert h["X-Fleet-Fallback"] == "false"
    assert h["X-Fleet-Backend"] == "ollama"
    assert h["X-Fleet-Retries"] == "0"
    # Score is conditional — omitted when not scorer-routed.
    assert "X-Fleet-Score" not in h


def test_fleet_headers_fallback_is_a_real_boolean():
    # served != requested → the client can detect substitution from ONE header.
    served = fleet_headers(
        node_id="bb", served_model="gpt-oss:120b", requested_model="qwen3-coder:30b",
    )
    assert served["X-Fleet-Fallback"] == "true"
    assert served["X-Fleet-Served-Model"] == "gpt-oss:120b"
    assert served["X-Fleet-Requested-Model"] == "qwen3-coder:30b"


def test_fleet_headers_coerces_values_and_merges_extra():
    h = fleet_headers(
        node_id="bb", served_model="m", requested_model="m",
        backend="mlx", score=42.9, retries=2,
        extra={"X-Generation-Time": 1234, "anthropic-version": "2023-06-01"},
    )
    assert h["X-Fleet-Backend"] == "mlx"
    assert h["X-Fleet-Score"] == "42"  # int-coerced
    assert h["X-Fleet-Retries"] == "2"
    assert h["X-Generation-Time"] == "1234"  # str-coerced
    assert h["anthropic-version"] == "2023-06-01"
    # every value is a str (drop-in for Starlette responses)
    assert all(isinstance(v, str) for v in h.values())


# ---------------------------------------------------------------------------
# parse_allow_fallback — per-request strict-mode override
# ---------------------------------------------------------------------------


def test_parse_allow_fallback_none_when_unspecified():
    assert parse_allow_fallback({}, {}) is None
    assert parse_allow_fallback({"model": "x"}, {}) is None


def test_parse_allow_fallback_header_strict():
    assert parse_allow_fallback({}, {"x-fleet-no-fallback": "true"}) is False
    assert parse_allow_fallback({}, {"x-fleet-no-fallback": "1"}) is False
    # explicitly requesting fallback back on
    assert parse_allow_fallback({}, {"x-fleet-no-fallback": "false"}) is True


def test_parse_allow_fallback_body_field():
    assert parse_allow_fallback({"fallback": False}, {}) is False
    assert parse_allow_fallback({"fallback": True}, {}) is True


def test_parse_allow_fallback_header_wins_over_body():
    assert parse_allow_fallback({"fallback": True}, {"x-fleet-no-fallback": "true"}) is False


def test_parse_allow_fallback_tolerates_non_dict_body():
    # Anthropic's raw_body may be empty/None — must not raise.
    assert parse_allow_fallback(None, {}) is None
    assert parse_allow_fallback("not a dict", {}) is None
