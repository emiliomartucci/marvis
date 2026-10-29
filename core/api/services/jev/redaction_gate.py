# v1.0.0 - 2026-09-21 - Plan 2026-09-21 U5/KTD9: double-confirmation de-redaction.
"""Identifier gate for the high-entropy redactor (plan R18/KTD9).

Problem (learning d33420e4, replayed live on 2026-09-21): the entropy sweep
redacts internal references — typed task refs, branch names, worktree/slug tokens
— so task comments lose their PR/branch pointers as
``[REDACTED_HIGH_ENTROPY]``.

Design — FAIL-CLOSED on the security side, double confirmation to KEEP:

- the redactor stays the authority: by default every high-entropy token is
  redacted exactly as today;
- a token is KEPT only when BOTH agree:
  (1) a deterministic identifier pattern matches (task:/learning:/
      handoff: refs, VCS branch, date-kebab slug, kebab-with-hex tail), AND
  (2) the Jev provider classifies it ``internal_reference`` with confidence
      >= ``typesafe_redaction_confidence_min`` (default 0.9);
- provider unavailable, flag off, low confidence, no pattern → redacted as
  today (KTD9 fail-closed). ONE batch request per text (Choice per token —
  Choice carries confidence; Noul answers do not, and confidence is the
  load-bearing signal here);
- typed shape redactors (SECRET=, BEARER, …) always run BEFORE the gate and
  are never bypassed: an ``api_key=...`` value stays redacted even when a
  pattern and the provider would both agree;
- one ledger row per gate call that kept tokens (surface=redaction_gate,
  ``kept`` count) — the calibration harness and the operator can audit every
  de-redaction.
"""
from __future__ import annotations

import logging
from typing import Any

from core.api.services import bug_reports_core
from core.api.services.jev import types as jev_types
from core.api.services.jev.outbound import looks_like_internal_identifier

logger = logging.getLogger("marvis.jev.redaction_gate")

def _provider() -> Any:
    from core.api.services.jev import load_provider

    return load_provider()


async def redact_with_identifier_gate(
    text: str,
    *,
    tenant_id: str | None = None,
    provider: Any = None,
    confidence_min: float | None = None,
    max_tokens: int | None = None,
) -> tuple[str, int]:
    """``bug_reports_core.redact`` + U5 gate: keeps doubly-confirmed IDs.

    Returns the same ``(redacted_text, redaction_count)`` shape. On any doubt
    or unavailability the output equals today's plain ``redact()``.
    """
    from core.api import config as _config
    from core.api.services.jev import ledger as _ledger
    from core.api.services.tool_usage import resolve_tenant_id

    settings = _config.settings
    confidence_min = (
        settings.typesafe_redaction_confidence_min
        if confidence_min is None
        else confidence_min
    )
    max_tokens = (
        settings.typesafe_redaction_max_tokens if max_tokens is None else max_tokens
    )

    safe_text, _ = bug_reports_core.redact_explicit(text)
    candidates = list(dict.fromkeys(
        token
        for token in bug_reports_core.high_entropy_tokens(safe_text)
        if looks_like_internal_identifier(token)
    ))[:max_tokens]
    if not candidates:
        return bug_reports_core.redact(text)

    provider = provider if provider is not None else _provider()
    if provider is None:
        return bug_reports_core.redact(text)  # carve-out absent (OSS mirror)

    questions = [
        jev_types.ScoringQuestion(
            id=f"t{index}",
            type="choice",
            instructions=(
                f"Classify the token `{token}`: is it an internal identifier or "
                "reference (task/learning/handoff id, git branch, worktree or "
                "slug name) rather than a credential?"
            ),
            criteria={
                "internal_reference": (
                    "Identifiers and references internal to the product: "
                    "task/learning/handoff ids, branch names, worktree/slug names"
                ),
                "credential": (
                    "API keys, tokens, passwords, private keys or other secrets "
                    "that must be redacted"
                ),
                "unclear": "Cannot tell reliably",
            },
        )
        for index, token in enumerate(candidates)
    ]
    result, _outcome = await provider.score_with_outcome(
        {"tokens": candidates},
        questions,
        tenant_id=tenant_id or resolve_tenant_id(None),
        surface="redaction_gate",
    )
    if result is None:
        return bug_reports_core.redact(text)

    keep: set[str] = set()
    for index, token in enumerate(candidates):
        answer = result.answers.get(f"t{index}")
        if answer is None:
            continue
        if (
            answer.choice == "internal_reference"
            and answer.confidence is not None
            and answer.confidence >= confidence_min
        ):
            keep.add(token)

    redacted, count = bug_reports_core.redact(text, keep_tokens=keep)
    if keep:
        _ledger.record_call(
            tenant_id=tenant_id or resolve_tenant_id(None),
            surface="redaction_gate",
            outcome="ok",
            extra={"kept": len(keep), "candidates": len(candidates)},
            attempted=False,
        )
    return redacted, count


__all__ = ["looks_like_internal_identifier", "redact_with_identifier_gate"]
