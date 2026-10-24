"""Small statistics helpers shared by the router and the node.

One definition per quantity, so two reports of "p95" cannot disagree because
two copies drifted.
"""

from __future__ import annotations


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile of raw samples; ``None`` for no samples.

    ``None`` rather than ``0.0`` because "no data" and "zero latency" are
    different facts, and the community telemetry payload reports them
    differently (``null`` against ``0``).  Its arithmetic is the one the
    published payload has always used -- ``test_community_payload_golden`` pins
    it -- so it is canonical; the benchmark report, which rounds to 0.1 ms,
    adopted it rather than the other way round.
    """
    if not values:
        return None
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac
