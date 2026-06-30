# v1.0.0 - 2026-05-27 - S1 F3.1d: pull-requests MCP tool group (use_cases-direct, no HTTP)
"""Pull-requests MCP tools — port of the Node PR/branch group, use_cases-direct.

Same TEMPLATE as ``tasks.py`` / ``graph.py``: the Node HTTP proxy
(``get``/``post`` -> ``:8100``) is replaced by an in-process
``await pr_uc.<fn>(LOCAL_CTX, db, ...)`` against the read/write pool the tool
acquires via ``acquire_db()`` / ``acquire_write_db()``. Docstrings are copied
VERBATIM from ``core/mcp-pir/index.mjs`` (curated QUANDO USARLO / NON USARLO /
RESTITUISCE blocks).

Schema port (Zod -> Pydantic), per S1 F3:
  * ``z.string().min(1)``         -> ``Annotated[str, Field(min_length=1)]``
  * ``z.string().optional()``     -> ``str | None = None``
  * ``z.string().optional().default("")`` -> ``str = ""``
  * ``z.boolean().optional()``    -> ``bool | None = None``

Visibility / four-eyes: the MCP surface uses ``LOCAL_CTX`` as an operator agent by
default, so role-level ``require_role_ctx`` gates inside the write use_cases pass.
Explicit triage actions that need the richer HTTP ``UserInfo`` resolve a real
tenant human reviewer instead of the submitting agent identity, preserving the
domain four-eyes gate while replacing Console Triage for hosted/self-hosted MCP.
Merge/revert are exposed here because hosted/self-hosted MCP has no Console Triage
cookie path: MCP must be able to exercise the same use_cases directly and let the
domain layer enforce PR state, CI, merge-order, git, and RBAC rules.

``get_pr`` deep KG context (DECISION 2 in the use_case) is a per-surface adapter
concern (rate-limit + access log + ``build_kg_context_for_pr``, which pulls fastapi
via ``services.kg.audit``). The MCP surface returns the PR status dict as-is; the
``deep`` flag is accepted for Node parity but not used to attach KG context here
(the same SKIP the graph group takes for fastapi-bound enrichment). Agents that want
the chain can call ``graph_context`` on the PR node.

fastapi-free invariant: ``use_cases.pull_requests`` is fastapi-free at import time
(it imports ``pr_service`` — a Fase-2 fastapi conversion target — FUNCTION-LOCAL
inside each use_case), so it is a module-top import here. No fastapi enters the MCP
import path. The plain ``GitOpsError`` / ``MergeConflictError`` the use_cases
re-raise UNCHANGED (for the HTTP adapter's exact legacy 409/500 bodies) are
``ServiceError`` subclasses, so the single ``except ServiceError`` catches them and
``raise_mcp_error`` maps ``code`` + ``message`` (the MCP surface ignores
``http_status`` / ``conflicting_files`` — HTTP is not its transport).

SKIPPED (no clean fastapi-free use_case):
  * ``git_log`` / ``git_diff`` — the logic lives in router-PRIVATE helpers
    (``_git_log`` / ``_git_diff`` defined IN ``routers/projects.py``); there is no
    ``use_cases`` function to call. Porting would mean importing the projects router
    (fastapi) or re-implementing the git shell-out helpers, neither of which is a
    use_cases-direct port — skipped per the S1 F3 SKIP rule.
  * ``triage_docs_change`` — backed by ``services.docs_governance.triage_orchestrator``
    called DIRECTLY by the router; there is no ``use_cases`` function (the service is
    fastapi-free, but the task scope is "port only if a use_case exists"). Skipped
    until the orchestrator is wrapped in a use_case seam.
"""
from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_db,
    acquire_write_db,
    dump,
    raise_mcp_error,
)
from core.api.models.auth import UserInfo
from core.api.use_cases import pull_requests as pr_uc
from core.api.use_cases._errors import ConflictError, ServiceError


async def _explicit_mcp_reviewer(db, action: str) -> UserInfo:
    """Resolve the real tenant user that explicit MCP triage records as reviewer."""
    configured_user_id = os.environ.get("MARVIS_MCP_TRIAGE_USER_ID", "").strip()
    if configured_user_id:
        cursor = await db.execute(
            """
            SELECT id, slug, type, system_role, workspace_id
            FROM users
            WHERE id = ?
            """,
            (configured_user_id,),
        )
        row = await cursor.fetchone()
    else:
        workspace_id = LOCAL_CTX.workspace_id or "ws_default"
        cursor = await db.execute(
            """
            SELECT id, slug, type, system_role, workspace_id
            FROM users
            WHERE type = 'human'
              AND system_role IN ('admin', 'super_admin')
              AND id != ?
              AND COALESCE(workspace_id, ?) = ?
            ORDER BY CASE system_role WHEN 'super_admin' THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (LOCAL_CTX.user_id, workspace_id, workspace_id),
        )
        row = await cursor.fetchone()

    if row is None:
        hint = "Set MARVIS_MCP_TRIAGE_USER_ID to a real admin/super_admin user id."
        raise_mcp_error(
            ServiceError(
                code="mcp_triage_reviewer_missing",
                message=f"No real MCP triage reviewer found for {action}. {hint}",
            )
        )

    return UserInfo(
        username=row["slug"],
        user_id=row["id"],
        system_role=row["system_role"],
        user_type=row["type"],
        workspace_id=row["workspace_id"] or LOCAL_CTX.workspace_id,
        scopes=list(LOCAL_CTX.scopes),
        teams=[],
    )


def _raise_gitops_mcp_error(exc: pr_uc.GitOpsError) -> None:
    """Map git-domain exceptions that intentionally bypass ServiceError."""
    if isinstance(exc, pr_uc.MergeConflictError):
        files = ", ".join(exc.conflicting_files) or "unknown files"
        raise_mcp_error(
            ConflictError(
                code="merge_conflict",
                message=f"Merge conflict in: {files}",
            )
        )
    raise_mcp_error(ServiceError(code="git_ops_failed", message=str(exc)))


def register(mcp) -> None:
    """Register the pull-requests tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def create_branch(
        task_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Atomic create branch + worktree + draft PR (orchestrator-managed).

        QUANDO USARLO: hai un task approved/in_progress e ti serve un worktree isolato per iniziare a lavorare. Un'unica call crea branch `feat/task-{uuid}`, worktree in `~/dev/task-{uuid}`, e draft PR row in Marvis. Preferisci su `git worktree add` manuale + register_branch (2-step flow).
        QUANDO NON USARLO: NOT se il worktree esiste gia' (creato manualmente o fuori orchestrator) -> usa register_branch per attaccarlo al task. NOT se il task non e' ancora approved -> il backend risponde 400.
        RESTITUISCE: {task_id, branch_name, worktree_path, status:'draft'} idempotent."""
        try:
            # create_branch delegates to start_branch_short_write: git I/O happens
            # first, then the use case opens its own short writer to insert the PR.
            # Holding acquire_write_db here deadlocks the nested writer and can leave
            # an orphan worktree without a pull_requests row.
            async with acquire_db() as db:
                result = await pr_uc.create_branch(LOCAL_CTX, db, task_id=task_id)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def register_branch(
        task_id: Annotated[str, Field(min_length=1)],
        branch_name: Annotated[str, Field(min_length=1)],
        worktree_path: str | None = None,
    ) -> dict[str, Any]:
        """Attach an existing git branch/worktree to a task as draft PR record (idempotent).

        QUANDO USARLO: il worktree e' stato creato manualmente via `git worktree add` o fuori orchestrator, e serve la PR row Marvis per poter chiamare submit_pr dopo. BOUNDARY: register_branch crea draft; submit_pr promuove draft -> open per review.
        QUANDO NON USARLO: NOT quando vuoi che Marvis crei il worktree per te -> usa endpoint HTTP /api/v1/pull_requests/{task_id}/branch. NOT dopo submit -> il record e' gia' open.
        RESTITUISCE: {task_id, branch_name, worktree_path, status:'draft'} idempotent."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.register_branch(
                    LOCAL_CTX,
                    db,
                    task_id=task_id,
                    branch_name=branch_name,
                    worktree_path=worktree_path,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def submit_pr(
        task_id: Annotated[str, Field(min_length=1)],
        title: Annotated[str, Field(min_length=1)],
        body: str = "",
    ) -> dict[str, Any]:
        """Promote a draft PR to open for review (draft -> open, task in_progress -> review).

        QUANDO USARLO: SOLO dopo tutti i commit pushati e test_command + build pass locally (Quality Gate 9.3). BOUNDARY: register_branch crea draft; submit_pr promuove draft -> open per review. Poi approve_pr / merge_pr chiudono il ciclo via MCP.
        QUANDO NON USARLO: NOT senza aver verificato che test + build passano. NOT se il lavoro e' abbandonato -> usa close_pr.
        RESTITUISCE: {pr_status:'open', task_status:'review', submitted_at}."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.submit_pull_request(
                    LOCAL_CTX, db, task_id=task_id, title=title, body=body,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def approve_pr(
        task_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Approve an open PR through explicit MCP triage.

        QUANDO USARLO: il PR e' open, hai verificato diff/test, e serve sostituire Console Triage nel flusso hosted/MCP. BOUNDARY: approve_pr registra la review; merge_pr chiude il ciclo.
        QUANDO NON USARLO: NOT senza review reale dell'utente; NOT se devi chiedere modifiche -> usa request_pr_changes.
        RESTITUISCE: PR row aggiornata con approved_by / approved_at oppure errore 403/422 di dominio."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.approve_pull_request(
                    LOCAL_CTX,
                    db,
                    task_id=task_id,
                    reviewer=await _explicit_mcp_reviewer(db, "approve_pr"),
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def request_pr_changes(
        task_id: Annotated[str, Field(min_length=1)],
        comment: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Request changes on an open PR and send the task back to in_progress.

        QUANDO USARLO: review negativa o incompleta; vuoi lasciare feedback strutturato e riaprire il ciclo di lavoro.
        QUANDO NON USARLO: NOT per abbandonare il PR -> usa close_pr. NOT per piccoli ritocchi che puoi fare prima del submit.
        RESTITUISCE: PR row aggiornata; task torna in_progress con review_feedback."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.request_pull_request_changes(
                    LOCAL_CTX,
                    db,
                    task_id=task_id,
                    reviewer=await _explicit_mcp_reviewer(db, "request_pr_changes"),
                    comment=comment,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def update_pr(
        task_id: Annotated[str, Field(min_length=1)],
        title: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        """Update PR title/body without changing lifecycle state.

        QUANDO USARLO: il contenuto del PR e' corretto ma titolo/body sono incompleti o non chiari.
        QUANDO NON USARLO: NOT per submit -> usa submit_pr. NOT per feedback di review -> usa request_pr_changes.
        RESTITUISCE: PR row aggiornata."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.update_pull_request(
                    LOCAL_CTX,
                    db,
                    task_id=task_id,
                    title=title,
                    body=body,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def merge_pr(
        task_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Merge an open PR into its target branch through the shared PR use_case.

        QUANDO USARLO: PR open, test/CI verificati, eventuale review registrata, e vuoi chiudere il ciclo hosted via MCP senza Console. BOUNDARY: submit_pr apre review; merge_pr esegue merge git + task completed.
        QUANDO NON USARLO: NOT su draft -> usa submit_pr. NOT con dubbi su diff/test. NOT per abbandonare -> usa close_pr.
        RESTITUISCE: esito merge oppure errore di dominio (merge conflict, CI failing, PR non open, git failure)."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.merge_pull_request(LOCAL_CTX, db, task_id=task_id)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
        except pr_uc.GitOpsError as e:
            _raise_gitops_mcp_error(e)

    @mcp.tool()
    async def revert_pr(
        task_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Create a revert task/PR for a previously merged PR.

        QUANDO USARLO: merge gia' completato e va annullato con tracciamento Marvis, non con revert git manuale fuori sistema.
        QUANDO NON USARLO: NOT per PR open/draft -> usa close_pr o request_pr_changes. NOT se vuoi solo correggere avanti -> crea una nuova task.
        RESTITUISCE: {revert_task_id, revert_pr_id, branch} oppure errore git/422 di dominio."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.revert_pull_request(LOCAL_CTX, db, task_id=task_id)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
        except pr_uc.GitOpsError as e:
            _raise_gitops_mcp_error(e)

    @mcp.tool()
    async def get_pr(
        task_id: Annotated[str, Field(min_length=1)],
        deep: bool | None = None,
    ) -> dict[str, Any]:
        """Get current PR state for a task (status, branch, worktree, review_feedback, commit SHAs).

        QUANDO USARLO: verificare 'is my PR still open?' prima di continuare il lavoro, o leggere review_feedback dopo PR rimandato indietro. Usa ?deep=true per includere kg_context inline — risparmia 2-3 tool call aggiuntivi.
        QUANDO NON USARLO: NOT per PR state di piu' task in una call -> usa list_tasks (contiene pr_state).
        RESTITUISCE: {pr_status, branch, worktree_path, review_feedback?, commit_shas[], merged_at?} + kg_context se deep=true."""
        # `deep` accepted for Node parity; the KG-context attach (DECISION 2) is a
        # fastapi-bound adapter concern (services.kg.audit), so it is NOT done here —
        # agents wanting the chain call graph_context on the PR node.
        try:
            async with acquire_db() as db:
                result = await pr_uc.get_pull_request(LOCAL_CTX, db, task_id=task_id)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def close_pr(
        task_id: Annotated[str, Field(min_length=1)],
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Abandon a PR without merging (close record + unlink branch).

        QUANDO USARLO: il lavoro non serve piu' o deve essere redone da zero (task di solito va in rejected/failed).
        QUANDO NON USARLO: NOT quando il lavoro e' pronto per review -> usa submit_pr. NOT per failure del task (usa update_task status='failed' in parallelo).
        RESTITUISCE: {pr_status:'closed', closed_reason, closed_at}."""
        try:
            async with acquire_write_db() as db:
                result = await pr_uc.close_pull_request(
                    LOCAL_CTX, db, task_id=task_id, reason=reason or "",
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
