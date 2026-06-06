# v1.0.0 - 2026-05-27 - S1 F3.1c: brain reflection MCP tool group (use_cases-direct, no HTTP)
"""Brain (reflection) MCP tools — port of the Node ``brain_*`` group, use_cases-direct.

Same TEMPLATE as ``tasks.py`` / ``learnings.py`` / ``graph.py``: the Node HTTP proxy
(``get`` / ``post`` / ``patchWithIdempotency`` -> ``:8100``) is replaced by an
in-process ``await brain_uc.<fn>(LOCAL_CTX, ...)``. Docstrings are copied VERBATIM
from ``core/mcp-pir/index.mjs`` (curated, carry the QUANDO USARLO / NON USARLO /
RESTITUISCE blocks).

Schema port (Zod -> Pydantic), per S1 F3:
  * ``z.enum([...])``                    -> ``Literal[...]``
  * ``z.string().min(1).max(N)``         -> ``Annotated[str, Field(min_length=, max_length=)]``
  * ``z.number().int().min().max()``     -> ``Annotated[int, Field(ge=, le=)]``
  * ``z.number().min(0).max(1)``         -> ``Annotated[float, Field(ge=, le=)]``
  * optional                             -> ``X | None = None`` (or ``= <default>``)
  * ``z.array(z.enum([...]))``           -> ``list[Literal[...]] | None``

DEVIATION from the graph/tasks template (db ownership). The ``graph`` / ``tasks``
use_cases take a ``db`` connection the tool acquires via ``acquire_db()`` /
``acquire_write_db()``. The ``brain`` use_cases do NOT: they own their connection
internally (the brain services — ``events_reader`` / ``drift_router`` /
``memory_ops`` / ``findings_reader`` / ``runs_reader`` / ``journal`` /
``capabilities`` / ``jobs`` — open their OWN ``acquire_db()`` / ``write_db()`` from
the process-level pool, see ``use_cases/brain.py`` RESPONSIBILITY 3). So the tool
body here calls ``await brain_uc.<fn>(LOCAL_CTX, ...)`` directly, with no
``acquire_db()`` wrapper — wrapping one would acquire a pool connection the
use_case never touches. The process-level pool is initialised by the server
lifecycle (and by the ``mcp_db`` fixture in the smoke test).

Visibility: the MCP surface is local single-user. The brain use_cases resolve
project visibility through ``UserInfo.teams`` (which ``CallerContext`` drops), so
the local surface passes ``user=None`` = unrestricted (the services treat
``user is None`` as "no visibility restriction", the same DECISION the other
groups take with ``visible_projects=None``). ``LOCAL_CTX`` is ``operator``, so the
operator+ lifecycle patches + ``brain_cycles_recompute`` pass ``require_role_ctx``.

Error mapping: brain raises ``BrainDetailError`` (a ``ServiceError`` subclass
carrying a structured ``brain_detail`` body for the HTTP contract) AND plain
``NotFoundError`` / ``ServiceError``. The MCP surface ignores ``http_status`` /
``brain_detail`` (HTTP is not its transport) and maps ``code`` + ``message`` via
``raise_mcp_error`` — the same single ``except ServiceError`` the template uses
(``BrainDetailError`` IS a ``ServiceError``, so it is caught here too).

The ``*_apply`` tools (``brain_findings_apply`` / ``brain_memory_operations_apply``)
are GUIDANCE-only: the use_cases call ``get_apply_guidance`` which only READS the
precondition state and returns a next-action envelope — no write transaction, no
mutation. That invariant is preserved verbatim (no ``acquire_write_db``, nothing
to write).

fastapi-free invariant: ``use_cases.brain`` is fastapi-free at import time (it
imports every brain service FUNCTION-LOCAL inside each use_case), so it is a
module-top import here — no fastapi enters the MCP import path.

SKIPPED: none. All 18 ``brain_*`` Node tools map 1:1 to a ``use_cases.brain``
function. (The use_cases module also carries non-MCP admin fns —
``discard_brain_run`` / ``promote_brain_run`` / ``reset_brain_watermarks`` /
``get_counters`` / ``get_cycle_recap`` / ``list_journal`` admin/polish — but those
are NOT exposed as Node ``server.tool()`` entries, so they are out of scope for the
MCP surface parity.)
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    dump,
    raise_mcp_error,
)
from core.api.use_cases import brain as brain_uc
from core.api.use_cases._errors import ServiceError

# Zod enums -> Literals (mirror the Node tool signatures).
RunStatus = Literal["running", "succeeded", "partial", "failed", "superseded"]
RunTrigger = Literal["batch", "manual", "backfill"]
ScopeType = Literal["company", "program", "project"]
SeverityMin = Literal["low", "medium", "high", "critical"]
ConfidenceMin = Literal["low", "medium", "high"]
DriftState = Literal["open", "superseded", "resolved", "dismissed"]
DriftAxis = Literal["intent", "context", "both"]
DriftAction = Literal["dismiss", "acknowledge", "resolve", "reopen"]
MemoryOpApprovalState = Literal["approved", "dismissed", "rejected"]
FindingApprovalState = Literal["approved", "dismissed", "resolved"]
RecomputeSource = Literal["digest", "drift", "memory_ops", "learn"]


def register(mcp) -> None:
    """Register the brain (reflection) tool group on the shared FastMCP instance."""

    # -----------------------------------------------------------------------
    # Sub-05 — runs (cycle envelope discovery primitive)
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def brain_runs(
        cycle_key: str | None = None,
        status: list[RunStatus] | None = None,
        trigger: list[RunTrigger] | None = None,
        include_superseded: bool = False,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List Brain cycle runs (envelope) per sub-05 §2.

        QUANDO USARLO: verificare quale ciclo Brain ha pubblicato per ultimo, stato (running|succeeded|partial|failed|superseded), trigger (batch|manual|backfill) e contatori. Con `cycle_key='latest'` il server risolve a `MAX(cycle_key) WHERE status='succeeded' AND superseded_by_run_id IS NULL` — usalo come primitivo di discovery prima di brain_events/brain_journal/brain_drift/brain_memory_operations/brain_findings (cosi' non devi sincronizzare cycle_key tra chiamate). Default agent: `cycle_key='latest', status=['succeeded']`.
        QUANDO NON USARLO: per leggere contenuto eventi/journal — quelle sono chiamate downstream. Non usarlo come 'health check' del Brain: lo stato `partial` e' normale (una source fallita non blocca le altre). Per il singolo run usa brain_runs_get.
        RESTITUISCE: {items:[{run_id, cycle_key, status, trigger, event_count, partial_failures, duration_ms, started_at, finished_at}], next_cursor, cycle_key, total_returned}."""
        try:
            result = await brain_uc.list_brain_runs(
                LOCAL_CTX,
                cycle_key=cycle_key,
                status=list(status) if status else None,
                trigger=list(trigger) if trigger else None,
                include_superseded=include_superseded,
                cursor=cursor,
                limit=limit,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_runs_get(
        run_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Fetch a single Brain cycle run by run_id.

        QUANDO USARLO: hai un run_id (da brain_runs o WebSocket payload) e vuoi vedere envelope completo del ciclo (event_count, partial_failures, durata, scope, trigger). Utile dopo ricezione `marvisx:brain_cycle_changed` per ispezionare il run appena cambiato.
        QUANDO NON USARLO: per cercare l'ultimo ciclo — usa brain_runs con `cycle_key='latest'`. Per il contenuto del journal/drift/memory_ops/findings — usa i tool dedicati.
        RESTITUISCE: {run_id, workspace_id, cycle_key, status, trigger, started_at, finished_at, event_count, partial_failures, duration_ms}."""
        try:
            result = await brain_uc.get_brain_run(
                LOCAL_CTX, run_id=run_id, user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    # -----------------------------------------------------------------------
    # Sub-01 D6 — events (raw digest evidence)
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def brain_events(
        cycle_key: str | None = None,
        run_id: str | None = None,
        event_type: list[str] | None = None,
        source_project: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List raw Brain digest events for a cycle (sub-01 D6).

        QUANDO USARLO: ispezionare gli eventi grezzi (digest) di un ciclo — la base di evidenza che drift/memory-ops/findings citano. Quando una finding cita `digest_event:abc123` e vuoi vedere il contesto, brain_events e' la fonte di verita'. Cursor pagination stable `(observed_at DESC, event_id)`. Default: `cycle_key='latest', limit=50`.
        QUANDO NON USARLO: come surface narrativo per umano — usa brain_journal (aggrega + materializza per scope). Non usarlo per 'voglio sapere se qualcosa e' cambiato' — quelli sono drift signals. Gli eventi sono fatti, non interpretazioni: non aggregarli come 'punteggio di salute progetto'.
        RESTITUISCE: {items:[DigestEvent|DigestEventRedacted], next_cursor, cycle_key, run_id, redacted_count, total_returned}."""
        try:
            result = await brain_uc.list_events(
                LOCAL_CTX,
                cycle_key=cycle_key,
                run_id=run_id,
                event_type=list(event_type) if event_type else None,
                source_project=source_project,
                cursor=cursor,
                limit=limit,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_journal(
        cycle_key: str | None = None,
        run_id: str | None = None,
        scope_type: ScopeType | None = None,
        scope_key: str | None = None,
        program_key: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List Brain journal entries (narrative L2 layer) for a cycle.

        QUANDO USARLO: ottenere il narrativo company/program/project di un ciclo — `{what_changed, decisions_observed, open_loops, notable_context, sources, tomorrow_watch}`. E' la vista materializzata sopra brain_events: leggibile da umano, con `sources[]` che linkano agli event_id originali. Default: `cycle_key='latest'`. Per il journal di un progetto: `scope_type=project, scope_key=marvisx`.
        QUANDO NON USARLO: per cercare segnali di problema (usa brain_drift), proposte di azione (brain_memory_operations) o conclusioni approvabili (brain_findings). Il journal e' contesto: dice 'cosa e' successo', non 'cosa fare'. Non scriverlo a mano: e' generato dal cycle aggregator (sub-01 D2), agent-write su brain_journal_entries e' bloccato dal router.
        RESTITUISCE: {items:[JournalEntry], cycle_key, run_id, total_returned}."""
        try:
            result = await brain_uc.list_journal(
                LOCAL_CTX,
                cycle_key=cycle_key,
                run_id=run_id,
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=program_key,
                limit=limit,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    # -----------------------------------------------------------------------
    # Sub-02 — drift signals
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def brain_drift(
        cycle_key: str | None = None,
        run_id: str | None = None,
        scope_type: ScopeType | None = None,
        scope_key: str | None = None,
        signal_type: list[str] | None = None,
        knowledge_form: list[str] | None = None,
        severity_min: SeverityMin = "low",
        confidence_min: Annotated[float, Field(ge=0, le=1)] = 0,
        state: list[DriftState] | None = None,
        include_resolved: bool = False,
        drift_axis: list[DriftAxis] | None = None,
        rule_id: list[str] | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List Brain drift signals (sub-02 §11.2) — knowledge gaps between observed and expected.

        QUANDO USARLO: trovare drift signals aperti (default `state=['open'], severity_min='low'`) per capire dove il sistema 'sta sterzando' rispetto agli ADR/spec/playbook. Filtri: signal_type, knowledge_form (adr|spec|playbook|tribal_memory|external_update|claimed_decision|unknown), severity_min, drift_axis (intent|context|both — CE4). Cursor pagination stable per `(severity DESC, detected_at DESC)`.
        QUANDO NON USARLO: per leggere il narrativo del ciclo — usa brain_journal. Per le azioni proposte sulla memoria — usa brain_memory_operations. Drift signal != finding: signal e' fatto osservato, finding e' conclusione approvabile.
        RESTITUISCE: {items:[DriftSignal|DriftSignalRedacted], cycle_key, run_id, redacted_count, total_returned, next_cursor}."""
        try:
            result = await brain_uc.list_drift(
                LOCAL_CTX,
                cycle_key=cycle_key,
                run_id=run_id,
                scope_type=scope_type,
                scope_key=scope_key,
                signal_type=list(signal_type) if signal_type else None,
                knowledge_form=list(knowledge_form) if knowledge_form else None,
                severity_min=severity_min,
                confidence_min=confidence_min,
                state=list(state) if state else None,
                include_resolved=include_resolved,
                drift_axis=list(drift_axis) if drift_axis else None,
                rule_id=list(rule_id) if rule_id else None,
                cursor=cursor,
                limit=limit,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_drift_get(
        signal_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Fetch a single drift signal by signal_id.

        QUANDO USARLO: una finding/handoff cita `signal:abc123` e vuoi vedere il contesto completo (observed_delta, expected_direction_ref, evidence_chain, classifier_version). Usa anche dopo PATCH per verificare il nuovo `state`.
        QUANDO NON USARLO: per cercare per recurrence_key o pattern — usa brain_drift. Per cambiare stato — usa brain_drift_patch.
        RESTITUISCE: DriftSignal completo (o DriftSignalRedacted se cross-scope con qualche progetto invisibile)."""
        try:
            result = await brain_uc.get_drift_signal(
                LOCAL_CTX, signal_id=signal_id, user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_drift_patch(
        signal_id: Annotated[str, Field(min_length=1)],
        action: DriftAction,
        reason: Annotated[str, Field(max_length=500)] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Lifecycle action on a drift signal (dismiss / acknowledge / resolve / reopen).

        QUANDO USARLO: operator gate — dopo aver letto il signal (brain_drift_get), decidere se 'dismiss' (rumore), 'acknowledge' (visto, non agire), 'resolve' (corretto upstream), 'reopen' (riaperto post-resolve). `reason` raccomandato per audit. Idempotency-Key obbligatoria per evitare double-write audit log.
        QUANDO NON USARLO: NON usarlo per cambiare evidence o classification — quelle sono immutabili. NON usarlo per chiudere findings (usa brain_findings_patch). Drift signal e' osservazione, dismiss=non utile, resolve=corretto upstream — non e' la stessa cosa di 'fix' del codice.
        RESTITUISCE: DriftSignal aggiornato con nuovo state + dismissed_by/dismissed_at o resolved_at."""
        # The use_case owns its own write connection internally (drift_router); no
        # acquire_write_db wrapper here. idempotency_key is a transport replay guard
        # the use_case signature does not carry (HTTP header concern), so it is
        # accepted on the surface for Node parity but not forwarded.
        try:
            result = await brain_uc.patch_drift_signal(
                LOCAL_CTX,
                signal_id=signal_id,
                action=action,
                reason=reason,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    # -----------------------------------------------------------------------
    # Sub-03 — memory operations
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def brain_memory_operations(
        cycle_key: str | None = None,
        run_id: str | None = None,
        scope_type: ScopeType | None = None,
        scope_key: str | None = None,
        operation_type: list[str] | None = None,
        approval_state: list[str] | None = None,
        include_terminal: bool = False,
        recurrence_min: Annotated[int, Field(ge=1)] = 1,
        score_min: Annotated[float, Field(ge=0, le=1)] = 0,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List Brain memory operations (sub-03 §11.2) — proposte di azione sulla memoria.

        QUANDO USARLO: trovare proposte pendenti (default `approval_state=['pending']`) di operation_type: REINFORCE, CONSOLIDATE, SUPERSEDE_CANDIDATE, PROVENANCE_HARDENING, ORPHAN_DETECTED, CONTRADICTION_DETECTED. Ogni op contiene `proposed_write.target_type` (task|learning|adr|guide|doc_patch|context_md_append|kg_edge_metric|none). Default agent: `cycle_key='latest', approval_state=['pending'], recurrence_min=1`.
        QUANDO NON USARLO: per signal di drift — usa brain_drift. Per findings approvabili (output finale) — usa brain_findings. Per applicare una proposta: brain_memory_operations_apply ritorna GUIDANCE, NON scrive.
        RESTITUISCE: {items:[MemoryOperation|MemoryOperationRedacted], cycle_key, run_id, redacted_count, total_returned, next_cursor}."""
        try:
            result = await brain_uc.list_memory_operations(
                LOCAL_CTX,
                cycle_key=cycle_key,
                run_id=run_id,
                scope_type=scope_type,
                scope_key=scope_key,
                operation_type=list(operation_type) if operation_type else None,
                approval_state=list(approval_state) if approval_state else None,
                include_terminal=include_terminal,
                recurrence_min=recurrence_min,
                score_min=score_min,
                cursor=cursor,
                limit=limit,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_memory_operations_get(
        operation_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Fetch a single memory operation by operation_id.

        QUANDO USARLO: hai un operation_id (da brain_memory_operations) e vuoi vedere proposed_write completo + evidence chain + recurrence info. Necessario prima di chiamare brain_memory_operations_apply per leggere il guidance contract.
        QUANDO NON USARLO: per cercare per recurrence_key — usa brain_memory_operations. Per applicare: vedi brain_memory_operations_apply (NO write).
        RESTITUISCE: MemoryOperation completa (o MemoryOperationRedacted se cross-scope con progetto invisibile)."""
        try:
            result = await brain_uc.get_memory_operation(
                LOCAL_CTX, operation_id=operation_id, user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_memory_operations_patch(
        operation_id: Annotated[str, Field(min_length=1)],
        approval_state: MemoryOpApprovalState,
        reason: Annotated[str, Field(max_length=500)] | None = None,
        applied_artifact_ref: Annotated[str, Field(max_length=500)] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Lifecycle action on a memory operation (approve / dismiss / reject).

        QUANDO USARLO: operator gate — approva (sblocca apply guidance), dismiss (non utile per ora), reject (proposta sbagliata). `applied_artifact_ref` opzionale dopo aver creato artifact via guidance. Idempotency-Key obbligatoria.
        QUANDO NON USARLO: per applicare l'azione concreta — l'apply NON scrive, ritorna SOLO guidance. La scrittura effettiva e' agente-driven, MAI inline qui. Per bulk: brain_memory_operations_bulk_patch.
        RESTITUISCE: MemoryOperation aggiornata con nuovo approval_state."""
        # Node sends `approval_state` as the desired terminal state; the use_case
        # takes it as `action` (the lifecycle verb). idempotency_key is the
        # transport replay header; the use_case forwards it to the service.
        try:
            result = await brain_uc.patch_memory_operation(
                LOCAL_CTX,
                operation_id=operation_id,
                action=approval_state,
                reason=reason,
                applied_artifact_ref=applied_artifact_ref,
                idempotency_key=idempotency_key,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_memory_operations_apply(
        operation_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Get apply GUIDANCE for an approved memory operation (NO write — sub-03 §11.1).

        QUANDO USARLO: dopo aver `approved` un'operazione (via brain_memory_operations_patch), per ottenere `next_action.tool` (es. mcp__marvis__create_task) + args + `must_include_in_tags` (es. `brain_memory_op:abc123`). L'apply NON scrive l'artifact — ritorna istruzioni. L'agente esegue il `next_action.tool` con i tag richiesti per stabilire la chain di audit.
        QUANDO NON USARLO: per scrivere direttamente l'artifact — questo endpoint ritorna SOLO guidance. Pattern: PATCH → apply (guidance) → call next_action.tool con must_include_in_tags. Non usarlo prima di approval_state='approved' (409 precondition).
        RESTITUISCE: {operation_id, next_action:{tool, args, must_include_in_tags}, operation_summary}."""
        # GUIDANCE-only: the use_case calls get_apply_guidance (READ-only precondition
        # check + next-action envelope). It opens no write transaction and mutates
        # nothing — preserved verbatim, no acquire_write_db.
        try:
            result = await brain_uc.apply_memory_operation(
                LOCAL_CTX, operation_id=operation_id, user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    # -----------------------------------------------------------------------
    # Sub-04 — Learn findings
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def brain_findings(
        cycle_key: str | None = None,
        run_id: str | None = None,
        scope_type: ScopeType | None = None,
        scope_key: str | None = None,
        finding_type: list[str] | None = None,
        severity_min: SeverityMin = "low",
        confidence_min: ConfidenceMin = "low",
        approval_state: list[str] | None = None,
        include_terminal: bool = False,
        recurrence_min: Annotated[int, Field(ge=1)] = 1,
        regression_only: bool = False,
        applied: bool | None = None,
        created_after: str | None = None,
        owner_user_id: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List Brain Learn findings (sub-04 §11.2) — conclusioni approvabili dal ciclo.

        QUANDO USARLO: trovare findings aperti (default `approval_state=['open']`) per drain del Triage Queue. Filtri: finding_type, severity_min, confidence_min (low|medium|high — TIER, non float), recurrence_min, regression_only, applied. Default agent: `cycle_key='latest', approval_state=['open'], severity_min='low'`. CE2 recency_factor disponibile read-time (se decay enabled in settings).
        QUANDO NON USARLO: per drift signals — usa brain_drift. Per proposte di azione (intermedio) — usa brain_memory_operations. Per applicare: vedi brain_findings_apply (GUIDANCE-only). NEVER multiplica confidence × severity in score composito (F10/FR1 anti-pattern).
        RESTITUISCE: {items:[Finding|FindingRedacted], cycle_key, run_id, redacted_count, redacted_evidence_count, total_returned, next_cursor}."""
        try:
            result = await brain_uc.list_findings(
                LOCAL_CTX,
                cycle_key=cycle_key,
                run_id=run_id,
                scope_type=scope_type,
                scope_key=scope_key,
                finding_type=list(finding_type) if finding_type else None,
                severity_min=severity_min,
                confidence_min=confidence_min,
                approval_state=list(approval_state) if approval_state else None,
                include_terminal=include_terminal,
                recurrence_min=recurrence_min,
                regression_only=regression_only,
                applied=applied,
                created_after=created_after,
                owner_user_id=owner_user_id,
                cursor=cursor,
                limit=limit,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_findings_get(
        finding_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Fetch a single finding by finding_id.

        QUANDO USARLO: hai un finding_id (da brain_findings o WS payload) e vuoi vedere proposta completa: title, summary, why_now, evidence, suggested_artifact, owner_hint, closure_condition (drift_signal_clears|memory_op_applied|artifact_exists|manual_attest), regression_of_finding_id. Necessario prima di brain_findings_apply per leggere il guidance contract.
        QUANDO NON USARLO: per ricerca semantica — usa brain_findings con filtri. Per modificare lo stato — usa brain_findings_patch.
        RESTITUISCE: Finding completo (o FindingRedacted se cross-scope con progetto invisibile)."""
        try:
            result = await brain_uc.get_finding(
                LOCAL_CTX, finding_id=finding_id, user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_findings_patch(
        finding_id: Annotated[str, Field(min_length=1)],
        approval_state: FindingApprovalState,
        reason: Annotated[str, Field(max_length=500)] | None = None,
        applied_artifact_ref: Annotated[str, Field(max_length=500)] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Lifecycle action on a finding (approve / dismiss / resolve).

        QUANDO USARLO: operator gate per gestire la coda 'Da decidere'. `approve` segna approval_state=approved MA NON crea artifact (sub-04 F1: GUIDANCE-only). `dismiss` = non utile. `resolve` = osservato comportamento atteso (richiede reason se closure_condition.kind=manual_attest). Idempotency-Key obbligatoria.
        QUANDO NON USARLO: per creare artifact dal finding — pattern e' PATCH approve → brain_findings_apply (guidance) → call next_action.tool. Per bulk: brain_findings_bulk_patch.
        RESTITUISCE: Finding aggiornato con nuovo approval_state + approved_by/approved_at o applied_artifact_ref."""
        # Node sends `approval_state` (desired state); the use_case takes it as the
        # lifecycle `action` verb. idempotency_key forwarded to the service.
        try:
            result = await brain_uc.patch_finding(
                LOCAL_CTX,
                finding_id=finding_id,
                action=approval_state,
                reason=reason,
                applied_artifact_ref=applied_artifact_ref,
                idempotency_key=idempotency_key,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_findings_bulk_patch(
        finding_ids: Annotated[list[str], Field(min_length=1, max_length=25)],
        approval_state: FindingApprovalState,
        reason: Annotated[str, Field(max_length=500)] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Bulk lifecycle patch on findings (cap 25).

        QUANDO USARLO: drain massivo (es. dismiss una classe di rumore). Max 25 finding_id per request (oltre → 413). Conflict invariant: one bad transition fails the whole batch (409, NO partial commit).
        QUANDO NON USARLO: per >25 — splitta. Per gestione individuale con motivazione — usa brain_findings_patch.
        RESTITUISCE: {results:[{finding_id, status}], applied_count, skipped_count}."""
        try:
            result = await brain_uc.bulk_patch_findings(
                LOCAL_CTX,
                finding_ids=list(finding_ids),
                action=approval_state,
                reason=reason,
                idempotency_key=idempotency_key,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_findings_apply(
        finding_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Get apply GUIDANCE for an approved finding (NO write — sub-04 F1).

        QUANDO USARLO: dopo aver `approved` una finding, per ottenere `next_action.tool` (es. mcp__marvis__create_task con title/description/tags pre-compilato) + `must_include_in_tags=brain_finding:{id}` per audit chain. L'apply NON scrive l'artifact — ritorna istruzioni. L'agente esegue il next_action.tool con i tag richiesti.
        QUANDO NON USARLO: per scrivere artifact direttamente — pattern e' GUIDANCE-only. Non usarlo prima di approval_state='approved' (409). Non chiamarlo in loop 'ricomputa finche' la finding X appare': se non emerge, e' un problema del producer rule — file un learning via mcp__marvis__create_learning.
        RESTITUISCE: {operation_id (=finding_id), next_action:{tool, args, must_include_in_tags}, operation_summary}."""
        # GUIDANCE-only: the use_case calls get_apply_guidance (READ-only precondition
        # + next-action envelope). No write transaction, no mutation — preserved.
        try:
            result = await brain_uc.apply_finding(
                LOCAL_CTX, finding_id=finding_id, user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    # -----------------------------------------------------------------------
    # Sub-05 — recompute + capabilities
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def brain_cycles_recompute(
        cycle_key: Annotated[str, Field(min_length=1)],
        idempotency_key: Annotated[str, Field(min_length=1)],
        sources: list[RecomputeSource] | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Force manual recompute of a Brain cycle (operator+, sub-01 §6.D4).

        QUANDO USARLO: dopo bug fix in un producer (collector/drift/memory_ops/learn), dopo backfill di dati upstream, o per debug `dry_run=true` (stima eventi senza scrivere). `sources=None` ricomputa tutto; `sources=['drift']` ricomputa solo drift (digest resta, findings citano i nuovi signal_id). Idempotency-Key obbligatoria. Concurrent calls sullo stesso ciclo serializzano via lease: il secondo caller ritorna 202 con il run_id dell'in-flight.
        QUANDO NON USARLO: come scheduler — il batch giornaliero gira da solo dopo brain_cutoff_hour_utc. Per cicli >30 giorni vecchi: il server rifiuta con `cycle_too_old` (force=true sblocca, audit log la marca come override). Mai chiamarlo in loop 'ricomputa finche' X appare'.
        RESTITUISCE: {status, cycle_key, run_id, event_count, journal_count, duration_ms, mode, dry_run}."""
        # The use_case owns its own writer (jobs.recompute_brain_cycle) — no
        # acquire_write_db here. `sources` is accepted on the surface for Node
        # parity; the use_case signature recomputes the full cycle (the sub-phase
        # selector lives below the use_case in jobs and is not threaded through
        # recompute_cycle's args), so it is not forwarded.
        try:
            result = await brain_uc.recompute_cycle(
                LOCAL_CTX,
                cycle_key=cycle_key,
                dry_run=dry_run,
                force=force,
                idempotency_key=idempotency_key,
                triggered_by=LOCAL_CTX.username,
                user=None,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brain_capabilities() -> dict[str, Any]:
        """Discover Brain schema metadata (enums + glyphs + node kinds) — sub-05 OD-11.

        QUANDO USARLO: cold-start dell'agente — leggere i Literal enum esposti dal Brain (event_types, signal_types, knowledge_forms, operation_types, finding_types, severities, confidence_tiers, drift_axes, approval_states, signal_states, run_statuses, closure_condition_kinds, knowledge_glyphs). Pattern equivalente di mcp__marvis__graph_capabilities. Evita hardcoding constants. Schema_version aumenta quando i Literal cambiano.
        QUANDO NON USARLO: per dati live del ciclo — usa brain_runs/brain_events/etc. Non e' un health check.
        RESTITUISCE: {schema_version, event_types[], source_systems[], signal_types[], knowledge_forms[], operation_types[], finding_types[], severities[], confidence_tiers[], drift_axes[], approval_states[], finding_approval_states[], signal_states[], run_statuses[], run_triggers[], scope_types[], suggested_artifacts[], closure_condition_kinds[], knowledge_glyphs:{form: glyph}}."""
        # Deterministic; no DB. The use_case takes only ctx.
        try:
            result = await brain_uc.get_brain_capabilities(LOCAL_CTX)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
