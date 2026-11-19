"""GPU memory a model can load into that the OS does not report as RAM (Linux).

The router decides whether a model fits on a node from the node's free memory,
and ``psutil`` reports only RAM.  Two kinds of GPU memory sit outside it:

- **AMD**, from the amdgpu driver's sysfs files: ``mem_info_vram_total`` /
  ``_used``.  On a discrete card that is its VRAM; on an APU it is the BIOS
  "UMA frame buffer" carve-out, which firmware withholds from the OS, so it is
  absent from ``/proc/meminfo`` MemTotal.  An AMD Ryzen AI Max+ PRO 495 box
  (GMKtec EVO-X5 Pro, 192 GB) can give 160 GB to it and leave Linux ~30 GB,
  which is how issue #9 was refused models Ollama ran fine.
- **NVIDIA**, from ``nvidia-smi``: each card's ``memory.total`` / ``memory.used``.

**GTT is never counted.**  amdgpu's GTT pool (``mem_info_gtt_*``) is ordinary
system RAM the GPU may map, already inside MemTotal and competing with
MemAvailable; adding it would count the same memory twice.  The same goes for
unified-memory machines: Apple Silicon has neither source, and NVIDIA's GB10
(DGX Spark) answers ``memory.total`` with "[N/A]", which is skipped.

Linux only.  Reading sysfs needs no extra tools and works inside a container,
where ``/sys`` is visible; ``nvidia-smi`` exists in a container only under
``--gpus``.  Every failure reads as "no GPU memory found", never as 0 GB of a
GPU that exists, and nothing here raises.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import re
import shutil
import subprocess
from pathlib import Path

from fleet_manager.models.node import GpuDevice

logger = logging.getLogger(__name__)

DRM_ROOT = Path("/sys/class/drm")
_CARD = re.compile(r"^card\d+$")  # card0, not connectors like card0-HDMI-A-1
_GB = 1024**3


def detect_gpu_memory(drm_root: Path = DRM_ROOT, nvidia_smi: str | None = None) -> list[GpuDevice]:
    """Every GPU whose memory the OS does not count, with total and used GB."""
    if platform.system() != "Linux":
        return []
    return _amdgpu_devices(drm_root) + _nvidia_devices(nvidia_smi or shutil.which("nvidia-smi"))


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _amdgpu_devices(drm_root: Path) -> list[GpuDevice]:
    devices = []
    try:
        cards = sorted(p for p in drm_root.iterdir() if _CARD.match(p.name))
    except OSError:
        return []
    for card in cards:
        dev = card / "device"
        total = _read_int(dev / "mem_info_vram_total")
        if not total:
            continue  # not an amdgpu device, or nothing carved out
        used = _read_int(dev / "mem_info_vram_used") or 0
        # amdgpu hides board_info on APUs (amdgpu_device.c); its absence is a
        # hint, not proof, so it only labels the device and decides nothing.
        kind = "discrete" if (dev / "board_info").exists() else "integrated"
        name = ""
        with contextlib.suppress(OSError):
            name = (dev / "product_name").read_text().strip()
        devices.append(
            GpuDevice(
                name=name or f"AMD GPU ({card.name})",
                kind=kind,
                total_gb=round(total / _GB, 2),
                used_gb=round(min(used, total) / _GB, 2),
                source="amdgpu",
            )
        )
    return devices


def _nvidia_devices(nvidia_smi: str | None) -> list[GpuDevice]:
    if not nvidia_smi:
        return []
    try:
        out = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("nvidia-smi gave no GPU memory: %s", exc)
        return []
    devices = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        name, total, used = parts
        try:
            total_mib, used_mib = float(total), float(used)
        except ValueError:
            continue  # "[N/A]" on unified-memory parts such as GB10: RAM already counts it
        if total_mib <= 0:
            continue
        devices.append(
            GpuDevice(
                name=name,
                kind="discrete",
                total_gb=round(total_mib / 1024, 2),
                used_gb=round(min(used_mib, total_mib) / 1024, 2),
                source="nvidia-smi",
            )
        )
    return devices
