"""Tests for server/hardware_lookup.py — chip→bandwidth table."""

from __future__ import annotations

from fleet_manager.server.hardware_lookup import (
    APPLE_SILICON_BANDWIDTH_GBPS,
    bandwidth_tier,
    resolve_bandwidth,
)


class TestResolveBandwidth:
    def test_apple_m3_ultra(self):
        assert resolve_bandwidth("Apple M3 Ultra") == 819.0

    def test_apple_m3_max(self):
        assert resolve_bandwidth("Apple M3 Max") == 400.0

    def test_apple_m3_pro(self):
        assert resolve_bandwidth("Apple M3 Pro") == 150.0

    def test_apple_m3_base(self):
        assert resolve_bandwidth("Apple M3") == 100.0

    def test_apple_m1(self):
        assert resolve_bandwidth("Apple M1") == 68.0

    def test_apple_m4_max(self):
        assert resolve_bandwidth("Apple M4 Max") == 546.0

    def test_whitespace_normalized(self):
        """Extra / inconsistent whitespace doesn't break lookup."""
        assert resolve_bandwidth("  apple   m3   ultra  ") == 819.0
        assert resolve_bandwidth("apple m3 ultra") == 819.0
        assert resolve_bandwidth("APPLE M3 ULTRA") == 819.0

    def test_unknown_chip_returns_none(self):
        assert resolve_bandwidth("Unknown Chip 9000") is None
        assert resolve_bandwidth("Intel Xeon E5-2670") is None

    def test_empty_string_returns_none(self):
        assert resolve_bandwidth("") is None

    def test_discrete_gpu_substring_match(self):
        """Linux-style 'Intel + NVIDIA RTX 4090' strings pick up the GPU."""
        assert resolve_bandwidth("Intel Xeon + NVIDIA RTX 4090") == 1008.0
        assert resolve_bandwidth("AMD Ryzen + RTX 3090") == 936.0
        assert resolve_bandwidth("random text with h100 somewhere") == 3350.0

    def test_apple_match_wins_over_gpu(self):
        """Apple Silicon exact match takes priority — no 'rtx' in the string."""
        assert resolve_bandwidth("Apple M3 Max") == 400.0  # not some GPU fallback

    def test_all_apple_chips_in_sensible_range(self):
        """Regression guard: all table entries should be 50-2000 GB/s."""
        for chip, bw in APPLE_SILICON_BANDWIDTH_GBPS.items():
            assert 50 <= bw <= 2000, f"{chip}: {bw} is out of sensible range"


class TestBandwidthTier:
    def test_extreme_tier(self):
        assert bandwidth_tier(819) == "extreme"
        assert bandwidth_tier(2039) == "extreme"  # A100

    def test_high_tier(self):
        assert bandwidth_tier(400) == "high"
        assert bandwidth_tier(500) == "high"

    def test_mid_tier(self):
        assert bandwidth_tier(200) == "mid"
        assert bandwidth_tier(150) == "mid"

    def test_entry_tier(self):
        assert bandwidth_tier(100) == "entry"
        assert bandwidth_tier(80) == "entry"

    def test_low_tier(self):
        assert bandwidth_tier(68) == "low"
        assert bandwidth_tier(50) == "low"

    def test_boundary_values(self):
        """Just under / at each tier boundary."""
        assert bandwidth_tier(700) == "extreme"  # at boundary
        assert bandwidth_tier(699) == "high"
        assert bandwidth_tier(350) == "high"
        assert bandwidth_tier(349) == "mid"
