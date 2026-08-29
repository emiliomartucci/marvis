"""Owner-confidential per-file layer (RBAC F4, plan d33292f0).

``file_meta`` is the authoritative record: effective confidentiality is
``file_meta.confidential OR frontmatter`` — the frontmatter can only ADD
secrecy, so stripping the marker from the file body never declassifies.
Owners and ACL identities are canonical user_id values. Only persons own
files; agents/bearer never do. FastAPI-free like access_grants.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path, PurePosixPath

import aiosqlite

from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import ServiceError

logger = logging.getLogger(__name__)


class ConfidentialFileError(ServiceError):
    """Domain error for owner-confidential file operations."""


def canonical_file_key(path: str) -> tuple[str, str] | None:
    """Normalize any accepted logical path to (project_slug, rel_path).

    Accepts ``projects/<slug>/rel...`` and ``<slug>/rel...``; rel_path never
    includes the slug. Returns None for paths outside a project (repos/, root
    files) — those are git/system surfaces, out of the F4 perimeter (D5).
    """
    pure = PurePosixPath((path or "").strip().strip("/"))
    parts = pure.parts
    if not parts:
        return None
    if parts[0] == "projects":
        parts = parts[1:]
    if len(parts) < 2 or parts[0] == "repos":
        return None
    return parts[0], PurePosixPath(*parts[1:]).as_posix()


async def _has_column(
    db: aiosqlite.Connection, table: str, column: str
) -> bool:
    cur = await db.execute(f'PRAGMA table_info("{table}")')
    return column in {str(row[1]) for row in await cur.fetchall()}


async def _file_meta_workspace_enabled(db: aiosqlite.Connection) -> bool:
    return await _has_column(db, "file_meta", "workspace_id")


async def get_file_meta(
    db: aiosqlite.Connection,
    path: str,
    *,
    workspace_id: str | None = None,
) -> dict | None:
    key = canonical_file_key(path)
    if key is None:
        return None
    try:
        if await _file_meta_workspace_enabled(db):
            workspace = (workspace_id or "").strip()
            if not workspace:
                return None
            cur = await db.execute(
                "SELECT id, project_slug, rel_path, owner_user_id, confidential, "
                "workspace_id FROM file_meta WHERE workspace_id = ? "
                "AND project_slug = ? AND rel_path = ? LIMIT 1",
                (workspace, *key),
            )
        else:
            cur = await db.execute(
                "SELECT id, project_slug, rel_path, owner_user_id, confidential "
                "FROM file_meta WHERE project_slug = ? AND rel_path = ? LIMIT 1",
                key,
            )
        row = await cur.fetchone()
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():  # pre-162 compatibility
            return None
        raise
    if row is None:
        return None
    return {
        "id": str(row[0]), "project_slug": str(row[1]), "rel_path": str(row[2]),
        "owner_user_id": row[3], "confidential": bool(row[4]),
        "workspace_id": row[5] if len(row) > 5 else None,
    }


async def db_confidential(
    db: aiosqlite.Connection,
    path: str,
    *,
    workspace_id: str | None = None,
) -> bool:
    meta = await get_file_meta(db, path, workspace_id=workspace_id)
    return bool(meta and meta["confidential"])


async def acl_identities(db: aiosqlite.Connection, file_id: str) -> set[str]:
    cur = await db.execute("SELECT identity FROM file_acl WHERE file_id = ?", (file_id,))
    return {str(row[0]) for row in await cur.fetchall()}


async def actor_cleared_for_file(
    db: aiosqlite.Connection, actor: CallerContext, meta: dict
) -> bool:
    """Owner + explicit ACL only. Admin bypass is decided by the CALLER
    (break-glass logging lives in the read predicate, not here)."""
    if not meta["confidential"]:
        return True
    workspace_id = require_workspace_ctx(actor)
    if meta.get("workspace_id") and meta["workspace_id"] != workspace_id:
        return False
    actor_id = (actor.user_id or "").strip()
    if actor_id and meta.get("owner_user_id") and actor_id == meta["owner_user_id"]:
        return True
    if actor_id and actor_id in await acl_identities(db, meta["id"]):
        return True
    return False


def _is_person(actor: CallerContext) -> bool:
    return (
        actor.user_type == "human"
        and bool(actor.user_id)
        and actor.user_id != "local"
        and not actor.user_id.startswith("tenant:")
    )


async def capture_owner(db: aiosqlite.Connection, actor: CallerContext, path: str) -> None:
    """Record first-writer ownership (write chokepoints call this after a
    successful write). Persons only; never overwrites an existing owner.
    Best-effort: an ownership miss must never fail the write itself."""
    if not _is_person(actor):
        return
    workspace_id = require_workspace_ctx(actor)
    key = canonical_file_key(path)
    if key is None:
        return
    try:
        if await _file_meta_workspace_enabled(db):
            await db.execute(
                """
                INSERT INTO file_meta (
                    id, project_slug, rel_path, owner_user_id, workspace_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT DO UPDATE SET
                    owner_user_id = COALESCE(
                        file_meta.owner_user_id, excluded.owner_user_id
                    ),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE file_meta.workspace_id = excluded.workspace_id
                """,
                (str(uuid.uuid4()), key[0], key[1], actor.user_id, workspace_id),
            )
        else:
            await db.execute(
                """
                INSERT INTO file_meta (id, project_slug, rel_path, owner_user_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_slug, rel_path) DO UPDATE SET
                    owner_user_id = COALESCE(
                        file_meta.owner_user_id, excluded.owner_user_id
                    ),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                """,
                (str(uuid.uuid4()), key[0], key[1], actor.user_id),
            )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture_owner failed for %s (non-critical): %s", path, exc)


async def _audit(db: aiosqlite.Connection, *, action: str, actor: CallerContext, path: str, details: dict | None = None) -> None:
    from core.api.services.audit import log_audit

    await log_audit(
        db,
        action=f"confidential.{action}",
        user=actor.user_id or actor.username,
        resource_type="file",
        resource_id=path,
        details=details or {},
        workspace_id=require_workspace_ctx(actor),
    )


async def _audit_external_failure(
    db: aiosqlite.Connection,
    *,
    action: str,
    actor: CallerContext,
    path: str,
    error: Exception,
) -> None:
    if db.in_transaction:
        await db.rollback()
    await db.execute("BEGIN IMMEDIATE")
    try:
        await _audit(
            db,
            action=f"{action}.failed",
            actor=actor,
            path=path,
            details={"stage": "failed", "failure_type": type(error).__name__},
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


def _resolve_fs_path(project: str, rel_path: str) -> Path:
    root = Path(os.environ.get("MARVIS_PROJECTS_ROOT", "/data/projects").strip() or "/data/projects").expanduser()
    target = (root / project / rel_path).resolve()
    target.relative_to(root.resolve())  # ValueError on escape
    return target


async def _require_project_write(
    db: aiosqlite.Connection,
    actor: CallerContext,
    *,
    project: str,
    path: str,
) -> None:
    from core.api.services.access_grants import can_write_project

    if not await can_write_project(db, actor, project, path=path):
        raise ConfidentialFileError(code="not_found", message="Not found")


_CLEARANCE_LINE_RE = re.compile(r"^\s*clearance\s*:.*$", re.M)


def _write_frontmatter_confidential(target: Path, *, confidential: bool) -> None:
    """Atomic frontmatter flip (os.replace): instant hide on live read paths."""
    text = target.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---\n"):
        head, sep, body = text[4:].partition("\n---\n")
        head = _CLEARANCE_LINE_RE.sub("", head).strip("\n")
        if confidential:
            head = (head + "\n" if head else "") + "clearance: confidential"
        new_text = f"---\n{head}\n---\n{body if sep else text}"
    else:
        new_text = (f"---\nclearance: confidential\n---\n{text}") if confidential else text
    tmp = target.with_name(target.name + ".confidential.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)


async def _purge_index_rows(
    db: aiosqlite.Connection,
    fs_path: Path,
    project: str,
    rel_path: str,
    workspace_id: str,
) -> dict:
    """Inside the caller's writer tx: drop searchable derivatives, keep the
    documents row (confidential=1, content_hash nulled), tag graph_nodes.

    ORDER MATTERS: the migration-136 ``documents_fts_update`` trigger RE-INSERTS
    an fts row on every UPDATE of documents (rowid=id, content=file_path). So
    the documents UPDATE must run FIRST and the fts/vec/chunks DELETE LAST —
    otherwise the trigger resurrects the row we just purged.
    """
    purged: dict[str, int] = {}
    candidates = {str(fs_path), f"{project}/{rel_path}", f"projects/{project}/{rel_path}"}
    placeholders = ",".join("?" for _ in candidates)
    documents_workspace = await _has_column(db, "documents", "workspace_id")
    workspace_clause = (
        " AND workspace_id = ?"
        if documents_workspace
        else ""
    )
    document_params = [*candidates]
    if documents_workspace:
        document_params.append(workspace_id)
    cur = await db.execute(
        f"SELECT id FROM documents WHERE file_path IN ({placeholders})"
        f"{workspace_clause}",
        document_params,
    )
    doc_ids = [int(row[0]) for row in await cur.fetchall()]
    # 1. documents UPDATE first (fires the fts_update trigger)
    for doc_id in doc_ids:
        await db.execute(
            "UPDATE documents SET confidential = 1, content_hash = NULL WHERE id = ?", (doc_id,)
        )
    purged["documents"] = len(doc_ids)

    cur = await db.execute(
        "SELECT id, metadata FROM graph_nodes WHERE file_path IN "
        f"({placeholders}) AND deprecated_at IS NULL",
        list(candidates),
    )
    node_rows = await cur.fetchall()
    for node_id, metadata in node_rows:
        try:
            data = json.loads(metadata) if metadata else {}
        except (TypeError, ValueError):
            data = {}
        data["clearance"] = "confidential"
        await db.execute(
            "UPDATE graph_nodes SET metadata = ? WHERE id = ?", (json.dumps(data), node_id)
        )
    purged["graph_nodes"] = len(node_rows)

    # 2. derivatives DELETE LAST — nothing writes documents after this, so the
    # trigger cannot resurrect the fts row. Delete by the doc_id COLUMN (an fts
    # row may carry an auto rowid ≠ doc_id).
    for doc_id in doc_ids:
        await db.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
        try:
            await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", (doc_id,))
        except Exception as exc:  # noqa: BLE001 — vec0 unavailable: residual vectors are read-time filtered
            logger.warning("vec_documents purge skipped for doc %s: %s", doc_id, exc)
        await db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        # Reinforcement ledger cascade (mig 174, plan Fase 2 mielinizzazione
        # R1): a purged confidential doc must leave no boost rows behind —
        # they are a searchable-relevance derivative like fts/vec/chunks.
        # Plain table, no triggers → safe anywhere after the documents UPDATE.
        # Fail-soft ONLY on a pre-mig-174 schema (no such table); every other
        # error still propagates.
        try:
            await db.execute("DELETE FROM salience_boosts WHERE doc_id = ?", (doc_id,))
        except aiosqlite.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            logger.debug("salience_boosts cascade skipped for doc %s: %s", doc_id, exc)
    return purged


async def mark_confidential(
    db: aiosqlite.Connection,
    actor: CallerContext,
    *,
    path: str,
) -> dict:
    """Serialize a confidentiality flip with project archive."""
    key = canonical_file_key(path)
    if key is None:
        raise ConfidentialFileError(
            code="invalid_path",
            message="path is not inside a project",
        )
    from core.api.services import project_lifecycle

    projects_root = Path(
        os.environ.get("MARVIS_PROJECTS_ROOT", "/data/projects").strip()
        or "/data/projects"
    ).expanduser().resolve()
    async with project_lifecycle.async_project_mutation_guard(
        projects_root=projects_root
    ):
        await project_lifecycle.record_project_write(
            db,
            workspace_id=require_workspace_ctx(actor),
            project_slug=key[0],
            writer_kind="confidential_mark",
            actor=actor.user_id or actor.username,
            resource_ref=key[1],
            projects_root=projects_root,
        )
        await db.commit()
        return await _mark_confidential_locked(db, actor, path=path)


async def _mark_confidential_locked(
    db: aiosqlite.Connection,
    actor: CallerContext,
    *,
    path: str,
) -> dict:
    """Owner-only mark. A file without an owner can be claimed by the person
    marking it (pre-F4 files); agents/bearer never mark. Fail-closed order:
    frontmatter first (instant hide), then ONE writer tx for meta + purge."""
    if not _is_person(actor):
        raise ConfidentialFileError(code="persons_only", message="only a person can mark a file confidential")
    key = canonical_file_key(path)
    if key is None:
        raise ConfidentialFileError(code="invalid_path", message="path is not inside a project")
    project, rel_path = key
    workspace_id = require_workspace_ctx(actor)
    await _require_project_write(db, actor, project=project, path=path)
    try:
        fs_path = _resolve_fs_path(project, rel_path)
    except (OSError, ValueError) as exc:
        raise ConfidentialFileError(code="invalid_path", message=f"cannot resolve path: {exc}")
    if not fs_path.is_file():
        raise ConfidentialFileError(code="file_not_found", message="file not found")

    meta = await get_file_meta(db, path, workspace_id=workspace_id)
    if meta and meta.get("owner_user_id") and meta["owner_user_id"] != actor.user_id:
        raise ConfidentialFileError(code="not_owner", message="only the file owner can change its confidentiality")
    if meta and meta["confidential"]:
        return {"path": path, "confidential": True, "already": True, "owner": meta["owner_user_id"]}

    # (i) ARM the flags and COMMIT first: file_meta hides live reads instantly
    # (file_readable consults it), and documents.confidential=1 arms the embed
    # guard so any reindex racing the frontmatter write below is skipped — the
    # file-change event fired by that write must never re-index a marked file.
    file_id = meta["id"] if meta else str(uuid.uuid4())
    if await _file_meta_workspace_enabled(db):
        cursor = await db.execute(
            """
            INSERT INTO file_meta (
                id, project_slug, rel_path, owner_user_id, confidential, workspace_id
            ) VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT DO UPDATE SET
                confidential = 1,
                owner_user_id = COALESCE(
                    file_meta.owner_user_id, excluded.owner_user_id
                ),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE file_meta.workspace_id = excluded.workspace_id
            """,
            (file_id, project, rel_path, actor.user_id, workspace_id),
        )
        if cursor.rowcount != 1:
            raise ConfidentialFileError(code="not_found", message="Not found")
    else:
        await db.execute(
            """
            INSERT INTO file_meta (
                id, project_slug, rel_path, owner_user_id, confidential
            ) VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(project_slug, rel_path) DO UPDATE SET
                confidential = 1,
                owner_user_id = COALESCE(
                    file_meta.owner_user_id, excluded.owner_user_id
                ),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (file_id, project, rel_path, actor.user_id),
        )
    documents_workspace = await _has_column(db, "documents", "workspace_id")
    document_clause = (
        " AND workspace_id = ?"
        if documents_workspace
        else ""
    )
    document_params = [
        str(fs_path),
        f"{project}/{rel_path}",
        f"projects/{project}/{rel_path}",
    ]
    if documents_workspace:
        document_params.append(workspace_id)
    await db.execute(
        "UPDATE documents SET confidential = 1 WHERE file_path IN (?, ?, ?)"
        + document_clause,
        document_params,
    )
    await _audit(
        db,
        action="mark.intent",
        actor=actor,
        path=path,
        details={"stage": "intent"},
    )
    await db.commit()

    try:
        # (ii) frontmatter marker — os.replace is atomic; a reindex triggered here
        # now sees the committed confidential flag and skips.
        _write_frontmatter_confidential(fs_path, confidential=True)

        # (iii) purge the existing derivatives + nul the content_hash in one tx.
        purged = await _purge_index_rows(
            db, fs_path, project, rel_path, workspace_id
        )
        await _audit(
            db,
            action="mark",
            actor=actor,
            path=path,
            details={"stage": "confirmed", **purged},
        )
        await db.commit()
    except Exception as exc:
        await _audit_external_failure(
            db, action="mark", actor=actor, path=path, error=exc
        )
        raise
    refreshed = await get_file_meta(db, path, workspace_id=workspace_id)
    return {"path": path, "confidential": True, "owner": refreshed["owner_user_id"] if refreshed else actor.user_id, "purged": purged}


async def unmark_confidential(
    db: aiosqlite.Connection,
    actor: CallerContext,
    *,
    path: str,
) -> dict:
    """Serialize a declassification flip with project archive."""
    key = canonical_file_key(path)
    if key is None:
        raise ConfidentialFileError(
            code="invalid_path",
            message="path is not inside a project",
        )
    from core.api.services import project_lifecycle

    projects_root = Path(
        os.environ.get("MARVIS_PROJECTS_ROOT", "/data/projects").strip()
        or "/data/projects"
    ).expanduser().resolve()
    async with project_lifecycle.async_project_mutation_guard(
        projects_root=projects_root
    ):
        await project_lifecycle.record_project_write(
            db,
            workspace_id=require_workspace_ctx(actor),
            project_slug=key[0],
            writer_kind="confidential_unmark",
            actor=actor.user_id or actor.username,
            resource_ref=key[1],
            projects_root=projects_root,
        )
        await db.commit()
        return await _unmark_confidential_locked(db, actor, path=path)


async def _unmark_confidential_locked(
    db: aiosqlite.Connection,
    actor: CallerContext,
    *,
    path: str,
) -> dict:
    """Owner-only declassify: flips DB flag + frontmatter; the file re-enters
    the index on the next reindex of its path."""
    workspace_id = require_workspace_ctx(actor)
    key = canonical_file_key(path)
    if key is None:
        raise ConfidentialFileError(code="invalid_path", message="path is not inside a project")
    await _require_project_write(db, actor, project=key[0], path=path)
    meta = await get_file_meta(db, path, workspace_id=workspace_id)
    if meta is None or not meta["confidential"]:
        return {"path": path, "confidential": False, "already": True}
    if not _is_person(actor) or (meta.get("owner_user_id") and meta["owner_user_id"] != actor.user_id):
        raise ConfidentialFileError(code="not_owner", message="only the file owner can change its confidentiality")
    project, rel_path = meta["project_slug"], meta["rel_path"]
    fs_path = _resolve_fs_path(project, rel_path)
    await db.execute("BEGIN IMMEDIATE")
    await _audit(
        db,
        action="unmark.intent",
        actor=actor,
        path=path,
        details={"stage": "intent"},
    )
    await db.commit()
    try:
        if fs_path.is_file():
            _write_frontmatter_confidential(fs_path, confidential=False)
        await db.execute(
            "UPDATE file_meta SET confidential = 0, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?"
            + (" AND workspace_id = ?" if meta.get("workspace_id") else ""),
            (meta["id"], workspace_id)
            if meta.get("workspace_id")
            else (meta["id"],),
        )
        documents_workspace = await _has_column(db, "documents", "workspace_id")
        document_clause = (
            " AND workspace_id = ?"
            if documents_workspace
            else ""
        )
        document_params = [
            str(fs_path),
            f"{project}/{rel_path}",
            f"projects/{project}/{rel_path}",
        ]
        if documents_workspace:
            document_params.append(workspace_id)
        await db.execute(
            "UPDATE documents SET confidential = 0 WHERE file_path IN (?, ?, ?)"
            + document_clause,
            document_params,
        )
        await _audit(
            db,
            action="unmark",
            actor=actor,
            path=path,
            details={"stage": "confirmed"},
        )
        await db.commit()
    except Exception as exc:
        await _audit_external_failure(
            db, action="unmark", actor=actor, path=path, error=exc
        )
        raise
    return {"path": path, "confidential": False}


async def share_file_acl(
    db: aiosqlite.Connection, actor: CallerContext, *, path: str, identity: str,
) -> dict:
    workspace_id = require_workspace_ctx(actor)
    project_key = canonical_file_key(path)
    if project_key is None:
        raise ConfidentialFileError(
            code="invalid_path", message="path is not inside a project"
        )
    await _require_project_write(
        db, actor, project=project_key[0], path=path
    )
    meta = await get_file_meta(db, path, workspace_id=workspace_id)
    if meta is None or not meta["confidential"]:
        raise ConfidentialFileError(code="not_confidential", message="file is not confidential — grants/teams already govern it")
    if not _is_person(actor) or meta.get("owner_user_id") != actor.user_id:
        raise ConfidentialFileError(code="not_owner", message="only the file owner can share it")
    target = (identity or "").strip()
    users_workspace = await _has_column(db, "users", "workspace_id")
    workspace_clause = " AND workspace_id = ?" if users_workspace else ""
    params = [target, target, target]
    if users_workspace:
        params.append(workspace_id)
    cur = await db.execute(
        "SELECT id FROM users WHERE deleted_at IS NULL "
        "AND (id = ? OR slug = ? OR email = ?)"
        + workspace_clause
        + " LIMIT 1",
        params,
    )
    row = await cur.fetchone()
    if row is None:
        raise ConfidentialFileError(code="identity_not_found", message="identity is not a tenant member")
    canonical = str(row[0])
    await db.execute(
        "INSERT OR IGNORE INTO file_acl (file_id, identity, granted_by) VALUES (?, ?, ?)",
        (meta["id"], canonical, actor.user_id),
    )
    await _audit(db, action="share", actor=actor, path=path, details={"identity": canonical})
    await db.commit()
    result = {"path": path, "owner": meta["owner_user_id"], "viewers": sorted(await acl_identities(db, meta["id"]))}
    # warning: an ACL does not confer project visibility by itself
    from core.api.services.access_grants import load_grants

    viewer_ctx = CallerContext(
        username=canonical,
        user_id=canonical,
        system_role="viewer",
        user_type="human",
        workspace_id=workspace_id,
    )
    grants = await load_grants(db, viewer_ctx)
    if meta["project_slug"] not in grants:
        result["warning"] = (
            f"{canonical} has no grant on project {meta['project_slug']} — the ACL alone "
            "does not make the file reachable; grant project access too"
        )
    return result


async def unshare_file_acl(
    db: aiosqlite.Connection, actor: CallerContext, *, path: str, identity: str,
) -> dict:
    workspace_id = require_workspace_ctx(actor)
    project_key = canonical_file_key(path)
    if project_key is None:
        raise ConfidentialFileError(
            code="invalid_path", message="path is not inside a project"
        )
    await _require_project_write(
        db, actor, project=project_key[0], path=path
    )
    meta = await get_file_meta(db, path, workspace_id=workspace_id)
    if meta is None:
        raise ConfidentialFileError(code="file_not_found", message="no confidential record for this path")
    if not _is_person(actor) or meta.get("owner_user_id") != actor.user_id:
        raise ConfidentialFileError(code="not_owner", message="only the file owner can unshare it")
    target = (identity or "").strip()
    users_workspace = await _has_column(db, "users", "workspace_id")
    workspace_clause = " AND workspace_id = ?" if users_workspace else ""
    params = [target, target, target]
    if users_workspace:
        params.append(workspace_id)
    cur = await db.execute(
        "SELECT id FROM users WHERE deleted_at IS NULL "
        "AND (id = ? OR slug = ? OR email = ?)"
        + workspace_clause
        + " LIMIT 1",
        params,
    )
    row = await cur.fetchone()
    canonical = str(row[0]) if row is not None else target
    await db.execute("DELETE FROM file_acl WHERE file_id = ? AND identity = ?", (meta["id"], canonical))
    await _audit(db, action="unshare", actor=actor, path=path, details={"identity": canonical})
    await db.commit()
    return {"path": path, "owner": meta["owner_user_id"], "viewers": sorted(await acl_identities(db, meta["id"]))}


def _rel_for_project(fpath: str, project: str) -> str | None:
    """Project-relative path from any stored file_path shape (absolute or
    root-relative). None when the path does not contain the project segment."""
    normalized = fpath.replace("\\", "/")
    marker = f"/{project}/"
    idx = normalized.rfind(marker)
    if idx != -1:
        return normalized[idx + len(marker):]
    if normalized.startswith(f"{project}/"):
        return normalized[len(project) + 1:]
    return None


async def file_path_confidential(
    db: aiosqlite.Connection,
    project: str,
    fpath: str,
    *,
    workspace_id: str | None = None,
) -> bool:
    """Indexer skip predicate (F4.c): keyed on file_meta, NOT the frontmatter."""
    rel = _rel_for_project(fpath, project)
    if not rel:
        return False
    try:
        if await _file_meta_workspace_enabled(db):
            workspace = (workspace_id or "").strip()
            if not workspace:
                raise ConfidentialFileError(
                    code="workspace_context_required",
                    message="Authenticated workspace context is required",
                )
            cur = await db.execute(
                "SELECT 1 FROM file_meta WHERE workspace_id = ? "
                "AND project_slug = ? AND rel_path = ? "
                "AND confidential = 1 LIMIT 1",
                (workspace, project, rel),
            )
        else:
            cur = await db.execute(
                "SELECT 1 FROM file_meta WHERE project_slug = ? "
                "AND rel_path = ? AND confidential = 1 LIMIT 1",
                (project, rel),
            )
        return await cur.fetchone() is not None
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():  # pre-162 compatibility
            return False
        raise


async def migrate_file_meta_path(
    db: aiosqlite.Connection,
    *,
    old_path: str,
    new_path: str,
    workspace_id: str | None = None,
) -> None:
    """Move/rename hook: confidentiality travels with the file (same tx as the
    caller's rename bookkeeping; caller commits)."""
    old_key = canonical_file_key(old_path)
    new_key = canonical_file_key(new_path)
    if old_key is None or new_key is None:
        return
    try:
        if await _file_meta_workspace_enabled(db):
            workspace = (workspace_id or "").strip()
            if not workspace:
                raise ConfidentialFileError(
                    code="workspace_context_required",
                    message="Authenticated workspace context is required",
                )
            await db.execute(
                "UPDATE file_meta SET project_slug = ?, rel_path = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE workspace_id = ? AND project_slug = ? AND rel_path = ?",
                (
                    new_key[0],
                    new_key[1],
                    workspace,
                    old_key[0],
                    old_key[1],
                ),
            )
        else:
            await db.execute(
                "UPDATE file_meta SET project_slug = ?, rel_path = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE project_slug = ? AND rel_path = ?",
                (new_key[0], new_key[1], old_key[0], old_key[1]),
            )
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():  # pre-162 compatibility
            return
        raise
