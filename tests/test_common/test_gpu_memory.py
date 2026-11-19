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
