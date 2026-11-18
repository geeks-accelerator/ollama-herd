"""Agent clients are not routed to models that cannot take tools.

Claude Code and Codex send tools on essentially every turn.  Auto-routing
preferred a loaded model and never looked at ``tools``, so with ``gemma3:27b``
(``completion, vision``) loaded and ``qwen3-coder:30b`` (``completion,
tools``) on disk, every turn went to gemma3 and failed 400.  Community
telemetry showed the pattern on three installs.  See
docs/plans/telemetry-errors-2026-11.md item 3 and
docs/plans/silent-failures-2026-11.md issue 3.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fleet_manager.models.node import ModelTagMeta
from fleet_manager.server.anthropic_autoroute import resolve_model
from fleet_manager.server.health_engine import HealthEngine, Severity
from fleet_manager.server.routes.routing import fleet_capabilities
from fleet_manager.server.trace_store import TraceStore

GEMMA = "gemma3:27b"
CODER = "qwen3-coder:30b"
CAPS = {GEMMA: {"completion", "vision"}, CODER: {"completion", "tools"}}


def _resolve(loaded, ondisk, *, has_tools=True, caps=CAPS, has_images=False):
    return resolve_model(
        "claude-sonnet-4-5",
        {},
        set(loaded),
        set(ondisk) | set(loaded),
        auto_route=True,
        has_tools=has_tools,
        has_images=has_images,
        capabilities=caps,
    )[0]


class TestResolveModel:
    def test_tool_capable_model_on_disk_beats_a_loaded_one_without_tools(self):
        """The reproduced row: this used to resolve to gemma3."""
        assert _resolve([GEMMA], [CODER]) == CODER

    def test_both_loaded_picks_the_tool_capable_one(self):
        assert _resolve([GEMMA, CODER], []) == CODER

    def test_no_tools_in_the_request_routes_as_before(self):
        assert _resolve([GEMMA], [CODER], has_tools=False) == GEMMA

    def test_nothing_reports_tools_routes_as_before(self):
        """Older Ollama under-reports; a fleet without tools metadata is unchanged."""
        caps = {GEMMA: {"completion", "vision"}}
        assert _resolve([GEMMA], ["mystery:7b"], caps=caps) == GEMMA

    def test_no_capability_metadata_at_all_routes_as_before(self):
        assert _resolve([GEMMA], [CODER], caps={}) == GEMMA

    def test_only_a_no_tools_model_still_resolves(self):
        """Nothing better exists: send it and let Ollama's 400 say why."""
        assert _resolve([GEMMA], [], caps={GEMMA: {"completion", "vision"}}) == GEMMA

    def test_unreported_model_stays_usable_beside_a_tools_one(self):
        caps = {CODER: {"completion", "tools"}}
        assert _resolve(["mystery:7b"], [CODER], caps=caps) == "mystery:7b"

    def test_images_with_tools_need_both(self):
        caps = {
            GEMMA: {"completion", "vision"},
            CODER: {"completion", "tools"},
            "qwen3-vl:32b": {"completion", "vision", "tools"},
        }
        assert _resolve([GEMMA], [CODER, "qwen3-vl:32b"], caps=caps, has_images=True) == (
            "qwen3-vl:32b"
        )


def _node(*, loaded=(), available=(), meta=None):
    return SimpleNamespace(
        node_id="n",
        status=SimpleNamespace(value="online"),
        ollama_base_url="http://n:11434",
        ollama=SimpleNamespace(
            models_loaded=[SimpleNamespace(name=m) for m in loaded],
            models_available=list(available),
            models_available_meta={
                name: ModelTagMeta(capabilities=sorted(caps)) for name, caps in (meta or {}).items()
            },
        ),
        mlx_servers=[],
    )


def _env(model_map):
    return patch.dict(
        os.environ,
        {"FLEET_ANTHROPIC_MODEL_MAP": json.dumps(model_map), "FLEET_ANTHROPIC_AUTO_ROUTE": "true"},
    )


class TestMappedModelNoTools:
    def test_fires_for_a_mapped_model_reported_without_tools(self):
        node = _node(loaded=[GEMMA], available=[GEMMA], meta={GEMMA: CAPS[GEMMA]})
        with _env({"claude-sonnet-4-5": GEMMA}):
            recs = HealthEngine()._check_mapped_models_hot([node])
        rec = next(r for r in recs if r.check_id == "mapped_model_no_tools")
        assert rec.severity == Severity.WARNING
        assert rec.data["no_tools"] == [GEMMA]

    def test_silent_for_a_tool_capable_target(self):
        node = _node(loaded=[CODER], available=[CODER], meta={CODER: CAPS[CODER]})
        with _env({"claude-sonnet-4-5": CODER}):
            recs = HealthEngine()._check_mapped_models_hot([node])
        assert not [r for r in recs if r.check_id == "mapped_model_no_tools"]

    def test_silent_when_capabilities_are_not_reported(self):
        node = _node(loaded=[GEMMA], available=[GEMMA])
        with _env({"claude-sonnet-4-5": GEMMA}):
            recs = HealthEngine()._check_mapped_models_hot([node])
        assert not [r for r in recs if r.check_id == "mapped_model_no_tools"]


class TestNoChatModelCheckAgreesWithTheRoute:
    def test_passes_capabilities_and_tools_like_the_route(self):
        """The check must reach the same answer the route does."""
        node = _node(loaded=[GEMMA], available=[CODER], meta=CAPS)
        captured = {}
        real = resolve_model

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real(*args, **kwargs)

        with _env({}), patch("fleet_manager.server.anthropic_autoroute.resolve_model", spy):
            HealthEngine()._check_anthropic_no_chat_model([node])
        assert captured["has_tools"] is True
        assert captured["capabilities"] == fleet_capabilities([node])


class TestToolsUnsupportedCheck:
    def test_fires_per_model_with_counts(self):
        now = time.time()
        failures = {
            ("a", GEMMA): {"retried": 0, "failed": 3, "last_ts": now},
            ("b", GEMMA): {"retried": 0, "failed": 2, "last_ts": now - 7200},
        }
        (rec,) = HealthEngine()._check_tools_unsupported(failures)
        assert rec.check_id == "tools_unsupported_requests"
        assert rec.severity == Severity.WARNING
        assert rec.data["models"][GEMMA]["failed"] == 5
        assert "gemma3:27b (5x)" in rec.description

    def test_info_once_quiet(self):
        failures = {("a", GEMMA): {"retried": 0, "failed": 1, "last_ts": time.time() - 7200}}
        (rec,) = HealthEngine()._check_tools_unsupported(failures)
        assert rec.severity == Severity.INFO

    def test_silent_with_no_failures(self):
        assert HealthEngine()._check_tools_unsupported({}) == []


@pytest.fixture
async def store(tmp_path):
    s = TraceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


async def _trace(store, node, model, status, message):
    await store.record_trace(
        request_id=f"r{time.time_ns()}",
        model=model,
        original_model=model,
        node_id=node,
        status=status,
        error_message=message,
    )


class TestBackendFailuresQuery:
    @pytest.mark.asyncio
    async def test_tools_refusals_are_their_own_kind(self, store):
        tools = (
            "Client error '400 Bad Request' for url 'http://n:11434/api/chat': "
            "registry.ollama.ai/library/gemma3:27b does not support tools"
        )
        await _trace(store, "n", GEMMA, "failed", tools)
        await _trace(store, "n", GEMMA, "failed", "Client error '400 Bad Request' for url 'x'")
        failures = await store.get_backend_failures()
        assert failures["tools_unsupported"][("n", GEMMA)]["failed"] == 1
        assert failures["model_not_found"] == {}
