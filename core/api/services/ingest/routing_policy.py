"""Tier-aware parser routing policy for Ingestor 2.0."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Workflow = Literal["local", "ocr", "docparse", "transcribe", "vision", "skip"]
DocparseMode = Literal["fast", "standard", "precise"]


@dataclass(frozen=True)
class IngestRoute:
    workflow: Workflow
    tier: str | None
    mode: DocparseMode | None
    reason: str
    confidence: float
    features_used: list[str]

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


LOCAL_SUFFIXES = {".md", ".markdown", ".txt", ".docx", ".xlsx"}
AUDIO_VIDEO_PREFIXES = ("audio/", "video/")
BILL_FEATURES = {"bill_hint", "table_hint"}


def choose_route(
    *,
    path: Path,
    mime_type: str,
    preflight: dict[str, Any],
    docparse_enabled: bool = True,
    ocr_enabled: bool = True,
    vision_enabled: bool = False,
    mode_override: DocparseMode | None = None,
) -> IngestRoute:
    """Choose the parser workflow from deterministic file evidence."""
    suffix = path.suffix.lower()
    pf = dict(preflight.get("preflight") or {})

    if suffix in LOCAL_SUFFIXES or mime_type in {
        "text/markdown",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return IngestRoute(
            workflow="local",
            tier=None,
            mode=None,
            reason=f"{suffix or mime_type} handled by local parser",
            confidence=0.98,
            features_used=["suffix", "mime_type"],
        )

    if mime_type.startswith(AUDIO_VIDEO_PREFIXES):
        return IngestRoute(
            workflow="transcribe",
            tier="tier-transcribe",
            mode=None,
            reason="audio/video content routes to transcript extraction",
            confidence=0.99,
            features_used=["mime_type"],
        )

    if mime_type == "application/pdf" or suffix == ".pdf":
        return _pdf_route(pf, docparse_enabled=docparse_enabled, ocr_enabled=ocr_enabled, mode_override=mode_override)

    if mime_type.startswith("image/"):
        return _image_route(
            pf,
            docparse_enabled=docparse_enabled,
            ocr_enabled=ocr_enabled,
            vision_enabled=vision_enabled,
            mode_override=mode_override,
        )

    return IngestRoute(
        workflow="skip",
        tier=None,
        mode=None,
        reason=f"unsupported MIME for Ingestor 2.0: {mime_type}",
        confidence=1.0,
        features_used=["mime_type"],
    )


def _pdf_route(
    pf: dict[str, Any],
    *,
    docparse_enabled: bool,
    ocr_enabled: bool,
    mode_override: DocparseMode | None,
) -> IngestRoute:
    text_chars = int(pf.get("text_layer_chars") or 0)
    table_or_bill = bool(pf.get("table_hint") or pf.get("bill_hint") or pf.get("identity_hint"))
    scanned = bool(pf.get("scanned_hint"))
    mixed = bool(pf.get("mixed_pdf_hint"))

    if table_or_bill and docparse_enabled:
        return IngestRoute(
            workflow="docparse",
            tier="tier-docparse",
            mode=mode_override or "standard",
            reason="PDF has bill/table/identity layout signals",
            confidence=0.88,
            features_used=_features(pf, ["table_hint", "bill_hint", "identity_hint", "text_layer_chars"]),
        )

    if scanned and ocr_enabled:
        return IngestRoute(
            workflow="ocr",
            tier="tier-ocr",
            mode=None,
            reason="PDF appears scanned with weak text layer",
            confidence=0.84,
            features_used=_features(pf, ["scanned_hint", "image_refs", "text_layer_chars"]),
        )

    if mixed and docparse_enabled:
        return IngestRoute(
            workflow="docparse",
            tier="tier-docparse",
            mode=mode_override or "standard",
            reason="PDF has mixed text and image/layout signals",
            confidence=0.78,
            features_used=_features(pf, ["mixed_pdf_hint", "image_refs", "text_layer_chars"]),
        )

    if text_chars >= 500:
        return IngestRoute(
            workflow="local",
            tier=None,
            mode=None,
            reason="PDF has a strong embedded text layer",
            confidence=0.92,
            features_used=["text_layer_chars"],
        )

    if docparse_enabled:
        return IngestRoute(
            workflow="docparse",
            tier="tier-docparse",
            mode=mode_override or "standard",
            reason="PDF has limited deterministic evidence; docparse preserves layout",
            confidence=0.62,
            features_used=_features(pf, ["text_layer_chars", "page_count"]),
        )

    return IngestRoute(
        workflow="local",
        tier=None,
        mode=None,
        reason="PDF routed local because docparse is disabled",
        confidence=0.55,
        features_used=_features(pf, ["text_layer_chars", "page_count"]),
    )


def _image_route(
    pf: dict[str, Any],
    *,
    docparse_enabled: bool,
    ocr_enabled: bool,
    vision_enabled: bool,
    mode_override: DocparseMode | None,
) -> IngestRoute:
    document_likelihood = _float(pf.get("document_likelihood"))
    screenshot_likelihood = _float(pf.get("screenshot_likelihood"))
    photo_likelihood = _float(pf.get("photo_likelihood"))
    text_likelihood = _float(pf.get("text_likelihood"))
    image_kind = str(pf.get("image_kind") or "")
    text_heavy_screenshot = (
        screenshot_likelihood >= 0.65
        and text_likelihood >= 0.70
        and document_likelihood < 0.55
    )
    visual_screenshot = (
        (image_kind == "screenshot" or screenshot_likelihood >= 0.65)
        and text_likelihood < 0.70
        and document_likelihood < 0.60
        and not pf.get("bill_hint")
        and not pf.get("identity_hint")
    )

    if (
        pf.get("poor_capture_hint")
        and (document_likelihood >= 0.45 or pf.get("document_boundary_hint"))
        and docparse_enabled
    ):
        return IngestRoute(
            workflow="docparse",
            tier="tier-docparse",
            mode=mode_override or "precise",
            reason="image looks low-resolution or orientation-challenged",
            confidence=0.82,
            features_used=_features(
                pf,
                ["poor_capture_hint", "orientation", "width", "height", "document_likelihood"],
            ),
        )

    if ocr_enabled and text_heavy_screenshot:
        return IngestRoute(
            workflow="ocr",
            tier="tier-ocr",
            mode=None,
            reason="image looks like a text-heavy screenshot",
            confidence=0.82,
            features_used=_features(
                pf,
                ["screenshot_likelihood", "text_likelihood", "width", "height"],
            ),
        )

    if vision_enabled and visual_screenshot:
        return IngestRoute(
            workflow="vision",
            tier="tier-vision",
            mode=None,
            reason="image needs visual context rather than document parsing",
            confidence=0.80,
            features_used=_features(
                pf,
                [
                    "image_kind",
                    "screenshot_likelihood",
                    "text_likelihood",
                    "document_likelihood",
                    "document_boundary_hint",
                ],
            ),
        )

    if (
        pf.get("bill_hint")
        or pf.get("identity_hint")
        or document_likelihood >= 0.70
        or (pf.get("document_boundary_hint") and photo_likelihood < 0.62)
    ) and docparse_enabled:
        return IngestRoute(
            workflow="docparse",
            tier="tier-docparse",
            mode=mode_override or "standard",
            reason="image looks like a document photo where layout matters",
            confidence=0.86,
            features_used=_features(
                pf,
                [
                    "bill_hint",
                    "identity_hint",
                    "document_boundary_hint",
                    "document_likelihood",
                    "aspect_ratio",
                ],
            ),
        )

    if vision_enabled and (
        image_kind in {"screenshot", "photo"}
        or screenshot_likelihood >= 0.62
        or photo_likelihood >= 0.62
    ):
        return IngestRoute(
            workflow="vision",
            tier="tier-vision",
            mode=None,
            reason="image needs visual context rather than document parsing",
            confidence=0.78,
            features_used=_features(
                pf,
                ["image_kind", "screenshot_likelihood", "photo_likelihood", "text_likelihood"],
            ),
        )

    if ocr_enabled:
        return IngestRoute(
            workflow="ocr",
            tier="tier-ocr",
            mode=None,
            reason="image routes to OCR for plain text extraction",
            confidence=0.78 if pf.get("screenshot_hint") else 0.68,
            features_used=_features(pf, ["screenshot_hint", "width", "height"]),
        )

    return IngestRoute(
        workflow="local",
        tier=None,
        mode=None,
        reason="image routed local because Gateway OCR/docparse is disabled",
        confidence=0.50,
        features_used=_features(pf, ["width", "height"]),
    )


def _features(pf: dict[str, Any], names: list[str]) -> list[str]:
    return [name for name in names if pf.get(name) not in {None, False, "", 0}]


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
