# Brain v1.2 — Direction REST endpoints (router module).
#
# Surface:
#   GET  /api/v1/brain/directions/{slug}              current + recent changelog
#   GET  /api/v1/brain/directions/{slug}/proposed     latest pending finding payload
#   POST /api/v1/brain/findings/{finding_id}/approve  atomic write-back
#   POST /api/v1/brain/findings/{finding_id}/reject   mark dismissed (terminal)
#   POST /api/v1/brain/findings/{finding_id}/edit     apply edited summary + oos
#   POST /api/v1/brain/directions/{slug}/manual       super_admin direct write
#
# RBAC:
#   GET endpoints              : viewer+ (read access)
#   POST approve/reject/edit   : super_admin only (mutates direction state)
#   POST manual                : super_admin only (bypasses finding workflow)
#
# Atomic write-back transaction (approve/edit/manual):
#   1) filesystem write via direction.write_direction_frontmatter (.bak backup)
#   2) DB cache UPSERT via direction.sync_db_cache
#   3) changelog append via direction.append_changelog
#   4) finding/drift_signal state update -> 'applied'
#
# WebSocket emit best-effort (eventual consistency on failure).
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from core.api.db import acquire_db, write_db
from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.services.brain import direction as direction_svc
from core.api.services import project_lifecycle
from core.api.use_cases._context import CallerContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/brain", tags=["brain-direction"])


# ---------------------------------------------------------------------------
# Response/Request models
# ---------------------------------------------------------------------------


class DirectionResponse(BaseModel):
    project_slug: str
    summary: str
    out_of_scope: str
    last_updated_at: str
    last_updated_by: str | None = None
    source_finding_id: str | None = None
    source_drift_signal: str | None = None
    schema_version: int = 1
    changelog: list[dict[str, Any]] = Field(default_factory=list)


class ProposedDirectionResponse(BaseModel):
    project_slug: str
    finding_id: str
    proposed_summary: str
    proposed_out_of_scope: str
    rationale: str | None = None
    confidence: str
    urgency_score: int
    current: dict[str, Any] | None = None


class EditDirectionRequest(BaseModel):
    edited_summary: str = Field(..., min_length=1, max_length=4000)
    edited_out_of_scope: str = Field(..., min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, max_length=1000)


class ManualDirectionRequest(BaseModel):
    summary: str = Field(..., min_length=1, max_length=4000)
    out_of_scope: str = Field(..., min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, max_length=1000)


class ApproveResponse(BaseModel):
    finding_id: str
    project_slug: str
    applied_at: str
    applied_by: str
    changelog_id: str
    direction: DirectionResponse


class RejectResponse(BaseModel):
    finding_id: str
    rejected_at: str
    rejected_by: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _load_finding(
    db: aiosqlite.Connection, finding_id: str
) -> dict[str, Any] | None:
    cur = await db.execute(
        "SELECT finding_id, finding_type, scope_type, scope_key, approval_state,"
        " entity_ref, proposed_payload_json, urgency_score, confidence,"
        " summary, why_now"
        " FROM brain_findings WHERE finding_id = ?",
        (finding_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return None
    cols = [
        "finding_id", "finding_type", "scope_type", "scope_key",
        "approval_state", "entity_ref", "proposed_payload_json",
        "urgency_score", "confidence", "summary", "why_now",
    ]
    return dict(zip(cols, row))


def _decode_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


async def _apply_write_back(
    *,
    slug: str,
    summary: str,
    out_of_scope: str,
    applied_by: str,
    change_type: str,
    source_finding_id: str | None,
    rationale: str | None,
    ctx: CallerContext,
) -> tuple[direction_svc.DirectionRecord, str]:
    """Run the atomic write-back transaction.

    Returns (DirectionRecord, changelog_id). Raises HTTPException(500) on
    filesystem or DB failure. Caller wraps in router.
    """
    # Load current direction (if any) to record old_summary in changelog.
    current = direction_svc.read_direction_frontmatter(slug)
    old_summary = current.summary if current else None
    old_oos = current.out_of_scope if current else None

    # Filesystem + DB cache/changelog share the project mutation lock. The
    # lifecycle event commits before context.md changes, so archive cannot race.
    projects_root = direction_svc._context_md_path(slug).parent.parent
    async with write_db() as db:
        async with project_lifecycle.guarded_project_file_write(
            ctx,
            db,
            project_slug=slug,
            writer_kind="brain_direction",
            resource_ref="context.md",
            projects_root=projects_root,
        ):
            try:
                record = direction_svc.write_direction_frontmatter(
                    slug,
                    summary,
                    out_of_scope,
                    applied_by=applied_by,
                    source_finding=source_finding_id,
                )
            except (ValueError, FileNotFoundError, OSError) as exc:
                logger.exception("direction.write_direction_frontmatter failed for %s", slug)
                raise HTTPException(status_code=500, detail=f"filesystem write failed: {exc}")
            try:
                await direction_svc.sync_db_cache(record, db=db)
                changelog_id = await direction_svc.append_changelog(
                    direction_svc.ChangelogEntry(
                        project_slug=slug,
                        change_type=change_type,
                        applied_at=record.last_updated_at,
                        applied_by=applied_by,
                        new_summary=summary,
                        new_out_of_scope=out_of_scope,
                        old_summary=old_summary,
                        old_out_of_scope=old_oos,
                        source_finding_id=source_finding_id,
                        rationale=rationale,
                    ),
                    db=db,
                )
            except Exception as exc:
                logger.exception("direction db sync failed for %s", slug)
                raise HTTPException(status_code=500, detail=f"db sync failed: {exc}")
    return record, changelog_id


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


@router.get("/directions/{slug}", response_model=DirectionResponse)
async def get_direction(
    slug: str = Path(..., min_length=1),
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> DirectionResponse:
    cache = await direction_svc.read_direction_db_cache(slug)
    if cache is None:
        # Fallback to filesystem read (in case cache not synced yet)
        fs = direction_svc.read_direction_frontmatter(slug)
        if fs is None:
            raise HTTPException(status_code=404, detail=f"direction not found for {slug}")
        cache = fs

    changelog = await direction_svc.list_changelog(slug, limit=25)
    return DirectionResponse(
        project_slug=cache.project_slug,
        summary=cache.summary,
        out_of_scope=cache.out_of_scope,
        last_updated_at=cache.last_updated_at,
        last_updated_by=cache.last_updated_by,
        source_finding_id=cache.source_finding_id,
        source_drift_signal=cache.source_drift_signal,
        schema_version=cache.schema_version,
        changelog=changelog,
    )


@router.get("/directions/{slug}/proposed", response_model=ProposedDirectionResponse)
async def get_proposed_direction(
    slug: str = Path(..., min_length=1),
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> ProposedDirectionResponse:
    """Latest pending direction_drift / direction_bootstrap finding for a slug."""
    async with acquire_db() as db:
        cur = await db.execute(
            "SELECT finding_id, finding_type, approval_state, urgency_score,"
            " confidence, proposed_payload_json, summary, why_now"
            " FROM brain_findings"
            " WHERE scope_type='project' AND scope_key=?"
            "   AND finding_type IN ('direction_drift', 'direction_bootstrap')"
            "   AND approval_state IN ('open', 'pending_bootstrap')"
            " ORDER BY urgency_score DESC, last_seen_cycle_key DESC"
            " LIMIT 1",
            (slug,),
        )
        row = await cur.fetchone()
        await cur.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no pending direction proposal for {slug}")
    payload = _decode_payload(row[5])
    proposed_summary = payload.get("proposed_summary") or payload.get("summary") or ""
    proposed_out_of_scope = (
        payload.get("proposed_out_of_scope") or payload.get("out_of_scope") or ""
    )
    current_record = await direction_svc.read_direction_db_cache(slug)
    current_dict = {
        "summary": current_record.summary if current_record else "",
        "out_of_scope": current_record.out_of_scope if current_record else "",
    } if current_record else None
    return ProposedDirectionResponse(
        project_slug=slug,
        finding_id=row[0],
        proposed_summary=str(proposed_summary),
        proposed_out_of_scope=str(proposed_out_of_scope),
        rationale=payload.get("rationale"),
        confidence=row[4],
        urgency_score=row[3],
        current=current_dict,
    )


# ---------------------------------------------------------------------------
# POST mutations (super_admin only)
# ---------------------------------------------------------------------------


@router.post(
    "/findings/{finding_id}/approve",
    response_model=ApproveResponse,
    status_code=200,
)
async def approve_direction_finding(
    finding_id: str = Path(..., min_length=1),
    user: UserInfo = Depends(require_role("super_admin")),
) -> ApproveResponse:
    """Atomically apply a direction finding: fs + db + changelog + state update."""
    async with acquire_db() as db:
        finding = await _load_finding(db, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"finding {finding_id} not found")
    if finding["finding_type"] not in ("direction_drift", "direction_bootstrap"):
        raise HTTPException(
            status_code=400,
            detail=f"finding {finding_id} is not a direction finding",
        )
    if finding["approval_state"] not in ("open", "pending_bootstrap"):
        raise HTTPException(
            status_code=409,
            detail=f"finding {finding_id} is in terminal state {finding['approval_state']}",
        )

    payload = _decode_payload(finding["proposed_payload_json"])
    new_summary = payload.get("proposed_summary") or payload.get("summary")
    new_oos = payload.get("proposed_out_of_scope") or payload.get("out_of_scope")
    if not new_summary or not new_oos:
        raise HTTPException(
            status_code=400,
            detail=f"finding {finding_id} payload missing summary/out_of_scope",
        )

    slug = finding["scope_key"]
    record, changelog_id = await _apply_write_back(
        slug=slug,
        summary=str(new_summary),
        out_of_scope=str(new_oos),
        applied_by=user.username,
        change_type=(
            "bootstrap"
            if finding["finding_type"] == "direction_bootstrap"
            else "direction_update"
        ),
        source_finding_id=finding_id,
        rationale=payload.get("rationale"),
        ctx=CallerContext.from_user_info(user, is_human_session=True),
    )

    # Mark finding as applied (+ state transition row)
    async with write_db() as db:
        await db.execute(
            "UPDATE brain_findings SET approval_state='applied',"
            " applied_at=?, applied_by_user_id=? WHERE finding_id=?",
            (record.last_updated_at, user.user_id, finding_id),
        )
        import uuid as _uuid
        await db.execute(
            "INSERT INTO brain_finding_states ("
            " state_id, finding_id, from_state, to_state, actor_user_id, reason"
            ") VALUES (?, ?, ?, 'applied', ?, ?)",
            (
                _uuid.uuid4().hex,
                finding_id,
                finding["approval_state"],
                user.user_id,
                f"direction approve via finding {finding_id}",
            ),
        )

    direction_resp = DirectionResponse(
        project_slug=record.project_slug,
        summary=record.summary,
        out_of_scope=record.out_of_scope,
        last_updated_at=record.last_updated_at,
        last_updated_by=record.last_updated_by,
        source_finding_id=record.source_finding_id,
        schema_version=record.schema_version,
        changelog=[],
    )
    return ApproveResponse(
        finding_id=finding_id,
        project_slug=slug,
        applied_at=record.last_updated_at,
        applied_by=user.username,
        changelog_id=changelog_id,
        direction=direction_resp,
    )


@router.post(
    "/findings/{finding_id}/reject",
    response_model=RejectResponse,
    status_code=200,
)
async def reject_direction_finding(
    finding_id: str = Path(..., min_length=1),
    user: UserInfo = Depends(require_role("super_admin")),
) -> RejectResponse:
    async with acquire_db() as db:
        finding = await _load_finding(db, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"finding {finding_id} not found")
    if finding["finding_type"] not in ("direction_drift", "direction_bootstrap"):
        raise HTTPException(
            status_code=400,
            detail=f"finding {finding_id} is not a direction finding",
        )
    if finding["approval_state"] not in ("open", "pending_bootstrap"):
        raise HTTPException(status_code=409, detail="finding is in terminal state")

    now = _utc_iso()
    async with write_db() as db:
        await db.execute(
            "UPDATE brain_findings SET approval_state='dismissed'"
            " WHERE finding_id=?",
            (finding_id,),
        )
        import uuid as _uuid
        await db.execute(
            "INSERT INTO brain_finding_states ("
            " state_id, finding_id, from_state, to_state, actor_user_id, reason"
            ") VALUES (?, ?, ?, 'dismissed', ?, ?)",
            (
                _uuid.uuid4().hex,
                finding_id,
                finding["approval_state"],
                user.user_id,
                "direction reject",
            ),
        )

    return RejectResponse(
        finding_id=finding_id,
        rejected_at=now,
        rejected_by=user.username,
    )


@router.post(
    "/findings/{finding_id}/edit",
    response_model=ApproveResponse,
    status_code=200,
)
async def edit_direction_finding(
    finding_id: str = Path(..., min_length=1),
    body: EditDirectionRequest = Body(...),
    user: UserInfo = Depends(require_role("super_admin")),
) -> ApproveResponse:
    """Apply an edited version of the proposed direction."""
    async with acquire_db() as db:
        finding = await _load_finding(db, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=f"finding {finding_id} not found")
    if finding["finding_type"] not in ("direction_drift", "direction_bootstrap"):
        raise HTTPException(status_code=400, detail="not a direction finding")
    if finding["approval_state"] not in ("open", "pending_bootstrap"):
        raise HTTPException(status_code=409, detail="finding is in terminal state")

    slug = finding["scope_key"]
    record, changelog_id = await _apply_write_back(
        slug=slug,
        summary=body.edited_summary,
        out_of_scope=body.edited_out_of_scope,
        applied_by=user.username,
        change_type=(
            "bootstrap"
            if finding["finding_type"] == "direction_bootstrap"
            else "direction_update"
        ),
        source_finding_id=finding_id,
        rationale=body.rationale or "edited via Console Triage",
        ctx=CallerContext.from_user_info(user, is_human_session=True),
    )

    async with write_db() as db:
        await db.execute(
            "UPDATE brain_findings SET approval_state='applied',"
            " applied_at=?, applied_by_user_id=? WHERE finding_id=?",
            (record.last_updated_at, user.user_id, finding_id),
        )
        import uuid as _uuid
        await db.execute(
            "INSERT INTO brain_finding_states ("
            " state_id, finding_id, from_state, to_state, actor_user_id, reason"
            ") VALUES (?, ?, ?, 'applied', ?, ?)",
            (
                _uuid.uuid4().hex,
                finding_id,
                finding["approval_state"],
                user.user_id,
                "direction edit",
            ),
        )

    return ApproveResponse(
        finding_id=finding_id,
        project_slug=slug,
        applied_at=record.last_updated_at,
        applied_by=user.username,
        changelog_id=changelog_id,
        direction=DirectionResponse(
            project_slug=record.project_slug,
            summary=record.summary,
            out_of_scope=record.out_of_scope,
            last_updated_at=record.last_updated_at,
            last_updated_by=record.last_updated_by,
            source_finding_id=record.source_finding_id,
            schema_version=record.schema_version,
            changelog=[],
        ),
    )


@router.post(
    "/directions/{slug}/manual",
    response_model=DirectionResponse,
    status_code=200,
)
async def manual_direction_write(
    slug: str = Path(..., min_length=1),
    body: ManualDirectionRequest = Body(...),
    user: UserInfo = Depends(require_role("super_admin")),
) -> DirectionResponse:
    """Bypass the finding workflow: super_admin writes a direction directly.

    Used for the marvisx manual bootstrap path (no LLM draft).
    """
    record, _ = await _apply_write_back(
        slug=slug,
        summary=body.summary,
        out_of_scope=body.out_of_scope,
        applied_by=user.username,
        change_type="manual_edit",
        source_finding_id=None,
        rationale=body.rationale or "manual direct write",
        ctx=CallerContext.from_user_info(user, is_human_session=True),
    )
    return DirectionResponse(
        project_slug=record.project_slug,
        summary=record.summary,
        out_of_scope=record.out_of_scope,
        last_updated_at=record.last_updated_at,
        last_updated_by=record.last_updated_by,
        source_finding_id=record.source_finding_id,
        schema_version=record.schema_version,
        changelog=[],
    )


__all__ = ["router"]
