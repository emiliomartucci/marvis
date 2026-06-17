from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import aiosqlite

ManualEdgeKind = Literal["related", "depends_on"]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$")


@dataclass(frozen=True)
class ManualProjectEdge:
    src_slug: str
    dst_slug: str
    kind: ManualEdgeKind
    provenance: str = "manual"


class ManualEdgeError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _validate_slug(slug: str, field: str) -> None:
    if not _SLUG_RE.fullmatch(slug):
        raise ManualEdgeError(422, f"{field} must be a valid project slug")


def _ensure_distinct(src_slug: str, dst_slug: str) -> None:
    if src_slug == dst_slug:
        raise ManualEdgeError(422, "src_slug and dst_slug must be different")


def _project_exists(slug: str) -> bool:
    from core.api.routers.projects import _find_project_entry

    return _find_project_entry(slug) is not None


def _validate_project_pair(src_slug: str, dst_slug: str) -> None:
    _validate_slug(src_slug, "src_slug")
    _validate_slug(dst_slug, "dst_slug")
    _ensure_distinct(src_slug, dst_slug)
    missing = [slug for slug in (src_slug, dst_slug) if not _project_exists(slug)]
    if missing:
        raise ManualEdgeError(404, f"Project not found: {', '.join(missing)}")


async def upsert_manual_project_edge(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    src_slug: str,
    dst_slug: str,
    kind: ManualEdgeKind,
    created_by: str | None,
) -> tuple[ManualProjectEdge, bool]:
    _validate_project_pair(src_slug, dst_slug)

    cur = await db.execute(
        "SELECT 1 FROM manual_project_edges "
        "WHERE workspace_id = ? AND src_slug = ? AND dst_slug = ? AND kind = ?",
        (workspace_id, src_slug, dst_slug, kind),
    )
    existed = await cur.fetchone() is not None

    await db.execute(
        "INSERT INTO manual_project_edges "
        "(workspace_id, src_slug, dst_slug, kind, provenance, created_by, updated_at) "
        "VALUES (?, ?, ?, ?, 'manual', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT(workspace_id, src_slug, dst_slug, kind) DO UPDATE SET "
        "updated_at = excluded.updated_at, created_by = excluded.created_by",
        (workspace_id, src_slug, dst_slug, kind, created_by),
    )
    await db.commit()
    return ManualProjectEdge(src_slug=src_slug, dst_slug=dst_slug, kind=kind), not existed


async def delete_manual_project_edge(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    src_slug: str,
    dst_slug: str,
    kind: ManualEdgeKind,
) -> tuple[ManualProjectEdge, bool]:
    _validate_project_pair(src_slug, dst_slug)

    cur = await db.execute(
        "DELETE FROM manual_project_edges "
        "WHERE workspace_id = ? AND src_slug = ? AND dst_slug = ? AND kind = ?",
        (workspace_id, src_slug, dst_slug, kind),
    )
    await db.commit()
    return ManualProjectEdge(src_slug=src_slug, dst_slug=dst_slug, kind=kind), cur.rowcount > 0


async def list_manual_project_edges(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
) -> list[ManualProjectEdge]:
    try:
        cur = await db.execute(
            "SELECT src_slug, dst_slug, kind FROM manual_project_edges "
            "WHERE workspace_id = ? ORDER BY src_slug, dst_slug, kind",
            (workspace_id,),
        )
        rows = await cur.fetchall()
    except aiosqlite.OperationalError as exc:
        if "no such table: manual_project_edges" in str(exc):
            return []
        raise
    return [
        ManualProjectEdge(
            src_slug=row["src_slug"],
            dst_slug=row["dst_slug"],
            kind=row["kind"],
        )
        for row in rows
    ]
