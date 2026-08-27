# v1.0.0 - 2026-07-03 - P3 tool profiles RBAC: per-role tool exposure middleware.
"""Role-based tool exposure for the hosted HTTP MCP server.

The tier map in :mod:`core.api.mcp.guidance` doubles as the minimum ``system_role``
each tool requires. This middleware turns that map into live exposure:

* ``tools/list`` hides every tool above the caller's role — the tool never
  appears, so there is no context tax and no tool-confusion for the model.
* ``tools/call`` of a hidden tool is denied with an EXPLICIT
  ``requires role '<role>'`` message (never a generic "unknown tool"). The
  per-tool ``tool.auth`` primitive cannot deliver this: fastmcp's ``_get_tool``
  collapses a failed component check to ``None`` and the caller sees a generic
  not-found error. Gating at the middleware layer lets the ``AuthorizationError``
  message reach the client verbatim.

Fail-CLOSED by contract: an unresolvable role is treated as ``viewer`` and an
untiered tool requires ``admin`` (see ``guidance.min_role_for_tool``). STDIO and
the static tenant Bearer are unaffected — stdio is skipped here (and never wired,
since the SDK stdio server has no middleware), and the Bearer resolves to
``admin`` in :func:`current_mcp_context`.

Kill-switch: ``MARVIS_TOOL_PROFILES=0`` disables the role gate (the usage counter
still runs) for instant per-tenant rollback via env + restart.

Usage counter (P3 F4): every ``tools/call`` emits one structured journald line
``marvis.tool_call {tool, role, user, ms, ok}`` — the data for the future
data-driven diet — regardless of whether the gate is enabled.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.middleware.middleware import Middleware

from core.api.mcp.guidance import min_role_for_tool
from core.api.services.repository_authority import (
    REPOSITORY_TOOL_ROUTE,
    RETIRED_REPOSITORY_TOOLS,
)
from core.api.use_cases._roles import ROLE_HIERARCHY

logger = logging.getLogger("marvis.tool_profiles")

_FALSEY = {"0", "false", "no", "off"}
_VIEWER_RANK = ROLE_HIERARCHY["viewer"]

def _current_mcp_context():
    """Resolve request identity without loading the application during import.

    The root-owned public gateway imports this module only for the retired-tool
    middleware.  Importing ``_adapter`` eagerly also initializes DB settings,
    which is both unnecessary at that boundary and invalid under the gateway's
    root service environment.  Backend-only identity resolution stays lazy.
    """
    from core.api.mcp._adapter import current_mcp_context

    return current_mcp_context()


def profiles_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Role gate is ON unless ``MARVIS_TOOL_PROFILES`` is explicitly falsey."""
    source = os.environ if env is None else env
    raw = source.get("MARVIS_TOOL_PROFILES")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def _role_rank(role: str | None) -> int:
    """Rank a role; fail-closed to viewer (lowest) for unknown/None."""
    return ROLE_HIERARCHY.get(role or "", _VIEWER_RANK)


def _caller_role() -> str:
    """Resolve the caller's ``system_role`` on the HTTP surface, fail-closed to viewer.

    This middleware only runs on non-stdio transports, so a MISSING access token
    means an unauthenticated / unresolvable caller → ``viewer`` — never the
    ``LOCAL_CTX`` operator default, which is a trusted stdio-only identity and
    would be a silent fail-open here (reviewed constraint #1: "claim assente →
    viewer, mai default-allow"). A PRESENT token (OAuth person OR the static
    tenant Bearer) resolves through the shared ``current_mcp_context`` (Bearer →
    admin, OAuth → claim/DB role, else viewer).
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        if get_access_token() is None:
            return "viewer"
        role = getattr(_current_mcp_context(), "system_role", None)
        return role if isinstance(role, str) and role else "viewer"
    except Exception:  # pragma: no cover - defense in depth
        logger.warning(
            "tool_profiles: role resolution failed; treating as viewer",
            exc_info=True,
        )
        return "viewer"


def is_authorized(tool_name: str, role: str) -> bool:
    """True when ``role`` may see/call ``tool_name`` under the tier map."""
    return _role_rank(role) >= _role_rank(min_role_for_tool(tool_name))


class ToolProfilesMiddleware(Middleware):
    """Filter ``tools/list`` and gate ``tools/call`` by role; log every call."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

    async def on_list_tools(self, context, call_next):  # noqa: ANN001
        tools = await call_next(context)
        if not self.enabled or self._is_stdio():
            return tools
        role = _caller_role()
        return [t for t in tools if is_authorized(getattr(t, "name", ""), role)]

    async def on_call_tool(self, context, call_next):  # noqa: ANN001
        tool_name = getattr(context.message, "name", "") or ""
        is_stdio = self._is_stdio()
        role = "local" if is_stdio else _caller_role()

        if self.enabled and not is_stdio:
            required = min_role_for_tool(tool_name)
            if _role_rank(role) < _role_rank(required):
                self._log_call(tool_name, role, 0.0, ok=False, denied=True)
                raise AuthorizationError(
                    f"Tool '{tool_name}' requires role '{required}'; "
                    f"your role is '{role}'."
                )

        start = time.monotonic()
        ok = True
        try:
            return await call_next(context)
        except Exception:
            ok = False
            raise
        finally:
            self._log_call(
                tool_name, role, (time.monotonic() - start) * 1000.0, ok=ok, denied=False
            )

    @staticmethod
    def _is_stdio() -> bool:
        try:
            from fastmcp.server.context import _current_transport

            return _current_transport.get() == "stdio"
        except Exception:  # pragma: no cover
            return False

    @staticmethod
    def _log_call(
        tool: str, role: str, ms: float, *, ok: bool, denied: bool
    ) -> None:
        user = "?"
        try:
            user = getattr(_current_mcp_context(), "user_id", "?") or "?"
        except Exception:  # pragma: no cover
            pass
        payload: dict[str, Any] = {
            "tool": tool,
            "role": role,
            "user": user,
            "ms": round(ms, 1),
            "ok": ok,
        }
        if denied:
            payload["denied"] = True
        logger.info("marvis.tool_call %s", json.dumps(payload, sort_keys=True))


class RetiredRepositoryToolsMiddleware(Middleware):
    """Route removed repository lifecycle calls before provider lookup.

    The public root gateway owns the first FastMCP lookup in hosted deployments.
    A fallback wired only on the private backend therefore cannot see a removed
    tool name: the proxy provider converts it to ``Unknown tool`` first.  Keep
    this middleware independent from role filtering so the gateway can install
    the route without duplicating backend authorization or usage accounting.
    """

    async def on_call_tool(self, context, call_next):  # noqa: ANN001
        tool_name = getattr(context.message, "name", "") or ""
        if tool_name in RETIRED_REPOSITORY_TOOLS:
            role = (
                "local"
                if ToolProfilesMiddleware._is_stdio()
                else _caller_role()
            )
            ToolProfilesMiddleware._log_call(
                tool_name, role, 0.0, ok=False, denied=True
            )
            raise ToolError(REPOSITORY_TOOL_ROUTE)
        return await call_next(context)


def apply_retired_repository_routes(server: Any) -> None:
    """Install the removed-tool route on the current FastMCP entry point."""
    server.add_middleware(RetiredRepositoryToolsMiddleware())


def apply_tool_profiles(server: Any) -> None:
    """Wire the role gate + usage counter onto an HTTP MCP server.

    MUST be called OUTSIDE the metadata try/except so a wiring failure RAISES at
    boot (fail-closed: never a silent unprotected start). STDIO servers never call
    this — the SDK stdio ``FastMCP`` has no middleware/per-tool auth and stays a
    trusted local surface.
    """
    enabled = profiles_enabled()
    apply_retired_repository_routes(server)
    server.add_middleware(ToolProfilesMiddleware(enabled=enabled))
    logger.info(
        "tool_profiles middleware wired (role_gate=%s)", "on" if enabled else "off"
    )
