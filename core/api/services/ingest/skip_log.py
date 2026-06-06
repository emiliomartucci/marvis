"""Audit logging for files silently skipped during ingest upload.

Centralizza l'INSERT in `ingest_skipped` (mig 103) cosi' watcher.py +
ingest_triage.py + parser_router.py possono loggare con la stessa API.

Reason enum (CHECK constraint mig 103):
  - dedup_sha256              file gia' presente per (sha256, project_slug)
  - invalid_path              path traversal o filename rifiutato
  - mime_not_allowed          estensione/MIME fuori whitelist
  - parse_error_pre_dispatch  parser_router fail prima di entrare saga
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal

import aiosqlite

logger = logging.getLogger(__name__)

SkipReason = Literal[
    "dedup_sha256",
    "invalid_path",
    "mime_not_allowed",
    "parse_error_pre_dispatch",
]


async def log_skip(
    db: aiosqlite.Connection,
    *,
    file_path_attempted: str,
    project_slug: str,
    reason: SkipReason,
    sha256: str | None = None,
    existing_ingest_id: str | None = None,
    error_message: str | None = None,
    created_by: str | None = None,
) -> str:
    """Insert one row into ingest_skipped + return new id.

    Best-effort: errors are logged WARN and swallowed, never break the upload
    flow (audit miss is preferable to a 500 on the user-facing endpoint).
    """
    skip_id = str(uuid.uuid4())
    try:
        await db.execute(
            """
            INSERT INTO ingest_skipped
                (id, file_path_attempted, project_slug, sha256,
                 reason, existing_ingest_id, error_message, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skip_id,
                file_path_attempted,
                project_slug,
                sha256,
                reason,
                existing_ingest_id,
                error_message,
                created_by,
            ),
        )
        await db.commit()
    except Exception as exc:
        logger.warning(
            "log_skip failed (non-critical): reason=%s file=%s err=%s",
            reason,
            file_path_attempted,
            exc,
        )
    return skip_id
