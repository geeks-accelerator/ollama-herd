"""Desktop/web chat-client compatibility.

A source read of 16 chat clients against Herd found five places the router
diverged from Ollama's API badly enough to break them.  One test class per fix:

- ``/api/tags`` field parity  — OllamaKit (Enchanted, Ollamac) and Reins decode
  ``digest``, ``modified_at`` and every ``details`` string as required non-null.
- ``/api/show``               — Ollama's desktop app, AnythingLLM, Cherry Studio.
- ``HEAD /``                  — OllamaKit's reachability probe (needs a 2xx).
- ``/v1/embeddings``          — Chatbox Knowledge Base, generic OpenAI embedders.
- opt-in CORS                 — browser clients (Hollama, TypingMind, Chatbox web).
"""

from __future__ import annotations

import base64
import json
import re
import struct
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    ImageMetrics,
    ImageModel,
    ModelTagMeta,
    VisionEmbeddingMetrics,
    VisionEmbeddingModel,
)
from tests.conftest import make_heartbeat
from tests.test_server.test_routes import create_test_app

REQUIRED_DETAIL_STRINGS = (
    "parent_model", "format", "family", "parameter_size", "quantization_level",
)

GPT_OSS_META = ModelTagMeta(
    modified_at="2026-08-14T10:22:21.109982-07:00",
    digest="a" * 64,
    format="gguf",
    family="gptoss",
    families=["gptoss"],
    parameter_size="116.8B",
    quantization_level="MXFP4",
    parent_model="",
    capabilities=["completion", "tools", "thinking"],
)


def _assert_ollama_shape(entry: dict) -> None:
    """Every key strict clients decode as required must be present and non-null."""
    for key in ("name", "model", "modified_at", "digest"):
        assert isinstance(entry[key], str) and entry[key], key
    assert isinstance(entry["size"], int)
    details = entry["details"]
    for key in REQUIRED_DETAIL_STRINGS:
        assert isinstance(details[key], str), key
    assert isinstance(details["families"], list)
    assert isinstance(details["fleet_nodes"], list) and details["fleet_nodes"]
    assert isinstance(entry["capabilities"], list)
    # RFC3339 — must parse
    datetime.fromisoformat(entry["modified_at"].replace("Z", "+00:00"))


@pytest.fixture
def app_client(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as c:
        yield c


def _install_mock_ollama(client: TestClient, node_id: str, handler) -> None:
    """Point the router's per-node httpx client at a MockTransport."""
    proxy = client.app.state.streaming_proxy
    node = client.app.state.registry.get_node(node_id)
    proxy._clients[node_id] = httpx.AsyncClient(
        base_url=node.ollama_base_url, transport=httpx.MockTransport(handler),
    )
    proxy._client_urls[node_id] = node.ollama_base_url


# ---------------------------------------------------------------------------
# 1. /api/tags field parity
# ---------------------------------------------------------------------------


class TestTagsFieldParity:
    def test_passes_through_real_ollama_metadata(self, app_client):
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[("gpt-oss:120b", 65.0)],
            available_models=["gpt-oss:120b"],
        )
        hb.ollama.models_available_meta = {"gpt-oss:120b": GPT_OSS_META}
        hb.ollama.models_available_sizes = {"gpt-oss:120b": 65.290069606}
        app_client.post("/heartbeat", json=hb.model_dump())

        models = app_client.get("/api/tags").json()["models"]
        assert len(models) == 1
        entry = models[0]
        _assert_ollama_shape(entry)
        assert entry["modified_at"] == GPT_OSS_META.modified_at
        assert entry["digest"] == GPT_OSS_META.digest
        assert entry["details"]["format"] == "gguf"
        assert entry["details"]["family"] == "gptoss"
        assert entry["details"]["families"] == ["gptoss"]
        assert entry["details"]["parameter_size"] == "116.8B"
        assert entry["details"]["quantization_level"] == "MXFP4"
        assert entry["details"]["fleet_nodes"] == ["studio"]
        # Top-level, as Ollama returns it — not under ``details``.
        assert entry["capabilities"] == ["completion", "tools", "thinking"]
        # Ollama's /api/tags reports ON-DISK bytes, not resident size.
        assert entry["size"] == 65_290_069_606

    def test_mlx_and_legacy_agent_models_get_non_null_defaults(self, app_client):
        # No models_available_meta at all == an older node agent.
        hb = make_heartbeat(
            node_id="studio",
            available_models=["llama3:8b", "mlx:Qwen3-Coder-Next-4bit"],
        )
        hb.ollama.models_available_sizes = {"mlx:Qwen3-Coder-Next-4bit": 44.8}
        app_client.post("/heartbeat", json=hb.model_dump())

        models = {m["name"]: m for m in app_client.get("/api/tags").json()["models"]}
        for entry in models.values():
            _assert_ollama_shape(entry)
            assert re.fullmatch(r"[0-9a-f]{64}", entry["digest"])

        mlx = models["mlx:Qwen3-Coder-Next-4bit"]
        assert mlx["details"]["format"] == "mlx"
        assert mlx["details"]["families"] == []
        assert mlx["size"] == 44_800_000_000
        # Unknown provenance → empty format, not a guess.
        assert models["llama3:8b"]["details"]["format"] == ""
        # Synthetic digests are unique per model (clients key lists on them).
        assert mlx["digest"] != models["llama3:8b"]["digest"]

    def test_synthesized_values_are_stable_across_calls(self, app_client):
        hb = make_heartbeat(node_id="studio", available_models=["mlx:foo"])
        app_client.post("/heartbeat", json=hb.model_dump())
        first = app_client.get("/api/tags").json()["models"][0]
        second = app_client.get("/api/tags").json()["models"][0]
        assert first["modified_at"] == second["modified_at"]
        assert first["digest"] == second["digest"]

    def test_fleet_nodes_merges_across_nodes(self, app_client):
        for node_id, ip in (("studio", "10.0.0.1"), ("mbp", "10.0.0.2")):
            hb = make_heartbeat(node_id=node_id, available_models=["phi4:14b"], lan_ip=ip)
            if node_id == "mbp":
                hb.ollama.models_available_meta = {
                    "phi4:14b": ModelTagMeta(digest="b" * 64, format="gguf"),
                }
            app_client.post("/heartbeat", json=hb.model_dump())
        models = app_client.get("/api/tags").json()["models"]
        assert len(models) == 1
        assert sorted(models[0]["details"]["fleet_nodes"]) == ["mbp", "studio"]
        # Metadata from whichever node reported it.
        assert models[0]["digest"] == "b" * 64

    def test_image_and_vision_models_keep_type_and_gain_required_keys(self, app_client):
        hb = make_heartbeat(node_id="studio")
        hb.image = ImageMetrics(models_available=[
            ImageModel(name="z-image-turbo", binary="mflux-generate-z-image-turbo"),
        ])
        hb.vision_embedding = VisionEmbeddingMetrics(models_available=[
            VisionEmbeddingModel(name="dinov2-vit-s14", runtime="onnx", dimensions=384),
        ])
        app_client.post("/heartbeat", json=hb.model_dump())
        models = {m["name"]: m for m in app_client.get("/api/tags").json()["models"]}
        image = models["z-image-turbo"]
        _assert_ollama_shape(image)
        assert image["details"]["type"] == "image"
        assert image["details"]["format"] == "mflux"
        vision = models["dinov2-vit-s14"]
        _assert_ollama_shape(vision)
        assert vision["details"]["type"] == "vision-embedding"
        assert vision["details"]["dimensions"] == 384
        assert vision["details"]["format"] == "onnx"

    def test_fleet_status_does_not_carry_tag_metadata(self, app_client):
        hb = make_heartbeat(node_id="studio", available_models=["gpt-oss:120b"])
        hb.ollama.models_available_meta = {"gpt-oss:120b": GPT_OSS_META}
        app_client.post("/heartbeat", json=hb.model_dump())
        node = app_client.get("/fleet/status").json()["nodes"][0]
        assert "models_available_meta" not in node["ollama"]
        assert node["ollama"]["models_available"] == ["gpt-oss:120b"]


class TestOllamaClientTagMeta:
    @pytest.mark.asyncio
    async def test_coerces_ollama_nulls(self):
        from fleet_manager.common.ollama_client import OllamaClient

        tags = {"models": [{
            "name": "phi4:14b", "model": "phi4:14b",
            "modified_at": "2026-01-01T00:00:00Z", "size": 9_000_000_000,
            "digest": "c" * 64,
            "details": {
                "parent_model": None, "format": "gguf", "family": "phi3",
                "families": None, "parameter_size": "14.7B",
                "quantization_level": "Q4_K_M",
            },
            "capabilities": None,
        }, {"name": "no-details:1b", "model": "no-details:1b"}, {
            "name": "qwen3.8:27b", "model": "qwen3.8:27b",
            "capabilities": ["completion", None, "vision", "thinking"],
        }]}

        client = OllamaClient()
        client._client = httpx.AsyncClient(
            base_url="http://ollama",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=tags)),
        )
        meta = await client.get_available_model_meta()
        await client.close()

        assert meta["phi4:14b"].parent_model == ""
        assert meta["phi4:14b"].families == []
        assert meta["phi4:14b"].quantization_level == "Q4_K_M"
        assert meta["phi4:14b"].capabilities == []
        assert meta["no-details:1b"] == ModelTagMeta()
        assert meta["qwen3.8:27b"].capabilities == ["completion", "vision", "thinking"]

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self):
        from fleet_manager.common.ollama_client import OllamaClient

        client = OllamaClient()
        client._client = httpx.AsyncClient(
            base_url="http://ollama",
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        )
        assert await client.get_available_model_meta() == {}
        await client.close()

    @pytest.mark.asyncio
    async def test_collector_carries_meta_into_heartbeat(self):
        from fleet_manager.node.collector import collect_heartbeat

        ollama = MagicMock()
        ollama.get_running_models = AsyncMock(return_value=[])
        ollama.get_available_models = AsyncMock(return_value=["gpt-oss:120b"])
        ollama.get_version = AsyncMock(return_value="0.34.4")
        ollama.get_available_model_sizes = AsyncMock(return_value={})
        ollama.get_available_model_meta = AsyncMock(
            return_value={"gpt-oss:120b": GPT_OSS_META},
        )
        payload = await collect_heartbeat("test-node", ollama)
        assert payload.ollama.models_available_meta["gpt-oss:120b"] == GPT_OSS_META

    @pytest.mark.asyncio
    async def test_collector_sends_meta_only_on_change_or_refresh(self, monkeypatch):
        from fleet_manager.node import collector

        ollama = MagicMock()
        ollama.get_running_models = AsyncMock(return_value=[])
        ollama.get_available_models = AsyncMock(return_value=["gpt-oss:120b"])
        ollama.get_available_model_meta = AsyncMock(
            return_value={"gpt-oss:120b": GPT_OSS_META},
        )
        clock = [1000.0]
        monkeypatch.setattr(collector.time, "time", lambda: clock[0])

        first = await collector.collect_heartbeat("n", ollama)
        assert first.ollama.models_available_meta is not None
        clock[0] += 5
        second = await collector.collect_heartbeat("n", ollama)
        assert second.ollama.models_available_meta is None  # unchanged → omitted

        # A pull changes the metadata → sent immediately.
        changed = {"gpt-oss:120b": GPT_OSS_META, "phi4:14b": ModelTagMeta(format="gguf")}
        ollama.get_available_model_meta = AsyncMock(return_value=changed)
        clock[0] += 5
        third = await collector.collect_heartbeat("n", ollama)
        assert set(third.ollama.models_available_meta) == {"gpt-oss:120b", "phi4:14b"}

        # Unchanged but past the refresh window → re-sent (re-seeds a
        # restarted router).
        clock[0] += collector.META_REFRESH_S + 1
        fourth = await collector.collect_heartbeat("n", ollama)
        assert fourth.ollama.models_available_meta is not None

    def test_registry_keeps_meta_when_heartbeat_omits_it(self, app_client):
        hb = make_heartbeat(node_id="studio", available_models=["gpt-oss:120b"])
        hb.ollama.models_available_meta = {"gpt-oss:120b": GPT_OSS_META}
        app_client.post("/heartbeat", json=hb.model_dump())
        # Next heartbeat: unchanged, so the agent sends null.
        hb2 = make_heartbeat(node_id="studio", available_models=["gpt-oss:120b"])
        assert hb2.ollama.models_available_meta is None
        app_client.post("/heartbeat", json=hb2.model_dump())
        entry = app_client.get("/api/tags").json()["models"][0]
        assert entry["digest"] == GPT_OSS_META.digest

    @pytest.mark.asyncio
    async def test_collector_meta_failure_keeps_model_list(self):
        from fleet_manager.node.collector import collect_heartbeat

        ollama = MagicMock()
        ollama.get_running_models = AsyncMock(return_value=[])
        ollama.get_available_models = AsyncMock(return_value=["gpt-oss:120b"])
        ollama.get_available_model_meta = AsyncMock(side_effect=RuntimeError("boom"))
        payload = await collect_heartbeat("test-node", ollama)
        assert payload.ollama.models_available == ["gpt-oss:120b"]
        # None = "unchanged": a failed probe must not wipe the router's copy.
        assert payload.ollama.models_available_meta is None


# ---------------------------------------------------------------------------
# 2. /api/show
# ---------------------------------------------------------------------------

SHOW_RESPONSE = {
    "modelfile": "FROM ...",
    "parameters": "stop <|end|>",
    "template": "{{ .Prompt }}",
    "details": {"format": "gguf", "family": "gptoss"},
    "model_info": {"general.architecture": "gptoss", "gptoss.context_length": 131072},
    "capabilities": ["completion", "tools", "thinking"],
}


class TestApiShow:
    def test_proxies_to_node_with_model_accepting_legacy_name(self, app_client):
        hb = make_heartbeat(node_id="studio", available_models=["llama3:latest"])
        app_client.post("/heartbeat", json=hb.model_dump())

        seen = {}

        def handler(request: httpx.Request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=SHOW_RESPONSE)

        _install_mock_ollama(app_client, "studio", handler)
        # Legacy "name" field + bare name (Ollama resolves to :latest).
        resp = app_client.post("/api/show", json={"name": "llama3", "verbose": True})
        assert resp.status_code == 200
        assert resp.json() == SHOW_RESPONSE
        assert resp.headers["X-Fleet-Node"] == "studio"
        assert seen["path"] == "/api/show"
        assert seen["body"] == {"model": "llama3:latest", "verbose": True}

    def test_unknown_model_is_ollama_style_404(self, app_client):
        resp = app_client.post("/api/show", json={"model": "nope:1b"})
        assert resp.status_code == 404
        assert resp.json() == {"error": "model 'nope:1b' not found"}

    def test_missing_model_is_400(self, app_client):
        resp = app_client.post("/api/show", json={})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_mlx_model_is_synthesized(self, app_client):
        hb = make_heartbeat(node_id="studio", available_models=["mlx:Qwen3-Coder-Next-4bit"])
        app_client.post("/heartbeat", json=hb.model_dump())
        resp = app_client.post("/api/show", json={"model": "mlx:Qwen3-Coder-Next-4bit"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["capabilities"] == ["completion"]
        assert body["details"]["format"] == "mlx"
        for key in REQUIRED_DETAIL_STRINGS:
            assert isinstance(body["details"][key], str)
        assert body["model_info"] == {}
        for key in ("modelfile", "parameters", "template"):
            assert body[key] == ""

    def test_unknown_mlx_model_is_404(self, app_client):
        resp = app_client.post("/api/show", json={"model": "mlx:missing"})
        assert resp.status_code == 404

    def test_fails_over_to_next_node(self, app_client):
        # studio has it loaded (tried first) but is unreachable.
        app_client.post("/heartbeat", json=make_heartbeat(
            node_id="studio", lan_ip="10.0.0.1",
            loaded_models=[("phi4:14b", 9.0)], available_models=["phi4:14b"],
        ).model_dump())
        app_client.post("/heartbeat", json=make_heartbeat(
            node_id="mbp", lan_ip="10.0.0.2", available_models=["phi4:14b"],
        ).model_dump())

        def down(request):
            raise httpx.ConnectError("refused", request=request)

        _install_mock_ollama(app_client, "studio", down)
        _install_mock_ollama(app_client, "mbp", lambda r: httpx.Response(200, json=SHOW_RESPONSE))

        resp = app_client.post("/api/show", json={"model": "phi4:14b"})
        assert resp.status_code == 200
        assert resp.headers["X-Fleet-Node"] == "mbp"

    def test_all_nodes_404_passes_through(self, app_client):
        app_client.post("/heartbeat", json=make_heartbeat(
            node_id="studio", available_models=["gone:1b"],
        ).model_dump())
        _install_mock_ollama(
            app_client, "studio",
            lambda r: httpx.Response(404, json={"error": "model 'gone:1b' not found"}),
        )
        resp = app_client.post("/api/show", json={"model": "gone:1b"})
        assert resp.status_code == 404
        assert resp.json() == {"error": "model 'gone:1b' not found"}

    def test_vision_embedding_model_is_synthesized(self, app_client):
        hb = make_heartbeat(node_id="studio")
        hb.vision_embedding = VisionEmbeddingMetrics(models_available=[
            VisionEmbeddingModel(name="dinov2-vit-s14", runtime="onnx", dimensions=384),
        ])
        app_client.post("/heartbeat", json=hb.model_dump())
        body = app_client.post("/api/show", json={"model": "dinov2-vit-s14"}).json()
        assert body["capabilities"] == ["embedding"]
        assert body["details"]["format"] == "onnx"


# ---------------------------------------------------------------------------
# 3. HEAD /
# ---------------------------------------------------------------------------


class TestRootHead:
    def test_head_root_is_200_and_get_still_redirects(self):
        from fleet_manager.server.app import create_app

        # No `with`: lifespan (mDNS, stores) is not needed for these routes.
        client = TestClient(create_app(ServerSettings()))
        head = client.head("/")
        assert head.status_code == 200
        assert head.content == b""

        get = client.get("/", follow_redirects=False)
        assert get.status_code in (302, 307)
        assert get.headers["location"] == "/dashboard"


# ---------------------------------------------------------------------------
# 4. /v1/embeddings
# ---------------------------------------------------------------------------


def _register_embed_node(client: TestClient, *, model: str = "mxbai-embed-large:latest"):
    hb = make_heartbeat(
        node_id="studio", loaded_models=[(model, 1.0)], available_models=[model],
    )
    client.post("/heartbeat", json=hb.model_dump())


class TestOpenAIEmbeddings:
    def test_ollama_path_returns_openai_shape(self, app_client):
        _register_embed_node(app_client)
        seen = {}

        def handler(request: httpx.Request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "model": "mxbai-embed-large:latest",
                "embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
                "prompt_eval_count": 7,
            })

        _install_mock_ollama(app_client, "studio", handler)
        resp = app_client.post("/v1/embeddings", json={
            "model": "mxbai-embed-large:latest", "input": ["hello", "world"],
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "list"
        assert body["model"] == "mxbai-embed-large:latest"
        assert [d["index"] for d in body["data"]] == [0, 1]
        assert all(d["object"] == "embedding" for d in body["data"])
        assert body["data"][1]["embedding"] == [0.4, 0.5, 0.6]
        assert body["usage"] == {"prompt_tokens": 7, "total_tokens": 7}
        # Same path /api/embed uses → same backend endpoint and headers.
        assert seen["path"] == "/api/embed"
        assert seen["body"]["input"] == ["hello", "world"]
        assert resp.headers["X-Fleet-Node"] == "studio"
        assert resp.headers["X-Fleet-Backend"] == "ollama"

    def test_string_input_and_base64_encoding(self, app_client):
        _register_embed_node(app_client)
        vec = [0.25, -1.5, 3.0]
        _install_mock_ollama(app_client, "studio", lambda r: httpx.Response(
            200, json={"embeddings": [vec]},
        ))
        resp = app_client.post("/v1/embeddings", json={
            "model": "mxbai-embed-large:latest", "input": "hello",
            "encoding_format": "base64",
        })
        assert resp.status_code == 200, resp.text
        encoded = resp.json()["data"][0]["embedding"]
        decoded = list(struct.unpack("<3f", base64.b64decode(encoded)))
        assert decoded == vec
        assert resp.json()["usage"] == {"prompt_tokens": 0, "total_tokens": 0}

    def test_nomic_goes_through_fastembed_interception(self, app_client, monkeypatch):
        """nomic-embed-text must hit the native :11439 server, not Ollama."""
        from fleet_manager.server.routes import text_embedding_compat

        hb = make_heartbeat(node_id="studio")
        hb.text_embedding_port = 11439
        app_client.post("/heartbeat", json=hb.model_dump())

        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "model": "nomic-embed-text",
                "embeddings": [[1.0, 2.0]],
                "prompt_eval_count": 2,
            })

        real_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(text_embedding_compat.httpx, "AsyncClient", patched)

        resp = app_client.post("/v1/embeddings", json={
            "model": "nomic-embed-text", "input": "hello there",
        })
        monkeypatch.setattr(text_embedding_compat.httpx, "AsyncClient", real_client)
        assert resp.status_code == 200, resp.text
        assert seen["url"].endswith(":11439/embed")
        assert seen["body"] == {"model": "nomic-embed-text", "input": ["hello there"]}
        body = resp.json()
        assert body["data"] == [{"object": "embedding", "index": 0, "embedding": [1.0, 2.0]}]
        assert body["usage"]["prompt_tokens"] == 2
        assert resp.headers["X-Fleet-Backend"] == "native"
        # The node id the fastembed path adds to its Ollama-shaped body must not
        # leak into the OpenAI response.
        assert "node" not in body

    @pytest.mark.parametrize("payload,fragment", [
        ({"input": "x"}, "model is required"),
        ({"model": "m", "input": [[1, 2, 3]]}, "list of strings"),
        ({"model": "m"}, "list of strings"),
        ({"model": "m", "input": ["ok", ""]}, "no empty strings"),
        ({"model": "m", "input": "x", "encoding_format": "int8"}, "encoding_format"),
        ({"model": "m", "input": "x", "dimensions": -1}, "dimensions"),
    ])
    def test_validation_errors_are_openai_shaped_400s(self, app_client, payload, fragment):
        resp = app_client.post("/v1/embeddings", json=payload)
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert fragment in err["message"]
        assert err["type"] == "invalid_request_error"

    def test_unknown_model_is_404_model_not_found(self, app_client):
        resp = app_client.post("/v1/embeddings", json={"model": "nope:1b", "input": "x"})
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "model_not_found"
        assert "nope:1b" in err["message"]

    def test_ignored_dimensions_fails_loud(self, app_client):
        _register_embed_node(app_client)
        _install_mock_ollama(app_client, "studio", lambda r: httpx.Response(
            200, json={"embeddings": [[0.1, 0.2, 0.3]]},
        ))
        resp = app_client.post("/v1/embeddings", json={
            "model": "mxbai-embed-large:latest", "input": "x", "dimensions": 2,
        })
        assert resp.status_code == 400
        assert "dimensions=2" in resp.json()["error"]["message"]

    def test_count_mismatch_is_502(self, app_client):
        _register_embed_node(app_client)
        _install_mock_ollama(app_client, "studio", lambda r: httpx.Response(
            200, json={"embeddings": [[0.1]]},
        ))
        resp = app_client.post("/v1/embeddings", json={
            "model": "mxbai-embed-large:latest", "input": ["a", "b"],
        })
        assert resp.status_code == 502

    def test_backend_error_is_translated(self, app_client):
        _register_embed_node(app_client)
        _install_mock_ollama(app_client, "studio", lambda r: httpx.Response(
            400, json={"error": "input too long"},
        ))
        resp = app_client.post("/v1/embeddings", json={
            "model": "mxbai-embed-large:latest", "input": "x",
        })
        assert resp.status_code == 400
        assert "input too long" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 5. Opt-in CORS
# ---------------------------------------------------------------------------


def _preflight(client: TestClient, origin: str, path: str = "/api/tags"):
    return client.options(path, headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,authorization",
    })


class TestCors:
    def _client(self, origins: str = "") -> TestClient:
        from fleet_manager.server.app import create_app

        return TestClient(create_app(ServerSettings(cors_origins=origins)))

    def test_default_is_off(self):
        client = self._client()
        assert ServerSettings().cors_origins == ""
        pre = _preflight(client, "http://localhost:3000")
        assert pre.status_code == 405
        assert "access-control-allow-origin" not in pre.headers
        get = client.get("/", headers={"Origin": "http://evil.example"},
                         follow_redirects=False)
        assert "access-control-allow-origin" not in get.headers

    def test_wildcard_pattern_allows_matching_origin_only(self):
        client = self._client("http://localhost:*, https://hollama.fernando.is")
        ok = _preflight(client, "http://localhost:3000")
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert "POST" in ok.headers["access-control-allow-methods"]

        exact = _preflight(client, "https://hollama.fernando.is")
        assert exact.status_code == 200

        bad = _preflight(client, "http://evil.example")
        assert bad.status_code == 400
        assert "access-control-allow-origin" not in bad.headers

    def test_star_allows_any_origin_and_exposes_fleet_headers(self):
        client = self._client("*")
        pre = _preflight(client, "https://anything.example")
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == "*"
        get = client.get("/", headers={"Origin": "https://anything.example"},
                         follow_redirects=False)
        assert get.headers["access-control-allow-origin"] == "*"
        assert "X-Fleet-Node" in get.headers["access-control-expose-headers"]

    def test_env_var_is_read(self, monkeypatch):
        monkeypatch.setenv("FLEET_CORS_ORIGINS", "chrome-extension://*")
        assert ServerSettings().cors_origins == "chrome-extension://*"

    @pytest.mark.parametrize("raw,expected", [
        ("", ([], None)),
        (" , ", ([], None)),
        ("*", (["*"], None)),
        ("http://a.test,*", (["*"], None)),
        ("http://a.test/, http://b.test", (["http://a.test", "http://b.test"], None)),
        ("http://localhost:*", ([], r"(?:http://localhost:.*)")),
    ])
    def test_parse_cors_origins(self, raw, expected):
        from fleet_manager.server.cors import parse_cors_origins

        assert parse_cors_origins(raw) == expected

    def test_wildcard_regex_is_anchored(self):
        from fleet_manager.server.cors import parse_cors_origins

        _, regex = parse_cors_origins("http://localhost:*")
        assert re.fullmatch(regex, "http://localhost:8080")
        assert not re.fullmatch(regex, "http://evil.example/?http://localhost:1")
        assert not re.fullmatch(regex, "https://localhost:8080")


# ---------------------------------------------------------------------------
# model_has_capability — presence-only reading of Ollama's capabilities
# ---------------------------------------------------------------------------


class TestModelHasCapability:
    """Ollama 0.33.x under-reports capabilities in /api/tags (gemma3:27b listed
    ["completion"] while /api/show said ["completion", "vision"]; fixed in
    0.34.1).  So the helper believes what is reported and treats a missing
    entry as unknown — never as a "no" that callers would act on."""

    def _node(self, meta):
        node = MagicMock()
        node.ollama.models_available_meta = meta
        return node

    def test_reported_capability_is_true(self):
        from fleet_manager.server.serializers import model_has_capability

        node = self._node({"qwen3.8:27b": ModelTagMeta(capabilities=["thinking"])})
        assert model_has_capability(node, "qwen3.8:27b", "thinking")

    def test_absent_capability_is_false_not_an_error(self):
        from fleet_manager.server.serializers import model_has_capability

        node = self._node({"gemma3:27b": ModelTagMeta(capabilities=["completion"])})
        assert not model_has_capability(node, "gemma3:27b", "vision")

    def test_bare_name_resolves_to_latest_like_ollama(self):
        from fleet_manager.server.serializers import model_has_capability

        node = self._node({"nomic-embed-text:latest": ModelTagMeta(capabilities=["embedding"])})
        assert model_has_capability(node, "nomic-embed-text", "embedding")

    def test_no_meta_or_no_node_is_false(self):
        from fleet_manager.server.serializers import model_has_capability

        assert not model_has_capability(self._node(None), "x:1b", "thinking")
        assert not model_has_capability(self._node({}), "x:1b", "thinking")
        assert not model_has_capability(None, "x:1b", "thinking")
        assert not model_has_capability(self._node({}), "", "thinking")


class TestSynthesizedCapabilitiesAgree:
    """/api/tags and /api/show must report the same capabilities for models
    herd synthesizes (no Ollama serves them).  Ollama 0.34.1 fixed exactly this
    disagreement between its own two endpoints."""

    def test_vision_embedding_model_agrees_across_endpoints(self, app_client):
        hb = make_heartbeat(node_id="mini", available_models=["gemma3:27b"])
        hb.vision_embedding = VisionEmbeddingMetrics(models_available=[
            VisionEmbeddingModel(name="dinov2-vit-s14", runtime="onnx", dimensions=384),
        ])
        app_client.post("/heartbeat", json=hb.model_dump())

        tags = {m["name"]: m for m in app_client.get("/api/tags").json()["models"]}
        show = app_client.post("/api/show", json={"model": "dinov2-vit-s14"}).json()
        assert tags["dinov2-vit-s14"]["capabilities"] == ["embedding"]
        assert show["capabilities"] == tags["dinov2-vit-s14"]["capabilities"]

    def test_synthesized_constants_are_not_shared_by_reference(self, app_client):
        from fleet_manager.server.routes import ollama_compat

        hb = make_heartbeat(node_id="mini", available_models=["gemma3:27b"])
        hb.vision_embedding = VisionEmbeddingMetrics(models_available=[
            VisionEmbeddingModel(name="dinov2-vit-s14", runtime="onnx", dimensions=384),
        ])
        app_client.post("/heartbeat", json=hb.model_dump())
        app_client.get("/api/tags")
        app_client.post("/api/show", json={"model": "dinov2-vit-s14"})
        assert ollama_compat._VISION_EMBEDDING_CAPABILITIES == ["embedding"]


class TestModelsSupportsVision:
    def test_reported_vision_capability_is_advertised(self, app_client):
        """qwen3.8 matches no vision name pattern; Ollama reports vision for it,
        so a client honoring supports_vision can offer to send it an image."""
        hb = make_heartbeat(node_id="mini", available_models=["qwen3.8:27b", "phi4:14b"])
        hb.ollama.models_available_meta = {
            "qwen3.8:27b": ModelTagMeta(capabilities=["completion", "vision"]),
            "phi4:14b": ModelTagMeta(capabilities=["completion"]),
        }
        app_client.post("/heartbeat", json=hb.model_dump())

        entries = {m["id"]: m for m in app_client.get("/v1/models").json()["models"]}
        assert entries["qwen3.8:27b"]["supports_vision"] is True
        assert entries["phi4:14b"]["supports_vision"] is False
