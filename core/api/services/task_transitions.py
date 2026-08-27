# v1.5.0 - 2026-04-12 - Add WIP limit check on approved -> in_progress transition
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from core.api.config import settings
from core.api.models import VALID_TRANSITIONS

logger = logging.getLogger(__name__)


class TaskTransitionConflict(ValueError):
    """The task changed after the transition's expected version was read."""

    def __init__(self, task_id: str, status: str | None, updated_at: str | None):
        self.task_id = task_id
        self.status = status
        self.updated_at = updated_at
        super().__init__(
            f"Task transition conflict: task={task_id}, "
            f"current_status={status!r}, current_updated_at={updated_at!r}"
        )


def _next_mutation_version(current: str | None) -> str:
    """Return a wall-clock version strictly newer than the persisted token."""
    now = datetime.now(timezone.utc)
    previous: datetime | None = None
    if current:
        try:
            previous = datetime.fromisoformat(current.replace("Z", "+00:00"))
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            else:
                previous = previous.astimezone(timezone.utc)
        except (TypeError, ValueError):
            previous = None
    if previous is not None and now <= previous:
        now = previous + timedelta(microseconds=1)
    return now.isoformat(timespec="microseconds")


async def validate_and_transition_task(
    db: aiosqlite.Connection,
    task_id: str,
    new_status: str,
    *,
    trigger: str = "manual",
    auto_commit: bool = True,
    expected_updated_at: str | None = None,
    actor: str | None = None,
    workspace_id: str,
) -> None:
    """Validate and apply a task status transition.

    Used by both tasks.py PATCH and pr_service.py auto-transitions.
    Raises ValueError if the transition is not allowed.
    No-op if already in the target status.

    The code/system "require merged PR" guard only fires when the task has
    completion_mode='pr' (the default, backward-compatible). Research tasks
    created with completion_mode='doc' or 'none' transition freely and are
    expected to be closed by the agent/human once the doc/handoff exists.
    """
    cursor = await db.execute(
        "SELECT id, status, completion_mode, updated_at, workspace_id "
        "FROM tasks WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
        (task_id, workspace_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise ValueError(f"Task not found: {task_id}")

    current = row["status"]
    mutation_version = row["updated_at"]
    if expected_updated_at is not None and expected_updated_at != mutation_version:
        raise TaskTransitionConflict(task_id, current, mutation_version)
    # completion_mode may be None on rows predating migration 063; treat as "pr" (strict default).
    try:
        completion_mode = row["completion_mode"] or "pr"
    except (IndexError, KeyError):
        completion_mode = "pr"

    if current == new_status:
        logger.debug("Task %s already in status %s (trigger=%s), no-op", task_id, new_status, trigger)
        return

    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {current} → {new_status} "
            f"(allowed: {sorted(allowed)}, trigger={trigger})"
        )

    # WIP guard: limit concurrent in_progress tasks per project.
    # doc/none completion_mode tasks count as 0.5 WIP slots.
    if new_status == "in_progress" and current == "approved":
        proj_cursor = await db.execute(
            "SELECT project FROM tasks WHERE id = ? AND workspace_id = ?",
            (task_id, workspace_id),
        )
        proj_row = await proj_cursor.fetchone()
        if proj_row and proj_row["project"]:
            project_slug = proj_row["project"]
            wip_cursor = await db.execute(
                "SELECT completion_mode FROM tasks "
                "WHERE project = ? AND workspace_id = ? AND status = 'in_progress' "
                "AND deleted_at IS NULL AND id != ?",
                (project_slug, workspace_id, task_id),
            )
            wip_rows = await wip_cursor.fetchall()
            wip_count = 0.0
            for wip_row in wip_rows:
                try:
                    cm = wip_row["completion_mode"] or "pr"
                except (IndexError, KeyError):
                    cm = "pr"
                wip_count += 0.5 if cm in ("doc", "none") else 1.0
            if wip_count >= settings.wip_max_in_progress:
                raise ValueError(
                    f"WIP limit reached: project '{project_slug}' already has "
                    f"{wip_count} in_progress tasks (limit: {settings.wip_max_in_progress}). "
                    f"Complete or fail existing tasks before starting new ones."
                )

    # Guard: block completed transition while a PR is still open.
    # Applies to tasks that actually have a PR lifecycle. Research/doc/none
    # tasks shouldn't have an open PR, but we keep the check defensive so a
    # stray draft PR attached to a research task still blocks completion.
    # Skipped for pr_merge trigger (PR is already set to 'merged' before this).
    if new_status == "completed" and trigger != "pr_merge":
        pr_cursor = await db.execute(
            "SELECT id FROM pull_requests"
            " WHERE task_id = ? AND workspace_id = ? "
            "AND status IN ('draft', 'open', 'merging') LIMIT 1",
            (task_id, workspace_id),
        )
        if await pr_cursor.fetchone():
            raise ValueError(
                f"Cannot transition task {task_id} to completed: PR is still open (trigger={trigger})"
            )

    # Guard: merged-PR requirement applies ONLY to completion_mode='pr'.
    # Research/plan/verify tasks (doc|none) bypass this guard and transition
    # freely, because they don't produce a PR as their deliverable.
    # Anti-zombie: pr_merge (direct), closed_by_sibling_pr (task A: PR body
    # parser auto-closes tasks bundled in PR body), handoff_written (task B:
    # post-index handoff marks preparation tasks done).
    if (
        new_status == "completed"
        and trigger not in ("pr_merge", "closed_by_sibling_pr", "handoff_written")
        and completion_mode == "pr"
    ):
        from core.api.routers.projects import _find_git_path
        # Get project slug for this task
        proj_cursor = await db.execute(
            "SELECT project FROM tasks WHERE id = ? AND workspace_id = ?",
            (task_id, workspace_id),
        )
        proj_row = await proj_cursor.fetchone()
        if proj_row and _find_git_path(proj_row["project"]):
            # Project has git repo — require merged PR
            merged_cursor = await db.execute(
                "SELECT id FROM pull_requests WHERE task_id = ? AND workspace_id = ? "
                "AND status = 'merged' LIMIT 1",
                (task_id, workspace_id),
            )
            if not await merged_cursor.fetchone():
                raise ValueError(
                    "Code/system projects with completion_mode='pr' require a merged PR. "
                    "Use the PR workflow, set trigger='pr_merge', or create the task with "
                    "completion_mode='doc' (research/plan) or 'none' (verify/diagnose)."
                )

    now = _next_mutation_version(mutation_version)
    try:
        mutation = await db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? "
            "AND workspace_id = ? AND status = ? AND updated_at = ?",
            (new_status, now, task_id, workspace_id, current, mutation_version),
        )
        if mutation.rowcount != 1:
            latest = await (
                await db.execute(
                    "SELECT status,updated_at FROM tasks WHERE id=? "
                    "AND workspace_id=? AND deleted_at IS NULL",
                    (task_id, workspace_id),
                )
            ).fetchone()
            raise TaskTransitionConflict(
                task_id,
                latest["status"] if latest else None,
                latest["updated_at"] if latest else None,
            )

        from core.api.services.audit import log_audit

        await log_audit(
            db,
            action=f"task.{new_status}",
            user=actor or f"system:{trigger}",
            resource_type="task",
            resource_id=task_id,
            details={
                "old_status": current,
                "new_status": new_status,
                "trigger": trigger,
                "expected_updated_at": mutation_version,
                "committed_updated_at": now,
            },
            workspace_id=workspace_id,
        )
        if auto_commit:
            await db.commit()
    except aiosqlite.OperationalError as exc:
        if auto_commit:
            await db.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            latest = await (
                await db.execute(
                    "SELECT status,updated_at FROM tasks WHERE id=? "
                    "AND workspace_id=? AND deleted_at IS NULL",
                    (task_id, workspace_id),
                )
            ).fetchone()
            raise TaskTransitionConflict(
                task_id,
                latest["status"] if latest else None,
                latest["updated_at"] if latest else None,
            ) from exc
        raise
    except Exception:
        if auto_commit:
            await db.rollback()
        raise
    logger.info(
        "Task %s: %s → %s (trigger=%s, completion_mode=%s)",
        task_id, current, new_status, trigger, completion_mode,
    )
