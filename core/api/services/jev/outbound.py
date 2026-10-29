"""Deterministic sanitization before the single scoring-provider egress port."""
from __future__ import annotations

import math
import re
from typing import Any

from core.api.services import bug_reports_core

_ID_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    # A bare UUID can also be an unlabeled credential. Only contextual refs
    # below may bypass entropy redaction; UUIDs need record-level proof.
    r"(?:task|learning|handoff|audit|finding|drift|pr):[0-9a-f][0-9a-z-]{7,}",
    r"(?:feat|fix|chore|refactor|docs|test|perf|release)/[a-z0-9._/-]{4,}",
    r"[a-z][a-z0-9]*-\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*",
    r"(?:\.?worktrees/)?[a-z][a-z0-9]*(?:-[a-z0-9]+)+-[0-9a-f]{6,}",
))
_SECRET_FIELDS = {
    "apikey", "password", "passwd", "pwd", "secret", "token", "authorization",
    "authtoken", "accesstoken", "refreshtoken", "clientsecret", "privatekey",
    "secretkey", "signingkey", "credentials",
}


def looks_like_internal_identifier(token: str) -> bool:
    return any(pattern.fullmatch(token) for pattern in _ID_PATTERNS)


def contains_credentials(text: str) -> bool:
    """Typed credential evidence; ordinary PII is not an ingest secret block."""
    return bug_reports_core.redact_explicit(text, include_email=False)[1] > 0


def sanitize_text(text: str) -> str:
    # Typed secret context always wins, including passwords shaped like UUIDs.
    explicit, _ = bug_reports_core.redact_explicit(text)
    identifiers = {
        token for token in bug_reports_core.high_entropy_tokens(explicit)
        if looks_like_internal_identifier(token)
    }
    return bug_reports_core.redact(explicit, keep_tokens=identifiers)[0]


def sanitize_payload(value: Any, *, _depth: int = 0) -> Any:
    """Copy JSON values recursively; never serialize arbitrary objects/secrets."""
    if _depth > 32:
        raise ValueError("scoring payload is too deeply nested")
    if isinstance(value, str):
        return sanitize_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item, _depth=_depth + 1) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            # Schema keys and choice labels must stay stable for validation.
            # Refuse sensitive keys instead of silently changing their meaning.
            if not isinstance(key, str) or sanitize_text(key) != key:
                raise ValueError("unsafe scoring payload key")
            if re.sub(r"[^a-z]", "", key.lower()) in _SECRET_FIELDS:
                result[key] = "[REDACTED_SECRET]"
            else:
                result[key] = sanitize_payload(item, _depth=_depth + 1)
        return result
    raise ValueError("scoring payload must contain only finite JSON values")
