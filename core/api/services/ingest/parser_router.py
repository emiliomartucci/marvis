"""Parser dispatch for ingest_pending rows."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from core.api.config import settings
from core.api.db import acquire_db, acquire_write_db
from core.api.services import pii_redactor, project_lifecycle
from core.api.services.ingest.auto_approve import (
    decide_ingress_routing,
    should_auto_approve,
)
from core.api.services.ingest.classifier import ALLOWED_TARGETS, classify_markdown
from core.api.services.ingest.confidence import (
    compute_composite_confidence,
    estimate_parser_quality,
)
from core.api.services.ingest.events import broadcast_ingest_changed
from core.api.services.ingest.parsers.docling_parser import parse_pdf_file
from core.api.services.ingest.parsers.docparse_gateway import parse_pdf_docparse
from core.api.services.ingest.parsers.docx_parser import DOCX_MIME_TYPE, parse_docx
from core.api.services.ingest.parsers.gateway_aux import MissingGatewayConfig
from core.api.services.ingest.parsers.image_parser import (
    SUPPORTED_IMAGE_SUFFIXES,
    UNSUPPORTED_PHASE1_SUFFIXES,
    parse_image_with_gateway,
)
from core.api.services.ingest.parsers.internal_markdown import parse_markdown_file
from core.api.services.ingest.parsers.ocr_pdf_parser import parse_pdf_ocr
from core.api.services.ingest.parsers.transcript_parser import (
    AUDIO_MIME_BY_SUFFIX,
    SUPPORTED_AUDIO_SUFFIXES,
    SUPPORTED_VIDEO_SUFFIXES,
    VIDEO_MIME_BY_SUFFIX,
    parse_media_transcript,
)
from core.api.services.ingest.parsers.vision_gateway import parse_vision_with_gateway
from core.api.services.ingest.parsers.xlsx_parser import parse_xlsx
from core.api.services.ingest.preflight import build_classifier_content, build_preflight
from core.api.services.ingest.routing_policy import IngestRoute, choose_route
from core.api.services.ingest.xlsx_privacy import (
    neutral_xlsx_filename,
    neutral_xlsx_title,
    xlsx_sha256,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")

try:
    import magic

    _MAGIC = magic.Magic(mime=True)
except Exception:  # pragma: no cover - python-magic/libmagic may be absent in old envs
    _MAGIC = None
_MARKDOWN_SUFFIXES = {".md", ".markdown", ".txt"}
_PDF_SUFFIXES = {".pdf"}
# Inlined to avoid circular import with api.services.ingest.watcher (which imports
# parse_pending from this module). Same constant lives in watcher.py:28 + insert_saga.py:22.
PROJECTS_ROOT = Path("/data/projects")
_XLSX_MIME_BY_SUFFIX = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}
_DOCX_SUFFIXES = {".docx"}
_IMAGE_MIME_BY_SUFFIX = {
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_MEDIA_SUFFIXES = SUPPORTED_AUDIO_SUFFIXES | SUPPORTED_VIDEO_SUFFIXES


def detect_mime(path: Path) -> str:
    if path.suffix.lower() in _MARKDOWN_SUFFIXES:
        return "text/markdown"
    if path.suffix.lower() in _PDF_SUFFIXES:
        return "application/pdf"
    if path.suffix.lower() in _XLSX_MIME_BY_SUFFIX:
        return _XLSX_MIME_BY_SUFFIX[path.suffix.lower()]
    if path.suffix.lower() in _DOCX_SUFFIXES:
        return DOCX_MIME_TYPE
    if path.suffix.lower() in _IMAGE_MIME_BY_SUFFIX:
        return _IMAGE_MIME_BY_SUFFIX[path.suffix.lower()]
    if path.suffix.lower() in AUDIO_MIME_BY_SUFFIX:
        return AUDIO_MIME_BY_SUFFIX[path.suffix.lower()]
    if path.suffix.lower() in VIDEO_MIME_BY_SUFFIX:
        return VIDEO_MIME_BY_SUFFIX[path.suffix.lower()]
    if _MAGIC is not None:
        try:
            return str(_MAGIC.from_file(str(path)))
        except Exception:
            logger.exception("python-magic failed for %s", path)
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _classification_filename(path: Path, mime_type: str) -> str:
    if mime_type == "application/pdf":
        return f"{path.stem}.md"
    if path.suffix.lower() in _MEDIA_SUFFIXES or mime_type.startswith(
        ("audio/", "video/")
    ):
        return f"{path.stem}.md"
    return path.name


def _is_image(path: Path, mime_type: str) -> bool:
    suffix = path.suffix.lower()
    return (
        suffix in SUPPORTED_IMAGE_SUFFIXES
        or suffix in UNSUPPORTED_PHASE1_SUFFIXES
        or mime_type.startswith("image/")
    )


def _is_xlsx(path: Path, mime_type: str) -> bool:
    return path.suffix.lower() in _XLSX_MIME_BY_SUFFIX or mime_type in set(
        _XLSX_MIME_BY_SUFFIX.values()
    )


def _is_docx(path: Path, mime_type: str) -> bool:
    return path.suffix.lower() in _DOCX_SUFFIXES or mime_type == DOCX_MIME_TYPE


def _is_transcript_media(path: Path, mime_type: str) -> bool:
    return path.suffix.lower() in _MEDIA_SUFFIXES or mime_type.startswith(
        ("audio/", "video/")
    )


def _metadata_sidecar_path(path: Path) -> Path:
    return path.with_suffix(".metadata.json")


def _image_classification(
    *,
    path: Path,
    auto_approve: bool,
    reason: str,
    rules_matched: list[str],
) -> dict[str, Any]:
    return {
        "type": "file",
        "title": path.name,
        "tags": ["image", path.suffix.lower().lstrip(".")],
        "target_folder": "docs/assets",
        "target_filename": path.name,
        "confidence": 0.82 if auto_approve else 0.52,
        "reason": reason,
        "auto_approve": auto_approve,
        "rules_matched": rules_matched,
    }


def _xlsx_classification(
    path: Path,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    structure = (parsed or {}).get("structure") or {}
    sha256 = str(structure.get("sha256") or xlsx_sha256(path))
    return {
        "type": "file",
        "title": neutral_xlsx_title(sha256),
        "tags": ["xlsx", "spreadsheet"],
        "target_folder": "docs/assets",
        "target_filename": neutral_xlsx_filename(sha256),
        "confidence": 0.74,
        "reason": "spreadsheet requires manual triage",
        "auto_approve": False,
    }


LLM_AUTO_APPROVE_THRESHOLD = 0.80
IDENTITY_AUTO_APPROVE_TERMS = (
    "atto di nascita",
    "carta d'ident",
    "carta ident",
    "codice fiscale",
    "cognome e nome",
    "documento d'ident",
    "documento ident",
    "fiscal code",
    "name and surname",
    "passport",
    "passaporto",
)
_PARSER_LANE_LIMITS = {
    "local": max(
        1,
        int(
            settings.ingest_local_parser_max_concurrency
            or settings.ingest_parser_max_concurrency
        ),
    ),
    "ocr": max(1, int(settings.ingest_ocr_max_concurrency)),
    "docparse": max(1, int(settings.ingest_docparse_max_concurrency)),
    "transcribe": max(1, int(settings.ingest_transcribe_max_concurrency)),
    "vision": max(1, int(settings.ingest_vision_max_concurrency)),
}
_PARSER_LANE_SEMAPHORES = {
    lane: asyncio.Semaphore(limit) for lane, limit in _PARSER_LANE_LIMITS.items()
}
_TRANSIENT_PARSE_ERROR_MARKERS = (
    "rate limited",
    "unavailable after",
    "unavailable: http 408",
    "unavailable: http 409",
    "unavailable: http 425",
    "unavailable: http 429",
    "unavailable: http 500",
    "unavailable: http 502",
    "unavailable: http 503",
    "unavailable: http 504",
)
_OCR_EMPTY_TEXT_MARKER = "tier-ocr returned empty text"


@dataclass(frozen=True)
class ParseDispatchResult:
    parsed: Any
    parser_used: str
    route: IngestRoute
    preflight: dict[str, Any]
    parser_quality: dict[str, Any]


class _ParserWaitCancelled(Exception):
    """Raised when a row leaves parser_waiting before the parser slot starts."""


def _parser_lane_for_workflow(workflow: str) -> str:
    if workflow in {"ocr", "docparse", "transcribe", "vision"}:
        return workflow
    return "local"


def _parser_lane_waiter_count(lane: str) -> int:
    waiters = getattr(_PARSER_LANE_SEMAPHORES[lane], "_waiters", None)
    if waiters is None:
        return 0
    return sum(1 for waiter in waiters if not waiter.done())


async def _mark_parser_waiting(
    ingest_id: str,
    workspace_id: str,
    project_slug: str,
) -> None:
    async with acquire_write_db() as db:
        await db.execute(
            """
            UPDATE ingest_pending
               SET status = 'parser_waiting',
                   error_message = NULL,
                   updated_at = datetime('now')
             WHERE id = ?
               AND workspace_id = ?
               AND status IN ('queued', 'parse_error', 'parsing')
            """,
            (ingest_id, workspace_id),
        )
        await db.commit()
    await broadcast_ingest_changed(
        "parser_waiting",
        workspace_id=workspace_id,
        ingest_id=ingest_id,
        project_slug=project_slug,
        status="parser_waiting",
    )


async def _mark_parser_active(
    ingest_id: str,
    workspace_id: str,
    project_slug: str,
) -> None:
    async with acquire_write_db() as db:
        cursor = await db.execute(
            """
            UPDATE ingest_pending
               SET status = 'parsing',
                   updated_at = datetime('now')
             WHERE id = ?
               AND workspace_id = ?
               AND status = 'parser_waiting'
            """,
            (ingest_id, workspace_id),
        )
        await db.commit()
    if cursor.rowcount != 1:
        raise _ParserWaitCancelled(ingest_id)
    await broadcast_ingest_changed(
        "parsing",
        workspace_id=workspace_id,
        ingest_id=ingest_id,
        project_slug=project_slug,
        status="parsing",
    )


async def _mark_parse_error(
    ingest_id: str,
    workspace_id: str,
    project_slug: str,
    message: str,
    *,
    attempts: int = 3,
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            async with acquire_write_db(label="ingest.parse_error") as db:
                await db.execute(
                    """
                    UPDATE ingest_pending
                       SET status = 'parse_error',
                           error_message = ?,
                           updated_at = datetime('now')
                     WHERE id = ?
                       AND workspace_id = ?
                    """,
                    (message[:1000], ingest_id, workspace_id),
                )
                await db.commit()
            await broadcast_ingest_changed(
                "parse_error",
                workspace_id=workspace_id,
                ingest_id=ingest_id,
                project_slug=project_slug,
                status="parse_error",
            )
            return
        except (sqlite3.OperationalError, RuntimeError) as exc:
            if attempt >= attempts:
                logger.exception(
                    "ingest parse_error write failed permanently: id=%s",
                    ingest_id,
                )
                return
            delay = min(0.2 * (2 ** (attempt - 1)), 1.0)
            logger.warning(
                "ingest parse_error write failed; retrying attempt=%d/%d id=%s error=%s",
                attempt,
                attempts,
                ingest_id,
                exc,
            )
            await asyncio.sleep(delay)


async def _run_heavy_parser(
    *,
    ingest_id: str,
    workspace_id: str,
    project_slug: str,
    lane: str,
    parser_name: str,
    path: Path,
    parse: Callable[[], Awaitable[T]],
) -> T:
    semaphore = _PARSER_LANE_SEMAPHORES[lane]
    limit = _PARSER_LANE_LIMITS[lane]
    waiters = _parser_lane_waiter_count(lane)
    if semaphore.locked() or waiters:
        logger.info(
            "ingest parser waiting: lane=%s parser=%s path=%s max_concurrency=%d waiters=%d",
            lane,
            parser_name,
            path,
            limit,
            waiters,
        )

    async with semaphore:
        await _mark_parser_active(ingest_id, workspace_id, project_slug)
        logger.info(
            "ingest parser acquired: lane=%s parser=%s path=%s max_concurrency=%d waiters=%d",
            lane,
            parser_name,
            path,
            limit,
            _parser_lane_waiter_count(lane),
        )
        try:
            return await parse()
        finally:
            logger.info(
                "ingest parser released: lane=%s parser=%s path=%s max_concurrency=%d waiters=%d",
                lane,
                parser_name,
                path,
                limit,
                _parser_lane_waiter_count(lane),
            )


def _llm_classifier_enabled() -> bool:
    """Read LLM_CLASSIFIER_ENABLED env (true|shadow|false). Default: false."""
    value = (os.environ.get("LLM_CLASSIFIER_ENABLED", "false") or "").strip().lower()
    return value == "true"


def _llm_classifier_shadow() -> bool:
    value = (os.environ.get("LLM_CLASSIFIER_ENABLED", "false") or "").strip().lower()
    return value == "shadow"


def _ingest_llm_provider() -> str:
    return settings.ingest_llm_provider.strip().lower()


def _ingest_llm_model() -> str:
    return settings.ingest_llm_classifier_model


async def _resolve_classify_provider(workspace_id: str):
    """Resolve the BYOK 'classify' provider from llm_function_config, or None.

    Reads on the read pool, fail-soft (None on any error) so the deterministic
    path is never broken by a config/DB hiccup.
    """
    try:
        from core.api.services.ingest.llm.config_store import resolve_function_provider

        async with acquire_db() as cfg_db:
            return await resolve_function_provider(
                cfg_db, "classify", workspace_id
            )
    except Exception:  # noqa: BLE001
        logger.debug("byok classify provider resolution failed", exc_info=True)
        return None


def _with_llm_no_result(
    base_classification: dict[str, Any],
    *,
    status: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(base_classification)
    metadata = dict(merged.get("llm_metadata") or {})
    metadata.update(
        {
            "status": status,
            "model": _ingest_llm_model(),
            "provider": _ingest_llm_provider(),
            "auto_approved": False,
            "reason": reason,
        }
    )
    if extra:
        metadata.update(extra)
    merged["llm_metadata"] = metadata
    return merged


_LLM_HARD_FAILURE_STATUSES = {
    "api_error",
    "bad_response",
    "client_init_failed",
    "exception",
    "factory_failed",
    "json_parse_failed",
    "no_result",
    "unavailable",
}


def _llm_failure_error_message(classification_json: dict[str, Any]) -> str | None:
    metadata = classification_json.get("llm_metadata")
    if not isinstance(metadata, dict):
        return None
    status = str(metadata.get("status") or "")
    if status not in _LLM_HARD_FAILURE_STATUSES:
        return None
    reason = str(metadata.get("reason") or "llm_classifier_failed")
    message = str(
        metadata.get("gateway_error_message")
        or metadata.get("error_message")
        or reason
    )
    return f"E5 LLM enrichment failed after retries: {status} ({message})"[:1000]


async def _maybe_summarize_transcript(
    *,
    ingest_id: str,
    parser_used: str,
    extracted_text: str,
    structure: dict[str, Any],
) -> dict[str, Any] | None:
    if parser_used != "tier_transcribe" or not extracted_text.strip():
        return None
    if not _llm_classifier_enabled():
        return None

    try:
        from core.api.services.ingest.llm.local_gateway import (
            summarize_transcript_with_local_gateway,
        )
    except Exception:  # noqa: BLE001
        logger.debug("transcript summarizer import failed", exc_info=True)
        return {
            "status": "import_failed",
            "reason": "transcript_summarizer_import_failed",
        }

    try:
        result, diagnostics = await summarize_transcript_with_local_gateway(
            extracted_text,
            structure=structure,
            idempotency_scope=f"ingest:{ingest_id}",
        )
    except Exception:  # noqa: BLE001 - summary must never break ingest
        logger.warning("transcript summarizer raised", exc_info=True)
        return {
            "status": "exception",
            "reason": "transcript_summarizer_exception",
        }

    if result is None:
        return _transcript_summary_failure_metadata(diagnostics)

    return {
        "status": "ok",
        "model": _ingest_llm_model(),
        "provider": _ingest_llm_provider(),
        "summary": result.summary,
        "topics": list(result.topics),
        "participants": list(result.participants),
        "keywords": list(result.keywords),
        "action_items": list(result.action_items),
        "confidence": result.confidence,
    }


def _transcript_summary_failure_metadata(
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata = {
        "status": "no_result",
        "model": _ingest_llm_model(),
        "provider": _ingest_llm_provider(),
        "reason": "transcript_summarizer_returned_none",
    }
    if isinstance(diagnostics, dict):
        for key in (
            "status",
            "reason",
            "gateway_status_code",
            "gateway_error_code",
            "gateway_error_message",
            "schema_retry_attempted",
            "raw_excerpt",
            "first_raw_excerpt",
        ):
            if key in diagnostics:
                metadata[key] = diagnostics[key]
    return metadata


def _ingest_event_for_status(status: str) -> str:
    if status == "done":
        return "done"
    if status == "rejected":
        return "rejected"
    if status == "parse_error":
        return "parse_error"
    return "parsed"


async def _maybe_llm_enrich(
    *,
    workspace_id: str,
    ingest_id: str | None = None,
    extracted_text: str,
    base_classification: dict[str, Any],
    preflight: dict[str, Any],
    parser_quality: dict[str, Any],
    route: IngestRoute,
) -> dict[str, Any] | None:
    """Run the local/cloud LLM classifier on a bounded evidence packet.

    Returns the enriched classification dict (with auto_approve, llm_metadata,
    suggested project_slug) only when the project_slug resolves to a valid
    project and the composite confidence gate passes.

    Reads context OUTSIDE the writer lock (M-D7).
    """
    enabled = _llm_classifier_enabled()
    shadow = _llm_classifier_shadow()
    # BYOK (U4): a DB-configured provider for the 'classify' function also enables
    # auto-classify (no env flag needed). The deterministic classifier stays
    # PRIMARY; this only gates the optional LLM override.
    workspace_id = workspace_id.strip()
    if not workspace_id:
        raise ValueError("workspace_id is required for LLM enrichment")
    resolved_provider = await _resolve_classify_provider(workspace_id)
    byok = resolved_provider is not None
    if not (enabled or shadow or byok):
        # No env flag and no configured BYOK provider: auto-classify is disabled.
        # The deterministic classification (already computed) remains and the item
        # routes to triage — no heuristic semantic guess (R10/D6).
        return None

    try:
        from core.api.services.ingest.llm.classification_context import (
            gather_classification_context,
        )
        from core.api.services.ingest.llm.factory import get_classifier
    except Exception:  # noqa: BLE001 - module unavailable in some test envs
        logger.debug("llm classifier import failed", exc_info=True)
        return None

    classifier_content = build_classifier_content(
        extracted_text=extracted_text,
        preflight=preflight,
        parser_quality=parser_quality,
    )

    try:
        async with acquire_db() as read_db:
            ctx = await gather_classification_context(
                classifier_content, read_db, workspace_id
            )
    except Exception:  # noqa: BLE001
        logger.warning("llm context gather failed", exc_info=True)
        ctx = {"projects": [], "similar_artifacts": [], "hotspots": []}
    if preflight.get("source_context"):
        ctx["source_context"] = preflight["source_context"]
    if ingest_id:
        ctx["_idempotency_scope"] = f"ingest:{ingest_id}"
    ctx["_workspace_id"] = workspace_id

    classifier = None
    if byok:
        try:
            from core.api.services.ingest.llm.byok_provider import build_classifier

            classifier = build_classifier(resolved_provider)
        except Exception:  # noqa: BLE001
            logger.warning("byok classifier build failed", exc_info=True)
            classifier = None
    if classifier is None and (enabled or shadow):
        # Legacy env-driven provider (OSS first-boot fallback).
        try:
            classifier = get_classifier()
        except Exception:  # noqa: BLE001
            logger.warning("llm classifier factory failed", exc_info=True)
            classifier = None
    if classifier is None:
        # Gate produced no usable provider: disabled, surfaced, never heuristic.
        return _with_llm_no_result(
            base_classification,
            status="disabled_no_provider",
            reason="no_llm_provider_configured",
        )

    try:
        llm_result = await classifier.classify(classifier_content, ctx)
    except Exception:  # noqa: BLE001 - defensive: classify() should never raise
        logger.warning("llm classify raised", exc_info=True)
        return _with_llm_no_result(
            base_classification,
            status="exception",
            reason="llm_classifier_exception",
        )

    if llm_result is None:
        diagnostics = getattr(classifier, "last_error", None)
        if isinstance(diagnostics, dict):
            return _with_llm_no_result(
                base_classification,
                status=str(diagnostics.get("status") or "no_result"),
                reason=str(diagnostics.get("reason") or "llm_classifier_returned_none"),
                extra=diagnostics,
            )
        return _with_llm_no_result(
            base_classification,
            status="no_result",
            reason="llm_classifier_returned_none",
        )

    # Validate project_slug actually exists on disk (avoid hallucinated slugs).
    valid_slug: str | None = None
    try:
        from core.api.services.ingest.insert_saga import _load_project_entry

        ptype, _repo = _load_project_entry(llm_result.project_slug)
        if ptype:
            from core.api.services.access_grants import (
                require_unique_project_workspace,
            )

            async with acquire_db() as ownership_db:
                await require_unique_project_workspace(
                    ownership_db,
                    project_slug=llm_result.project_slug,
                    workspace_id=workspace_id,
                    allow_local_single_user=True,
                )
            valid_slug = llm_result.project_slug
    except Exception:  # noqa: BLE001 - any failure -> reject
        valid_slug = None

    provider = _ingest_llm_provider()
    model = _ingest_llm_model()
    target_folder = ALLOWED_TARGETS.get(llm_result.document_type)
    confidence_decision = compute_composite_confidence(
        route=route,
        parser_quality=parser_quality,
        llm_confidence=float(llm_result.confidence),
        valid_project=valid_slug is not None,
        document_type=llm_result.document_type,
        extracted_text=extracted_text,
    )
    metadata = {
        "model": model,
        "provider": provider,
        "project_slug": llm_result.project_slug,
        "valid_slug": valid_slug,
        "document_type": llm_result.document_type,
        "title": llm_result.title,
        "tags": llm_result.tags,
        "llm_confidence": llm_result.confidence,
        "composite_confidence": confidence_decision.score,
        "confidence_gate": confidence_decision.as_json(),
        "reasoning": llm_result.reasoning,
        "pii_detected": bool(pii_redactor.analyze(extracted_text[:6000])),
    }
    source_context = preflight.get("source_context") or {}
    source_project_slug = source_context.get("project_slug")
    if source_project_slug:
        metadata.update(
            {
                "source_project_slug": source_project_slug,
                "source_project_prior": source_context.get("prior"),
                "source_project_reason": source_context.get("reason"),
                "source_project_followed": llm_result.project_slug == source_project_slug,
                "source_project_overridden": llm_result.project_slug != source_project_slug,
            }
        )

    # Shadow mode: log decision but never override the deterministic classifier.
    if shadow:
        shadow_blob = {
            "shadow_mode": True,
            "llm_metadata": metadata,
        }
        merged = dict(base_classification)
        merged.setdefault("llm_shadow", shadow_blob)
        return merged

    if valid_slug is None:
        logger.info(
            "llm classifier returned unknown slug=%s; falling back to deterministic",
            llm_result.project_slug,
        )
        return _with_llm_no_result(
            base_classification,
            status="invalid_project",
            reason="llm_classifier_invalid_project_slug",
            extra={
                "project_slug": llm_result.project_slug,
                "document_type": llm_result.document_type,
                "llm_confidence": llm_result.confidence,
                "reasoning": llm_result.reasoning,
            },
        )

    if target_folder is None:
        return _with_llm_no_result(
            base_classification,
            status="invalid_document_type",
            reason="llm_classifier_invalid_document_type",
            extra={
                "project_slug": llm_result.project_slug,
                "document_type": llm_result.document_type,
                "llm_confidence": llm_result.confidence,
                "reasoning": llm_result.reasoning,
            },
        )

    privacy_block_reason = _llm_auto_approve_privacy_block_reason(
        preflight=preflight,
        extracted_text=extracted_text,
        document_type=llm_result.document_type,
    )
    if privacy_block_reason:
        merged = dict(base_classification)
        merged["llm_metadata"] = {
            **metadata,
            "auto_approved": False,
            "auto_approve_blocked_reason": privacy_block_reason,
        }
        return merged

    if (
        llm_result.confidence < LLM_AUTO_APPROVE_THRESHOLD
        or not confidence_decision.auto_approve
    ):
        # Keep deterministic decision but surface the LLM hint so the human
        # triage UI can show it.
        merged = dict(base_classification)
        merged["llm_metadata"] = {**metadata, "auto_approved": False}
        return merged

    enriched = dict(base_classification)
    enriched.update(
        {
            "type": llm_result.document_type,
            "title": llm_result.title,
            "tags": list(llm_result.tags),
            "target_folder": target_folder,
            "confidence": confidence_decision.score,
            "reason": "llm_routing",
            "auto_approve": True,
            "suggested_project_slug": valid_slug,
            "llm_metadata": {
                **metadata,
                "project_slug": valid_slug,
                "auto_approved": True,
            },
        }
    )
    return enriched


def _llm_auto_approve_privacy_block_reason(
    *,
    preflight: dict[str, Any],
    extracted_text: str,
    document_type: str | None = None,
) -> str | None:
    packet = preflight or {}
    pf = packet.get("preflight") or {}
    file_info = packet.get("file") or {}
    filename_text = " ".join(
        str(file_info.get(key) or "") for key in ("filename", "stem")
    )
    if _contains_identity_signal(filename_text, minimum=1):
        return "identity_document_requires_manual_triage"
    if bool(pf.get("identity_hint")) and _contains_identity_signal(
        extracted_text, minimum=1
    ):
        return "identity_document_requires_manual_triage"
    if _contains_identity_signal(extracted_text, minimum=2):
        return "identity_document_requires_manual_triage"

    # E5 already redacts PII before sending the prompt to the local Gateway.
    # Generic business documents regularly contain email, phone, IBAN, VAT or
    # fiscal-code-like strings; those should not block auto-triage by
    # themselves. Keep the conservative fallback only when the LLM did not
    # return a document type.
    if document_type == "record" and pii_redactor.analyze(extracted_text[:6000]):
        return "sensitive_record_requires_manual_triage"
    if document_type is None and pii_redactor.analyze(extracted_text[:6000]):
        return "pii_requires_manual_triage"
    return None


def _contains_identity_signal(text: str, *, minimum: int = 1) -> bool:
    lowered = (text or "").lower()
    matches = sum(1 for term in IDENTITY_AUTO_APPROVE_TERMS if term in lowered)
    return matches >= minimum


def _gateway_aux_configured() -> bool:
    key = settings.ingest_llm_gateway_api_key
    key_value = (
        key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
    )
    return bool(
        settings.pir_env != "test"
        and key_value
        and (settings.llm_gateway_aux_base_url or settings.llm_gateway_base_url)
    )


def _route_for(path: Path, mime_type: str, preflight: dict[str, Any]) -> IngestRoute:
    gateway_enabled = _gateway_aux_configured()
    return choose_route(
        path=path,
        mime_type=mime_type,
        preflight=preflight,
        docparse_enabled=(
            gateway_enabled
            and settings.ingest_docparse_enabled
            and (
                settings.ingest_docparse_pdfs_enabled
                if mime_type == "application/pdf"
                else settings.ingest_docparse_images_enabled
            )
        ),
        ocr_enabled=gateway_enabled,
        vision_enabled=bool(gateway_enabled and settings.ingest_vision_images_enabled),
        mode_override=settings.ingest_docparse_mode_override or None,
    )


async def _maybe_llm_route(
    *,
    route: IngestRoute,
    preflight: dict[str, Any],
    mime_type: str,
) -> IngestRoute:
    if route.confidence >= 0.70 or not _llm_classifier_enabled():
        return route
    try:
        from core.api.services.ingest.llm.local_gateway import (
            classify_route_with_local_gateway,
        )
    except Exception:  # noqa: BLE001
        logger.debug("local route classifier import failed", exc_info=True)
        return route

    decision = await classify_route_with_local_gateway(
        preflight=preflight,
        deterministic_route=route.as_json(),
    )
    if decision is None:
        return route
    if decision.workflow not in _allowed_workflows_for_mime(mime_type):
        logger.info(
            "route classifier ignored invalid workflow=%s for mime=%s",
            decision.workflow,
            mime_type,
        )
        return route
    return replace(
        route,
        workflow=decision.workflow,
        tier=_tier_for_workflow(decision.workflow),
        mode=decision.mode if decision.workflow == "docparse" else None,
        reason=f"tier-fast route classifier: {decision.reason}",
        confidence=float(decision.confidence),
        features_used=[*route.features_used, "tier_fast_route_classifier"],
    )


def _allowed_workflows_for_mime(mime_type: str) -> set[str]:
    if mime_type.startswith(("audio/", "video/")):
        return {"transcribe"}
    if mime_type == "application/pdf":
        return {"local", "ocr", "docparse"}
    if mime_type.startswith("image/"):
        return {"ocr", "docparse", "vision"}
    return {"local"}


def _tier_for_workflow(workflow: str) -> str | None:
    return {
        "ocr": "tier-ocr",
        "docparse": "tier-docparse",
        "transcribe": "tier-transcribe",
        "vision": "tier-vision",
    }.get(workflow)


def _block_llm_auto_approve(
    classification_json: dict[str, Any],
    *,
    reason: str,
    existing_ingest_id: str | None = None,
) -> dict[str, Any]:
    blocked = dict(classification_json)
    blocked["auto_approve"] = False
    blocked["reason"] = reason
    metadata = dict(blocked.get("llm_metadata") or {})
    metadata["auto_approved"] = False
    metadata["auto_approve_blocked_reason"] = reason
    if existing_ingest_id:
        metadata["existing_ingest_id"] = existing_ingest_id
    blocked["llm_metadata"] = metadata
    return blocked


def _reject_llm_auto_approve_duplicate(
    classification_json: dict[str, Any],
    *,
    reason: str,
    existing_ingest_id: str,
) -> dict[str, Any]:
    rejected = _block_llm_auto_approve(
        classification_json,
        reason=reason,
        existing_ingest_id=existing_ingest_id,
    )
    rejected["auto_reject"] = True
    metadata = dict(rejected.get("llm_metadata") or {})
    metadata["auto_rejected"] = True
    metadata["auto_reject_reason"] = reason
    metadata["existing_ingest_id"] = existing_ingest_id
    rejected["llm_metadata"] = metadata
    return rejected


async def _find_ingest_duplicate_for_project(
    db: Any,
    *,
    ingest_id: str,
    workspace_id: str,
    sha256: str | None,
    project_slug: str,
) -> str | None:
    if not sha256:
        return None
    async with db.execute(
        """
        SELECT id
          FROM ingest_pending
         WHERE sha256 = ?
           AND project_slug = ?
           AND workspace_id = ?
           AND id != ?
         LIMIT 1
        """,
        (sha256, project_slug, workspace_id, ingest_id),
    ) as cursor:
        row = await cursor.fetchone()
    return str(row["id"]) if row is not None else None


async def _apply_llm_project_switch_if_safe(
    db: Any,
    *,
    ingest_id: str,
    workspace_id: str,
    sha256: str | None,
    current_project_slug: str,
    target_project_slug: str,
    path: Path,
    classification_json: dict[str, Any],
    lifecycle_lock_held: bool = False,
) -> tuple[str, Path, dict[str, Any], str, str | None]:
    new_root = PROJECTS_ROOT / target_project_slug
    if not new_root.is_dir():
        logger.warning(
            "llm_routing project switch aborted: %s not a project root",
            new_root,
        )
        return (
            current_project_slug,
            path,
            _block_llm_auto_approve(
                classification_json,
                reason="llm_routing_project_switch_invalid_project",
            ),
            "awaiting_triage",
            None,
        )

    duplicate_id = await _find_ingest_duplicate_for_project(
        db,
        ingest_id=ingest_id,
        workspace_id=workspace_id,
        sha256=sha256,
        project_slug=target_project_slug,
    )
    if duplicate_id is not None:
        logger.warning(
            "llm_routing project switch aborted: sha256 already exists in %s as %s",
            target_project_slug,
            duplicate_id,
        )
        return (
            current_project_slug,
            path,
            _reject_llm_auto_approve_duplicate(
                classification_json,
                reason="llm_routing_project_switch_dedup_collision",
                existing_ingest_id=duplicate_id,
            ),
            "rejected",
            "auto_reject:llm_routing_duplicate",
        )

    async with AsyncExitStack() as mutation_stack:
        if not lifecycle_lock_held:
            await mutation_stack.enter_async_context(
                project_lifecycle.async_project_mutation_guard(
                    projects_root=PROJECTS_ROOT
                )
            )
        await project_lifecycle.record_project_write(
            db,
            workspace_id=workspace_id,
            project_slug=current_project_slug,
            writer_kind="ingest_llm_project_switch_source",
            actor="pir-ingest",
            resource_ref=str(path),
            projects_root=PROJECTS_ROOT,
        )
        await project_lifecycle.record_project_write(
            db,
            workspace_id=workspace_id,
            project_slug=target_project_slug,
            writer_kind="ingest_llm_project_switch_target",
            actor="pir-ingest",
            resource_ref=path.name,
            projects_root=PROJECTS_ROOT,
        )
        await db.commit()

        new_input = new_root / "input"
        new_input.mkdir(parents=True, exist_ok=True)
        new_source = new_input / path.name
        source_sidecar = _metadata_sidecar_path(path)
        target_sidecar = _metadata_sidecar_path(new_source)
        if new_source.exists():
            logger.warning(
                "llm_routing project switch aborted: %s already exists",
                new_source,
            )
            return (
                current_project_slug,
                path,
                _block_llm_auto_approve(
                    classification_json,
                    reason="llm_routing_project_switch_path_collision",
                ),
                "awaiting_triage",
                None,
            )
        if source_sidecar.exists() and target_sidecar.exists():
            logger.warning(
                "llm_routing project switch aborted: %s already exists",
                target_sidecar,
            )
            return (
                current_project_slug,
                path,
                _block_llm_auto_approve(
                    classification_json,
                    reason="llm_routing_project_switch_sidecar_collision",
                ),
                "awaiting_triage",
                None,
            )

        path.replace(new_source)
        if source_sidecar.exists():
            source_sidecar.replace(target_sidecar)
    return (
        target_project_slug,
        new_source,
        classification_json,
        "approved",
        "auto_approve:llm_routing",
    )


async def _parse_pdf_local(path: Path):
    try:
        return await parse_pdf_file(path, allow_docparse=False)
    except TypeError as exc:
        if "allow_docparse" not in str(exc):
            raise
        return await parse_pdf_file(path)


def _transient_parse_max_attempts() -> int:
    raw = os.environ.get("INGEST_TRANSIENT_PARSE_MAX_ATTEMPTS", "3")
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _transient_parse_delay_seconds(attempt: int) -> float:
    if settings.pir_env == "test":
        return 0.0
    raw = os.environ.get("INGEST_TRANSIENT_PARSE_RETRY_BASE_SECONDS", "5")
    try:
        base = max(0.0, float(raw))
    except ValueError:
        base = 5.0
    return min(base * attempt, 30.0)


def _is_transient_parse_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_PARSE_ERROR_MARKERS)


def _is_empty_ocr_result(exc: BaseException) -> bool:
    return _OCR_EMPTY_TEXT_MARKER in str(exc).lower()


def _docparse_enabled_for_pdf() -> bool:
    return bool(
        _gateway_aux_configured()
        and settings.ingest_docparse_enabled
        and settings.ingest_docparse_pdfs_enabled
    )


def _docparse_fallback_mode(route: IngestRoute) -> str:
    return (
        route.mode
        or settings.ingest_docparse_mode_override
        or settings.ingest_docparse_mode
    )


def _ocr_to_docparse_route(route: IngestRoute) -> IngestRoute:
    return replace(
        route,
        workflow="docparse",
        tier="tier-docparse",
        mode=_docparse_fallback_mode(route),
        reason=f"{route.reason}; tier-ocr empty result fell back to docparse",
        confidence=max(route.confidence, 0.86),
        features_used=[*route.features_used, "ocr_empty_docparse_fallback"],
    )


async def _parse_file(
    ingest_id: str,
    workspace_id: str,
    project_slug: str,
    path: Path,
    mime_type: str,
    *,
    preflight: dict[str, Any],
    route: IngestRoute,
) -> ParseDispatchResult:
    if route.workflow == "skip":
        raise ValueError(route.reason)

    if mime_type == "application/pdf":
        effective_route = route

        async def parse_pdf_route():
            nonlocal effective_route
            if route.workflow == "docparse":
                try:
                    return await parse_pdf_docparse(path, mode=route.mode)
                except MissingGatewayConfig:
                    logger.warning(
                        "tier-docparse not configured; falling back to local PDF"
                    )
                    return await _parse_pdf_local(path)
            if route.workflow == "ocr":
                try:
                    return await parse_pdf_ocr(path)
                except MissingGatewayConfig:
                    logger.warning("tier-ocr not configured; falling back to local PDF")
                    return await _parse_pdf_local(path)
                except RuntimeError as exc:
                    if not _is_empty_ocr_result(exc) or not _docparse_enabled_for_pdf():
                        raise
                    effective_route = _ocr_to_docparse_route(route)
                    logger.warning(
                        "tier-ocr returned empty text; falling back to tier-docparse: path=%s mode=%s",
                        path,
                        effective_route.mode,
                    )
                    return await parse_pdf_docparse(path, mode=effective_route.mode)
            return await _parse_pdf_local(path)

        parsed = await _run_heavy_parser(
            ingest_id=ingest_id,
            workspace_id=workspace_id,
            project_slug=project_slug,
            lane=_parser_lane_for_workflow(route.workflow),
            parser_name=f"pdf:{route.workflow}",
            path=path,
            parse=parse_pdf_route,
        )
        parser_quality = estimate_parser_quality(
            parser_used=parsed.parser_used,
            extracted_text=parsed.text,
            structure=parsed.structure,
        )
        return ParseDispatchResult(
            parsed,
            parsed.parser_used,
            effective_route,
            preflight,
            parser_quality,
        )

    if _is_image(path, mime_type):
        if route.workflow == "vision":
            parsed = await _run_heavy_parser(
                ingest_id=ingest_id,
                workspace_id=workspace_id,
                project_slug=project_slug,
                lane="vision",
                parser_name="image:vision",
                path=path,
                parse=lambda: parse_vision_with_gateway(path, mime_type),
            )
            parser_quality = estimate_parser_quality(
                parser_used=str(parsed["parser_used"]),
                extracted_text=str(
                    parsed.get("text") or parsed.get("extracted_text") or ""
                ),
                structure=parsed.get("structure") or {},
            )
            return ParseDispatchResult(
                parsed,
                str(parsed["parser_used"]),
                route,
                preflight,
                parser_quality,
            )

        prefer_docparse = route.workflow == "docparse"
        parsed = await _run_heavy_parser(
            ingest_id=ingest_id,
            workspace_id=workspace_id,
            project_slug=project_slug,
            lane=_parser_lane_for_workflow(route.workflow),
            parser_name=f"image:{route.workflow}",
            path=path,
            parse=lambda: parse_image_with_gateway(
                path,
                mime_type,
                prefer_docparse=prefer_docparse,
                docparse_mode=route.mode,
            ),
        )
        parser_quality = estimate_parser_quality(
            parser_used=str(parsed["parser_used"]),
            extracted_text=str(
                parsed.get("text") or parsed.get("extracted_text") or ""
            ),
            structure=parsed.get("structure") or {},
        )
        return ParseDispatchResult(
            parsed,
            str(parsed["parser_used"]),
            route,
            preflight,
            parser_quality,
        )
    if _is_xlsx(path, mime_type):
        await _mark_parser_active(ingest_id, workspace_id, project_slug)
        parsed = await asyncio.to_thread(parse_xlsx, path)
        parser_quality = estimate_parser_quality(
            parser_used=str(parsed["parser_used"]),
            extracted_text=str(parsed.get("text") or ""),
            structure=parsed.get("structure") or {},
        )
        return ParseDispatchResult(
            parsed,
            str(parsed["parser_used"]),
            route,
            preflight,
            parser_quality,
        )
    if _is_docx(path, mime_type):
        await _mark_parser_active(ingest_id, workspace_id, project_slug)
        parsed = await asyncio.to_thread(parse_docx, path)
        parser_quality = estimate_parser_quality(
            parser_used="internal_docx",
            extracted_text=parsed.text,
            structure=parsed.structure,
        )
        return ParseDispatchResult(
            parsed, "internal_docx", route, preflight, parser_quality
        )
    if _is_transcript_media(path, mime_type):
        parsed = await _run_heavy_parser(
            ingest_id=ingest_id,
            workspace_id=workspace_id,
            project_slug=project_slug,
            lane="transcribe",
            parser_name="transcript",
            path=path,
            parse=lambda: parse_media_transcript(path, mime_type),
        )
        parser_quality = estimate_parser_quality(
            parser_used="tier_transcribe",
            extracted_text=parsed.text,
            structure=parsed.structure,
        )
        return ParseDispatchResult(
            parsed, "tier_transcribe", route, preflight, parser_quality
        )
    if path.suffix.lower() in _MARKDOWN_SUFFIXES or mime_type in {
        "text/markdown",
        "text/plain",
    }:
        await _mark_parser_active(ingest_id, workspace_id, project_slug)
        parsed = parse_markdown_file(path)
        parser_quality = estimate_parser_quality(
            parser_used="internal_markdown",
            extracted_text=parsed.text,
            structure=parsed.structure,
        )
        return ParseDispatchResult(
            parsed, "internal_markdown", route, preflight, parser_quality
        )
    raise ValueError(f"Unsupported phase-1 file type: {mime_type}")


async def _parse_file_with_transient_retries(
    ingest_id: str,
    workspace_id: str,
    project_slug: str,
    path: Path,
    mime_type: str,
    *,
    preflight: dict[str, Any],
    route: IngestRoute,
) -> ParseDispatchResult:
    attempts = _transient_parse_max_attempts()
    for attempt in range(1, attempts + 1):
        try:
            return await _parse_file(
                ingest_id,
                workspace_id,
                project_slug,
                path,
                mime_type,
                preflight=preflight,
                route=route,
            )
        except Exception as exc:
            if attempt >= attempts or not _is_transient_parse_error(exc):
                raise
            delay = _transient_parse_delay_seconds(attempt)
            await _mark_parser_waiting(ingest_id, workspace_id, project_slug)
            logger.warning(
                "ingest parser transient failure; retrying attempt=%d/%d delay=%.1fs route=%s path=%s error=%s",
                attempt,
                attempts,
                delay,
                route.workflow,
                path,
                exc,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("transient parse retry loop exited unexpectedly")


def _with_ingest_v2_diagnostics(
    structure: dict[str, Any],
    *,
    preflight: dict[str, Any],
    route: IngestRoute,
    parser_quality: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(structure or {})
    merged["ingest_v2"] = {
        "route": route.as_json(),
        "parser_quality": parser_quality,
        "preflight": _bounded_preflight_for_storage(preflight),
    }
    if preflight.get("source_context"):
        merged["ingest_v2"]["source_context"] = dict(preflight["source_context"])
    if (preflight.get("preflight") or {}).get("image_kind"):
        merged["ingest_v2"]["image_probe"] = {
            key: value
            for key, value in dict(preflight.get("preflight") or {}).items()
            if key
            in {
                "image_kind",
                "document_likelihood",
                "screenshot_likelihood",
                "photo_likelihood",
                "text_likelihood",
                "signals",
                "white_background_ratio",
                "edge_density",
                "brightness",
                "contrast",
            }
        }
    return merged


def _bounded_preflight_for_storage(preflight: dict[str, Any]) -> dict[str, Any]:
    stored = {
        "file": dict(preflight.get("file") or {}),
        "preflight": dict(preflight.get("preflight") or {}),
        "content_sample": dict(preflight.get("content_sample") or {}),
    }
    sample = stored["content_sample"]
    for key in ("first_excerpt", "middle_excerpt", "last_excerpt", "parser_excerpt"):
        if key in sample:
            sample[key] = str(sample[key])[:500]
    return stored


def _xlsx_preflight_for_storage(
    preflight: dict[str, Any],
    *,
    sha256: str,
) -> dict[str, Any]:
    """Replace source-derived XLSX labels before diagnostics are persisted."""
    stored = dict(preflight or {})
    neutral_filename = neutral_xlsx_filename(sha256)
    file_info = dict(stored.get("file") or {})
    file_info["filename"] = neutral_filename
    file_info["stem"] = Path(neutral_filename).stem
    stored["file"] = file_info
    return stored


def _reconcile_ingress_metadata(
    structure: dict[str, Any] | None, ingress_metadata_raw: str | None
) -> dict[str, Any] | None:
    """Merge an api_ingress row's payload metadata into structure_json (U3).

    Stored under the ``ingress_metadata`` key so one canonical place carries it
    downstream (Triage view + KG). No-op when there is no metadata or it is not
    a JSON object.
    """
    if not ingress_metadata_raw:
        return structure
    try:
        parsed = json.loads(ingress_metadata_raw)
    except (json.JSONDecodeError, TypeError):
        return structure
    if not isinstance(parsed, dict) or not parsed:
        return structure
    return {**(structure or {}), "ingress_metadata": parsed}


async def parse_pending(ingest_id: str, workspace_id: str) -> None:
    workspace_id = workspace_id.strip()
    if not workspace_id:
        raise ValueError("workspace_id is required for ingest parsing")
    row = None
    async with acquire_write_db() as db:
        async with db.execute(
            "SELECT * FROM ingest_pending WHERE workspace_id = ? AND id = ?",
            (workspace_id, ingest_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return
        if row["status"] not in {"queued", "parse_error"}:
            return
        from core.api.services.access_grants import require_unique_project_workspace

        await require_unique_project_workspace(
            db,
            project_slug=str(row["project_slug"] or ""),
            workspace_id=workspace_id,
            allow_local_single_user=True,
        )
        await db.execute(
            """
            UPDATE ingest_pending
               SET status = 'parser_waiting',
                   error_message = NULL,
                   updated_at = datetime('now')
             WHERE id = ?
               AND workspace_id = ?
            """,
            (ingest_id, workspace_id),
        )
        await db.commit()

    project_slug = row["project_slug"]
    await broadcast_ingest_changed(
        "parser_waiting",
        workspace_id=workspace_id,
        ingest_id=ingest_id,
        project_slug=project_slug,
        status="parser_waiting",
    )
    try:
        path = Path(row["file_path"])
        if not path.exists():
            raise FileNotFoundError(str(path))
        pending_llm_project_slug: str | None = None
        error_message: str | None = None
        mime_type = detect_mime(path)
        preflight = build_preflight(path, mime_type)
        source_context = _source_context_for_row(
            project_slug=project_slug,
            source_kind=row["source_kind"],
            path=path,
        )
        if source_context:
            preflight["source_context"] = source_context
        route = await _maybe_llm_route(
            route=_route_for(path, mime_type, preflight),
            preflight=preflight,
            mime_type=mime_type,
        )
        dispatch = await _parse_file_with_transient_retries(
            ingest_id,
            workspace_id,
            project_slug,
            path,
            mime_type,
            preflight=preflight,
            route=route,
        )
        parsed = dispatch.parsed
        parser_used = dispatch.parser_used
        is_image = _is_image(path, mime_type)
        is_xlsx = _is_xlsx(path, mime_type)
        is_docx = _is_docx(path, mime_type)
        if is_image:
            item = {
                "file_path": row["file_path"],
                "file_size_bytes": row["file_size_bytes"],
            }
            auto_approve = should_auto_approve(item, parsed)
            classification_json = _image_classification(
                path=path,
                auto_approve=auto_approve,
                reason=(
                    "safe image fast-lane"
                    if auto_approve
                    else "image requires manual triage"
                ),
                rules_matched=(
                    ["safe_ext", "under_1mb", "exif_redacted", "no_pii"]
                    if auto_approve
                    else []
                ),
            )
            next_status = "done" if auto_approve else "awaiting_triage"
            extracted_text = str(
                parsed.get("text") or parsed.get("extracted_text") or ""
            )
            structure = parsed.get("structure") or {}
            target_folder = classification_json["target_folder"]
            target_filename = classification_json["target_filename"]
            triage_decision_id = "auto_approve:image_parser" if auto_approve else None
        elif is_xlsx:
            classification_json = _xlsx_classification(path, parsed)
            next_status = "awaiting_triage"
            extracted_text = str(parsed.get("text") or "")
            structure = parsed.get("structure") or {}
            if str(structure.get("sha256") or "").lower() != str(
                row["sha256"] or ""
            ).lower():
                raise ValueError("XLSX source changed since queueing")
            target_folder = classification_json["target_folder"]
            target_filename = classification_json["target_filename"]
            triage_decision_id = None
        elif is_docx:
            classification = classify_markdown(
                frontmatter=parsed.frontmatter,
                original_filename=f"{path.stem}.md",
            )
            classification_json = classification.as_json()
            next_status = "awaiting_triage"
            extracted_text = parsed.text
            structure = parsed.structure
            target_folder = classification.target_folder
            target_filename = classification.target_filename
            triage_decision_id = None
        else:
            classification = classify_markdown(
                frontmatter=parsed.frontmatter,
                original_filename=_classification_filename(path, mime_type),
            )
            classification_json = classification.as_json()
            next_status = "awaiting_triage"
            extracted_text = parsed.text
            structure = parsed.structure
            target_folder = classification.target_folder
            target_filename = classification.target_filename
            triage_decision_id = None

        parser_quality = dict(dispatch.parser_quality)
        transcript_summary = await _maybe_summarize_transcript(
            ingest_id=ingest_id,
            parser_used=parser_used,
            extracted_text=extracted_text,
            structure=structure,
        )
        if transcript_summary is not None:
            structure = dict(structure or {})
            structure["transcript_summary"] = transcript_summary
            if transcript_summary.get("status") == "ok":
                classification_json = dict(classification_json)
                classification_json["transcript_summary"] = transcript_summary
                parser_quality["transcript_summary"] = transcript_summary

        diagnostic_preflight = dispatch.preflight
        if is_xlsx:
            diagnostic_preflight = _xlsx_preflight_for_storage(
                dispatch.preflight,
                sha256=str(structure.get("sha256") or ""),
            )
        structure = _with_ingest_v2_diagnostics(
            structure,
            preflight=diagnostic_preflight,
            route=dispatch.route,
            parser_quality=parser_quality,
        )

        if (
            next_status == "awaiting_triage"
            and extracted_text.strip()
            and not is_xlsx
        ):
            # Ingestor 2.0: optional local tier-fast project routing +
            # frontmatter inference. Auto-approval requires composite
            # confidence >= 0.80, not just LLM self-confidence.
            try:
                enriched = await _maybe_llm_enrich(
                    workspace_id=workspace_id,
                    ingest_id=ingest_id,
                    extracted_text=extracted_text,
                    base_classification=classification_json,
                    preflight=dispatch.preflight,
                    parser_quality=parser_quality,
                    route=dispatch.route,
                )
            except Exception:  # noqa: BLE001 - never break the saga
                logger.exception("llm enrichment failed")
                enriched = None
            if enriched is not None:
                classification_json = enriched
                target_folder = enriched.get("target_folder", target_folder)
                llm_error_message = _llm_failure_error_message(enriched)
                if llm_error_message:
                    next_status = "parse_error"
                    error_message = llm_error_message
                    triage_decision_id = None
                elif enriched.get("auto_approve") is True:
                    # Stay in 'approved' so execute_saga (scheduled below)
                    # picks the row up; saga moves the file to target_folder,
                    # populates the KG, indexes the embedding, then flips
                    # status to 'inserted' and finally 'done'.
                    next_status = "approved"
                    triage_decision_id = "auto_approve:llm_routing"
                    # Move row + file to LLM-suggested project so the saga
                    # processes the artifact under the new project root
                    # (saga rejects file_path that escapes project_root).
                    suggested_slug = enriched.get("suggested_project_slug")
                    if suggested_slug and suggested_slug != project_slug:
                        pending_llm_project_slug = str(suggested_slug)

        # --- U3 per-source policy gate (single authority; saga only asserts) ---
        # Owner surfaces are policy-exempt (decide_ingress_routing returns None).
        # api_ingress: 'open'/unknown -> always triage (default-deny); 'trusted'
        # -> keeps the intrinsic auto-insert decision (necessary-not-sufficient).
        # An 'open' downgrade flips next_status to awaiting_triage, which also
        # disables the saga/project-switch blocks below (they require 'approved').
        ingress_routing = decide_ingress_routing(
            source_kind=row["source_kind"],
            ingest_policy=row["ingest_policy"],
            intrinsic_status=next_status,
            intrinsic_basis=triage_decision_id,
        )
        if ingress_routing is not None:
            next_status = ingress_routing.status
            triage_decision_id = ingress_routing.triage_decision_id
            if pending_llm_project_slug and not ingress_routing.auto_insert:
                pending_llm_project_slug = None
            classification_json = dict(classification_json)
            classification_json["ingest_policy"] = {
                "effective_policy": row["ingest_policy"] or "open",
                "auto_insert": ingress_routing.auto_insert,
                "decision": ingress_routing.decision,
            }
            if "auto_approve" in classification_json:
                classification_json["auto_approve"] = ingress_routing.auto_insert

        # --- U3 reconcile ingress payload metadata into structure_json ---
        ingress_metadata_raw = (
            row["ingress_metadata"] if "ingress_metadata" in row.keys() else None
        )
        structure = _reconcile_ingress_metadata(structure, ingress_metadata_raw)

        async with acquire_write_db() as db:
            async with AsyncExitStack() as mutation_stack:
                if (
                    pending_llm_project_slug
                    and next_status == "approved"
                    and triage_decision_id == "auto_approve:llm_routing"
                ):
                    await mutation_stack.enter_async_context(
                        project_lifecycle.async_project_mutation_guard(
                            projects_root=PROJECTS_ROOT
                        )
                    )
                    (
                        project_slug,
                        path,
                        classification_json,
                        next_status,
                        triage_decision_id,
                    ) = await _apply_llm_project_switch_if_safe(
                        db,
                        ingest_id=ingest_id,
                        workspace_id=workspace_id,
                        sha256=row["sha256"],
                        current_project_slug=project_slug,
                        target_project_slug=pending_llm_project_slug,
                        path=path,
                        classification_json=classification_json,
                        lifecycle_lock_held=True,
                    )
                await db.execute(
                    """
                    UPDATE ingest_pending
                       SET status = ?,
                           project_slug = ?,
                           file_path = ?,
                           mime_type = ?,
                           parser_used = ?,
                           extracted_text = ?,
                           structure_json = ?,
                           classification_json = ?,
                           target_folder = ?,
                           target_filename = ?,
                           triage_decision_id = ?,
                           error_message = ?,
                           updated_at = datetime('now')
                     WHERE id = ?
                       AND workspace_id = ?
                    """,
                    (
                        next_status,
                        project_slug,
                        str(path),
                        mime_type,
                        parser_used,
                        extracted_text,
                        json.dumps(structure, ensure_ascii=False),
                        json.dumps(classification_json, ensure_ascii=False),
                        target_folder,
                        target_filename,
                        triage_decision_id,
                        error_message,
                        ingest_id,
                        workspace_id,
                    ),
                )
                await db.commit()
        await broadcast_ingest_changed(
            _ingest_event_for_status(next_status),
            workspace_id=workspace_id,
            ingest_id=ingest_id,
            project_slug=project_slug,
            status=next_status,
        )
        if (
            next_status == "approved"
            and triage_decision_id == "auto_approve:llm_routing"
        ):
            # Lazy import to avoid circular dependency (insert_saga imports
            # nothing here, but parse_pending is the canonical entry point
            # so we keep the boundary explicit).
            from core.api.services.ingest.insert_saga import execute_saga

            asyncio.create_task(execute_saga(ingest_id, workspace_id))
    except _ParserWaitCancelled:
        logger.info("ingest parse cancelled before parser slot: id=%s", ingest_id)
        return
    except Exception as exc:
        logger.exception("ingest parse failed: id=%s", ingest_id)
        await _mark_parse_error(ingest_id, workspace_id, project_slug, str(exc))


def _source_context_for_row(
    *,
    project_slug: str | None,
    source_kind: str | None,
    path: Path,
) -> dict[str, Any]:
    if not project_slug or source_kind != "terminal_upload":
        return {}
    try:
        project_root = PROJECTS_ROOT / project_slug
        in_project_input = path.resolve().is_relative_to((project_root / "input").resolve())
    except Exception:
        in_project_input = False
    if in_project_input:
        return {
            "project_slug": project_slug,
            "prior": 0.95,
            "reason": f"{source_kind or 'ingest'}_project_input",
        }
    return {
        "project_slug": project_slug,
        "prior": 0.75,
        "reason": f"{source_kind or 'ingest'}_row_project",
    }
