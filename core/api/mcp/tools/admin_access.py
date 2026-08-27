"""Admin access-grant MCP tools for multi-user hosted tenants."""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import Field

from core.api.mcp._adapter import acquire_db, acquire_write_db, current_mcp_context, raise_mcp_error
from core.api.services import access_grants
from core.api.use_cases._errors import ServiceError

Role = Literal["admin", "member", "viewer"]
Clearance = Literal["public", "internal", "confidential"]
Identity = Annotated[str, Field(min_length=1, max_length=255)]
ProjectSlug = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_&-]{0,127}$")]
Scope = Annotated[str, Field(min_length=1, max_length=500)]


def _dump_grant(grant: access_grants.AccessGrant) -> dict:
    return asdict(grant)


def register(mcp) -> None:
    """Register tenant access administration tools."""

    @mcp.tool()
    async def grant_access(
        identity: Identity,
        project: ProjectSlug,
        role: Role,
        clearance: Clearance = "internal",
        scope: Scope | None = None,
    ) -> dict:
        """Grant or update tenant-scoped access for a person.

        QUANDO USARLO: admin tenant vuole dare accesso immediato a una persona su un progetto/scope con role e clearance espliciti.
        QUANDO NON USARLO: NOT per creare utenti WorkOS o invitare via email; prima deve esistere la membership/identity tenant. NOT per accessi globali impliciti.
        RESTITUISCE: {identity, project_slug, role, clearance, scope}; errori scope_denied/identity_not_found/project_not_found sono ToolError reali."""
        try:
            async with acquire_write_db(label="mcp.grant_access") as db:
                grant = await access_grants.grant_access(
                    db,
                    current_mcp_context(),
                    identity=identity,
                    project_slug=project,
                    role=role,
                    clearance=clearance,
                    scope=scope,
                )
                return _dump_grant(grant)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def revoke_access(identity: Identity, project: ProjectSlug) -> dict:
        """Revoke tenant-scoped access for a person/project.

        QUANDO USARLO: admin tenant vuole togliere subito visibilita' e write access a una persona su un progetto.
        QUANDO NON USARLO: NOT per cancellare l'utente WorkOS o revocare accessi in altri tenant.
        RESTITUISCE: {identity, project_slug, revoked}; revoca inesistente torna revoked=false senza leak extra."""
        try:
            async with acquire_write_db(label="mcp.revoke_access") as db:
                revoked = await access_grants.revoke_access(
                    db,
                    current_mcp_context(),
                    identity=identity,
                    project_slug=project,
                )
                return {"identity": identity, "project_slug": project, "revoked": revoked}
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_access(identity: Identity | None = None) -> list[dict]:
        """List tenant access grants visible to an admin.

        QUANDO USARLO: audit rapido degli accessi tenant, opzionalmente per persona.
        QUANDO NON USARLO: NOT per scoprire identita' cross-tenant; il tool e' admin-only e tenant-scoped.
        RESTITUISCE: list[{identity, project_slug, role, clearance, scope}]."""
        try:
            async with acquire_db() as db:
                grants = await access_grants.list_access(
                    db,
                    current_mcp_context(),
                    identity=identity,
                )
                return [_dump_grant(grant) for grant in grants]
        except ServiceError as e:
            raise_mcp_error(e)
