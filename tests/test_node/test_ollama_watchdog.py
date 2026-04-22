"""Tests for the Ollama watchdog — probe behavior, kick logic, cooldown."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fleet_manager.node.ollama_watchdog import OllamaWatchdog

# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_tags_ok(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/tags",
        json={"models": []},
        status_code=200,
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        ok, reason = await wd._probe_tags(c)
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_probe_tags_non_200_fails(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/tags",
        status_code=503,
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        ok, reason = await wd._probe_tags(c)
    assert ok is False
    assert reason == "tags_http_503"


@pytest.mark.asyncio
async def test_probe_chat_ok(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/chat",
        method="POST",
        json={"message": {"content": "hi"}, "done": True},
        status_code=200,
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        ok, reason = await wd._probe_chat(c, "qwen3-coder:30b")
    assert ok is True


@pytest.mark.asyncio
async def test_probe_chat_500_fails(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/chat",
        method="POST",
        status_code=500,
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        ok, reason = await wd._probe_chat(c, "qwen3-coder:30b")
    assert ok is False
    assert reason == "chat_http_500"


@pytest.mark.asyncio
async def test_pick_probe_model_returns_smallest_hot(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/ps",
        json={
            "models": [
                {"model": "big:70b", "size": 42_000_000_000},
                {"model": "small:4b", "size": 3_300_000_000},
                {"model": "medium:27b", "size": 17_000_000_000},
            ],
        },
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        m = await wd._pick_probe_model(c)
    assert m == "small:4b"


@pytest.mark.asyncio
async def test_pick_probe_model_returns_none_when_nothing_hot(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/ps",
        json={"models": []},
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        assert await wd._pick_probe_model(c) is None


@pytest.mark.asyncio
async def test_pick_probe_model_survives_ollama_errors(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/ps",
        status_code=500,
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        assert await wd._pick_probe_model(c) is None


# ---------------------------------------------------------------------------
# _kick_runners — subprocess call + stats tracking
# ---------------------------------------------------------------------------


def test_kick_runners_calls_pkill_on_unix_like():
    wd = OllamaWatchdog()
    calls: list = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stderr=b"")

    with patch("fleet_manager.node.ollama_watchdog.sys.platform", "darwin"), patch(
        "fleet_manager.node.ollama_watchdog.subprocess.run", _fake_run,
    ):
        result = wd._kick_runners("test_reason")

    assert result is True
    assert len(calls) == 1
    assert calls[0][0] == "pkill"
    assert "-9" in calls[0]
    assert "ollama runner" in calls[0]
    # Stats updated
    assert wd.stats["kicks_total"] == 1
    assert wd.stats["last_kick_reason"] == "test_reason"
    assert wd.stats["last_kick_at"] is not None


def test_kick_runners_uses_taskkill_on_windows():
    wd = OllamaWatchdog()
    calls: list = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stderr=b"")

    with patch("fleet_manager.node.ollama_watchdog.sys.platform", "win32"), patch(
        "fleet_manager.node.ollama_watchdog.subprocess.run", _fake_run,
    ):
        wd._kick_runners("win_test")

    assert calls[0][0] == "taskkill"
    assert "/F" in calls[0]


def test_kick_runners_accepts_pkill_returncode_1():
    """pkill exits 1 when nothing matched — still a successful 'kick attempt'."""
    wd = OllamaWatchdog()
    with patch("fleet_manager.node.ollama_watchdog.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr=b"")
        result = wd._kick_runners("nothing_to_kill")
    assert result is True
    assert wd.stats["kicks_total"] == 1


def test_kick_runners_returns_false_when_binary_missing():
    wd = OllamaWatchdog()
    with patch(
        "fleet_manager.node.ollama_watchdog.subprocess.run",
        side_effect=FileNotFoundError(),
    ):
        result = wd._kick_runners("binary_missing")
    assert result is False
    assert wd.stats["kicks_total"] == 0


# ---------------------------------------------------------------------------
# Cooldown logic — must not thrash
# ---------------------------------------------------------------------------


def test_can_kick_true_at_startup():
    wd = OllamaWatchdog(cooldown_s=120.0)
    # Never kicked → always OK to kick
    assert wd._can_kick() is True


def test_can_kick_false_right_after_kick():
    wd = OllamaWatchdog(cooldown_s=120.0)
    wd._last_kick_ts = time.time()
    assert wd._can_kick() is False


def test_can_kick_true_after_cooldown():
    wd = OllamaWatchdog(cooldown_s=120.0)
    wd._last_kick_ts = time.time() - 200  # 200s ago > 120s cooldown
    assert wd._can_kick() is True


# ---------------------------------------------------------------------------
# Failure threshold — requires N consecutive failures before kicking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_failure_does_not_trigger_kick():
    wd = OllamaWatchdog(consecutive_failures_before_kick=2)
    with patch.object(wd, "_kick_runners") as mock_kick:
        await wd._record_failure("test")
    mock_kick.assert_not_called()
    assert wd._consecutive_failures == 1


@pytest.mark.asyncio
async def test_two_consecutive_failures_trigger_kick():
    wd = OllamaWatchdog(consecutive_failures_before_kick=2)
    with patch.object(wd, "_kick_runners", return_value=True) as mock_kick:
        await wd._record_failure("first")
        await wd._record_failure("second")
    assert mock_kick.call_count == 1
    # Counter resets after successful kick
    assert wd._consecutive_failures == 0


@pytest.mark.asyncio
async def test_kick_suppressed_when_cooldown_active():
    wd = OllamaWatchdog(consecutive_failures_before_kick=2, cooldown_s=120.0)
    wd._last_kick_ts = time.time()  # Just kicked
    with patch.object(wd, "_kick_runners") as mock_kick:
        await wd._record_failure("first")
        await wd._record_failure("second")
    # Two failures hit threshold but cooldown blocks the kick
    mock_kick.assert_not_called()


# ---------------------------------------------------------------------------
# _one_cycle — end-to-end probe cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_healthy_resets_failure_counter(httpx_mock):
    httpx_mock.add_response(url="http://localhost:11434/api/tags", json={"models": []})
    httpx_mock.add_response(
        url="http://localhost:11434/api/ps",
        json={"models": [{"model": "m:4b", "size": 3_000_000_000}]},
    )
    httpx_mock.add_response(
        url="http://localhost:11434/api/chat",
        method="POST",
        json={"message": {"content": "hi"}, "done": True},
    )
    wd = OllamaWatchdog()
    wd._consecutive_failures = 1  # pretend we had a prior failure
    async with httpx.AsyncClient() as c:
        await wd._one_cycle(c)
    assert wd._consecutive_failures == 0
    assert wd.stats["probes_total"] == 1
    assert wd.stats["probes_failed"] == 0


@pytest.mark.asyncio
async def test_cycle_tags_failure_increments_counter(httpx_mock):
    httpx_mock.add_response(url="http://localhost:11434/api/tags", status_code=503)
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        await wd._one_cycle(c)
    assert wd._consecutive_failures == 1
    assert wd.stats["probes_failed"] == 1


@pytest.mark.asyncio
async def test_cycle_no_hot_models_treated_as_healthy(httpx_mock):
    """Fresh boot state — Ollama is up but no models loaded yet."""
    httpx_mock.add_response(url="http://localhost:11434/api/tags", json={"models": []})
    httpx_mock.add_response(url="http://localhost:11434/api/ps", json={"models": []})
    wd = OllamaWatchdog()
    wd._consecutive_failures = 1
    async with httpx.AsyncClient() as c:
        await wd._one_cycle(c)
    # Tags was OK + nothing hot → reset counter
    assert wd._consecutive_failures == 0


@pytest.mark.asyncio
async def test_cycle_chat_hang_triggers_failure(httpx_mock):
    """Simulate the observed stuck state: tags OK but chat times out."""
    httpx_mock.add_response(url="http://localhost:11434/api/tags", json={"models": []})
    httpx_mock.add_response(
        url="http://localhost:11434/api/ps",
        json={"models": [{"model": "m:4b", "size": 3_000_000_000}]},
    )
    httpx_mock.add_exception(
        url="http://localhost:11434/api/chat",
        method="POST",
        exception=httpx.ReadTimeout("timed out"),
    )
    wd = OllamaWatchdog()
    async with httpx.AsyncClient() as c:
        await wd._one_cycle(c)
    assert wd._consecutive_failures == 1
    assert wd.stats["probes_failed"] == 1
