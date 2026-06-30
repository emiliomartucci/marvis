# v1.0.0 - 2026-05-27 - S1 F3.0: Python MCP server skeleton (FastMCP, stdio, use_cases-direct)
"""Python MCP server — the payoff of the S1 "collapse runtime" refactor.

A single ``FastMCP("marvis")`` instance exposes the Marvis tools, each calling the
``use_cases`` layer DIRECTLY (no Node). Claude Code launches this as a stdio
subprocess exactly as it does the Node ``index.mjs`` today:

    python -m core.api.mcp.server

F3.0 ships the SKELETON + the per-tool TEMPLATE on two domains (tasks +
learnings). The remaining 79 Node tools land in later F3 batches that copy the
template in ``tools/tasks.py`` / ``tools/learnings.py``.

SDK contract used (``mcp`` >= 1.27):
  * ``FastMCP(name)`` + ``@mcp.tool()`` decorator. The function docstring becomes
    the tool ``description``; the type hints become the input JSON schema.
  * ``mcp.run()`` defaults to the stdio transport — parity 1:1 with the Node
    server.
  * ``await mcp.list_tools()`` introspects the registered tools (used by the smoke
    test for ``tools/list`` parity against the Node baseline).

Remote hosted tier: ``MARVIS_MCP_TRANSPORT=http`` builds a separate
``fastmcp.FastMCP`` instance with ``StaticTokenVerifier`` by default, or dual
StaticTokenVerifier + WorkOS AuthKit OAuth when ``WORKOS_AUTHKIT_DOMAIN`` and
``MCP_PUBLIC_BASE_URL`` are configured. It serves streamable-http on
``127.0.0.1:$MARVIS_MCP_PORT/mcp``. The module-level ``mcp`` singleton stays
stdio-only and unauthenticated so local plugin behavior does not drift.
"""
from __future__ import annotations

import logging
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP as StdioFastMCP

from core.api.mcp.guidance import (
    apply_tool_metadata_to_server,
    build_instructions,
)
from core.api.mcp.tools import register_all

# Module-level singleton: the MCP process is the lifetime container (the
# `app.state` equivalent for a server with no FastAPI `app`). Tools register on
# this at import time so `from core.api.mcp.server import mcp` already carries the
# full tool set — the smoke test introspects it without launching stdio.
_INSTRUCTIONS = build_instructions()


def _apply_core_tool_meta(server) -> None:
    """Mark cold-start tools as always-loaded when the server supports metadata."""
    try:
        apply_tool_metadata_to_server(server)
    except Exception:  # pragma: no cover - FastMCP internal-shape guard
        pass


def _build_stdio_mcp():
    """Build the trusted local stdio MCP server (no auth)."""
    server = StdioFastMCP("marvis", instructions=_INSTRUCTIONS)
    register_all(server)
    _apply_core_tool_meta(server)
    return server


def _join_url_path(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.strip('/')}"


def _oauth_protected_resource_metadata(
    *,
    public_base_url: str,
    authkit_domain: str,
    mcp_path: str = "/mcp",
    resource_name: str = "Marvis MCP",
) -> dict[str, object]:
    """Build RFC 9728 protected resource metadata for the hosted MCP endpoint."""
    return {
        "resource": _join_url_path(public_base_url, mcp_path),
        "authorization_servers": [authkit_domain.rstrip("/")],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [],
        "resource_name": resource_name,
    }


def _build_http_mcp():
    """Build the remote MCP server with per-tenant Bearer and optional OAuth."""
    from fastmcp import FastMCP as HttpFastMCP
    from fastmcp.server.auth import MultiAuth
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from starlette.responses import JSONResponse

    token = os.environ.get("TENANT_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TENANT_BEARER_TOKEN is required when MARVIS_MCP_TRANSPORT=http"
        )

    tenant_id = os.environ.get("TENANT_ID", "tenant").strip() or "tenant"
    static_verifier = StaticTokenVerifier(
        tokens={
            token: {
                "client_id": tenant_id,
                "scopes": ["read:data", "write:data"],
            }
        },
        required_scopes=["read:data"],
    )
    authkit_domain = os.environ.get("WORKOS_AUTHKIT_DOMAIN", "").strip()
    public_base_url = os.environ.get("MCP_PUBLIC_BASE_URL", "").strip()
    mcp_path = os.environ.get("MARVIS_MCP_PATH", "/mcp").strip() or "/mcp"
    auth = static_verifier

    if authkit_domain:
        if not public_base_url:
            raise RuntimeError(
                "MCP_PUBLIC_BASE_URL is required when WORKOS_AUTHKIT_DOMAIN is set"
            )
        from fastmcp.server.auth.providers.workos import AuthKitProvider

        authkit_provider = AuthKitProvider(
            authkit_domain=authkit_domain,
            base_url=public_base_url,
            # WorkOS AuthKit managed clients reject custom MCP scopes unless RBAC
            # is configured. AuthKit security is aud/resource + allowlist here.
            required_scopes=[],
            scopes_supported=[],
            resource_name=f"Marvis brain - {tenant_id}",
        )
        authkit_provider.set_mcp_path(mcp_path)
        auth = MultiAuth(server=authkit_provider, verifiers=[static_verifier])
        auth.set_mcp_path(mcp_path)

    server = HttpFastMCP(name="marvis", instructions=_INSTRUCTIONS, auth=auth)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # noqa: ANN001
        return JSONResponse(
            {
                "status": "ok",
                "service": "marvis-mcp",
                "transport": "http",
                "tenant": tenant_id,
                "db_path": os.environ.get("MARVIS_DB_PATH")
                or os.environ.get("PIR_DB_PATH"),
                "projects_root": os.environ.get("MARVIS_PROJECTS_ROOT"),
            }
        )

    if authkit_domain:

        @server.custom_route(
            "/.well-known/oauth-protected-resource", methods=["GET"]
        )
        async def oauth_protected_resource(_request):  # noqa: ANN001
            return JSONResponse(
                _oauth_protected_resource_metadata(
                    public_base_url=public_base_url,
                    authkit_domain=authkit_domain,
                    mcp_path=mcp_path,
                    resource_name=f"Marvis brain - {tenant_id}",
                )
            )

    register_all(server)
    _apply_core_tool_meta(server)
    return server


mcp = _build_stdio_mcp()


def _transport_from_env() -> Literal["stdio", "http"]:
    raw = os.environ.get("MARVIS_MCP_TRANSPORT", "stdio").strip().lower()
    if raw in {"", "stdio"}:
        return "stdio"
    if raw in {"http", "streamable-http", "streamable_http"}:
        return "http"
    raise SystemExit(
        "Unsupported MARVIS_MCP_TRANSPORT="
        f"{raw!r}; expected 'stdio' or 'http'"
    )


def _http_port_from_env() -> int:
    raw = os.environ.get("MARVIS_MCP_PORT", "8100").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit(f"MARVIS_MCP_PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"MARVIS_MCP_PORT must be between 1 and 65535, got {port}")
    return port


def _http_stateless_from_env() -> bool:
    """Use stateless Streamable HTTP for hosted tenants by default.

    Hosted MCP clients can keep a stale ``Mcp-Session-Id`` after a tenant restart.
    FastMCP's stateful transport returns ``Session not found`` before it processes
    a fresh initialize request, which makes the connector look permanently
    broken. Stateless HTTP creates a fresh transport per request and ignores stale
    client session IDs. Keep self-hosted/local defaults unchanged unless an env
    override is explicit.
    """
    for name in ("MARVIS_MCP_STATELESS_HTTP", "FASTMCP_STATELESS_HTTP"):
        raw = os.environ.get(name)
        if raw is not None:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("DEPLOY_MODE", "").strip() == "hosted-tenant"


class _DropStatelessTerminateNone(logging.Filter):
    """Hide FastMCP's per-request stateless cleanup log, keep real terminations."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != "Terminating session: None"


def _suppress_stateless_terminate_none_log() -> None:
    logger = logging.getLogger("mcp.server.streamable_http")
    if any(isinstance(f, _DropStatelessTerminateNone) for f in logger.filters):
        return
    logger.addFilter(_DropStatelessTerminateNone())


def main(transport: Literal["stdio", "http"] | None = None) -> None:
    """Run the MCP server.

    Mirror the user's ``~/.marvis/settings.yaml`` onto the API ``settings``
    singleton + project-index roots BEFORE serving any tool, so ``search`` /
    ``graph_*`` reach the SAME SQLite file the ``marvis`` CLI uses (instead of the
    bare ``db_path='console.db'`` default). Best-effort: no settings file → the
    API defaults / ``$PIR_DB_PATH`` env stand (parity with the CLI runtime).

    Then open the DB the SAME way the FastAPI lifespan does — ``init_pool()``
    creates the read-only pool AND the single dedicated writer. This is NOT
    optional for write tools: ``acquire_db()`` has a no-pool fallback (so reads
    answer even if the pool is absent), but ``acquire_write_db()`` raises
    ``"DB not initialized — call init_pool() first"`` when the writer is None.
    Without this, every mutator (``create_task``, ``update_task``, ...) failed —
    the agent could read the brain via MCP but never write it back.

    init + serve + close run in ONE event loop: aiosqlite connections are bound
    to the running loop, so opening the pool in a separate ``asyncio.run()`` pass
    before ``mcp.run()`` would leave the writer attached to an already-closed loop.

    Default transport stays stdio. HTTP is opt-in via
    ``MARVIS_MCP_TRANSPORT=http`` (or the caller passing ``transport="http"``) and
    binds a streamable-http endpoint to ``127.0.0.1:$MARVIS_MCP_PORT/mcp`` with
    per-tenant Bearer auth from ``TENANT_BEARER_TOKEN``.
    """
    import asyncio

    from core.api.config import settings
    from core.api.db import close_pool, init_pool
    from core.api.mcp import _adapter as mcp_adapter
    from core.api.runtime_settings import apply_marvis_settings

    apply_marvis_settings()
    selected_transport = transport or _transport_from_env()
    mcp_adapter.set_tool_error_runtime(
        "mcp" if selected_transport == "stdio" else "fastmcp"
    )

    async def _serve() -> None:
        await init_pool(size=settings.db_pool_size)
        try:
            if selected_transport == "stdio":
                await mcp.run_stdio_async()
            else:
                http_mcp = _build_http_mcp()
                stateless_http = _http_stateless_from_env()
                if stateless_http:
                    _suppress_stateless_terminate_none_log()
                await http_mcp.run_http_async(
                    transport="streamable-http",
                    host="127.0.0.1",
                    port=_http_port_from_env(),
                    path="/mcp",
                    stateless_http=stateless_http,
                )
        finally:
            await close_pool()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
