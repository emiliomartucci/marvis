# Service-private dataclasses for Brain v1 (sub-01).
# Cross-boundary contracts (re-used outside api/services/brain/) live in
# api/models/brain.py — keep this file internal to the subpackage.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True, frozen=True)
class BrainSettings:
    """Loaded once per cycle from app_settings."""

    mode: str                       # "shadow" | "true" | "false"
    cutoff_hour_utc: int            # event-stream cutoff hour
    freeze_hour_utc: int            # cycle-key freeze hour (UTC)
    recompute_max_age_days: int
    per_source_event_cap: int
    lease_ttl_minutes: int


@dataclass(slots=True)
class SourceFailure:
    """Per-source failure isolation accumulator entry."""

    source_system: str
    error: str
    traceback: str | None = None


@dataclass(slots=True)
class CycleResult:
    """Return envelope for run_brain_jobs_if_due."""

    status: str
    run_id: str | None = None
    cycle_key: str | None = None
    event_count: int = 0
    journal_count: int = 0
    duration_ms: int | None = None
    partial_failures: list[SourceFailure] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class SourceCollectorContext:
    """Read-only context handed to each SourceCollector.

    Cycle window semantics (bug fix 2026-05-18):
      * ``cycle_window_start`` / ``cycle_window_end`` bound the substrate slice
        for THIS cycle independently of the watermark. They are derived from
        ``brain_runs.cycle_window_start_utc`` / ``cycle_window_end_utc`` so a
        manual recompute of a past cycle reads only that day's events even if
        the watermark advanced far ahead.
      * ``since_watermark`` is still consulted for incremental periodic runs:
        each collector reads from ``max(since_watermark, cycle_window_start)``
        and persists watermark advance based on the latest event observed.
      * For backward-compat the periodic path passes ``cycle_window_start =
        None`` and ``cycle_window_end = None`` so behavior collapses to the
        legacy ``watermark < observed_at <= cutoff_at`` filter.
    """

    cycle_key: str
    cutoff_at: datetime
    since_watermark: datetime
    now: datetime
    run_id: str
    workspace_id: str
    per_source_event_cap: int
    cycle_window_start: datetime | None = None
    cycle_window_end: datetime | None = None

    @property
    def lower_bound_iso(self) -> str:
        """SQL bind value — max(watermark, cycle_window_start) ISO string."""
        if (
            self.cycle_window_start is not None
            and self.cycle_window_start > self.since_watermark
        ):
            return self.cycle_window_start.isoformat()
        return self.since_watermark.isoformat()

    @property
    def upper_bound_iso(self) -> str:
        """SQL bind value — min(cutoff_at, cycle_window_end) ISO string.

        Note: cycle_window_end is EXCLUSIVE upper, cutoff_at is INCLUSIVE.
        We expose ISO without baking-in inclusivity — call sites pick the
        right comparator (`<` vs `<=`).
        """
        if (
            self.cycle_window_end is not None
            and self.cycle_window_end < self.cutoff_at
        ):
            return self.cycle_window_end.isoformat()
        return self.cutoff_at.isoformat()

    def in_window(self, observed_at: datetime) -> bool:
        """Single-source-of-truth Python-side predicate.

        Semantics:
          * observed_at > since_watermark (exclusive)
          * observed_at <= cutoff_at (inclusive)
          * if cycle_window_start: observed_at >= cycle_window_start
          * if cycle_window_end: observed_at < cycle_window_end (exclusive)
        """
        if observed_at <= self.since_watermark:
            return False
        if observed_at > self.cutoff_at:
            return False
        if (
            self.cycle_window_start is not None
            and observed_at < self.cycle_window_start
        ):
            return False
        if (
            self.cycle_window_end is not None
            and observed_at >= self.cycle_window_end
        ):
            return False
        return True


@dataclass(slots=True)
class EventDraft:
    """Raw event materialized by a collector before stable-id derivation."""

    event_type: str
    source_system: str
    source_ref: str
    title: str
    summary: str
    observed_at: datetime
    derived_from_state_at: datetime
    evidence: dict[str, Any] = field(default_factory=dict)
    source_project: str | None = None
    target_project: str | None = None
    program_key: str | None = None
    schema_version: int = 1
