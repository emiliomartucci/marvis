# v1.0.0 - 2026-06-02 - FTS5 query sanitization (fix: hyphenated/special user queries broke MATCH)
"""Sanitize arbitrary user text into a safe FTS5 ``MATCH`` expression.

A raw user query passed straight to ``... MATCH ?`` lets FTS5 interpret special
tokens: a hyphen/colon (``opt-out``, ``de-PiR``, ``a:b``) is parsed as a column
filter / operator and raises ``no such column: X``, which kills the KG,
semantic-BM25 (``documents_fts``) and row-FTS lanes — fail-silent: search
returns 0 / "retriever unavailable" while the data is actually there.

``fts5_safe_query`` wraps each whitespace-delimited token in double quotes so
FTS5 treats it as a literal string token (a phrase per token, implicit-AND
across tokens) — never as a column filter or boolean operator. Tokens with no
word character (pure punctuation) are dropped. Returns ``""`` when nothing is
matchable; callers treat that as "no FTS hit" and skip the MATCH.

Note: this is a robustness default for a search box, not an FTS power-user
surface — prefix (``term*``) / boolean / column syntax in the raw query is
intentionally neutralised in exchange for never crashing the lane.
"""
from __future__ import annotations

import re

_WORDISH = re.compile(r"\w", re.UNICODE)


def fts5_safe_query(q: str) -> str:
    """Return an FTS5-safe MATCH expression for ``q`` (``""`` if nothing matchable)."""
    tokens = [t for t in (q or "").split() if _WORDISH.search(t)]
    if not tokens:
        return ""
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)
