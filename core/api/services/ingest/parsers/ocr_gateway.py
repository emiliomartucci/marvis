"""OCR parser backed by the Mac Gateway AUX /v1/ocr endpoint."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from core.api.services.ingest.parsers.gateway_aux import (
    auth_headers,
    aux_base_url,
    request_gateway_with_retries,
    settings,
)


def _normalize_lines(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    lines: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        line: dict[str, Any] = {"text": text}
        confidence = item.get("confidence", item.get("score"))
        if confidence is not None:
            try:
                line["confidence"] = float(confidence)
            except (TypeError, ValueError):
                pass
        if item.get("bbox") is not None:
            line["bbox"] = item["bbox"]
        if item.get("page") is not None:
            line["page"] = item["page"]
        lines.append(line)
    return lines


def _confidence_avg(lines: list[dict[str, Any]], payload: dict[str, Any]) -> float:
    explicit = payload.get("confidence_avg", payload.get("avg_confidence"))
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    values = [
        float(line["confidence"])
        for line in lines
        if isinstance(line.get("confidence"), (int, float))
    ]
    return sum(values) / len(values) if values else 0.0


def _text_from_payload(payload: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    direct = payload.get("text") or payload.get("extracted_text")
    if direct:
        return str(direct).strip()
    return "\n".join(str(line["text"]) for line in lines).strip()


async def parse_ocr_with_gateway(path: Path, mime_type: str) -> dict[str, Any]:
    cfg = settings()
    size = path.stat().st_size
    if size > int(cfg.ingest_ocr_max_bytes):
        raise ValueError(f"File too large for OCR: {size} bytes")

    timeout = httpx.Timeout(
        connect=5.0,
        read=float(cfg.ingest_ocr_timeout_seconds),
        write=60.0,
        pool=5.0,
    )
    async with httpx.AsyncClient(
        base_url=f"{aux_base_url()}/",
        timeout=timeout,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    ) as client:
        async def post_ocr() -> httpx.Response:
            with path.open("rb") as handle:
                return await client.post(
                    "ocr",
                    headers=auth_headers(),
                    files={"file": (path.name, handle, mime_type)},
                )

        response = await request_gateway_with_retries(
            post_ocr,
            service_name="tier-ocr",
            max_attempts=5,
        )

    if response.status_code in {401, 403}:
        raise RuntimeError("tier-ocr authorization failed")
    if response.status_code == 429:
        raise RuntimeError("tier-ocr rate limited")
    if response.status_code >= 500:
        raise RuntimeError(f"tier-ocr unavailable: HTTP {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("tier-ocr returned invalid payload")

    lines = _normalize_lines(payload.get("lines"))
    text = _text_from_payload(payload, lines)
    if not text:
        raise RuntimeError("tier-ocr returned empty text")

    return {
        "extracted_text": text,
        "parser_used": "tier_ocr",
        "confidence_avg": _confidence_avg(lines, payload),
        "lines": lines,
        "raw": payload,
    }
