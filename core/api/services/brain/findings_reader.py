# Brain v1 — Learn Findings read API (sub-04 §11.1) + CE2 recency decay (§11.5).
#
# Splits read paths off findings.py to keep the write/builder module focused
# on the cycle pipeline. This module owns:
#   * list_findings — cursor-paginated GET with §11.1 filters + visibility
#   * fetch_single_finding — detail fetch with visibility (404 if invisible)
#   * apply_lifecycle_patch — PATCH approve/dismiss/resolve (single)
#   * apply_bulk_patch — PATCH:bulk with cap 25
#   * get_apply_guidance — apply-as-guidance endpoint (NO write)
#
# §11.5 CE2 recency decay is a READ-TIME PURE function. Settings load
# happens here on every list_findings call. No DB column, no migration. The
# decay factor is a SECONDARY TIEBREAKER in the sort tuple — it NEVER
# multiplies into severity_rank/confidence_rank (range-compression
# anti-pattern, parent §7.4).
from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite

from core.api.db import acquire_db, write_db
from core.api.models import UserInfo
from core.api.models.brain import (
    ArtifactSelector,
    ClosureArtifactExists,
    ClosureCondition,
    ClosureDriftSignalClears,
    ClosureManualAttest,
    ClosureMemoryOpApplied,
    ConfidenceTier,
    Finding,
    FindingApprovalState,
    FindingBulkPatchResponse,
    FindingBulkResultEntry,
    FindingPatchAction,
    FindingRedacted,
    FindingsListResponse,
    OwnerHint,
    Severity,
)
from core.api.visibility import get_visible_projects

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
BULK_PATCH_MAX = 25

# Sub-04 §11.5 CE2 — recency decay tuning. Read-time only.
DEFAULT_RECENCY_HALF_LIFE_DAYS = 30
RECENCY_FACTOR_FLOOR = 0.01

_SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_CONFIDENCE_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}

# Default agent-facing filter (sub-04 §11.4 invariant 1).
_DEFAULT_STATES_AGENT: tuple[str, ...] = ("open",)
_TERMINAL_STATES_INCLUDE: tuple[str, ...] = (
    "open",
    "approved",
    "dismissed",
    "resolved",
)


# ---------------------------------------------------------------------------
# Settings (read-time)
# ---------------------------------------------------------------------------


async def _get_setting(db: aiosqlite.Connection, key: str, default: str) -> str:
    row = await (
        await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    ).fetchone()
    if row is None:
        return default
    return row[0] if not hasattr(row, "keys") else row["value"]


async def load_decay_settings(
    db: aiosqlite.Connection,
) -> tuple[bool, int]:
    """Return (decay_enabled, half_life_days). NEVER cached — settings can
    flip at runtime and the surface MUST observe the new value next request.
    """
    raw_enabled = await _get_setting(db, "brain_findings_recency_decay_enabled", "false")
    enabled = raw_enabled.strip().lower() in {"1", "true", "yes", "on"}
    raw_half_life = await _get_setting(
        db, "brain_findings_recency_half_life_days", str(DEFAULT_RECENCY_HALF_LIFE_DAYS)
    )
    try:
        half_life = max(1, int(raw_half_life))
    except (TypeError, ValueError):
        half_life = DEFAULT_RECENCY_HALF_LIFE_DAYS
    return (enabled, half_life)


def _recency_factor(
    detected_at: datetime, now: datetime, half_life_days: int
) -> float:
    """Sub-04 §11.5 — half-life decay, clamped to [RECENCY_FACTOR_FLOOR, 1.0].

    Returns 1.0 at age=0 and falls to 0.5 at age=half_life_days exactly.
    Clamps at RECENCY_FACTOR_FLOOR to avoid div-zero downstream and to keep
    the value comparable in cursor encodings.
    """
    if half_life_days <= 0:
        return 1.0
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = (now - detected_at).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)
    factor = 0.5 ** (age_days / half_life_days)
    if math.isnan(factor) or math.isinf(factor):
        return RECENCY_FACTOR_FLOOR
    return max(RECENCY_FACTOR_FLOOR, min(1.0, factor))


# ---------------------------------------------------------------------------
# Parsing / shape helpers
# ---------------------------------------------------------------------------


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


def _parse_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _load_closure(kind: str, raw_json: str | None) -> ClosureCondition:
    data = _parse_json_dict(raw_json)
    data.setdefault("kind", kind)
    mapping = {
        "drift_signal_clears": ClosureDriftSignalClears,
        "memory_op_applied": ClosureMemoryOpApplied,
        "artifact_exists": ClosureArtifactExists,
        "manual_attest": ClosureManualAttest,
    }
    cls = mapping.get(kind)
    if cls is None:
        return ClosureManualAttest(instruction="legacy closure (unknown kind)")
    try:
        if cls is ClosureArtifactExists and "selector" in data:
            sel = data["selector"]
            if isinstance(sel, dict):
                data["selector"] = ArtifactSelector(**sel)
        return cls.model_validate(data)
    except Exception:
        if cls is ClosureArtifactExists:
            return ClosureArtifactExists(
                artifact_kind="task",
                selector=ArtifactSelector(),
            )
        if cls is ClosureManualAttest:
            return ClosureManualAttest(instruction="legacy closure (parse error)")
        if cls is ClosureMemoryOpApplied:
            return ClosureMemoryOpApplied(memory_operation_id="unknown")
        return ClosureDriftSignalClears(drift_signal_id="unknown")


def _load_owner_hint(raw_json: str | None) -> OwnerHint | None:
    data = _parse_json_dict(raw_json)
    if not data:
        return None
    try:
        return OwnerHint.model_validate(data)
    except Exception:
        return None


def _row_to_finding(
    row: aiosqlite.Row, evidence: list[str]
) -> Finding:
    return Finding(
        finding_id=row["finding_id"],
        run_id=row["run_id"],
        cycle_key=row["cycle_key"],
        detected_at=_parse_iso(row["detected_at"]) or datetime.now(timezone.utc),
        finding_type=row["finding_type"],
        schema_version=row["schema_version"],
        scope_type=row["scope_type"],
        scope_key=row["scope_key"],
        program_key=row["program_key"],
        title=row["title"],
        summary=row["summary"],
        why_now=row["why_now"],
        evidence=evidence,
        evidence_hash=row["evidence_hash"],
        involved_projects=[
            str(p) for p in _parse_json_list(row["involved_projects_json"])
        ],
        suggested_artifact=row["suggested_artifact"],
        owner_hint=_load_owner_hint(row["owner_hint_json"]),
        closure_condition=_load_closure(
            row["closure_condition_kind"], row["closure_condition_json"]
        ),
        closure_condition_human=row["closure_condition_human"],
        severity=row["severity"],
        confidence=row["confidence"],
        approval_state=row["approval_state"],
        regression_of_finding_id=row["regression_of_finding_id"],
        proposal_fingerprint=row["proposal_fingerprint"],
        recurrence_count=row["recurrence_count"] or 1,
        first_seen_cycle_key=row["first_seen_cycle_key"],
        last_seen_cycle_key=row["last_seen_cycle_key"],
        applied_artifact_ref=row["applied_artifact_ref"],
        applied_at=_parse_iso(row["applied_at"]),
        applied_by_user_id=row["applied_by_user_id"],
        expires_at=_parse_iso(row["expires_at"]) or datetime.now(timezone.utc),
        superseded_by_finding_id=row["superseded_by_finding_id"],
        recency_factor=None,
    )


def _redact(f: Finding) -> FindingRedacted:
    return FindingRedacted(
        finding_id=f.finding_id,
        cycle_key=f.cycle_key,
        finding_type=f.finding_type,
        severity=f.severity,
    )


def _is_visible(visible: set[str] | None, involved_projects: list[str]) -> bool:
    if visible is None:
        return True
    if not involved_projects:
        return True
    return set(involved_projects).issubset(visible)


def _evidence_prefix(kind: str) -> str:
    mapping = {
        "digest_event": "digest_event",
        "drift_signal": "drift_signal",
        "journal_entry": "journal_entry",
        "memory_op": "memory_op",
        "handoff": "handoff",
        "learning": "learning",
        "kg_node": "kg_node",
        "audit_log": "audit_log",
        "task": "task",
        "pr": "pr",
        "commit": "commit",
    }
    return mapping.get(kind, kind)


async def _fetch_evidence(
    db: aiosqlite.Connection, finding_id: str
) -> list[str]:
    rows = await (
        await db.execute(
            "SELECT evidence_kind, evidence_ref, position"
            " FROM brain_finding_evidence"
            " WHERE finding_id = ?"
            " ORDER BY position ASC",
            (finding_id,),
        )
    ).fetchall()
    out: list[str] = []
    for r in rows:
        get = (lambda key: r[key]) if hasattr(r, "keys") else None
        if get is not None:
            kind = get("evidence_kind")
            ref = get("evidence_ref")
        else:
            kind, ref, _ = r
        out.append(f"{_evidence_prefix(kind)}:{ref}")
    return out


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
                "SELECT run_id, cycle_key FROM brain_runs"
                " WHERE workspace_id = ? AND status IN ('succeeded','partial')"
                "  AND superseded_by_run_id IS NULL"
                " ORDER BY cycle_key DESC, started_at DESC LIMIT 1",
                (workspace_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "cycle_key": row[1]}
    row = await (
        await db.execute(
            "SELECT run_id, cycle_key FROM brain_runs"
            " WHERE workspace_id = ? AND cycle_key = ?"
            "  AND status IN ('succeeded','partial')"
            "  AND superseded_by_run_id IS NULL"
            " ORDER BY started_at DESC LIMIT 1",
            (workspace_id, cycle_key),
        )
    ).fetchone()
    if row is None:
        return None
    return {"run_id": row[0], "cycle_key": row[1]}


# ---------------------------------------------------------------------------
# Cursor (stable sort signature — §11.5 CE2 inserts recency_factor only)
# ---------------------------------------------------------------------------


def _encode_cursor(
    severity_rank: int,
    confidence_rank: int,
    recurrence_count: int,
    recency_factor: float,
    detected_at: str,
    finding_id: str,
) -> str:
    payload = json.dumps(
        {
            "sr": severity_rank,
            "cr": confidence_rank,
            "rc": recurrence_count,
            "rf": round(recency_factor, 6),
            "d": detected_at,
            "i": finding_id,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, int, int, float, str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        p = json.loads(raw)
        return (
            int(p["sr"]),
            int(p["cr"]),
            int(p["rc"]),
            float(p.get("rf", 1.0)),
            str(p["d"]),
            str(p["i"]),
        )
    except (
        KeyError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None


def _sort_tuple(
    *,
    severity: str,
    confidence: str,
    recurrence_count: int,
    recency_factor: float,
    detected_at: datetime,
    finding_id: str,
    decay_enabled: bool,
) -> tuple:
    """Stable sort key — sub-04 §11.1 (decay disabled) / §11.5 (decay enabled).

    Higher = earlier in result list. We negate counts so DESC ordering falls
    out of natural tuple comparison.
    """
    sr = _SEVERITY_RANK.get(severity, 0)
    cr = _CONFIDENCE_RANK.get(confidence, 0)
    # Always six-element tuple. When decay is OFF the recency_factor
    # position carries 1.0 for every row, collapsing to a no-op tiebreaker —
    # detected_at remains the active tiebreaker. Invariant #11 holds:
    # disabled mode preserves the sub-04 §11.1 baseline ordering.
    rf = recency_factor if decay_enabled else 1.0
    return (
        -sr,
        -cr,
        -recurrence_count,
        -rf,
        -(detected_at.timestamp() if detected_at else 0.0),
        finding_id,
    )


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


async def list_findings(
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    finding_types: list[str] | None = None,
    severity_min: str | None = None,
    confidence_min: str | None = None,
    approval_states: list[str] | None = None,
    include_terminal: bool = False,
    recurrence_min: int = 1,
    regression_only: bool = False,
    applied: bool | None = None,
    created_after: datetime | None = None,
    owner_user_id: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: UserInfo | None = None,
    workspace_id: str = "ws_default",
    now: datetime | None = None,
) -> FindingsListResponse:
    """Paginated findings list with §11.1 filters + visibility + CE2 decay."""
    limit = max(1, min(MAX_LIMIT, int(limit)))
    over_fetch = limit + 1
    states_to_apply = list(approval_states) if approval_states else list(_DEFAULT_STATES_AGENT)
    if include_terminal:
        for extra in _TERMINAL_STATES_INCLUDE:
            if extra not in states_to_apply:
                states_to_apply.append(extra)

    severity_min = severity_min if severity_min in _SEVERITY_RANK else "low"
    confidence_min = confidence_min if confidence_min in _CONFIDENCE_RANK else "low"
    sev_threshold = _SEVERITY_RANK[severity_min]
    conf_threshold = _CONFIDENCE_RANK[confidence_min]

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        run = await _resolve_run(
            db, cycle_key=cycle_key, run_id=run_id, workspace_id=workspace_id
        )
        if run is None:
            return FindingsListResponse(items=[], total_returned=0)

        decay_enabled, half_life_days = await load_decay_settings(db)
        visible = await get_visible_projects(db, user, workspace_id) if user else None

        where = ["f.run_id = ?"]
        params: list[Any] = [run["run_id"]]
        if scope_type:
            where.append("f.scope_type = ?")
            params.append(scope_type)
        if scope_key:
            where.append("f.scope_key = ?")
            params.append(scope_key)
        if finding_types:
            placeholders = ",".join("?" for _ in finding_types)
            where.append(f"f.finding_type IN ({placeholders})")
            params.extend(finding_types)
        if states_to_apply:
            placeholders = ",".join("?" for _ in states_to_apply)
            where.append(f"f.approval_state IN ({placeholders})")
            params.extend(states_to_apply)
        if recurrence_min > 1:
            where.append("f.recurrence_count >= ?")
            params.append(recurrence_min)
        if regression_only:
            where.append("f.regression_of_finding_id IS NOT NULL")
        if applied is True:
            where.append("f.applied_artifact_ref IS NOT NULL")
        elif applied is False:
            where.append("f.applied_artifact_ref IS NULL")
        if created_after is not None:
            where.append("f.created_at > ?")
            params.append(created_after.astimezone(timezone.utc).isoformat())
        if owner_user_id:
            where.append("json_extract(f.owner_hint_json, '$.user_id') = ?")
            params.append(owner_user_id)
        # severity/confidence min applied in-Python (rank order not in SQL).

        # Over-fetch a window and sort in Python so the recency_factor
        # tiebreaker stays consistent with cursor decoding. We pull more
        # than `over_fetch` rows so threshold-filtered eliminations don't
        # truncate the page artificially. Cap at 1000 to bound memory.
        sql_limit = min(1000, over_fetch * 4 + 50)
        query = (
            "SELECT f.* FROM brain_findings f "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY f.created_at DESC, f.finding_id ASC "
            "LIMIT ?"
        )
        params.append(sql_limit)
        rows = await (await db.execute(query, params)).fetchall()

        # Build ranked rows.
        materialized: list[tuple[tuple, Finding | None, aiosqlite.Row]] = []
        for row in rows:
            if _SEVERITY_RANK.get(row["severity"], 0) < sev_threshold:
                continue
            if _CONFIDENCE_RANK.get(row["confidence"], 0) < conf_threshold:
                continue
            detected = _parse_iso(row["detected_at"]) or now
            rf = _recency_factor(detected, now, half_life_days)
            key = _sort_tuple(
                severity=row["severity"],
                confidence=row["confidence"],
                recurrence_count=row["recurrence_count"] or 1,
                recency_factor=rf,
                detected_at=detected,
                finding_id=row["finding_id"],
                decay_enabled=decay_enabled,
            )
            materialized.append((key, None, row))
        materialized.sort(key=lambda t: t[0])

        # Cursor walking (skip rows <= cursor key).
        cursor_tuple: tuple | None = None
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded:
                csr, ccr, crc, crf, cdetected, cid = decoded
                cdetected_dt = _parse_iso(cdetected) or now
                cursor_tuple = (
                    -csr,
                    -ccr,
                    -crc,
                    -(crf if decay_enabled else 1.0),
                    -(cdetected_dt.timestamp() if cdetected_dt else 0.0),
                    cid,
                )

        page: list[Finding | FindingRedacted] = []
        next_cursor: str | None = None
        redacted_count = 0
        redacted_evidence_count = 0
        produced = 0
        used = 0
        last_visible: tuple | None = None
        last_visible_finding: Finding | None = None
        for key, _, row in materialized:
            if cursor_tuple is not None and key <= cursor_tuple:
                continue
            used += 1
            evidence = await _fetch_evidence(db, row["finding_id"])
            finding = _row_to_finding(row, evidence)
            detected_for_factor = finding.detected_at or now
            rf = _recency_factor(detected_for_factor, now, half_life_days)
            if decay_enabled:
                finding = finding.model_copy(update={"recency_factor": rf})
            if not _is_visible(visible, finding.involved_projects):
                redacted_count += 1
                page.append(_redact(finding))
            else:
                page.append(finding)
                last_visible = key
                last_visible_finding = finding
            produced += 1
            if produced >= limit:
                # peek for next
                break
        if produced >= limit and used < len(materialized):
            anchor = last_visible
            anchor_finding = last_visible_finding
            if anchor is not None and anchor_finding is not None:
                next_cursor = _encode_cursor(
                    severity_rank=_SEVERITY_RANK[anchor_finding.severity],
                    confidence_rank=_CONFIDENCE_RANK[anchor_finding.confidence],
                    recurrence_count=anchor_finding.recurrence_count,
                    recency_factor=anchor_finding.recency_factor or 1.0,
                    detected_at=anchor_finding.detected_at.astimezone(timezone.utc).isoformat(),
                    finding_id=anchor_finding.finding_id,
                )

    return FindingsListResponse(
        items=page,
        next_cursor=next_cursor,
        cycle_key=run["cycle_key"],
        run_id=run["run_id"],
        redacted_count=redacted_count,
        redacted_evidence_count=redacted_evidence_count,
        total_returned=len(page),
    )


async def fetch_single_finding(
    *,
    finding_id: str,
    user: UserInfo | None,
    workspace_id: str = "ws_default",
    now: datetime | None = None,
) -> Finding | None:
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                "SELECT * FROM brain_findings WHERE finding_id = ?",
                (finding_id,),
            )
        ).fetchone()
        if row is None:
            return None
        evidence = await _fetch_evidence(db, finding_id)
        visible = await get_visible_projects(db, user, workspace_id) if user else None
        decay_enabled, half_life_days = await load_decay_settings(db)
    finding = _row_to_finding(row, evidence)
    if not _is_visible(visible, finding.involved_projects):
        return None
    if decay_enabled:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        rf = _recency_factor(finding.detected_at or now, now, half_life_days)
        finding = finding.model_copy(update={"recency_factor": rf})
    return finding


# ---------------------------------------------------------------------------
# Lifecycle PATCH (operator/admin/super_admin)
# ---------------------------------------------------------------------------


_ALLOWED_TRANSITIONS: dict[FindingApprovalState, set[FindingApprovalState]] = {
    "open": {"approved", "dismissed", "resolved"},
    "approved": {"resolved"},
    "dismissed": set(),
    "resolved": set(),
    "superseded": set(),
    "expired": set(),
}


class LifecycleConflict(Exception):
    """Raised when target state is invalid from current state (sub-04 §8)."""

    def __init__(self, current: str, attempted: str):
        super().__init__(f"lifecycle: {current} → {attempted} forbidden")
        self.current = current
        self.attempted = attempted


class FindingValidationError(Exception):
    """Raised when a precondition on the request body fails."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"validation: {kind} ({detail})")
        self.kind = kind
        self.detail = detail


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def apply_lifecycle_patch(
    *,
    finding_id: str,
    action: FindingPatchAction,
    reason: str | None,
    applied_artifact_ref: str | None,
    user: UserInfo,
    workspace_id: str = "ws_default",
    now: datetime,
    idempotency_key: str | None = None,
) -> Finding | None:
    """PATCH lifecycle transition. Idempotent on same target state.

    Per sub-04 §8 / §11.1:
      * dismissed requires non-empty reason.
      * resolved on a manual_attest closure requires non-empty reason.
    """
    target_state: FindingApprovalState = action  # type: ignore[assignment]
    existing = await fetch_single_finding(
        finding_id=finding_id, user=user, workspace_id=workspace_id, now=now,
    )
    if existing is None:
        return None
    current = existing.approval_state

    if target_state == "dismissed" and not (reason and reason.strip()):
        raise FindingValidationError("reason_required", "dismissed")
    if (
        target_state == "resolved"
        and existing.closure_condition.kind == "manual_attest"
        and not (reason and reason.strip())
    ):
        raise FindingValidationError("reason_required", "manual_attest")

    if current == target_state:
        return existing

    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if target_state not in allowed:
        raise LifecycleConflict(current=current, attempted=target_state)

    iso_now = _utc_iso(now)
    async with write_db() as db:
        await db.execute(
            "UPDATE brain_findings SET approval_state = ?,"
            "  applied_artifact_ref = COALESCE(?, applied_artifact_ref),"
            "  applied_at = CASE WHEN ? IS NOT NULL THEN ? ELSE applied_at END,"
            "  applied_by_user_id = CASE WHEN ? IS NOT NULL THEN ? ELSE applied_by_user_id END"
            " WHERE finding_id = ?",
            (
                target_state,
                applied_artifact_ref,
                applied_artifact_ref,
                iso_now,
                applied_artifact_ref,
                user.user_id,
                finding_id,
            ),
        )
        await db.execute(
            "INSERT INTO brain_finding_states ("
            " state_id, finding_id, from_state, to_state, actor_user_id,"
            " reason, applied_artifact_ref"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                finding_id,
                current,
                target_state,
                user.user_id,
                reason,
                applied_artifact_ref,
            ),
        )
    return await fetch_single_finding(
        finding_id=finding_id, user=user, workspace_id=workspace_id, now=now,
    )


async def apply_bulk_patch(
    *,
    finding_ids: list[str],
    action: FindingPatchAction,
    reason: str | None,
    user: UserInfo,
    workspace_id: str = "ws_default",
    now: datetime,
    idempotency_key: str | None = None,
) -> FindingBulkPatchResponse:
    """Bulk PATCH — cap 25 (sub-04 §11.1).

    All-or-nothing for lifecycle conflicts: if ANY transition is invalid the
    whole batch fails with 409. Manual-attest resolves are rejected — each
    requires individual attestation text (anti-pattern §13 last row).
    """
    if len(finding_ids) > BULK_PATCH_MAX:
        raise FindingValidationError("bulk_cap_exceeded", str(len(finding_ids)))
    if len(finding_ids) == 0:
        raise FindingValidationError("bulk_empty", "finding_ids must be non-empty")
    if action == "dismissed" and not (reason and reason.strip()):
        raise FindingValidationError("reason_required", "dismissed")

    # Pre-validate every finding before mutating any.
    snapshots: list[Finding] = []
    for fid in finding_ids:
        f = await fetch_single_finding(
            finding_id=fid, user=user, workspace_id=workspace_id, now=now,
        )
        if f is None:
            raise FindingValidationError("not_found_or_invisible", fid)
        if action == "resolved" and f.closure_condition.kind == "manual_attest":
            raise FindingValidationError("bulk_manual_attest", fid)
        allowed = _ALLOWED_TRANSITIONS.get(f.approval_state, set())
        if action != f.approval_state and action not in allowed:
            raise LifecycleConflict(current=f.approval_state, attempted=action)
        snapshots.append(f)

    results: list[FindingBulkResultEntry] = []
    applied = 0
    skipped = 0
    for f in snapshots:
        if action == f.approval_state:
            results.append(
                FindingBulkResultEntry(finding_id=f.finding_id, status="skipped")
            )
            skipped += 1
            continue
        await apply_lifecycle_patch(
            finding_id=f.finding_id,
            action=action,
            reason=reason,
            applied_artifact_ref=None,
            user=user,
            workspace_id=workspace_id,
            now=now,
            idempotency_key=idempotency_key,
        )
        results.append(
            FindingBulkResultEntry(finding_id=f.finding_id, status=action)
        )
        applied += 1

    return FindingBulkPatchResponse(
        results=results, applied_count=applied, skipped_count=skipped
    )


# ---------------------------------------------------------------------------
# Apply guidance (NO writes — returns next_action only, sub-04 §11.1 / F1)
# ---------------------------------------------------------------------------


class ApplyPreconditionError(Exception):
    """Raised when the finding is not in a state that supports apply."""

    def __init__(self, kind: str, current_state: str):
        super().__init__(f"apply precondition: {kind} (state={current_state})")
        self.kind = kind
        self.current_state = current_state


def _next_action_for(finding: Finding) -> dict[str, Any]:
    """Server-side mapping table (sub-04 §11.1 apply guidance)."""
    must_include = f"brain_finding:{finding.finding_id}"
    sa = finding.suggested_artifact
    ft = finding.finding_type

    project = finding.scope_key if finding.scope_type == "project" else "marvisx"
    tool: str | None = None
    args: dict[str, Any] = {}
    target_path: str | None = None
    body: str | None = None
    rationale = ""

    if sa == "task":
        tool = "mcp__marvis__create_task"
        priority_for_severity = {
            "low": "low", "medium": "medium", "high": "high", "critical": "critical",
        }
        args = {
            "title": finding.title,
            "description": finding.summary,
            "project": project,
            "priority": priority_for_severity.get(finding.severity, "medium"),
            "delegation": "hybrid",
            "impact": 5,
            "confidence": _CONFIDENCE_RANK.get(finding.confidence, 1) * 3,
            "ease": 5,
            "source": "session",
            "tags": [must_include],
        }
        rationale = (
            f"{ft} → task with severity={finding.severity}, "
            f"confidence={finding.confidence}, recurrence={finding.recurrence_count}."
        )
    elif sa == "learning":
        tool = "mcp__marvis__create_learning"
        args = {
            "title": finding.title,
            "category": "process",
            "description": finding.summary,
            "prevention": finding.why_now,
            "severity": finding.severity,
            "module": None,
            "project": project,
            "tags": [must_include],
        }
        rationale = "promotion_candidate / idea → learning artifact."
    elif sa == "adr":
        tool = None
        target_path = f"docs/adrs/{finding.cycle_key}-{finding.finding_id[:8]}.md"
        body = (
            f"# ADR — {finding.title}\n\n"
            f"## Context\n\n{finding.why_now}\n\n"
            f"## Decision\n\n_TODO_\n\n"
            f"## Consequences\n\n_TODO_\n\n"
            f"## Evidence\n\n- " + "\n- ".join(finding.evidence)
        )
        rationale = "open_question / decision gap → manual ADR scaffold."
    elif sa == "guide":
        tool = None
        target_path = f"docs/guides/brain-{finding.finding_id[:8]}.md"
        body = (
            f"# Guide — {finding.title}\n\n"
            f"## Why now\n\n{finding.why_now}\n\n"
            f"## Steps\n\n_TODO_\n\n"
            f"## Evidence\n\n- " + "\n- ".join(finding.evidence)
        )
        rationale = "procedure_change → manual guide scaffold (Edit)."
    elif sa == "status_update":
        tool = None
        target_path = (
            f"projects/{project}/context.md"
            if finding.scope_type == "project"
            else "context.md"
        )
        body = (
            f"## Brain finding {finding.finding_id[:8]} — {finding.title}\n\n"
            f"{finding.summary}\n"
        )
        rationale = "scope_gap → context.md append (manual Edit)."
    elif sa == "question":
        tool = None
        rationale = (
            "open_question → no canonical tool. Answer via closure_condition_human "
            "on resolve PATCH."
        )
    else:
        rationale = "No canonical apply path. Operator inspects evidence."

    return {
        "tool": tool,
        "args": args,
        "rationale": rationale,
        "must_include_in_tags": must_include,
        "target_path": target_path,
        "body": body,
    }


async def get_apply_guidance(
    *,
    finding_id: str,
    user: UserInfo,
    workspace_id: str = "ws_default",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    finding = await fetch_single_finding(
        finding_id=finding_id, user=user, workspace_id=workspace_id, now=now,
    )
    if finding is None:
        return None
    if finding.approval_state != "approved":
        raise ApplyPreconditionError("not_approved", finding.approval_state)
    if finding.applied_artifact_ref:
        raise ApplyPreconditionError("already_applied", finding.approval_state)
    next_action = _next_action_for(finding)
    finding_summary = {
        "finding_type": finding.finding_type,
        "title": finding.title,
        "owner_hint": finding.owner_hint.model_dump() if finding.owner_hint else None,
        "closure_condition": finding.closure_condition.model_dump(),
        "severity": finding.severity,
        "confidence": finding.confidence,
        "recurrence_count": finding.recurrence_count,
    }
    return {
        "finding_id": finding.finding_id,
        "next_action": next_action,
        "finding_summary": finding_summary,
    }


__all__ = [
    "ApplyPreconditionError",
    "BULK_PATCH_MAX",
    "DEFAULT_LIMIT",
    "DEFAULT_RECENCY_HALF_LIFE_DAYS",
    "FindingValidationError",
    "LifecycleConflict",
    "MAX_LIMIT",
    "RECENCY_FACTOR_FLOOR",
    "_recency_factor",
    "apply_bulk_patch",
    "apply_lifecycle_patch",
    "fetch_single_finding",
    "get_apply_guidance",
    "list_findings",
    "load_decay_settings",
]
