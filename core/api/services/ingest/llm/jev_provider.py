# v1.0.0 - 2026-09-21 - Plan master U1/SP-B (R3/R4): Jev classifier for ingest.
"""Jev (System One) classifier for the ingest pipeline.

Implements the same ``LLMClassifier`` protocol as the local gateway but with
ONE batch call instead of a multi-token LLM completion: ~0.8s, ~$0.00002
per document vs seconds and ~100x the cost for a local LLM tier.

Batch composition (plan R3/R4 — all in one request):
- Choice: document_type (13 options from ALLOWED_TARGETS)
- Choice: project_slug (from the known projects in the classification
  context, NOT invented)
- Noul: contains_secret (R4 gate: a credential, API key, token or password
  that must be redacted BEFORE indexing)
- Score: signal_quality (low/usable/high — routing signal for triage)

Fail-closed: provider unavailable, malformed answers, no known projects ->
``None`` preserves deterministic classification but never clears an unknown
safety verdict. Typed credentials block locally before egress. The caller and
insert saga enforce requires_redaction/unverified before indexing; a semantic
clear verdict never bypasses the deterministic scan of the original document.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core.api.services.ingest.llm.base import LLMClassification
from core.api.services.ingest.llm.classification_context import _project_candidates
from core.api.services import bug_reports_core
from core.api.services.jev.outbound import contains_credentials, sanitize_payload, sanitize_text

logger = logging.getLogger(__name__)

_SECRET_THRESHOLD = 0.9
_CONTENT_CAP = 6000
_TYPE_CRITERIA = {
    "handoff": "Session continuity notes between agents or work sessions",
    "plan": "Implementation plan, change plan, product contract",
    "brainstorm": "Exploratory synthesis, open-ended exploration output",
    "solution": "Verified post-fix explanation, how a problem was solved",
    "audit": "Adversarial review or conformance assessment",
    "research": "Research notes, external source analysis",
    "guide": "Reusable non-code specification or reference",
    "analysis": "Analysis of data, metrics, or a system",
    "policy": "Durable choice, policy, or decision record",
    "contract": "Typed document with mandatory frontmatter",
    "transcript": "Conversation transcript, chat log",
    "record": "Invoice, receipt, bill, bank statement, formal record",
    "report": "Measured result, QA output, experiment outcome",
}
_QUALITY_CRITERIA = [
    "low: ephemeral, generic, or no durable value",
    "useful context",
    "high: specific, actionable detail",
]


class JevClassifier:
    """Batch-mode System One classifier behind the ingest LLMClassifier port."""

    def __init__(self, provider: Any = None) -> None:
        self._provider = provider
        self.last_error: dict[str, Any] | None = None
        self.secret_safety = "unverified"

    def _unavailable(self, status: str, reason: str) -> None:
        self.last_error = {"status": status, "reason": reason, "secret_safety": self.secret_safety}
        return None

    async def classify(
        self,
        content_excerpt: str,
        context: dict,
    ) -> LLMClassification | None:
        self.last_error = None
        self.secret_safety = "unverified"
        try:
            return await self._classify(content_excerpt, context)
        except Exception:  # provider/model/schema failures cannot erase the safety state
            return self._unavailable("no_result", "jev_classifier_invalid_result")

    async def _classify(self, content_excerpt: str, context: dict) -> LLMClassification | None:
        if contains_credentials(content_excerpt or ""):
            self.secret_safety = "requires_redaction"
            return self._unavailable("requires_redaction", "deterministic_secret_detected")
        # The model only sees sanitized text. An unlabeled high-entropy token
        # removed by that sanitizer cannot be cleared by its answer.
        if bug_reports_core.high_entropy_tokens(content_excerpt or ""):
            self.secret_safety = "unverified"
            return self._unavailable("unverified", "ambiguous_high_entropy_detected")
        from core.api.services.jev import load_provider
        from core.api.services.jev.types import ScoringQuestion
        from core.api.services.tool_usage import resolve_tenant_id

        provider = self._provider if self._provider is not None else load_provider()
        if provider is None:
            return self._unavailable("disabled", "jev_flag_off")

        content = sanitize_text((content_excerpt or "")[:_CONTENT_CAP])
        known_projects = [
            project for project in _project_candidates(
                content, context.get("projects") or [],
                source_context=context.get("source_context"),
            )
            if re.fullmatch(r"[a-z0-9_&-]{2,64}", project["slug"])
        ]
        if not known_projects:
            return self._unavailable("no_projects", "no_known_projects_in_context")
        known_projects = sanitize_payload(known_projects)
        project_criteria = {
            p["slug"]: " ".join(filter(None, (p["name"], p["description"]))) or p["slug"]
            for p in known_projects
        }

        questions = [
            ScoringQuestion(
                id="document_type",
                type="choice",
                instructions="What kind of document is this?",
                criteria=_TYPE_CRITERIA,
            ),
            ScoringQuestion(
                id="project_slug",
                type="choice",
                instructions=(
                    "Which project does this document belong to? Pick the "
                    "project whose name or description best matches the content."
                ),
                criteria=project_criteria,
            ),
            ScoringQuestion(
                id="contains_secret",
                type="noul",
                instructions=(
                    "Does the text contain a credential, API key, token, "
                    "password, or private key that must be redacted before indexing?"
                ),
            ),
            ScoringQuestion(
                id="signal_quality",
                type="score",
                instructions="How much durable signal does this document carry?",
                criteria=_QUALITY_CRITERIA,
            ),
        ]
        state = {"content": content, "known_projects": known_projects}
        result, outcome = await provider.score_with_outcome(
            state,
            questions,
            tenant_id=resolve_tenant_id(None),
            surface="ingest_classify",
        )
        if result is None:
            return self._unavailable(outcome, f"provider_{outcome}")

        secret_answer = result.answers.get("contains_secret")
        if secret_answer is None or secret_answer.noul is None:
            return self._unavailable("no_result", "missing_secret_answer")
        contains_secret = secret_answer.noul >= _SECRET_THRESHOLD
        if contains_secret:
            self.secret_safety = "requires_redaction"
        elif (secret_answer.noul <= 0.1 and len(content_excerpt) <= _CONTENT_CAP
              and not context.get("_source_text_truncated")):
            self.secret_safety = "clear"
        else:
            self.secret_safety = "unverified"
        type_answer = result.answers.get("document_type")
        project_answer = result.answers.get("project_slug")
        if type_answer is None or type_answer.choice not in _TYPE_CRITERIA:
            return self._unavailable("no_result", "missing_type")
        if project_answer is None or project_answer.choice is None:
            return self._unavailable("no_result", "missing_project")
        if project_answer.choice not in project_criteria:
            return self._unavailable("no_result", "project_outside_set")
        quality_answer = result.answers.get("signal_quality")
        quality = quality_answer.score if quality_answer is not None else None
        quality_usable = (
            quality is not None and quality >= 1.0
            and quality_answer.confidence is not None
            and quality_answer.confidence > 0.4
        )

        # R4: the secret gate is recorded in the classification so the caller
        # can refuse indexing BEFORE the content reaches the embedding pipeline.
        self.last_error = None
        return LLMClassification(
            project_slug=project_answer.choice,
            document_type=type_answer.choice,  # type: ignore[arg-type]
            title=sanitize_text(str(context.get("_document_title") or content.split("\n", 1)[0]))[:300].strip() or "untitled",
            tags=["jev-classified"] + (["needs-redaction"] if contains_secret else []),
            secret_safety=self.secret_safety,
            confidence=min(type_answer.confidence or 0.0, project_answer.confidence or 0.0)
            if quality_usable else 0.0,
            reasoning=(
                f"Jev batch: type={type_answer.choice} "
                f"(conf {type_answer.confidence or '?'}) project={project_answer.choice} "
                f"(conf {project_answer.confidence or '?'}) "
                f"secret={secret_answer.noul if secret_answer and secret_answer.noul is not None else '?'} "
                f"quality={quality}/2"
            ),
        )

    @property
    def secret_detected(self) -> bool:
        """R4: True when the last classify() found a confident secret."""
        return self.secret_safety == "requires_redaction"
