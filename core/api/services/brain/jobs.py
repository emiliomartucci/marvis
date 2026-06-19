# Brain v1 — Top-level cycle orchestrator (sub-01 D5 entry).
# Mirrors the cycle/lease pattern from api/services/inbox_digest_jobs.py.
# Brain owns its cycle math — DO NOT import that module.
from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Literal

from core.api.config import settings as app_settings
from core.api.db import acquire_db, write_db
from core.api.services.brain.cycle import (
    current_brain_cycle_key,
    cycle_cutoff_at,
    persist_event,
    polish_run_journals,
    publish_run_journals,
    supersede_active_runs,
    update_run_status,
)
from core.api.services.brain.digest_collector import (
    SourceCollector,
    collect_from_source,
    registered_collectors,
)
from core.api.services.brain.models import (
    BrainSettings,
    CycleResult,
    SourceCollectorContext,
    SourceFailure,
)
from core.api.services.brain.watermarks import advance_watermark, get_watermark
from core.api.services.brain.ws_emitter import emit_phase_complete

logger = logging.getLogger(__name__)

# Reflection cost-mode (SP2 U1). DISTINCT from `BrainSettings.mode` (which is
# the brain *enablement* state: false / shadow / active). This gates only the
# single LLM-cost phase of the cycle:
#   - "full" → runs `polish_run_journals` (LLM journal polish + F5 citations).
#   - "free" → skips it; the rest of the cycle (substrate digest, deterministic
#     drift / memory-ops / findings, journals) runs unchanged at zero BYOK cost.
# Server scheduler keeps "full"; the OSS opportunistic upkeep floor uses "free".
ReflectionMode = Literal["free", "full"]


# ----------------------------------------------------------------------
# Off-peak scheduler gate (CE1 — sub-01 §11.5)
# ----------------------------------------------------------------------


def is_off_peak_window(now: datetime) -> bool:
    """Return True when *now* (UTC) is in the off-peak window.

    Window: weekends (Sat/Sun) OR hour >= 22 OR hour < 6 UTC. The gate is a
    soft skip; idempotent retry happens on the next periodic poll.
    """
    hour = now.hour
    is_weekend = now.weekday() >= 5
    return is_weekend or hour >= 22 or hour < 6


# ----------------------------------------------------------------------
# Settings loader (app_settings, prefix brain_)
# ----------------------------------------------------------------------


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        return max(minimum, parsed)
    return parsed


async def _get_setting(db, key: str, default: str) -> str:
    row = await (
        await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    ).fetchone()
    if row is None:
        return default
    return row[0] if not hasattr(row, "keys") else row["value"]


async def load_brain_settings(db) -> BrainSettings:
    mode = await _get_setting(db, "brain_enabled", "shadow")
    cutoff = _as_int(await _get_setting(db, "brain_cutoff_hour_utc", "6"), 6)
    freeze = _as_int(await _get_setting(db, "brain_freeze_hour_utc", "4"), 4)
    max_age = _as_int(
        await _get_setting(db, "brain_recompute_max_age_days", "30"),
        30,
        minimum=1,
    )
    per_source_cap = _as_int(
        await _get_setting(db, "brain_per_source_event_cap", "1000"),
        1000,
        minimum=1,
    )
    lease_ttl = _as_int(
        await _get_setting(db, "brain_run_lease_ttl_minutes", "120"),
        120,
        minimum=5,
    )
    return BrainSettings(
        mode=mode,
        cutoff_hour_utc=cutoff,
        freeze_hour_utc=freeze,
        recompute_max_age_days=max_age,
        per_source_event_cap=per_source_cap,
        lease_ttl_minutes=lease_ttl,
    )


# ----------------------------------------------------------------------
# Lease (app_settings JSON value, TTL escape valve)
# ----------------------------------------------------------------------


def _lease_key(workspace_id: str) -> str:
    return f"brain_run_lease_{workspace_id}"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _claim_run_lease(
    *, workspace_id: str, cycle_key: str, now: datetime, ttl_minutes: int
) -> dict[str, Any]:
    key = _lease_key(workspace_id)
    async with write_db() as db:
        row = await (
            await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        ).fetchone()
        if row is not None:
            raw = row[0] if not hasattr(row, "keys") else row["value"]
            try:
                current = json.loads(raw or "{}")
            except json.JSONDecodeError:
                current = {}
            started_at = _parse_dt(current.get("started_at"))
            if (
                current.get("cycle_key") == cycle_key
                and started_at is not None
                and (now - started_at).total_seconds() < ttl_minutes * 60
            ):
                return {"claimed": False, "started_at": started_at.isoformat()}

        value = json.dumps(
            {"cycle_key": cycle_key, "started_at": now.isoformat()},
            ensure_ascii=False,
        )
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now.isoformat()),
        )
    return {"claimed": True, "started_at": now.isoformat()}


async def _release_run_lease(*, workspace_id: str) -> None:
    async with write_db() as db:
        await db.execute(
            "DELETE FROM app_settings WHERE key = ?", (_lease_key(workspace_id),)
        )


# ----------------------------------------------------------------------
# Cycle runner
# ----------------------------------------------------------------------


async def _record_last_cycle(*, cycle_key: str, now: datetime) -> None:
    async with write_db() as db:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            ("brain_last_cycle_key", cycle_key, now.isoformat()),
        )


async def _warehouse_consolidation_due(
    *, workspace_id: str, now: datetime, interval_seconds: int
) -> bool:
    """Daily cadence guard for the warehouse consolidation pass.

    Derives "last run" from the newest persisted warehouse-consolidation
    proposal — these are `operation_type='consolidate'` ops whose source_ref
    points at a learning (`learning:...`), which window M2 never emits (M2
    consolidates digest-event source_refs). Returns False (skip) if the last
    such op was persisted within ``interval_seconds`` of ``now``. The pass is
    idempotent regardless (operation_id collision dedups within a cycle), so a
    missing/unparseable timestamp falls back to "due" — at worst a harmless
    re-scan that produces zero new rows.
    """
    if interval_seconds <= 0:
        return True
    async with acquire_db() as db:
        row = await (
            await db.execute(
                "SELECT MAX(detected_at) FROM brain_memory_operations"
                " WHERE operation_type = 'consolidate'"
                "  AND source_ref LIKE 'learning:%'",
            )
        ).fetchone()
    last_raw = row[0] if row else None
    if not last_raw:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    elapsed = (now.astimezone(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds()
    return elapsed >= interval_seconds


async def _temporal_recency_due(
    *, workspace_id: str, now: datetime, interval_seconds: int
) -> bool:
    """Daily cadence guard for the KG temporal recency pass (Fase D).

    Derives "last run" from the newest persisted recency proposal — these are
    `operation_type='reinforce'` ops whose proposed_write target_type is 'none'
    (M1 reinforce uses 'kg_edge_metric', so this filter isolates the recency
    pass). Idempotent regardless (operation_id collision dedups within a cycle),
    so a missing/unparseable timestamp falls back to "due".
    """
    if interval_seconds <= 0:
        return True
    async with acquire_db() as db:
        row = await (
            await db.execute(
                "SELECT MAX(detected_at) FROM brain_memory_operations"
                " WHERE operation_type = 'reinforce'"
                "  AND proposed_write_target_type = 'none'",
            )
        ).fetchone()
    last_raw = row[0] if row else None
    if not last_raw:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    elapsed = (now.astimezone(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds()
    return elapsed >= interval_seconds


def _cycle_window_from_key(
    cycle_key: str, *, cutoff_hour_utc: int
) -> tuple[datetime, datetime]:
    """Derive (cycle_window_start_utc, cycle_window_end_utc) from a cycle_key.

    The Brain cycle represents the 24h window of substrate activity that ended
    at ``cutoff_at`` (typically 06:00 UTC). So for ``cycle_key='2026-04-18'``
    with ``cutoff_hour_utc=6``:
      * window_start = 2026-04-17T06:00:00Z (previous day's cutoff)
      * window_end   = 2026-04-18T06:00:00Z (this cycle's cutoff, exclusive)

    Returns timezone-aware UTC datetimes. Raises ValueError on malformed key.
    """
    cycle_date = datetime.fromisoformat(cycle_key).date()
    window_end = datetime.combine(
        cycle_date,
        dtime(hour=cutoff_hour_utc, tzinfo=timezone.utc),
    )
    window_start = window_end - timedelta(days=1)
    return window_start, window_end


async def _execute_cycle(
    *,
    workspace_id: str,
    cycle_key: str,
    cutoff_at: datetime,
    settings: BrainSettings,
    trigger: str,
    triggered_by: str | None,
    now: datetime,
    collectors: list[SourceCollector] | None = None,
    cycle_window_start: datetime | None = None,
    cycle_window_end: datetime | None = None,
    reflection_mode: ReflectionMode = "full",
) -> CycleResult:
    """Run a single cycle end-to-end. Caller holds the lease.

    Per-source failures are isolated: one bad source → status='partial', not
    'failed'. Hard exception in journal aggregation → status='failed'.
    """
    started = time.monotonic()
    failures: list[SourceFailure] = []
    event_count = 0
    journal_count = 0

    if collectors is None:
        collectors = registered_collectors()
        if not collectors:
            # Standalone runtime (`marvis brain run` / brain_cycles_recompute via
            # the CLI + stdio MCP) never runs the API lifespan startup hook that
            # registers the source collectors, so the registry is empty and the
            # cycle would silently collect 0 events even with in-window data
            # (issue #6). Register them lazily here so the standalone path lights
            # up the same capture→reflect collectors as the API service. Idempotent
            # and inert when the API already registered them at startup.
            from core.api.services.brain.sources import register_all_collectors

            register_all_collectors()
            collectors = registered_collectors()

    # Cycle window resolution (bug fix 2026-05-18). When caller did not pass
    # explicit window bounds (legacy periodic path), default to the legacy
    # behavior: window = (cutoff_at - 24h, cutoff_at]. Manual recompute always
    # passes explicit bounds derived from cycle_key so backfill of past cycles
    # reads only that cycle's substrate slice — not the entire backlog.
    if cycle_window_end is None:
        cycle_window_end = cutoff_at
    if cycle_window_start is None:
        cycle_window_start = cutoff_at - timedelta(days=1)

    async with write_db() as db:
        # Pre-allocate the new run_id so we can mark predecessors as superseded
        # in one update that points directly at it, satisfying the partial
        # unique index uniq_brain_runs_active_cycle before the new insert lands.
        import uuid as _uuid

        run_id = _uuid.uuid4().hex
        await supersede_active_runs(
            db, workspace_id=workspace_id, cycle_key=cycle_key, new_run_id=run_id
        )
        await db.execute(
            "INSERT INTO brain_runs ("
            " run_id, workspace_id, cycle_key,"
            " cycle_window_start_utc, cycle_window_end_utc, cutoff_hour_utc_at_run,"
            " scope_type, scope_key, trigger, triggered_by, started_at, status"
            ") VALUES (?, ?, ?, ?, ?, ?, 'company', '__company__', ?, ?, ?, 'running')",
            (
                run_id,
                workspace_id,
                cycle_key,
                cycle_window_start.astimezone(timezone.utc).isoformat(),
                cycle_window_end.astimezone(timezone.utc).isoformat(),
                settings.cutoff_hour_utc,
                trigger,
                triggered_by,
                now.astimezone(timezone.utc).isoformat(),
            ),
        )

    await emit_phase_complete(
        cycle_key=cycle_key,
        run_id=run_id,
        status="running",
        phase="digest",
        deltas={"events": 0, "drift": 0, "memory_ops": 0, "findings": 0},
    )

    try:
        for collector in collectors:
            since = await get_watermark(
                source_system=collector.source_system, workspace_id=workspace_id
            )
            ctx = SourceCollectorContext(
                cycle_key=cycle_key,
                cutoff_at=cutoff_at,
                since_watermark=since,
                now=now,
                run_id=run_id,
                workspace_id=workspace_id,
                per_source_event_cap=settings.per_source_event_cap,
                cycle_window_start=cycle_window_start,
                cycle_window_end=cycle_window_end,
            )
            try:
                drafts = await collect_from_source(collector, ctx)
            except Exception as exc:  # noqa: BLE001 — per-source isolation
                logger.exception(
                    "brain.jobs: source %s collect failed", collector.source_system
                )
                failures.append(
                    SourceFailure(
                        source_system=collector.source_system,
                        error=str(exc),
                        traceback=traceback.format_exc(),
                    )
                )
                continue

            latest_observed = since
            last_event_id: str | None = None
            async with write_db() as db:
                for draft in drafts:
                    try:
                        event_id = await persist_event(
                            db, run_id=run_id, cycle_key=cycle_key, draft=draft
                        )
                    except Exception as exc:  # noqa: BLE001 — per-event isolation
                        logger.exception(
                            "brain.jobs: persist_event failed for %s/%s",
                            collector.source_system,
                            draft.source_ref,
                        )
                        failures.append(
                            SourceFailure(
                                source_system=collector.source_system,
                                error=f"persist_event: {exc}",
                                traceback=traceback.format_exc(),
                            )
                        )
                        continue
                    event_count += 1
                    if draft.observed_at > latest_observed:
                        latest_observed = draft.observed_at
                        last_event_id = event_id

            if latest_observed > since:
                # Clamp watermark advance to cycle_window_end so backfilling
                # an OLD cycle doesn't push the watermark past its window
                # (which would silently skip future cycles' substrate slices).
                clamped_observed = (
                    cycle_window_end
                    if latest_observed > cycle_window_end
                    else latest_observed
                )
                await advance_watermark(
                    source_system=collector.source_system,
                    workspace_id=workspace_id,
                    observed_at=clamped_observed,
                    last_event_id=last_event_id,
                    cycle_key=cycle_key,
                    now=now,
                )

        async with write_db() as db:
            journal_count = await publish_run_journals(
                db,
                run_id=run_id,
                cycle_key=cycle_key,
                workspace_id=workspace_id,
                now=now,
            )

        # Wave 3.1 gap 2: best-effort LLM polish, persists to DB so reads
        # never re-pay the LLM cost. No-op when brain_llm_polish_enabled=0.
        # SP2 U1: this is the cycle's ONLY LLM-cost phase — skipped entirely in
        # "free" reflection mode (the OSS opportunistic upkeep floor) so a run
        # never touches a BYOK key unless the operator opted into "full".
        if reflection_mode == "full":
            try:
                await polish_run_journals(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    now=now,
                )
            except Exception:  # noqa: BLE001 — phase-level isolation
                logger.exception("brain.jobs: journal polish failed")
        else:
            logger.debug(
                "brain.jobs: journal polish skipped reason=free_mode run_id=%s",
                run_id,
            )

        await emit_phase_complete(
            cycle_key=cycle_key,
            run_id=run_id,
            status="running",
            phase="journal",
            deltas={"events": event_count, "drift": 0, "memory_ops": 0, "findings": 0},
        )

        # Phase 3 — Drift Checker (sub-02). Inline phase of the same run.
        # Failures here append to `failures` so brain_runs.status = 'partial'.
        try:
            from core.api.services.brain.drift import run_phase as run_drift_phase

            drift_report = await run_drift_phase(
                run_id=run_id,
                cycle_key=cycle_key,
                workspace_id=workspace_id,
                now=now,
            )
            for pf in drift_report.partial_failures:
                failures.append(
                    SourceFailure(
                        source_system=f"drift:{pf.get('rule_id', 'unknown')}",
                        error=pf.get("error", "unknown"),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — phase-level isolation
            logger.exception("brain.jobs: drift phase failed")
            failures.append(
                SourceFailure(source_system="drift", error=str(exc))
            )

        await emit_phase_complete(
            cycle_key=cycle_key,
            run_id=run_id,
            status="running",
            phase="drift",
            deltas={"events": event_count, "drift": 0, "memory_ops": 0, "findings": 0},
        )

        # Phase 4 — Memory Operations (sub-03). Runs after Drift, before
        # Findings (sub-04). Cascade rollup gated by setting.
        try:
            from core.api.services.brain.memory_ops import run_phase as run_memory_ops_phase

            memory_ops_report = await run_memory_ops_phase(
                run_id=run_id,
                cycle_key=cycle_key,
                workspace_id=workspace_id,
                now=now,
                include_cascade=True,
            )
            for pf in memory_ops_report.partial_failures:
                failures.append(
                    SourceFailure(
                        source_system=f"memory_op:{pf.get('rule_id', 'unknown')}",
                        error=pf.get("error", "unknown"),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — phase-level isolation
            logger.exception("brain.jobs: memory ops phase failed")
            failures.append(
                SourceFailure(source_system="memory_ops", error=str(exc))
            )

        await emit_phase_complete(
            cycle_key=cycle_key,
            run_id=run_id,
            status="running",
            phase="memory_ops",
            deltas={"events": event_count, "drift": 0, "memory_ops": 0, "findings": 0},
        )

        # Phase 4b — Warehouse consolidation (full-store learning dedup).
        # Ships DORMANT: gated on brain_warehouse_consolidation_enabled (default
        # False → skipped entirely, no-op). When enabled, a daily cadence guard
        # skips if a warehouse-consolidation proposal was persisted within
        # brain_warehouse_consolidation_interval_seconds. PROPOSALS only —
        # never auto-applied. Isolated try/except so it can NEVER crash the
        # cycle (findings phase must still complete).
        if app_settings.brain_warehouse_consolidation_enabled:
            try:
                from core.api.services.brain.warehouse_consolidate import (
                    run_warehouse_consolidation,
                )

                if await _warehouse_consolidation_due(
                    workspace_id=workspace_id,
                    now=now,
                    interval_seconds=app_settings.brain_warehouse_consolidation_interval_seconds,
                ):
                    wh_summary = await run_warehouse_consolidation(
                        run_id=run_id,
                        cycle_key=cycle_key,
                        workspace_id=workspace_id,
                        now=now,
                    )
                    logger.info(
                        "brain.jobs: warehouse consolidation run_id=%s summary=%s",
                        run_id,
                        wh_summary,
                    )
                else:
                    logger.info(
                        "brain.jobs: warehouse consolidation skipped (cadence guard) run_id=%s",
                        run_id,
                    )
            except Exception as exc:  # noqa: BLE001 — phase-level isolation
                logger.exception("brain.jobs: warehouse consolidation failed")
                failures.append(
                    SourceFailure(source_system="warehouse_consolidation", error=str(exc))
                )

        # Phase 4c — KG temporal recency (Fase D producer). Ships DORMANT: gated
        # on temporal_memory_enabled (the MARVIS_TEMPORAL_MEMORY flag IS the gate;
        # default off → skipped). When on, a daily cadence guard bounds re-emission.
        # PROPOSALS only — never auto-applied. Isolated try/except so it can NEVER
        # crash the cycle.
        if app_settings.temporal_memory_enabled:
            try:
                from core.api.services.brain.temporal_recency import (
                    run_temporal_recency,
                )

                if await _temporal_recency_due(
                    workspace_id=workspace_id,
                    now=now,
                    interval_seconds=app_settings.brain_temporal_recency_interval_seconds,
                ):
                    tr_summary = await run_temporal_recency(
                        run_id=run_id,
                        cycle_key=cycle_key,
                        workspace_id=workspace_id,
                        now=now,
                    )
                    logger.info(
                        "brain.jobs: temporal recency run_id=%s summary=%s",
                        run_id,
                        tr_summary,
                    )
                else:
                    logger.info(
                        "brain.jobs: temporal recency skipped (cadence guard) run_id=%s",
                        run_id,
                    )
            except Exception as exc:  # noqa: BLE001 — phase-level isolation
                logger.exception("brain.jobs: temporal recency failed")
                failures.append(
                    SourceFailure(source_system="temporal_recency", error=str(exc))
                )

        # Phase 5 — Learn Findings (sub-04). Final L5 phase of the cycle.
        # Consumes digest events + journal entries + drift signals + memory
        # operations of the current run_id (NEVER re-reads substrate).
        try:
            from core.api.services.brain.findings import run_phase as run_findings_phase

            findings_report = await run_findings_phase(
                run_id=run_id,
                cycle_key=cycle_key,
                workspace_id=workspace_id,
                now=now,
            )
            for pf in findings_report.partial_failures:
                failures.append(
                    SourceFailure(
                        source_system=f"finding_rule:{pf.get('rule_id', 'unknown')}",
                        error=pf.get("error", "unknown"),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — phase-level isolation
            logger.exception("brain.jobs: findings phase failed")
            failures.append(
                SourceFailure(source_system="findings", error=str(exc))
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        status = "succeeded" if not failures else "partial"
        async with write_db() as db:
            await update_run_status(
                db,
                run_id=run_id,
                status=status,
                event_count=event_count,
                partial_failures=failures,
                duration_ms=duration_ms,
                finished_at=now,
            )
        await _record_last_cycle(cycle_key=cycle_key, now=now)

        await emit_phase_complete(
            cycle_key=cycle_key,
            run_id=run_id,
            status=status,
            phase="done",
            deltas={
                "events": event_count,
                "drift": 0,
                "memory_ops": 0,
                "findings": 0,
            },
        )

        return CycleResult(
            status="partial" if failures else "ok",
            run_id=run_id,
            cycle_key=cycle_key,
            event_count=event_count,
            journal_count=journal_count,
            duration_ms=duration_ms,
            partial_failures=failures,
        )

    except Exception as exc:  # noqa: BLE001 — cycle-fatal error
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.exception("brain.jobs: cycle %s failed", cycle_key)
        async with write_db() as db:
            await update_run_status(
                db,
                run_id=run_id,
                status="failed",
                event_count=event_count,
                partial_failures=failures,
                duration_ms=duration_ms,
                error_summary=str(exc),
                finished_at=now,
            )
        await emit_phase_complete(
            cycle_key=cycle_key,
            run_id=run_id,
            status="failed",
            phase="done",
            deltas={"events": event_count, "drift": 0, "memory_ops": 0, "findings": 0},
        )
        return CycleResult(
            status="failed",
            run_id=run_id,
            cycle_key=cycle_key,
            event_count=event_count,
            journal_count=0,
            duration_ms=duration_ms,
            partial_failures=failures,
            error=str(exc),
        )


def _timedelta_days_safe(days: int):
    from datetime import timedelta

    return timedelta(days=days)


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------


async def run_brain_jobs_if_due(
    *, now: datetime | None = None, workspace_id: str = "ws_default"
) -> dict[str, Any]:
    """Periodic scheduler entry — idempotent per cycle.

    Returns one of:
      {"status": "disabled"}            — brain_enabled=false
      {"status": "idle", ...}           — before cutoff or cycle already published
      {"status": "ok", ...}             — successful run
      {"status": "already_running", ...} — concurrent invocation
      {"status": "partial", ...}        — per-source failures
      {"status": "failed", ...}         — cycle-fatal error
    """
    now = now or datetime.now(timezone.utc)

    if app_settings.brain_run_off_peak_only and not is_off_peak_window(now):
        logger.debug(
            "brain.cycle.skipped_business_hours run_attempt at %s", now.isoformat()
        )
        return {
            "status": "skipped_business_hours",
            "reason": "off_peak_only_flag",
        }

    async with acquire_db() as db:
        settings = await load_brain_settings(db)
        if settings.mode == "false":
            return {"status": "disabled"}
        last_cycle_key = await _get_setting(db, "brain_last_cycle_key", "")

    cycle_key = current_brain_cycle_key(now, settings.freeze_hour_utc)
    cutoff_at = cycle_cutoff_at(now, settings.cutoff_hour_utc)

    if now.hour < settings.cutoff_hour_utc:
        return {"status": "idle", "cycle_key": cycle_key, "reason": "before_cutoff"}
    if last_cycle_key == cycle_key:
        return {
            "status": "idle",
            "cycle_key": cycle_key,
            "reason": "cycle_already_published",
        }

    lease = await _claim_run_lease(
        workspace_id=workspace_id,
        cycle_key=cycle_key,
        now=now,
        ttl_minutes=settings.lease_ttl_minutes,
    )
    if not lease["claimed"]:
        return {
            "status": "already_running",
            "cycle_key": cycle_key,
            "lease_started_at": lease.get("started_at"),
        }

    try:
        result = await _execute_cycle(
            workspace_id=workspace_id,
            cycle_key=cycle_key,
            cutoff_at=cutoff_at,
            settings=settings,
            trigger="batch",
            triggered_by=None,
            now=now,
        )
    finally:
        await _release_run_lease(workspace_id=workspace_id)

    return _cycle_result_to_payload(result, settings_mode=settings.mode)


async def recompute_brain_cycle(
    cycle_key: str,
    *,
    triggered_by: str | None,
    now: datetime | None = None,
    workspace_id: str = "ws_default",
    force: bool = False,
) -> dict[str, Any]:
    """Manual recompute entry — used by D4 POST /brain/cycles/{key}/recompute."""
    now = now or datetime.now(timezone.utc)
    async with acquire_db() as db:
        settings = await load_brain_settings(db)
        if settings.mode == "false":
            return {"status": "disabled", "cycle_key": cycle_key}

    # Validate cycle_key early — both for force=True and force=False paths.
    try:
        cycle_date = datetime.fromisoformat(cycle_key).date()
    except ValueError:
        return {
            "status": "rejected",
            "error_kind": "invalid_cycle_key",
            "cycle_key": cycle_key,
        }

    # Cycle window: anchored on the cycle_key, NOT on wallclock `now`. Without
    # this, manual recompute of past cycles used `cycle_cutoff_at(now, ...)`
    # which slides forward to today's cutoff — causing every backfill cycle
    # to read the entire backlog into the first run and zero into the rest
    # (regression observed 2026-05-17/18).
    cycle_window_start, cycle_window_end = _cycle_window_from_key(
        cycle_key, cutoff_hour_utc=settings.cutoff_hour_utc
    )
    cutoff_at = cycle_window_end

    if not force:
        today = now.date()
        age_days = (today - cycle_date).days
        if age_days > settings.recompute_max_age_days:
            return {
                "status": "rejected",
                "error_kind": "cycle_too_old",
                "cycle_key": cycle_key,
                "age_days": age_days,
            }

    lease = await _claim_run_lease(
        workspace_id=workspace_id,
        cycle_key=cycle_key,
        now=now,
        ttl_minutes=settings.lease_ttl_minutes,
    )
    if not lease["claimed"]:
        return {
            "status": "already_running",
            "cycle_key": cycle_key,
            "lease_started_at": lease.get("started_at"),
        }

    try:
        result = await _execute_cycle(
            workspace_id=workspace_id,
            cycle_key=cycle_key,
            cutoff_at=cutoff_at,
            settings=settings,
            trigger="manual",
            triggered_by=triggered_by,
            now=now,
            cycle_window_start=cycle_window_start,
            cycle_window_end=cycle_window_end,
        )
    finally:
        await _release_run_lease(workspace_id=workspace_id)

    return _cycle_result_to_payload(result, settings_mode=settings.mode)


def _cycle_result_to_payload(result: CycleResult, *, settings_mode: str) -> dict[str, Any]:
    return {
        "status": result.status,
        "run_id": result.run_id,
        "cycle_key": result.cycle_key,
        "event_count": result.event_count,
        "journal_count": result.journal_count,
        "duration_ms": result.duration_ms,
        "partial_failures": [
            {"source_system": f.source_system, "error": f.error}
            for f in result.partial_failures
        ],
        "mode": settings_mode,
        "error": result.error,
    }


async def run_brain_cycle_once(
    *,
    reflection_mode: ReflectionMode = "full",
    now: datetime | None = None,
    workspace_id: str = "ws_default",
) -> dict[str, Any]:
    """One-shot cycle runner for ``marvis brain run`` (SP2 U1).

    Also the entry the U2 opportunistic trigger and the U3 timer invoke. Unlike
    :func:`run_brain_jobs_if_due` it is NOT gated by the off-peak / before-cutoff
    / already-published idle checks — an explicit "run now" always runs the
    current cycle — but it IS lease-guarded, so a concurrent invocation returns
    ``already_running`` instead of double-running. Shares the SAME
    :func:`_execute_cycle` path as the scheduler (no second cycle code path).

    ``reflection_mode="free"`` skips the LLM journal-polish phase (no BYOK cost);
    ``"full"`` runs it (degrading to the deterministic narrative when no brain
    LLM gateway is configured).
    """
    now = now or datetime.now(timezone.utc)

    async with acquire_db() as db:
        settings = await load_brain_settings(db)
        if settings.mode == "false":
            return {"status": "disabled", "reflection_mode": reflection_mode}

    cycle_key = current_brain_cycle_key(now, settings.freeze_hour_utc)
    cutoff_at = cycle_cutoff_at(now, settings.cutoff_hour_utc)

    lease = await _claim_run_lease(
        workspace_id=workspace_id,
        cycle_key=cycle_key,
        now=now,
        ttl_minutes=settings.lease_ttl_minutes,
    )
    if not lease["claimed"]:
        return {
            "status": "already_running",
            "cycle_key": cycle_key,
            "lease_started_at": lease.get("started_at"),
            "reflection_mode": reflection_mode,
        }

    try:
        result = await _execute_cycle(
            workspace_id=workspace_id,
            cycle_key=cycle_key,
            cutoff_at=cutoff_at,
            settings=settings,
            trigger="manual",
            triggered_by="marvis brain run",
            now=now,
            reflection_mode=reflection_mode,
        )
    finally:
        await _release_run_lease(workspace_id=workspace_id)

    payload = _cycle_result_to_payload(result, settings_mode=settings.mode)
    payload["reflection_mode"] = reflection_mode
    return payload


__all__ = [
    "is_off_peak_window",
    "load_brain_settings",
    "recompute_brain_cycle",
    "run_brain_cycle_once",
    "run_brain_jobs_if_due",
]
