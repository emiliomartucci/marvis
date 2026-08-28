# v1.0.0 - 2026-05-27 - S1 F0: domain exception hierarchy (replaces HTTPException in the domain)
"""Domain exceptions — the use_cases-layer replacement for ``HTTPException``.

Use cases raise these instead of FastAPI's ``HTTPException`` so the domain stays
free of transport coupling. Each surface owns the mapping:

- HTTP adapter (``routers/_adapter.py``) maps to ``HTTPException`` using the
  ``http_status`` hint.
- MCP adapter (``mcp/_adapter.py``) IGNORES ``http_status`` and maps ``code`` +
  ``message`` to the MCP error shape.

``http_status`` is therefore an adapter *hint*, never coupling.
"""
from __future__ import annotations


class ServiceError(Exception):
    """Base domain error. Carries a machine-readable ``code`` and a human ``message``.

    ``http_status`` is a hint for the HTTP adapter only; other surfaces (MCP)
    ignore it and map ``code``/``message`` to their own error shape.
    """

    http_status: int = 400

    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.context = dict(context or {})
        super().__init__(f"{code}: {message}")

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, "
            f"http_status={self.http_status})"
        )


class NotFoundError(ServiceError):
    http_status = 404


class ValidationError(ServiceError):
    http_status = 422


class AuthorizationError(ServiceError):
    http_status = 403


class ConflictError(ServiceError):
    http_status = 409


class RateLimitError(ServiceError):
    http_status = 429


class ServiceUnavailableError(ServiceError):
    http_status = 503
