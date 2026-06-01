# v1.0.0 - 2026-05-27 - S1 F3.0: Python MCP server skeleton (FastMCP, stdio, use_cases-direct)
"""Python MCP server — the payoff of the S1 "collapse runtime" refactor.

A single ``FastMCP("pir")`` instance exposes the PiR tools, each calling the
``use_cases`` layer DIRECTLY (no Node, no HTTP, no uvicorn). Claude Code launches
this as a stdio subprocess exactly as it does the Node ``index.mjs`` today:

    python -m core.api.mcp.server

F3.0 ships the SKELETON + the per-tool TEMPLATE on two domains (tasks +
learnings). The remaining 79 Node tools land in later F3 batches that copy the
template in ``tools/tasks.py`` / ``tools/learnings.py``.

SDK contract used (``mcp`` >= 1.12):
  * ``FastMCP(name)`` + ``@mcp.tool()`` decorator. The function docstring becomes
    the tool ``description``; the type hints become the input JSON schema.
  * ``mcp.run()`` defaults to the stdio transport — parity 1:1 with the Node
    server. No HTTP.
  * ``await mcp.list_tools()`` introspects the registered tools (used by the smoke
    test for ``tools/list`` parity against the Node baseline).

The ``mcp`` SDK import is local to this module (and ``main``); the rest of the MCP
package (``_adapter`` / ``tools`` registration logic) does not need the SDK at
import time beyond the decorator binding, so an environment without ``mcp`` can
still import the use_cases and adapter (the smoke test guards on importability).
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from core.api.mcp.tools import register_all

# Module-level singleton: the MCP process is the lifetime container (the
# `app.state` equivalent for a server with no FastAPI `app`). Tools register on
# this at import time so `from core.api.mcp.server import mcp` already carries the
# full tool set — the smoke test introspects it without launching stdio.
mcp = FastMCP("pir")

register_all(mcp)


def main() -> None:
    """Run the MCP server over stdio (the OSS runtime entrypoint).

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
    """
    import asyncio

    from core.api.config import settings
    from core.api.db import close_pool, init_pool
    from core.api.runtime_settings import apply_marvis_settings

    apply_marvis_settings()

    async def _serve() -> None:
        await init_pool(size=settings.db_pool_size)
        try:
            await mcp.run_stdio_async()
        finally:
            await close_pool()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
