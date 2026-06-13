# v1.0.0 - 2026-06-12 - Todos classifier factory
from __future__ import annotations

from core.api.config import settings
from core.api.services.todos.llm.base import TodoClassifier


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
