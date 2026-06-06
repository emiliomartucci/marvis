"""B-L3 finding suggested_artifact reasoning polish service.

Lookup-augmented prose: given a deterministic mapping_table (finding_type →
artifact_type), explain in Italian WHY the finding suggests a particular
artifact for operator triage. tier-write Gemma 3 12B QAT keeps the answer
constrained to 1-2 sentences without chain-of-thought leaks.
"""

from __future__ import annotations

import json
from typing import Any

from core.api.services.brain.llm._runner import run_polish
from core.api.services.brain.llm.base import BrainLLMService, PolishResult
from core.api.services.brain.llm.cache import polish_cache_key
from core.api.services.brain.llm.constants import MAX_TOKENS

FINDING_REASONING_SYSTEM_PROMPT = """You explain WHY a Brain finding suggests a specific artifact type (task, ADR, learning, etc.).

Inputs:
- finding context (type, scope, severity)
- mapping_table (finding_type -> artifact_type rules, deterministic — already authoritative)
- evidence list (cite only these refs)

Compose 1-2 Italian sentences explaining the reasoning for the operator triage UX.

MANDATORY:
- Output language: Italian only
- Cite at least 1 evidence_ref from the allowed_evidence_refs list
- Use ONLY refs in the allowed_evidence_refs list
- Lookup-augmented: defer to mapping_table for the suggested artifact, do NOT propose alternatives
- NO new facts beyond the supplied inputs

Return a JSON object only, no markdown commentary:
{
  "reasoning_polished": "<1-2 Italian sentences>",
  "cited_evidence_refs": ["ref1"]
}"""


def build_finding_reasoning_user_prompt(
    *,
    finding: dict[str, Any],
    mapping_table: dict[str, Any],
    evidence: list[dict[str, Any]],
    allowed_evidence_refs: list[str],
    deterministic_reasoning: str,
) -> str:
    finding_json = json.dumps(finding, ensure_ascii=False, indent=2)
    mapping_json = json.dumps(mapping_table, ensure_ascii=False, indent=2)
    evidence_json = json.dumps(evidence, ensure_ascii=False, indent=2)
    refs_json = json.dumps(allowed_evidence_refs, ensure_ascii=False)
    deterministic = (deterministic_reasoning or "").strip() or "(nessuna baseline)"
    return (
        "Finding (deterministic):\n"
        f"{finding_json}\n\n"
        "Mapping table (authoritative — do NOT contradict):\n"
        f"{mapping_json}\n\n"
        "Supporting evidence (cite only these refs):\n"
        f"{evidence_json}\n\n"
        "Allowed evidence refs (use ONLY these):\n"
        f"{refs_json}\n\n"
        "Deterministic baseline (already shown to operator if you fail):\n"
        f"{deterministic}\n\n"
        "Compose the Italian polish reasoning as JSON."
    )


async def polish_finding_reasoning(
    *,
    service: BrainLLMService,
    grounding_strict: bool,
    run_id: str,
    finding_id: str,
    finding: dict[str, Any],
    mapping_table: dict[str, Any],
    evidence: list[dict[str, Any]],
    allowed_evidence_refs: list[str],
    deterministic_reasoning: str,
) -> PolishResult:
    user_prompt = build_finding_reasoning_user_prompt(
        finding=finding,
        mapping_table=mapping_table,
        evidence=evidence,
        allowed_evidence_refs=allowed_evidence_refs,
        deterministic_reasoning=deterministic_reasoning,
    )
    return await run_polish(
        service=service,
        purpose="finding_reasoning",
        system_prompt=FINDING_REASONING_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=MAX_TOKENS["finding_reasoning"],
        allowed_evidence_refs=allowed_evidence_refs,
        idempotency_key=polish_cache_key("finding_reasoning", run_id, finding_id),
        grounding_strict=grounding_strict,
    )
