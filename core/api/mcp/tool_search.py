"""Lexical ranking of MCP tools for the ``find_tool`` gateway.

BM25 over three fields per tool — the name split on ``_`` (weight 3), argument
names plus argument descriptions, and the description — with accent folding and
a small Italian/English function-word list. Measured in task 2951f474: when the
agent writes its own query, this finds the right tool 16 times out of 17, so no
embedding model is needed. Pure Python, no I/O.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

# Function words only; domain words (task, progetto, file...) must stay searchable.
_STOPWORDS = frozenset(
    """
    a ad al alla alle allo agli ai anche che chi ci col come con cosa da dal dalla
    dalle dei del della delle dello degli di e ed fra gli il in io la le lo loro
    ma mi mio ne nel nella nelle nello negli noi non o per piu poi qua quale quali
    quando quel quella quello questa questo qui se si sia sono su sua suo sul sulla
    sulle tra tu tua tuo un una uno vi voi
    an and are as at be by do does for from has have how i if in into is it its
    me my of on or our so than that the their them then there these this to us
    was we what when where which who why will with you your
    """.split()
)
_FIELD_WEIGHTS = {"name": 3.0, "args": 1.0, "description": 1.0}
_WORD = re.compile(r"[a-z0-9]+")
_K1, _B = 1.2, 0.75


def _tokenize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.replace("_", " ").lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return [word for word in _WORD.findall(folded) if word not in _STOPWORDS]


def _argument_text(schema: Mapping[str, Any] | None) -> str:
    parts: list[str] = []
    for name, spec in ((schema or {}).get("properties") or {}).items():
        parts.append(str(name))
        if isinstance(spec, Mapping) and spec.get("description"):
            parts.append(str(spec["description"]))
    return " ".join(parts)


def rank_tools(query: str, tools: Iterable[Any], limit: int = 5) -> list[Any]:
    """Return up to ``limit`` tools ranked for ``query``; tools need ``name``,
    ``description`` and ``parameters`` (fastmcp) or ``inputSchema`` attributes."""
    candidates = list(tools)
    terms = _tokenize(query)
    if not candidates or not terms:
        return []
    fields: dict[str, list[Counter]] = {field: [] for field in _FIELD_WEIGHTS}
    for tool in candidates:
        schema = getattr(tool, "parameters", None) or getattr(tool, "inputSchema", None)
        texts = {
            "name": tool.name,
            "args": _argument_text(schema),
            "description": getattr(tool, "description", None) or "",
        }
        for field, text in texts.items():
            fields[field].append(Counter(_tokenize(text)))
    count = len(candidates)
    avg_len = {f: (sum(sum(c.values()) for c in cs) / count) or 1.0 for f, cs in fields.items()}
    doc_freq: Counter = Counter()
    for i in range(count):
        doc_freq.update({term for field in _FIELD_WEIGHTS for term in fields[field][i]})
    idf = {term: math.log(1 + (count - df + 0.5) / (df + 0.5)) for term, df in doc_freq.items()}

    scored: list[tuple[float, str, Any]] = []
    for i, tool in enumerate(candidates):
        score = 0.0
        for field, weight in _FIELD_WEIGHTS.items():
            counts = fields[field][i]
            length = sum(counts.values())
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    norm = tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * length / avg_len[field]))
                    score += weight * idf.get(term, 0.0) * norm
        if score > 0:
            scored.append((score, tool.name, tool))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [tool for _score, _name, tool in scored[:limit]]
