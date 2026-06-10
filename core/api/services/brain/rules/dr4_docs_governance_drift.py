# DR4 — Docs Governance Drift (sub-02 §5 DR4, CE4 axis=context).
# Pass-through from `docs_drift_history` (a derived table). Preserves original
# fingerprint and severity. Rule emits one signal per row in the cycle window.
from __future__ import annotations

import logging
from datetime import datetime

from core.api.db import acquire_db
from core.api.models.brain import DriftSignal, Severity
from core.api.services.brain.cycle_snapshot import CycleSnapshot
from core.api.services.brain.rules._signals import build_signal

logger = logging.getLogger(__name__)


def _severity_from_history(value: str | None) -> Severity:
    if value in {"low", "medium", "high", "critical"}:
        return value  # type: ignore[return-value]
    return "low"


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    signals: list[DriftSignal] = []
    try:
        async with acquire_db() as db:
            cursor = await db.execute(
                "SELECT fingerprint, drift_detail, severity, project, doc_path, "
                "       layer, created_at FROM docs_drift_history "
                "WHERE date(created_at) = date(?) "
                "AND dedup_expires_at IS NOT NULL "
                "ORDER BY created_at DESC",
                (snapshot.cycle_key,),
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 — table may not exist in test DB
        logger.debug("DR4: docs_drift_history lookup skipped (%s)", exc)
        return signals

    for row in rows:
        fingerprint = row[0]
        drift_detail = row[1] or ""
        severity_raw = row[2]
        project = row[3]
        doc_path = row[4]
        observed_ref = f"docs_drift:{fingerprint}"
        observed_delta = (drift_detail or f"Docs governance drift in {doc_path}")[:2000]
        evidence_refs = [
            f"docs_drift_history:{fingerprint}",
            f"doc_path:{doc_path}",
        ]
        scope_type = "project" if project else "company"
        scope_key = project or "__company__"
        signals.append(
            build_signal(
                run_id=run_id,
                cycle_key=snapshot.cycle_key,
                detected_at=now,
                rule_id="DR4",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=None,
                signal_type="docs_governance_drift",
                expected_direction_source="doc",
                expected_direction_ref=f"doc:{doc_path}",
                observed_direction_ref=observed_ref,
                observed_delta=observed_delta,
                evidence_refs=evidence_refs,
                severity_base=_severity_from_history(severity_raw),
                drift_axis="context",
                involved_projects=[project] if project else [],
            )
        )
    return signals


__all__ = ["build_signals"]
