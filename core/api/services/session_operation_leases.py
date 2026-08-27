"""Persisted compare-and-swap leases for tmux session lifecycle effects."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Literal

import aiosqlite

from core.api.db import write_db


SessionOperation = Literal["complete", "delete", "hibernate", "resume", "restart"]
_LEASE_SECONDS = 600


class SessionOperationBusy(RuntimeError):
    """Another lifecycle effect currently owns this tenant/name."""


class SessionOperationMissing(RuntimeError):
    """The expected tenant/name/generation no longer exists."""


@dataclass(frozen=True, slots=True)
class SessionOperationLease:
    workspace_id: str
    session_name: str
    session_uuid: str
    operation: SessionOperation
    generation: int


def _required(value: str, field: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{field} must be non-empty")
    return normalized


async def acquire_session_operation_lease(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    session_name: str,
    session_uuid: str,
    operation: SessionOperation,
) -> SessionOperationLease:
    """Atomically advance the generation when no unexpired operation owns it."""
    workspace = _required(workspace_id, "workspace_id")
    name = _required(session_name, "session_name")
    expected_uuid = _required(session_uuid, "session_uuid")

    await db.execute(
        "INSERT OR IGNORE INTO session_operation_leases("
        "workspace_id,session_name,session_uuid,generation) VALUES (?, ?, ?, 0)",
        (workspace, name, expected_uuid),
    )
    cursor = await db.execute(
        "UPDATE session_operation_leases SET session_uuid=?, operation=?, "
        "generation=generation+1, "
        "lease_expires_at=strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND session_name=? "
        "AND (operation IS NULL OR lease_expires_at IS NULL "
        "OR lease_expires_at<=strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "AND EXISTS (SELECT 1 FROM sessions_meta sm "
        "WHERE sm.workspace_id=? AND sm.name=? AND sm.session_uuid=?)",
        (
            expected_uuid,
            operation,
            f"+{_LEASE_SECONDS} seconds",
            workspace,
            name,
            workspace,
            name,
            expected_uuid,
        ),
    )
    if cursor.rowcount != 1:
        current = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta "
                "WHERE workspace_id=? AND name=? AND session_uuid=?",
                (workspace, name, expected_uuid),
            )
        ).fetchone()
        if current is None:
            raise SessionOperationMissing("session generation changed")
        raise SessionOperationBusy("session lifecycle operation already in progress")

    row = await (
        await db.execute(
            "SELECT generation FROM session_operation_leases "
            "WHERE workspace_id=? AND session_name=? AND session_uuid=? "
            "AND operation=?",
            (workspace, name, expected_uuid, operation),
        )
    ).fetchone()
    if row is None:
        raise SessionOperationBusy("session lifecycle lease not persisted")
    return SessionOperationLease(
        workspace_id=workspace,
        session_name=name,
        session_uuid=expected_uuid,
        operation=operation,
        generation=int(row["generation"]),
    )


async def release_session_operation_lease(
    db: aiosqlite.Connection,
    lease: SessionOperationLease,
) -> bool:
    """Release only the generation acquired by this caller."""
    cursor = await db.execute(
        "UPDATE session_operation_leases SET operation=NULL, "
        "lease_expires_at=NULL, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND session_name=? AND session_uuid=? "
        "AND operation=? AND generation=?",
        (
            lease.workspace_id,
            lease.session_name,
            lease.session_uuid,
            lease.operation,
            lease.generation,
        ),
    )
    return cursor.rowcount == 1


async def session_operation_generation_is_current(
    db: aiosqlite.Connection,
    lease: SessionOperationLease,
) -> bool:
    row = await (
        await db.execute(
            "SELECT 1 FROM session_operation_leases WHERE workspace_id=? "
            "AND session_name=? AND session_uuid=? AND operation=? "
            "AND generation=? AND lease_expires_at>"
            "strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (
                lease.workspace_id,
                lease.session_name,
                lease.session_uuid,
                lease.operation,
                lease.generation,
            ),
        )
    ).fetchone()
    return row is not None


@asynccontextmanager
async def session_operation_lease(
    *,
    workspace_id: str,
    session_name: str,
    session_uuid: str,
    operation: SessionOperation,
) -> AsyncIterator[SessionOperationLease]:
    """Acquire/release using short writer holds around slow external I/O."""
    async with write_db(label=f"session_lease.acquire.{operation}") as db:
        lease = await acquire_session_operation_lease(
            db,
            workspace_id=workspace_id,
            session_name=session_name,
            session_uuid=session_uuid,
            operation=operation,
        )
    try:
        yield lease
    finally:
        async with write_db(label=f"session_lease.release.{operation}") as db:
            await release_session_operation_lease(db, lease)
