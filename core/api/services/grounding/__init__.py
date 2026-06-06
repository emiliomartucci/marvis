# v1.0.0 - 2026-06-04 - Track 2 #2: span-citation + grounding layer (flag MARVIS_BRIEF_CITATIONS)
"""Span-citation + grounding-verification layer (Track 2 #2).

Pure, model-free pieces of the select-then-generate-then-verify pipeline that
makes every fact in the brief carry its source span and drops/flags claims that
are not anchored to evidence. See the roadmap "Research Insights — #2".

This package is a LIBRARY. It exposes the layer but does NOT rewire the live
``session_brief``. The integration seam is gated by the ``MARVIS_BRIEF_CITATIONS``
setting (default OFF): when off, the brief is byte-for-byte unchanged because the
seam is never entered (see ``core/api/use_cases/projects.py`` note + the flag in
``core/api/config.py``).

The only model-dependent piece (the NLI verifier) is behind a ``Verifier``
Protocol whose DEFAULT implementation (``NoopVerifier``) loads nothing.
"""
from __future__ import annotations

from core.api.services.grounding.citations import (
    Decision,
    EvidenceSet,
    EvidenceSpan,
    decide,
    validate_citations,
    verbatim_guard,
)
from core.api.services.grounding.verifier import (
    MiniCheckVerifier,
    NoopVerifier,
    Verdict,
    Verifier,
)

__all__ = [
    "Decision",
    "EvidenceSet",
    "EvidenceSpan",
    "decide",
    "validate_citations",
    "verbatim_guard",
    "MiniCheckVerifier",
    "NoopVerifier",
    "Verdict",
    "Verifier",
    "annotate_brief_grounding",
]


async def annotate_brief_grounding(db: object, slug: str, brief: dict) -> dict:
    """Brief integration seam for the grounding layer (Track 2 #2).

    This is the single entry point ``get_session_brief`` calls WHEN
    ``MARVIS_BRIEF_CITATIONS`` is on. It is reached only behind that flag (default
    OFF), so the live brief is unaffected by default.

    Current behaviour: a SAFE PASSTHROUGH. It returns ``brief`` unchanged. The real
    on-path — SELECT top-K #4 chunk spans into an ``EvidenceSet``, GENERATE the
    synthesized sentences with grammar-constrained ``cite_ids``, then VERIFY each
    sentence (``verbatim_guard`` → NLI head → ``decide`` → DROP/HEDGE/KEEP/
    FLAG_SYNTHESIS) — is wired DELIBERATELY together with a real ``Verifier`` (an
    off-host MiniCheck), never on the shared host (learning a09b8754). Until that
    wiring exists this stays a no-op so flipping the flag can never run a model or
    mutate the brief.
    """
    return brief
