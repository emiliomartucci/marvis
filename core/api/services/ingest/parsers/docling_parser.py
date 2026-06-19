"""PDF parser using Docling as the primary extractor."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.api.services.ingest.parsers.ocr_pdf_parser import parse_pdf_ocr
from core.api.services.ingest.parsers.docparse_gateway import parse_pdf_docparse
from core.api.services.ingest.parsers.gateway_aux import MissingGatewayConfig, settings
from core.api.services.ingest.parsers.pdf_types import PdfParseResult

logger = logging.getLogger(__name__)

DOCLING_TIMEOUT_SECONDS = 120
DOCLING_WORKERS = 3
MIN_TEXT_FOR_OCR_FALLBACK = 100


@lru_cache(maxsize=1)
def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=DOCLING_WORKERS, thread_name_prefix="docling")


@lru_cache(maxsize=1)
def _converter():
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def _is_encrypted(file_path: Path) -> bool:
    try:
        from pypdf import PdfReader

        return bool(PdfReader(str(file_path)).is_encrypted)
    except ImportError:
        logger.warning("pypdf not installed; skipping PDF encryption preflight")
        return False
    except Exception:
        logger.warning("PDF encryption preflight failed for %s", file_path, exc_info=True)
        return False


def _safe_doc_dict(document: Any) -> dict[str, Any]:
    if hasattr(document, "export_to_dict"):
        value = document.export_to_dict()
        return value if isinstance(value, dict) else {"document": value}
    if hasattr(document, "dict"):
        value = document.dict()
        return value if isinstance(value, dict) else {"document": value}
    return {}


def _count_tables(document: Any, structure: dict[str, Any]) -> int:
    tables = getattr(document, "tables", None)
    if isinstance(tables, list):
        return len(tables)
    if hasattr(document, "iterate_items"):
        try:
            return sum(
                1
                for item in document.iterate_items()
                if str(getattr(item, "label", "")).lower() == "table"
            )
        except Exception:
            logger.debug("Docling table iteration failed", exc_info=True)
    return _count_table_like_entries(structure)


def _count_table_like_entries(value: Any) -> int:
    if isinstance(value, dict):
        count = 1 if str(value.get("label", "")).lower() == "table" else 0
        return count + sum(_count_table_like_entries(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_table_like_entries(item) for item in value)
    return 0


def _convert_with_docling(file_path: Path) -> PdfParseResult:
    result = _converter().convert(str(file_path))
    document = result.document
    markdown = document.export_to_markdown()
    structure = _safe_doc_dict(document)
    structure.setdefault("tables_count", _count_tables(document, structure))
    pages = getattr(document, "pages", None)
    if pages is not None:
        structure.setdefault("page_count", len(pages))
    return PdfParseResult(
        frontmatter={},
        text=markdown,
        structure=structure,
        parser_used="docling",
    )


def _convert_sync(file_path: Path) -> PdfParseResult:
    return _convert_with_docling(file_path)


async def parse_pdf_file(
    file_path: Path,
    *,
    allow_docparse: bool = True,
    docparse_mode: str | None = None,
) -> PdfParseResult:
    """Parse a PDF and run OCR fallback when Docling finds too little text."""
    if _is_encrypted(file_path):
        raise ValueError("PDF encrypted, requires password")

    cfg = settings()
    if allow_docparse and getattr(cfg, "ingest_docparse_enabled", False) and getattr(
        cfg, "ingest_docparse_pdfs_enabled", True
    ):
        try:
            return await parse_pdf_docparse(file_path, mode=docparse_mode)
        except MissingGatewayConfig:
            logger.warning("tier-docparse not configured; falling back to Docling")

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor(), _convert_sync, file_path)
    try:
        parsed = await asyncio.wait_for(future, timeout=DOCLING_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"parse timeout {DOCLING_TIMEOUT_SECONDS}s") from exc
    except Exception:
        logger.warning(
            "Docling parse failed; trying OCR fallback: %s",
            file_path,
            exc_info=True,
        )
        return await parse_pdf_ocr(file_path)

    if len(parsed.text.strip()) >= MIN_TEXT_FOR_OCR_FALLBACK:
        return parsed

    logger.info("Docling extracted <100 chars, trying OCR fallback: %s", file_path)
    return await parse_pdf_ocr(file_path)
