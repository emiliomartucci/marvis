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
untiered tool requires ``admin`` (see ``guidance.min_role_for_tool``). STDIO is
skipped here (the SDK stdio server has no middleware). Enterprise strict mode
accepts bound OIDC people and service agents; service agents are capped at
operator. Graph traversal remains admin-only until its retrieval predicate is
permission-aware.

Kill-switch: ``MARVIS_TOOL_PROFILES=0`` disables the hosted role gate (the usage
counter still runs). Enterprise strict mode always enforces the role gate.

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

from core.api.observability import phase_timing
from core.api.mcp.guidance import TOOL_DIET_CORE, min_role_for_tool
from core.api.mcp.tool_gateway_names import (
    FIND_TOOL,
    GATEWAY_TOOLS,
    find_tool_description,
    gateway_enabled,
)
from core.api.services.repository_authority import (
    REPOSITORY_TOOL_ROUTE,
    RETIRED_REPOSITORY_TOOLS,
)
from core.api.use_cases._roles import ROLE_HIERARCHY

logger = logging.getLogger("marvis.tool_profiles")

_FALSEY = {"0", "false", "no", "off"}
_VIEWER_RANK = ROLE_HIERARCHY["viewer"]
_ENTERPRISE_GRAPH_TOOLS = {
    "project_impact", "list_graph_pins", "pin_graph_node", "unpin_graph_node",
    "mint_graph_ingest_token", "kg_reindex_path",
}
_ENTERPRISE_CLOUD_F_LEASE_TOOLS = {
    "get_cloud_f_control", "acquire_cloud_f_change", "complete_cloud_f_change",
    "get_cloud_f_change_operation",
}


def _enterprise_strict() -> bool:
    return os.environ.get("MARVIS_ENTERPRISE_OIDC_STRICT", "").strip() == "1"


def _required_role(tool_name: str) -> str:
    # The gateway itself is open to every role: find_tool only returns, and
    # call_tool only runs, tools the caller's own role may call.
    if tool_name in GATEWAY_TOOLS:
        return "viewer"
    # Enterprise provisions bound Entra identities locally; the replacement
    # add_user/list_user_requests implementation requires a tenant admin.
    if _enterprise_strict() and tool_name in {"add_user", "list_user_requests", "list_access"}:
        return "admin"
    # The Cloud/F lease is one tenant-wide lock with no project boundary; a
    # scoped operator must not hold or inspect it.
    if _enterprise_strict() and tool_name in _ENTERPRISE_CLOUD_F_LEASE_TOOLS:
        return "admin"
    # The bug intake token is tenant-scoped. Individual reporter ownership is
    # unavailable, so its readback must match the runtime tenant-admin gate.
    if _enterprise_strict() and tool_name in {"bug_status", "list_bug_reports"}:
        return "admin"
    # Graph traversal/ranking still reads hidden nodes before the response
    # filter. Keep the full graph surface closed to scoped Enterprise actors
    # until its SQL/traversal accepts the same project/path predicate as search.
    if _enterprise_strict() and (
        tool_name.startswith("graph_") or tool_name in _ENTERPRISE_GRAPH_TOOLS
    ):
        return "admin"
    return min_role_for_tool(tool_name)

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
    viewer, mai default-allow"). A PRESENT token resolves through the shared
    ``current_mcp_context``. Enterprise strict mode validates its bound OIDC
    principal separately before this role is used.
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


def _enterprise_principal_role() -> str | None:
    """Return a bound OIDC principal's maximum tool role, or deny the principal.

    The adapter proves the user row is active on every request. This transport
    gate preserves that proof's mechanism and caps service agents even if a
    future adapter accidentally supplies an administrative role.
    """
    principal = _current_mcp_context()
    if not getattr(principal, "workspace_id", None) or not getattr(principal, "user_id", None):
        return None
    mechanism = getattr(principal, "auth_mechanism", None)
    role = getattr(principal, "system_role", None)
    if mechanism == "mcp_oauth_person":
        return role if isinstance(role, str) and role else "viewer"
    if mechanism == "mcp_oauth_agent" and getattr(principal, "user_type", None) == "agent":
        return "operator" if role in {"operator", "admin", "super_admin"} else "viewer"
    tenant_id = os.environ.get("TENANT_ID", "").strip()
    if (
        mechanism == "mcp_static" and tenant_id
        and getattr(principal, "user_type", None) == "agent"
        and principal.user_id == f"tenant:{tenant_id}"
        and role == "admin"
    ):
        return "admin"
    return None


def is_authorized(tool_name: str, role: str) -> bool:
    """True when ``role`` may see/call ``tool_name`` under the tier map."""
    return _role_rank(role) >= _role_rank(_required_role(tool_name))


def diet_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Core-tool diet is ON for every tenant (owner decision 2026-09-30).

    ``MARVIS_TOOL_DIET=0`` opts a tenant out. The diet keeps only
    ``guidance.TOOL_DIET_CORE`` in ``tools/list`` after the role filter.
    ``tools/call`` of a hidden tool still succeeds, but agents never call a tool
    they cannot see (0 of 51 requests in task 2951f474), so the hidden tools
    stay reachable only through the tool gateway (``find_tool``/``call_tool``),
    listed next to the diet. Rollout history: shipped default-off on
    2026-09-29 behind an explicit env, measured for a day on the main agent
    tenant, then made the fleet default.
    """
    source = os.environ if env is None else env
    raw = source.get("MARVIS_TOOL_DIET")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def _diet_filter(tools: list) -> list:  # noqa: ANN001
    """Trim to the diet core; add the gateway, naming the tools it hides."""
    kept = [t for t in tools if getattr(t, "name", "") in TOOL_DIET_CORE]
    if not gateway_enabled():
        return kept
    hidden = [
        t.name for t in tools
        if t.name not in TOOL_DIET_CORE and t.name not in GATEWAY_TOOLS
    ]
    for tool in tools:
        if tool.name == FIND_TOOL:
            kept.append(tool.model_copy(update={"description": find_tool_description(hidden)}))
        elif tool.name in GATEWAY_TOOLS:
            kept.append(tool)
    return kept


def caller_may_call(tool_name: str) -> bool:
    """Whether the current HTTP caller's role passes the gate for ``tool_name``.

    Mirrors ``ToolProfilesMiddleware.on_call_tool`` for the gateway's search, so
    ``find_tool`` never names a tool the caller would be denied.
    """
    if _enterprise_strict():
        try:
            role = _enterprise_principal_role()
        except Exception:
            return False
        return role is not None and is_authorized(tool_name, role)
    if not profiles_enabled():
        return True
    return is_authorized(tool_name, _caller_role())


class ToolProfilesMiddleware(Middleware):
    """Filter ``tools/list`` and gate ``tools/call`` by role; log every call."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

    async def on_list_tools(self, context, call_next):  # noqa: ANN001
        tools = await call_next(context)
        if self._is_stdio():
            return tools
        strict_role = None
        if _enterprise_strict():
            try:
                strict_role = _enterprise_principal_role()
            except Exception:
                return []
            if strict_role is None:
                return []
        role = strict_role if strict_role is not None else _caller_role()
        if self.enabled or _enterprise_strict():
            tools = [t for t in tools if is_authorized(getattr(t, "name", ""), role)]
        # P4 tool diet: trim the role-filtered list to the data-driven core.
        # SOFT by design — on_call_tool never checks TOOL_DIET_CORE, so a
        # hidden tool stays callable for flows that name it explicitly.
        if diet_enabled():
            tools = _diet_filter(tools)
        else:
            tools = [t for t in tools if getattr(t, "name", "") not in GATEWAY_TOOLS]
        return tools

    async def on_call_tool(self, context, call_next):  # noqa: ANN001
        tool_name = getattr(context.message, "name", "") or ""
        is_stdio = self._is_stdio()
        strict_role = None
        if not is_stdio and _enterprise_strict():
            try:
                strict_role = _enterprise_principal_role()
                if strict_role is None:
                    raise RuntimeError("Enterprise identity is not active")
            except Exception as exc:
                self._log_call(tool_name, "unbound", 0.0, ok=False, denied=True)
                raise AuthorizationError("Enterprise identity is not active") from exc
        role = "local" if is_stdio else strict_role if strict_role is not None else _caller_role()

        if (self.enabled or _enterprise_strict()) and not is_stdio:
            required = _required_role(tool_name)
            if _role_rank(role) < _role_rank(required):
                self._log_call(tool_name, role, 0.0, ok=False, denied=True)
                raise AuthorizationError(
                    f"Tool '{tool_name}' requires role '{required}'; "
                    f"your role is '{role}'."
                )

        start = time.monotonic()
        ok = True
        phases_token = phase_timing.begin()
        try:
            return await call_next(context)
        except Exception:
            ok = False
            raise
        finally:
            self._log_call(
                tool_name,
                role,
                (time.monotonic() - start) * 1000.0,
                ok=ok,
                denied=False,
                phases=phase_timing.end(phases_token),
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
        tool: str,
        role: str,
        ms: float,
        *,
        ok: bool,
        denied: bool,
        phases: dict[str, float] | None = None,
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
        if phases:
            payload["phases"] = {name: round(value, 1) for name, value in phases.items()}
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
