"""Bounded source snapshots used by semantic rules, never substrate re-reads."""
from __future__ import annotations

from typing import Any

_FIELD_CAP = 4000


def capture_source_context(**fields: Any) -> dict[str, Any]:
    texts = {key: str(value or "") for key, value in fields.items()}
    return {"source_context": {key: value[:_FIELD_CAP] for key, value in texts.items()},
            "source_context_truncated": any(len(value) > _FIELD_CAP for value in texts.values())}


def source_text(event: Any) -> str:
    evidence = event.evidence or {}
    context = evidence.get("source_context")
    if not isinstance(context, dict) or evidence.get("source_context_truncated"):
        return ""
    if not any(str(context.get(k) or "").strip() for k in ("description", "prevention")):
        return ""
    return "\n".join(f"{key}: {context[key]}" for key in ("title", "description", "prevention", "updated_at") if context.get(key))


def latest_source_events(events: list[Any]) -> list[Any]:
    latest: dict[str, Any] = {}
    for event in events:
        previous = latest.get(event.source_ref)
        if previous is None or (event.observed_at, event.event_id) > (previous.observed_at, previous.event_id):
            latest[event.source_ref] = event
    return [latest[ref] for ref in sorted(latest) if source_text(latest[ref])]
