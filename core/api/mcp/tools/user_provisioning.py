"""Self-service user provisioning MCP tools (RBAC F3)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from core.api.mcp._adapter import acquire_db, acquire_write_db, current_mcp_context, raise_mcp_error
from core.api.services import user_provisioning
from core.api.use_cases._errors import ServiceError

Email = Annotated[str, Field(min_length=3, max_length=254)]
MintableRole = Literal["operator", "viewer"]
TeamIds = Annotated[list[str], Field(max_length=10)]


def register(mcp) -> None:
    """Register user provisioning tools."""

    @mcp.tool()
    async def add_user(
        email: Email,
        role: MintableRole = "viewer",
        teams: TeamIds | None = None,
    ) -> dict:
        """Invita un collega nel tenant (coda di provisioning, ~60s).

        QUANDO USARLO: operator+ vuole aggiungere un collega con la sua email aziendale; role al massimo pari al proprio (mai admin — quelli si creano solo dalla console). teams pre-assegna i team di cui sei lead (o org-admin).
        QUANDO NON USARLO: NOT per dare accesso a un utente già esistente -> usa grant_access o i team. NOT per creare admin.
        RESTITUISCE: riga in coda {id, email, requested_role, status} + istruzioni primo login (Magic Auth/Google). Finché il worker non completa, il login OAuth fallisce con errore org: race attesa. Stato via list_user_requests."""
        try:
            async with acquire_write_db(label="mcp.add_user") as db:
                return await user_provisioning.enqueue_request(
                    db, current_mcp_context(), email=email, role=role, teams=teams,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_user_requests(
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict]:
        """Stato delle richieste add_user (anche failed/rejected).

        QUANDO USARLO: dopo add_user per verificare l'esito del provisioning; qui si scoprono anche i failed (niente notifiche per gli operator).
        QUANDO NON USARLO: NOT per elencare gli utenti del tenant.
        RESTITUISCE: list[{id, email, requested_role, status, attempts, error, created_at, processed_at}] — le proprie richieste; admin vede tutte."""
        try:
            async with acquire_db() as db:
                return await user_provisioning.list_requests(
                    db, current_mcp_context(), limit=limit,
                )
        except ServiceError as e:
            raise_mcp_error(e)
