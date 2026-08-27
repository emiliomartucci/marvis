"""Pure, dependency-free core logic for the report_bug MVP.

Kept import-light (stdlib only) so it loads on every tenant's collapsed MCP
runtime and is trivially unit-testable without a DB, network, or FastMCP.

Split of responsibilities:
- redaction runs in the CALLING tenant's process, BEFORE the payload leaves it
  (never embed/store a raw secret — the vector is reversible via NN search).
- dedup_key is computed tenant-side and re-asserted operator-side (unique index).
- the sliding-window limiter is per (tenant,user) in-process state (one process
  per tenant → no shared DB counter needed).
- the ingest credential is a per-tenant token derived from an operator secret via
  HMAC: the operator ships each tenant only its OWN token (never the secret), and
  verifies by recompute — attributable per-tenant, ingest-only, no server-side map.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from collections import deque

# ---------------------------------------------------------------------------
# Caps (bound embedding cost + PII/secret surface)
# ---------------------------------------------------------------------------
TITLE_CAP = 200
DESCRIPTION_CAP = 8_000
ENV_FIELD_CAP = 512


def cap(text: str, limit: int) -> str:
    """Trim to ``limit`` chars on a codepoint boundary (never mid-surrogate)."""
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit]


# ---------------------------------------------------------------------------
# Redaction — whitelist-biased: err toward redacting (a leaked secret in an
# embedding is reversible; an over-redacted bug report is merely less legible).
# Order matters: most specific shapes first, generic high-entropy last.
# ---------------------------------------------------------------------------
_REDACTORS: list[tuple[str, re.Pattern[str]]] = [
    # Private key blocks
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    # JWTs (three base64url segments) — before generic token shapes
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
    # Provider-prefixed API keys (OpenAI, GitHub, Anthropic, Slack, Google, Stripe, AWS...)
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")),
    ("API_KEY", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}\b")),
    ("API_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("API_KEY", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("API_KEY", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA|AGPA|AROA|AIDA)[A-Z0-9]{12,}\b")),
    # bearer/authorization inline
    ("BEARER", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{12,}")),
    # emails
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # generic key=value / key: value where value looks secret-ish
    (
        "SECRET",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd|authorization|auth[_-]?token|access[_-]?token|private[_-]?key)\b\s*[:=]\s*['\"]?([A-Za-z0-9._~+/=-]{8,})['\"]?"
        ),
    ),
]

# generic high-entropy blob: >=20 chars of base64/hex-ish with no whitespace
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/=_-]{20,}\b")


def _looks_high_entropy(token: str) -> bool:
    """Heuristic: long, mixed-class, high Shannon entropy → likely a secret.

    Whitelist-biased: skip obvious non-secrets (all-lower dictionary-ish words,
    pure decimal ids, hex-only <32 that could be a short hash reference is still
    flagged only if long). Errs toward redacting on ambiguity.
    """
    if len(token) < 20:
        return False
    classes = sum(
        bool(re.search(p, token))
        for p in (r"[a-z]", r"[A-Z]", r"[0-9]")
    )
    # need at least two ALPHANUMERIC character classes. Separators ([+/=_-]) do
    # not count as a class on their own: snake_case identifiers such as
    # `project_limit_lock_unavailable` are error codes the operator must be able
    # to read in a bug report, not secrets — counting `_` as a class redacted
    # every >=20-char error code (fleet reports 572d977c/5d8f816c/8ed9fd80 all
    # arrived as "[REDACTED_HIGH_ENTROPY]", destroying the diagnostic signal).
    if classes < 2:
        return False
    # Shannon entropy per char
    import math

    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = -sum((c / len(token)) * math.log2(c / len(token)) for c in counts.values())
    return entropy >= 3.0


def redact(text: str) -> tuple[str, int]:
    """Return ``(redacted_text, redaction_count)``.

    Applies typed shape redactors, then a generic high-entropy sweep. The count
    is a signal surfaced to the operator (a report with many redactions is
    likely a paste of a config/secret, worth a closer look)."""
    if not text:
        return "", 0
    count = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"[REDACTED_{label}]"

    for label, pattern in _REDACTORS:
        text, n = pattern.subn(_sub, text)
    # generic high-entropy pass (skip already-redacted placeholders)
    def _entropy_sub(m: re.Match[str]) -> str:
        nonlocal count
        tok = m.group(0)
        if tok.startswith("REDACTED_") or "REDACTED_" in tok:
            return tok
        if _looks_high_entropy(tok):
            count += 1
            return "[REDACTED_HIGH_ENTROPY]"
        return tok

    text = _HIGH_ENTROPY.sub(_entropy_sub, text)
    return text, count


def redact_fields(**fields: str | None) -> tuple[dict[str, str], int]:
    """Redact a set of named fields, returning (redacted_map, total_count)."""
    out: dict[str, str] = {}
    total = 0
    for name, value in fields.items():
        if value is None:
            continue
        red, n = redact(str(value))
        out[name] = red
        total += n
    return out, total


# ---------------------------------------------------------------------------
# Dedup key — hash-first. Normalizes whitespace/case so trivial re-phrasings of
# the SAME report collapse; scoped by tenant so cross-tenant never collides.
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def dedup_key(tenant_id: str, title: str, description: str) -> str:
    """sha256(tenant | normalized-title | normalized-description). Tenant-scoped."""
    h = hashlib.sha256()
    h.update(tenant_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(_normalize(title).encode("utf-8"))
    h.update(b"\x00")
    h.update(_normalize(description).encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Per-tenant ingest credential (transport C) — HMAC-derived so the operator
# never ships tenants the shared secret, only their own token, and verifies by
# recompute (no server-side token→tenant map to store/rotate).
# ---------------------------------------------------------------------------
def derive_ingest_token(secret: str, tenant_id: str) -> str:
    """Deterministic per-tenant ingest token = HMAC-SHA256(secret, tenant_id)."""
    return hmac.new(secret.encode("utf-8"), tenant_id.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_ingest_token(secret: str, tenant_id: str, token: str) -> bool:
    """Constant-time verify that ``token`` is the ingest token for ``tenant_id``."""
    if not secret or not tenant_id or not token:
        return False
    expected = derive_ingest_token(secret, tenant_id)
    return hmac.compare_digest(expected, token)


# ---------------------------------------------------------------------------
# In-process sliding-window rate limiter (one process per tenant → no shared
# counter). Fail-open by construction: state is memory-only, restart resets.
# ---------------------------------------------------------------------------
class SlidingWindowRateLimiter:
    """Per-key sliding window. ``limit`` events per ``window_seconds``."""

    def __init__(self, limit: int = 10, window_seconds: float = 3600.0) -> None:
        self.limit = limit
        self.window = float(window_seconds)
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str, now: float) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_ms)``. Records the event when allowed.

        retry_after_ms is 0 when allowed; otherwise the ms until the oldest
        in-window event ages out (so the caller/agent can back off precisely)."""
        dq = self._events.setdefault(key, deque())
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.limit:
            retry_after_s = (dq[0] + self.window) - now
            return False, max(0, int(retry_after_s * 1000))
        dq.append(now)
        return True, 0
