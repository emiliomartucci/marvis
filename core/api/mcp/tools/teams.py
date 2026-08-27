"""Thin MCP adapters for the shared FastAPI-free team use case."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    _invalidate_db_role_cache,
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    raise_mcp_error,
    sync_oauth_user,
)
from core.api.use_cases import teams as team_uc
from core.api.use_cases._errors import ServiceError


TeamMemberRole = Literal["member", "admin"]
TeamProjectRole = Literal["member", "viewer"]
TeamProjectClearance = Literal["public", "internal"]
AssignableSystemRole = Literal["viewer", "operator", "admin"]

TeamArg = Annotated[str, Field(min_length=1, max_length=128)]
UserArg = Annotated[str, Field(min_length=1, max_length=255)]
ProjectArg = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_&-]{0,127}$"),
]


# Stable import aliases for internal callers/tests; all policy and SQL lives in
# use_cases.teams, not in this transport adapter.
create_team_impl = team_uc.create_team
list_teams_impl = team_uc.list_teams
add_team_member_impl = team_uc.add_team_member
remove_team_member_impl = team_uc.remove_team_member
assign_team_project_impl = team_uc.assign_team_project
unassign_team_project_impl = team_uc.unassign_team_project
set_user_role_impl = team_uc.set_user_role


def register(mcp) -> None:
    """Register tenant-scoped team tools on the shared FastMCP instance."""

    @mcp.tool()
    async def create_team(
        slug: Annotated[str, Field(min_length=1, max_length=50)],
        display_name: Annotated[str, Field(min_length=1, max_length=100)],
        description: Annotated[str, Field(max_length=500)] | None = None,
    ) -> dict[str, Any]:
        """Crea un team self-service nel workspace autenticato.

        Una persona creatrice diventa lead; lo stesso slug puo' esistere in workspace diversi. Restituisce id tenant-safe, slug, display_name, member_count e your_role."""
        try:
            async with acquire_write_db(label="mcp.create_team") as db:
                return await team_uc.create_team(
                    db,
                    current_mcp_context(),
                    slug=slug,
                    display_name=display_name,
                    description=description,
                )
        except ServiceError as exc:
            raise_mcp_error(exc)

    @mcp.tool()
    async def list_teams() -> list[dict[str, Any]]:
        """Elenca i team visibili nel workspace autenticato.

        Admin vede tutti; gli altri solo i team di cui sono membri. Nessun dato di altri workspace viene incluso."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                await sync_oauth_user(db, ctx)
                return await team_uc.list_teams(db, ctx)
        except ServiceError as exc:
            raise_mcp_error(exc)

    @mcp.tool()
    async def add_team_member(
        team_id: TeamArg,
        user: UserArg,
        role: TeamMemberRole = "member",
    ) -> dict[str, Any]:
        """Aggiunge un utente esistente del workspace a un team.

        Team e utente sono risolti solo nel workspace autenticato; un lead non puo' creare un secondo lead."""
        try:
            async with acquire_write_db(label="mcp.add_team_member") as db:
                return await team_uc.add_team_member(
                    db,
                    current_mcp_context(),
                    team=team_id,
                    user=user,
                    role=role,
                )
        except ServiceError as exc:
            raise_mcp_error(exc)

    @mcp.tool()
    async def remove_team_member(
        team_id: TeamArg,
        user: UserArg,
    ) -> dict[str, Any]:
        """Rimuove un membro dal team autenticato; l'unico lead non puo' auto-rimuoversi."""
        try:
            async with acquire_write_db(label="mcp.remove_team_member") as db:
                return await team_uc.remove_team_member(
                    db,
                    current_mcp_context(),
                    team=team_id,
                    user=user,
                )
        except ServiceError as exc:
            raise_mcp_error(exc)

    @mcp.tool()
    async def assign_team_project(
        team_id: TeamArg,
        project: ProjectArg,
        role: TeamProjectRole = "member",
        clearance: TeamProjectClearance = "internal",
    ) -> dict[str, Any]:
        """Assegna o aggiorna un progetto per un team del workspace.

        Richiede org-admin o project-admin di quel progetto; i team non conferiscono confidential."""
        try:
            async with acquire_write_db(label="mcp.assign_team_project") as db:
                return await team_uc.assign_team_project(
                    db,
                    current_mcp_context(),
                    team=team_id,
                    project=project,
                    role=role,
                    clearance=clearance,
                )
        except ServiceError as exc:
            raise_mcp_error(exc)

    @mcp.tool()
    async def unassign_team_project(
        team_id: TeamArg,
        project: ProjectArg,
    ) -> dict[str, Any]:
        """Rimuove l'assegnazione di un progetto da un team del workspace."""
        try:
            async with acquire_write_db(label="mcp.unassign_team_project") as db:
                return await team_uc.unassign_team_project(
                    db,
                    current_mcp_context(),
                    team=team_id,
                    project=project,
                )
        except ServiceError as exc:
            raise_mcp_error(exc)

    @mcp.tool()
    async def set_user_role(
        user: UserArg,
        role: AssignableSystemRole,
    ) -> dict[str, Any]:
        """Cambia viewer/operator/admin per un utente del workspace (admin only).

        super_admin resta manual-only e l'ultimo admin non puo' essere rimosso."""
        try:
            async with acquire_write_db(label="mcp.set_user_role") as db:
                ctx = current_mcp_context()
                result = await team_uc.set_user_role(
                    db,
                    ctx,
                    user=user,
                    role=role,
                )
                if result["changed"]:
                    _invalidate_db_role_cache(result["user_id"], ctx.workspace_id)
                return result
        except ServiceError as exc:
            raise_mcp_error(exc)
