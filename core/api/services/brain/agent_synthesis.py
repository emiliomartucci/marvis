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

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.api.db import acquire_db, write_db
from core.api.services.brain.run_selection import latest_readable_run

logger = logging.getLogger(__name__)

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


async def get_staleness(*, workspace_id: str = "ws_default", caller_ctx: Any = None) -> dict[str, Any]:
    """Return caller-visible journal freshness plus advisory delta judgment.

    The watermark covers every visible journal scope conservatively. This read
    never schedules synthesis or persists provider output.
    """
    if caller_ctx is None or caller_ctx.workspace_id != workspace_id:
        return {"last_synthesis_at": None, "last_cycle_key": None,
                "synthesis_advisory": {"should": True, "outcome": "error", "reasoning": "caller scope unavailable", "advisory": True}}
    from core.api.services.jev.journal_context import synthesis_advice
    return await synthesis_advice(caller_ctx)


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
    evidence_refs: list[str] | None = None,
    agent_by: str,
    workspace_id: str = "ws_default",
    caller_ctx: Any = None,
) -> dict[str, Any]:
    """Write an agent-authored finding into the Triage queue (approval_state='open').

    The agent's effective content, author, workspace, closure and real source
    references determine identity. Authorship is inserted in the same transaction
    as the finding, state and evidence. A missing source returns an explicit
    recovery instruction; authors never become invented graph nodes. Attach to
    the latest readable run.
    Identical retries return written=False without creating another row.
    """
    # Imported lazily: findings.py pulls in the whole rule engine, and this
    # module is imported at read-time by the journal path too.
    from core.api.models.brain import ClosureManualAttest
    from core.api.services.brain.findings import (
        FindingDraft,
        _EVIDENCE_KINDS,
        _persist_findings,
        finalize_finding,
    )

    from core.api.use_cases._errors import ValidationError
    if not evidence_refs:
        return {
            "written": False,
            "reason": "missing_evidence",
            "message": "Read the supporting source, then retry with evidence_refs "
                       "such as task:<id>, digest_event:<id>, or learning:<id>.",
        }
    for ref in evidence_refs or []:
        prefix, separator, value = ref.partition(":")
        if not separator or prefix not in _EVIDENCE_KINDS or not value.strip():
            raise ValidationError(code="invalid_evidence_ref", message="Evidence requires a typed source reference")
    strict_evidence = os.environ.get("MARVIS_ENTERPRISE_OIDC_STRICT", "").strip() == "1"
    if strict_evidence and (
        caller_ctx is None or caller_ctx.workspace_id != workspace_id
    ):
        raise ValidationError(code="evidence_not_found", message="Evidence source is not available")
    if strict_evidence and (
        len(evidence_refs) > 8
        or any(ref.partition(":")[0] not in {"task", "learning"} for ref in evidence_refs)
    ):
        raise ValidationError(
            code="unsupported_evidence_ref",
            message="Enterprise findings accept up to eight task or learning sources",
        )

    async def _validate_evidence_on_writer(db: aiosqlite.Connection) -> None:
        from core.api.services.jev.evidence_gate import resolve_evidence_sources

        sources = await resolve_evidence_sources(
            list(evidence_refs or []), workspace_id=workspace_id,
            ctx=caller_ctx, db=db,
        )
        if any("text" not in source for source in sources):
            raise ValidationError(
                code="evidence_not_found", message="Evidence source is not available"
            )
    # A finding needs a run_id (FK). Attach to the latest mechanical cycle run.
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        run = await latest_readable_run(db, workspace_id=workspace_id)
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
        evidence=list(evidence_refs or []),
        authored_by_agent=agent_by,
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
    # Content is identity, not evidence. Author provenance is stored atomically.
    content = json.dumps({
        "workspace": workspace_id, "author": agent_by,
        "title": finding.title, "summary": finding.summary,
        "why_now": finding.why_now, "program_key": finding.program_key,
        "projects": finding.involved_projects, "artifact": finding.suggested_artifact,
        "severity": finding.severity, "confidence": finding.confidence,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    def content_id(base: str) -> str:
        return hashlib.blake2b(f"{base}|{content}".encode(), digest_size=16).hexdigest()
    finding = finding.model_copy(update={
        "finding_id": content_id(finding.finding_id),
        "proposal_fingerprint": content_id(finding.proposal_fingerprint),
    })
    persisted, _fingerprints = await _persist_findings(
        run_id=run_id, findings=[finding],
        prewrite_validate=_validate_evidence_on_writer if strict_evidence else None,
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
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (_LAST_SYNTHESIS_KEY, ts, ts),
        )
    result = {
        "written": True,
        "finding_id": finding.finding_id,
        "run_id": run_id,
        "cycle_key": cycle_key,
        "approval_state": "open",
    }
    # SP-A U1 (plan 2026-09-21): advisory evidence gate. Runs AFTER the
    # finding is persisted — the write never depends on the provider. The
    # finding stays open regardless of the verdict (KTD4: advisory-first).
    try:
        from core.api.services.jev.evidence_gate import check_evidence

        gate = await check_evidence(
            finding.title,
            finding.summary,
            list(evidence_refs or []),
            workspace_id=workspace_id,
            ctx=caller_ctx,
        )
        result["evidence_gate"] = gate.to_dict()
    except Exception:  # noqa: BLE001 - the gate must never break the write
        logger.warning("evidence gate failed for finding %s", finding.finding_id)
        result["evidence_gate"] = {
            "surface": "evidence_gate",
            "verdict": "no_checked_refs",
            "outcome": "error",
            "refs": [{"ref": ref, "check": "not_judged"} for ref in evidence_refs or []],
        }
    return result
