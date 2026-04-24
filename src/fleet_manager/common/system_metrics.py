"""System metrics collection (cross-platform: macOS, Linux, Windows)."""

from __future__ import annotations

import logging
import os
import platform
import subprocess

import psutil

from fleet_manager.models.node import (
    CpuMetrics,
    DiskMetrics,
    MemoryMetrics,
    MemoryPressure,
    ThermalMetrics,
    ThermalState,
)

# Linux temperature threshold for THERMAL WARNING.  Modern Intel / AMD CPUs
# typically throttle somewhere between 90-105°C depending on chip. Setting the
# warning at 85°C gives a small margin so the dashboard flags "running hot"
# before the kernel actually cuts frequency. Tunable if operators on
# always-hot datacenter nodes want a different bar.
_LINUX_THERMAL_WARNING_C = 85.0

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


def get_disk_metrics() -> DiskMetrics:
    root = os.environ.get("SYSTEMDRIVE", "C:\\") if platform.system() == "Windows" else "/"
    usage = psutil.disk_usage(root)
    return DiskMetrics(
        total_gb=round(usage.total / (1024**3), 2),
        used_gb=round(usage.used / (1024**3), 2),
        available_gb=round(usage.free / (1024**3), 2),
    )


def _get_memory_pressure() -> MemoryPressure:
    system = platform.system()
    if system == "Darwin":
        return _get_memory_pressure_darwin()
    if system == "Linux":
        return _get_memory_pressure_linux()
    # Windows: psutil.virtual_memory().percent is used elsewhere for scoring
    return MemoryPressure.NORMAL


def _get_memory_pressure_darwin() -> MemoryPressure:
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


def _get_memory_pressure_linux() -> MemoryPressure:
    """Estimate memory pressure from /proc/meminfo on Linux."""
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        mem_total = meminfo.get("MemTotal", 0)
        mem_available = meminfo.get("MemAvailable", 0)
        if mem_total == 0:
            return MemoryPressure.NORMAL
        used_pct = (mem_total - mem_available) / mem_total * 100
        if used_pct >= 95:
            return MemoryPressure.CRITICAL
        if used_pct >= 85:
            return MemoryPressure.WARN
        return MemoryPressure.NORMAL
    except Exception as e:
        logger.warning(f"Could not read /proc/meminfo (defaulting to NORMAL): {e}")
        return MemoryPressure.NORMAL


def get_thermal_metrics() -> ThermalMetrics:
    """Return the node's thermal state in a platform-aware way.

    Coverage is genuinely uneven across platforms:
      - **Linux**: ``psutil.sensors_temperatures()`` returns per-sensor temps.
        We scan for CPU/coretemp/k10temp/zenpower and take the peak reading.
        This is the only platform with a first-class thermal signal.
      - **macOS**: ``sensors_temperatures`` isn't implemented. Apple Silicon
        doesn't expose ``machdep.xcpm`` (Intel-only), and ``powermetrics``
        requires sudo — a hard no for a node agent. We return UNKNOWN and
        let the dashboard fall back to a sustained-CPU proxy.
      - **Windows**: ``sensors_temperatures`` is driver-dependent. Try it;
        return UNKNOWN if empty.

    This is an honest detection, not a best-effort guess — when we can't
    tell, we say so, and the caller decides how to surface that.
    """
    system = platform.system()
    if system == "Linux":
        return _get_thermal_linux()
    if system == "Windows":
        return _get_thermal_windows()
    # Darwin (macOS) + everything else → no reliable thermal source
    return ThermalMetrics(state=ThermalState.UNKNOWN)


def _get_thermal_linux() -> ThermalMetrics:
    """psutil.sensors_temperatures() on Linux.  Returns UNKNOWN on failure."""
    sensors_fn = getattr(psutil, "sensors_temperatures", None)
    if sensors_fn is None:
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    try:
        temps = sensors_fn()
    except Exception as e:
        logger.debug(f"psutil.sensors_temperatures() raised: {e}")
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    if not temps:
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    # Scan the usual CPU-temp drivers in order of specificity.
    preferred_drivers = ("coretemp", "k10temp", "zenpower", "cpu_thermal")
    peak_c: float | None = None
    peak_source: str = ""
    for driver in preferred_drivers:
        readings = temps.get(driver) or []
        for r in readings:
            # Prefer the "Package id" or "Tctl" readings when labeled.
            current = getattr(r, "current", None)
            if current is None:
                continue
            if peak_c is None or current > peak_c:
                peak_c = current
                peak_source = f"psutil:{driver}"
    # If we didn't find a preferred driver, try any reading as a fallback.
    if peak_c is None:
        for driver, readings in temps.items():
            for r in readings:
                current = getattr(r, "current", None)
                if current is None:
                    continue
                if peak_c is None or current > peak_c:
                    peak_c = current
                    peak_source = f"psutil:{driver}"
    if peak_c is None:
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    state = (
        ThermalState.WARNING if peak_c >= _LINUX_THERMAL_WARNING_C
        else ThermalState.NOMINAL
    )
    return ThermalMetrics(state=state, temperature_c=round(peak_c, 1), source=peak_source)


def _get_thermal_windows() -> ThermalMetrics:
    """psutil.sensors_temperatures() on Windows — driver-dependent."""
    sensors_fn = getattr(psutil, "sensors_temperatures", None)
    if sensors_fn is None:
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    try:
        temps = sensors_fn()
    except Exception:
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    if not temps:
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    peak_c: float | None = None
    peak_source = ""
    for driver, readings in temps.items():
        for r in readings:
            current = getattr(r, "current", None)
            if current is None:
                continue
            if peak_c is None or current > peak_c:
                peak_c = current
                peak_source = f"psutil:{driver}"
    if peak_c is None:
        return ThermalMetrics(state=ThermalState.UNKNOWN)
    state = (
        ThermalState.WARNING if peak_c >= _LINUX_THERMAL_WARNING_C
        else ThermalState.NOMINAL
    )
    return ThermalMetrics(state=state, temperature_c=round(peak_c, 1), source=peak_source)


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
