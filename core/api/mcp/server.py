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
``fastmcp.FastMCP`` instance with ``StaticTokenVerifier`` and serves
streamable-http on ``127.0.0.1:$MARVIS_MCP_PORT/mcp``. The module-level
``mcp`` singleton stays stdio-only and unauthenticated so local plugin behavior
does not drift.
"""
from __future__ import annotations

import os
from typing import Literal

from mcp.server.fastmcp import FastMCP as StdioFastMCP

from core.api.mcp.tools import register_all

# Module-level singleton: the MCP process is the lifetime container (the
# `app.state` equivalent for a server with no FastAPI `app`). Tools register on
# this at import time so `from core.api.mcp.server import mcp` already carries the
# full tool set — the smoke test introspects it without launching stdio.
_INSTRUCTIONS = (
    "Marvis is a company-brain MCP for cross-project orchestration and institutional memory.\n"
    "Route by task type (prefer these structured tools over re-deriving context from raw files):\n"
    "- Cold-start / 'state of project X': session_brief(slug) — it also suggests the next tool when the project is cross-project.\n"
    "- 'If I pause / close / de-prioritize project X, what blocks?': project_impact(slug) — the project-level blast radius. "
    "(graph_impact on a 'project:artifact:<slug>' node with depends_on does the same.)\n"
    "- 'What breaks if I change function X' (code level): graph_impact / graph_neighbors on a 'py:function:...' node.\n"
    "- 'What do we already know / what bit us before' (decisions and risky actions): check_learnings(q).\n"
    "- Cross-project discovery by meaning: search(q). Body of ONE known project: get_project(slug) — do not layer search on top of get_project.\n"
    "Before answering an orchestration or planning task, confirm you actually called the relevant "
    "tool (session_brief / project_impact / graph_impact) AND that your answer addresses the task "
    "as asked — do not reply from raw files or pivot to an unrelated skill.\n"
    "When you state a number from the knowledge graph (a count, in-degree, how many dependents), "
    "quote it from the tool result's `summary` block and cite it inline — e.g. \"148 edges from 11 "
    "sources [graph_neighbors summary]\". Do NOT re-count a returned list by hand, and do NOT state a "
    "count that no tool result supports.\n"
)


# mcp-ergonomics (tiering): mark the cold-start core always-loaded so clients with
# MCP tool-search (e.g. Claude Code — the agent that drives the brain) keep these in
# context and defer the other ~60 tools on demand. A 70-tool surface is well over the
# ~20-30 threshold where tool-selection accuracy degrades. Additive: clients without
# tool-search still see every tool (no regression). Best-effort — a FastMCP registry
# shape change degrades to a no-op, never a boot failure.
_CORE_TOOLS = frozenset(
    {
        "session_brief",
        "search",
        "check_learnings",
        "list_tasks",
        "get_task",
        "create_task",
        "update_task",
        "get_project",
        "graph_impact",
        "graph_neighbors",
        "project_impact",
    }
)


def _apply_core_tool_meta(server) -> None:
    """Mark cold-start tools as always-loaded when the server supports metadata."""
    try:
        for _name, _tool in server._tool_manager._tools.items():
            if _name in _CORE_TOOLS:
                _m = dict(_tool.meta or {})
                _m["anthropic/alwaysLoad"] = True
                _tool.meta = _m
    except Exception:  # pragma: no cover - FastMCP internal-shape guard
        pass


def _build_stdio_mcp():
    """Build the trusted local stdio MCP server (no auth)."""
    server = StdioFastMCP("marvis", instructions=_INSTRUCTIONS)
    register_all(server)
    _apply_core_tool_meta(server)
    return server


def _build_http_mcp():
    """Build the remote MCP server with per-tenant Bearer auth."""
    from fastmcp import FastMCP as HttpFastMCP
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    token = os.environ.get("TENANT_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TENANT_BEARER_TOKEN is required when MARVIS_MCP_TRANSPORT=http"
        )

    tenant_id = os.environ.get("TENANT_ID", "tenant").strip() or "tenant"
    verifier = StaticTokenVerifier(
        tokens={
            token: {
                "client_id": tenant_id,
                "scopes": ["read:data", "write:data"],
            }
        },
        required_scopes=["read:data"],
    )
    server = HttpFastMCP(name="marvis", instructions=_INSTRUCTIONS, auth=verifier)
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
                await http_mcp.run_http_async(
                    transport="streamable-http",
                    host="127.0.0.1",
                    port=_http_port_from_env(),
                    path="/mcp",
                )
        finally:
            await close_pool()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
