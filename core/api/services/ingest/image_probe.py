"""Lightweight image evidence for ingest routing decisions."""
from __future__ import annotations

from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from PIL import Image

BILL_TERMS = {"bolletta", "fattura", "invoice", "pod", "pdr", "kwh", "energia", "gas"}
IDENTITY_TERMS = {
    "carta-identita",
    "carta_identita",
    "identity",
    "passport",
    "passaporto",
    "codice-fiscale",
}
DOCUMENT_TERMS = {"contratto", "contract", "report", "modulo", "form", "documento"}


def probe_image(path: Path) -> dict[str, Any]:
    """Return cheap, auditable image signals without OCR or vision calls."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            width = int(image.width)
            height = int(image.height)
            orientation = exif.get(0x0112) if exif else None
            camera_hint = bool(exif.get(0x010F) or exif.get(0x0110)) if exif else False
            stats = _pixel_stats(image)
            aspect_ratio = round(width / height, 4) if height else None
            suffix = path.suffix.lower()
            name_score = _filename_document_score(path)
            screenshot_hint = _screenshot_hint(
                suffix=suffix,
                width=width,
                height=height,
                camera_hint=camera_hint,
            )
            document_boundary_hint = _document_boundary_hint(width, height)
            poor_capture_hint = _poor_capture_hint(width, height, orientation)

            document_likelihood = _clamp(
                0.12
                + (0.35 if document_boundary_hint else 0.0)
                + (0.18 if stats["white_ratio"] >= 0.45 else 0.0)
                + (0.16 if stats["edge_density"] >= 0.08 else 0.0)
                + name_score
                - (0.18 if camera_hint and stats["white_ratio"] < 0.35 else 0.0)
            )
            screenshot_likelihood = _clamp(
                0.10
                + (0.45 if screenshot_hint else 0.0)
                + (0.18 if width > height else 0.0)
                + (0.14 if stats["edge_density"] >= 0.10 else 0.0)
                - (0.28 if camera_hint else 0.0)
            )
            photo_likelihood = _clamp(
                0.18
                + (0.42 if camera_hint else 0.0)
                + (0.18 if stats["white_ratio"] < 0.25 else 0.0)
                + (0.10 if stats["contrast"] > 55 else 0.0)
                - (0.20 if screenshot_hint else 0.0)
            )
            text_likelihood = _clamp(
                0.20
                + (0.35 if stats["edge_density"] >= 0.10 else 0.0)
                + (0.18 if stats["contrast"] >= 35 else 0.0)
                + (0.15 if document_boundary_hint else 0.0)
            )

            return {
                "kind": "image",
                "format": image.format,
                "width": width,
                "height": height,
                "aspect_ratio": aspect_ratio,
                "orientation": orientation,
                "exif_camera_hint": camera_hint,
                "exif_tag_count": len(exif or {}),
                "screenshot_hint": screenshot_hint,
                "document_boundary_hint": document_boundary_hint,
                "poor_capture_hint": poor_capture_hint,
                "white_background_ratio": stats["white_ratio"],
                "edge_density": stats["edge_density"],
                "brightness": stats["brightness"],
                "contrast": stats["contrast"],
                "document_likelihood": round(document_likelihood, 4),
                "screenshot_likelihood": round(screenshot_likelihood, 4),
                "photo_likelihood": round(photo_likelihood, 4),
                "text_likelihood": round(text_likelihood, 4),
                "image_kind": _image_kind(
                    document_likelihood=document_likelihood,
                    screenshot_likelihood=screenshot_likelihood,
                    photo_likelihood=photo_likelihood,
                ),
                "signals": _signals(
                    screenshot_hint=screenshot_hint,
                    document_boundary_hint=document_boundary_hint,
                    poor_capture_hint=poor_capture_hint,
                    camera_hint=camera_hint,
                    name_score=name_score,
                    stats=stats,
                ),
            }
    except Exception:
        return {"kind": "image", "preflight_error": "image_probe_failed"}


def _pixel_stats(image: Image.Image) -> dict[str, float]:
    sample = image.convert("L")
    sample.thumbnail((160, 160))
    width, height = sample.size
    pixels = list(sample.tobytes())
    if not pixels:
        return {"white_ratio": 0.0, "edge_density": 0.0, "brightness": 0.0, "contrast": 0.0}

    white_ratio = sum(1 for value in pixels if value >= 235) / len(pixels)
    brightness = mean(pixels)
    contrast = pstdev(pixels) if len(pixels) > 1 else 0.0
    edge_hits = 0
    comparisons = 0
    for y in range(height):
        row = y * width
        for x in range(width):
            value = pixels[row + x]
            if x + 1 < width:
                comparisons += 1
                edge_hits += abs(value - pixels[row + x + 1]) >= 32
            if y + 1 < height:
                comparisons += 1
                edge_hits += abs(value - pixels[row + width + x]) >= 32
    edge_density = edge_hits / comparisons if comparisons else 0.0
    return {
        "white_ratio": round(white_ratio, 4),
        "edge_density": round(edge_density, 4),
        "brightness": round(brightness, 2),
        "contrast": round(contrast, 2),
    }


def _filename_document_score(path: Path) -> float:
    stem = path.stem.lower()
    if any(term in stem for term in BILL_TERMS | IDENTITY_TERMS):
        return 0.28
    if any(term in stem for term in DOCUMENT_TERMS):
        return 0.18
    return 0.0


def _screenshot_hint(*, suffix: str, width: int, height: int, camera_hint: bool) -> bool:
    if camera_hint:
        return False
    if suffix != ".png":
        return False
    return width >= 800 and height >= 500


def _document_boundary_hint(width: int, height: int) -> bool:
    if not width or not height:
        return False
    aspect = width / height
    return 0.55 <= aspect <= 1.9


def _poor_capture_hint(width: int, height: int, orientation: Any) -> bool:
    return width * height < 600_000 or orientation in {3, 6, 8}


def _image_kind(
    *,
    document_likelihood: float,
    screenshot_likelihood: float,
    photo_likelihood: float,
) -> str:
    ranked = sorted(
        [
            ("document", document_likelihood),
            ("screenshot", screenshot_likelihood),
            ("photo", photo_likelihood),
        ],
        key=lambda item: item[1],
        reverse=True,
    )
    kind, score = ranked[0]
    return kind if score >= 0.62 else "ambiguous"


def _signals(
    *,
    screenshot_hint: bool,
    document_boundary_hint: bool,
    poor_capture_hint: bool,
    camera_hint: bool,
    name_score: float,
    stats: dict[str, float],
) -> list[str]:
    signals: list[str] = []
    if screenshot_hint:
        signals.append("screenshot_hint")
    if document_boundary_hint:
        signals.append("document_boundary_hint")
    if poor_capture_hint:
        signals.append("poor_capture_hint")
    if camera_hint:
        signals.append("exif_camera_hint")
    if name_score:
        signals.append("filename_document_terms")
    if stats["white_ratio"] >= 0.45:
        signals.append("white_background")
    if stats["edge_density"] >= 0.10:
        signals.append("edge_dense")
    return signals


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
