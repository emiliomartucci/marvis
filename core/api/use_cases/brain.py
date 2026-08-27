# v1.0.0 - 2026-05-27 - S1 F1.9: brain use_cases extracted from router
"""Brain v1 use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Sibling of :mod:`core.api.use_cases.learnings` (the S1 "collapse runtime"
TEMPLATE). One pure async function per Brain surface operation
(events / drift / memory-operations / findings / journal / runs / counters /
recompute / capabilities + the Wave-3.1 admin endpoints). The HTTP router becomes
a thin adapter that resolves identity into a :class:`CallerContext`, calls these
functions, and maps :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http`` (or, for the structured-detail bodies the Brain
contract pins, via the local ``_to_http_brain`` helper). The Python MCP surface
(later) calls the SAME functions with ``CallerContext.local_single_user()``.
One implementation, no fork.

Four adapter responsibilities (mirrors the learnings template docstring):

RESPONSIBILITY 1 — identity resolution. The adapter builds a ``CallerContext``
    via ``from_user_info`` and ALSO forwards the original ``UserInfo`` as the
    ``user=`` keyword. Brain readers/writers resolve project visibility
    internally through ``core.api.visibility.get_visible_projects(db, user, ...)``
    which needs ``UserInfo.teams`` — a field ``CallerContext`` intentionally
    drops (identity-expansion stays at the transport boundary). So the use_case
    takes ``ctx`` for its OWN logic (RBAC + validation) but passes the full
    ``user`` straight through to the services. ``UserInfo`` is a fastapi-free
    pydantic model, so importing it here keeps the contract intact. The MCP/local
    surface passes ``user=None`` (local operator sees all; services treat
    ``user is None`` as "no visibility restriction").

RESPONSIBILITY 2 — error translation. Brain raises TWO error-BODY shapes; both
    travel as a :class:`BrainDetailError` so the adapter can reproduce the legacy
    ``HTTPException(status, detail=<body>)`` byte-identically:
    * Plain-STRING ``{"detail": "<string>"}`` bodies (the 404 ``"Not found"`` and
      the 400 ``"Pass cycle_key OR run_id, not both"``) → ``brain_detail`` is the
      raw string. The bare 404s also use :class:`NotFoundError` whose plain
      ``message`` is replayed as ``detail`` (no structured body, no ``error_kind``).
    * Structured ``{"detail": {"error_kind": ...}}`` bodies (recompute idempotency,
      lifecycle conflicts, apply preconditions, bulk caps, finding validation,
      promote-blocked-state, invalid ISO, invalid confidence/timestamp) →
      ``brain_detail`` is the structured dict.
    Either way the adapter ALWAYS catches ServiceError locally and runs
    ``_to_http_brain``: the global ``ServiceError`` handler would emit a top-level
    ``{"code","message"}`` (NOT nested under ``"detail"``), which the brain HTTP
    tests pin against — so it must never reach that handler.

RESPONSIBILITY 3 — function-local service imports. Every brain service
    (events_reader, drift_router, memory_ops, findings_reader, runs_reader,
    journal, capabilities, jobs, watermarks, recap, cycle, llm.router_glue)
    transitively imports ``fastapi`` at module top (via ``core.api.db`` /
    ``core.api.visibility``). So this module imports them FUNCTION-LOCAL inside
    each use_case (the search.py pattern) to keep the use_case fastapi-free at
    import time — the property the import-linter contract + the smoke test assert.

RESPONSIBILITY 4 — the ``*_apply`` GUIDANCE-only invariant. The two apply
    endpoints (memory-operation apply, finding apply) NEVER write: they call the
    services' ``get_apply_guidance`` which only READS preconditions and returns a
    next-action envelope. That property is preserved verbatim here — these
    use_cases open no write transaction and mutate nothing. The adapter behavior
    (404/409 mapping) is unchanged.

DECISION (request models stay in the router). The router-defined Pydantic request
    bodies (``WatermarkResetRequest``, ``PromoteFindingToTaskRequest`` …) and the
    response envelopes for the admin endpoints stay declared in the router (HTTP
    request/response contract surface). This module owns the pure execution logic
    those handlers delegate to.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from core.api.use_cases._context import CallerContext, require_role_ctx
from core.api.use_cases._errors import ServiceError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from core.api.models.auth import UserInfo
    from core.api.models.brain import (
        ApplyResponse,
        BrainCapabilities,
        BrainRun,
        DriftListResponse,
        DriftSignal,
        EventsListResponse,
        Finding,
        FindingBulkPatchResponse,
        FindingsListResponse,
        JournalListResponse,
        MemoryOperation,
        MemoryOperationsListResponse,
        PipelineCounters,
        RecomputeResponse,
        RunsListResponse,
    )


# ---------------------------------------------------------------------------
# Domain error carrying a structured ``detail`` body (Brain HTTP contract parity)
# ---------------------------------------------------------------------------


class BrainDetailError(ServiceError):
    """A ServiceError whose HTTP body is a STRUCTURED dict, not ``{code,message}``.

    Brain endpoints historically returned ``HTTPException(status, detail={...})``
    bodies that the HTTP tests pin (e.g. ``body["detail"]["error_kind"]``). The
    adapter's ``_to_http_brain`` reproduces ``HTTPException(http_status,
    detail=brain_detail)`` exactly for these, instead of routing through the
    generic ``to_http`` (which would change the body shape). The MCP surface still
    sees ``code`` + ``message`` (``http_status`` is only an HTTP hint).
    """

    def __init__(self, *, http_status: int, code: str, message: str, brain_detail: Any) -> None:
        self.http_status = http_status
        self.brain_detail = brain_detail
        super().__init__(code=code, message=message)


# ---------------------------------------------------------------------------
# Pure helpers (lifted verbatim from the router)
# ---------------------------------------------------------------------------


def _cycle_or_run_error() -> BrainDetailError:
    """The shared ``cycle_key`` XOR ``run_id`` 400 (legacy PLAIN-STRING detail).

    The baseline router raised ``HTTPException(400, detail="Pass cycle_key OR
    run_id, not both")`` — a plain string, not a structured dict. Carrying that
    string as ``brain_detail`` makes ``_to_http_brain`` reproduce the body and the
    400 status byte-identically.
    """
    return BrainDetailError(
        http_status=400,
        code="cycle_key_or_run_id",
        message="Pass cycle_key OR run_id, not both",
        brain_detail="Pass cycle_key OR run_id, not both",
    )


def _split_csv(values: list[str] | None) -> list[str] | None:
    """Split a list of (possibly comma-joined) query values into clean tokens."""
    if not values:
        return None
    out: list[str] = []
    for v in values:
        for token in str(v).split(","):
            token = token.strip()
            if token:
                out.append(token)
    return out or None


def _parse_iso_query(value: str | None) -> datetime | None:
    """Parse an ISO-8601 query param. Raises ``BrainDetailError`` (400) on garbage.

    Mirrors the router's ``_parse_iso_query`` 400 body exactly:
    ``{"error_kind": "invalid_timestamp", "param": "created_after", "value": …}``.
    """
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrainDetailError(
            http_status=400,
            code="invalid_timestamp",
            message=f"Invalid timestamp: {value}",
            brain_detail={
                "error_kind": "invalid_timestamp",
                "param": "created_after",
                "value": value,
            },
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_SEVERITY_TO_IMPACT: dict[str, int] = {
    "low": 4,
    "medium": 6,
    "high": 8,
    "critical": 10,
}


def build_promote_task_description(*, finding: "Finding", override: str | None) -> str:
    """Compose a Marvis-style task description from finding fields.

    Format follows the project convention (see CLAUDE.md "Task Description
    Format"): ``Devo {azione} perche {motivazione}. Attenzione a {dipendenze}.``
    """
    if override:
        return override
    azione = f"chiudere il finding Brain '{finding.title}' su scope {finding.scope_type}={finding.scope_key}"
    motivazione = (finding.summary or finding.why_now or "drift osservato dal Brain").strip()
    attenzione = (finding.why_now or "verifica evidence + invariants").strip()
    refs_line = ""
    if finding.evidence:
        refs_line = "\n" + "\n".join(f"-{r}" for r in finding.evidence[:5])
    return (
        f"Devo {azione} perche {motivazione}. Attenzione a {attenzione}."
        f"\n-/data/projects/{finding.scope_key}"
        f"\n-brain:finding:{finding.finding_id}"
        f"{refs_line}"
    )


# ---------------------------------------------------------------------------
# Sub-01 D6 — digest events
# ---------------------------------------------------------------------------


async def list_events(
    ctx: CallerContext,
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    event_type: list | None = None,
    source_project: str | None = None,
    cursor: str | None = None,
    limit: int,
    user: "UserInfo | None" = None,
) -> "EventsListResponse":
    """Paginated digest events for a cycle, visibility-filtered (any auth caller).

    Raises :class:`BrainDetailError` (400, legacy PLAIN-STRING body
    ``"Pass cycle_key OR run_id, not both"``) when both ``cycle_key`` and
    ``run_id`` are supplied.
    """
    if cycle_key and run_id:
        raise _cycle_or_run_error()
    from core.api.services.brain.events_reader import list_events_for_cycle

    return await list_events_for_cycle(
        cycle_key=cycle_key,
        run_id=run_id,
        event_type=event_type,
        source_project=source_project,
        cursor=cursor,
        limit=limit,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
    )


# ---------------------------------------------------------------------------
# Sub-02 Drift Checker (L3)
# ---------------------------------------------------------------------------


async def list_drift(
    ctx: CallerContext,
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    signal_type: list | None = None,
    knowledge_form: list | None = None,
    severity_min: str = "low",
    confidence_min: float = 0.0,
    state: list | None = None,
    include_resolved: bool = False,
    drift_axis: list[str] | None = None,
    rule_id: list[str] | None = None,
    cursor: str | None = None,
    limit: int,
    user: "UserInfo | None" = None,
) -> "DriftListResponse":
    """List drift signals (any auth caller). ``state`` defaults to ``['open']``."""
    if cycle_key and run_id:
        raise _cycle_or_run_error()
    from core.api.services.brain.drift_router import list_drift_signals

    states = list(state) if state else ["open"]
    if include_resolved and "resolved" not in states:
        states.append("resolved")
    return await list_drift_signals(
        cycle_key=cycle_key,
        run_id=run_id,
        scope_type=scope_type,
        scope_key=scope_key,
        signal_types=signal_type,
        knowledge_forms=knowledge_form,
        severity_min=severity_min,
        confidence_min=confidence_min,
        states=states,
        drift_axes=drift_axis,
        rule_ids=rule_id,
        cursor=cursor,
        limit=limit,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
    )


async def get_drift_signal(
    ctx: CallerContext,
    *,
    signal_id: str,
    user: "UserInfo | None" = None,
) -> "DriftSignal":
    """Fetch a single drift signal. Raises :class:`NotFoundError` if absent/invisible."""
    from core.api.services.brain.drift_router import fetch_single_drift_signal
    from core.api.use_cases._errors import NotFoundError

    result = await fetch_single_drift_signal(
        signal_id=signal_id,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
    )
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    return result


async def patch_drift_signal(
    ctx: CallerContext,
    *,
    signal_id: str,
    action: str,
    reason: str | None = None,
    user: "UserInfo | None" = None,
) -> "DriftSignal":
    """Drift lifecycle action (operator+). Raises :class:`NotFoundError` if absent."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.drift_router import apply_drift_patch
    from core.api.use_cases._errors import NotFoundError

    result = await apply_drift_patch(
        signal_id=signal_id,
        action=action,
        reason=reason,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
        now=datetime.now(timezone.utc),
    )
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    return result


# ---------------------------------------------------------------------------
# Sub-03 Memory Operations (L4)
# ---------------------------------------------------------------------------


async def list_memory_operations(
    ctx: CallerContext,
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    operation_type: list[str] | None = None,
    approval_state: list[str] | None = None,
    include_terminal: bool = False,
    recurrence_min: int = 1,
    score_min: float = 0.0,
    cursor: str | None = None,
    limit: int,
    user: "UserInfo | None" = None,
) -> "MemoryOperationsListResponse":
    """List memory operations (any auth caller). ``approval_state`` defaults to ``['pending']``."""
    if cycle_key and run_id:
        raise _cycle_or_run_error()
    from core.api.services.brain.memory_ops import (
        list_memory_operations as _list_memory_operations,
    )

    states = list(approval_state) if approval_state else ["pending"]
    if include_terminal:
        for extra in ("approved", "dismissed", "rejected"):
            if extra not in states:
                states.append(extra)
    op_types = _split_csv(operation_type)
    return await _list_memory_operations(
        cycle_key=cycle_key,
        run_id=run_id,
        scope_type=scope_type,
        scope_key=scope_key,
        operation_types=op_types,
        approval_states=states,
        recurrence_min=recurrence_min,
        score_min=score_min,
        cursor=cursor,
        limit=limit,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
    )


async def get_memory_operation(
    ctx: CallerContext,
    *,
    operation_id: str,
    user: "UserInfo | None" = None,
) -> "MemoryOperation":
    """Fetch a single memory operation. Raises :class:`NotFoundError` if absent."""
    from core.api.services.brain.memory_ops import fetch_single_operation
    from core.api.use_cases._errors import NotFoundError

    result = await fetch_single_operation(
        operation_id=operation_id,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
    )
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    return result


async def patch_memory_operation(
    ctx: CallerContext,
    *,
    operation_id: str,
    action: str,
    reason: str | None = None,
    applied_artifact_ref: str | None = None,
    idempotency_key: str | None = None,
    user: "UserInfo | None" = None,
) -> "MemoryOperation":
    """Memory op lifecycle action (operator+).

    Raises :class:`BrainDetailError` (409, ``error_kind=lifecycle_conflict``) on a
    forward-only conflict, or :class:`NotFoundError` if the op is absent.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.memory_ops import (
        LifecycleConflict,
        apply_lifecycle_patch,
    )
    from core.api.use_cases._errors import NotFoundError

    try:
        result = await apply_lifecycle_patch(
            operation_id=operation_id,
            action=action,
            reason=reason,
            applied_artifact_ref=applied_artifact_ref,
            user=user,
            workspace_id=ctx.workspace_id or "ws_default",
            now=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
        )
    except LifecycleConflict as exc:
        raise BrainDetailError(
            http_status=409,
            code="lifecycle_conflict",
            message=f"{exc.current} -> {exc.attempted} is not allowed",
            brain_detail={
                "error_kind": "lifecycle_conflict",
                "current_state": exc.current,
                "attempted_state": exc.attempted,
            },
        ) from exc
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    return result


async def apply_memory_operation(
    ctx: CallerContext,
    *,
    operation_id: str,
    user: "UserInfo | None" = None,
) -> "ApplyResponse":
    """Apply GUIDANCE for an approved memory operation (operator+, NO write).

    GUIDANCE-only: this calls ``get_apply_guidance`` which only READS the op's
    precondition state and returns a next-action envelope — it opens no write
    transaction and mutates nothing. Raises :class:`BrainDetailError` (409) on a
    precondition failure, :class:`NotFoundError` if absent.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.memory_ops import (
        ApplyPreconditionError,
        get_apply_guidance,
    )
    from core.api.use_cases._errors import NotFoundError

    try:
        result = await get_apply_guidance(
            operation_id=operation_id,
            user=user,
            workspace_id=ctx.workspace_id or "ws_default",
        )
    except ApplyPreconditionError as exc:
        raise BrainDetailError(
            http_status=409,
            code=exc.kind,
            message=f"apply precondition failed: {exc.kind}",
            brain_detail={
                "error_kind": exc.kind,
                "current_state": exc.current_state,
            },
        ) from exc
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    return result


# ---------------------------------------------------------------------------
# Sub-04 Learn Findings (L5)
# ---------------------------------------------------------------------------


async def list_findings(
    ctx: CallerContext,
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    finding_type: list[str] | None = None,
    severity_min: str = "low",
    confidence_min: str = "low",
    approval_state: list[str] | None = None,
    include_terminal: bool = False,
    recurrence_min: int = 1,
    regression_only: bool = False,
    applied: bool | None = None,
    created_after: str | None = None,
    owner_user_id: str | None = None,
    cursor: str | None = None,
    limit: int,
    backlog: bool = False,
    user: "UserInfo | None" = None,
) -> "FindingsListResponse":
    """List Learn findings (any auth caller), with LLM polish applied to items.

    Raises :class:`BrainDetailError` (400) on an invalid ``confidence_min`` or a
    malformed ``created_after`` timestamp.
    """
    if cycle_key and run_id:
        raise _cycle_or_run_error()
    if confidence_min not in ("low", "medium", "high"):
        raise BrainDetailError(
            http_status=400,
            code="invalid_confidence_min",
            message=f"Invalid confidence_min: {confidence_min}",
            brain_detail={
                "error_kind": "invalid_confidence_min",
                "value": confidence_min,
            },
        )

    from core.api.services.brain.findings_reader import (
        list_findings as _list_findings,
    )
    from core.api.services.brain.llm.router_glue import apply_polish_to_findings

    finding_types = _split_csv(finding_type)
    approval_states = _split_csv(approval_state)
    created_after_dt = _parse_iso_query(created_after)

    response = await _list_findings(
        cycle_key=cycle_key,
        run_id=run_id,
        scope_type=scope_type,
        scope_key=scope_key,
        finding_types=finding_types,
        severity_min=severity_min,
        confidence_min=confidence_min,
        approval_states=approval_states,
        include_terminal=include_terminal,
        recurrence_min=recurrence_min,
        regression_only=regression_only,
        applied=applied,
        created_after=created_after_dt,
        owner_user_id=owner_user_id,
        cursor=cursor,
        limit=limit,
        backlog=backlog,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
    )
    response.items = apply_polish_to_findings(list(response.items))
    return response


async def get_finding(
    ctx: CallerContext,
    *,
    finding_id: str,
    user: "UserInfo | None" = None,
) -> "Finding":
    """Fetch a single finding (any auth caller), LLM-polished. 404 if absent."""
    from core.api.models.brain import Finding
    from core.api.services.brain.findings_reader import fetch_single_finding
    from core.api.services.brain.llm.router_glue import apply_polish_to_finding
    from core.api.use_cases._errors import NotFoundError

    result = await fetch_single_finding(
        finding_id=finding_id,
        user=user,
        workspace_id=ctx.workspace_id or "ws_default",
    )
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    if isinstance(result, Finding):
        return apply_polish_to_finding(result)
    return result


async def patch_finding(
    ctx: CallerContext,
    *,
    finding_id: str,
    action: str,
    reason: str | None = None,
    applied_artifact_ref: str | None = None,
    idempotency_key: str | None = None,
    user: "UserInfo | None" = None,
) -> "Finding":
    """Finding lifecycle action (operator+).

    Raises :class:`BrainDetailError` (409 lifecycle conflict / 422 validation), or
    :class:`NotFoundError` if absent.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.findings_reader import (
        FindingValidationError,
        LifecycleConflict,
        apply_lifecycle_patch,
    )
    from core.api.use_cases._errors import NotFoundError

    try:
        result = await apply_lifecycle_patch(
            finding_id=finding_id,
            action=action,
            reason=reason,
            applied_artifact_ref=applied_artifact_ref,
            user=user,
            workspace_id=ctx.workspace_id or "ws_default",
            now=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
        )
    except LifecycleConflict as exc:
        raise BrainDetailError(
            http_status=409,
            code="lifecycle_conflict",
            message=f"{exc.current} -> {exc.attempted} is not allowed",
            brain_detail={
                "error_kind": "lifecycle_conflict",
                "current_state": exc.current,
                "attempted_state": exc.attempted,
            },
        ) from exc
    except FindingValidationError as exc:
        raise BrainDetailError(
            http_status=422,
            code=exc.kind,
            message=str(exc.detail),
            brain_detail={"error_kind": exc.kind, "detail": exc.detail},
        ) from exc
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    return result


async def bulk_patch_findings(
    ctx: CallerContext,
    *,
    finding_ids: list[str],
    action: str,
    reason: str | None = None,
    idempotency_key: str | None = None,
    user: "UserInfo | None" = None,
) -> "FindingBulkPatchResponse":
    """Bulk lifecycle patch (operator+, cap 25).

    Raises :class:`BrainDetailError`: 413 on cap overflow, 409 on lifecycle
    conflict, 404 when an id is missing/invisible, 422 on other validation.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.findings_reader import (
        BULK_PATCH_MAX,
        FindingValidationError,
        LifecycleConflict,
        apply_bulk_patch,
    )

    if len(finding_ids) > BULK_PATCH_MAX:
        raise BrainDetailError(
            http_status=413,
            code="bulk_cap_exceeded",
            message=f"bulk cap {BULK_PATCH_MAX} exceeded",
            brain_detail={
                "error_kind": "bulk_cap_exceeded",
                "limit": BULK_PATCH_MAX,
                "received": len(finding_ids),
            },
        )
    try:
        return await apply_bulk_patch(
            finding_ids=list(finding_ids),
            action=action,
            reason=reason,
            user=user,
            workspace_id=ctx.workspace_id or "ws_default",
            now=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
        )
    except LifecycleConflict as exc:
        raise BrainDetailError(
            http_status=409,
            code="lifecycle_conflict",
            message=f"{exc.current} -> {exc.attempted} is not allowed",
            brain_detail={
                "error_kind": "lifecycle_conflict",
                "current_state": exc.current,
                "attempted_state": exc.attempted,
            },
        ) from exc
    except FindingValidationError as exc:
        if exc.kind == "bulk_cap_exceeded":
            raise BrainDetailError(
                http_status=413,
                code=exc.kind,
                message=str(exc.detail),
                brain_detail={"error_kind": exc.kind, "detail": exc.detail},
            ) from exc
        if exc.kind == "not_found_or_invisible":
            raise BrainDetailError(
                http_status=404,
                code=exc.kind,
                message=str(exc.detail),
                brain_detail={"error_kind": exc.kind, "detail": exc.detail},
            ) from exc
        raise BrainDetailError(
            http_status=422,
            code=exc.kind,
            message=str(exc.detail),
            brain_detail={"error_kind": exc.kind, "detail": exc.detail},
        ) from exc


async def apply_finding(
    ctx: CallerContext,
    *,
    finding_id: str,
    user: "UserInfo | None" = None,
) -> "ApplyResponse":
    """Apply GUIDANCE for an approved finding (operator+, NO write — sub-04 F1).

    GUIDANCE-only: calls ``get_apply_guidance`` which only READS precondition
    state and returns a next-action envelope — no write transaction, no mutation.
    Raises :class:`BrainDetailError` (409) on a precondition failure, or
    :class:`NotFoundError` if absent.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.models.brain import ApplyResponse
    from core.api.services.brain.findings_reader import (
        ApplyPreconditionError,
        get_apply_guidance,
    )
    from core.api.use_cases._errors import NotFoundError

    try:
        guidance = await get_apply_guidance(
            finding_id=finding_id,
            user=user,
            workspace_id=ctx.workspace_id or "ws_default",
        )
    except ApplyPreconditionError as exc:
        raise BrainDetailError(
            http_status=409,
            code=exc.kind,
            message=f"apply precondition failed: {exc.kind}",
            brain_detail={
                "error_kind": exc.kind,
                "current_state": exc.current_state,
            },
        ) from exc
    if guidance is None:
        raise NotFoundError(code="not_found", message="Not found")
    return ApplyResponse(
        operation_id=guidance["finding_id"],
        next_action=guidance["next_action"],
        operation_summary=guidance["finding_summary"],
    )


# ---------------------------------------------------------------------------
# Sub-05 Surfaces — journal, runs, counters, recompute, capabilities
# ---------------------------------------------------------------------------


async def list_journal(
    ctx: CallerContext,
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    program_key: str | None = None,
    limit: int = 50,
    user: "UserInfo | None" = None,
) -> "JournalListResponse":
    """List journal entries (any auth caller), LLM-polished. Resolves the served
    cycle/run from the first entry, matching the router's envelope construction."""
    from core.api.models.brain import JournalListResponse
    from core.api.services.brain.journal import list_entries_for_cycle
    from core.api.services.brain.llm.router_glue import apply_polish_to_journal

    entries = await list_entries_for_cycle(
        cycle_key=cycle_key,
        run_id=run_id,
        scope_type=scope_type,
        scope_key=scope_key,
        program_key=program_key,
        workspace_id=ctx.workspace_id or "ws_default",
        limit=limit,
    )
    # Brain RBAC (2026-07-03 plan Cross-Review §2): unlike the other readers
    # (events/drift/findings/memory_ops thread ``user`` into their service),
    # ``list_entries_for_cycle`` has no visibility param — so filter here. A
    # non-admin caller sees ONLY project-scoped entries for projects they can
    # see; the company/program aggregate narrative stays admin-only (mirrors the
    # write gate). ``user is None`` (admin/bearer/local) keeps the full set.
    if user is not None:
        from core.api.db import acquire_db
        from core.api.visibility import get_visible_projects

        async with acquire_db() as _vis_db:
            visible = await get_visible_projects(
                _vis_db, user, ctx.workspace_id or "ws_default"
            )
        if visible is not None:
            entries = [
                e
                for e in entries
                if e.scope_type == "project" and e.scope_key in visible
            ]
    resolved_cycle = entries[0].cycle_key if entries else (
        cycle_key if cycle_key not in (None, "latest") else None
    )
    resolved_run = entries[0].run_id if entries else None
    polished_entries = apply_polish_to_journal(list(entries))
    return JournalListResponse(
        items=polished_entries,
        next_cursor=None,
        cycle_key=resolved_cycle,
        run_id=resolved_run,
        total_returned=len(polished_entries),
    )


async def _authorize_brain_write_scope(
    user: "UserInfo | None",
    *,
    scope_type: str,
    scope_key: str,
    involved_projects: list[str] | None = None,
) -> None:
    """Gate an agent-native brain write by the caller's project visibility.

    Brain RBAC fix (2026-07-03 plan Cross-Review §1): the agent-native writes
    (``write_journal_narrative`` / ``write_finding``) previously performed no
    scope check — any authenticated caller could write the narrative of company
    or of projects that are not theirs, later read by everyone's LLMs. Gate:

    - ``user is None`` (admin / super_admin / static bearer / local single-user)
      -> unrestricted, unchanged.
    - non-admin caller: ``company`` / ``program`` scope is admin-only; ``project``
      (and each ``involved_projects`` entry, and an ``artifact`` scope's declared
      projects) must be inside the caller's visible set.

    Raises :class:`AuthorizationError` (403) when the scope is not permitted.
    """
    if user is None:
        return
    from core.api.db import acquire_db
    from core.api.use_cases._errors import AuthorizationError
    from core.api.visibility import get_visible_projects

    async with acquire_db() as _vis_db:
        visible = await get_visible_projects(_vis_db, user)
    if visible is None:
        # admin / super_admin / agent-bypass resolve unrestricted downstream.
        return
    if scope_type in ("company", "program"):
        raise AuthorizationError(
            code="forbidden_brain_scope",
            message="company/program brain writes are admin-only",
        )
    targets: set[str] = set()
    if scope_type == "project":
        targets.add(scope_key)
    for project in involved_projects or ():
        if project:
            targets.add(project)
    if not targets:
        # artifact scope with no declared project cannot be attributed to a
        # visible project -> deny for a non-admin caller (least privilege).
        raise AuthorizationError(
            code="forbidden_brain_scope",
            message="brain write scope not attributable to a visible project",
        )
    hidden = targets - visible
    if hidden:
        raise AuthorizationError(
            code="forbidden_brain_scope",
            message="brain write scope not visible to caller",
        )


async def write_journal_narrative(
    ctx: CallerContext,
    *,
    cycle_key: str,
    scope_type: str,
    scope_key: str,
    narrative: str,
    user: "UserInfo | None" = None,
) -> dict[str, Any]:
    """Agent-native: persist the agent's narrative synthesis onto a journal entry.

    The platform runs no synthesis LLM (decision 2026-07-01-brain-agent-native);
    the caller's own agent writes the narrative it produced for a cycle+scope.
    Provenance is kept separate from the cycle's narrative_polished (migration
    158). Operator+ gate. Also records brain_last_synthesis_at (see get_brain_staleness).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    await _authorize_brain_write_scope(
        user, scope_type=scope_type, scope_key=scope_key
    )
    from core.api.services.brain.agent_synthesis import persist_journal_narrative

    updated = await persist_journal_narrative(
        cycle_key=cycle_key,
        scope_type=scope_type,
        scope_key=scope_key,
        narrative=narrative,
        agent_by=(ctx.user_id or ctx.username or "agent"),
        workspace_id=ctx.workspace_id or "ws_default",
    )
    if not updated:
        raise ServiceError(
            code="journal_entry_not_found",
            message="no published journal entry for that cycle_key/scope",
        )
    return {
        "written": True,
        "cycle_key": cycle_key,
        "scope_type": scope_type,
        "scope_key": scope_key,
    }


async def get_brain_staleness(
    ctx: CallerContext,
    *,
    user: "UserInfo | None" = None,
) -> dict[str, Any]:
    """Agent-native: freshness of the agent synthesis vs the mechanical cycle.

    Mechanical (no LLM). ``last_synthesis_at`` = last agent write; ``last_cycle_key``
    = last mechanical cycle. An agent reads this on connect to decide whether to
    re-synthesize before answering.
    """
    from core.api.services.brain.agent_synthesis import get_staleness

    return await get_staleness(workspace_id=ctx.workspace_id or "ws_default")


async def write_finding(
    ctx: CallerContext,
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
    user: "UserInfo | None" = None,
) -> dict[str, Any]:
    """Agent-native: persist an agent-authored finding into the Triage queue.

    The platform runs no synthesis LLM (decision 2026-07-01-brain-agent-native);
    the caller's own agent writes the finding (its conclusion). Provenance is kept
    separate from cycle findings via authored_by_agent (migration 159). Lands as
    approval_state='open' so the existing Triage/patch flow applies — re-read via
    list_findings, patch via patch_finding. Operator+ gate. Returns written=False
    with a reason when there is no run to attach to or the content dedups.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    await _authorize_brain_write_scope(
        user,
        scope_type=scope_type,
        scope_key=scope_key,
        involved_projects=involved_projects,
    )
    from core.api.services.brain.agent_synthesis import persist_agent_finding

    return await persist_agent_finding(
        finding_type=finding_type,
        scope_type=scope_type,
        scope_key=scope_key,
        title=title,
        summary=summary,
        why_now=why_now,
        severity=severity,
        confidence=confidence,
        suggested_artifact=suggested_artifact,
        program_key=program_key,
        involved_projects=involved_projects,
        closure_instruction=closure_instruction,
        agent_by=(ctx.user_id or ctx.username or "agent"),
        workspace_id=ctx.workspace_id or "ws_default",
    )


async def list_brain_runs(
    ctx: CallerContext,
    *,
    cycle_key: str | None = None,
    status: list[str] | None = None,
    trigger: list[str] | None = None,
    include_superseded: bool = False,
    cursor: str | None = None,
    limit: int,
    user: "UserInfo | None" = None,
) -> "RunsListResponse":
    """List cycle runs (any auth caller). ``latest`` resolves to the most recent
    succeeded, non-superseded cycle."""
    from core.api.services.brain.runs_reader import list_runs

    return await list_runs(
        cycle_key=cycle_key,
        status=status,
        trigger=trigger,
        include_superseded=include_superseded,
        cursor=cursor,
        limit=limit,
        workspace_id=ctx.workspace_id or "ws_default",
    )


async def trigger_brain_run(
    ctx: CallerContext,
    db,
    *,
    user: "UserInfo | None" = None,
) -> dict[str, bool]:
    """Start a manual brain cycle in a detached subprocess."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    from core.api.services.brain.manual_run import (
        has_running_brain_cycle,
        spawn_manual_brain_run,
    )

    workspace_id = ctx.workspace_id or "ws_default"
    if await has_running_brain_cycle(db, workspace_id=workspace_id):
        raise BrainDetailError(
            http_status=409,
            code="already_running",
            message="A brain cycle is already running",
            brain_detail={
                "error_kind": "already_running",
                "detail": "A brain cycle is already running",
            },
        )

    try:
        spawn_manual_brain_run()
    except OSError as exc:
        raise BrainDetailError(
            http_status=500,
            code="spawn_failed",
            message=str(exc),
            brain_detail={"error_kind": "spawn_failed", "detail": str(exc)},
        ) from exc
    return {"started": True}


async def get_brain_run(
    ctx: CallerContext,
    *,
    run_id: str,
    user: "UserInfo | None" = None,
) -> "BrainRun":
    """Fetch a single cycle run. Raises :class:`NotFoundError` if absent."""
    from core.api.services.brain.runs_reader import fetch_single_run
    from core.api.use_cases._errors import NotFoundError

    result = await fetch_single_run(
        run_id=run_id, workspace_id=ctx.workspace_id or "ws_default"
    )
    if result is None:
        raise NotFoundError(code="not_found", message="Not found")
    return result


async def discard_brain_run(
    ctx: CallerContext,
    *,
    run_id: str,
    user: "UserInfo | None" = None,
) -> "BrainRun":
    """Discard a head run (operator+): flip status to 'failed' so ``latest`` skips it.

    Raises :class:`BrainDetailError`: 404 if unknown, 422 if not head/not discardable.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.runs_reader import DiscardError, discard_run

    try:
        return await discard_run(
            run_id=run_id, workspace_id=ctx.workspace_id or "ws_default"
        )
    except DiscardError as exc:
        status_code = 404 if exc.kind == "not_found" else 422
        raise BrainDetailError(
            http_status=status_code,
            code=exc.kind,
            message=str(exc.detail),
            brain_detail={"error_kind": exc.kind, "detail": exc.detail},
        ) from exc


async def promote_brain_run(
    ctx: CallerContext,
    *,
    run_id: str,
    user: "UserInfo | None" = None,
) -> "BrainRun":
    """Promote a superseded run back to head (operator+).

    Raises :class:`BrainDetailError`: 404 if unknown, 422 if already the head.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.runs_reader import PromoteError, promote_run

    try:
        return await promote_run(
            run_id=run_id, workspace_id=ctx.workspace_id or "ws_default"
        )
    except PromoteError as exc:
        status_code = 404 if exc.kind == "not_found" else 422
        raise BrainDetailError(
            http_status=status_code,
            code=exc.kind,
            message=str(exc.detail),
            brain_detail={"error_kind": exc.kind, "detail": exc.detail},
        ) from exc


async def reset_brain_watermarks(
    ctx: CallerContext,
    *,
    to_iso: str,
    source_systems: list[str] | None = None,
    user: "UserInfo | None" = None,
) -> dict:
    """Reset source watermarks backwards (super_admin).

    Raises :class:`BrainDetailError` (422, ``error_kind=invalid_iso``) on a
    malformed ISO instant. Returns ``{"reset": [...], "count": N}``.
    """
    require_role_ctx(ctx, "super_admin")
    from core.api.services.brain.watermarks import reset_watermarks

    try:
        audit = await reset_watermarks(
            workspace_id=ctx.workspace_id or "ws_default",
            to_iso=to_iso,
            source_systems=source_systems,
            now=datetime.now(timezone.utc),
        )
    except ValueError as exc:
        raise BrainDetailError(
            http_status=422,
            code="invalid_iso",
            message=str(exc),
            brain_detail={"error_kind": "invalid_iso", "detail": str(exc)},
        ) from exc
    return {"reset": audit, "count": len(audit)}


async def get_counters(
    ctx: CallerContext,
    *,
    cycle_key: str | None = None,
    user: "UserInfo | None" = None,
) -> "PipelineCounters":
    """Aggregated 6-station PipelineSubbar counters (any auth caller)."""
    from core.api.services.brain.runs_reader import get_pipeline_counters

    return await get_pipeline_counters(
        cycle_key=cycle_key, workspace_id=ctx.workspace_id or "ws_default"
    )


async def get_cycle_recap(
    ctx: CallerContext,
    *,
    cycle_key: str,
    user: "UserInfo | None" = None,
) -> dict:
    """Deterministic italian narrative recap of a cycle (any auth caller)."""
    from core.api.services.brain.recap import build_cycle_recap

    return await build_cycle_recap(
        cycle_key=cycle_key, workspace_id=ctx.workspace_id or "ws_default"
    )


async def recompute_cycle(
    ctx: CallerContext,
    *,
    cycle_key: str,
    dry_run: bool = False,
    force: bool = False,
    idempotency_key: str | None = None,
    triggered_by: str | None = None,
    user: "UserInfo | None" = None,
) -> "RecomputeResponse":
    """Manual recompute of a Brain cycle (operator+).

    ``Idempotency-Key`` is mandatory for the real (non-dry-run) path: missing it
    raises :class:`BrainDetailError` (422 ``missing_idempotency_key``).
    ``dry_run`` short-circuits before any side-effect. A ``cycle_too_old``
    rejection maps to 409, other rejections to 400 (both structured bodies).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.models.brain import RecomputeResponse

    if not idempotency_key:
        raise BrainDetailError(
            http_status=422,
            code="missing_idempotency_key",
            message="Idempotency-Key header required for state mutations",
            brain_detail={
                "error_kind": "missing_idempotency_key",
                "detail": "Idempotency-Key header required for state mutations",
            },
        )

    if dry_run:
        # `dry_run` short-circuits before any side-effect (router parity).
        return RecomputeResponse(
            status="dry_run",
            cycle_key=cycle_key,
            run_id=None,
            dry_run=True,
        )

    from core.api.services.brain.jobs import recompute_brain_cycle

    result = await recompute_brain_cycle(
        cycle_key=cycle_key,
        triggered_by=triggered_by,
        force=force,
        workspace_id=ctx.workspace_id or "ws_default",
    )

    if result.get("status") in ("rejected",):
        error_kind = result.get("error_kind")
        raise BrainDetailError(
            http_status=409 if error_kind == "cycle_too_old" else 400,
            code=error_kind or "rejected",
            message=f"recompute rejected: {error_kind or 'rejected'}",
            brain_detail={
                "error_kind": error_kind or "rejected",
                "cycle_key": cycle_key,
                "age_days": result.get("age_days"),
            },
        )

    return RecomputeResponse(
        status=str(result.get("status", "unknown")),
        cycle_key=str(result.get("cycle_key") or cycle_key),
        run_id=result.get("run_id"),
        event_count=int(result.get("event_count") or 0),
        journal_count=int(result.get("journal_count") or 0),
        duration_ms=result.get("duration_ms"),
        mode=result.get("mode"),
        dry_run=False,
    )


async def get_brain_capabilities(ctx: CallerContext) -> "BrainCapabilities":
    """Brain schema metadata snapshot (any auth caller). Deterministic; no DB."""
    from core.api.services.brain.capabilities import get_capabilities

    return get_capabilities()


# ---------------------------------------------------------------------------
# Wave 3.1 admin endpoints
# ---------------------------------------------------------------------------


async def supersede_orphan_partials_uc(
    ctx: CallerContext,
    *,
    user: "UserInfo | None" = None,
) -> int:
    """Mark orphan ``partial`` runs as superseded by the latest succeeded run on
    the same cycle (admin+). Idempotent. Returns the superseded count.

    Owns its own writer connection (like the router) — this is an admin backfill,
    not a request-scoped read.
    """
    require_role_ctx(ctx, "admin", "super_admin")
    from core.api.db import write_db
    from core.api.services.brain.cycle import supersede_orphan_partials

    workspace_id = ctx.workspace_id or "ws_default"
    async with write_db() as db:
        count = await supersede_orphan_partials(db, workspace_id=workspace_id)
    return int(count)


async def polish_pending_journal_uc(
    ctx: CallerContext,
    *,
    limit: int = 50,
    user: "UserInfo | None" = None,
) -> dict:
    """Polish the next ``limit`` journal entries with NULL narrative_polished
    (operator+). Returns ``{updated, remaining, skipped, skipped_reason}``."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.services.brain.cycle import polish_pending_journals

    workspace_id = ctx.workspace_id or "ws_default"
    return await polish_pending_journals(
        workspace_id=workspace_id,
        limit=int(limit),
        now=datetime.now(timezone.utc),
    )


async def promote_finding_to_task_uc(
    ctx: CallerContext,
    *,
    finding_id: str,
    title_override: str | None = None,
    project_override: str | None = None,
    description_override: str | None = None,
    user: "UserInfo | None" = None,
) -> dict:
    """Promote a Brain finding to a tracked Marvis task (operator+).

    Reads the finding (must be ``open`` or ``approved``), INSERTs an ``approved``
    task (severity → ICE-D impact), and supersedes the finding. Raises
    :class:`NotFoundError` if absent, :class:`BrainDetailError` (409
    ``promote_blocked_state``) if the finding is in a non-promotable state.

    Returns ``{task_id, finding_id, finding_superseded_by, project, title}``.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    from core.api.db import write_db
    from core.api.services.brain.findings_reader import fetch_single_finding
    from core.api.use_cases._errors import NotFoundError

    workspace_id = ctx.workspace_id or "ws_default"
    finding = await fetch_single_finding(
        finding_id=finding_id, user=user, workspace_id=workspace_id
    )
    if finding is None:
        raise NotFoundError(code="finding_not_found", message="Finding not found")
    current_state = str(finding.approval_state)
    if current_state not in ("open", "approved"):
        raise BrainDetailError(
            http_status=409,
            code="promote_blocked_state",
            message=f"finding in state {current_state} cannot be promoted",
            brain_detail={
                "error_kind": "promote_blocked_state",
                "current_state": current_state,
                "allowed_states": ["open", "approved"],
            },
        )

    project = project_override or finding.scope_key
    title = title_override or f"[Brain] {finding.title}"
    description = build_promote_task_description(
        finding=finding, override=description_override
    )

    severity = str(finding.severity).lower()
    impact = _SEVERITY_TO_IMPACT.get(severity, 6)
    confidence = 7
    ease = 5

    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags = ["brain", "promoted-from-finding", str(finding.finding_type)]
    tags_json = json.dumps(tags, ensure_ascii=False)
    superseded_marker = f"task:{task_id}"

    actor_username = user.username if user is not None else ctx.username
    actor_user_id = user.user_id if user is not None else ctx.user_id

    async with write_db() as db:
        await db.execute(
            "INSERT INTO tasks ("
            " id, title, description, status, project, priority,"
            " created_by, owner_id, source, source_ref, tags, kind,"
            " impact, confidence, ease, delegation, scored_by, scored_at,"
            " due_date, workspace_id, completion_mode, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'brain', ?, ?, 'normal',"
            " ?, ?, ?, 'hybrid', ?, ?, NULL, ?, 'pr', ?, ?)",
            (
                task_id,
                title[:200],
                description,
                "approved",
                project,
                "high",
                actor_username,
                None,
                f"brain:finding:{finding_id}",
                tags_json,
                impact,
                confidence,
                ease,
                actor_username,
                now,
                workspace_id,
                now,
                now,
            ),
        )
        await db.execute(
            "UPDATE brain_findings SET"
            "  approval_state = 'superseded',"
            "  superseded_by_finding_id = ?"
            " WHERE finding_id = ?",
            (superseded_marker, finding_id),
        )
        await db.execute(
            "INSERT INTO brain_finding_states ("
            " state_id, finding_id, from_state, to_state, actor_user_id, reason"
            ") VALUES (?, ?, ?, 'superseded', ?, ?)",
            (
                uuid.uuid4().hex,
                finding_id,
                current_state,
                actor_user_id,
                f"promoted to task {task_id}",
            ),
        )

    return {
        "task_id": task_id,
        "finding_id": finding_id,
        "finding_superseded_by": superseded_marker,
        "project": project,
        "title": title[:200],
    }
