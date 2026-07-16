"""Tests for the per-client concurrency cap (#3b)."""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import InferenceRequest, QueueEntry
from fleet_manager.server.queue_manager import (
    ClientConcurrencyExceeded,
    QueueManager,
)
from fleet_manager.server.routes.routing import client_concurrency_response


def _entry(ip: str) -> QueueEntry:
    return QueueEntry(request=InferenceRequest(model="m", client_ip=ip), assigned_node="bb")


def _qm(limit: int) -> QueueManager:
    return QueueManager(settings=ServerSettings(client_max_in_flight=limit))


# ---------------------------------------------------------------------------
# acquire / release counter lifecycle (sync, deterministic)
# ---------------------------------------------------------------------------


def test_cap_disabled_never_raises():
    qm = _qm(0)  # default: unlimited
    for _ in range(50):
        qm._acquire_client(_entry("1.2.3.4"))  # must not raise
    assert qm._client_in_flight == {}  # nothing counted when disabled


def test_cap_enforced_at_limit():
    qm = _qm(2)
    qm._acquire_client(_entry("1.2.3.4"))
    qm._acquire_client(_entry("1.2.3.4"))
    with pytest.raises(ClientConcurrencyExceeded) as ei:
        qm._acquire_client(_entry("1.2.3.4"))
    assert ei.value.limit == 2
    assert ei.value.client_ip == "1.2.3.4"
    assert ei.value.retry_after == 2  # from default setting


def test_release_frees_a_slot():
    qm = _qm(1)
    e1 = _entry("1.2.3.4")
    qm._acquire_client(e1)
    e2 = _entry("1.2.3.4")
    with pytest.raises(ClientConcurrencyExceeded):
        qm._acquire_client(e2)
    qm._release_client(e1)          # free the slot
    qm._acquire_client(e2)          # now succeeds
    assert qm._client_in_flight["1.2.3.4"] == 1


def test_release_is_idempotent_no_underflow():
    qm = _qm(3)
    e1 = _entry("1.2.3.4")
    qm._acquire_client(e1)
    qm._release_client(e1)
    qm._release_client(e1)          # double release must not underflow
    qm._release_client(e1)
    assert "1.2.3.4" not in qm._client_in_flight
    # counter is clean — three fresh acquires still allowed
    for _ in range(3):
        qm._acquire_client(_entry("1.2.3.4"))


def test_anonymous_clients_not_capped():
    qm = _qm(1)
    for _ in range(10):
        qm._acquire_client(_entry(""))  # no IP → never capped
    assert qm._client_in_flight == {}


def test_clients_are_independent():
    qm = _qm(1)
    qm._acquire_client(_entry("10.0.0.1"))
    qm._acquire_client(_entry("10.0.0.2"))  # different client, own budget
    with pytest.raises(ClientConcurrencyExceeded):
        qm._acquire_client(_entry("10.0.0.1"))


# ---------------------------------------------------------------------------
# enqueue integration + the 429 response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_raises_over_cap():
    qm = _qm(1)

    def _process_fn(entry):
        async def _gen():
            yield "data: {}\n\n"
        return _gen()

    await qm.enqueue(_entry("9.9.9.9"), _process_fn)  # first: ok
    with pytest.raises(ClientConcurrencyExceeded):
        await qm.enqueue(_entry("9.9.9.9"), _process_fn)  # second: rejected
    await qm.shutdown()


def test_client_concurrency_response_is_429_with_retry_after():
    resp = client_concurrency_response(ClientConcurrencyExceeded("1.2.3.4", 4, 7))
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "7"
