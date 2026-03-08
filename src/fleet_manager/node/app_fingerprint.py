"""Application fingerprinting — identifies workload types by resource signatures.

Rather than reading application names (privacy concern), the agent observes
resource consumption patterns. Heavy workloads have distinctive signatures:
- Video calls: sustained high CPU + network I/O + camera/mic
- Compilation: CPU spikes across many cores, high memory churn
- Video editing: sustained high CPU + GPU, large memory footprint
- IDE/coding: moderate CPU, stable memory, keyboard-heavy input
- Idle: low CPU, stable memory, no input activity

The fingerprinter classifies the current workload into categories that the
capacity learner uses to weight its decisions.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from enum import StrEnum

import psutil

logger = logging.getLogger(__name__)


class WorkloadType(StrEnum):
    """Classified workload intensity levels."""

    IDLE = "idle"
    LIGHT = "light"
    MODERATE = "moderate"
    HEAVY = "heavy"
    INTENSIVE = "intensive"


class ResourceSnapshot:
    """A point-in-time resource measurement."""

    __slots__ = (
        "timestamp",
        "cpu_pct",
        "memory_pct",
        "net_bytes_sent",
        "net_bytes_recv",
        "disk_io_read",
        "disk_io_write",
    )

    def __init__(
        self,
        timestamp: float,
        cpu_pct: float,
        memory_pct: float,
        net_bytes_sent: int,
        net_bytes_recv: int,
        disk_io_read: int,
        disk_io_write: int,
    ):
        self.timestamp = timestamp
        self.cpu_pct = cpu_pct
        self.memory_pct = memory_pct
        self.net_bytes_sent = net_bytes_sent
        self.net_bytes_recv = net_bytes_recv
        self.disk_io_read = disk_io_read
        self.disk_io_write = disk_io_write


class AppFingerprinter:
    """Classifies current workload based on resource consumption patterns.

    Maintains a sliding window of resource snapshots (default 2 minutes)
    and classifies the aggregate pattern into a WorkloadType.
    """

    def __init__(self, window_seconds: int = 120, sample_interval: int = 5):
        self._window_seconds = window_seconds
        self._sample_interval = sample_interval
        max_samples = window_seconds // sample_interval + 1
        self._snapshots: deque[ResourceSnapshot] = deque(maxlen=max_samples)
        self._last_net_io: tuple[int, int] | None = None
        self._last_disk_io: tuple[int, int] | None = None

    def collect_snapshot(self) -> ResourceSnapshot:
        """Collect a resource snapshot and add it to the sliding window."""
        now = time.time()
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        memory_pct = mem.percent

        # Network I/O (delta since last sample)
        net = psutil.net_io_counters()
        if self._last_net_io is not None:
            net_sent = net.bytes_sent - self._last_net_io[0]
            net_recv = net.bytes_recv - self._last_net_io[1]
        else:
            net_sent = 0
            net_recv = 0
        self._last_net_io = (net.bytes_sent, net.bytes_recv)

        # Disk I/O (delta since last sample)
        try:
            disk = psutil.disk_io_counters()
            if disk and self._last_disk_io is not None:
                disk_read = disk.read_bytes - self._last_disk_io[0]
                disk_write = disk.write_bytes - self._last_disk_io[1]
            else:
                disk_read = 0
                disk_write = 0
            if disk:
                self._last_disk_io = (disk.read_bytes, disk.write_bytes)
        except Exception:
            disk_read = 0
            disk_write = 0

        snap = ResourceSnapshot(
            timestamp=now,
            cpu_pct=cpu_pct,
            memory_pct=memory_pct,
            net_bytes_sent=max(0, net_sent),
            net_bytes_recv=max(0, net_recv),
            disk_io_read=max(0, disk_read),
            disk_io_write=max(0, disk_write),
        )
        self._snapshots.append(snap)
        return snap

    def classify(self) -> WorkloadType:
        """Classify current workload based on recent resource snapshots."""
        if len(self._snapshots) < 2:
            return WorkloadType.IDLE

        # Compute averages over the window
        avg_cpu = sum(s.cpu_pct for s in self._snapshots) / len(self._snapshots)
        avg_mem = sum(s.memory_pct for s in self._snapshots) / len(self._snapshots)

        # Network bandwidth (bytes/sec average)
        total_net = sum(s.net_bytes_sent + s.net_bytes_recv for s in self._snapshots)
        window_duration = self._snapshots[-1].timestamp - self._snapshots[0].timestamp
        avg_net_bps = total_net / window_duration if window_duration > 0 else 0

        # Classification logic based on resource signatures
        if avg_cpu > 85:
            return WorkloadType.INTENSIVE
        if avg_cpu > 60:
            # High CPU + high network = likely video call or streaming
            if avg_net_bps > 500_000:  # > 500KB/s sustained
                return WorkloadType.INTENSIVE
            return WorkloadType.HEAVY
        if avg_cpu > 35:
            return WorkloadType.MODERATE
        if avg_cpu > 10 or avg_mem > 70:
            return WorkloadType.LIGHT
        return WorkloadType.IDLE

    def get_cpu_trend(self, seconds: int = 300) -> float:
        """Get CPU trend over the last N seconds.

        Returns a value between -1.0 (falling) and +1.0 (rising).
        0.0 means stable.
        """
        if len(self._snapshots) < 4:
            return 0.0

        cutoff = time.time() - seconds
        recent = [s for s in self._snapshots if s.timestamp >= cutoff]
        if len(recent) < 4:
            recent = list(self._snapshots)

        if len(recent) < 4:
            return 0.0

        mid = len(recent) // 2
        first_half_avg = sum(s.cpu_pct for s in recent[:mid]) / mid
        second_half_avg = sum(s.cpu_pct for s in recent[mid:]) / (len(recent) - mid)

        # Normalize: difference of 50% CPU = trend of 1.0
        diff = second_half_avg - first_half_avg
        return max(-1.0, min(1.0, diff / 50.0))

    def get_summary(self) -> dict:
        """Get a summary of current resource patterns for logging/debugging."""
        if not self._snapshots:
            return {
                "workload": WorkloadType.IDLE.value,
                "avg_cpu": 0.0,
                "avg_memory": 0.0,
                "cpu_trend": 0.0,
                "samples": 0,
            }

        avg_cpu = sum(s.cpu_pct for s in self._snapshots) / len(self._snapshots)
        avg_mem = sum(s.memory_pct for s in self._snapshots) / len(self._snapshots)

        return {
            "workload": self.classify().value,
            "avg_cpu": round(avg_cpu, 1),
            "avg_memory": round(avg_mem, 1),
            "cpu_trend": round(self.get_cpu_trend(), 2),
            "samples": len(self._snapshots),
        }
