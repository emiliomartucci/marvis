"""Brain agent-native: persist the agent-written synthesis + read staleness.

Decision 2026-07-01-brain-agent-native: the platform exposes the deterministic
substrate; the user's own agent does the LLM synthesis and persists it here. This
is the domain seam — the MCP write tools are one caller, and a future GUI-BYOK
path (Sprint 2) is a second caller of the same functions. Provenance is kept
SEPARATE from the cycle's own columns so the cycle and the agent never overwrite
each other: ``narrative_agent`` on journal entries (migration 158) and
``authored_by_agent`` on findings (migration 159).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.api.db import acquire_db, write_db

_LAST_SYNTHESIS_KEY = "brain_last_synthesis_at"
_LAST_CYCLE_KEY = "brain_last_cycle_key"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def persist_journal_narrative(
    *,
    cycle_key: str,
    scope_type: str,
    scope_key: str,
    narrative: str,
    agent_by: str,
    workspace_id: str = "ws_default",
) -> bool:
    """Write the agent's narrative onto the matching journal entry.

    Targets the row by (workspace_id, cycle_key, scope_type, scope_key) — the
    natural key of a published entry. Returns True when a row was updated. Also
    records ``brain_last_synthesis_at`` so staleness is interrogable. Never
    touches ``narrative_polished`` (cycle provenance) or ``body_json``
    (deterministic substrate).
    """
    ts = _now_iso()
    async with write_db() as db:
        cur = await db.execute(
            "UPDATE brain_journal_entries SET "
            "  narrative_agent = ?, narrative_agent_at = ?, narrative_agent_by = ? "
            "WHERE workspace_id = ? AND cycle_key = ? "
            "  AND scope_type = ? AND scope_key = ?",
            (narrative, ts, agent_by, workspace_id, cycle_key, scope_type, scope_key),
        )
        updated = cur.rowcount > 0
        if updated:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (_LAST_SYNTHESIS_KEY, ts, ts),
            )
    return updated


async def get_staleness(*, workspace_id: str = "ws_default") -> dict[str, Any]:
    """Return the agent-synthesis freshness signal (mechanical, no LLM).

    ``last_synthesis_at`` is when the agent last wrote a synthesis (via
    persist_*). ``last_cycle_key`` is the last mechanical cycle. An agent can read
    this on connect to decide whether to re-synthesize before answering.
    """
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                "SELECT key, value FROM app_settings WHERE key IN (?, ?)",
                (_LAST_SYNTHESIS_KEY, _LAST_CYCLE_KEY),
            )
        ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    return {
        "last_synthesis_at": values.get(_LAST_SYNTHESIS_KEY),
        "last_cycle_key": values.get(_LAST_CYCLE_KEY),
    }


async def persist_agent_finding(
    *,
    finding_type: str,
    scope_type: str,
    scope_key: str,
    title: str,
    summary: str,
    why_now: str,
    severity: str,
    confidence: str,
    suggested_artifact: str = "none",
    program_key: str | None = None,
    involved_projects: list[str] | None = None,
    closure_instruction: str | None = None,
    agent_by: str,
    workspace_id: str = "ws_default",
) -> dict[str, Any]:
    """Write an agent-authored finding into the Triage queue (approval_state='open').

    The platform runs no synthesis LLM (decision 2026-07-01-brain-agent-native):
    the agent's conclusion is persisted here, reusing the SAME finalize/persist
    path the mechanical rules use (so id/hash/fingerprint/evidence/state rows are
    derived identically), then stamped with ``authored_by_agent`` so it is
    distinguishable from rule-authored findings (migration 159). Attaches to the
    latest succeeded/partial run (findings carry a ``run_id`` FK). Evidence carries
    an ``agent:<id>`` ref so the content-derived finding_id is unique to this
    write and does not collide with a cycle finding. Returns ``written=False`` with
    a ``reason`` when there is no run to attach to or the content dedups.
    """
    # Imported lazily: findings.py pulls in the whole rule engine, and this
    # module is imported at read-time by the journal path too.
    from core.api.models.brain import ClosureManualAttest
    from core.api.services.brain.findings import (
        FindingDraft,
        _persist_findings,
        finalize_finding,
    )

    # A finding needs a run_id (FK). Attach to the latest mechanical cycle run.
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        run = await (
            await db.execute(
                "SELECT run_id, cycle_key FROM brain_runs "
                "WHERE workspace_id = ? AND status IN ('succeeded', 'partial') "
                "  AND superseded_by_run_id IS NULL "
                "ORDER BY cycle_key DESC, started_at DESC LIMIT 1",
                (workspace_id,),
            )
        ).fetchone()
    if run is None:
        return {"written": False, "reason": "no_brain_run"}
    run_id = run["run_id"]
    cycle_key = run["cycle_key"]

    instruction = (closure_instruction or "").strip()
    if len(instruction) < 10:
        instruction = "Operator attests this agent finding is resolved."
    instruction = instruction[:500]

    draft = FindingDraft(
        finding_type=finding_type,  # type: ignore[arg-type]
        scope_type=scope_type,  # type: ignore[arg-type]
        scope_key=scope_key,
        program_key=program_key,
        title=title[:200],
        summary=summary[:2000],
        why_now=why_now[:500],
        evidence=[f"agent:{agent_by}"],
        suggested_artifact=suggested_artifact,  # type: ignore[arg-type]
        closure_condition=ClosureManualAttest(instruction=instruction),
        severity=severity,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        involved_projects=list(involved_projects or []),
    )
    finding = await finalize_finding(
        draft=draft,
        run_id=run_id,
        cycle_key=cycle_key,
        now=datetime.now(timezone.utc),
    )
    persisted, _fingerprints = await _persist_findings(
        run_id=run_id, findings=[finding]
    )
    if persisted <= 0:
        return {
            "written": False,
            "reason": "duplicate",
            "finding_id": finding.finding_id,
        }

    ts = _now_iso()
    async with write_db() as db:
        await db.execute(
            "UPDATE brain_findings SET authored_by_agent = ? WHERE finding_id = ?",
            (agent_by, finding.finding_id),
        )
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (_LAST_SYNTHESIS_KEY, ts, ts),
        )
    return {
        "written": True,
        "finding_id": finding.finding_id,
        "run_id": run_id,
        "cycle_key": cycle_key,
        "approval_state": "open",
    }
