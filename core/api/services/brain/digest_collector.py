# Brain v1 — Digest source collectors (sub-01 D1).
# Each collector is mechanical: reads substrate ≤ cutoff_at, yields EventDraft objects.
# No LLM, no business state mutation, no auto-task creation.
from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
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
    """Materialize a collector with orchestrator-side pagination (Wave 3.1 gap 5).

    Pattern:
      * Pull up to `ctx.per_source_event_cap` drafts in one pass.
      * If the batch hits the cap, advance `ctx.since` to the max observed_at
        seen so far and re-call the collector. Repeat until a partial batch
        comes back or the bound `MAX_COLLECT_ITERATIONS` is reached.
      * No synthetic `external_update_seen` overflow event — the orchestrator
        now drains the source completely and the cycle reports `succeeded`
        instead of `partial`.

    Guards:
      * Bail when no watermark progress (last_observed_at <= ctx.since).
      * Bail at MAX_COLLECT_ITERATIONS with a WARNING — keeps a pathological
        source from blocking the cycle.
    """
    all_drafts: list[EventDraft] = []
    current_ctx = ctx
    iterations = 0

    while True:
        batch: list[EventDraft] = []
        cap = current_ctx.per_source_event_cap
        async for draft in collector.collect(current_ctx):
            if len(batch) >= cap:
                # Stop reading from this batch — the collector should not
                # return more than cap, but we defend regardless.
                break
            batch.append(draft)
        all_drafts.extend(batch)
        iterations += 1

        if len(batch) < cap:
            # Batch is below cap → source is fully drained for this window.
            break

        if iterations >= MAX_COLLECT_ITERATIONS:
            logger.warning(
                "collect_from_source: max %d iterations reached for %s "
                "(cycle=%s) — stopping pagination",
                MAX_COLLECT_ITERATIONS,
                collector.source_system,
                current_ctx.cycle_key,
            )
            break

        # Advance the watermark on the iteration ctx so the next call returns
        # the subsequent slice. We use the max observed_at to keep ordering
        # robust even when collectors yield slightly out-of-order events.
        try:
            last_observed = max(d.observed_at for d in batch)
        except ValueError:
            break

        if last_observed <= current_ctx.since_watermark:
            logger.warning(
                "collect_from_source: no watermark advance for %s (cycle=%s, "
                "since=%s, last_observed=%s) — stopping to avoid infinite loop",
                collector.source_system,
                current_ctx.cycle_key,
                current_ctx.since_watermark.isoformat() if current_ctx.since_watermark else "",
                last_observed.isoformat() if last_observed else "",
            )
            break

        current_ctx = dataclasses.replace(current_ctx, since_watermark=last_observed)

    return all_drafts


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
