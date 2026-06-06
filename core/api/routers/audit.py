# v2.0.0 - 2026-05-27 - S1 F1.4: thin adapter over use_cases.audit
# v1.1.0 - 2026-04-01 - Allow operators to read check_learnings audit entries only
"""HTTP adapter for the audit-trail READ domain (S1 collapse-runtime).

This router is a thin transport adapter. All query + RBAC logic lives in
:mod:`core.api.use_cases.audit` (pure, fastapi-free). The single handler:

1. resolves identity into a :class:`CallerContext` (``from_user_info``; audit has
   no human-only gate, so ``is_human_session=False`` — no ``Request`` needed);
2. calls the use_case inside ``try/except ServiceError`` -> ``to_http``.

The audit-read RBAC decision raises :class:`AuthorizationError` in the use_case,
but the legacy HTTP contract pins a PLAIN-STRING 403 body
(``detail == "Insufficient permissions"``, asserted by
``tests/test_audit_permissions.py``). ``to_http`` would emit a structured
``{"code","message"}`` dict and break that contract, so this adapter translates
that one case to the legacy ``HTTPException(403, "Insufficient permissions")``
itself — the analogue of the costs template keeping its own boundary error shape.

SCOPE: this domain is read-only. The audit WRITE path
(``core.api.services.audit.log_audit``, called by other routers) is NOT touched —
moving it behind a use_case is a LATER phase (F4), out of scope here.

The response DTO ``AuditEntry`` is re-exported from the use_case so ``response_model=``
references the same class and any existing importer of
``core.api.routers.audit.AuditEntry`` keeps working.
"""
from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.db import get_db
from core.api.models import UserInfo
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.use_cases import audit as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import AuthorizationError, ServiceError

# Re-export the domain DTO (and the operator-allowlist constant) from the use_case
# so (a) `response_model=` below references the same class and (b) existing
# importers of `core.api.routers.audit.AuditEntry` keep working unchanged.
from core.api.use_cases.audit import (  # noqa: F401  (re-export surface)
    OPERATOR_ALLOWED_AUDIT_ACTIONS as _OPERATOR_ALLOWED_AUDIT_ACTIONS,
    AuditEntry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("", response_model=list[AuditEntry])
async def list_audit_entries(
    action: str | None = Query(None, description="Filter by action prefix (e.g. 'pr.merge')"),
    user: str | None = Query(None, description="Filter by username"),
    resource_type: str | None = Query(None, description="Filter by resource type"),
    resource_id: str | None = Query(None, description="Filter by resource ID"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[AuditEntry]:
    """Return recent audit log entries.

    Admins can query the full audit trail. Operators are intentionally limited to
    `check_learnings` entries so reflection agents can verify closed-loop learning
    usage without exposing broader privileged audit history.
    """
    ctx = CallerContext.from_user_info(current_user, is_human_session=False)
    try:
        return await uc.list_audit_entries(
            ctx,
            db,
            action=action,
            user=user,
            resource_type=resource_type,
            resource_id=resource_id,
            limit=limit,
            offset=offset,
        )
    except AuthorizationError:
        # Preserve the legacy plain-string 403 body (HTTP contract parity).
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    except ServiceError as e:
        raise to_http(e)
