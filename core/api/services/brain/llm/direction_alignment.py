"""Brain v1.2.1 — DR8 direction alignment LLM classifier.

Implements the missing `classify_direction_alignment(payload)` method called
by `api.services.brain.rules.dr8_direction_misalignment.build_signals`.

Two-stage pipeline:
    1. tier-fast Gemma 4 E4B classify (temperature 0.1, JSON strict)
         -> {status, confidence, vision_coverage_pct, observed_themes, rationale}
    2. tier-write Gemma 3 12B QAT rewrite (temperature 0.3, prose) when
         status != "aligned"
         -> {proposed_summary, proposed_out_of_scope}

Per brainstorm §6: NO banned-words retry. Output is accepted as-is or
rejected entirely. On any failure (LLM error, JSON malformed, missing
direction) the function returns None and the deterministic 0.55 fallback
in DR8 stays authoritative.

Prompt heritage: italian asciutto from `/tmp/spike_drift_above_v1.py` SYSTEM.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.api.services.brain.llm.parsers import (
    ParseError,
    coerce_text,
    parse_json_or_raise,
)

logger = logging.getLogger(__name__)

VALID_STATUSES: frozenset[str] = frozenset(
    {"aligned", "drift_above", "drift_below", "drift_lateral", "blocked"}
)

CLASSIFIER_MODEL = "tier-fast"
REWRITER_MODEL = "tier-write"

CLASSIFIER_TEMPERATURE = 0.1
REWRITER_TEMPERATURE = 0.3

CLASSIFIER_MAX_TOKENS = 800
REWRITER_MAX_TOKENS = 1500


SYSTEM_CLASSIFIER = """Sei il drift detector di Brain v1. Confronti la direction DICHIARATA di un progetto (summary + out_of_scope) con gli events osservati nel ciclo (commits, tasks, PRs, handoffs).

Classifichi il drift in 5 categorie esclusive:
- aligned: gli events confermano la direction (>=70% coverage)
- drift_above: events piu' grandi della vision (vision sotto-dimensionata, progetto cresciuto oltre)
- drift_below: events piu' piccoli/lenti della vision (ambizione non raggiunta)
- drift_lateral: events in direzione non prevista (nuovo focus emerso non dichiarato)
- blocked: events segnalano blocco esplicito (handoff/tasks bloccati)

Stimi `vision_coverage_pct` 0-100: quanta percentuale degli events si riconduce alla direction dichiarata.

REGOLE OUTPUT:
1. Output STRICT JSON, niente prosa fuori.
2. observed_themes: 3-6 temi sintetici dedotti dagli events (es. "knowledge graph cross-project", "console Marvis UI", "brain pipeline").
3. rationale: 1-2 frasi italiane asciutte, max 40 parole totali.
4. confidence: 0.0-1.0 (stima quanto la classificazione e' affidabile dati gli events).
5. Italiano sempre, numeri esatti, no stime vaghe.

Schema output STRETTO:
{
  "status": "aligned|drift_above|drift_below|drift_lateral|blocked",
  "confidence": 0.0-1.0,
  "vision_coverage_pct": 0-100,
  "observed_themes": ["..."],
  "rationale": "..."
}"""


SYSTEM_REWRITER = """Sei il direction rewriter di Brain v1. Hai ricevuto la classificazione del drift e devi proporre una direction aggiornata che rifletta i temi osservati.

Compito: produrre `proposed_summary` (5-6 righe italiano asciutto) + `proposed_out_of_scope` (2-3 righe, cosa il progetto NON fa).

REGOLE OUTPUT:
1. Output STRICT JSON, niente prosa fuori.
2. proposed_summary: 5-6 righe, italiano asciutto, copre vision + mission attuale dedotta dagli events.
3. proposed_out_of_scope: 2-3 righe, esclusioni chiare, complementare al summary.
4. Numeri esatti dove possibile, no stime vaghe.
5. Niente marketing-speak, niente "ottimizzare/consolidare/significativo/evoluzione".
6. Italiano sempre.

Schema output STRETTO:
{
  "proposed_summary": "...",
  "proposed_out_of_scope": "..."
}"""


def build_classifier_prompt(payload: dict[str, Any]) -> str:
    """Build the tier-fast classifier user prompt from DR8 payload."""
    slug = payload.get("project_slug", "?")
    direction_summary = payload.get("direction_summary") or "(nessuna)"
    direction_oos = payload.get("direction_out_of_scope") or "(nessuna)"
    events_observed = payload.get("events_observed") or "(nessuno)"
    events_refs = payload.get("events_refs") or []

    refs_block = "\n".join(f"  - {r}" for r in events_refs[:20]) if events_refs else "  (nessuna)"

    return (
        f"Progetto: {slug}\n\n"
        "DIRECTION DICHIARATA:\n"
        f"  summary: {direction_summary}\n"
        f"  out_of_scope: {direction_oos}\n\n"
        "EVENTS OSSERVATI NEL CICLO:\n"
        f"  {events_observed}\n\n"
        "REFS EVENTI (per contesto):\n"
        f"{refs_block}\n\n"
        "Classifica il drift. Solo JSON nello schema indicato."
    )


def build_rewriter_prompt(
    payload: dict[str, Any], classifier_result: dict[str, Any]
) -> str:
    """Build the tier-write rewriter user prompt from payload + classifier output."""
    slug = payload.get("project_slug", "?")
    direction_summary = payload.get("direction_summary") or "(nessuna)"
    direction_oos = payload.get("direction_out_of_scope") or "(nessuna)"
    events_observed = payload.get("events_observed") or "(nessuno)"

    themes = classifier_result.get("observed_themes") or []
    themes_block = "\n".join(f"  - {t}" for t in themes[:8]) if themes else "  (nessuno)"
    status = classifier_result.get("status") or "?"
    rationale = classifier_result.get("rationale") or ""

    return (
        f"Progetto: {slug}\n"
        f"Drift status: {status}\n"
        f"Rationale classifier: {rationale}\n\n"
        "DIRECTION DICHIARATA (potenzialmente obsoleta):\n"
        f"  summary: {direction_summary}\n"
        f"  out_of_scope: {direction_oos}\n\n"
        "TEMI OSSERVATI (dedotti dagli events):\n"
        f"{themes_block}\n\n"
        "EVENTS OSSERVATI NEL CICLO:\n"
        f"  {events_observed}\n\n"
        "Proponi proposed_summary + proposed_out_of_scope aggiornati. Solo JSON nello schema indicato."
    )


def _validate_classifier_result(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the classifier JSON shape. Return cleaned dict or None on shape error."""
    status = parsed.get("status")
    if not isinstance(status, str) or status not in VALID_STATUSES:
        logger.warning(
            "dr8_classifier_invalid_status status=%r valid=%s", status, sorted(VALID_STATUSES)
        )
        return None

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        logger.warning("dr8_classifier_invalid_confidence value=%r", parsed.get("confidence"))
        return None
    if not (0.0 <= confidence <= 1.0):
        # Clamp instead of reject — small LLMs can emit 1.1 or -0.05.
        confidence = max(0.0, min(1.0, confidence))

    try:
        coverage = float(parsed.get("vision_coverage_pct", 0.0))
    except (TypeError, ValueError):
        coverage = 0.0
    coverage = max(0.0, min(100.0, coverage))

    themes = parsed.get("observed_themes") or []
    if not isinstance(themes, list):
        themes = []
    themes = [coerce_text(t) for t in themes if coerce_text(t)]

    rationale = coerce_text(parsed.get("rationale"))

    return {
        "status": status,
        "confidence": confidence,
        "vision_coverage_pct": coverage,
        "observed_themes": themes,
        "rationale": rationale,
    }


def _validate_rewriter_result(parsed: dict[str, Any]) -> dict[str, str] | None:
    """Validate the rewriter JSON shape. Return cleaned dict or None on shape error."""
    summary = coerce_text(parsed.get("proposed_summary"))
    out_of_scope = coerce_text(parsed.get("proposed_out_of_scope"))
    if not summary and not out_of_scope:
        logger.warning("dr8_rewriter_empty_payload parsed=%s", json.dumps(parsed)[:200])
        return None
    return {
        "proposed_summary": summary,
        "proposed_out_of_scope": out_of_scope,
    }


async def classify_direction_alignment_impl(
    *,
    llm_surface: Any,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Two-stage classifier+rewriter for DR8 direction_misalignment.

    Args:
        llm_surface: object exposing `call_json(model, system_prompt, user_prompt,
            max_tokens, temperature) -> dict | None`. The NoOp service exposes
            the same surface but returns None always.
        payload: dict from DR8 caller with keys
            project_slug, direction_summary, direction_out_of_scope,
            events_observed, events_refs.

    Returns:
        dict with keys {status, confidence, rationale, proposed_summary,
        proposed_out_of_scope, vision_coverage_pct, observed_themes} OR None
        on any failure (LLM error, malformed JSON, missing direction).
    """
    # Guard: direction missing -> caller will keep deterministic baseline.
    if not payload.get("direction_summary"):
        logger.debug("dr8_classifier_skipped reason=missing_direction_summary")
        return None

    classifier_user = build_classifier_prompt(payload)
    classify_parsed = await llm_surface.call_json(
        model=CLASSIFIER_MODEL,
        system_prompt=SYSTEM_CLASSIFIER,
        user_prompt=classifier_user,
        max_tokens=CLASSIFIER_MAX_TOKENS,
        temperature=CLASSIFIER_TEMPERATURE,
    )
    if classify_parsed is None:
        logger.warning("dr8_classifier_call_failed stage=classify")
        return None

    classifier_result = _validate_classifier_result(classify_parsed)
    if classifier_result is None:
        return None

    # Stage 2: rewriter only when drift detected.
    proposed_summary: str | None = None
    proposed_out_of_scope: str | None = None
    if classifier_result["status"] != "aligned":
        rewriter_user = build_rewriter_prompt(payload, classifier_result)
        rewrite_parsed = await llm_surface.call_json(
            model=REWRITER_MODEL,
            system_prompt=SYSTEM_REWRITER,
            user_prompt=rewriter_user,
            max_tokens=REWRITER_MAX_TOKENS,
            temperature=REWRITER_TEMPERATURE,
        )
        if rewrite_parsed is not None:
            rewriter_result = _validate_rewriter_result(rewrite_parsed)
            if rewriter_result is not None:
                proposed_summary = rewriter_result["proposed_summary"] or None
                proposed_out_of_scope = rewriter_result["proposed_out_of_scope"] or None
        # rewrite failure is non-fatal: we still return the classifier verdict
        # so DR8 can record confidence + status without a proposed payload.

    return {
        "status": classifier_result["status"],
        "confidence": classifier_result["confidence"],
        "vision_coverage_pct": classifier_result["vision_coverage_pct"],
        "observed_themes": classifier_result["observed_themes"],
        "rationale": classifier_result["rationale"],
        "proposed_summary": proposed_summary,
        "proposed_out_of_scope": proposed_out_of_scope,
    }


# Re-export parse helper so test code can patch one place.
__all__ = [
    "CLASSIFIER_MAX_TOKENS",
    "CLASSIFIER_MODEL",
    "CLASSIFIER_TEMPERATURE",
    "ParseError",
    "REWRITER_MAX_TOKENS",
    "REWRITER_MODEL",
    "REWRITER_TEMPERATURE",
    "SYSTEM_CLASSIFIER",
    "SYSTEM_REWRITER",
    "VALID_STATUSES",
    "build_classifier_prompt",
    "build_rewriter_prompt",
    "classify_direction_alignment_impl",
    "parse_json_or_raise",
]
