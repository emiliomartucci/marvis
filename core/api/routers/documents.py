# v1.1.0 - 2026-03-28 - Add batch-decay endpoint for REM agent nightly salience decay
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.api.db import get_db, get_vec_db, get_write_db
from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.services.salience_service import compute_decay

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SalienceUpdateRequest(BaseModel):
    salience: float


class DocumentResponse(BaseModel):
    id: int
    file_path: str
    project: str
    doc_type: str
    doc_title: str | None
    salience: float
    archived: int
    salience_updated_at: str | None
    workspace_id: str


class BoostResponse(BaseModel):
    salience: float
    previous: float


class ArchiveResponse(BaseModel):
    archived: bool


class BatchDecayRequest(BaseModel):
    dry_run: bool = False


class BatchDecayResponse(BaseModel):
    updated: int
    archived: int
    unchanged: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_documents(
    project: str | None = Query(None, description="Filter by project slug"),
    doc_type: str | None = Query(None, description="Filter by doc_type"),
    min_salience: float | None = Query(None, ge=0.0, le=1.0, description="Minimum salience"),
    archived: int = Query(0, ge=0, le=1, description="0=active only, 1=archived only"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[DocumentResponse]:
    """List documents with filters. Workspace-isolated."""
    ws = user.workspace_id or "ws_default"
    conditions: list[str] = ["COALESCE(workspace_id, 'ws_default') = ?"]
    params: list = [ws]

    conditions.append("archived = ?")
    params.append(archived)

    if project:
        conditions.append("project = ?")
        params.append(project)

    if doc_type:
        conditions.append("doc_type = ?")
        params.append(doc_type)

    if min_salience is not None:
        conditions.append("salience >= ?")
        params.append(min_salience)

    where = " AND ".join(conditions)
    query = (
        f"SELECT id, file_path, project, doc_type, doc_title, salience, archived, "
        f"salience_updated_at, workspace_id "
        f"FROM documents WHERE {where} "
        f"ORDER BY salience DESC, id DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [
        DocumentResponse(
            id=row["id"],
            file_path=row["file_path"],
            project=row["project"],
            doc_type=row["doc_type"],
            doc_title=row["doc_title"],
            salience=row["salience"] if row["salience"] is not None else 0.5,
            archived=row["archived"] if row["archived"] is not None else 0,
            salience_updated_at=row["salience_updated_at"],
            workspace_id=row["workspace_id"] or "ws_default",
        )
        for row in rows
    ]


@router.patch("/{doc_id}/salience")
async def update_salience(
    doc_id: int,
    body: SalienceUpdateRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> DocumentResponse:
    """Update a document's salience score directly. Workspace-isolated."""
    if body.salience < 0.0 or body.salience > 1.0:
        raise HTTPException(status_code=422, detail="salience must be between 0.0 and 1.0")

    ws = user.workspace_id or "ws_default"

    # Verify document exists and belongs to workspace
    cursor = await db.execute(
        "SELECT id FROM documents WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        [doc_id, ws],
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE documents SET salience = ?, salience_updated_at = ? WHERE id = ?",
        [body.salience, now, doc_id],
    )
    await db.commit()

    # Return updated doc
    cursor = await db.execute(
        "SELECT id, file_path, project, doc_type, doc_title, salience, archived, "
        "salience_updated_at, workspace_id FROM documents WHERE id = ?",
        [doc_id],
    )
    updated = await cursor.fetchone()
    return DocumentResponse(
        id=updated["id"],
        file_path=updated["file_path"],
        project=updated["project"],
        doc_type=updated["doc_type"],
        doc_title=updated["doc_title"],
        salience=updated["salience"] if updated["salience"] is not None else 0.5,
        archived=updated["archived"] if updated["archived"] is not None else 0,
        salience_updated_at=updated["salience_updated_at"],
        workspace_id=updated["workspace_id"] or "ws_default",
    )


@router.post("/{doc_id}/boost")
async def boost_document(
    doc_id: int,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> BoostResponse:
    """Increment salience by +0.15 (clamp to 1.0). Rate-limited: 1 boost per doc per caller per hour."""
    ws = user.workspace_id or "ws_default"
    caller_id = user.user_id or user.username

    # Verify document exists and belongs to workspace
    cursor = await db.execute(
        "SELECT id, salience FROM documents WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        [doc_id, ws],
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    old_salience = row["salience"] if row["salience"] is not None else 0.5

    # Rate limit check: 1 boost per doc per caller per hour
    rate_cursor = await db.execute(
        "SELECT 1 FROM boost_log WHERE doc_id = ? AND caller_id = ? AND boosted_at > datetime('now', '-1 hour')",
        [doc_id, caller_id],
    )
    if await rate_cursor.fetchone():
        raise HTTPException(status_code=429, detail="Boost rate limit: 1 per document per hour")

    # Apply boost
    new_salience = min(old_salience + 0.15, 1.0)
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        "INSERT INTO boost_log (doc_id, caller_id, boosted_at) VALUES (?, ?, ?)",
        [doc_id, caller_id, now],
    )
    await db.execute(
        "UPDATE documents SET salience = ?, salience_updated_at = ? WHERE id = ?",
        [new_salience, now, doc_id],
    )
    await db.commit()

    return BoostResponse(salience=round(new_salience, 4), previous=round(old_salience, 4))


@router.post("/{doc_id}/archive")
async def archive_document(
    doc_id: int,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
    vec_db: aiosqlite.Connection = Depends(get_vec_db),
) -> ArchiveResponse:
    """Archive a document: sets archived=1 and removes from vec_documents."""
    ws = user.workspace_id or "ws_default"

    # Verify document exists and belongs to workspace
    cursor = await db.execute(
        "SELECT id FROM documents WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        [doc_id, ws],
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    # Archive the document
    await db.execute("UPDATE documents SET archived = 1 WHERE id = ?", [doc_id])
    await db.commit()

    # Remove from vec_documents (vec-enabled connection)
    await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
    await vec_db.commit()

    return ArchiveResponse(archived=True)


@router.post("/batch-decay")
async def batch_decay(
    body: BatchDecayRequest = BatchDecayRequest(),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> BatchDecayResponse:
    """Apply Ebbinghaus decay to all non-archived documents. Used by REM agent nightly.

    Rate limited: max 1 call per 12 hours per caller (enforced via boost_log).
    """
    caller_id = user.user_id or user.username
    ws = user.workspace_id or "ws_default"

    # Rate limit: 1 batch-decay per 12 hours
    rate_cursor = await db.execute(
        "SELECT 1 FROM boost_log WHERE doc_id = -1 AND caller_id = ? AND boosted_at > datetime('now', '-12 hours')",
        [caller_id],
    )
    if await rate_cursor.fetchone():
        raise HTTPException(status_code=429, detail="batch-decay rate limit: max 1 per 12 hours")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Fetch all non-archived documents in workspace
    cursor = await db.execute(
        "SELECT id, salience, doc_type, salience_updated_at FROM documents "
        "WHERE archived = 0 AND COALESCE(workspace_id, 'ws_default') = ?",
        [ws],
    )
    rows = await cursor.fetchall()

    updates: list[tuple] = []
    unchanged = 0

    for row in rows:
        current = row["salience"] if row["salience"] is not None else 0.5
        doc_type = row["doc_type"] or "file"
        updated_at = row["salience_updated_at"]

        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at)
                days = (now - dt).total_seconds() / 86400
            except (ValueError, TypeError):
                days = 30.0
        else:
            days = 30.0

        new_salience = compute_decay(current, doc_type, days)

        if abs(new_salience - current) < 0.0001:
            unchanged += 1
        else:
            updates.append((new_salience, now_iso, row["id"]))

    if not body.dry_run and updates:
        await db.executemany(
            "UPDATE documents SET salience = ?, salience_updated_at = ? WHERE id = ?",
            updates,
        )
        # Log rate limit marker
        await db.execute(
            "INSERT INTO boost_log (doc_id, caller_id, boosted_at) VALUES (-1, ?, ?)",
            [caller_id, now_iso],
        )
        await db.commit()

    logger.info("batch-decay: %d updated, %d unchanged, dry_run=%s", len(updates), unchanged, body.dry_run)
    return BatchDecayResponse(updated=len(updates), archived=0, unchanged=unchanged)
