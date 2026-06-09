# v1.1.0 - 2026-05-27 - S1 F3.0: add LOCAL_CTX singleton + db/result wiring helpers (MCP server skeleton)
"""MCP adapter: domain<->MCP wiring for the Python MCP server.

Two responsibilities, both transport-thin:

1. ``raise_mcp_error(ServiceError)`` — maps a domain :class:`ServiceError` to the
   SDK-native tool-error channel by RAISING ``ToolError(f"{code}: {message}")``.
   FastMCP catches the raised ``ToolError`` and emits a real
   ``CallToolResult(isError=True, content=[text=...])`` — the only mechanism a
   client (Claude Code) recognises as a tool error. The MCP surface ignores
   ``err.http_status`` (HTTP is not its transport) and maps ``code`` + ``message``
   to the error text. This is uniform across EVERY tool regardless of return type:
   raising bypasses FastMCP's structured-output validation, so it also works for
   the ``-> list`` tools (which could not return the old error dict — it failed the
   ``list`` output schema). See ``to_mcp_error`` below for why the returned-dict
   shape was abandoned (verified empirically: a returned dict is success DATA, not
   a protocol ``isError``).
2. The single-user wiring the tools share: :data:`LOCAL_CTX` (the local operator
   identity), :func:`dump` (DTO/dict normaliser), and the db acquire helpers
   re-exported so ``tools/*.py`` import db access from one place.

The ``ToolError`` import is local to :func:`raise_mcp_error` so the rest of the
module (``LOCAL_CTX`` + the seam callables) stays testable without the ``mcp`` SDK
installed (parity with ``server.py``'s function-local SDK import).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, NoReturn

from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

logger = logging.getLogger(__name__)

# Re-export the db context managers so tools import db access from the adapter,
# not from deep in the api package. ``acquire_db`` = read pool; ``acquire_write_db``
# = writer + lock (mutators). Both are @asynccontextmanager importable directly.
from core.api.db import acquire_db, acquire_write_db  # noqa: F401  (re-export)

# ---------------------------------------------------------------------------
# Single-user local identity (OSS: no JWT, no auth).
# ---------------------------------------------------------------------------
# The MCP process IS the lifetime container, so this is a module singleton (the
# `app.state` equivalent for a process with no FastAPI `app`). Every tool calls
# the SAME use_cases the HTTP adapter calls, only the CallerContext fill differs:
# HTTP fills from UserInfo; MCP fills with this local operator. One implementation,
# no fork. `is_human_session=True` -> the four-eyes approval gate is a local no-op
# by design (documented S1 §AUTH: single-user collapses four-eyes).
LOCAL_CTX: CallerContext = CallerContext.local_single_user()


# ---------------------------------------------------------------------------
# MCP-local seam callables (the fastapi-free counterparts of the router seams).
# ---------------------------------------------------------------------------
# create_task / update_task use_cases take the side-effect hooks
# (sync_graph / schedule_embed / requires_pr_gate) as injected callables — the
# "costs programs_loader" seam — so the domain stays fastapi-free. The HTTP
# router injects ITS versions (which live in routers/tasks.py and pull fastapi
# for the test-seam machinery). The MCP surface MUST NOT import the router (that
# would drag fastapi into the collapsed single-process runtime), so it injects
# these MCP-local seams instead:
#
#   * mcp_sync_graph     -> graph_service.sync_task_to_graph (already fastapi-free,
#                           same KG node emit the HTTP surface does — no divergence).
#   * mcp_schedule_embed -> in-process auto-embed (S1 F4). Fires the SAME
#                           fastapi-free embed body the HTTP surface uses
#                           (embedding_service.embed_task_document) fire-and-forget
#                           on the running tool loop — no Node, no HTTP, no fork. The
#                           use_case calls this sync seam un-awaited (side effect),
#                           so it schedules a background task and returns immediately;
#                           the embed (incl. the slow model/remote backend call) runs OUTSIDE
#                           any write lock, then writes via the single-writer pool.
#                           No-ops gracefully when the embedder is unavailable.
#   * mcp_requires_pr_gate -> returns False locally. The completed-PR gate (code/
#                           system tasks must complete via a merged PR) is a
#                           governance chokepoint meaningful only with Console/Triage;
#                           in single-user OSS it collapses to a no-op, the SAME
#                           per-surface trade-off documented for the four-eyes
#                           approval gate (S1 §AUTH). The gate stays enforced on the
#                           HTTP surface for the paid tiers.
# Background task set (prevents GC of fire-and-forget embed coroutines) — the MCP
# process is the lifetime container, so this is a module-level set, the same pattern
# the HTTP router uses (routers.tasks._bg_embed_tasks). asyncio holds only a weak
# reference to a bare create_task() result, so an un-held task can be collected
# mid-flight; keeping it here until done prevents that.
_bg_embed_tasks: set[asyncio.Task] = set()


def mcp_schedule_embed(
    *,
    task_id: str,
    title: str,
    project: str,
    status: str,
    workspace_id: str,
    **_ignored: Any,
) -> None:
    """In-process auto-embed seam for the local MCP surface (S1 F4).

    Sync callable: the create/update task use_case invokes it un-awaited for its
    side effect. There is a running event loop when a tool calls it (tools are
    async), so we schedule the SHARED fastapi-free embed body
    (``embedding_service.embed_task_document``) fire-and-forget — the exact helper
    the HTTP router runs, just without FastAPI in the path. No-ops gracefully when
    the embedder is unavailable (mirrors the router's ``is_available()`` guard).
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        return

    async def _embed() -> None:
        try:
            await embedding_service.embed_task_document(
                task_id=task_id,
                title=title,
                project=project,
                status=status,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.debug(
                "MCP auto-embed task %s failed (non-critical)", task_id, exc_info=True
            )

    try:
        t = asyncio.create_task(_embed())
    except RuntimeError:
        # No running loop (e.g. a sync caller outside an async context). Auto-embed
        # is a non-critical side effect; skip rather than crash the mutation.
        logger.debug("MCP auto-embed skipped: no running event loop")
        return
    _bg_embed_tasks.add(t)
    t.add_done_callback(_bg_embed_tasks.discard)


# Background set for the remote-backend fire-and-forget learning embeds (the local
# backend awaits inline instead — see below). Same GC-prevention pattern as
# ``_bg_embed_tasks``.
_bg_embed_learnings: set[asyncio.Task] = set()


async def mcp_embed_learning(
    *,
    learning_id: str,
    title: str,
    description: str,
    category: str,
    severity: str,
    prevention: str | None = None,
    project: str | None = None,
    workspace_id: str,
) -> None:
    """Backend-aware embed-on-write for learnings on the local MCP surface.

    The learning analogue of ``mcp_schedule_embed``, but ASYNC + backend-aware: the
    product decision is "embed synchronously on write", and a local Granite embed
    (sub-second) can honor it — it is awaited inline so the just-created learning is
    immediately retrievable by meaning, not just by keyword. A rate-limited remote
    backend (remote, 3 RPM) must not block the create -> fire-and-forget instead.
    Either way the SHARED fastapi-free body (``embedding_service.embed_learning_document``)
    runs the embed OUTSIDE the write lock. The caller (the ``create_learning`` tool)
    invokes this AFTER its ``acquire_write_db`` block has released the writer lock, so
    the inline await cannot deadlock the non-reentrant single-writer lock (learning
    f83f5209). No-ops when the embedder is unavailable.
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        return

    async def _embed() -> None:
        try:
            await embedding_service.embed_learning_document(
                learning_id=learning_id,
                title=title,
                description=description,
                category=category,
                severity=severity,
                prevention=prevention,
                project=project,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.debug(
                "MCP auto-embed learning %s failed (non-critical)",
                learning_id,
                exc_info=True,
            )

    if embedding_service.embedding_is_synchronous():
        # Local Granite: await inline -> synchronous, immediately retrievable.
        await _embed()
        return

    # Remote / rate-limited backend: fire-and-forget so the create never blocks.
    try:
        t = asyncio.create_task(_embed())
    except RuntimeError:
        logger.debug("MCP auto-embed learning skipped: no running event loop")
        return
    _bg_embed_learnings.add(t)
    t.add_done_callback(_bg_embed_learnings.discard)


def mcp_requires_pr_gate(_project: str | None) -> bool:
    """Local PR-gate seam: governance gate collapses in single-user OSS (S1 §AUTH)."""
    return False


def raise_mcp_error(err: ServiceError) -> NoReturn:
    """Surface a domain ``ServiceError`` as a real MCP tool error by RAISING.

    Raises ``mcp.server.fastmcp.exceptions.ToolError(f"{err.code}: {err.message}")``.
    FastMCP catches this in its ``call_tool`` path and returns a native
    ``CallToolResult(isError=True, content=[TextContent(text="<code>: <message>")])``
    — the SDK-native error channel a client (Claude Code) recognises as a tool
    error.

    Why RAISE and not return a dict (verified empirically, S1 F3.2): a *returned*
    value is success DATA — FastMCP places it in ``structuredContent`` with
    ``isError=False``, so a returned ``{"isError": True, ...}`` dict is silently a
    SUCCESS the client must manually inspect (it never trips the protocol flag).
    Worse, on the seven ``-> list[dict]`` tools that returned dict fails FastMCP's
    structured-output validation against the ``list`` type and the client gets a
    Pydantic validation error instead of the domain message. Raising fixes both:
    it emits a real ``isError`` for every tool AND bypasses output validation, so
    ``-> dict`` and ``-> list`` tools surface errors identically.

    The ``ToolError`` import is function-local so importing this module needs no
    ``mcp`` SDK (parity with ``server.py``).
    """
    from mcp.server.fastmcp.exceptions import ToolError

    raise ToolError(f"{err.code}: {err.message}")


def dump(result: Any) -> Any:
    """Normalise a use_case return into an MCP-serialisable structure.

    Mutators return Pydantic DTOs (``.model_dump()``); read tools may already
    return plain dict/list. Lists of DTOs are mapped element-wise. This keeps the
    per-tool body a one-liner and centralises the DTO->dict decision (S1 F3
    return-typing rule: ``dict[str, Any]`` for heterogeneous reads, DTO dumps for
    mutators).
    """
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, list):
        return [r.model_dump() if hasattr(r, "model_dump") else r for r in result]
    return result
