# Brain v2 P4-F1 PR-b — salience decay cycle phase.
#
# Wires the existing pure salience_service.compute_decay (Ebbinghaus per-doc-type
# half-life) into the daily brain cycle as an isolated phase, so document salience
# decays every cycle and stale docs sink in search ranking over time.
#
# DECAY ONLY: this phase never boosts. compute_boost / boost_document (and the
# boost_log rate-limit marker used by the REM-agent batch-decay endpoint) are
# deliberately untouched — v1 boost stays out of the cycle.
#
# Same-day idempotent: a run stamps salience_updated_at = now on every changed doc,
# so a second same-day pass computes days~0 -> compute_decay returns the current
# value -> no rows change. Daily cadence therefore applies exactly one day of decay.
#
# Semantics mirror routers/documents.batch_decay verbatim (null salience -> 0.5,
# null/unparseable salience_updated_at -> 30d, epsilon 0.0001) so the cycle and the
# REM endpoint stay interchangeable.
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from core.api.db import write_db
from core.api.services.salience_service import compute_decay

logger = logging.getLogger(__name__)

_DECAY_EPSILON = 0.0001
_DEFAULT_DAYS_IF_NULL = 30.0
_DEFAULT_SALIENCE = 0.5
_DEFAULT_DOC_TYPE = "file"


@dataclass(slots=True)
class SalienceDecayReport:
    """Return envelope for run_salience_decay_phase()."""

    run_id: str
    scanned: int = 0
    updated: int = 0
    unchanged: int = 0


def compute_decay_updates(
    rows: Iterable[Sequence], *, now: datetime
) -> tuple[list[tuple[float, str, object]], int]:
    """PURE: (id, salience, doc_type, salience_updated_at) rows ->
    (updates=[(new_salience, now_iso, id)], unchanged_count).

    No DB access — deterministic given rows + now. Rows may be aiosqlite.Row or
    plain tuples; only positional order matters.
    """
    now_iso = now.isoformat()
    updates: list[tuple[float, str, object]] = []
    unchanged = 0
    for row in rows:
        doc_id, salience, doc_type, updated_at = row[0], row[1], row[2], row[3]
        current = salience if salience is not None else _DEFAULT_SALIENCE
        dtype = doc_type or _DEFAULT_DOC_TYPE
        if updated_at:
            try:
                dt = datetime.fromisoformat(updated_at)
                days = (now - dt).total_seconds() / 86400
            except (ValueError, TypeError):
                days = _DEFAULT_DAYS_IF_NULL
        else:
            days = _DEFAULT_DAYS_IF_NULL
        new_salience = compute_decay(current, dtype, days)
        if abs(new_salience - current) < _DECAY_EPSILON:
            unchanged += 1
        else:
            updates.append((new_salience, now_iso, doc_id))
    return updates, unchanged


async def run_salience_decay_phase(
    *, run_id: str, now: datetime, workspace_id: str = "ws_default"
) -> SalienceDecayReport:
    """Cycle phase: Ebbinghaus decay of non-archived documents' salience.

    Owns its write_db(); a failure propagates to the caller (jobs._execute_cycle),
    which appends it to `failures` and marks the run partial. DECAY ONLY — no boost.
    """
    async with write_db() as db:
        # Exclude confidential docs: the documents_fts_update trigger (mig 136)
        # DELETE+INSERTs the FTS row on ANY documents UPDATE, so decaying a
        # confidential doc (whose FTS row mark_confidential purged) would
        # resurrect its file_path into search. Never touch them.
        cur = await db.execute(
            "SELECT id, salience, doc_type, salience_updated_at FROM documents "
            "WHERE COALESCE(archived, 0) = 0 "
            "AND COALESCE(confidential, 0) = 0 "
            "AND COALESCE(workspace_id, 'ws_default') = ?",
            (workspace_id,),
        )
        rows = await cur.fetchall()
        await cur.close()

        updates, unchanged = compute_decay_updates(rows, now=now)

        if updates:
            await db.executemany(
                "UPDATE documents SET salience = ?, salience_updated_at = ? WHERE id = ?",
                updates,
            )
            await db.commit()

    report = SalienceDecayReport(
        run_id=run_id, scanned=len(rows), updated=len(updates), unchanged=unchanged
    )
    logger.info(
        "brain.salience_decay: run_id=%s scanned=%d updated=%d unchanged=%d",
        run_id,
        report.scanned,
        report.updated,
        report.unchanged,
    )
    return report


__all__ = ["compute_decay_updates", "run_salience_decay_phase", "SalienceDecayReport"]
