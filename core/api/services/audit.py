# v1.0.0 - 2026-03-03 - Audit logging for privileged human operations
"""
Audit logging service.

Writes immutable records to the audit_log table for human-only privileged
operations: PR merge/revert, task approve/complete, delete operations, user
management actions.

Non-critical: failures are logged as warnings but never block the caller.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def log_audit(
    db: aiosqlite.Connection,
    action: str,
    user: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write one audit record to the audit_log table.

    Args:
        db:            Active aiosqlite connection (request-scoped or fresh).
        action:        Verb describing the privileged operation, e.g.
                       "pr.merge", "task.approve", "task.delete", "user.delete".
        user:          Username (or agent slug) performing the action.
        resource_type: Entity type: "pull_request", "task", "user", "raci", etc.
        resource_id:   Primary key of the affected resource.
        details:       Optional free-form dict serialised to JSON for extra context.

    Non-critical: any exception is caught and logged as a warning so the
    caller's business logic is never interrupted by an audit failure.
    """
    try:
        await db.execute(
            "INSERT INTO audit_log (action, user, resource_type, resource_id, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                action,
                user,
                resource_type,
                resource_id,
                json.dumps(details) if details is not None else None,
            ),
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("log_audit failed (non-critical): %s", exc)
