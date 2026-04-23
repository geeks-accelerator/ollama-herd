"""Chip name → memory bandwidth lookup.

Used by Signal 5 (role affinity), Signal 3 (queue depth), and Signal 4
(wait time) to make routing device-aware.  Apple Silicon unified memory
bandwidth is the primary prompt-eval bottleneck on Mac fleets — a Mac
Studio M3 Ultra pushes ~819 GB/s while a MacBook Pro M3 (non-Max) does
~100 GB/s, an 8× gap that's invisible to memory-tier scoring.

Bandwidth numbers are from Apple's published specs and community
benchmarks.  When multiple variants exist (e.g. M3 Max 14-core vs
16-core GPU) we use the more common / higher-end value — routing is not
the right layer for precise per-SKU numbers and "within 20%" is more
than good enough for relative ranking.

Unknown chips return ``None`` so the scorer can fall back to memory-tier
heuristics without the operator noticing.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Apple Silicon — unified memory bandwidth (GB/s)
# ---------------------------------------------------------------------------

# Keys are lowercase, normalized chip strings (whitespace preserved; we match
# against the output of ``sysctl -n machdep.cpu.brand_string`` after lowering).
APPLE_SILICON_BANDWIDTH_GBPS: dict[str, float] = {
    # M3 generation
    "apple m3 ultra": 819.0,   # 32-core GPU variant; Mac Studio 2025
    "apple m3 max": 400.0,     # 40-core GPU; 14-core GPU is ~300
    "apple m3 pro": 150.0,     # 18-core GPU variant; 14-core is ~120
    "apple m3": 100.0,
    # M2 generation
    "apple m2 ultra": 800.0,
    "apple m2 max": 400.0,
    "apple m2 pro": 200.0,
    "apple m2": 100.0,
    # M1 generation
    "apple m1 ultra": 800.0,
    "apple m1 max": 400.0,
    "apple m1 pro": 200.0,
    "apple m1": 68.0,
    # M4 generation (as of 2026)
    "apple m4 max": 546.0,
    "apple m4 pro": 273.0,
    "apple m4": 120.0,
}

# ---------------------------------------------------------------------------
# Fallback heuristics for non-Apple fleets
# ---------------------------------------------------------------------------

# Rough discrete-GPU bandwidth by substring match.  Not meant to be exact —
# just "within the right order of magnitude" for scoring.  These apply only
# when the chip string mentions the GPU explicitly (e.g. brought through by
# a Linux collector that reads nvidia-smi).
DISCRETE_GPU_BANDWIDTH_GBPS: dict[str, float] = {
    "h100": 3350.0,
    "a100": 2039.0,
    "l40": 864.0,
    "rtx 5090": 1792.0,
    "rtx 4090": 1008.0,
    "rtx 4080": 716.0,
    "rtx 3090": 936.0,
    "rtx 3080": 760.0,
    "rtx 3070": 448.0,
    "rtx 2080": 616.0,
    "rtx 2070": 448.0,
}


def _normalize(chip: str) -> str:
    """Lowercase and collapse whitespace for table lookup."""
    return " ".join(chip.lower().split())


def resolve_bandwidth(chip: str) -> float | None:
    """Return estimated memory bandwidth in GB/s for the given chip string.

    Returns ``None`` when the chip is empty or unrecognised, so the caller
    can fall back to memory-tier heuristics without guessing.

    Examples::

        >>> resolve_bandwidth("Apple M3 Ultra")
        819.0
        >>> resolve_bandwidth("  apple   m3   ULTRA  ")
        819.0
        >>> resolve_bandwidth("Unknown Chip 9000") is None
        True
        >>> resolve_bandwidth("")
        is None
        True
    """
    if not chip:
        return None
    key = _normalize(chip)
    # Exact Apple Silicon match first — these dominate the target fleet.
    if key in APPLE_SILICON_BANDWIDTH_GBPS:
        return APPLE_SILICON_BANDWIDTH_GBPS[key]
    # Discrete GPU substring match (e.g. "Intel Xeon + NVIDIA RTX 4090")
    for gpu_substring, bw in DISCRETE_GPU_BANDWIDTH_GBPS.items():
        if gpu_substring in key:
            return bw
    return None


def bandwidth_tier(bw_gbps: float) -> str:
    """Human-readable tier label for dashboards / logs.

    Not used in scoring — scoring treats bandwidth as a continuous number.
    """
    if bw_gbps >= 700:
        return "extreme"   # M*/Ultra, datacenter GPUs
    if bw_gbps >= 350:
        return "high"      # M*/Max, RTX 3080+
    if bw_gbps >= 150:
        return "mid"       # M*/Pro
    if bw_gbps >= 80:
        return "entry"     # Base Apple Silicon, budget GPUs
    return "low"
