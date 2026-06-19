# v1.0.0 - 2026-06-09 - P5: `marvis task` — human task surface from the terminal.
"""``marvis task`` — list / show / approve / reject tasks from the terminal.

Tasks were agent-only (MCP); the human had no terminal surface. This group is a
thin adapter over the S1 task use_cases (`list_tasks` / `get_task` / `update_task`),
run as the local single-user context (`is_human_session=True` → the four-eyes gate
collapses locally, by design, so the human running the CLI can approve directly).
No HTTP, no token, no new domain logic.

Heavy imports are LAZY (inside command bodies) so `marvis task --help` stays fast.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from core.cli._runtime_ctx import (
    LOCAL_CTX,
    console,
    emit,
    err_console,
    handle_service_error,
    run_async,
    with_db,
    with_write_db,
)

_PANEL_TASK = "Tasks"
_OPEN_STATUSES = {"pending", "approved", "in_progress", "review"}

task_app = typer.Typer(
    no_args_is_help=True,
    help="List, inspect and approve/reject tasks from the terminal.",
)


def register(app: typer.Typer) -> None:
    """Attach the ``task`` group onto an existing app (same pattern as project)."""
    app.add_typer(
        task_app,
        name="task",
        rich_help_panel=_PANEL_TASK,
        help="Manage tasks (list / show / approve / reject).",
    )


# Local seams for update_task. In single-user OSS the PR gate collapses (mirrors
# core/api/mcp/_adapter.mcp_requires_pr_gate) and approve/reject never trigger it;
# re-embedding on a status flip is best-effort and a no-op from the CLI.
def _requires_pr_gate(_project: str | None) -> bool:
    return False


def _schedule_embed(**_kwargs: Any) -> None:
    return None


@task_app.command("list")
@handle_service_error
def list_cmd(
    status: str = typer.Option(
        "open",
        "--status",
        help="open | all | a concrete status (pending/approved/in_progress/review/completed/rejected/failed).",
    ),
    project: str | None = typer.Option(None, "--project", help="Filter by project slug."),
    limit: int = typer.Option(50, "--limit", help="Max rows."),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """List tasks (default: open ones)."""
    norm = (status or "open").lower()
    uc_status = None if norm in ("open", "all") else norm

    async def _run() -> list[dict[str, Any]]:
        from core.api.use_cases import tasks as tasks_uc

        async with with_db() as db:
            rows = await tasks_uc.list_tasks(
                LOCAL_CTX, db, project=project, status=uc_status, limit=limit
            )
        data = [r.model_dump(mode="json") for r in rows]
        if norm == "open":
            data = [d for d in data if d.get("status") in _OPEN_STATUSES]
        return data

    result = run_async(_run())

    def _render(rows: list[dict[str, Any]]) -> None:
        if not rows:
            console.print("[dim]No tasks.[/dim]")
            return
        table = Table(title=f"tasks ({norm})", header_style="bold cyan")
        table.add_column("id", no_wrap=True)
        table.add_column("status", no_wrap=True)
        table.add_column("pri", no_wrap=True)
        table.add_column("project", no_wrap=True)
        table.add_column("title")
        for d in rows:
            table.add_row(
                str(d.get("id", ""))[:8],
                d.get("status", ""),
                d.get("priority", "") or "",
                d.get("project", "") or "",
                d.get("title", "") or "",
            )
        console.print(table)

    emit(result, json_out=json_out, render=_render)


@task_app.command("show")
@handle_service_error
def show_cmd(
    task_id: str = typer.Argument(..., help="Task id (or prefix-complete id)."),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Show a single task's detail."""

    async def _run() -> dict[str, Any]:
        from core.api.use_cases import tasks as tasks_uc

        async with with_db() as db:
            r = await tasks_uc.get_task(LOCAL_CTX, db, task_id=task_id)
        return r.model_dump(mode="json")

    result = run_async(_run())

    def _render(d: dict[str, Any]) -> None:
        console.print(f"[bold]{d.get('title', '')}[/bold]  [dim]{d.get('id', '')}[/dim]")
        console.print(
            f"status={d.get('status')} priority={d.get('priority')} "
            f"project={d.get('project')} ice={d.get('ice_score')}"
        )
        if d.get("description"):
            console.print()
            console.print(d["description"])

    emit(result, json_out=json_out, render=_render)


def _set_status(task_id: str, new_status: str, *, json_out: bool, verb: str) -> None:
    """Shared write path for approve/reject. Local human session → the four-eyes
    gate passes. If a REMOTE multi-user backend returns the human-only 403, translate
    it into actionable CLI guidance instead of the raw 'via Console' string."""
    from core.api.models.tasks import TaskUpdateRequest
    from core.api.use_cases import tasks as tasks_uc
    from core.api.use_cases._errors import AuthorizationError

    async def _run() -> Any:
        async with with_write_db() as db:
            return await tasks_uc.update_task(
                LOCAL_CTX,
                db,
                task_id=task_id,
                body=TaskUpdateRequest(status=new_status),
                requires_pr_gate=_requires_pr_gate,
                schedule_embed=_schedule_embed,
            )

    try:
        result = run_async(_run())
    except AuthorizationError as exc:
        if getattr(exc, "code", "") == "approval_requires_human":
            err_console.print(
                "[yellow]This backend requires a human session to approve tasks (multi-user mode).[/yellow]"
            )
            err_console.print(
                "Run this on the local single-user CLI (where approval is allowed directly), "
                "or approve via your deployment's Console."
            )
            raise typer.Exit(1) from exc
        raise

    payload = result.model_dump(mode="json")

    def _render(d: dict[str, Any]) -> None:
        console.print(f"[green]{verb}[/] {d.get('id', '')} → {d.get('status')}")

    emit(payload, json_out=json_out, render=_render)


@task_app.command("approve")
@handle_service_error
def approve_cmd(
    task_id: str = typer.Argument(..., help="Task id to approve (pending → approved)."),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Approve a pending task. Locally the human running the CLI is the gate."""
    _set_status(task_id, "approved", json_out=json_out, verb="approved")


@task_app.command("reject")
@handle_service_error
def reject_cmd(
    task_id: str = typer.Argument(..., help="Task id to reject (pending → rejected)."),
    reason: str | None = typer.Option(
        None, "--reason", help="Optional note (echoed; rejection reason is not a stored field yet)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Reject a pending task."""
    _set_status(task_id, "rejected", json_out=json_out, verb="rejected")
    if reason:
        err_console.print(f"[dim]reason: {reason}[/dim]")
