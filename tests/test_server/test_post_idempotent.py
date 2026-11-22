"""``post_idempotent``: one retry when a reused keep-alive connection turns
out to have been closed by the server (run 6: 3 of ~23,000 vision embeddings),
shared by vision embeddings, native text embeddings and reranks, and Ollama's
/api/embed."""

from __future__ import annotations

import logging

import httpx
import pytest

from fleet_manager.server.streaming import post_idempotent


def _client(failures: list[Exception]):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if failures:
            raise failures.pop(0)
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://n"), calls


@pytest.mark.parametrize("error", [httpx.ReadError("closed"), httpx.RemoteProtocolError("closed")])
async def test_a_closed_connection_is_retried_once(error, caplog):
    caplog.set_level(logging.INFO, logger="fleet_manager.server.streaming")
    client, calls = _client([error])
    resp = await post_idempotent(client, "/embed", label="Embed on n1", json={})
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert "retrying once" in caplog.text


async def test_it_gives_up_after_the_second_failure():
    client, calls = _client([httpx.ReadError("closed"), httpx.ReadError("closed")])
    with pytest.raises(httpx.ReadError):
        await post_idempotent(client, "/embed", label="Embed on n1", json={})
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")], ids=["timeout", "refused"]
)
async def test_a_server_in_trouble_is_not_retried(error):
    client, calls = _client([error])
    with pytest.raises(type(error)):
        await post_idempotent(client, "/embed", label="Embed on n1", json={})
    assert calls["n"] == 1
