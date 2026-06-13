# v1.0.0 - 2026-06-12 - Heuristic fallback classifier for todos (no-LLM tier)
"""Deterministic fallback classifier used when no todos LLM provider is set.

On local installs without an LLM provider (``TODOS_LLM_PROVIDER=none``) the
background classification used to be silently skipped: every captured todo
stayed ``type=promemoria`` / ``fu=today`` / ``project=NULL`` forever while the
GUI promised a classification that never landed (issue #22).

``heuristic_classify`` is a pure function mirroring the LLM classifier output
shape (:class:`TodoClassification`). Rules are intentionally conservative:
when in doubt the defaults are NEVER made worse — ``None`` is returned and the
caller leaves the row untouched.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Sequence

from core.api.services.todos.llm.base import TodoClassification

# Must match TodoClassification.project_slug pattern (note: stricter than the
# project index slug regex, e.g. "+" is not allowed here).
_CLASSIFIABLE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.&\-]{0,126}$")

_MIN_PROJECT_TOKEN_LEN = 3  # avoid 1-2 char slugs matching everywhere

_IDEA_PREFIX_RE = re.compile(r"^\W*idea\b", re.IGNORECASE)

_DECIDI_RE = re.compile(
    r"\bdecidere se\b|\bdecidi se\b|\bdecide whether\b|\bdecide if\b|^\W*decidere\s*:",
    re.IGNORECASE,
)

# Imperative action verbs (IT imperative/infinitive + EN base form). Only the
# FIRST word of the text is checked: an imperative opener is the strongest
# deterministic signal that the capture is an action.
_ACTION_VERBS = frozenset(
    {
        # Italian
        "chiama", "chiamare", "manda", "mandare", "invia", "inviare",
        "scrivi", "scrivere", "compra", "comprare", "prepara", "preparare",
        "aggiorna", "aggiornare", "sistema", "sistemare", "fixa", "fixare",
        "controlla", "controllare", "verifica", "verificare", "crea", "creare",
        "paga", "pagare", "prenota", "prenotare", "rispondi", "rispondere",
        "deploya", "deployare", "pubblica", "pubblicare", "testa", "testare",
        "completa", "completare", "finisci", "finire", "apri", "aprire",
        "chiudi", "chiudere", "porta", "portare",
        # English
        "call", "send", "write", "buy", "prepare", "update", "fix",
        "check", "verify", "create", "pay", "book", "reply", "answer",
        "deploy", "publish", "test", "complete", "finish", "open", "close",
        "email", "ship", "push",
    }
)

_IT_WEEKDAYS = {
    "lunedi": 0, "martedi": 1, "mercoledi": 2, "giovedi": 3,
    "venerdi": 4, "sabato": 5, "domenica": 6,
}
_EN_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")

_ACCENT_MAP = str.maketrans("àáèéìíòóùú", "aaeeiioouu")


def _normalize(text: str) -> str:
    return text.lower().translate(_ACCENT_MAP)


def _next_weekday(today: date, target_dow: int) -> date:
    """Next occurrence of a weekday, strictly in the future (same day -> +7)."""
    days_ahead = (target_dow - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def _match_explicit_date(normalized: str, today: date) -> date | None:
    iso = _ISO_DATE_RE.search(normalized)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            pass
    dmy = _DMY_DATE_RE.search(normalized)
    if dmy:
        day, month = int(dmy.group(1)), int(dmy.group(2))
        year_raw = dmy.group(3)
        try:
            if year_raw:
                year = int(year_raw)
                if year < 100:
                    year += 2000
                return date(year, month, day)
            candidate = date(today.year, month, day)
            if candidate < today:
                candidate = date(today.year + 1, month, day)
            return candidate
        except ValueError:
            pass
    return None


def _match_fu(normalized: str, today: date) -> date | None:
    explicit = _match_explicit_date(normalized, today)
    if explicit is not None:
        return explicit
    if re.search(r"\bdopodomani\b", normalized):
        return today + timedelta(days=2)
    if re.search(r"\bdomani\b|\btomorrow\b", normalized):
        return today + timedelta(days=1)
    if re.search(r"\boggi\b|\btoday\b", normalized):
        return today
    for table in (_IT_WEEKDAYS, _EN_WEEKDAYS):
        for word, dow in table.items():
            if re.search(rf"\b{word}\b", normalized):
                return _next_weekday(today, dow)
    return None


def _match_project(
    normalized: str,
    projects: Sequence[tuple[str, str | None]],
) -> str | None:
    """Case-insensitive whole-word match of slug or project name in the text.

    On multiple matches the longest matched token wins (most specific).
    """
    best_slug: str | None = None
    best_len = 0
    for slug, name in projects:
        if not slug or not _CLASSIFIABLE_SLUG_RE.match(slug):
            continue
        for token in (slug, (name or "").strip()):
            token_norm = _normalize(token)
            if len(token_norm) < _MIN_PROJECT_TOKEN_LEN:
                continue
            pattern = rf"(?<![a-z0-9]){re.escape(token_norm)}(?![a-z0-9])"
            if re.search(pattern, normalized) and len(token_norm) > best_len:
                best_slug = slug
                best_len = len(token_norm)
    return best_slug


def _match_type(normalized: str, has_project: bool) -> str:
    if _IDEA_PREFIX_RE.match(normalized):
        return "idea"
    if _DECIDI_RE.search(normalized):
        return "decidi"
    words = re.findall(r"[a-z0-9]+", normalized[:80])
    if has_project and words and words[0] in _ACTION_VERBS:
        return "azione"
    return "promemoria"


def heuristic_classify(
    text: str,
    today: str,
    projects: Sequence[tuple[str, str | None]] = (),
) -> TodoClassification | None:
    """Best-effort deterministic classification (IT + EN).

    Args:
        text: raw captured todo text.
        today: ISO date (``YYYY-MM-DD``) used as the reference day.
        projects: ``(slug, name)`` candidates from the project index.

    Returns:
        A :class:`TodoClassification` when at least one field could be
        inferred, ``None`` otherwise (caller keeps the defaults intact).
    """
    if not text or not text.strip():
        return None
    try:
        today_date = date.fromisoformat(today)
    except (TypeError, ValueError):
        return None

    normalized = _normalize(text)
    project_slug = _match_project(normalized, projects)
    todo_type = _match_type(normalized, has_project=project_slug is not None)
    fu = _match_fu(normalized, today_date)

    if todo_type == "promemoria" and project_slug is None and fu is None:
        return None  # nothing inferred: never make the defaults worse

    return TodoClassification(
        type=todo_type,  # type: ignore[arg-type]  # values constrained above
        project_slug=project_slug,
        fu_date=fu.isoformat() if fu else None,
        doer=None,
        confidence=0.3,
        reasoning="heuristic fallback (no LLM provider configured)",
    )
