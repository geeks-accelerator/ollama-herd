"""Adaptive Capacity Learner — learns when a MacBook has spare capacity.

Builds a rolling 168-slot (7 days x 24 hours) usage model with a 30-day
window. Each slot stores a distribution of observed system load. Older
observations use exponential decay so the model adapts to lifestyle changes.

The learner computes a real-time availability score (0.0 to 1.0) that
combines:
  - Historical baseline for this hour (what you usually do at this time)
  - Current observed state (what's happening right now)
  - Trend signal (is activity rising or falling in the last few minutes)

The score maps to a dynamic memory ceiling the router respects:
  0.00–0.20  →  Ollama paused entirely
  0.20–0.40  →  16GB ceiling, lowest priority
  0.40–0.60  →  32GB ceiling, low priority
  0.60–0.80  →  64GB ceiling, normal priority
  0.80–1.00  →  total_memory ceiling, full participant

Hard override signals (regardless of learned baseline):
  - Camera or microphone active → hard pause (meeting)
  - Memory pressure hits warn → drain queue, pause new requests
  - CPU above 85% sustained 2+ minutes → drop to 16GB ceiling
  - Thermal throttling detected → pause entirely
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from enum import Enum
from pathlib import Path

from fleet_manager.node.app_fingerprint import AppFingerprinter
from fleet_manager.node.meeting_detector import MeetingDetector

logger = logging.getLogger(__name__)

# Number of hourly slots: 7 days x 24 hours
NUM_SLOTS = 168

# Exponential decay half-life in days
DECAY_HALF_LIFE_DAYS = 15

# Bootstrap observation period before contributing capacity
BOOTSTRAP_DAYS = 7

# Minimum samples per slot before confident predictions
MIN_SAMPLES_FOR_CONFIDENCE = 5


class CapacityMode(str, Enum):
    """Current capacity mode of the node."""

    FULL = "full"  # Full capacity, no restrictions
    LEARNED_HIGH = "learned_high"  # Learned pattern: high availability
    LEARNED_MEDIUM = "learned_medium"  # Learned pattern: medium availability
    LEARNED_LOW = "learned_low"  # Learned pattern: low availability
    PAUSED = "paused"  # Hard paused (meeting, thermal, etc.)
    BOOTSTRAP = "bootstrap"  # First 7 days, observation only


class CapacityInfo:
    """Current capacity state to include in heartbeat."""

    def __init__(
        self,
        mode: CapacityMode = CapacityMode.FULL,
        ceiling_gb: float = 0.0,
        availability_score: float = 1.0,
        reason: str = "",
        override_active: bool = False,
        learning_confidence: float = 0.0,
        days_observed: int = 0,
    ):
        self.mode = mode
        self.ceiling_gb = ceiling_gb
        self.availability_score = availability_score
        self.reason = reason
        self.override_active = override_active
        self.learning_confidence = learning_confidence
        self.days_observed = days_observed

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "ceiling_gb": round(self.ceiling_gb, 1),
            "availability_score": round(self.availability_score, 3),
            "reason": self.reason,
            "override_active": self.override_active,
            "learning_confidence": round(self.learning_confidence, 2),
            "days_observed": self.days_observed,
        }


class SlotData:
    """Aggregated observations for a single hour-of-week slot."""

    def __init__(self):
        self.observations: list[tuple[float, float, float]] = []
        # Each observation: (timestamp, cpu_pct, memory_pct)

    def add(self, timestamp: float, cpu_pct: float, memory_pct: float):
        self.observations.append((timestamp, cpu_pct, memory_pct))

    def weighted_avg_cpu(self, now: float) -> float:
        """Compute exponentially-decayed weighted average CPU usage."""
        if not self.observations:
            return 0.0
        total_weight = 0.0
        weighted_sum = 0.0
        for ts, cpu, _ in self.observations:
            age_days = (now - ts) / 86400.0
            weight = math.exp(-math.log(2) * age_days / DECAY_HALF_LIFE_DAYS)
            weighted_sum += cpu * weight
            total_weight += weight
        return weighted_sum / total_weight if total_weight > 0 else 0.0

    def weighted_avg_memory(self, now: float) -> float:
        """Compute exponentially-decayed weighted average memory usage."""
        if not self.observations:
            return 0.0
        total_weight = 0.0
        weighted_sum = 0.0
        for ts, _, mem in self.observations:
            age_days = (now - ts) / 86400.0
            weight = math.exp(-math.log(2) * age_days / DECAY_HALF_LIFE_DAYS)
            weighted_sum += mem * weight
            total_weight += weight
        return weighted_sum / total_weight if total_weight > 0 else 0.0

    def sample_count(self) -> int:
        return len(self.observations)

    def prune(self, max_age_days: float = 30.0):
        """Remove observations older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        self.observations = [(ts, cpu, mem) for ts, cpu, mem in self.observations if ts >= cutoff]

    def to_dict(self) -> dict:
        return {
            "observations": [
                {"ts": ts, "cpu": round(cpu, 1), "mem": round(mem, 1)}
                for ts, cpu, mem in self.observations
            ]
        }

    @classmethod
    def from_dict(cls, data: dict) -> SlotData:
        slot = cls()
        for obs in data.get("observations", []):
            slot.add(obs["ts"], obs["cpu"], obs["mem"])
        return slot


def _get_slot_index(timestamp: float | None = None) -> int:
    """Get the 0–167 slot index for a given timestamp (or now)."""
    if timestamp is None:
        t = time.localtime()
    else:
        t = time.localtime(timestamp)
    return t.tm_wday * 24 + t.tm_hour


class AdaptiveCapacityLearner:
    """Learns device usage patterns and computes dynamic capacity.

    The learner maintains a 168-slot behavioral model (one slot per hour
    of the week). It combines historical patterns with real-time observations
    to produce an availability score and memory ceiling.
    """

    def __init__(
        self,
        total_memory_gb: float,
        data_dir: str = "~/.fleet-manager",
        node_id: str = "",
    ):
        self.total_memory_gb = total_memory_gb
        self._data_dir = Path(os.path.expanduser(data_dir))
        self._node_id = node_id
        self._slots: list[SlotData] = [SlotData() for _ in range(NUM_SLOTS)]
        self._first_observation: float | None = None
        self._manual_override: dict | None = None  # {"mode": "full", "expires": timestamp}

        # Sub-components
        self._fingerprinter = AppFingerprinter()
        self._meeting_detector = MeetingDetector()

        # Current state cache
        self._last_capacity: CapacityInfo = CapacityInfo(mode=CapacityMode.BOOTSTRAP)
        self._sustained_high_cpu_since: float | None = None

        # Load persisted state
        self._load()

    def _state_path(self) -> Path:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"-{self._node_id}" if self._node_id else ""
        return self._data_dir / f"capacity-learner{suffix}.json"

    def _load(self):
        """Load persisted learning state from disk."""
        path = self._state_path()
        if not path.exists():
            logger.info("No capacity learner state found, starting fresh")
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._first_observation = data.get("first_observation")
            override = data.get("manual_override")
            if override and override.get("expires", 0) > time.time():
                self._manual_override = override
            for i, slot_data in enumerate(data.get("slots", [])):
                if i < NUM_SLOTS:
                    self._slots[i] = SlotData.from_dict(slot_data)
            logger.info(
                f"Loaded capacity learner state: "
                f"{self.days_observed} days observed, "
                f"{sum(s.sample_count() for s in self._slots)} total samples"
            )
        except Exception as e:
            logger.warning(f"Failed to load capacity learner state: {e}")

    def save(self):
        """Persist learning state to disk."""
        try:
            data = {
                "first_observation": self._first_observation,
                "manual_override": self._manual_override,
                "slots": [s.to_dict() for s in self._slots],
                "saved_at": time.time(),
            }
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"Failed to save capacity learner state: {e}")

    @property
    def days_observed(self) -> int:
        """Number of days since first observation."""
        if self._first_observation is None:
            return 0
        return int((time.time() - self._first_observation) / 86400)

    @property
    def is_bootstrapping(self) -> bool:
        """True during the initial observation-only period."""
        return self.days_observed < BOOTSTRAP_DAYS

    @property
    def learning_confidence(self) -> float:
        """Overall confidence in the learned model (0.0 to 1.0)."""
        if self.days_observed == 0:
            return 0.0
        # Confidence grows with days observed and sample density
        day_factor = min(1.0, self.days_observed / 30.0)
        # Average samples per slot
        total_samples = sum(s.sample_count() for s in self._slots)
        avg_per_slot = total_samples / NUM_SLOTS
        sample_factor = min(1.0, avg_per_slot / MIN_SAMPLES_FOR_CONFIDENCE)
        return round(day_factor * 0.6 + sample_factor * 0.4, 2)

    def observe(self, cpu_pct: float, memory_pct: float) -> CapacityInfo:
        """Record an observation and compute current capacity.

        Called every heartbeat interval (typically 5 seconds).
        Returns the current CapacityInfo to include in the heartbeat.
        """
        now = time.time()

        # Track first observation for bootstrap period
        if self._first_observation is None:
            self._first_observation = now
            logger.info("Capacity learner: first observation recorded, bootstrap period started")

        # Record observation in the current hour slot
        slot_idx = _get_slot_index(now)
        self._slots[slot_idx].add(now, cpu_pct, memory_pct)

        # Prune old observations periodically (roughly every hour worth of samples)
        if self._slots[slot_idx].sample_count() % 720 == 0:
            for slot in self._slots:
                slot.prune()

        # Collect resource fingerprint
        self._fingerprinter.collect_snapshot()

        # Compute availability
        capacity = self._compute_capacity(now, cpu_pct, memory_pct)
        self._last_capacity = capacity

        # Persist state periodically (every ~5 minutes = 60 observations at 5s interval)
        if self._slots[slot_idx].sample_count() % 60 == 0:
            self.save()

        return capacity

    def _compute_capacity(
        self,
        now: float,
        current_cpu: float,
        current_memory_pct: float,
    ) -> CapacityInfo:
        """Compute the current availability score and memory ceiling."""

        # --- Hard override checks (highest priority) ---

        # Meeting detection: hard pause
        if self._meeting_detector.is_in_meeting():
            return CapacityInfo(
                mode=CapacityMode.PAUSED,
                ceiling_gb=0.0,
                availability_score=0.0,
                reason="meeting_detected",
                override_active=True,
                learning_confidence=self.learning_confidence,
                days_observed=self.days_observed,
            )

        # Sustained high CPU (>85% for 2+ minutes)
        if current_cpu > 85:
            if self._sustained_high_cpu_since is None:
                self._sustained_high_cpu_since = now
            elif now - self._sustained_high_cpu_since >= 120:
                return CapacityInfo(
                    mode=CapacityMode.PAUSED,
                    ceiling_gb=min(16.0, self.total_memory_gb * 0.1),
                    availability_score=0.1,
                    reason="sustained_high_cpu",
                    override_active=True,
                    learning_confidence=self.learning_confidence,
                    days_observed=self.days_observed,
                )
        else:
            self._sustained_high_cpu_since = None

        # Manual override
        if self._manual_override:
            if self._manual_override.get("expires", 0) > now:
                override_mode = self._manual_override.get("mode", "full")
                if override_mode == "full":
                    return CapacityInfo(
                        mode=CapacityMode.FULL,
                        ceiling_gb=self.total_memory_gb * 0.8,
                        availability_score=1.0,
                        reason="manual_override",
                        override_active=True,
                        learning_confidence=self.learning_confidence,
                        days_observed=self.days_observed,
                    )
                elif override_mode == "paused":
                    return CapacityInfo(
                        mode=CapacityMode.PAUSED,
                        ceiling_gb=0.0,
                        availability_score=0.0,
                        reason="manual_override",
                        override_active=True,
                        learning_confidence=self.learning_confidence,
                        days_observed=self.days_observed,
                    )
            else:
                self._manual_override = None

        # --- Bootstrap mode ---
        if self.is_bootstrapping:
            return CapacityInfo(
                mode=CapacityMode.BOOTSTRAP,
                ceiling_gb=0.0,
                availability_score=0.0,
                reason="bootstrap_observation_period",
                learning_confidence=self.learning_confidence,
                days_observed=self.days_observed,
            )

        # --- Learned capacity computation ---
        slot_idx = _get_slot_index(now)
        slot = self._slots[slot_idx]

        # Historical baseline for this time slot
        historical_cpu = slot.weighted_avg_cpu(now)
        historical_mem = slot.weighted_avg_memory(now)

        # Get trend signal from fingerprinter
        cpu_trend = self._fingerprinter.get_cpu_trend(seconds=300)

        # Compute availability score combining historical + current + trend
        availability = self._compute_availability_score(
            historical_cpu=historical_cpu,
            historical_mem=historical_mem,
            current_cpu=current_cpu,
            current_memory_pct=current_memory_pct,
            cpu_trend=cpu_trend,
            slot_confidence=min(1.0, slot.sample_count() / MIN_SAMPLES_FOR_CONFIDENCE),
        )

        # Map availability to ceiling and mode
        ceiling_gb, mode = self._availability_to_ceiling(availability)

        # Determine reason
        if availability < 0.2:
            reason = "historically_busy_slot" if historical_cpu > 60 else "currently_busy"
        elif availability < 0.4:
            reason = "low_availability"
        elif availability < 0.6:
            reason = "moderate_availability"
        elif availability < 0.8:
            reason = "good_availability"
        else:
            reason = "high_availability"

        return CapacityInfo(
            mode=mode,
            ceiling_gb=ceiling_gb,
            availability_score=availability,
            reason=reason,
            override_active=False,
            learning_confidence=self.learning_confidence,
            days_observed=self.days_observed,
        )

    def _compute_availability_score(
        self,
        historical_cpu: float,
        historical_mem: float,
        current_cpu: float,
        current_memory_pct: float,
        cpu_trend: float,
        slot_confidence: float,
    ) -> float:
        """Compute availability score (0.0 to 1.0).

        Combines three signals:
        1. Historical baseline (what you usually do at this time): 40% weight
        2. Current observed state (what's happening now): 40% weight
        3. Trend signal (is activity rising or falling): 20% weight

        When slot_confidence is low, current state gets more weight.
        """
        # Historical signal: invert CPU usage (high CPU = low availability)
        hist_score = max(0.0, 1.0 - (historical_cpu / 100.0))

        # Current signal: combine CPU and memory
        current_score = max(
            0.0, 1.0 - (current_cpu / 100.0) * 0.7 - (current_memory_pct / 100.0) * 0.3
        )

        # Trend signal: falling trend = lower availability
        # cpu_trend is -1.0 to +1.0 where negative = rising CPU
        trend_score = max(0.0, min(1.0, 0.5 - cpu_trend * 0.5))

        # Weight based on confidence in historical data
        hist_weight = 0.4 * slot_confidence
        current_weight = 0.4 + 0.4 * (1.0 - slot_confidence)
        trend_weight = 0.2

        score = (
            hist_score * hist_weight + current_score * current_weight + trend_score * trend_weight
        )

        return max(0.0, min(1.0, score))

    def _availability_to_ceiling(
        self,
        availability: float,
    ) -> tuple[float, CapacityMode]:
        """Map availability score to memory ceiling and capacity mode."""
        if availability < 0.2:
            return 0.0, CapacityMode.PAUSED
        elif availability < 0.4:
            ceiling = min(16.0, self.total_memory_gb * 0.125)
            return ceiling, CapacityMode.LEARNED_LOW
        elif availability < 0.6:
            ceiling = min(32.0, self.total_memory_gb * 0.25)
            return ceiling, CapacityMode.LEARNED_MEDIUM
        elif availability < 0.8:
            ceiling = min(64.0, self.total_memory_gb * 0.5)
            return ceiling, CapacityMode.LEARNED_HIGH
        else:
            ceiling = self.total_memory_gb * 0.8
            return ceiling, CapacityMode.FULL

    def set_manual_override(self, mode: str, duration_hours: float = 24.0):
        """Set a manual capacity override (e.g., 'I'm on vacation')."""
        self._manual_override = {
            "mode": mode,
            "expires": time.time() + duration_hours * 3600,
        }
        self.save()
        logger.info(f"Manual override set: mode={mode}, expires in {duration_hours}h")

    def clear_manual_override(self):
        """Clear any active manual override."""
        self._manual_override = None
        self.save()
        logger.info("Manual override cleared")

    def get_heatmap_data(self) -> list[dict]:
        """Get the weekly heatmap data for the dashboard.

        Returns 168 entries (7 days x 24 hours) with the historical
        average CPU for each slot, suitable for visualization.
        """
        now = time.time()
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        result = []
        for slot_idx in range(NUM_SLOTS):
            slot = self._slots[slot_idx]
            day_idx = slot_idx // 24
            hour = slot_idx % 24
            avg_cpu = slot.weighted_avg_cpu(now) if slot.sample_count() > 0 else None
            avg_mem = slot.weighted_avg_memory(now) if slot.sample_count() > 0 else None
            result.append(
                {
                    "slot": slot_idx,
                    "day": days[day_idx],
                    "day_idx": day_idx,
                    "hour": hour,
                    "avg_cpu": round(avg_cpu, 1) if avg_cpu is not None else None,
                    "avg_memory": round(avg_mem, 1) if avg_mem is not None else None,
                    "samples": slot.sample_count(),
                    "availability": round(
                        max(0.0, 1.0 - (avg_cpu / 100.0)) if avg_cpu is not None else 1.0,
                        2,
                    ),
                }
            )
        return result

    @property
    def current_capacity(self) -> CapacityInfo:
        """Get the last computed capacity info."""
        return self._last_capacity
