# v0.1.0 - 2026-08-16 - Fase 2 mielinizzazione U3: memory_feedback MCP tool group
"""memory_feedback MCP tool — agent feedback into the reinforcement ledger.

Same template as ``tasks.py`` / ``learnings.py`` (use_cases-direct, no HTTP,
no fastapi). One mutator tool; the whole gate (visibility negative-control,
anti-gaming caps, note redaction, ledger append) lives in
``core.api.use_cases.feedback`` so it is testable without the MCP SDK.

Surface contract (plan 2026-08-16 Fase 2 v3, U3):
- actor = authenticated principal from ``current_mcp_context()`` — NEVER a
  parameter. ``agent_name`` is optional self-declared telemetry (untrusted,
  never used by the caps).
- mode off (R4): the tool answers inert WITHOUT touching the writer lock —
  zero reads, zero writes, no ledger.
- write is SYNCHRONOUS inside ``acquire_write_db`` (R5); the use_case commits
  (repo convention). No follow-up work exists for this tool, so nothing runs
  after the lock.
- over-cap is NOT an error: ``{ok, applied: false, reason: "cap"}`` with the
  rejection counted in ``boost_rejects`` (R7 — the agent must not retry).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.config import settings
from core.api.mcp._adapter import (
    acquire_write_db,
    current_mcp_context,
    raise_mcp_error,
)
from core.api.use_cases import feedback as feedback_uc
from core.api.use_cases._errors import ServiceError

FeedbackOutcome = Literal["helped", "misled"]


def register(mcp) -> None:
    """Register the feedback tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def memory_feedback(
        doc_id: Annotated[int, Field(ge=1)],
        outcome: FeedbackOutcome,
        # NO max_length here: a >300-char note must be TRUNCATED by the
        # use_case (redact_note, R5 "max 300"), never rejected at the tool
        # boundary — length is not a protocol error.
        note: str | None = None,
        agent_name: Annotated[str, Field(max_length=80)] | None = None,
    ) -> dict[str, Any]:
        """Registra l'esito REALE dell'uso di un documento recuperato: rinforza (helped) o penalizza (misled) la sua salience futura.

        QUANDO USARLO: chiamalo SOLO se il contenuto del doc ha CAMBIATO la tua azione in questa sessione — outcome='helped' se ti ha guidato alla mossa giusta, outcome='misled' se ti ha fuorviato. doc_id è quello restituito da search/check_learnings/get_learning. note (opzionale, max 300) = in una riga COSA ha cambiato; agent_name (opzionale) = solo telemetria auto-dichiarata, non influenza nulla.
        QUANDO NON USARLO: se il contenuto non l'hai usato, NON chiamare — nessuna chiamata È il segnale corretto. NOT per cortesia, NOT in loop su ogni risultato: i cap anti-gaming contano i rigetti. NOT per contenuti mai recuperati dai tool di recall.
        RESTITUISCE: {ok, mode, applied, reason?} — applied:false con reason:'cap' significa feedback contato ma oltre i limiti (non è un errore: NON ritentare). Con MARVIS_REINFORCEMENT=off il tool è inerte ({ok, mode:'off', applied:false})."""
        # R4: mode off = inert tool, zero DB work — answer BEFORE taking the
        # writer lock so the off-path cost is exactly zero.
        if settings.reinforcement_mode == "off":
            return {"ok": True, "mode": "off", "applied": False}
        try:
            ctx = current_mcp_context()
            async with acquire_write_db(label="mcp.memory_feedback") as db:
                # The use_case commits (caller-commits writer contract).
                result = await feedback_uc.memory_feedback(
                    ctx,
                    db,
                    doc_id=doc_id,
                    outcome=outcome,
                    note=note,
                    agent_name=agent_name,
                )
            return result
        except ServiceError as e:
            raise_mcp_error(e)
