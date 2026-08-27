"""Thin HTTP adapters for the shared FastAPI-free team use case."""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.models.teams import (
    TeamCreateRequest,
    TeamMemberRequest,
    TeamMemberResponse,
    TeamProjectAssignRequest,
    TeamProjectResponse,
    TeamResponse,
    TeamUpdateRequest,
)
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import get_current_user, get_current_user_or_agent
from core.api.use_cases import teams as team_uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError
from core.api.visibility import (
    invalidate_visibility_cache_for_team,
    invalidate_visibility_cache_for_user,
)


router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


def _ctx(user: UserInfo, *, human_session: bool = False) -> CallerContext:
    return CallerContext.from_user_info(user, is_human_session=human_session)


@router.post("", response_model=TeamResponse, status_code=201)
async def create_team(
    body: TeamCreateRequest,
    current_user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TeamResponse:
    """Create a new team. Operator+ self-service; the person creating it
    becomes the team lead (team_members.role='admin')."""
    try:
        result = await team_uc.create_team(
            db,
            _ctx(current_user),
            slug=body.slug or "",
            display_name=body.display_name,
            description=body.description,
            avatar_color=body.avatar_color,
        )
        return TeamResponse.model_validate(result)
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: str,
    body: TeamUpdateRequest,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TeamResponse:
    """Update a team's display_name, description, or avatar_color.

    Allowed for: global admin/super_admin OR team admin of this team.
    """
    try:
        result = await team_uc.update_team(
            db,
            _ctx(current_user),
            team=team_id,
            display_name=body.display_name,
            description=body.description,
            avatar_color=body.avatar_color,
            supplied_fields=set(body.model_fields_set),
        )
        return TeamResponse.model_validate(result)
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.delete("/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    current_user: UserInfo = Depends(require_role("super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Soft-delete a team. Super_admin human-only (team admins cannot delete)."""
    try:
        result = await team_uc.delete_team(
            db,
            _ctx(current_user, human_session=True),
            team=team_id,
        )
        await invalidate_visibility_cache_for_team(result["team_id"], db)
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.get("", response_model=list[TeamResponse])
async def list_teams(
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TeamResponse]:
    """List teams visible to the current user."""
    try:
        rows = await team_uc.list_teams(db, _ctx(current_user))
        return [TeamResponse.model_validate(row) for row in rows]
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.get("/{team_id}/members", response_model=list[TeamMemberResponse])
async def list_team_members(
    team_id: str,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TeamMemberResponse]:
    """List members of a team."""
    try:
        rows = await team_uc.list_team_members(
            db,
            _ctx(current_user),
            team=team_id,
        )
        return [TeamMemberResponse.model_validate(row) for row in rows]
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.post("/{team_id}/members", status_code=201)
async def add_team_member(
    team_id: str,
    body: TeamMemberRequest,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Add a member to a team. Requires team lead or admin+."""
    try:
        result = await team_uc.add_team_member(
            db,
            _ctx(current_user),
            team=team_id,
            user=body.user_id,
            role=body.role,
        )
        await invalidate_visibility_cache_for_team(result["team_id"], db)
        return result
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(
    team_id: str,
    user_id: str,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Remove a member from a team. Requires team lead or admin+."""
    try:
        result = await team_uc.remove_team_member(
            db,
            _ctx(current_user),
            team=team_id,
            user=user_id,
        )
        await invalidate_visibility_cache_for_user(result["user_id"])
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.get("/{team_id}/projects", response_model=list[TeamProjectResponse])
async def list_team_projects(
    team_id: str,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TeamProjectResponse]:
    """List projects assigned to a team."""
    try:
        rows = await team_uc.list_team_projects(
            db,
            _ctx(current_user),
            team=team_id,
        )
        return [TeamProjectResponse.model_validate(row) for row in rows]
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.post("/{team_id}/projects", status_code=201)
async def assign_team_project(
    team_id: str,
    body: TeamProjectAssignRequest,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Assign a project to a team (or update its role/clearance).

    Allowed for: org-admin OR project-admin of THAT project (D3). The same
    subjects — and only them — change role/clearance after the assignment.
    """
    try:
        result = await team_uc.assign_team_project(
            db,
            _ctx(current_user),
            team=team_id,
            project=body.project,
            role=body.role,
            clearance=body.clearance,
            is_public=body.is_public,
        )
        await invalidate_visibility_cache_for_team(result["team_id"], db)
        return result
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.delete("/{team_id}/projects/{slug}", status_code=204)
async def remove_team_project(
    team_id: str,
    slug: str,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Remove a project from a team. Org-admin or project-admin of that project."""
    try:
        result = await team_uc.unassign_team_project(
            db,
            _ctx(current_user),
            team=team_id,
            project=slug,
        )
        await invalidate_visibility_cache_for_team(result["team_id"], db)
    except ServiceError as exc:
        raise to_http(exc) from exc
