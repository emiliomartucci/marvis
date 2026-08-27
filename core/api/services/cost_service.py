# api/services/cost_service.py
# v1.0.0 - 2026-02-28 - Cost entry service: atomic delta creation for agent + human entries

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import aiosqlite

logger = logging.getLogger(__name__)

# Fallback defaults when billing: block is absent in project.yaml
_BILLING_DEFAULTS: dict = {
    "human_cost_rate": 0.0,
    "human_bill_rate": 0.0,
    "agent_cost_rate": 0.0,
    "agent_bill_rate": 0.0,
    "token_markup_factor": 1.0,
}


def _get_billing_config(project_slug: str) -> dict:
    """
    Read billing: block from project.yaml via _read_project_yaml.
    Returns defaults when project not found or billing: block absent.
    NEVER raises exceptions.
    Call this BEFORE opening a transaction to avoid holding lock during disk I/O.
    """
    try:
        # Deferred import to avoid circular import (pattern from pr_service.py)
        from core.api.routers.projects import _find_project_entry, _read_project_yaml
        entry = _find_project_entry(project_slug)
        if entry is None:
            return _BILLING_DEFAULTS.copy()
        yaml_data = _read_project_yaml(entry.metadata_path)
        if yaml_data is None:
            return _BILLING_DEFAULTS.copy()
        billing = yaml_data.get("billing") or {}
        return {**_BILLING_DEFAULTS, **billing}
    except ImportError:
        logger.warning("billing config: import failed for %s", project_slug)
        return _BILLING_DEFAULTS.copy()
    except Exception as exc:
        logger.warning("billing config read failed for %s: %s", project_slug, exc)
        return _BILLING_DEFAULTS.copy()


async def _compute_agent_delta(
    db: aiosqlite.Connection,
    workspace_id: str,
    conversation_id: str,
    current_snapshot: float,
) -> float:
    """
    Compute cost delta for this conversation.
    current_snapshot = session_costs.cost_usd (current cumulative value).
    Delta = snapshot - sum(previous cost_usd_delta for this conversation in
    the same workspace. A conversation is consumed once across that
    workspace, even if more than one task refers to it.
    """
    cursor = await db.execute(
        """SELECT COALESCE(SUM(cost_usd_delta), 0.0) AS already_tracked
           FROM task_cost_entries tce
           JOIN tasks t ON t.id = tce.task_id
           WHERE t.workspace_id = ?
             AND tce.conversation_id = ?
             AND tce.entry_type = 'agent'""",
        (workspace_id, conversation_id),
    )
    row = await cursor.fetchone()
    already_tracked = float(row["already_tracked"]) if row else 0.0
    return max(0.0, current_snapshot - already_tracked)


async def _resolve_task_workspace(
    db: aiosqlite.Connection,
    task_id: str,
    project_slug: str,
) -> str | None:
    """Return the task's proven workspace, never a default/sentinel."""
    cursor = await db.execute(
        "SELECT workspace_id FROM tasks "
        "WHERE id = ? AND project = ? "
        "AND workspace_id IS NOT NULL AND length(trim(workspace_id)) > 0",
        (task_id, project_slug),
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return str(
        row["workspace_id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
    )


async def create_agent_entry(
    task_id: str,
    project_slug: str,
    source: Literal["task_completed"],
    created_by: str,
    db_path: str,  # NEVER pass db object — own connection for atomicity
    conversation_id: str | None = None,
    pr_id: str | None = None,
    agent_seconds: int = 0,
) -> dict:
    """
    Create agent cost entry with BEGIN IMMEDIATE for atomic delta.
    NEVER raises exceptions to caller (best-effort fire-and-forget).
    Returns dict with result.

    Note: _get_billing_config() is called before the transaction
    to avoid holding the write lock during disk I/O.
    """
    if not conversation_id:
        logger.info(
            "Skipping agent cost entry for task %s: no conversation_id (non-tmux or session not found)",
            task_id,
        )
        return {"skipped": True, "reason": "no conversation_id"}

    # Resolve billing config BEFORE opening transaction (todo #012 fix)
    billing = _get_billing_config(project_slug)

    try:
        from core.api.db import write_db
        async with write_db() as db:
            workspace_id = await _resolve_task_workspace(db, task_id, project_slug)
            if workspace_id is None:
                return {"skipped": True, "reason": "task workspace not found"}
            cursor = await db.execute(
                "SELECT cost_usd FROM session_costs "
                "WHERE workspace_id = ? AND conversation_id = ?",
                (workspace_id, conversation_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return {"skipped": True, "reason": "session cost not found"}
            current_snapshot = float(row["cost_usd"])

            delta = await _compute_agent_delta(
                db,
                workspace_id,
                conversation_id,
                current_snapshot,
            )
            if delta <= 0.001:
                return {"skipped": True, "reason": "delta too small", "delta": delta}

            total_cost_usd = (
                delta * billing["token_markup_factor"]
                + (agent_seconds / 3600.0) * billing["agent_cost_rate"]
            )
            total_bill_usd = (
                delta * billing["token_markup_factor"]
                + (agent_seconds / 3600.0) * billing["agent_bill_rate"]
            )

            entry_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()

            await db.execute(
                """INSERT OR IGNORE INTO task_cost_entries (
                    id, task_id, entry_type, source, conversation_id, pr_id,
                    cost_usd_delta, token_markup_factor,
                    agent_seconds, agent_cost_rate, agent_bill_rate,
                    total_cost_usd, total_bill_usd,
                    created_by, created_at
                ) VALUES (?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_id, task_id, source, conversation_id, pr_id,
                    round(delta, 8),
                    billing["token_markup_factor"],
                    agent_seconds,
                    billing["agent_cost_rate"], billing["agent_bill_rate"],
                    round(total_cost_usd, 6), round(total_bill_usd, 6),
                    created_by, now,
                ),
            )

            return {
                "entry_id": entry_id,
                "delta_usd": round(delta, 6),
                "total_cost_usd": round(total_cost_usd, 6),
                "total_bill_usd": round(total_bill_usd, 6),
            }

    except Exception as exc:
        logger.warning(
            "Non-fatal: agent cost entry failed for task %s, source %s: %s",
            task_id, source, exc,
        )
        return {"skipped": True, "reason": str(exc)}


async def create_human_entry(
    task_id: str,
    project_slug: str,
    human_minutes: float,
    created_by: str,
    db: aiosqlite.Connection,  # request-scoped connection OK for manual (synchronous)
    description: str | None = None,
    is_billable: bool = True,
    idempotency_key: str | None = None,
) -> dict:
    """
    Create manual human entry. Uses request-scoped connection (no race for manual entries).
    Idempotency via dedicated idempotency_key column with UNIQUE INDEX(task_id, idempotency_key).
    Commit is the caller's responsibility (router calls await db.commit()).
    """
    if idempotency_key:
        cursor = await db.execute(
            "SELECT id FROM task_cost_entries WHERE task_id = ? AND idempotency_key = ?",
            (task_id, idempotency_key),
        )
        existing = await cursor.fetchone()
        if existing:
            return {"skipped": True, "reason": "duplicate", "entry_id": existing["id"]}

    billing = _get_billing_config(project_slug)
    total_cost_usd = (human_minutes / 60.0) * billing["human_cost_rate"]
    total_bill_usd = (human_minutes / 60.0) * billing["human_bill_rate"]

    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO task_cost_entries (
            id, task_id, entry_type, source,
            human_minutes, human_cost_rate, human_bill_rate,
            total_cost_usd, total_bill_usd,
            is_billable, description, idempotency_key,
            created_by, created_at
        ) VALUES (?, ?, 'human', 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry_id, task_id,
            human_minutes, billing["human_cost_rate"], billing["human_bill_rate"],
            round(total_cost_usd, 6), round(total_bill_usd, 6),
            1 if is_billable else 0, description, idempotency_key,
            created_by, now,
        ),
    )
    # Note: caller (router) is responsible for db.commit()
    return {
        "entry_id": entry_id,
        "human_minutes": human_minutes,
        "total_cost_usd": round(total_cost_usd, 6),
        "total_bill_usd": round(total_bill_usd, 6),
    }


async def get_task_cost_summary(db: aiosqlite.Connection, task_id: str) -> dict:
    """Aggregate all entries for a task (all entries, not just billable)."""
    cursor = await db.execute(
        """SELECT
            COALESCE(SUM(total_cost_usd), 0.0) AS total_cost,
            COALESCE(SUM(total_bill_usd), 0.0) AS total_bill,
            COALESCE(SUM(CASE WHEN entry_type='agent' THEN total_cost_usd ELSE 0 END), 0.0) AS agent_cost,
            COALESCE(SUM(CASE WHEN entry_type='human' THEN total_cost_usd ELSE 0 END), 0.0) AS human_cost,
            COALESCE(SUM(CASE WHEN is_billable=1 THEN total_bill_usd ELSE 0 END), 0.0) AS billable,
            COALESCE(SUM(CASE WHEN is_billable=0 THEN total_cost_usd ELSE 0 END), 0.0) AS non_billable,
            COUNT(*) AS entry_count
           FROM task_cost_entries
           WHERE task_id = ?""",
        (task_id,),
    )
    row = await cursor.fetchone()
    return {
        "task_id": task_id,
        "total_cost_usd": round(row["total_cost"] or 0.0, 4),
        "total_bill_usd": round(row["total_bill"] or 0.0, 4),
        "agent_cost_usd": round(row["agent_cost"] or 0.0, 4),
        "human_cost_usd": round(row["human_cost"] or 0.0, 4),
        "billable_usd": round(row["billable"] or 0.0, 4),
        "non_billable_usd": round(row["non_billable"] or 0.0, 4),
        "entry_count": row["entry_count"] or 0,
    }
