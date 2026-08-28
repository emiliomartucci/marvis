# v1.0.0 - 2026-07-03 - P1 F1: per-user notifications MCP tools (list + ack), tier0
"""MCP tools — per-user notification inbox: ``list_notifications`` + ``ack_notification``.

Both are scoped to the CALLING person's ``users.id`` (resolved via
``person_user_id`` on ``current_mcp_context()`` — NEVER ``LOCAL_CTX``, the anti-gotcha
the create_task MCP tool still trips). A static tenant Bearer / non-person agent has
no personal inbox → empty. A bearer-admin may pass ``user_id`` to triage another
user's inbox (admin-gated). Non-admin callers get the read-time visibility filter so
a revoked grant hides its stale notifications from both the list and the counter.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    current_visible_projects,
    dump,
    person_user_id,
    raise_mcp_error,
)
from core.api.use_cases import notifications as notifications_uc
from core.api.use_cases._context import CallerContext, require_role_ctx
from core.api.use_cases._errors import ServiceError


async def _resolve_scope(
    db, ctx: CallerContext, user_id_override: str | None
) -> tuple[str | None, set[str] | None]:
    """Return ``(effective_user_id, visible_projects)`` for the caller.

    ``user_id_override`` is an admin-only triage path (see another user's inbox,
    unfiltered). Otherwise the effective user is the caller's own person id and the
    visibility set comes from their grants (``None`` = admin/bearer, unrestricted).
    """
    if user_id_override:
        require_role_ctx(ctx, "admin", "super_admin")
        return user_id_override, None
    effective = person_user_id(ctx)
    visible = await current_visible_projects(db, ctx)
    return effective, visible


def register(mcp) -> None:
    """Register the notifications tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def list_notifications(
        status: Annotated[
            Literal["unread", "all"] | None,
            Field(default=None, description="Filter: 'unread' for the actionable set, else all"),
        ] = None,
        limit: Annotated[int, Field(default=20, ge=1, le=200)] = 20,
        user_id: Annotated[
            str | None,
            Field(default=None, description="Admin-only: triage another user's inbox by users.id"),
        ] = None,
    ) -> list[dict[str, Any]]:
        """List your notifications (commenti, findings/drift del brain, sistema), newest first.

        QUANDO USARLO: vedere cosa devi chiudere — un campo notices in session_brief/get_project/get_task ti dice che ce n'e'; questo tool le apre.
        QUANDO NON USARLO: NOT per notifiche di un altro utente (solo un admin puo' passare user_id). Il bearer statico del tenant non ha una inbox personale (vuoto).
        RESTITUISCE: array di {id, type, title, body, target_type, target_id, project, read_at, created_at, rollup_count}; rollup_count>1 = piu' eventi accorpati."""
        ctx = current_mcp_context()
        try:
            async with acquire_db() as db:
                effective, visible = await _resolve_scope(db, ctx, user_id)
                result = await notifications_uc.list_notifications(
                    ctx,
                    db,
                    effective_user_id=effective,
                    visible_projects=visible,
                    status=status,
                    limit=limit,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def ack_notification(
        notification_id: Annotated[
            str | None, Field(default=None, description="Ack a single notification by id")
        ] = None,
        target_id: Annotated[
            str | None,
            Field(default=None, description="Ack ALL your unread notifications for this target (e.g. a task id)"),
        ] = None,
        user_id: Annotated[
            str | None,
            Field(default=None, description="Admin-only: ack on behalf of another user by users.id"),
        ] = None,
    ) -> dict[str, Any]:
        """Segna come lette (dismiss) le tue notifiche, per id singolo o per target.

        QUANDO USARLO: dopo aver gestito un commento/finding — pulisce il contatore notices (che conta solo le non lette).
        QUANDO NON USARLO: NOT per notifiche altrui (solo admin via user_id). Passa ESATTAMENTE uno tra notification_id e target_id.
        RESTITUISCE: {acked: n} — quante righe non lette sono state segnate lette (0 se nessuna tua corrispondeva)."""
        ctx = current_mcp_context()
        try:
            async with acquire_write_db(label="mcp.ack_notification") as db:
                effective, _visible = await _resolve_scope(db, ctx, user_id)
                result = await notifications_uc.ack_notification(
                    ctx,
                    db,
                    effective_user_id=effective,
                    notification_id=notification_id,
                    target_id=target_id,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
