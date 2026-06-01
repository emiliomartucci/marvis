# Brain v1.0.1 — Shared helpers for source collectors (sub-01 §3 Sources).
# These helpers exist to keep individual collector files mechanical and short:
# substrate-specific reads belong in the collector module, while every-source
# concerns (timezone normalization, scope resolution, evidence hashing) live
# here.
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from core.api.services.brain.cycle import evidence_hash as _cycle_evidence_hash
from core.api.services.brain.cycle import make_event_id as _cycle_make_event_id
from core.api.services.brain.scope import resolve_program


def evidence_hash(evidence: dict[str, Any]) -> str:
    """Re-export of cycle.evidence_hash for collector code locality."""
    return _cycle_evidence_hash(evidence)


def make_event_id(
    *, cycle_key: str, event_type: str, source_ref: str, evidence: dict[str, Any]
) -> str:
    """Wrap cycle.make_event_id so collectors don't have to hash twice."""
    return _cycle_make_event_id(
        cycle_key=cycle_key,
        event_type=event_type,
        source_ref=source_ref,
        evidence_hash_hex=_cycle_evidence_hash(evidence),
    )


def normalize_iso(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse → aware UTC datetime.

    Handles the three flavours we encounter across substrate tables:
      * tasks.updated_at: `datetime('now')` → naive LOCAL string. Treated as
        UTC here per repo convention (mirror inbox_digest_jobs).
      * learnings.updated_at: optional `datetime('now','utc')`.
      * inbox_items.updated_at: same as tasks.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def stable_short_hash(value: str, *, length: int = 12) -> str:
    """BLAKE2b-prefixed deterministic short hash (filename anonymizer)."""
    return hashlib.blake2b(value.encode("utf-8"), digest_size=length).hexdigest()


def canonical_payload(payload: dict[str, Any]) -> str:
    """Canonical JSON used for evidence hashing — mirrors cycle.canonical_evidence."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def resolve_source_project(slug: str | None) -> tuple[str | None, str | None]:
    """Return (source_project_slug, program_key) — both possibly None.

    Centralised so each collector calls the same resolver — Drift/Memory-Ops
    can later assume program_key is consistent with source_project.
    """
    if not slug:
        return None, None
    return slug, resolve_program(slug)


__all__ = [
    "canonical_payload",
    "evidence_hash",
    "make_event_id",
    "normalize_iso",
    "resolve_source_project",
    "stable_short_hash",
]
