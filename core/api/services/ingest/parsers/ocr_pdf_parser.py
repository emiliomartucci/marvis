"""OCR fallback for scanned PDFs."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.api.services.ingest.parsers.gateway_aux import MissingGatewayConfig
from core.api.services.ingest.parsers.ocr_gateway import parse_ocr_with_gateway
from core.api.services.ingest.parsers.pdf_types import PdfParseResult

logger = logging.getLogger(__name__)

OCR_TIMEOUT_SECONDS = 180
OCR_WORKERS = 2


@lru_cache(maxsize=1)
def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=OCR_WORKERS, thread_name_prefix="pdf-ocr")


@lru_cache(maxsize=1)
def _rapidocr_converter() -> Any:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            RapidOcrOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise RuntimeError("Docling with RapidOCR is required for PDF OCR fallback") from exc

    pipeline_options = PdfPipelineOptions(
        do_ocr=True,
        ocr_options=RapidOcrOptions(
            backend="onnxruntime",
            force_full_page_ocr=True,
            lang=["english"],
        ),
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )


def _ocr_sync(file_path: Path) -> PdfParseResult:
    result = _rapidocr_converter().convert(str(file_path))
    document = result.document
    markdown = str(document.export_to_markdown() or "").strip()
    structure = _safe_doc_dict(document)
    pages = getattr(document, "pages", None)
    page_count = len(pages) if pages is not None else None
    if page_count is not None:
        structure.setdefault("page_count", page_count)
    structure.update(
        {
            "ocr_backend": "rapidocr",
            "ocr_engine": "docling_rapidocr",
        }
    )
    return PdfParseResult(
        frontmatter={},
        text=markdown,
        structure=structure,
        parser_used="rapidocr_fallback",
    )


def _safe_doc_dict(document: Any) -> dict[str, Any]:
    if hasattr(document, "export_to_dict"):
        value = document.export_to_dict()
        return value if isinstance(value, dict) else {"document": value}
    if hasattr(document, "dict"):
        value = document.dict()
        return value if isinstance(value, dict) else {"document": value}
    return {}


async def parse_pdf_ocr(file_path: Path) -> PdfParseResult:
    try:
        ocr = await parse_ocr_with_gateway(file_path, "application/pdf")
        raw = ocr.get("raw") or {}
        return PdfParseResult(
            frontmatter={},
            text=str(ocr["extracted_text"]),
            structure={
                "pages": raw.get("pages"),
                "ocr_backend": "tier_ocr",
                "ocr_confidence_avg": ocr.get("confidence_avg", 0.0),
                "ocr_lines": ocr.get("lines") or [],
                "ocr_raw": raw,
            },
            parser_used="tier_ocr",
        )
    except MissingGatewayConfig:
        pass

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor(), _ocr_sync, file_path)
    try:
        return await asyncio.wait_for(future, timeout=OCR_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"OCR timeout {OCR_TIMEOUT_SECONDS}s") from exc
