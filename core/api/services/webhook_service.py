# v1.2.0 - 2026-03-09 - Full PR merge/close loop with task state auto-transition
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import aiosqlite

from core.api.config import settings

logger = logging.getLogger(__name__)

# UUID-format branch pattern for Marvis task branches
TASK_BRANCH_RE = re.compile(
    r"^feat/task-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

# Task statuses considered "final" — no further transitions allowed
FINAL_STATUSES = {"completed", "rejected", "failed"}


def extract_task_id_from_branch(branch: str) -> str | None:
    """Extract task UUID from branch name if it matches feat/task-{uuid} pattern."""
    m = TASK_BRANCH_RE.match(branch)
    return m.group(1) if m else None


@asynccontextmanager
async def _open_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Writer connection for webhook background tasks."""
    from core.api.db import write_db
    async with write_db() as db:
        yield db


async def _log_event(
    db: aiosqlite.Connection,
    delivery_id: str,
    event_type: str,
    action: str | None,
    repo: str | None,
    pr_number: int | None,
    branch: str | None,
    task_id: str | None = None,
    pr_id: str | None = None,
    payload: dict | None = None,
    error: str | None = None,
) -> bool:
    """Insert webhook event log. Returns True if inserted (new), False if duplicate."""
    payload_json = json.dumps(payload) if payload is not None else None
    cursor = await db.execute(
        """INSERT OR IGNORE INTO webhook_events
           (delivery_id, event_type, action, repo, pr_number, branch,
            task_id, pr_id, payload, processed, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (delivery_id, event_type, action, repo, pr_number, branch,
         task_id, pr_id, payload_json, error),
    )
    await db.commit()
    if cursor.rowcount > 0:
        return True

    # A transition conflict is deliberately retained as unprocessed so the
    # original delivery can be retried after the competing task writer settles.
    # Reclaim only this explicit retryable state; a duplicate in-flight or
    # successfully processed delivery remains idempotently ignored.
    retry = await db.execute(
        "UPDATE webhook_events SET error = NULL WHERE delivery_id = ? "
        "AND processed = 0 AND error LIKE 'task_transition_conflict:%'",
        (delivery_id,),
    )
    await db.commit()
    return retry.rowcount > 0


async def _update_event_error(
    db: aiosqlite.Connection, delivery_id: str, error: str
) -> None:
    """Update webhook_events error field after processing failure."""
    await db.execute(
        "UPDATE webhook_events SET error = ? WHERE delivery_id = ?",
        (error, delivery_id),
    )
    await db.commit()


async def _update_event_processed(
    db: aiosqlite.Connection, delivery_id: str, task_id: str | None = None, pr_id: str | None = None
) -> None:
    """Mark webhook event as processed with optional task_id/pr_id linkage."""
    await db.execute(
        "UPDATE webhook_events SET processed = 1, task_id = COALESCE(?, task_id), pr_id = COALESCE(?, pr_id) WHERE delivery_id = ?",
        (task_id, pr_id, delivery_id),
    )
    await db.commit()


async def _send_telegram(text: str) -> None:
    """Best-effort Telegram notify via urllib. Errors only logged, never raised."""
    if not settings.telegram_bot_token or not settings.telegram_owner_chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        data = json.dumps({
            "chat_id": settings.telegram_owner_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # timeout=5s — fire-and-forget style (blocking call in background task is acceptable)
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        logger.warning("Telegram notify failed: %s", exc)


async def _find_task_by_id(db: aiosqlite.Connection, task_id: str) -> aiosqlite.Row | None:
    """Find task by ID."""
    cursor = await db.execute(
        "SELECT id, status, project, title, workspace_id FROM tasks "
        "WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    )
    return await cursor.fetchone()


async def _find_pr_by_branch(db: aiosqlite.Connection, branch: str) -> aiosqlite.Row | None:
    """Find active PR by branch name."""
    cursor = await db.execute(
        "SELECT * FROM pull_requests WHERE branch = ? AND status IN ('draft', 'open', 'merging') ORDER BY created_at DESC LIMIT 1",
        (branch,),
    )
    return await cursor.fetchone()


async def _resolve_unique_project_workspace(
    db: aiosqlite.Connection, project: str
) -> str | None:
    """Resolve a webhook project only when ownership identifies one workspace."""
    cursor = await db.execute(
        "SELECT DISTINCT workspace_id FROM ("
        "SELECT workspace_id FROM tasks WHERE project = ? AND deleted_at IS NULL "
        "UNION SELECT t.workspace_id FROM project_teams pt "
        "JOIN teams t ON t.id = pt.team_id WHERE pt.project = ?"
        ") WHERE workspace_id IS NOT NULL LIMIT 2",
        (project, project),
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        return None
    return rows[0]["workspace_id"]


async def handle_pr_opened(
    delivery_id: str, payload: dict, db: aiosqlite.Connection
) -> None:
    """Handle pull_request.opened event."""
    pr_data = payload.get("pull_request", {})
    branch = pr_data.get("head", {}).get("ref", "")
    repo = payload.get("repository", {}).get("full_name", "")
    pr_number = pr_data.get("number")
    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body", "") or ""
    pr_html_url = pr_data.get("html_url", "")
    now = datetime.now(timezone.utc).isoformat()

    # Idempotency: log event immediately, skip if duplicate delivery_id
    inserted = await _log_event(
        db, delivery_id, "pull_request", "opened", repo, pr_number, branch, payload=payload
    )
    if not inserted:
        logger.info("Webhook delivery_id %s already processed, skipping", delivery_id)
        return

    task_id = extract_task_id_from_branch(branch)

    if task_id:
        # Branch matches feat/task-{uuid} pattern
        task = await _find_task_by_id(db, task_id)

        if task is None:
            # Task not found in DB — orphan PR
            logger.warning("PR #%s on branch %s: task %s not found (orphan PR)", pr_number, branch, task_id)
            await _update_event_error(db, delivery_id, f"task_not_found:{task_id}")
            await _send_telegram(
                f"<b>PR Orfana</b>\n"
                f"PR <a href='{pr_html_url}'>#{pr_number}</a> su <code>{branch}</code>\n"
                f"Task <code>{task_id[:8]}</code> non trovata nel DB.\n"
                f"Repo: {repo}"
            )
            return

        if task["status"] in FINAL_STATUSES:
            # Task in final state — only log, no DB changes
            logger.info(
                "PR #%s for task %s ignored: task status is %s (final)",
                pr_number, task_id, task["status"],
            )
            await _update_event_processed(db, delivery_id, task_id=task_id)
            return

        # Task found and in non-final state — insert PR record + update task to review
        pr_id = str(uuid.uuid4())
        project = task["project"]

        try:
            await db.execute(
                """INSERT OR IGNORE INTO pull_requests
                   (id, task_id, project, branch, target, status, title, body,
                    created_at, workspace_id)
                   VALUES (?, ?, ?, ?, 'main', 'open', ?, ?, ?, ?)""",
                (
                    pr_id,
                    task_id,
                    project,
                    branch,
                    pr_title,
                    pr_body,
                    now,
                    task["workspace_id"],
                ),
            )
            await db.commit()

            # Only transition to review if not already in review
            if task["status"] != "review":
                from core.api.services.task_transitions import (
                    TaskTransitionConflict,
                    validate_and_transition_task,
                )
                try:
                    await validate_and_transition_task(
                        db,
                        task_id,
                        "review",
                        trigger="webhook_pr_opened",
                        workspace_id=task["workspace_id"],
                    )
                except TaskTransitionConflict as exc:
                    logger.warning(
                        "Task %s changed while processing PR-open delivery %s: %s",
                        task_id,
                        delivery_id,
                        exc,
                    )
                    await _update_event_error(
                        db, delivery_id, f"task_transition_conflict: {exc}"
                    )
                    return
                except ValueError as exc:
                    logger.warning("Could not transition task %s to review: %s", task_id, exc)

            await _update_event_processed(db, delivery_id, task_id=task_id, pr_id=pr_id)

        except Exception as exc:
            logger.exception("Error processing PR opened for task %s: %s", task_id, exc)
            await _update_event_error(db, delivery_id, str(exc))
            raise

        short_id = task_id[:8]
        await _send_telegram(
            f"<b>PR Collegata</b>\n"
            f"PR <a href='{pr_html_url}'>#{pr_number}</a> collegata a task <code>{short_id}</code>\n"
            f"Branch: <code>{branch}</code>\n"
            f"Repo: {repo}"
        )

    else:
        # Unknown branch — create retroactive task with source="github"
        source_ref = f"github:{repo}:{pr_number}"
        new_task_id = str(uuid.uuid4())

        # Extract project from repo name (last segment) as best guess
        project_guess = repo.split("/")[-1] if repo else "unknown"
        workspace_id = await _resolve_unique_project_workspace(db, project_guess)
        if workspace_id is None:
            message = (
                "Cannot create retroactive task: webhook project does not resolve "
                "to exactly one workspace"
            )
            logger.warning("%s (project=%s)", message, project_guess)
            await _update_event_error(db, delivery_id, message)
            return

        try:
            await db.execute(
                """INSERT OR IGNORE INTO tasks
                   (id, title, description, status, project, priority,
                    created_by, source, source_ref, created_at, updated_at, workspace_id)
                   VALUES (?, ?, ?, 'pending', ?, 'medium', 'github', 'github', ?, ?, ?, ?)""",
                (new_task_id, pr_title or f"PR #{pr_number} da GitHub",
                 pr_body or None, project_guess,
                 source_ref, now, now, workspace_id),
            )
            await db.commit()

            # Insert PR record linked to new task
            pr_id = str(uuid.uuid4())
            await db.execute(
                """INSERT INTO pull_requests
                   (id, task_id, project, branch, target, status, title, body,
                    created_at, workspace_id)
                   VALUES (?, ?, ?, ?, 'main', 'open', ?, ?, ?, ?)""",
                (
                    pr_id,
                    new_task_id,
                    project_guess,
                    branch,
                    pr_title,
                    pr_body,
                    now,
                    workspace_id,
                ),
            )
            await db.commit()

            await _update_event_processed(db, delivery_id, task_id=new_task_id, pr_id=pr_id)

        except Exception as exc:
            logger.exception("Error creating retroactive task for branch %s: %s", branch, exc)
            await _update_event_error(db, delivery_id, str(exc))
            raise

        short_id = new_task_id[:8]
        await _send_telegram(
            f"<b>Nuova PR Non Tracciata</b>\n"
            f"PR <a href='{pr_html_url}'>#{pr_number}</a> su branch <code>{branch}</code>\n"
            f"Task retroattiva creata: <code>{short_id}</code>\n"
            f"Repo: {repo}"
        )


async def handle_pr_merged(
    delivery_id: str, payload: dict, db: aiosqlite.Connection
) -> None:
    """Handle pull_request.closed with merged=true."""
    pr_data = payload.get("pull_request", {})
    branch = pr_data.get("head", {}).get("ref", "")
    repo = payload.get("repository", {}).get("full_name", "")
    pr_number = pr_data.get("number")
    pr_html_url = pr_data.get("html_url", "")

    # Find active PR in DB by branch
    pr_row = await _find_pr_by_branch(db, branch)
    if not pr_row:
        msg = f"No active PR in DB for branch {branch} (PR #{pr_number} merged on GitHub)"
        logger.warning(msg)
        await _update_event_error(db, delivery_id, msg)
        await _send_telegram(
            f"<b>PR Merged (non trovata in DB)</b>\n"
            f"PR <a href='{pr_html_url}'>#{pr_number}</a> mergiata su GitHub\n"
            f"Branch: <code>{branch}</code> — nessun record attivo in DB.\n"
            f"Repo: {repo}"
        )
        return

    task_id = pr_row["task_id"]
    workspace_id = pr_row["workspace_id"]
    if not workspace_id:
        await _update_event_error(db, delivery_id, "missing_pr_workspace")
        return

    try:
        from core.api.services.pr_service import merge_pr
        await merge_pr(task_id, db, workspace_id=workspace_id)
        await _update_event_processed(db, delivery_id, task_id=task_id, pr_id=pr_row["id"])
    except Exception as exc:
        logger.exception("merge_pr failed for task %s (branch %s): %s", task_id, branch, exc)
        await _update_event_error(db, delivery_id, str(exc))
        await _send_telegram(
            f"<b>PR Merge Fallito</b>\n"
            f"PR <a href='{pr_html_url}'>#{pr_number}</a> — errore nel merge Marvis\n"
            f"Task: <code>{task_id[:8]}</code>\n"
            f"Errore: {exc}\n"
            f"Repo: {repo}"
        )
        return

    await _send_telegram(
        f"<b>PR Merged</b>\n"
        f"PR <a href='{pr_html_url}'>#{pr_number}</a> mergiata con successo\n"
        f"Task <code>{task_id[:8]}</code> → completed\n"
        f"Repo: {repo}"
    )


async def handle_pr_closed(
    delivery_id: str, payload: dict, db: aiosqlite.Connection
) -> None:
    """Handle pull_request.closed with merged=false."""
    pr_data = payload.get("pull_request", {})
    branch = pr_data.get("head", {}).get("ref", "")
    repo = payload.get("repository", {}).get("full_name", "")
    pr_number = pr_data.get("number")
    pr_body = pr_data.get("body", "") or ""
    pr_html_url = pr_data.get("html_url", "")

    # Find active PR in DB by branch
    pr_row = await _find_pr_by_branch(db, branch)
    if not pr_row:
        msg = f"No active PR in DB for branch {branch} (PR #{pr_number} closed on GitHub)"
        logger.warning(msg)
        await _update_event_error(db, delivery_id, msg)
        await _send_telegram(
            f"<b>PR Chiusa (non trovata in DB)</b>\n"
            f"PR <a href='{pr_html_url}'>#{pr_number}</a> chiusa su GitHub\n"
            f"Branch: <code>{branch}</code> — nessun record attivo in DB.\n"
            f"Repo: {repo}"
        )
        return

    task_id = pr_row["task_id"]
    workspace_id = pr_row["workspace_id"]
    if not workspace_id:
        await _update_event_error(db, delivery_id, "missing_pr_workspace")
        return
    reason = pr_body or f"PR #{pr_number} chiusa senza merge"

    try:
        from core.api.services.pr_service import close_pr
        await close_pr(task_id, reason, db, workspace_id=workspace_id)
        await _update_event_processed(db, delivery_id, task_id=task_id, pr_id=pr_row["id"])
    except Exception as exc:
        logger.exception("close_pr failed for task %s (branch %s): %s", task_id, branch, exc)
        await _update_event_error(db, delivery_id, str(exc))
        await _send_telegram(
            f"<b>PR Close Fallito</b>\n"
            f"PR <a href='{pr_html_url}'>#{pr_number}</a> — errore nel close Marvis\n"
            f"Task: <code>{task_id[:8]}</code>\n"
            f"Errore: {exc}\n"
            f"Repo: {repo}"
        )
        return

    await _send_telegram(
        f"<b>PR Chiusa</b>\n"
        f"PR <a href='{pr_html_url}'>#{pr_number}</a> chiusa senza merge\n"
        f"Task <code>{task_id[:8]}</code> → in_progress\n"
        f"Repo: {repo}"
    )


async def process_webhook_event(
    event: str, delivery_id: str, payload: dict
) -> None:
    """Main dispatcher for GitHub webhook events. Opens its own DB connection."""
    action = payload.get("action")
    merged = payload.get("pull_request", {}).get("merged", False)

    logger.info(
        "Processing webhook event=%s action=%s delivery=%s merged=%s",
        event, action, delivery_id, merged,
    )

    async with _open_db() as db:
        try:
            if event == "pull_request" and action == "opened":
                await handle_pr_opened(delivery_id, payload, db)

            elif event == "pull_request" and action == "closed" and merged:
                # Ensure event is logged before dispatch
                await _log_event(
                    db, delivery_id, "pull_request", "merged",
                    payload.get("repository", {}).get("full_name"),
                    payload.get("pull_request", {}).get("number"),
                    payload.get("pull_request", {}).get("head", {}).get("ref"),
                    payload=payload,
                )
                await handle_pr_merged(delivery_id, payload, db)

            elif event == "pull_request" and action == "closed" and not merged:
                # Ensure event is logged before dispatch
                await _log_event(
                    db, delivery_id, "pull_request", "closed",
                    payload.get("repository", {}).get("full_name"),
                    payload.get("pull_request", {}).get("number"),
                    payload.get("pull_request", {}).get("head", {}).get("ref"),
                    payload=payload,
                )
                await handle_pr_closed(delivery_id, payload, db)

            else:
                logger.info(
                    "Unhandled webhook event=%s action=%s delivery=%s — ignoring",
                    event, action, delivery_id,
                )
                # Always persist unhandled events to webhook_events for audit trail
                repo = payload.get("repository", {}).get("full_name") if isinstance(payload.get("repository"), dict) else None
                await _log_event(
                    db, delivery_id, event, action,
                    repo, None, None,
                    payload=payload,
                )

        except Exception as exc:
            logger.exception(
                "Unhandled exception in process_webhook_event event=%s delivery=%s: %s",
                event, delivery_id, exc,
            )
