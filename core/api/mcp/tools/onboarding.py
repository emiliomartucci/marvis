from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_write_db,
    current_mcp_context,
    dump,
    raise_mcp_error,
)
from core.api.use_cases import onboarding as onboarding_uc
from core.api.use_cases._errors import AuthorizationError, ServiceError

SetupSection = Literal["Identità", "Sorgenti", "Ritmo", "Fonti del brain"]


def _require_local_host_context() -> None:
    if current_mcp_context() is LOCAL_CTX:
        return
    raise AuthorizationError(
        code="local_host_operation_required",
        message=(
            "Host filesystem and setup operations are available only through "
            "trusted local stdio."
        ),
    )


def register(mcp) -> None:
    """Register GUI onboarding tools."""

    @mcp.tool()
    async def scan_workdir(
        root: Annotated[str, Field(min_length=1)],
        exclusions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Propose candidate projects under a local work folder.

        QUANDO USARLO: onboarding GUI, prima della conferma umana, per scoprire cartelle candidate e distinguere code/no-code via presenza di .git.
        QUANDO NON USARLO: NOT per importare o persistere progetti; questo tool non scrive nulla.
        RESTITUISCE: {root, exclusions, proposals:[{path,name,kind}]}."""
        try:
            _require_local_host_context()
            return dump(onboarding_uc.scan_workdir(root=root, exclusions=exclusions or []))
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def write_setup(
        section: SetupSection,
        content: Annotated[str | None, Field(max_length=20000)] = None,
        checkboxes: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Write one authored setup.md section or checkbox states.

        QUANDO USARLO: aggiornare il contratto setup.md mentre l'utente compila il wizard o l'agente completa i checkbox.
        QUANDO NON USARLO: NOT per scrivere stato derivato (progetti, programmi, tipo code/no-code, relazioni, status). NOT per sezioni non authored.
        RESTITUISCE: {path, content, sections, checkboxes}."""
        try:
            _require_local_host_context()
            return dump(
                onboarding_uc.write_setup(
                    section=section,
                    content=content,
                    checkboxes=checkboxes,
                )
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def seed_demo(lang: Literal["it", "en"] = "it") -> dict[str, Any]:
        """Seed the Casa Lorenzi demo data idempotently.

        QUANDO USARLO: primo avvio GUI locale, per evitare una console vuota con dati demo eliminabili.
        QUANDO NON USARLO: NOT per creare dati reali utente; il seed marca tutto con tag/source_ref demo.
        RESTITUISCE: {project, created, tasks, todos, lang}."""
        try:
            _require_local_host_context()
            ctx = current_mcp_context()
            async with acquire_write_db(label="mcp.onboarding.seed_demo") as db:
                return dump(await onboarding_uc.seed_demo(ctx, db, lang=lang))
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def teardown_demo() -> dict[str, Any]:
        """Delete only Casa Lorenzi demo-tagged data.

        QUANDO USARLO: l'utente sceglie di eliminare i dati demo dal wizard/tour.
        QUANDO NON USARLO: NOT per cancellare progetti o task reali; cancella solo marker/source_ref/tag demo.
        RESTITUISCE: {project, tasks_deleted, todos_deleted, project_deleted}."""
        try:
            _require_local_host_context()
            ctx = current_mcp_context()
            async with acquire_write_db(label="mcp.onboarding.teardown_demo") as db:
                return dump(await onboarding_uc.teardown_demo(ctx, db))
        except ServiceError as e:
            raise_mcp_error(e)
