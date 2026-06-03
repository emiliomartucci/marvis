# v1.0.0 - 2026-03-28 - Ebbinghaus decay + boost logic for brain-inspired memory
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# Half-lives per doc_type in days (inspired by engram-rs: episodic < semantic < procedural)
HALF_LIFE_DAYS: dict[str, float] = {
    # Episodic — specific experiences tied to time and context
    "handoff": 21.0,
    "task": 14.0,
    # Semantic — extracted and reworked knowledge
    "file": 45.0,
    "brainstorm": 30.0,
    "audit": 60.0,
    "plan": 45.0,
    # Structural — project context and consolidated knowledge
    "project": 90.0,
    "learning": 120.0,
}

SEVERITY_TAGS: frozenset[str] = frozenset({"critical", "security", "data-loss", "production"})
SALIENCE_FLOOR = 0.1
SALIENCE_CEILING = 1.0


def compute_decay(
    current_salience: float,
    doc_type: str,
    days_since_update: float,
) -> float:
    """Ebbinghaus-inspired exponential decay per doc type.

    Each doc type has a different half-life: handoffs decay fast (21d),
    learnings decay slow (120d). Floor is 0.1 (never fully forgotten).
    """
    if days_since_update <= 0:
        return current_salience
    hl = HALF_LIFE_DAYS.get(doc_type, 45.0)
    decayed = current_salience * math.pow(2.0, -days_since_update / hl)
    return max(round(decayed, 4), SALIENCE_FLOOR)


def compute_boost(
    current_salience: float,
    severity_tags: list[str] | None = None,
    access_count: int = 0,
    is_anomaly: bool = False,
) -> float:
    """Boost signals inspired by amygdala salience tagging.

    Severity: +0.2 for critical/security/data-loss/production tags.
    Frequency: logarithmic (from engram-rs), saturates at ~30 accesses.
    Anomaly: +0.15 (Hawkins: prediction error has max salience).
    """
    boost = 0.0
    if severity_tags and SEVERITY_TAGS & set(severity_tags):
        boost += 0.2
    if access_count > 0:
        boost += min(0.2, 0.17 * math.log(1 + access_count))
    if is_anomaly:
        boost += 0.15
    result = min(round(current_salience + boost, 4), SALIENCE_CEILING)
    if boost > 0:
        logger.debug("salience boost: %.4f -> %.4f (sev=%s, acc=%d, anom=%s)",
                     current_salience, result, bool(severity_tags), access_count, is_anomaly)
    return result
