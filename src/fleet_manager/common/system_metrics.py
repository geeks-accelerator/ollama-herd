"""System metrics collection for Apple Silicon devices."""

from __future__ import annotations

import logging
import platform
import subprocess

import psutil

from fleet_manager.models.node import CpuMetrics, MemoryMetrics, MemoryPressure

logger = logging.getLogger(__name__)


def get_cpu_metrics() -> CpuMetrics:
    return CpuMetrics(
        cores_physical=psutil.cpu_count(logical=False) or 1,
        utilization_pct=psutil.cpu_percent(interval=None),
    )


def get_memory_metrics() -> MemoryMetrics:
    vm = psutil.virtual_memory()
    pressure = _get_memory_pressure()
    wired = getattr(vm, "wired", 0)
    return MemoryMetrics(
        total_gb=round(vm.total / (1024**3), 2),
        used_gb=round(vm.used / (1024**3), 2),
        available_gb=round(vm.available / (1024**3), 2),
        pressure=pressure,
        wired_gb=round(wired / (1024**3), 2),
        compressed_gb=0.0,
    )


def _get_memory_pressure() -> MemoryPressure:
    if platform.system() != "Darwin":
        return MemoryPressure.NORMAL
    try:
        result = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.lower()
        if "critical" in output:
            return MemoryPressure.CRITICAL
        if "warn" in output:
            return MemoryPressure.WARN
        return MemoryPressure.NORMAL
    except Exception as e:
        logger.warning(f"Could not read memory pressure (defaulting to NORMAL): {e}")
        return MemoryPressure.NORMAL


def get_local_ip() -> str:
    """Get the LAN IP address of this machine."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logger.warning(f"Could not determine LAN IP (defaulting to 127.0.0.1): {e}")
        return "127.0.0.1"
