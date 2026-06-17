# v1.0.0 - 2026-06-12 - Todos classifier factory
from __future__ import annotations

import logging

import aiosqlite

from core.api.config import settings
from core.api.services.todos.llm.base import TodoClassifier

logger = logging.getLogger(__name__)


def get_classifier() -> TodoClassifier | None:
    provider = settings.todos_llm_provider.strip().lower()
    if provider in {"none", "off", "disabled"}:
        return None
    if provider in {"local", "gateway", "mac", "tier-fast"}:
        from core.api.services.todos.llm.local_gateway import LocalGatewayTodoClassifier

        return LocalGatewayTodoClassifier()
    raise ValueError(
        f"Unknown TODOS_LLM_PROVIDER: {provider!r} "
        "(expected one of: local, gateway, mac, tier-fast, none)"
    )


async def todos_llm_key_missing(
    db: aiosqlite.Connection, workspace_id: str = "ws_default"
) -> bool:
    """True when advanced todos auto-classification is NOT available, so the
    worker falls back to the deterministic heuristic (gh #22).

    Advanced classification is available when EITHER a BYOK ``classify`` provider
    is configured (the user's own key, managed on the Console BYOK page) OR the
    env gateway key is present. When the provider is explicitly disabled
    (``none``/``off``/``disabled``) and no BYOK provider is set, the heuristic is
    the deliberate choice, not a missing key, so this returns ``False`` (no nag).

    This drives the honest-UX banner so the degradation is never silent.
    """
    # BYOK: a configured 'classify' provider makes advanced classification
    # available regardless of the env gateway key.
    try:
        from core.api.services.ingest.llm.config_store import classify_provider_status

        if await classify_provider_status(db, workspace_id) == "configured":
            return False
    except Exception:  # noqa: BLE001 - BYOK tables may be absent → fall through
        logger.debug("todos_llm_key_missing: BYOK status check failed", exc_info=True)

    provider = settings.todos_llm_provider.strip().lower()
    if provider in {"none", "off", "disabled"}:
        return False
    return not settings.ingest_llm_gateway_api_key
