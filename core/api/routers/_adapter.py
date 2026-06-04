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
    return HTTPException(
        status_code=err.http_status,
        detail={"code": err.code, "message": err.message},
    )
