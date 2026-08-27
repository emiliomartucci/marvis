"""Transaction-bound audit persistence for privileged operations.

The caller owns one transaction containing both the business mutation and the
required audit append. This module never starts, commits, rolls back, or swallows
an error; a failed append is therefore an operation failure.
"""
from __future__ import annotations

from typing import Any
import sqlite3

import aiosqlite

from core.api.services.audit_chain import (
    AuditAppendReceipt,
    append_audit_entry,
    append_audit_entry_sync,
)


async def log_audit(
    db: aiosqlite.Connection,
    action: str,
    user: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    *,
    workspace_id: str,
) -> AuditAppendReceipt:
    """Append one required audit entry inside the caller-owned transaction."""
    return await append_audit_entry(
        db,
        workspace_id=workspace_id,
        action=action,
        user=user,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )


def log_audit_sync(
    db: sqlite3.Connection,
    action: str,
    user: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    *,
    workspace_id: str,
) -> AuditAppendReceipt:
    """Append from a caller-owned synchronous SQLite transaction."""
    return append_audit_entry_sync(
        db,
        workspace_id=workspace_id,
        action=action,
        user=user,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )


__all__ = ["log_audit", "log_audit_sync"]
