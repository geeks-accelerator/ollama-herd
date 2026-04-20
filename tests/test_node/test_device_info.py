"""Tests for device_info probe — probe never raises, always includes arch."""

from __future__ import annotations

import platform as _platform
from unittest.mock import patch

from fleet_manager.node.device_info import probe_device_info


class TestProbe:
    def test_always_returns_dict_with_arch(self):
        info = probe_device_info()
        assert isinstance(info, dict)
        assert "arch" in info
        assert info["arch"] == _platform.machine()

    def test_never_raises_on_subprocess_failure(self):
        """Even if every subprocess call fails, probe returns arch at least."""
        with patch("fleet_manager.node.device_info._safe_run", return_value=""):
            info = probe_device_info()
            assert "arch" in info
            # Other keys may be absent — that's the point

    def test_macos_includes_os_key(self):
        """If we're on macOS in CI, probe should identify it."""
        if _platform.system() != "Darwin":
            import pytest
            pytest.skip("macOS-only test")
        info = probe_device_info()
        assert info.get("os") == "macOS"

    def test_hardware_summary_composed_from_parts(self):
        """Summary concatenates available fields."""
        import fleet_manager.node.device_info as di

        with (
            patch.object(di, "_probe_macos", return_value={
                "os": "macOS",
                "os_version": "15.2",
                "chip": "M3 Ultra",
                "total_memory_gb": 192,
            }),
            patch("sys.platform", "darwin"),
        ):
            info = probe_device_info()
            assert "hardware_summary" in info
            # Should contain the chip and memory
            assert "M3 Ultra" in info["hardware_summary"]
            assert "192GB" in info["hardware_summary"]
