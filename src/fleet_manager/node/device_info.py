"""Device info probe — gathers hardware details for platform registration.

Returns a dict safe to include in POST /api/nodes/register's
`device_info` field.  All keys are optional; extras are allowed by the
platform.  Only hardware facts that help the platform classify nodes
for routing — never anything user-identifying.

Platform-specific probes:
  - macOS: system_profiler + sw_vers + sysctl
  - Linux: /proc/cpuinfo + /proc/meminfo + lspci / nvidia-smi if present
  - Windows: Get-ComputerInfo via PowerShell

Each probe is best-effort — failures produce a smaller dict, never
raise.  The platform's dashboard renders only the fields we report.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import subprocess
import sys

import psutil

logger = logging.getLogger(__name__)


def _safe_run(cmd: list[str], timeout: float = 5.0) -> str:
    """Run a subprocess and return stdout.  Returns '' on any failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout if result.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _probe_macos() -> dict:
    """Hardware probe for macOS."""
    info: dict = {"os": "macOS"}

    # sw_vers — OS version
    sw = _safe_run(["sw_vers", "-productVersion"])
    if sw.strip():
        info["os_version"] = sw.strip()

    # sysctl for chip name (faster than system_profiler)
    chip = _safe_run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if chip.strip():
        info["chip"] = chip.strip()

    # system_profiler for richer info — parsed from human-readable text
    # (JSON mode is available but the text parser is simpler and
    # resistant to version drift)
    profile = _safe_run(
        ["system_profiler", "SPHardwareDataType"], timeout=8.0
    )
    if profile:
        for line in profile.splitlines():
            s = line.strip()
            if s.startswith("Chip:") and "chip" not in info:
                info["chip"] = s.split(":", 1)[1].strip()
            elif s.startswith("Model Name:"):
                info["model_name"] = s.split(":", 1)[1].strip()
            elif s.startswith("Total Number of Cores:"):
                cores_str = s.split(":", 1)[1].strip()
                # e.g. "32 (24 performance and 8 efficiency)"
                with contextlib.suppress(ValueError, IndexError):
                    info["cpu_cores"] = int(cores_str.split()[0])
            elif s.startswith("Memory:"):
                mem_str = s.split(":", 1)[1].strip()
                # e.g. "512 GB"
                try:
                    parts = mem_str.split()
                    if len(parts) >= 2 and parts[1].upper() == "GB":
                        gb = int(parts[0])
                        info["total_memory_gb"] = gb
                        # On Apple Silicon, unified memory means VRAM == RAM
                        if platform.machine() == "arm64":
                            info["total_vram_gb"] = gb
                except (ValueError, IndexError):
                    pass

    # GPU name for Apple Silicon is the chip (unified memory architecture)
    if platform.machine() == "arm64" and "chip" in info:
        info["gpu"] = f"{info['chip']} GPU"

    return info


def _probe_linux() -> dict:
    """Hardware probe for Linux."""
    info: dict = {"os": "Linux"}

    # OS distribution + version
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["os_version"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass

    # CPU model from /proc/cpuinfo
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["chip"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass

    # Core count via psutil (matches what kernel sees)
    with contextlib.suppress(Exception):
        info["cpu_cores"] = psutil.cpu_count(logical=False) or 0

    # Memory
    try:
        gb = round(psutil.virtual_memory().total / (1024**3))
        info["total_memory_gb"] = gb
    except Exception:
        pass

    # NVIDIA GPU via nvidia-smi
    smi = _safe_run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    )
    if smi.strip():
        first = smi.strip().splitlines()[0]
        parts = [p.strip() for p in first.split(",")]
        if len(parts) >= 1:
            info["gpu"] = parts[0]
        if len(parts) >= 2:
            with contextlib.suppress(ValueError):
                info["total_vram_gb"] = round(int(parts[1]) / 1024)

    return info


def _probe_windows() -> dict:
    """Hardware probe for Windows."""
    info: dict = {"os": "Windows"}

    info["os_version"] = platform.release()

    # CPU model via wmic (or PowerShell fallback)
    cpu = _safe_run(["wmic", "cpu", "get", "name", "/value"])
    if "Name=" in cpu:
        for line in cpu.splitlines():
            if line.startswith("Name="):
                info["chip"] = line.split("=", 1)[1].strip()
                break

    try:
        info["cpu_cores"] = psutil.cpu_count(logical=False) or 0
        info["total_memory_gb"] = round(psutil.virtual_memory().total / (1024**3))
    except Exception:
        pass

    # NVIDIA on Windows
    smi = _safe_run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    )
    if smi.strip():
        first = smi.strip().splitlines()[0]
        parts = [p.strip() for p in first.split(",")]
        if len(parts) >= 1:
            info["gpu"] = parts[0]
        if len(parts) >= 2:
            with contextlib.suppress(ValueError):
                info["total_vram_gb"] = round(int(parts[1]) / 1024)

    return info


def probe_device_info() -> dict:
    """Return a device_info dict for platform registration.

    Always includes `arch`.  Other keys are best-effort — absent on
    failure.  Never raises.
    """
    info: dict = {"arch": platform.machine()}

    try:
        if sys.platform == "darwin":
            info.update(_probe_macos())
        elif sys.platform.startswith("linux"):
            info.update(_probe_linux())
        elif sys.platform == "win32":
            info.update(_probe_windows())
        else:
            info["os"] = sys.platform
    except Exception as exc:  # paranoid: never break registration
        logger.debug(f"device_info probe failed: {exc}")

    # Compose a human-readable summary if we have enough parts
    model = info.get("model_name", "")
    chip = info.get("chip", "")
    mem = info.get("total_memory_gb")
    os_ver = info.get("os_version", "")
    parts = [p for p in (model or chip, f"{mem}GB" if mem else "", os_ver) if p]
    if parts:
        info["hardware_summary"] = ", ".join(parts)

    return info
