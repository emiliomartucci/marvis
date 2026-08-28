# v1.0.0 - 2026-05-27 - S1 F0: HTTP adapter — ServiceError -> HTTPException (single error->HTTP map)
"""HTTP adapter: maps a domain :class:`ServiceError` to a FastAPI ``HTTPException``.

This is the HTTP surface's error boundary, so importing ``fastapi`` here is
correct (it lives under ``routers``, not ``use_cases``). The mapping is the one
and only place that turns a domain error into an HTTP status.
"""
from __future__ import annotations

from fastapi import HTTPException

from core.api.use_cases._errors import ServiceError


def to_http(err: ServiceError) -> HTTPException:
    """Translate a domain ``ServiceError`` into an ``HTTPException``.

    Uses ``err.http_status`` for the status code and a structured
    ``{"code", "message"}`` detail body.
    """
    headers = None
    retry_after = err.context.get("retry_after")
    if isinstance(retry_after, int) and retry_after > 0:
        headers = {"Retry-After": str(retry_after)}
    detail = {"code": err.code, "message": err.message}
    if err.context:
        detail["context"] = err.context
    return HTTPException(
        status_code=err.http_status,
        detail=detail,
        headers=headers,
    )
