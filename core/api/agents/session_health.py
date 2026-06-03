# v1.0.0 - 2026-03-02 - DevX session health checker (v1: dimension C only)
"""Session health check — DevX Layer Sprint 3.

v1 implementa solo Dimensione C (Continuita): sessione idle senza input richiesto.
Le dimensioni A (task hygiene), B (context%), D (output quality) sono deferred Sprint 4.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from core.api.models import SessionInfo

logger = logging.getLogger(__name__)

IDLE_THRESHOLD_MINUTES = 15
SESSION_MANAGER_COOLDOWN_MINUTES = 30  # Rate limit: max 1 msg per sessione per 30 min


@dataclass
class HealthCheckResult:
    session_name: str
    action: str              # "send_message" | "escalate" | "ok"
    message: str | None
    escalation_reason: str | None


async def check_session_health(
    session: SessionInfo,
    last_action_at: datetime | None,
) -> HealthCheckResult:
    """v1: Solo dimensione C — sessione idle senza input richiesto.

    Le dimensioni A, B, D sono deferred alla prossima sprint
    dopo validazione di C in produzione.
    """
    # Rate limit: non mandare piu di 1 messaggio per cooldown period
    if last_action_at is not None:
        elapsed = (datetime.now(timezone.utc) - last_action_at).total_seconds() / 60
        if elapsed < SESSION_MANAGER_COOLDOWN_MINUTES:
            return HealthCheckResult(session.name, "ok", None, None)

    # C — Continuita: sessione idle senza input richiesto
    if session.activity_state == "idle" and not _session_waiting_for_input(session):
        return HealthCheckResult(
            session.name,
            "send_message",
            "La sessione sembra ferma. Continua con il task corrente. "
            "Ricorda di aggiornare i task e fare handoff a fine sessione.",
            None,
        )

    return HealthCheckResult(session.name, "ok", None, None)


def _session_waiting_for_input(session: SessionInfo) -> bool:
    """True se la sessione CC sta aspettando input (needs_input activity state)."""
    return session.activity_state == "needs_input"
