#!/usr/bin/env python3
# v1.0.0 - 2026-04-30 - Reparse legacy parse_error files post OCR fix (Story P1.5.E0)
"""Riprocessa file in stato parse_error post-fix OCR backend.

Usage (post-deploy):
    /data/pir/venv/bin/python /data/pir/scripts/reparse_failed.py

Re-runs ``parse_pending`` for every ingest row currently stuck in
``status='parse_error'``. Idempotent: parse_pending updates status to
``parsing`` -> ``parsed``/``parse_error`` again, so successful runs leave
the row in the new (good) state and failed runs simply log the error.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.api.db import acquire_db, close_pool, init_pool  # noqa: E402
from core.api.services.ingest.parser_router import parse_pending  # noqa: E402

logger = logging.getLogger("reparse_failed")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    await init_pool()
    try:
        async with acquire_db() as db:
            cursor = await db.execute(
                "SELECT id, file_path FROM ingest_pending WHERE status = 'parse_error'"
            )
            rows = await cursor.fetchall()
        logger.info("found %d parse_error rows to reparse", len(rows))
        for row in rows:
            ingest_id = row["id"]
            file_path = row["file_path"]
            try:
                logger.info("reparse %s %s", ingest_id, file_path)
                await parse_pending(ingest_id)
            except Exception:
                logger.exception("reparse failed for %s", ingest_id)
        logger.info("done")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
