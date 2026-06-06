"""B-L2 finding summary + why_now polish service (Gemma 4 E4B instruct).

Composes a concise Italian summary (1-2 sentences) plus a `why_now`
temporal reasoning sentence for triage UX. Severity / confidence / recurrence
count are Python-side categorical values and never come back through the LLM.
"""

from __future__ import annotations

import json
from typing import Any

from core.api.services.brain.llm._runner import run_polish
from core.api.services.brain.llm.base import BrainLLMService, PolishResult
from core.api.services.brain.llm.cache import polish_cache_key
from core.api.services.brain.llm.constants import MAX_TOKENS

FINDING_SUMMARY_SYSTEM_PROMPT = """You compose Italian finding summaries for Brain v1.

Given a finding (drift signal, memory operation, etc.) plus supporting evidence, compose a concise Italian summary (1-2 sentences) AND a `why_now` reasoning sentence explaining the temporal urgency.

MANDATORY:
- Output language: Italian only
- Cite at least 1 evidence_ref from the allowed_evidence_refs list
- Use ONLY refs in the allowed_evidence_refs list
- NO new facts beyond the evidence list
- Do NOT polish severity, confidence or recurrence_count — those are Python-computed categorical fields

Return a JSON object only, no markdown commentary:
{
  "summary_polished": "<1-2 Italian sentences>",
  "why_now_polished": "<1 Italian sentence on temporal context>",
  "cited_evidence_refs": ["ref1"]
}"""


def build_finding_summary_user_prompt(
    *,
    finding: dict[str, Any],
    evidence: list[dict[str, Any]],
    allowed_evidence_refs: list[str],
    deterministic_summary: str,
    deterministic_why_now: str,
) -> str:
    finding_json = json.dumps(finding, ensure_ascii=False, indent=2)
    evidence_json = json.dumps(evidence, ensure_ascii=False, indent=2)
    refs_json = json.dumps(allowed_evidence_refs, ensure_ascii=False)
    summary = (deterministic_summary or "").strip() or "(nessuna baseline)"
    why_now = (deterministic_why_now or "").strip() or "(nessuna baseline)"
    return (
        "Finding (deterministic, do NOT contradict numerical fields):\n"
        f"{finding_json}\n\n"
        "Supporting evidence (cite only these refs):\n"
        f"{evidence_json}\n\n"
        "Allowed evidence refs (use ONLY these):\n"
        f"{refs_json}\n\n"
        "Deterministic baseline (already shown to operator if you fail):\n"
        f"summary: {summary}\n"
        f"why_now: {why_now}\n\n"
        "Compose the Italian polish summary as JSON."
    )


async def polish_finding_summary(
    *,
    service: BrainLLMService,
    grounding_strict: bool,
    run_id: str,
    finding_id: str,
    finding: dict[str, Any],
    evidence: list[dict[str, Any]],
    allowed_evidence_refs: list[str],
    deterministic_summary: str,
    deterministic_why_now: str,
) -> PolishResult:
    user_prompt = build_finding_summary_user_prompt(
        finding=finding,
        evidence=evidence,
        allowed_evidence_refs=allowed_evidence_refs,
        deterministic_summary=deterministic_summary,
        deterministic_why_now=deterministic_why_now,
    )
    return await run_polish(
        service=service,
        purpose="finding_summary",
        system_prompt=FINDING_SUMMARY_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=MAX_TOKENS["finding_summary"],
        allowed_evidence_refs=allowed_evidence_refs,
        idempotency_key=polish_cache_key("finding_summary", run_id, finding_id),
        grounding_strict=grounding_strict,
    )
