"""Advisory lock helpers for ingest recovery.

SQLite has no `SELECT ... FOR UPDATE`; under the MarvisX single-writer
connection this uses an atomic status-preconditioned UPDATE over
`ingest_pending.locked_by/locked_at`.
"""
from __future__ import annotations

import os

WORKER_ID = f"recovery-{os.getpid()}"
LOCK_TIMEOUT_SECONDS = 120


async def try_acquire_advisory(
    ingest_id: str,
    db,
    expected_status: str,
    *,
    workspace_id: str = "ws_default",
    worker_id: str = WORKER_ID,
    timeout_seconds: int = LOCK_TIMEOUT_SECONDS,
) -> bool:
    """Return True when this worker acquired the row-level recovery lock."""
    cursor = await db.execute(
        """
        UPDATE ingest_pending
           SET locked_by = ?,
               locked_at = datetime('now')
         WHERE id = ?
           AND workspace_id = ?
           AND status = ?
           AND (
                locked_by IS NULL
                OR datetime(locked_at) < datetime('now', ?)
           )
        """,
        (
            worker_id,
            ingest_id,
            workspace_id,
            expected_status,
            f"-{timeout_seconds} seconds",
        ),
    )
    return cursor.rowcount == 1


async def release_advisory(
    ingest_id: str,
    db,
    *,
    workspace_id: str = "ws_default",
    worker_id: str = WORKER_ID,
) -> None:
    """Release this worker's recovery lock if it still owns it."""
    await db.execute(
        """
        UPDATE ingest_pending
           SET locked_by = NULL,
               locked_at = NULL
         WHERE id = ?
           AND workspace_id = ?
           AND locked_by = ?
        """,
        (ingest_id, workspace_id, worker_id),
    )
