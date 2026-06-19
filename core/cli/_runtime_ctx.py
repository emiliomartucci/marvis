# v1.0.0 - 2026-05-27 - S2 F1: shared runtime helpers for the thin marvis CLI
"""Shared plumbing for the ``marvis`` runtime subcommands (status/brief/triage/...).

The runtime commands are thin terminal adapters over the S1 ``use_cases`` layer:
no HTTP, no token. Every command follows the same shape — build the local
single-user :class:`CallerContext`, open a DB context, call the (async) use_case,
serialize the result. This module is the ONE place that:

- opens an asyncio loop (:func:`run_async`),
- holds the local identity singleton (:data:`LOCAL_CTX`),
- wires read/write DB access (:func:`with_db` / :func:`with_write_db`),
- emits output with ``--json`` purity (:func:`emit`),
- maps :class:`ServiceError` to a clean exit code (:func:`handle_service_error`).

Design constraints (S1/S2 learnings, non-negotiable):

- **Writes go through ``acquire_write_db`` only.** ``acquire_db()`` opens a
  ``query_only=ON`` connection — an INSERT/UPDATE through it raises. The read
  vs write split is honored by :func:`with_db` vs :func:`with_write_db`.
- **Lazy imports.** Heavy modules (``core.api.use_cases.*``, embedding) are
  imported INSIDE the command bodies, never at module top, so ``marvis --help``
  and ``marvis status`` stay fast and never trigger a model load.
- **``--json`` purity.** When ``--json`` is set, stdout carries ONLY valid JSON.
  Rich tables and every human message/warning go to stderr, so
  ``marvis brief slug --json | jq`` never breaks.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import typer
from rich.console import Console

from core.api.use_cases._context import CallerContext

# Human output → stdout (suppressed under --json); warnings/logs → stderr.
console = Console(stderr=False)
err_console = Console(stderr=True)

# Local single-user identity, reused across every runtime command. No token,
# no JWT — this is the OSS local operator (human session, four-eyes collapsed).
LOCAL_CTX: CallerContext = CallerContext.local_single_user()

# A few commands (audit trail) need the full local view: the OSS single user is
# the sole operator, so there is no second permission model to protect against.
# ``_authorize_audit_read`` narrows plain operators to a learnings-only slice;
# the local CLI legitimately reads its own complete trail, so it elevates here.
LOCAL_ADMIN_CTX: CallerContext = CallerContext(
    username="local",
    system_role="super_admin",
    user_type="human",
    is_human_session=True,
    user_id="local",
)

T = TypeVar("T")

def _apply_settings() -> None:
    """Point the runtime at the user's configured DB + projects_root (once).

    Thin delegate to :func:`core.api.runtime_settings.apply_marvis_settings`, the
    single shared implementation the stdio MCP server also calls, so the CLI and
    the MCP surface never diverge on which SQLite file / projects_root they use.
    Best-effort: if no settings file exists the API defaults / env vars stand
    (lets tests and ad-hoc runs work with ``$PIR_DB_PATH``).
    """
    from core.api.runtime_settings import apply_marvis_settings

    apply_marvis_settings()


def run_async(coro: Awaitable[T]) -> T:
    """Run an async use_case from a sync Typer command — the ONLY loop opened.

    Use-cases stay purely ``async``; nesting a second ``asyncio.run`` inside one
    would explode, so every command funnels through here.
    """
    return asyncio.run(coro)


@asynccontextmanager
async def with_db() -> AsyncIterator[Any]:
    """Read-only DB context (``query_only=ON`` pool/connection). For reads only."""
    _apply_settings()
    from core.api.db import acquire_db

    async with acquire_db() as db:
        yield db


@asynccontextmanager
async def with_write_db() -> AsyncIterator[Any]:
    """Writer DB context (single-writer + lock). For mutations (e.g. approve).

    ``acquire_write_db`` requires the writer connection to exist; outside the
    FastAPI lifespan we lazily initialize the single-writer pool the first time
    a write is needed.
    """
    _apply_settings()
    from core.api import db as db_mod

    if db_mod._writer is None:  # lazy one-shot init for the CLI process
        await db_mod.init_pool()

    async with db_mod.acquire_write_db(label="marvis-cli") as db:
        yield db


def emit(result: Any, *, json_out: bool, render: Callable[[Any], None] | None = None) -> None:
    """Serialize a use_case result.

    - ``json_out=True`` → write ONLY ``json.dumps(result)`` to **stdout** (no Rich
      boxes, no log lines), so the output pipes cleanly into ``jq``.
    - otherwise → call ``render`` (Rich table on ``console`` / stdout). Human
      warnings printed elsewhere go to ``err_console`` / stderr.
    """
    if json_out:
        sys.stdout.write(json.dumps(_to_jsonable(result), default=str))
        sys.stdout.write("\n")
        return
    if render is not None:
        render(result)


def _to_jsonable(obj: Any) -> Any:
    """Best-effort conversion of Pydantic models / nested DTOs to plain JSON."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def handle_service_error(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator: map a ``ServiceError`` raised by a use_case to exit code 1.

    Prints the human-readable ``message`` to stderr (never stdout, to keep
    ``--json`` pipes clean) and exits 1. Typer keeps exit 2 for argument misuse;
    exit 0 is success.
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        from core.api.use_cases._errors import ServiceError

        try:
            return func(*args, **kwargs)
        except ServiceError as exc:
            err_console.print(f"[red]{exc.message}[/red]")
            raise typer.Exit(1) from exc

    return wrapper
