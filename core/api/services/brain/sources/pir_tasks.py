# Marvis Tasks + Pull Requests source collector
# (sub-01 §3 — `task_changed` + `pr_changed`).
#
# `tasks.updated_at` and `pull_requests.created_at/merged_at/deploy_at` are
# written by SQLite `datetime('now')` (LOCAL — see project memory). Python-
# side normalize_iso() treats those as UTC per repo convention to avoid the
# common 2-hour-window drift incident documented in
# docs/solutions/2026-02-26-sqlite-datetime-timezone-naive.md.
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator

from core.api.db import acquire_db
from core.api.services.brain.digest_collector import SourceCollector
from core.api.services.brain.models import EventDraft, SourceCollectorContext
from core.api.services.brain.sources.base import (
    normalize_iso,
    resolve_source_project,
)

logger = logging.getLogger(__name__)


# Pagination page size — see ingestor._PAGE_SIZE rationale (bug fix 2026-05-18).
_PAGE_SIZE: int = 500


def _tags_hash(tags_raw: str | None) -> str:
    """Stable hash of the tags list (order-independent)."""
    try:
        tags = json.loads(tags_raw or "[]")
        if not isinstance(tags, list):
            tags = []
    except (TypeError, json.JSONDecodeError):
        tags = []
    canonical = json.dumps(sorted(str(t) for t in tags), separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


# ----------------------------------------------------------------------
# task_changed
# ----------------------------------------------------------------------


async def _collect_tasks(
    ctx: SourceCollectorContext,
) -> AsyncIterator[EventDraft]:
    """Yield `task_changed` events whose `updated_at` lands in the cycle window.

    Window = (max(watermark, cycle_window_start), min(cutoff_at, cycle_window_end)]
    SQL filter uses string comparison: substrate timestamps are ISO 8601
    lexicographically-ordered. After fetching, every row's `updated_at` is
    parsed and re-checked Python-side via ``ctx.in_window`` so a row with
    a malformed timestamp is skipped instead of poisoning the watermark.

    Pagination loop (LIMIT/OFFSET) — bug fix 2026-05-18: previous code
    fetched a single batch and the per-source cap silently truncated cycles
    with >1000 tasks updated.
    """
    lower_iso = ctx.lower_bound_iso
    upper_iso = ctx.upper_bound_iso

    offset = 0
    while True:
        async with acquire_db() as db:
            cur = await db.execute(
                "SELECT id, title, status, priority, project, tags, updated_at "
                "FROM tasks "
                "WHERE deleted_at IS NULL "
                "  AND updated_at > ? AND updated_at <= ? "
                "ORDER BY updated_at ASC "
                "LIMIT ? OFFSET ?",
                (lower_iso, upper_iso, _PAGE_SIZE, offset),
            )
            rows = await cur.fetchall()
        if not rows:
            break

        for row in rows:
            task_id = row[0] if not hasattr(row, "keys") else row["id"]
            title = row[1] if not hasattr(row, "keys") else row["title"]
            status = row[2] if not hasattr(row, "keys") else row["status"]
            priority = row[3] if not hasattr(row, "keys") else row["priority"]
            project = row[4] if not hasattr(row, "keys") else row["project"]
            tags_raw = row[5] if not hasattr(row, "keys") else row["tags"]
            updated_at_raw = row[6] if not hasattr(row, "keys") else row["updated_at"]

            observed_at = normalize_iso(updated_at_raw)
            if observed_at is None:
                continue
            if not ctx.in_window(observed_at):
                continue

            source_project, program_key = resolve_source_project(project)
            evidence = {
                "task_id": str(task_id),
                "status": status,
                "priority": priority,
                "project": project,
                "tags_hash": _tags_hash(tags_raw),
                "updated_at": observed_at.isoformat(),
            }
            yield EventDraft(
                event_type="task_changed",
                source_system="pir",
                source_ref=f"task:{task_id}",
                title=(title or f"Task {task_id}")[:200],
                summary=f"task status={status} priority={priority}",
                observed_at=observed_at,
                derived_from_state_at=observed_at,
                evidence=evidence,
                source_project=source_project,
                program_key=program_key,
            )

        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE


# ----------------------------------------------------------------------
# pr_changed
# ----------------------------------------------------------------------


def _latest_pr_state_iso(row: dict | tuple) -> str | None:
    """Pull requests have no single `updated_at`. We use max of the four
    state-transition timestamps the schema actually persists."""
    def _g(key: str, idx: int) -> str | None:
        return row[idx] if not hasattr(row, "keys") else row[key]
    candidates = [
        _g("created_at", 5),
        _g("merged_at", 6),
        _g("approved_at", 7),
        _g("deploy_at", 8),
    ]
    valid = [c for c in candidates if c]
    return max(valid) if valid else None


async def _collect_prs(
    ctx: SourceCollectorContext,
) -> AsyncIterator[EventDraft]:
    """Yield `pr_changed` events for pull_requests whose latest state
    transition timestamp lands in the cycle window.

    Window = (max(watermark, cycle_window_start), min(cutoff_at, cycle_window_end)]
    Pagination loop guards against cycles with >page_size PRs (bug fix 2026-05-18).
    The PR table has no canonical ``updated_at`` so we fetch by id and
    re-derive the state-transition timestamp Python-side; the pagination
    therefore caps row scan cost rather than emitted events.
    """
    offset = 0
    while True:
        async with acquire_db() as db:
            cur = await db.execute(
                "SELECT id, task_id, project, branch, status, "
                "       created_at, merged_at, approved_at, deploy_at, "
                "       deploy_status "
                "FROM pull_requests "
                "ORDER BY id ASC "
                "LIMIT ? OFFSET ?",
                (_PAGE_SIZE, offset),
            )
            rows = await cur.fetchall()
        if not rows:
            break

        for row in rows:
            latest_iso = _latest_pr_state_iso(row)
            if not latest_iso:
                continue
            observed_at = normalize_iso(latest_iso)
            if observed_at is None:
                continue
            if not ctx.in_window(observed_at):
                continue

            pr_id = row[0] if not hasattr(row, "keys") else row["id"]
            task_id = row[1] if not hasattr(row, "keys") else row["task_id"]
            project = row[2] if not hasattr(row, "keys") else row["project"]
            branch = row[3] if not hasattr(row, "keys") else row["branch"]
            status = row[4] if not hasattr(row, "keys") else row["status"]
            deploy_status = (
                row[9] if not hasattr(row, "keys") else row["deploy_status"]
            )

            source_project, program_key = resolve_source_project(project)

            evidence = {
                "pr_id": str(pr_id),
                "task_id": str(task_id),
                "status": status,
                "branch": branch,
                "deploy_status": deploy_status,
                "updated_at": observed_at.isoformat(),
            }

            # Decision marker (sub-01 §5.1): only stamped when the state is an
            # actual decision transition (merged/closed). Inferred from columns,
            # not from natural language.
            if status in ("merged", "closed"):
                evidence["decision_marker"] = (
                    "merged" if status == "merged" else "rejected"
                )
            elif deploy_status in ("success", "failed"):
                evidence["decision_marker"] = "deployed"

            yield EventDraft(
                event_type="pr_changed",
                source_system="pir",
                source_ref=f"pr:{task_id}",
                title=f"PR {branch} ({status})"[:200],
                summary=f"pr status={status} deploy={deploy_status or 'none'}",
                observed_at=observed_at,
                derived_from_state_at=observed_at,
                evidence=evidence,
                source_project=source_project,
                program_key=program_key,
            )

        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE


# ----------------------------------------------------------------------
# Public collector entry — chains tasks + PRs
# ----------------------------------------------------------------------


async def collect(ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    async for ev in _collect_tasks(ctx):
        yield ev
    async for ev in _collect_prs(ctx):
        yield ev


pir_tasks_collector = SourceCollector(source_system="pir", collect=collect)


__all__ = ["collect", "pir_tasks_collector"]
