"""B-L1 journal narrative polish service (Gemma 4 E4B instruct).

Builds a 2-3 sentence Italian narrative summarising deterministic journal
entries for a scope. Counting and numerical aggregation stay Python-side
(learning 2c2b67ab); the LLM only narrates around the structured data.
"""

from __future__ import annotations

import json
from typing import Any

from core.api.services.brain.llm._runner import run_polish
from core.api.services.brain.llm.base import BrainLLMService, PolishResult
from core.api.services.brain.llm.cache import polish_cache_key
from core.api.services.brain.llm.constants import MAX_TOKENS

JOURNAL_SYSTEM_PROMPT = """You compose Italian journal narratives for Brain v1, the MarvisX project memory system.

Compose a 2-3 sentence Italian narrative summarising the supplied journal entries for the given scope/cycle.

MANDATORY:
- Output language: Italian only
- Cite at least 1 evidence_ref from the allowed_evidence_refs list
- Use ONLY refs in the allowed_evidence_refs list
- NO new facts beyond the supplied entries
- NO counting or aggregation beyond the provided counts (counts stay numerical, Python-computed)

Return a JSON object only, no markdown commentary:
{
  "narrative_polished": "<Italian prose, 2-3 sentences>",
  "cited_evidence_refs": ["ref1", "ref2"]
}"""


def build_journal_user_prompt(
    *,
    scope_type: str,
    scope_key: str,
    cycle_key: str,
    entries: list[dict[str, Any]],
    allowed_evidence_refs: list[str],
    deterministic_narrative: str,
) -> str:
    """Build the user-facing prompt with structured data + grounding refs."""
    # Wave 3.1 smart payload (Emilio 2026-05-19): no `indent` → ~30% shorter
    # user prompt. Mac Gateway tier-write returns 502 on long prompts when
    # Mac Studio inference is under load (Gemma 3 12B QAT MLX prefill latency).
    entries_json = json.dumps(entries, ensure_ascii=False)
    refs_json = json.dumps(allowed_evidence_refs, ensure_ascii=False)
    deterministic = (deterministic_narrative or "").strip() or "(nessuna baseline)"
    return (
        f"Scope: {scope_type} = {scope_key}\n"
        f"Cycle: {cycle_key}\n\n"
        "Journal entries (already aggregated, do NOT recount):\n"
        f"{entries_json}\n\n"
        "Allowed evidence refs (use ONLY these):\n"
        f"{refs_json}\n\n"
        "Deterministic fallback (already shown to operator if you fail):\n"
        f"{deterministic}\n\n"
        "Compose the Italian polish narrative as JSON."
    )


async def polish_journal_entry(
    *,
    service: BrainLLMService,
    grounding_strict: bool,
    run_id: str,
    entry_id: str,
    scope_type: str,
    scope_key: str,
    cycle_key: str,
    entries: list[dict[str, Any]],
    allowed_evidence_refs: list[str],
    deterministic_narrative: str,
) -> PolishResult:
    """Run B-L1 polish. `entry_id` becomes the cache primary_id."""
    user_prompt = build_journal_user_prompt(
        scope_type=scope_type,
        scope_key=scope_key,
        cycle_key=cycle_key,
        entries=entries,
        allowed_evidence_refs=allowed_evidence_refs,
        deterministic_narrative=deterministic_narrative,
    )
    return await run_polish(
        service=service,
        purpose="journal",
        system_prompt=JOURNAL_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=MAX_TOKENS["journal"],
        allowed_evidence_refs=allowed_evidence_refs,
        idempotency_key=polish_cache_key("journal", run_id, entry_id),
        grounding_strict=grounding_strict,
    )
