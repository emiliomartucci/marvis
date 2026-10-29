# Brain v1 — Digest source collectors (sub-01 D1).
# Each collector is mechanical: reads substrate ≤ cutoff_at, yields EventDraft objects.
# No LLM, no business state mutation, no auto-task creation.
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from core.api.services.brain.models import EventDraft, SourceCollectorContext

logger = logging.getLogger(__name__)


CollectorFn = Callable[[SourceCollectorContext], AsyncIterator[EventDraft]]

# Wave 3.1 gap 5: orchestrator-side pagination upper bound. A pathological
# source that never advances its watermark must not loop forever; this caps
# total iterations per (source, cycle) so backfill stays bounded even when a
# collector mis-orders observed_at. With cap=1000 the effective ceiling is
# 50_000 events per source per cycle — well above any realistic substrate.
MAX_COLLECT_ITERATIONS = 50


@dataclass(slots=True, frozen=True)
class SourceCollector:
    """Minimal collector descriptor. Concrete implementations live alongside
    the substrate they read — Brain just sequences them per cycle.

    For v1 the implementations are stubs registered explicitly from jobs.py.
    Sub-02/03/04 will register additional collectors as phases extend.
    """

    source_system: str
    collect: CollectorFn


_REGISTERED: list[SourceCollector] = []


def register_collector(collector: SourceCollector) -> None:
    """Idempotent registration keyed by source_system."""
    for existing in _REGISTERED:
        if existing.source_system == collector.source_system:
            return
    _REGISTERED.append(collector)


def registered_collectors() -> list[SourceCollector]:
    return list(_REGISTERED)


def clear_registered_collectors() -> None:
    """Test-only helper."""
    _REGISTERED.clear()


async def empty_collector(_ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    """Default no-op collector used as a placeholder for sources without a producer."""
    if False:  # pragma: no cover — keeps the async generator shape.
        yield None  # type: ignore[misc]


async def collect_from_source(
    collector: SourceCollector, ctx: SourceCollectorContext
) -> list[EventDraft]:
    """Drain one iterator; never infer a cursor from unordered timestamps.

    The total cap bounds memory and work. Reading one extra item distinguishes
    an exactly-full, exhausted source from an incomplete source. Incomplete
    batches raise before persistence, leaving the durable watermark unchanged.
    """
    drafts: list[EventDraft] = []
    total_cap = ctx.per_source_event_cap * MAX_COLLECT_ITERATIONS
    async for draft in collector.collect(ctx):
        if len(drafts) >= total_cap:
            raise RuntimeError(f"source_incomplete: total event cap {total_cap} exceeded")
        drafts.append(draft)
    return drafts


__all__ = [
    "CollectorFn",
    "MAX_COLLECT_ITERATIONS",
    "SourceCollector",
    "clear_registered_collectors",
    "collect_from_source",
    "empty_collector",
    "register_collector",
    "registered_collectors",
]
