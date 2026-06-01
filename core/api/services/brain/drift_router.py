# Brain v1 — Drift router service (sub-02 C5, §11.1).
# List / fetch / patch backing for the HTTP router. Visibility uses
# api/visibility.py — ALL-or-redacted per §4.3.
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.api.db import acquire_db, write_db
from core.api.models import UserInfo
from core.api.models.brain import (
    DirectionSource,
    DriftAxis,
    DriftListResponse,
    DriftPatchAction,
    DriftSignal,
    DriftSignalRedacted,
    KnowledgeForm,
    RuleId,
    ScopeType,
    Severity,
    SignalState,
    SignalType,
)
from core.api.visibility import get_visible_projects

logger = logging.getLogger(__name__)

DEFAULT_DRIFT_LIMIT = 50
MAX_DRIFT_LIMIT = 200

_SEVERITY_RANK: dict[Severity, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

_VALID_AXES: frozenset[DriftAxis] = frozenset({"intent", "context", "both"})


def _encode_cursor(severity: Severity, confidence: float, detected_at: str, signal_id: str) -> str:
    payload = json.dumps(
        {
            "s": _SEVERITY_RANK.get(severity, 0),
            "c": round(confidence, 6),
            "d": detected_at,
            "i": signal_id,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, float, str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        return (
            int(payload["s"]),
            float(payload["c"]),
            str(payload["d"]),
            str(payload["i"]),
        )
    except (KeyError, ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _split_axes(values: list[str] | None) -> list[DriftAxis] | None:
    if not values:
        return None
    out: list[DriftAxis] = []
    for v in values:
        for token in str(v).split(","):
            token = token.strip()
            if token in _VALID_AXES:
                out.append(token)  # type: ignore[arg-type]
    return out or None


async def _resolve_run(
    db: aiosqlite.Connection,
    *,
    cycle_key: str | None,
    run_id: str | None,
    workspace_id: str,
) -> dict[str, Any] | None:
    if run_id:
        row = await (
            await db.execute(
                "SELECT run_id, cycle_key FROM brain_runs WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "cycle_key": row[1]}
    if not cycle_key or cycle_key == "latest":
        row = await (
            await db.execute(
                "SELECT run_id, cycle_key FROM brain_runs "
                "WHERE workspace_id = ? AND status IN ('succeeded','partial') "
                "AND superseded_by_run_id IS NULL "
                "ORDER BY cycle_key DESC, started_at DESC LIMIT 1",
                (workspace_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "cycle_key": row[1]}
    row = await (
        await db.execute(
            "SELECT run_id, cycle_key FROM brain_runs "
            "WHERE workspace_id = ? AND cycle_key = ? "
            "AND status IN ('succeeded','partial') "
            "AND superseded_by_run_id IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (workspace_id, cycle_key),
        )
    ).fetchone()
    if row is None:
        return None
    return {"run_id": row[0], "cycle_key": row[1]}


def _row_to_signal(row: aiosqlite.Row) -> DriftSignal:
    return DriftSignal(
        signal_id=row["signal_id"],
        run_id=row["run_id"],
        cycle_key=row["cycle_key"],
        detected_at=_parse_iso(row["detected_at"]) or datetime.now(timezone.utc),
        rule_id=row["rule_id"],
        schema_version=row["schema_version"],
        scope_type=row["scope_type"],
        scope_key=row["scope_key"],
        program_key=row["program_key"],
        signal_type=row["signal_type"],
        knowledge_form=row["knowledge_form"],
        classifier_version=row["classifier_version"],
        expected_direction_source=row["expected_direction_source"],
        expected_direction_ref=row["expected_direction_ref"],
        observed_direction_ref=row["observed_direction_ref"],
        observed_delta=row["observed_delta"],
        evidence=json.loads(row["evidence_json"] or "[]"),
        evidence_hash=row["evidence_hash"],
        severity=row["severity"],
        confidence=row["confidence"],
        recurrence_key=row["recurrence_key"],
        involved_projects=json.loads(row["involved_projects_json"] or "[]"),
        state=row["state"],
        superseded_by_signal_id=row["superseded_by_signal_id"],
        resolved_at=_parse_iso(row["resolved_at"]),
        dismissed_at=_parse_iso(row["dismissed_at"]),
        dismissed_by=row["dismissed_by"],
        dismiss_reason=row["dismiss_reason"],
        drift_axis=row["drift_axis"],
    )


def _redact(sig: DriftSignal) -> DriftSignalRedacted:
    return DriftSignalRedacted(
        signal_id=sig.signal_id,
        cycle_key=sig.cycle_key,
        signal_type=sig.signal_type,
        severity=sig.severity,
    )


def _is_visible(visible: set[str] | None, involved_projects: list[str]) -> bool:
    if visible is None:
        return True
    if not involved_projects:
        return True
    return set(involved_projects).issubset(visible)


async def list_drift_signals(
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    signal_types: list[SignalType] | None = None,
    knowledge_forms: list[KnowledgeForm] | None = None,
    severity_min: Severity = "low",
    confidence_min: float = 0.0,
    states: list[SignalState] | None = None,
    drift_axes: list[str] | None = None,
    rule_ids: list[str] | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_DRIFT_LIMIT,
    user: UserInfo | None = None,
    workspace_id: str = "ws_default",
) -> DriftListResponse:
    limit = max(1, min(MAX_DRIFT_LIMIT, int(limit)))
    over_fetch = limit + 1
    states_to_apply = states or ["open"]
    axes = _split_axes(drift_axes)

    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        run = await _resolve_run(
            db, cycle_key=cycle_key, run_id=run_id, workspace_id=workspace_id
        )
        if run is None:
            return DriftListResponse(items=[], total_returned=0)

        visible = await get_visible_projects(db, user, workspace_id) if user else None

        where = ["s.run_id = ?"]
        params: list[Any] = [run["run_id"]]
        if scope_type:
            where.append("s.scope_type = ?")
            params.append(scope_type)
        if scope_key:
            where.append("s.scope_key = ?")
            params.append(scope_key)
        if signal_types:
            placeholders = ",".join("?" for _ in signal_types)
            where.append(f"s.signal_type IN ({placeholders})")
            params.extend(signal_types)
        if knowledge_forms:
            placeholders = ",".join("?" for _ in knowledge_forms)
            where.append(f"s.knowledge_form IN ({placeholders})")
            params.extend(knowledge_forms)
        if rule_ids:
            placeholders = ",".join("?" for _ in rule_ids)
            where.append(f"s.rule_id IN ({placeholders})")
            params.extend(rule_ids)
        if axes:
            placeholders = ",".join("?" for _ in axes)
            where.append(f"s.drift_axis IN ({placeholders})")
            params.extend(axes)
        if states_to_apply:
            placeholders = ",".join("?" for _ in states_to_apply)
            where.append(f"s.state IN ({placeholders})")
            params.extend(states_to_apply)
        min_rank = _SEVERITY_RANK.get(severity_min, 1)
        where.append(
            "CASE s.severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 3 WHEN 'critical' THEN 4 END >= ?"
        )
        params.append(min_rank)
        where.append("s.confidence >= ?")
        params.append(confidence_min)

        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded:
                cur_sev, cur_conf, cur_dt, cur_id = decoded
                where.append(
                    "(CASE s.severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 3 WHEN 'critical' THEN 4 END, "
                    "s.confidence, s.detected_at, s.signal_id) < (?, ?, ?, ?)"
                )
                params.extend([cur_sev, cur_conf, cur_dt, cur_id])

        query = (
            "SELECT s.* FROM brain_drift_signals s "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY "
            "  CASE s.severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 3 WHEN 'critical' THEN 4 END DESC, "
            "  s.confidence DESC, s.detected_at DESC, s.signal_id ASC "
            "LIMIT ?"
        )
        params.append(over_fetch)
        rows = await (await db.execute(query, params)).fetchall()

    items: list[DriftSignal | DriftSignalRedacted] = []
    next_cursor: str | None = None
    redacted_count = 0
    page_rows = rows[:limit]
    if len(rows) > limit:
        last = page_rows[-1]
        next_cursor = _encode_cursor(
            last["severity"],
            last["confidence"],
            last["detected_at"],
            last["signal_id"],
        )
    for row in page_rows:
        sig = _row_to_signal(row)
        if not _is_visible(visible, sig.involved_projects):
            redacted_count += 1
            items.append(_redact(sig))
            continue
        items.append(sig)

    return DriftListResponse(
        items=items,
        next_cursor=next_cursor,
        cycle_key=run["cycle_key"],
        run_id=run["run_id"],
        redacted_count=redacted_count,
        total_returned=len(items),
    )


async def fetch_single_drift_signal(
    *,
    signal_id: str,
    user: UserInfo | None,
    workspace_id: str = "ws_default",
) -> DriftSignal | None:
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                "SELECT * FROM brain_drift_signals WHERE signal_id = ?",
                (signal_id,),
            )
        ).fetchone()
        if row is None:
            return None
        visible = await get_visible_projects(db, user, workspace_id) if user else None
    sig = _row_to_signal(row)
    if not _is_visible(visible, sig.involved_projects):
        # 404 — never reveal existence to redacted callers.
        return None
    return sig


async def apply_drift_patch(
    *,
    signal_id: str,
    action: DriftPatchAction,
    reason: str | None,
    user: UserInfo,
    workspace_id: str = "ws_default",
    now: datetime,
) -> DriftSignal | None:
    iso_now = now.astimezone(timezone.utc).isoformat()
    # First load + visibility gate.
    existing = await fetch_single_drift_signal(
        signal_id=signal_id, user=user, workspace_id=workspace_id
    )
    if existing is None:
        return None

    new_state: SignalState = existing.state
    resolved_at = existing.resolved_at.astimezone(timezone.utc).isoformat() if existing.resolved_at else None
    dismissed_at = existing.dismissed_at.astimezone(timezone.utc).isoformat() if existing.dismissed_at else None
    dismissed_by = existing.dismissed_by
    dismiss_reason = existing.dismiss_reason

    if action == "dismiss":
        new_state = "dismissed"
        dismissed_at = iso_now
        dismissed_by = user.user_id
        dismiss_reason = reason
    elif action == "acknowledge":
        # Acknowledge keeps state=open but stores reason as dismiss_reason
        # surrogate (no separate column in v1 schema).
        new_state = "open"
        dismiss_reason = reason
    elif action == "resolve":
        new_state = "resolved"
        resolved_at = iso_now
    elif action == "reopen":
        new_state = "open"
        resolved_at = None
        dismissed_at = None
        dismissed_by = None
        dismiss_reason = None

    async with write_db() as db:
        await db.execute(
            "UPDATE brain_drift_signals SET "
            "  state = ?, resolved_at = ?, dismissed_at = ?, dismissed_by = ?,"
            "  dismiss_reason = ?"
            " WHERE signal_id = ?",
            (
                new_state,
                resolved_at,
                dismissed_at,
                dismissed_by,
                dismiss_reason,
                signal_id,
            ),
        )
    refreshed = await fetch_single_drift_signal(
        signal_id=signal_id, user=user, workspace_id=workspace_id
    )
    return refreshed


__all__ = [
    "DEFAULT_DRIFT_LIMIT",
    "MAX_DRIFT_LIMIT",
    "apply_drift_patch",
    "fetch_single_drift_signal",
    "list_drift_signals",
]
