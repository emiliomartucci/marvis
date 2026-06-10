"""Document parser backed by the Mac Gateway AUX /v1/docparse endpoint."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from core.api.services.ingest.parsers.gateway_aux import (
    auth_headers,
    aux_base_url,
    request_gateway_with_retries,
    settings,
)
from core.api.services.ingest.parsers.pdf_types import PdfParseResult


async def parse_docparse_with_gateway(
    path: Path,
    mime_type: str,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    cfg = settings()
    size = path.stat().st_size
    if size > int(cfg.ingest_docparse_max_bytes):
        raise ValueError(f"File too large for docparse: {size} bytes")

    timeout_seconds = float(cfg.ingest_docparse_timeout_seconds)
    timeout = httpx.Timeout(
        connect=5.0,
        read=timeout_seconds,
        write=60.0,
        pool=5.0,
    )
    async with httpx.AsyncClient(
        base_url=f"{aux_base_url()}/",
        timeout=timeout,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        async def post_docparse() -> httpx.Response:
            with path.open("rb") as handle:
                return await client.post(
                    "docparse",
                    headers=auth_headers(),
                    data={
                        "output_format": "markdown",
                        "mode": mode or cfg.ingest_docparse_mode,
                    },
                    files={"file": (path.name, handle, mime_type)},
                )

        response = await request_gateway_with_retries(
            post_docparse,
            service_name="tier-docparse",
        )
        payload = await _resolve_docparse_response(
            client,
            response,
            timeout_seconds=timeout_seconds,
        )

    text = str(payload.get("text") or "").strip()
    markdown = str(payload.get("markdown") or "").strip()
    if not text and markdown:
        text = markdown
    if not text:
        raise RuntimeError("tier-docparse returned empty text")

    return {
        "text": text,
        "markdown": markdown,
        "page_count": payload.get("page_count"),
        "elements": payload.get("elements") or [],
        "pages": payload.get("pages") or [],
        "metadata": payload.get("metadata") or {},
        "raw": payload,
    }


async def parse_pdf_docparse(path: Path, *, mode: str | None = None) -> PdfParseResult:
    parsed = await parse_docparse_with_gateway(path, "application/pdf", mode=mode)
    return PdfParseResult(
        frontmatter={},
        text=parsed["markdown"] or parsed["text"],
        structure={
            "page_count": parsed.get("page_count"),
            "elements_count": len(parsed.get("elements") or []),
            "elements": parsed.get("elements") or [],
            "pages": parsed.get("pages") or [],
            "metadata": parsed.get("metadata") or {},
            "docparse_backend": "tier_docparse",
        },
        parser_used="tier_docparse",
    )


async def _resolve_docparse_response(
    client: httpx.AsyncClient,
    response: httpx.Response,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    if response.status_code in {401, 403}:
        raise RuntimeError("tier-docparse authorization failed")
    if response.status_code == 429:
        raise RuntimeError("tier-docparse rate limited")
    if response.status_code >= 500:
        raise RuntimeError(f"tier-docparse unavailable: HTTP {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("tier-docparse returned invalid payload")
    if response.status_code != 202:
        return payload

    status_url = str(payload.get("status_url") or "").strip()
    if not status_url:
        raise RuntimeError("tier-docparse async response missing status_url")
    return await _poll_docparse_job(client, status_url, timeout_seconds=timeout_seconds)


async def _poll_docparse_job(
    client: httpx.AsyncClient,
    status_url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    request_url = _status_request_url(client, status_url)
    while time.monotonic() < deadline:
        async def get_status() -> httpx.Response:
            return await client.get(request_url, headers=auth_headers())

        response = await request_gateway_with_retries(
            get_status,
            service_name="tier-docparse-poll",
        )
        if response.status_code in {401, 403}:
            raise RuntimeError("tier-docparse poll authorization failed")
        if response.status_code == 404:
            raise RuntimeError("tier-docparse job not found")
        if response.status_code >= 500:
            raise RuntimeError(f"tier-docparse poll unavailable: HTTP {response.status_code}")
        response.raise_for_status()
        status_payload = response.json()
        if not isinstance(status_payload, dict):
            raise RuntimeError("tier-docparse poll returned invalid payload")

        status = status_payload.get("status")
        if status == "done":
            result = status_payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("tier-docparse completed without result")
            return result
        if status == "failed":
            error = status_payload.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(f"tier-docparse job failed: {message or 'unknown error'}")

        retry_after = response.headers.get("Retry-After")
        try:
            sleep_for = float(retry_after) if retry_after else 2.0
        except ValueError:
            sleep_for = 2.0
        await asyncio.sleep(max(0.2, min(sleep_for, 5.0)))

    raise TimeoutError(f"tier-docparse job timed out after {timeout_seconds:g}s")


def _status_request_url(client: httpx.AsyncClient, status_url: str) -> str:
    if status_url.startswith(("http://", "https://")):
        return status_url
    if status_url.startswith("/"):
        return str(client.base_url.copy_with(path=status_url, query=None, fragment=None))
    return status_url
