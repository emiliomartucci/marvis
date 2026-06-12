# v1.0.0 - 2026-06-12 - Local Gateway todos classifier
from __future__ import annotations

import json
import logging

from core.api.config import settings
from core.api.services.inbox_llm_classifier import _sanitize
from core.api.services.ingest.llm.local_gateway import complete_structured_json
from core.api.services.pii_redactor import redact
from core.api.services.todos.llm.base import TodoClassification

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Sei il classifier dei todo di MarvisX.

Ricevi una singola riga catturata dall'utente o da un agente. Devi inferire:
- type: promemoria, azione, idea, decidi, rivedi
- project_slug: slug progetto se deducibile, altrimenti null
- fu_date: data ISO YYYY-MM-DD se deducibile, altrimenti null
- doer: human, agent o hybrid se e' un'azione, altrimenti null
- confidence: 0.0-1.0
- reasoning: motivazione breve, niente chain-of-thought

Non produrre mai type=approva: gli approva sono proiezioni read-only di code reali.
Rispondi solo JSON valido conforme allo schema."""


class LocalGatewayTodoClassifier:
    """Thin adapter exposing the TodoClassifier Protocol surface."""

    async def classify(
        self,
        text: str,
        context: dict,
    ) -> TodoClassification | None:
        sanitized = redact(_sanitize(text[:5000], 5000))
        try:
            return await complete_structured_json(
                response_model=TodoClassification,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=json.dumps(
                    {"text": sanitized, "context": context},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                feature="todos.classify",
                max_tokens=500,
                timeout=float(settings.ingest_llm_classifier_timeout_seconds),
                idempotency_scope=str(context.get("todo_id") or ""),
            )
        except Exception:  # noqa: BLE001 - classifier is fail-soft by contract
            logger.warning("todos_classifier_failed", exc_info=True)
            return None
