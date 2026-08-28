#!/usr/bin/env python3
# v1.0.0 - 2026-04-30 - Reparse legacy parse_error files post OCR fix (Story P1.5.E0)
"""Riprocessa file in stato parse_error post-fix OCR backend.

Usage (post-deploy, all workspaces):
    /data/pir/venv/bin/python /data/pir/scripts/reparse_failed.py

Re-runs ``parse_pending`` for every ingest row currently stuck in
``status='parse_error'``. Idempotent: parse_pending updates status to
``parsing`` -> ``parsed``/``parse_error`` again, so successful runs leave
the row in the new (good) state and failed runs simply log the error.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.api.db import acquire_db, close_pool, init_pool  # noqa: E402
from core.api.services.ingest.parser_router import parse_pending  # noqa: E402

logger = logging.getLogger("reparse_failed")


async def _workspace_ids(db) -> tuple[list[str], int]:
    cursor = await db.execute(
        "SELECT workspace_id, COUNT(*) AS row_count FROM ingest_pending "
        "WHERE status='parse_error' GROUP BY workspace_id "
        "ORDER BY workspace_id ASC"
    )
    rows = await cursor.fetchall()
    workspace_ids: list[str] = []
    quarantined_unowned = 0
    for row in rows:
        workspace_id = (row["workspace_id"] or "").strip()
        if not workspace_id:
            quarantined_unowned += int(row["row_count"] or 0)
            continue
        workspace_ids.append(workspace_id)
    if quarantined_unowned:
        logger.warning(
            "ingest reparse quarantined %d parse_error row(s) without workspace ownership",
            quarantined_unowned,
        )
    return workspace_ids, quarantined_unowned


async def main(workspace_id: str | None = None) -> dict[str, int]:
    if workspace_id is not None:
        workspace_id = workspace_id.strip()
        if not workspace_id:
            raise ValueError("workspace_id must be non-empty when provided")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    await init_pool()
    result = {
        "workspaces_scanned": 0,
        "quarantined_unowned": 0,
        "queued": 0,
        "failed": 0,
    }
    try:
        async with acquire_db() as db:
            if workspace_id is not None:
                workspace_ids = [workspace_id]
                quarantined_unowned = 0
            else:
                workspace_ids, quarantined_unowned = await _workspace_ids(db)
            rows_by_workspace: list[tuple[str, list]] = []
            for current_workspace_id in workspace_ids:
                cursor = await db.execute(
                    """
                    SELECT id, file_path
                      FROM ingest_pending
                     WHERE workspace_id = ?
                       AND status = 'parse_error'
                     ORDER BY created_at ASC, id ASC
                    """,
                    (current_workspace_id,),
                )
                rows_by_workspace.append(
                    (current_workspace_id, await cursor.fetchall())
                )
        result["workspaces_scanned"] = len(rows_by_workspace)
        result["quarantined_unowned"] = quarantined_unowned
        for current_workspace_id, rows in rows_by_workspace:
            logger.info(
                "found %d parse_error rows to reparse workspace_id=%s",
                len(rows),
                current_workspace_id,
            )
            for row in rows:
                ingest_id = row["id"]
                file_path = row["file_path"]
                try:
                    logger.info(
                        "reparse %s %s workspace_id=%s",
                        ingest_id,
                        file_path,
                        current_workspace_id,
                    )
                    await parse_pending(ingest_id, current_workspace_id)
                    result["queued"] += 1
                except Exception:
                    result["failed"] += 1
                    logger.exception(
                        "reparse failed for %s workspace_id=%s",
                        ingest_id,
                        current_workspace_id,
                    )
        logger.info("done")
        return result
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reparse failed Marvis ingest rows")
    parser.add_argument(
        "--workspace-id",
        default=os.environ.get("MARVIS_MCP_WORKSPACE_ID") or None,
        help="Workspace whose failed rows may be reparsed; omit to enumerate all workspaces",
    )
    args = parser.parse_args()
    asyncio.run(main(args.workspace_id))
