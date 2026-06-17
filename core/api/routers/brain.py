# v2.0.0 - 2026-05-27 - S1 F1.9: thin adapter over use_cases.brain
# Brain v1 — HTTP router (sub-01 D6 events + sub-02 drift + sub-03 memory ops + sub-04 findings + sub-05 surfaces).
"""HTTP adapter for the Brain v1 reflection layer (S1 collapse-runtime).

This router is a thin transport adapter. All query + validation + RBAC + error
logic lives in :mod:`core.api.use_cases.brain` (pure, fastapi-free). Each handler:

1. resolves identity into a :class:`CallerContext` (``from_user_info``; Brain has
   no human-only gate, so ``is_human_session=False``) and forwards the original
   ``UserInfo`` as ``user=`` so the services can resolve project visibility
   (``UserInfo.teams`` has no ``CallerContext`` counterpart by design);
2. calls the use_case inside ``try/except ServiceError`` -> ``_to_http_brain``.

Error translation (``_to_http_brain``): Brain pins TWO body shapes that the
generic ``to_http`` ({"code","message"}) would break, so this adapter owns the
mapping itself:
  * :class:`use_cases.brain.BrainDetailError` -> ``HTTPException(status,
    detail=<structured dict>)`` — preserves bodies like
    ``{"detail": {"error_kind": "missing_idempotency_key"}}`` that HTTP tests pin.
  * any other ``ServiceError`` (the bare 404 "Not found" and the 400 "Pass
    cycle_key OR run_id, not both") -> ``HTTPException(status, detail=<message>)``
    — the legacy PLAIN-STRING bodies.

Request models + admin response envelopes stay declared HERE (HTTP contract
surface). The ``*_apply`` endpoints remain GUIDANCE-only (the use_cases call the
services' read-only ``get_apply_guidance`` and write nothing).

Re-exports: the response/DTO models stay imported from ``core.api.models.brain``
exactly as before, so any external importer of those symbols via this module is
unaffected; ``router`` (the only symbol ``main.py`` imports) is preserved.
"""
from __future__ import annotations

import logging

import aiosqlite
from pydantic import BaseModel, ConfigDict

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response

from core.api.db import get_db
from core.api.models import UserInfo
from core.api.models.brain import (
    ApplyResponse,
    BrainCapabilities,
    BrainRun,
    DriftListResponse,
    DriftPatchRequest,
    DriftSignal,
    EventsListResponse,
    EventType,
    Finding,
    FindingBulkPatchRequest,
    FindingBulkPatchResponse,
    FindingPatchRequest,
    FindingsListResponse,
    KnowledgeForm,
    JournalListResponse,
    MemoryOpPatchRequest,
    MemoryOperation,
    MemoryOperationsListResponse,
    PipelineCounters,
    RecomputeRequest,
    RecomputeResponse,
    RunsListResponse,
    Severity,
    SignalState,
    SignalType,
)
from core.api.rbac import require_role
from core.api.use_cases import brain as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError
from core.api.use_cases.brain import BrainDetailError
from core.api.services.brain.drift_router import (
    DEFAULT_DRIFT_LIMIT,
    MAX_DRIFT_LIMIT,
)
from core.api.services.brain.events_reader import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
)
from core.api.services.brain.findings_reader import (
    DEFAULT_LIMIT as FINDINGS_DEFAULT_LIMIT,
    MAX_LIMIT as FINDINGS_MAX_LIMIT,
)
from core.api.services.brain.memory_ops import (
    DEFAULT_LIMIT as MEMORY_OP_DEFAULT_LIMIT,
    MAX_LIMIT as MEMORY_OP_MAX_LIMIT,
)
from core.api.services.brain.runs_reader import (
    DEFAULT_LIMIT as RUNS_DEFAULT_LIMIT,
    MAX_LIMIT as RUNS_MAX_LIMIT,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/brain", tags=["brain"])


class BrainRunTriggerResponse(BaseModel):
    started: bool


def _to_http_brain(err: ServiceError) -> HTTPException:
    """Map a Brain ``ServiceError`` to its legacy ``HTTPException`` body.

    ``BrainDetailError`` carries a structured ``detail`` dict; everything else
    (bare ``NotFoundError`` / ``ValidationError``) used a plain-string detail.
    """
    if isinstance(err, BrainDetailError):
        return HTTPException(status_code=err.http_status, detail=err.brain_detail)
    return HTTPException(status_code=err.http_status, detail=err.message)


def _ctx(user: UserInfo) -> CallerContext:
    """Build a CallerContext for the Brain surface (no human-only gate)."""
    return CallerContext.from_user_info(user, is_human_session=False)


@router.get(
    "/events",
    response_model=EventsListResponse,
    summary="List Brain digest events (D6)",
)
async def get_brain_events(
    cycle_key: str | None = Query(default=None, description="YYYY-MM-DD or 'latest'"),
    run_id: str | None = Query(default=None, description="Specific run_id"),
    event_type: list[EventType] | None = Query(default=None),
    source_project: str | None = Query(default=None),
    cursor: str | None = Query(default=None, description="Opaque pagination cursor"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> EventsListResponse:
    """Paginated digest events for a cycle, visibility-filtered."""
    try:
        return await uc.list_events(
            _ctx(user),
            cycle_key=cycle_key,
            run_id=run_id,
            event_type=event_type,
            source_project=source_project,
            cursor=cursor,
            limit=limit,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


# ---------------------------------------------------------------------------
# Sub-02 Drift Checker (L3)
# ---------------------------------------------------------------------------


@router.get(
    "/drift",
    response_model=DriftListResponse,
    summary="List Brain drift signals (sub-02 §11.1)",
)
async def get_brain_drift(
    cycle_key: str | None = Query(default=None, description="YYYY-MM-DD or 'latest'"),
    run_id: str | None = Query(default=None),
    scope_type: str | None = Query(default=None),
    scope_key: str | None = Query(default=None),
    signal_type: list[SignalType] | None = Query(default=None),
    knowledge_form: list[KnowledgeForm] | None = Query(default=None),
    severity_min: Severity = Query(default="low"),
    confidence_min: float = Query(default=0.0, ge=0.0, le=1.0),
    state: list[SignalState] | None = Query(
        default=None, description="Defaults to ['open'] when omitted"
    ),
    include_resolved: bool = Query(default=False),
    drift_axis: list[str] | None = Query(
        default=None,
        description="CE4 axis filter: intent | context | both (comma-list)",
    ),
    rule_id: list[str] | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_DRIFT_LIMIT, ge=1, le=MAX_DRIFT_LIMIT),
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> DriftListResponse:
    try:
        return await uc.list_drift(
            _ctx(user),
            cycle_key=cycle_key,
            run_id=run_id,
            scope_type=scope_type,
            scope_key=scope_key,
            signal_type=signal_type,
            knowledge_form=knowledge_form,
            severity_min=severity_min,
            confidence_min=confidence_min,
            state=state,
            include_resolved=include_resolved,
            drift_axis=drift_axis,
            rule_id=rule_id,
            cursor=cursor,
            limit=limit,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.get(
    "/drift/{signal_id}",
    response_model=DriftSignal,
    summary="Fetch a single Brain drift signal",
)
async def get_brain_drift_signal(
    signal_id: str,
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> DriftSignal:
    try:
        return await uc.get_drift_signal(_ctx(user), signal_id=signal_id, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


@router.patch(
    "/drift/{signal_id}",
    response_model=DriftSignal,
    summary="Drift signal lifecycle action (dismiss / acknowledge / resolve / reopen)",
)
async def patch_brain_drift_signal(
    signal_id: str,
    body: DriftPatchRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> DriftSignal:
    try:
        return await uc.patch_drift_signal(
            _ctx(user),
            signal_id=signal_id,
            action=body.action,
            reason=body.reason,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


# ---------------------------------------------------------------------------
# Sub-03 Memory Operations (L4)
# ---------------------------------------------------------------------------


@router.get(
    "/memory-operations",
    response_model=MemoryOperationsListResponse,
    summary="List Brain memory operations (sub-03 §11.1)",
)
async def get_brain_memory_operations(
    cycle_key: str | None = Query(default=None, description="YYYY-MM-DD or 'latest'"),
    run_id: str | None = Query(default=None),
    scope_type: str | None = Query(default=None),
    scope_key: str | None = Query(default=None),
    operation_type: list[str] | None = Query(default=None),
    approval_state: list[str] | None = Query(
        default=None, description="Defaults to ['pending'] when omitted"
    ),
    include_terminal: bool = Query(default=False),
    recurrence_min: int = Query(default=1, ge=1),
    score_min: float = Query(default=0.0, ge=0.0, le=1.0),
    cursor: str | None = Query(default=None),
    limit: int = Query(
        default=MEMORY_OP_DEFAULT_LIMIT, ge=1, le=MEMORY_OP_MAX_LIMIT
    ),
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> MemoryOperationsListResponse:
    try:
        return await uc.list_memory_operations(
            _ctx(user),
            cycle_key=cycle_key,
            run_id=run_id,
            scope_type=scope_type,
            scope_key=scope_key,
            operation_type=operation_type,
            approval_state=approval_state,
            include_terminal=include_terminal,
            recurrence_min=recurrence_min,
            score_min=score_min,
            cursor=cursor,
            limit=limit,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.get(
    "/memory-operations/{operation_id}",
    response_model=MemoryOperation,
    summary="Fetch a single Brain memory operation",
)
async def get_brain_memory_operation(
    operation_id: str,
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> MemoryOperation:
    try:
        return await uc.get_memory_operation(
            _ctx(user), operation_id=operation_id, user=user
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.patch(
    "/memory-operations/{operation_id}",
    response_model=MemoryOperation,
    summary="Memory op lifecycle action (approve / dismiss / reject)",
)
async def patch_brain_memory_operation(
    operation_id: str,
    body: MemoryOpPatchRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> MemoryOperation:
    try:
        return await uc.patch_memory_operation(
            _ctx(user),
            operation_id=operation_id,
            action=body.approval_state,
            reason=body.reason,
            applied_artifact_ref=body.applied_artifact_ref,
            idempotency_key=idempotency_key,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.post(
    "/memory-operations/{operation_id}/apply",
    response_model=ApplyResponse,
    summary="Apply guidance for an approved memory operation (NO write)",
)
async def apply_brain_memory_operation(
    operation_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> ApplyResponse:
    try:
        return await uc.apply_memory_operation(
            _ctx(user), operation_id=operation_id, user=user
        )
    except ServiceError as e:
        raise _to_http_brain(e)


# ---------------------------------------------------------------------------
# Sub-04 Learn Findings (L5)
# ---------------------------------------------------------------------------


@router.get(
    "/findings",
    response_model=FindingsListResponse,
    summary="List Brain Learn findings (sub-04 §11.1)",
)
async def get_brain_findings(
    cycle_key: str | None = Query(default=None, description="YYYY-MM-DD or 'latest'"),
    run_id: str | None = Query(default=None),
    scope_type: str | None = Query(default=None),
    scope_key: str | None = Query(default=None),
    finding_type: list[str] | None = Query(default=None),
    severity_min: Severity = Query(default="low"),
    confidence_min: str = Query(default="low", description="low | medium | high"),
    approval_state: list[str] | None = Query(
        default=None, description="Defaults to ['open'] when omitted"
    ),
    include_terminal: bool = Query(default=False),
    recurrence_min: int = Query(default=1, ge=1),
    regression_only: bool = Query(default=False),
    applied: bool | None = Query(default=None),
    created_after: str | None = Query(default=None),
    owner_user_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(
        default=FINDINGS_DEFAULT_LIMIT, ge=1, le=FINDINGS_MAX_LIMIT
    ),
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> FindingsListResponse:
    try:
        return await uc.list_findings(
            _ctx(user),
            cycle_key=cycle_key,
            run_id=run_id,
            scope_type=scope_type,
            scope_key=scope_key,
            finding_type=finding_type,
            severity_min=severity_min,
            confidence_min=confidence_min,
            approval_state=approval_state,
            include_terminal=include_terminal,
            recurrence_min=recurrence_min,
            regression_only=regression_only,
            applied=applied,
            created_after=created_after,
            owner_user_id=owner_user_id,
            cursor=cursor,
            limit=limit,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.get(
    "/findings/{finding_id}",
    response_model=Finding,
    summary="Fetch a single Brain Learn finding",
)
async def get_brain_finding(
    finding_id: str,
    user: UserInfo = Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> Finding:
    try:
        return await uc.get_finding(_ctx(user), finding_id=finding_id, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


@router.patch(
    "/findings/{finding_id}",
    response_model=Finding,
    summary="Finding lifecycle action (approve / dismiss / resolve)",
)
async def patch_brain_finding(
    finding_id: str,
    body: FindingPatchRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> Finding:
    try:
        return await uc.patch_finding(
            _ctx(user),
            finding_id=finding_id,
            action=body.approval_state,
            reason=body.reason,
            applied_artifact_ref=body.applied_artifact_ref,
            idempotency_key=idempotency_key,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.patch(
    "/findings:bulk",
    response_model=FindingBulkPatchResponse,
    summary="Bulk lifecycle patch (cap 25, sub-04 §11.1)",
)
async def patch_brain_findings_bulk(
    body: FindingBulkPatchRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> FindingBulkPatchResponse:
    try:
        return await uc.bulk_patch_findings(
            _ctx(user),
            finding_ids=list(body.finding_ids),
            action=body.approval_state,
            reason=body.reason,
            idempotency_key=idempotency_key,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.post(
    "/findings/{finding_id}/apply",
    response_model=ApplyResponse,
    summary="Apply guidance for an approved finding (NO write — sub-04 F1)",
)
async def apply_brain_finding(
    finding_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> ApplyResponse:
    try:
        return await uc.apply_finding(_ctx(user), finding_id=finding_id, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


# ---------------------------------------------------------------------------
# Sub-05 Surfaces — journal, runs, counters, recompute, capabilities
# ---------------------------------------------------------------------------


@router.get(
    "/journal",
    response_model=JournalListResponse,
    summary="List Brain journal entries (sub-01 §6.D2/D4)",
)
async def get_brain_journal(
    cycle_key: str | None = Query(default=None, description="YYYY-MM-DD or 'latest'"),
    run_id: str | None = Query(default=None),
    scope_type: str | None = Query(default=None),
    scope_key: str | None = Query(default=None),
    program_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> JournalListResponse:
    try:
        return await uc.list_journal(
            _ctx(user),
            cycle_key=cycle_key,
            run_id=run_id,
            scope_type=scope_type,
            scope_key=scope_key,
            program_key=program_key,
            limit=limit,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.get(
    "/runs",
    response_model=RunsListResponse,
    summary="List Brain cycle runs (sub-05 §2)",
)
async def get_brain_runs(
    cycle_key: str | None = Query(default=None, description="YYYY-MM-DD or 'latest'"),
    status: list[str] | None = Query(default=None),
    trigger: list[str] | None = Query(default=None),
    include_superseded: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=RUNS_DEFAULT_LIMIT, ge=1, le=RUNS_MAX_LIMIT),
    user=Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> RunsListResponse:
    """Stable sort `(cycle_key DESC, run_id ASC)`. `latest` resolves to the
    most recent succeeded, non-superseded cycle (sub-01 §6.D4)."""
    try:
        return await uc.list_brain_runs(
            _ctx(user),
            cycle_key=cycle_key,
            status=status,
            trigger=trigger,
            include_superseded=include_superseded,
            cursor=cursor,
            limit=limit,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.post(
    "/run",
    status_code=202,
    response_model=BrainRunTriggerResponse,
    summary="Start a manual Brain cycle asynchronously",
)
async def post_brain_run(
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> BrainRunTriggerResponse:
    try:
        payload = await uc.trigger_brain_run(_ctx(user), db, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)
    return BrainRunTriggerResponse(**payload)


@router.get(
    "/runs/{run_id}",
    response_model=BrainRun,
    summary="Fetch a single Brain cycle run",
)
async def get_brain_run(
    run_id: str,
    user=Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> BrainRun:
    try:
        return await uc.get_brain_run(_ctx(user), run_id=run_id, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


@router.post(
    "/runs/{run_id}/discard",
    response_model=BrainRun,
    summary="Mark a head run as failed so UX latest skips it (operator+)",
)
async def post_brain_run_discard(
    run_id: str,
    user=Depends(require_role("operator", "admin", "super_admin")),
) -> BrainRun:
    """Discard a head run by flipping status to 'failed'.

    Use case: a low-yield manual recompute creates a head that intercepts
    `cycle_key=latest` resolution. Discard removes it from the
    active-cycle index so the prior cycle's head wins `latest`.
    Row stays queryable via `include_superseded=true` for forensics.

    Returns 404 if unknown, 422 if not head or not discardable.
    """
    try:
        return await uc.discard_brain_run(_ctx(user), run_id=run_id, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


class WatermarkResetRequest(BaseModel):
    """POST /api/v1/brain/watermarks/reset body."""

    model_config = ConfigDict(extra="forbid")

    to_iso: str
    source_systems: list[str] | None = None


@router.post(
    "/watermarks/reset",
    summary="Reset brain source watermarks to a past instant (super_admin)",
)
async def post_brain_watermarks_reset(
    body: WatermarkResetRequest,
    user=Depends(require_role("super_admin")),
) -> dict:
    """Force watermarks backwards so subsequent cycles re-read historical substrate.

    Use case: monthly backfill — reset to 30 days ago, then loop trigger
    one cycle per day to reconstruct the brain's narrative timeline.

    Body:
      - to_iso: ISO 8601 timestamp (e.g. "2026-04-17T00:00:00Z")
      - source_systems: optional list (default: all 6 collectors)

    Returns audit list of {source_system, previous_observed_at, new_observed_at}.
    """
    try:
        return await uc.reset_brain_watermarks(
            _ctx(user),
            to_iso=body.to_iso,
            source_systems=body.source_systems,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.post(
    "/runs/{run_id}/promote",
    response_model=BrainRun,
    summary="Revert a supersede so the target run becomes UX-visible (operator+)",
)
async def post_brain_run_promote(
    run_id: str,
    user=Depends(require_role("operator", "admin", "super_admin")),
) -> BrainRun:
    """Promote a superseded run back to head of its cycle.

    Use case: a low-yield manual recompute superseded a richer auto-cron run.
    Promote swaps the chain head pointer so the richer run's journal/drift/
    memory_op/finding rows surface again on every read path.

    Returns 404 if the run is unknown, 422 if it's already the visible head
    (no superseder to revert).
    """
    try:
        return await uc.promote_brain_run(_ctx(user), run_id=run_id, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


@router.get(
    "/counters",
    response_model=PipelineCounters,
    summary="Aggregated 6-station PipelineSubbar counters",
)
async def get_brain_counters(
    cycle_key: str | None = Query(default=None, description="YYYY-MM-DD or 'latest'"),
    user=Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> PipelineCounters:
    """Single-call replacement for 5 concurrent fetches (sub-05 v1.1)."""
    try:
        return await uc.get_counters(_ctx(user), cycle_key=cycle_key, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


@router.get(
    "/cycles/{cycle_key}/recap",
    summary="Deterministic italian narrative recap of a cycle (viewer+)",
)
async def get_brain_cycle_recap(
    cycle_key: str,
    user=Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> dict:
    """Italian narrative + per-project recap built deterministically from
    journal entries + digest events. v1.1 LLM polish layer can replace
    the strings server-side when brain_llm_polish_enabled flips.
    """
    try:
        return await uc.get_cycle_recap(_ctx(user), cycle_key=cycle_key, user=user)
    except ServiceError as e:
        raise _to_http_brain(e)


@router.post(
    "/cycles/{cycle_key}/recompute",
    response_model=RecomputeResponse,
    summary="Manual recompute of a Brain cycle (operator+, sub-01 §6.D4)",
)
async def post_brain_recompute(
    cycle_key: str,
    body: RecomputeRequest = Body(default_factory=RecomputeRequest),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user=Depends(require_role("operator", "admin", "super_admin")),
) -> RecomputeResponse:
    """Triggers `recompute_brain_cycle`. Concurrent calls on the same cycle
    serialize via the run lease — second caller receives the in-flight run_id
    (sub-05 §9 failure invariant #1).
    """
    try:
        return await uc.recompute_cycle(
            _ctx(user),
            cycle_key=cycle_key,
            dry_run=body.dry_run,
            force=body.force,
            idempotency_key=idempotency_key,
            triggered_by=user.user_id or user.username,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)


@router.get(
    "/capabilities",
    response_model=BrainCapabilities,
    summary="Brain schema metadata (sub-05 OD-11)",
)
async def get_brain_capabilities(
    user=Depends(require_role("viewer", "operator", "admin", "super_admin")),
) -> BrainCapabilities:
    """Snapshot of every Literal enum exposed by the Brain v1 surface.

    Mirrors `graph_capabilities` precedent so agents can discover enums
    without hardcoding constants.
    """
    try:
        return await uc.get_brain_capabilities(_ctx(user))
    except ServiceError as e:
        raise _to_http_brain(e)


# ---------------------------------------------------------------------------
# Wave 3.1 admin — mark orphan partial runs as superseded.
# ---------------------------------------------------------------------------


class SupersedeOrphanPartialsResponse(BaseModel):
    """Outcome envelope for POST /admin/supersede-orphan-partials."""

    superseded: int


@router.post(
    "/admin/supersede-orphan-partials",
    response_model=SupersedeOrphanPartialsResponse,
    summary=(
        "One-shot backfill: mark `partial` brain_runs orphan as superseded by the"
        " latest succeeded run on the same cycle (Wave 3.1 UI canonical fix)"
    ),
)
async def supersede_orphan_partials_endpoint(
    user: UserInfo = Depends(require_role("admin", "super_admin")),
) -> SupersedeOrphanPartialsResponse:
    """Clean up partial runs that landed before the supersession logic
    included `'partial'` in its WHERE clause.

    Idempotent: partials without a later succeeded run on the same
    `(workspace_id, cycle_key)` stay untouched.
    """
    try:
        count = await uc.supersede_orphan_partials_uc(_ctx(user), user=user)
    except ServiceError as e:
        raise _to_http_brain(e)
    return SupersedeOrphanPartialsResponse(superseded=count)


# ---------------------------------------------------------------------------
# Wave 3.1 silent-skip v3 follow-up — polish backfill for historic entries.
# ---------------------------------------------------------------------------


class PolishPendingResponse(BaseModel):
    """Outcome envelope for POST /journal/polish-pending."""

    updated: int
    remaining: int
    skipped: dict[str, int] = {}
    skipped_reason: str | None = None


@router.post(
    "/journal/polish-pending",
    response_model=PolishPendingResponse,
    summary=(
        "Backfill polish for historic journal entries with NULL narrative_polished"
        " (Wave 3.1 silent-skip v3 follow-up)"
    ),
)
async def polish_pending_journal_entries(
    limit: int = Query(default=50, ge=1, le=200),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> PolishPendingResponse:
    """Polish the next `limit` non-empty journal entries that still have
    `narrative_polished IS NULL`.

    Built for the silent-skip v3 cohort: ~360 entries landed in DB before the
    X-Sync header fix and `polish_run_journals` only re-polishes inside its
    own cycle run. Callers should invoke this repeatedly (cap 200) until
    `remaining == 0`.

    Each call:
      * Bounded by `limit` to avoid holding the writer lock for minutes.
      * Logs `polish_pending_journals start/done` so journalctl shows the
        backfill progress without needing to query the DB.
      * Returns `{updated, remaining, skipped: {...}}` so the operator can
        decide when to stop calling.
    """
    try:
        result = await uc.polish_pending_journal_uc(
            _ctx(user), limit=int(limit), user=user
        )
    except ServiceError as e:
        raise _to_http_brain(e)
    return PolishPendingResponse(
        updated=int(result.get("updated", 0)),
        remaining=int(result.get("remaining", 0)),
        skipped=dict(result.get("skipped", {})),
        skipped_reason=result.get("skipped_reason"),
    )


# ---------------------------------------------------------------------------
# Wave 3.1 gap 7 — promote a Brain finding to a Marvis task.
# ---------------------------------------------------------------------------


class PromoteFindingToTaskRequest(BaseModel):
    """Payload for POST /findings/{id}/promote-to-task."""

    model_config = ConfigDict(extra="forbid")

    title_override: str | None = None
    project_override: str | None = None
    description_override: str | None = None


class PromoteFindingToTaskResponse(BaseModel):
    """Outcome envelope: created task + finding state transition."""

    task_id: str
    finding_id: str
    finding_superseded_by: str
    project: str
    title: str


@router.post(
    "/findings/{finding_id}/promote-to-task",
    response_model=PromoteFindingToTaskResponse,
    summary="Promote a Brain finding to a Marvis task (Wave 3.1 gap 7)",
)
async def promote_finding_to_task(
    finding_id: str,
    body: PromoteFindingToTaskRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> PromoteFindingToTaskResponse:
    """Convert a Brain finding into a tracked Marvis task.

    Lifecycle:
      * Reads finding (must be `open` or `approved`).
      * Builds task title/description (Marvis convention) from the finding.
      * Maps severity → ICE-D impact (low=4, medium=6, high=8, critical=10);
        confidence and ease default to 7/5 per task spec; delegation=hybrid;
        source='brain' so the audit trail keeps the Brain provenance.
      * INSERTs the task in `approved` state — promotion implies the Brain
        already flagged this as actionable; no second human approval needed.
      * Updates the finding: approval_state='superseded',
        superseded_by_finding_id=f'task:{task_id}'. Records the transition
        in `brain_finding_states`.
    """
    try:
        result = await uc.promote_finding_to_task_uc(
            _ctx(user),
            finding_id=finding_id,
            title_override=body.title_override,
            project_override=body.project_override,
            description_override=body.description_override,
            user=user,
        )
    except ServiceError as e:
        raise _to_http_brain(e)
    return PromoteFindingToTaskResponse(**result)
