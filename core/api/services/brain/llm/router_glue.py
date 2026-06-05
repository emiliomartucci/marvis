"""Read-path glue between Brain HTTP router and the polish layer.

Pattern: deterministic baseline returns immediately, polished output ships
opportunistically from the in-memory TTL cache. Cache misses spawn a
fire-and-forget background polish task that writes back on next request.

Constraints (constitution invariants):
- LLM call NEVER inside `acquire_write_db()` lock (sleep-before-write).
- No persistence of polished content — cache is the only state.
- Failures fall through silently (log WARNING, never raise).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.api.config import settings as global_settings
from core.api.models.brain import Finding, FindingRedacted, JournalEntry
from core.api.services.brain.llm.base import BrainLLMService, PolishResult
from core.api.services.brain.llm.cache import (
    IdempotencyCache,
    get_polish_cache,
    polish_cache_key,
)
from core.api.services.brain.llm.factory import (
    BrainLLMConfigError,
    get_brain_llm_service,
)
from core.api.services.brain.llm.finding_reasoning import polish_finding_reasoning
from core.api.services.brain.llm.finding_summary import polish_finding_summary
from core.api.services.brain.llm.journal_polish import polish_journal_entry

logger = logging.getLogger(__name__)


# Deterministic mapping table fed inline to B-L3 prompts. Reading this from
# code keeps the LLM lookup-augmented without re-reading docs at request time.
FINDING_TYPE_TO_ARTIFACT_HINT: dict[str, str] = {
    "stale_open_loop": "task",
    "ce3_compression_candidate": "context_md_append",
    "ce3_cascade_rollup": "context_md_append",
    "doc_decay_candidate": "doc_patch",
    "regression_resurfaced": "task",
    "orphan_signal": "learning",
    "contradiction_repeat": "adr",
}


def _polish_active() -> bool:
    return bool(global_settings.brain_llm_polish_enabled)


def _safe_get_service() -> BrainLLMService | None:
    try:
        return get_brain_llm_service()
    except BrainLLMConfigError as exc:
        logger.warning("brain_polish_disabled_misconfig reason=%s", exc)
        return None
    except Exception:  # noqa: BLE001 — defensive, never break the read path
        logger.warning("brain_polish_service_unavailable", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Journal apply
# ---------------------------------------------------------------------------


def _journal_allowed_refs(entry: JournalEntry) -> list[str]:
    refs = [ref for ref in (entry.body.sources or []) if isinstance(ref, str) and ref]
    # `what_changed`, `open_loops`, `notable_context` may contain `{"ref": "..."}`
    # objects — surface them as additional allowed refs.
    for collection in (
        entry.body.what_changed,
        entry.body.open_loops,
        entry.body.notable_context,
        entry.body.tomorrow_watch,
    ):
        for item in collection or []:
            if isinstance(item, dict):
                ref = item.get("ref") or item.get("evidence_ref")
                if isinstance(ref, str) and ref:
                    refs.append(ref)
    # Dedup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _journal_entries_for_prompt(entry: JournalEntry) -> list[dict[str, Any]]:
    body = entry.body.model_dump(mode="python")
    # Keep only the section labels that contain narrative-worthy data.
    keep = ("what_changed", "decisions_observed", "open_loops", "notable_context")
    flat: list[dict[str, Any]] = []
    for label in keep:
        items = body.get(label) or []
        if not items:
            continue
        flat.append({"section": label, "items": items})
    return flat


def _journal_deterministic_narrative(entry: JournalEntry) -> str:
    decisions = entry.body.decisions_observed or []
    if decisions:
        return " ".join(decisions[:3])
    open_loops = entry.body.open_loops or []
    if open_loops:
        # Stitch a stub from the first open loop title if present.
        first = open_loops[0]
        if isinstance(first, dict):
            title = first.get("title") or first.get("summary")
            if isinstance(title, str) and title:
                return title
    return ""


def apply_polish_to_journal(items: list[JournalEntry]) -> list[JournalEntry]:
    if not _polish_active() or not global_settings.brain_llm_journal_polish_enabled:
        return items
    service = _safe_get_service()
    if service is None:
        return items

    cache = get_polish_cache()
    out: list[JournalEntry] = []
    for entry in items:
        cache_key = polish_cache_key("journal", entry.run_id, entry.entry_id)
        cached = cache.get(cache_key)
        if cached is not None and cached.success:
            polished = cached.polished.get("narrative_polished") or None
            out.append(
                entry.model_copy(
                    update={
                        "narrative_polished": polished,
                        "cited_evidence_refs": list(cached.cited_evidence_refs),
                        "polish_model": cached.model,
                    }
                )
            )
            continue
        if cached is None:
            _schedule_journal_polish(
                service=service,
                cache=cache,
                entry=entry,
            )
        out.append(entry)
    return out


def _schedule_journal_polish(
    *,
    service: BrainLLMService,
    cache: IdempotencyCache,
    entry: JournalEntry,
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        _background_polish_journal(service=service, cache=cache, entry=entry)
    )


async def _background_polish_journal(
    *,
    service: BrainLLMService,
    cache: IdempotencyCache,
    entry: JournalEntry,
) -> None:
    try:
        # Sleep-before-write nano-pattern (carry-forward sub-01 §11 invariant).
        await asyncio.sleep(0.1)
        allowed_refs = _journal_allowed_refs(entry)
        if not allowed_refs:
            return
        result = await polish_journal_entry(
            service=service,
            grounding_strict=global_settings.brain_llm_grounding_strict,
            run_id=entry.run_id,
            entry_id=entry.entry_id,
            scope_type=str(entry.scope_type),
            scope_key=entry.scope_key,
            cycle_key=entry.cycle_key,
            entries=_journal_entries_for_prompt(entry),
            allowed_evidence_refs=allowed_refs,
            deterministic_narrative=_journal_deterministic_narrative(entry),
        )
        _cache_on_success(cache, polish_cache_key("journal", entry.run_id, entry.entry_id), result)
    except Exception:  # noqa: BLE001 — never break the request loop
        logger.warning("brain_polish_background_journal_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Finding apply
# ---------------------------------------------------------------------------


def _finding_mapping_table(finding: Finding) -> dict[str, Any]:
    suggested = finding.suggested_artifact
    suggested_dump = suggested.model_dump(mode="python")
    return {
        "finding_type": finding.finding_type,
        "deterministic_artifact_hint": FINDING_TYPE_TO_ARTIFACT_HINT.get(
            str(finding.finding_type), suggested_dump.get("target_type", "none")
        ),
        "suggested_artifact": suggested_dump,
    }


def _finding_evidence_payload(finding: Finding) -> list[dict[str, Any]]:
    return [{"ref": ref} for ref in finding.evidence]


def apply_polish_to_findings(
    items: list[Finding | FindingRedacted],
) -> list[Finding | FindingRedacted]:
    if not _polish_active():
        return items
    summary_on = global_settings.brain_llm_finding_summary_enabled
    reasoning_on = global_settings.brain_llm_finding_reasoning_enabled
    if not (summary_on or reasoning_on):
        return items

    service = _safe_get_service()
    if service is None:
        return items

    cache = get_polish_cache()
    out: list[Finding | FindingRedacted] = []
    for item in items:
        if not isinstance(item, Finding):
            out.append(item)
            continue
        out.append(
            _apply_finding_polish(
                finding=item,
                service=service,
                cache=cache,
                summary_on=summary_on,
                reasoning_on=reasoning_on,
            )
        )
    return out


def apply_polish_to_finding(item: Finding) -> Finding:
    """Single-finding variant for `GET /findings/{id}`."""
    if not _polish_active():
        return item
    summary_on = global_settings.brain_llm_finding_summary_enabled
    reasoning_on = global_settings.brain_llm_finding_reasoning_enabled
    if not (summary_on or reasoning_on):
        return item
    service = _safe_get_service()
    if service is None:
        return item
    return _apply_finding_polish(
        finding=item,
        service=service,
        cache=get_polish_cache(),
        summary_on=summary_on,
        reasoning_on=reasoning_on,
    )


def _apply_finding_polish(
    *,
    finding: Finding,
    service: BrainLLMService,
    cache: IdempotencyCache,
    summary_on: bool,
    reasoning_on: bool,
) -> Finding:
    update: dict[str, Any] = {}
    cited: list[str] = []
    model_used = ""

    if summary_on:
        summary_key = polish_cache_key("finding_summary", finding.run_id, finding.finding_id)
        cached = cache.get(summary_key)
        if cached is not None and cached.success:
            summary_polished = cached.polished.get("summary_polished")
            why_now_polished = cached.polished.get("why_now_polished")
            if summary_polished:
                update["summary_polished"] = summary_polished
            if why_now_polished:
                update["why_now_polished"] = why_now_polished
            cited.extend(cached.cited_evidence_refs)
            model_used = cached.model or model_used
        elif cached is None:
            _schedule_finding_polish(
                service=service,
                cache=cache,
                finding=finding,
                purpose="finding_summary",
            )

    if reasoning_on:
        reasoning_key = polish_cache_key(
            "finding_reasoning", finding.run_id, finding.finding_id
        )
        cached = cache.get(reasoning_key)
        if cached is not None and cached.success:
            reasoning_polished = cached.polished.get("reasoning_polished")
            if reasoning_polished:
                update["reasoning_polished"] = reasoning_polished
            cited.extend(cached.cited_evidence_refs)
            model_used = cached.model or model_used
        elif cached is None:
            _schedule_finding_polish(
                service=service,
                cache=cache,
                finding=finding,
                purpose="finding_reasoning",
            )

    if not update:
        return finding

    # Dedup cited refs preserving order.
    seen: set[str] = set()
    dedup_cited: list[str] = []
    for ref in cited:
        if ref not in seen:
            seen.add(ref)
            dedup_cited.append(ref)
    update["cited_evidence_refs"] = dedup_cited
    if model_used:
        update["polish_model"] = model_used
    return finding.model_copy(update=update)


def _schedule_finding_polish(
    *,
    service: BrainLLMService,
    cache: IdempotencyCache,
    finding: Finding,
    purpose: str,
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        _background_polish_finding(
            service=service, cache=cache, finding=finding, purpose=purpose
        )
    )


async def _background_polish_finding(
    *,
    service: BrainLLMService,
    cache: IdempotencyCache,
    finding: Finding,
    purpose: str,
) -> None:
    try:
        await asyncio.sleep(0.1)
        finding_payload = {
            "finding_type": str(finding.finding_type),
            "title": finding.title,
            "scope_type": str(finding.scope_type),
            "scope_key": finding.scope_key,
            "severity": str(finding.severity),
            "confidence": str(finding.confidence),
            "recurrence_count": finding.recurrence_count,
        }
        evidence_payload = _finding_evidence_payload(finding)
        if purpose == "finding_summary":
            result = await polish_finding_summary(
                service=service,
                grounding_strict=global_settings.brain_llm_grounding_strict,
                run_id=finding.run_id,
                finding_id=finding.finding_id,
                finding=finding_payload,
                evidence=evidence_payload,
                allowed_evidence_refs=list(finding.evidence),
                deterministic_summary=finding.summary,
                deterministic_why_now=finding.why_now,
            )
            cache_key = polish_cache_key(
                "finding_summary", finding.run_id, finding.finding_id
            )
        else:
            result = await polish_finding_reasoning(
                service=service,
                grounding_strict=global_settings.brain_llm_grounding_strict,
                run_id=finding.run_id,
                finding_id=finding.finding_id,
                finding=finding_payload,
                mapping_table=_finding_mapping_table(finding),
                evidence=evidence_payload,
                allowed_evidence_refs=list(finding.evidence),
                deterministic_reasoning="",
            )
            cache_key = polish_cache_key(
                "finding_reasoning", finding.run_id, finding.finding_id
            )
        _cache_on_success(cache, cache_key, result)
    except Exception:  # noqa: BLE001 — never break the request loop
        logger.warning(
            "brain_polish_background_finding_failed purpose=%s",
            purpose,
            exc_info=True,
        )


def _cache_on_success(
    cache: IdempotencyCache, cache_key: str, result: PolishResult
) -> None:
    if result.success:
        cache.set(cache_key, result)
