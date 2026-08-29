# v1.0.0 - 2026-05-27 - S1 F3.0: MCP tool groups package (tasks + learnings template batch)
"""MCP tool groups, one module per domain (mirror of the use_cases domains).

Each module exposes a ``register(mcp)`` function that decorates and registers its
tools on the shared ``FastMCP`` instance. ``server.py`` calls every group's
``register`` in turn. The per-tool pattern is the TEMPLATE the later batches copy
(see ``tasks.py`` / ``learnings.py``):

    @mcp.tool()
    async def <name>(<typed params>) -> dict:
        \"\"\"<Node description verbatim — incl. QUANDO USARLO/NON USARLO>\"\"\"
        try:
            async with acquire_db() as db:               # or acquire_write_db() for mutators
                result = await use_cases.<domain>.<action>(LOCAL_CTX, db, ...)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)   # raises ToolError -> real CallToolResult(isError=True)

F3.0 shipped the ``tasks`` + ``learnings`` groups; F3.1a adds ``projects`` +
``search`` + ``handoffs``; F3.1b adds ``graph`` (the Knowledge-Graph family); F3.1c
adds ``brain`` (the reflection family); F3.1d (the final batch) adds ``ingest`` +
``safety``. The former pull-request group is retired. Repository lifecycle is
intentionally not an MCP domain: Git and GitHub own branches, PRs, reviews, checks,
and merges.
"""
from __future__ import annotations

from . import (
    admin_access,
    agent_tokens,
    confidential,
    agent_onboarding,
    brain,
    bug_reports,
    comments,
    feedback,
    guide,
    graph,
    handoffs,
    ingest,
    learnings,
    notifications,
    onboarding,
    onboarding_wizard,
    projects,
    safety,
    search,
    tasks,
    teams,
    todos,
    user_provisioning,
    workflows,
)

try:
    from . import file_shares, storage, workspace
except ImportError:
    file_shares = storage = workspace = None

#: Ordered registration callables. ``server.py`` iterates this so adding a new
#: group is one import + one entry, never an edit to the server wiring.
REGISTRARS = (
    admin_access.register,
    agent_tokens.register,
    confidential.register,
    tasks.register,
    teams.register,
    bug_reports.register,
    comments.register,
    todos.register,
    learnings.register,
    notifications.register,
    projects.register,
    search.register,
    feedback.register,
    handoffs.register,
    graph.register,
    agent_onboarding.register,
    guide.register,
    brain.register,
    ingest.register,
    onboarding.register,
    onboarding_wizard.register,
    safety.register,
    user_provisioning.register,
    workflows.register,
)

if storage is not None and file_shares is not None and workspace is not None:
    REGISTRARS += (
        storage.register,
        file_shares.register,
        workspace.register,
    )


def register_all(mcp) -> None:
    """Register every tool group on the shared FastMCP instance."""
    for registrar in REGISTRARS:
        registrar(mcp)
