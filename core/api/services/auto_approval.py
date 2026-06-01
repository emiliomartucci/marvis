# v1.3.0 - 2026-05-28 - Self-improvement veto agent list is config-driven (SELF_IMPROVEMENT_AGENTS)
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from core.api.config import settings

logger = logging.getLogger(__name__)


class ApprovalDecision(str, Enum):
    AUTO_APPROVED = "auto_approved"
    PENDING_HUMAN = "pending"


@dataclass
class ApprovalPolicy:
    """Evaluate whether a new task should be auto-approved or require human review.

    Safety valves:
    - Impact veto: high-impact tasks always need human review
    - Confidence floor: low-confidence scores need human review
    - Daily cap: checked externally (not in this class)
    """

    agent_ease_threshold: int = 6
    hybrid_ease_threshold: int = 7
    impact_veto_threshold: int = 9
    min_confidence: int = 5

    def evaluate(
        self,
        delegation: str | None,
        ease: int | None,
        impact: int | None,
        confidence: int | None,
        scored_by: str | None,
        created_by: str | None,
    ) -> tuple[ApprovalDecision, str]:
        """Return (decision, reason) for a task's initial status."""

        # Unscored tasks always need human review
        if delegation is None or ease is None:
            return ApprovalDecision.PENDING_HUMAN, "unscored_task"

        # Self-improvement hybrid proposals always require human review.
        # The agent list is config-driven (SELF_IMPROVEMENT_AGENTS) so OSS core
        # ships no internal agent names; our deploy .env arms the veto. Read
        # settings live so tests can override via monkeypatch.
        if (
            created_by is not None
            and created_by in settings.self_improvement_agents
            and delegation == "hybrid"
        ):
            return ApprovalDecision.PENDING_HUMAN, "agent_self_improvement_hybrid_veto"

        # Human delegation = human IS the gate, auto-approve
        if delegation == "human":
            return ApprovalDecision.AUTO_APPROVED, "delegation_human"

        # Safety valve: high impact always needs human eyes
        if impact is not None and impact >= self.impact_veto_threshold:
            return ApprovalDecision.PENDING_HUMAN, "high_impact_veto"

        # Safety valve: low confidence = uncertain, needs human
        if confidence is not None and confidence < self.min_confidence:
            return ApprovalDecision.PENDING_HUMAN, "low_confidence"

        # Threshold-based auto-approval
        if delegation == "agent" and ease >= self.agent_ease_threshold:
            return ApprovalDecision.AUTO_APPROVED, "agent_ease_threshold"

        if delegation == "hybrid" and ease >= self.hybrid_ease_threshold:
            return ApprovalDecision.AUTO_APPROVED, "hybrid_ease_threshold"

        return ApprovalDecision.PENDING_HUMAN, "below_threshold"


# Singleton — can be overridden per-project via project.yaml thresholds
DEFAULT_POLICY = ApprovalPolicy()
