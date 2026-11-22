"""Detecting GPU memory the OS does not report as RAM (common/gpu_memory.py).

Issue #9: an AMD APU with 160 GB of 192 GB given to the GPU in the BIOS shows
Linux ~30 GB. The carve-out is readable from amdgpu's sysfs files; GTT must
never be added (it is already RAM), and NVIDIA's unified-memory parts answer
"[N/A]".
"""

from __future__ import annotations

import pytest

from fleet_manager.common import gpu_memory
from fleet_manager.common.gpu_memory import detect_gpu_memory

GB = 1024**3


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    monkeypatch.setattr(gpu_memory.platform, "system", lambda: "Linux")


def _card(root, name, vram_total=None, vram_used=0, gtt_total=None, board_info=False, product=""):
    dev = root / name / "device"
    dev.mkdir(parents=True)
    if vram_total is not None:
        (dev / "mem_info_vram_total").write_text(f"{vram_total}\n")
        (dev / "mem_info_vram_used").write_text(f"{vram_used}\n")
    if gtt_total is not None:
        (dev / "mem_info_gtt_total").write_text(f"{gtt_total}\n")
        (dev / "mem_info_gtt_used").write_text("0\n")
    if board_info:
        (dev / "board_info").write_text("type : oam\n")
    if product:
        (dev / "product_name").write_text(product + "\n")


def _no_nvidia(root):
    return detect_gpu_memory(drm_root=root, nvidia_smi="")


class TestAmdgpu:
    def test_large_carve_out_apu_the_issue_9_machine(self, tmp_path):
        _card(tmp_path, "card1", vram_total=160 * GB, vram_used=12 * GB, gtt_total=15 * GB)
        (d,) = _no_nvidia(tmp_path)
        assert (d.total_gb, d.used_gb, d.kind, d.source) == (160.0, 12.0, "integrated", "amdgpu")

    def test_gtt_is_never_counted(self, tmp_path):
        """Small carve-out + large GTT: GTT is RAM, so only the carve-out counts."""
        _card(tmp_path, "card0", vram_total=GB // 2, gtt_total=110 * GB)
        (d,) = _no_nvidia(tmp_path)
        assert d.total_gb == 0.5

    def test_discrete_card(self, tmp_path):
        _card(
            tmp_path,
            "card0",
            vram_total=24 * GB,
            vram_used=2 * GB,
            board_info=True,
            product="Radeon RX 7900 XTX",
        )
        (d,) = _no_nvidia(tmp_path)
        assert (d.name, d.kind, d.total_gb) == ("Radeon RX 7900 XTX", "discrete", 24.0)

    def test_connectors_and_other_drivers_are_skipped(self, tmp_path):
        _card(tmp_path, "card0", vram_total=8 * GB)
        (tmp_path / "card0-HDMI-A-1").mkdir()
        _card(tmp_path, "card2")  # an Intel or virtual GPU: no mem_info files
        (tmp_path / "renderD128").mkdir()
        assert [d.total_gb for d in _no_nvidia(tmp_path)] == [8.0]

    def test_no_drm_directory(self, tmp_path):
        assert _no_nvidia(tmp_path / "missing") == []


def _fake_smi(tmp_path, body):
    script = tmp_path / "nvidia-smi"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


class TestNvidia:
    def test_two_cards(self, tmp_path):
        smi = _fake_smi(
            tmp_path,
            "echo 'NVIDIA GeForce RTX 4090, 24564, 1024'\necho 'NVIDIA RTX A6000, 49140, 0'",
        )
        devices = detect_gpu_memory(drm_root=tmp_path / "none", nvidia_smi=smi)
        assert [(d.name, d.total_gb, d.used_gb) for d in devices] == [
            ("NVIDIA GeForce RTX 4090", 23.99, 1.0),
            ("NVIDIA RTX A6000", 47.99, 0.0),
        ]

    def test_unified_memory_part_reports_na_and_is_skipped(self, tmp_path):
        smi = _fake_smi(tmp_path, "echo 'NVIDIA GB10, [N/A], [N/A]'")
        assert detect_gpu_memory(drm_root=tmp_path / "none", nvidia_smi=smi) == []

    def test_failing_binary_is_no_gpu_not_an_error(self, tmp_path):
        smi = _fake_smi(tmp_path, "echo 'NVIDIA-SMI has failed' >&2; exit 9")
        assert detect_gpu_memory(drm_root=tmp_path / "none", nvidia_smi=smi) == []

    def test_missing_binary(self, tmp_path):
        assert (
            detect_gpu_memory(drm_root=tmp_path / "none", nvidia_smi=str(tmp_path / "nope")) == []
        )


def test_not_linux_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_memory.platform, "system", lambda: "Darwin")
    _card(tmp_path, "card0", vram_total=8 * GB)
    assert _no_nvidia(tmp_path) == []


# --- Metal's working set (Apple Silicon): a cap inside RAM, its own field ---


class _Fn:
    """A ctypes function stand-in that records restype/argtypes and calls."""

    def __init__(self, impl):
        self.impl = impl
        self.restype = None
        self.argtypes = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.impl(*args)


def _fake_cdll(device=0xD0, size=115_448_725_504):
    import ctypes

    selectors = {b"recommendedMaxWorkingSetSize": 0x51, b"release": 0x52}
    libs = {
        gpu_memory.METAL_FRAMEWORK: type("M", (), {})(),
        gpu_memory.OBJC_RUNTIME: type("O", (), {})(),
    }
    metal, objc = libs[gpu_memory.METAL_FRAMEWORK], libs[gpu_memory.OBJC_RUNTIME]
    metal.MTLCreateSystemDefaultDevice = _Fn(lambda: device)
    objc.sel_registerName = _Fn(lambda name: selectors[name])
    objc.objc_msgSend = _Fn(lambda dev, sel: size if sel == 0x51 else 0)

    def cdll(path):
        if path not in libs:
            raise OSError(path)
        return libs[path]

    cdll.metal, cdll.objc, cdll.ctypes = metal, objc, ctypes
    return cdll


class TestMetalWorkingSet:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        gpu_memory.metal_working_set_gb.cache_clear()
        monkeypatch.setattr(gpu_memory.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(gpu_memory.platform, "machine", lambda: "arm64")
        yield
        gpu_memory.metal_working_set_gb.cache_clear()

    def test_reads_the_recommended_working_set(self, monkeypatch):
        cdll = _fake_cdll()
        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", cdll)
        assert gpu_memory.metal_working_set_gb() == 107.52

    def test_sets_64_bit_return_types(self, monkeypatch):
        """ctypes defaults to a C int, which truncates the device pointer and size."""
        import ctypes

        cdll = _fake_cdll()
        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", cdll)
        gpu_memory.metal_working_set_gb()
        assert cdll.metal.MTLCreateSystemDefaultDevice.restype is ctypes.c_void_p
        assert cdll.objc.objc_msgSend.restype is ctypes.c_uint64
        assert cdll.objc.objc_msgSend.argtypes == [ctypes.c_void_p, ctypes.c_void_p]
        assert cdll.objc.sel_registerName.restype is ctypes.c_void_p

    def test_releases_the_device(self, monkeypatch):
        cdll = _fake_cdll()
        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", cdll)
        gpu_memory.metal_working_set_gb()
        assert (0xD0, 0x52) in cdll.objc.objc_msgSend.calls

    def test_read_once(self, monkeypatch):
        cdll = _fake_cdll()
        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", cdll)
        gpu_memory.metal_working_set_gb()
        gpu_memory.metal_working_set_gb()
        assert len(cdll.metal.MTLCreateSystemDefaultDevice.calls) == 1

    def test_no_device_reads_zero(self, monkeypatch):
        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", _fake_cdll(device=None))
        assert gpu_memory.metal_working_set_gb() == 0.0

    def test_no_metal_framework_reads_zero(self, monkeypatch):
        def missing(path):
            raise OSError(f"dlopen({path}) failed")

        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", missing)
        assert gpu_memory.metal_working_set_gb() == 0.0

    @pytest.mark.parametrize(
        ("system", "machine"),
        [("Linux", "x86_64"), ("Linux", "aarch64"), ("Windows", "AMD64"), ("Darwin", "x86_64")],
    )
    def test_elsewhere_reads_zero_without_loading_anything(self, monkeypatch, system, machine):
        """Intel Macs too: their default device's figure is VRAM or iGPU, not this cap."""
        monkeypatch.setattr(gpu_memory.platform, "system", lambda: system)
        monkeypatch.setattr(gpu_memory.platform, "machine", lambda: machine)

        def must_not_load(path):
            raise AssertionError(f"loaded {path}")

        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", must_not_load)
        assert gpu_memory.metal_working_set_gb() == 0.0

    def test_not_a_gpu_memory_backend(self, monkeypatch):
        """It is a cap inside RAM; detect_gpu_memory's devices are added to RAM."""
        monkeypatch.setattr(gpu_memory.ctypes, "CDLL", _fake_cdll())
        assert detect_gpu_memory() == []


@pytest.mark.skipif(
    __import__("platform").system() != "Darwin" or __import__("platform").machine() != "arm64",
    reason="real Metal read needs Apple Silicon",
)
def test_real_metal_working_set_is_inside_ram(monkeypatch):
    """On real hardware: positive and below physical memory (107.52 of 128 GiB on an M3 Ultra)."""
    import psutil

    # The module's autouse fixture pretends Linux; skipif already checked the real host.
    monkeypatch.setattr(gpu_memory.platform, "system", lambda: "Darwin")
    gpu_memory.metal_working_set_gb.cache_clear()
    try:
        value = gpu_memory.metal_working_set_gb()
    finally:
        gpu_memory.metal_working_set_gb.cache_clear()
    assert 0 < value < psutil.virtual_memory().total / GB
