"""Auto-approve fast-lane rules for Universal Ingestion."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.api.services import pii_redactor

AUTO_APPROVE_MAX_BYTES = 1 * 1024 * 1024
AUTO_APPROVE_SAFE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
AUTO_APPROVE_OCR_BACKENDS = frozenset({"tesseract", "tier_ocr"})

# Intrinsic pipeline outcomes that mean "would auto-insert without a human"
# (image fast-lane -> 'done'; LLM routing -> 'approved' -> saga).
_AUTO_INSERT_STATUSES = frozenset({"approved", "done"})


@dataclass(frozen=True)
class IngressRouting:
    """Final routing for an api_ingress row after the per-source policy gate.

    `decision` is a human-/audit-readable label recorded in classification_json
    (the triage_decision_id stays the pipeline's intrinsic basis so the saga
    scheduler, which keys on it, is untouched).
    """

    status: str
    triage_decision_id: str | None
    auto_insert: bool
    decision: str


def decide_ingress_routing(
    *,
    source_kind: str | None,
    ingest_policy: str | None,
    intrinsic_status: str,
    intrinsic_basis: str | None,
) -> IngressRouting | None:
    """Per-source policy gate (U3). The single authority for api_ingress routing.

    Returns ``None`` for non-api_ingress rows — owner surfaces (file_drop /
    manual_upload / terminal_upload / api_upload) are policy-exempt and keep
    their intrinsic decision untouched. Branch on source_kind FIRST (A5).

    For api_ingress the KEY-BOUND policy decides auto-insert eligibility, layered
    on top of the existing confidence gate:
      - policy is the OUTER gate (``trusted`` is necessary-not-sufficient);
      - the intrinsic confidence gate is the INNER gate.
    Default-deny: any policy that is not exactly ``trusted`` NEVER auto-inserts,
    regardless of confidence (D9, learning 89161faf). insert_saga only asserts
    this decision; it never re-decides.
    """
    if source_kind != "api_ingress":
        return None

    intrinsic_auto_insert = (
        intrinsic_status in _AUTO_INSERT_STATUSES
        and bool(intrinsic_basis)
        and intrinsic_basis.startswith("auto_approve:")
    )

    if ingest_policy == "trusted":
        if intrinsic_auto_insert:
            # Permit: keep the intrinsic target status AND basis so the existing
            # saga/project-switch machinery (which string-matches the basis) runs
            # unchanged. The trusted decision is recorded in `decision`.
            return IngressRouting(
                status=intrinsic_status,
                triage_decision_id=intrinsic_basis,
                auto_insert=True,
                decision="auto_insert:trusted_conf",
            )
        return IngressRouting(
            status="awaiting_triage",
            triage_decision_id="policy_gate:trusted_below_gate",
            auto_insert=False,
            decision="triage:trusted_below_gate",
        )

    # 'open' or anything unknown/NULL -> default-deny: force triage.
    if ingest_policy == "open":
        return IngressRouting(
            status="awaiting_triage",
            triage_decision_id="policy_gate:open",
            auto_insert=False,
            decision="triage:policy_open",
        )
    return IngressRouting(
        status="awaiting_triage",
        triage_decision_id="policy_gate:default_deny",
        auto_insert=False,
        decision="triage:default_deny",
    )


def should_auto_approve(item: Mapping[str, Any], extracted: Mapping[str, Any]) -> bool:
    """Return true only for small safe images with OCR and no detected PII."""
    suffix = Path(str(item.get("file_path") or "")).suffix.lower()
    if suffix not in AUTO_APPROVE_SAFE_SUFFIXES:
        return False

    file_size = item.get("file_size_bytes")
    if not isinstance(file_size, int) or file_size >= AUTO_APPROVE_MAX_BYTES:
        return False

    if extracted.get("exif_redacted") is not True:
        return False

    parser_used = str(extracted.get("parser_used") or "")
    if parser_used not in AUTO_APPROVE_OCR_BACKENDS:
        return False

    text = str(extracted.get("extracted_text") or extracted.get("text") or "")
    return not _contains_pii(text)


def _contains_pii(text: str) -> bool:
    return bool(pii_redactor.analyze(text))
