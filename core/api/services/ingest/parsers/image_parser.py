"""Standalone image parser for Universal Ingestion phase 1."""
from __future__ import annotations

import hashlib
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from core.api.services.ingest.parsers.docparse_gateway import parse_docparse_with_gateway
from core.api.services.ingest.parsers.gateway_aux import MissingGatewayConfig
from core.api.services.ingest.parsers.gateway_aux import settings as gateway_settings
from core.api.services.ingest.parsers.ocr_gateway import parse_ocr_with_gateway

logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
UNSUPPORTED_PHASE1_SUFFIXES = frozenset({".avif", ".heic", ".heif"})
EXIF_GPS_TAG = 0x8825
EXIF_ORIENTATION_TAG = 0x0112
IMAGE_MIME_BY_SUFFIX = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_IMAGE_MARKUP_RE = re.compile(r"(<\s*img\b|!\[[^\]]*\]\()", re.IGNORECASE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _validate_image_suffix(image_path: Path) -> None:
    suffix = image_path.suffix.lower()
    if suffix in UNSUPPORTED_PHASE1_SUFFIXES:
        raise ValueError(f"unsupported_phase1_image_ext: {suffix}")
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        raise ValueError(f"unsupported_image_ext: {suffix}")


def _image_result(
    image_path: Path,
    *,
    exif_result: dict[str, Any],
    ocr_result: dict[str, Any],
) -> dict[str, Any]:
    post_redact_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()

    structure = {
        "kind": "image",
        "format": exif_result["format"],
        "width": exif_result["width"],
        "height": exif_result["height"],
        "mode": exif_result["mode"],
        "exif_redacted": exif_result["exif_redacted"],
        "exif_removed": exif_result["exif_removed"],
        "sha256_post_redact": post_redact_sha,
        "ocr_confidence_avg": ocr_result.get("confidence_avg", 0.0),
        "ocr_lines": ocr_result.get("lines") or [],
    }
    if ocr_result.get("raw") is not None:
        structure["ocr_raw"] = ocr_result["raw"]
    return {
        "frontmatter": {},
        "text": str(ocr_result.get("extracted_text") or ""),
        "structure": structure,
        "parser_used": str(ocr_result.get("parser_used") or "none"),
        "extracted_text": str(ocr_result.get("extracted_text") or ""),
        "exif_redacted": exif_result["exif_redacted"],
        "exif_removed": exif_result["exif_removed"],
        "sha256_post_redact": post_redact_sha,
        "ocr_confidence_avg": ocr_result.get("confidence_avg", 0.0),
    }


def parse_image(path: str | Path) -> dict[str, Any]:
    """Parse a standalone image and redact privacy-sensitive EXIF metadata."""
    image_path = Path(path)
    _validate_image_suffix(image_path)
    exif_result = _strip_exif_privacy(image_path)
    return _image_result(
        image_path,
        exif_result=exif_result,
        ocr_result=_ocr_with_fallback(image_path),
    )


async def parse_image_with_gateway(
    path: str | Path,
    mime_type: str | None = None,
    *,
    prefer_docparse: bool | None = None,
    docparse_mode: str | None = None,
) -> dict[str, Any]:
    """Parse an image with the Mac Gateway OCR endpoint when configured."""
    image_path = Path(path)
    _validate_image_suffix(image_path)
    exif_result = _strip_exif_privacy(image_path)
    upload_mime = mime_type or IMAGE_MIME_BY_SUFFIX[image_path.suffix.lower()]
    cfg = gateway_settings()
    docparse_enabled = getattr(cfg, "ingest_docparse_enabled", False) and getattr(
        cfg, "ingest_docparse_images_enabled", True
    )
    if prefer_docparse is not None:
        docparse_enabled = bool(prefer_docparse)
    if docparse_enabled:
        try:
            docparse_result = await parse_docparse_with_gateway(
                image_path,
                upload_mime,
                mode=docparse_mode,
            )
            if _docparse_result_is_image_only(docparse_result):
                logger.warning(
                    "tier-docparse returned image-only markup; falling back to OCR: path=%s",
                    image_path,
                )
            else:
                return _image_docparse_result(
                    image_path,
                    exif_result=exif_result,
                    docparse_result=docparse_result,
                )
        except MissingGatewayConfig:
            logger.warning("tier-docparse not configured; falling back to OCR")

    try:
        ocr_result = await parse_ocr_with_gateway(
            image_path,
            upload_mime,
        )
    except MissingGatewayConfig:
        ocr_result = _ocr_with_fallback(image_path)
    return _image_result(
        image_path,
        exif_result=exif_result,
        ocr_result=ocr_result,
    )


def _docparse_result_is_image_only(docparse_result: dict[str, Any]) -> bool:
    text = str(docparse_result.get("text") or "")
    markdown = str(docparse_result.get("markdown") or "")
    raw = f"{text}\n{markdown}"
    if not _IMAGE_MARKUP_RE.search(raw):
        return False
    meaningful = _meaningful_docparse_text(raw)
    if len(meaningful) >= 40:
        return False
    return True


def _meaningful_docparse_text(raw: str) -> str:
    without_markdown_images = _MARKDOWN_IMAGE_RE.sub(" ", raw)
    without_html = _HTML_TAG_RE.sub(" ", without_markdown_images)
    tokens = re.findall(r"\b[^\W\d_]{2,}\b", without_html, flags=re.UNICODE)
    return " ".join(tokens)


def _image_docparse_result(
    image_path: Path,
    *,
    exif_result: dict[str, Any],
    docparse_result: dict[str, Any],
) -> dict[str, Any]:
    text = str(docparse_result.get("text") or docparse_result.get("markdown") or "")
    structure = {
        "kind": "image",
        "format": exif_result["format"],
        "width": exif_result["width"],
        "height": exif_result["height"],
        "mode": exif_result["mode"],
        "exif_redacted": exif_result["exif_redacted"],
        "exif_removed": exif_result["exif_removed"],
        "sha256_post_redact": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "docparse_backend": "tier_docparse",
        "page_count": docparse_result.get("page_count"),
        "elements_count": len(docparse_result.get("elements") or []),
        "elements": docparse_result.get("elements") or [],
        "pages": docparse_result.get("pages") or [],
        "metadata": docparse_result.get("metadata") or {},
    }
    return {
        "frontmatter": {},
        "text": text,
        "structure": structure,
        "parser_used": "tier_docparse",
        "extracted_text": text,
        "exif_redacted": exif_result["exif_redacted"],
        "exif_removed": exif_result["exif_removed"],
        "sha256_post_redact": structure["sha256_post_redact"],
    }


def _strip_exif_privacy(path: Path) -> dict[str, Any]:
    """Remove GPS and non-orientation EXIF while preserving display orientation."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            removed = False
            if exif:
                orientation = exif.get(EXIF_ORIENTATION_TAG)
                sanitized = Image.Exif()
                if orientation is not None:
                    sanitized[EXIF_ORIENTATION_TAG] = orientation
                removed = set(exif.keys()) != set(sanitized.keys()) or any(
                    exif.get(key) != sanitized.get(key) for key in sanitized.keys()
                )
                if EXIF_GPS_TAG in exif:
                    removed = True
                if removed:
                    image.save(path, exif=sanitized.tobytes() if sanitized else b"")
            return {
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "exif_redacted": True,
                "exif_removed": removed,
            }
    except Exception as exc:
        raise ValueError(f"invalid_image: {exc}") from exc


def _ocr_with_fallback(path: Path) -> dict[str, Any]:
    """Run Tesseract OCR locally (PaddleOCR-VL via Mac Gateway tier-ocr upstream)."""
    try:
        import pytesseract

        with Image.open(path) as image:
            try:
                text = pytesseract.image_to_string(image, lang="ita+eng")
            except Exception:
                logger.warning("Tesseract ita+eng failed, retrying default language", exc_info=True)
                text = pytesseract.image_to_string(image)
        return {
            "extracted_text": str(text),
            "parser_used": "tesseract",
            "confidence_avg": 0.0,
            "lines": [],
        }
    except Exception as exc:
        logger.warning("No OCR backend available for image parser: %s", exc)
        return {
            "extracted_text": "",
            "parser_used": "none",
            "confidence_avg": 0.0,
            "lines": [],
        }
