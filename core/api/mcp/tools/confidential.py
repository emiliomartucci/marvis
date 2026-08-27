"""Owner-confidential file MCP tools (RBAC F4)."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import acquire_write_db, current_mcp_context, raise_mcp_error
from core.api.services import confidential_files
from core.api.use_cases._errors import ServiceError

WorkspacePath = Annotated[str, Field(min_length=1, max_length=1024)]
Identity = Annotated[str, Field(min_length=1, max_length=255)]


def register(mcp) -> None:
    """Register owner-confidential file tools."""

    @mcp.tool()
    async def mark_confidential(path: WorkspacePath) -> dict[str, Any]:
        """Rendi un file riservato al suo owner (owner-confidential).

        QUANDO USARLO: sei il creatore/owner del file e vuoi che lo veda solo tu (+ le persone con cui lo condividi via share_confidential). Il flag vive nel DB (autoritativo) E nel frontmatter: rimuovere la riga dal file NON lo declassifica.
        QUANDO NON USARLO: NOT per la riservatezza di progetto (grants/teams la governano gia'). NOT da agenti: solo persone.
        RESTITUISCE: {path, confidential, owner, purged} — il purge rimuove il file da ricerca/indice in un'unica transazione."""
        try:
            async with acquire_write_db(label="mcp.mark_confidential") as db:
                return await confidential_files.mark_confidential(
                    db, current_mcp_context(), path=path,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def unmark_confidential(path: WorkspacePath) -> dict[str, Any]:
        """Declassifica un file owner-confidential (solo l'owner).

        QUANDO USARLO: l'owner decide che il file torna visibile secondo i normali grants/teams del progetto; rientra in indice al prossimo reindex del path.
        QUANDO NON USARLO: NOT per file mai marcati.
        RESTITUISCE: {path, confidential:false}."""
        try:
            async with acquire_write_db(label="mcp.unmark_confidential") as db:
                return await confidential_files.unmark_confidential(
                    db, current_mcp_context(), path=path,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def share_confidential(path: WorkspacePath, identity: Identity) -> dict[str, Any]:
        """Condividi un file owner-confidential con una persona del tenant (ACL).

        QUANDO USARLO: l'owner vuole dare accesso in lettura a una persona specifica. L'ACL corrente (owner + viewers) torna nella risposta.
        QUANDO NON USARLO: NOT per dare accesso al progetto -> grant_access/teams. NOT per consegnare il file a chi apre un link (anche fuori tenant) -> usa share_file, che crea un URL HTTPS a scadenza; qui non nasce nessun link. Se la persona non ha un grant sul progetto la risposta include un warning: l'ACL da sola non rende il file raggiungibile.
        RESTITUISCE: {path, owner, viewers[], warning?}."""
        try:
            async with acquire_write_db(label="mcp.share_confidential") as db:
                return await confidential_files.share_file_acl(
                    db, current_mcp_context(), path=path, identity=identity,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def unshare_confidential(path: WorkspacePath, identity: Identity) -> dict[str, Any]:
        """Revoca la condivisione di un file owner-confidential (solo l'owner).

        QUANDO USARLO: togliere una persona dall'ACL del file; effetto immediato (check-time).
        RESTITUISCE: {path, owner, viewers[]}."""
        try:
            async with acquire_write_db(label="mcp.unshare_confidential") as db:
                return await confidential_files.unshare_file_acl(
                    db, current_mcp_context(), path=path, identity=identity,
                )
        except ServiceError as e:
            raise_mcp_error(e)
