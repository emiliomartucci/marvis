# v1.0.0 - 2026-05-27 - S1 F1.4: audit use_cases extracted from router
"""Audit use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Sibling of :mod:`core.api.use_cases.learnings` (the S1 "collapse runtime"
TEMPLATE). One pure async function per operation; the HTTP router becomes a thin
adapter that resolves identity into a :class:`CallerContext`, calls these
functions, and maps :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http``. The Python MCP surface (later) calls the SAME
functions with ``CallerContext.local_single_user()``. One implementation, no fork.

``audit`` is the audit-trail READ sink (read-only). Its single endpoint queries
``audit_log``. Two notes on how the template lands here:

SCOPE — the WRITE path stays put.
    Other routers write audit rows via ``core.api.services.audit.log_audit``
    (NOT defined in this domain). Moving that writer behind a use_case is a LATER
    phase (F4) and is explicitly OUT OF SCOPE here. This module only owns the
    read/query side, so it never imports nor touches ``log_audit``.

DECISION (RBAC) — access is action/resource-scoped, not a flat role level.
    Admins/super_admins read the full trail; operators are intentionally narrowed
    to ``check_learnings`` / ``docs_triage_bot`` actions on the ``learning``
    resource (so reflection agents can verify closed-loop learning usage without
    exposing broader privileged history). This is NOT expressible with
    ``require_role_ctx`` (which is a flat level check), so the decision lives in
    :func:`_authorize_audit_read` raising :class:`AuthorizationError`. The pure
    function is reusable by the MCP surface. The HTTP adapter, however, must
    preserve the exact legacy 403 body (a plain ``"Insufficient permissions"``
    string, pinned by ``tests/test_audit_permissions.py``), so it translates this
    one case itself rather than via ``to_http`` — the analogue of the costs
    template keeping its own transport-boundary error shape.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite
from pydantic import BaseModel

from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import AuthorizationError

# Actions an ``operator`` may read in the audit trail (prefix-matched, mirrors the
# legacy ``action.startswith(...)`` semantics).
OPERATOR_ALLOWED_AUDIT_ACTIONS = ("check_learnings", "docs_triage_bot")


# ---------------------------------------------------------------------------
# Domain DTOs (Pydantic is allowed in use_cases — only ``fastapi`` is forbidden)
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    id: str
    timestamp: str
    action: str
    user: str
    resource_type: str
    resource_id: str
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _authorize_audit_read(
    ctx: CallerContext,
    action: str | None,
    resource_type: str | None,
) -> None:
    """Authorize an audit-trail read (RBAC DECISION).

    Admins/super_admins see everything. Operators are narrowed to the
    ``OPERATOR_ALLOWED_AUDIT_ACTIONS`` prefixes on the ``learning`` resource.
    Anyone else is denied. Raises :class:`AuthorizationError` on denial — pure,
    so the MCP surface can reuse it. The HTTP adapter preserves the legacy
    plain-string 403 body for this specific case (see module docstring).
    """
    if ctx.system_role in ("admin", "super_admin"):
        return

    if ctx.system_role != "operator":
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )

    if not action or not action.startswith(OPERATOR_ALLOWED_AUDIT_ACTIONS):
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )

    if resource_type and resource_type != "learning":
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )


def _row_to_entry(row: aiosqlite.Row) -> AuditEntry:
    details = None
    if row["details_json"]:
        try:
            details = json.loads(row["details_json"])
        except (json.JSONDecodeError, TypeError):
            details = None
    return AuditEntry(
        id=row["id"],
        timestamp=row["timestamp"],
        action=row["action"],
        user=row["user"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        details=details,
    )


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def list_audit_entries(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    action: str | None = None,
    user: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditEntry]:
    """Return recent audit log entries, newest first.

    Admins query the full trail; operators are narrowed to ``check_learnings`` /
    ``docs_triage_bot`` entries (RBAC DECISION). Raises :class:`AuthorizationError`
    when the caller is not allowed to read the requested slice.
    """
    _authorize_audit_read(ctx, action, resource_type)
    workspace_id = require_workspace_ctx(ctx)

    conditions: list[str] = ["workspace_id = ?"]
    params: list[str | int] = [workspace_id]

    if action:
        conditions.append("action LIKE ?")
        params.append(f"{action}%")
    if user:
        conditions.append("user = ?")
        params.append(user)
    if resource_type:
        conditions.append("resource_type = ?")
        params.append(resource_type)
    if resource_id:
        conditions.append("resource_id = ?")
        params.append(resource_id)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.extend([limit, offset])

    cursor = await db.execute(
        f"SELECT id, timestamp, action, user, resource_type, resource_id, details_json "
        f"FROM audit_log WHERE {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params,
    )
    rows = await cursor.fetchall()
    return [_row_to_entry(row) for row in rows]
