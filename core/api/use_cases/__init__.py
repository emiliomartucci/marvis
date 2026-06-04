# v1.0.0 - 2026-05-27 - S1 F0: scaffold use_cases layer (fastapi-free domain logic)
"""Application use_cases layer.

Domain logic extracted from FastAPI routers as pure async functions. This
package MUST NOT import ``fastapi`` (enforced by the import-linter contract
``use_cases-must-not-import-fastapi`` in ``.importlinter``): identity is carried
as a typed :class:`~core.api.use_cases._context.CallerContext` value and errors
are raised as :class:`~core.api.use_cases._errors.ServiceError` instead of
``HTTPException``. Both the HTTP router adapters and the Python MCP server call
these same functions, so there is a single implementation, no fork.
"""
