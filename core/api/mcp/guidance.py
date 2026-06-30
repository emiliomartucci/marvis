# v1.0.0 - 2026-06-26 - Agent-facing MCP routing guidance.
"""Shared MCP guidance for server instructions and the ``guide`` tools."""
from __future__ import annotations

from typing import Any


ROUTING_GUIDE: tuple[dict[str, str], ...] = (
    {
        "intent": "first-time agent setup / instruction patching",
        "tool": "agent_onboarding_guide(client=..., project_slug=...)",
        "why": "Concrete AGENTS.md/CLAUDE.md/Codex config patches plus hosted-canonical checks.",
    },
    {
        "intent": "active projects / project inventory",
        "tool": "list_projects(lifecycle='active')",
        "why": "Lean list of slug, program, lifecycle, language and task counts.",
    },
    {
        "intent": "cold-start one project",
        "tool": "session_brief(slug)",
        "why": "One bundle with project state, open tasks, latest handoff, learnings and salience docs.",
    },
    {
        "intent": "cross-project discovery by meaning",
        "tool": "search(q)",
        "why": "Semantic + hybrid search across tasks, projects, files, handoffs, learnings, inbox and audits.",
    },
    {
        "intent": "known project raw body or docs",
        "tool": "get_project(slug)",
        "why": "Use only when you need context.md body, handoff index or docs for a known slug.",
    },
    {
        "intent": "what blocks if project X pauses",
        "tool": "project_impact(slug)",
        "why": "Portfolio-level blast radius. Use graph_impact only when you already have a node_id.",
    },
    {
        "intent": "code or KG impact",
        "tool": "graph_impact(node_id)",
        "why": "Transitive blast radius before changing a function, file or graph node.",
    },
    {
        "intent": "direct callers / direct dependents",
        "tool": "graph_neighbors(node_id, direction='incoming')",
        "why": "One-hop topology with server-side summary counts; cite summary, do not recount lists.",
    },
    {
        "intent": "why this exists",
        "tool": "graph_context(node_id)",
        "why": "Rationale chain from node to commits, PRs, tasks, handoffs and learnings.",
    },
    {
        "intent": "what bit us before",
        "tool": "check_learnings(q)",
        "why": "Past incidents and prevention rules before risky work or a decision.",
    },
    {
        "intent": "task counts",
        "tool": "tasks_summary()",
        "why": "Aggregated counts only; use list_tasks for task rows.",
    },
    {
        "intent": "task rows",
        "tool": "list_tasks(project=..., status=...)",
        "why": "Exact filters for tracked tasks; not semantic discovery.",
    },
)

AGENT_ONBOARDING_CONTRACT: dict[str, Any] = {
    "canonical_surface": "marvis_hosted MCP",
    "startup_recipe": [
        "Call guide() when tool routing is unclear.",
        "Call agent_onboarding_guide(client=..., project_slug=...) on first connection or when local instructions may be stale.",
        "Call session_brief(slug) before project work; use the returned repo_path/metadata_path as source-of-truth.",
        "Before code, push, deploy, refactor, reindex or destructive work, call check_learnings(q).",
        "Verify hosted artifacts with hosted read_file/grep/session_brief before claiming they exist.",
    ],
    "non_negotiables": [
        "Hosted work is not proven by local files, local CLI output or stale AGENTS.md/CLAUDE.md content.",
        "Hints must propose instruction patches, not generic reminders.",
        "List and hot-path tools must stay compact by default.",
    ],
}

TOOL_TIERS: dict[str, dict[str, Any]] = {
    "tier0": {
        "max_description_chars": 900,
        "risk": "core",
        "always_load": True,
        "tools": (
            "guide",
            "agent_onboarding_guide",
            "list_projects",
            "session_brief",
            "search",
            "check_learnings",
            "read_file",
            "grep",
            "create_task",
            "update_task",
            "approve_task",
        ),
    },
    "tier1": {
        "max_description_chars": 600,
        "risk": "frequent_read",
        "always_load": False,
        "tools": (
            "get_project",
            "list_tasks",
            "get_task",
            "tasks_summary",
            "list_handoffs",
            "get_handoff",
            "graph_impact",
            "graph_neighbors",
            "graph_context",
            "project_impact",
        ),
    },
    "tier2": {
        "max_description_chars": 800,
        "risk": "write_workflow",
        "always_load": False,
        "tools": (
            "reject_task",
            "create_learning",
            "update_learning",
            "write_file",
            "edit",
            "create_branch",
            "register_branch",
            "submit_pr",
            "close_pr",
        ),
    },
    "tier3": {
        "max_description_chars": 1000,
        "risk": "dangerous_admin",
        "always_load": False,
        "tools": (
            "delete_task",
            "delete_learning",
            "merge_pr",
            "run_bash",
            "storage_quota",
            "teardown_demo",
        ),
    },
}

_INSTRUCTION_PATCHES: dict[str, dict[str, str]] = {
    "codex": {
        "target": "AGENTS.md",
        "recommended_patch": (
            "For Marvis hosted work, use `marvis_hosted` MCP as canonical. "
            "Local files and local MCP results are not proof unless verified through hosted MCP "
            "(`session_brief`, `read_file`, `grep`, or the relevant hosted tool). "
            "Before writing reports/plans for hosted work, create or verify the artifact in the hosted repo path returned by `session_brief`. "
            "If local AGENTS.md conflicts with hosted context, hosted context wins."
        ),
    },
    "claude": {
        "target": "CLAUDE.md",
        "recommended_patch": (
            "When a task mentions Marvis hosted, server canonicality, tenant repo, hosted MCP, or canonical reports, "
            "first call hosted Marvis MCP `session_brief(project_slug)`. Treat hosted MCP output as source of truth. "
            "Do not rely on local Marvis CLI, local repo files, or stale project instructions as proof for hosted work."
        ),
    },
    "unknown": {
        "target": "agent instructions",
        "recommended_patch": (
            "Marvis hosted is the canonical company brain. On first use, call `guide()` and "
            "`agent_onboarding_guide(client='unknown')`. Use `session_brief(slug)` for project cold-start. "
            "Verify artifacts through hosted MCP before claiming they exist."
        ),
    },
}


def _normalize_client(client: str | None) -> str:
    raw = (client or "unknown").strip().lower()
    if raw in {"codex", "openai", "codex-cli", "codex_app"}:
        return "codex"
    if raw in {"claude", "claude-code", "claude_desktop", "anthropic"}:
        return "claude"
    return "unknown"


def _excerpt_warnings(excerpt: str | None) -> list[str]:
    text = (excerpt or "").lower()
    warnings: list[str] = []
    if "mcp__marvis__" in text and "marvis_hosted" not in text:
        warnings.append(
            "Instructions mention local `mcp__marvis__` but not `marvis_hosted`; hosted work can drift."
        )
    if "marvis cli" in text or "local cli" in text:
        warnings.append(
            "Instructions mention local CLI; hosted MCP must be the proof surface for hosted work."
        )
    if "local files" in text and "hosted" not in text:
        warnings.append(
            "Instructions appear local-first; add the hosted-canonical patch before continuing."
        )
    return warnings


def always_loaded_tool_names() -> frozenset[str]:
    """Return the Tier 0 tool names that should stay visible when supported."""
    return frozenset(TOOL_TIERS["tier0"]["tools"])


def tool_metadata_for(name: str) -> dict[str, Any]:
    """Return neutral metadata for a tool without depending on client support."""
    for tier_name, tier in TOOL_TIERS.items():
        if name in tier["tools"]:
            return {
                "marvis/tier": tier_name,
                "marvis/risk": tier["risk"],
                "marvis/maxDescriptionChars": tier["max_description_chars"],
            }
    return {"marvis/tier": "untiered", "marvis/risk": "unknown"}


def _iter_tool_objects(server: Any):
    """Yield registered tool objects across supported FastMCP registry shapes."""
    seen: set[int] = set()

    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if isinstance(tools, dict):
        for name, tool in tools.items():
            seen.add(id(tool))
            yield name, tool

    for provider in getattr(server, "providers", []) or []:
        components = getattr(provider, "_components", None)
        if not isinstance(components, dict):
            continue
        for key, tool in components.items():
            if id(tool) in seen:
                continue
            name = getattr(tool, "name", None)
            if not name and isinstance(key, str) and key.startswith("tool:"):
                name = key.removeprefix("tool:").split("@", 1)[0]
            if name:
                seen.add(id(tool))
                yield name, tool


def apply_tool_metadata_to_server(server: Any) -> None:
    """Apply Marvis tier metadata to registered tools for stdio and HTTP servers."""
    core_tools = always_loaded_tool_names()
    for name, tool in _iter_tool_objects(server):
        meta = dict(getattr(tool, "meta", None) or {})
        meta.update(tool_metadata_for(name))
        if name in core_tools:
            meta["anthropic/alwaysLoad"] = True
        setattr(tool, "meta", meta)


def build_agent_hint(
    *,
    client: str | None = None,
    project_slug: str | None = None,
    current_instructions_excerpt: str | None = None,
    issue: str | None = None,
) -> dict[str, Any]:
    """Build a compact event-based hint that proposes an instruction patch."""
    normalized = _normalize_client(client)
    patch = _INSTRUCTION_PATCHES[normalized]
    warnings = _excerpt_warnings(current_instructions_excerpt)
    one_line = (
        f"Patch {patch['target']} so Marvis hosted MCP is canonical before continuing"
        if warnings or issue
        else f"Add a Marvis hosted MCP rule to {patch['target']} for future sessions"
    )
    return {
        "kind": "instruction_patch",
        "severity": "warning" if warnings or issue else "info",
        "one_line": one_line,
        "applies_to": [patch["target"]],
        "recommended_patch": patch["recommended_patch"],
        "why": issue
        or "First-time or ambiguous hosted setup; local instructions can silently override hosted context.",
        "warnings": warnings,
        "dismiss_key": f"hosted-canonical-instructions-v1:{normalized}:{project_slug or 'any'}",
    }


def agent_onboarding_payload(
    *,
    client: str | None = None,
    project_slug: str | None = None,
    current_instructions_excerpt: str | None = None,
    issue: str | None = None,
    detail: str = "standard",
) -> dict[str, Any]:
    """Return first-time agent onboarding guidance and setup-tailored patches."""
    normalized = _normalize_client(client)
    hint = build_agent_hint(
        client=normalized,
        project_slug=project_slug,
        current_instructions_excerpt=current_instructions_excerpt,
        issue=issue,
    )
    slug = project_slug or "<project_slug>"
    payload: dict[str, Any] = {
        "source": "agent onboarding contract",
        "contract": {
            "canonical_surface": AGENT_ONBOARDING_CONTRACT["canonical_surface"],
            "non_negotiables": AGENT_ONBOARDING_CONTRACT["non_negotiables"],
        },
        "client": normalized,
        "project_slug": project_slug,
        "startup_recipe": [
            "guide()",
            f"session_brief('{slug}')",
            "check_learnings(q) before risky work",
            "read_file/grep hosted artifacts before claiming completion",
        ],
        "instruction_patches": [hint],
        "canonicality_checks": [
            f"`session_brief('{slug}')` returns hosted repo_path",
            "reports/plans are readable with hosted `read_file`",
            "code/docs are verified through hosted MCP or hosted server runtime, not local-only files",
        ],
        "common_failures": [
            "Local AGENTS.md or CLAUDE.md says local Marvis is canonical.",
            "Agent writes a report locally and claims hosted completion without hosted read_file/grep.",
            "Agent uses local CLI or local MCP for a hosted task.",
        ],
        "next_tool": f"session_brief('{slug}')" if project_slug else "list_projects(lifecycle='active')",
    }
    if detail == "compact":
        payload.pop("common_failures")
    return payload


def build_instructions() -> str:
    """Build the hosted/local MCP server instructions from the shared route map."""
    lines = [
        "Marvis is a company-brain MCP for cross-project orchestration and institutional memory.",
        "Route by task type; prefer structured tools over raw files:",
    ]
    for item in ROUTING_GUIDE[:10]:
        lines.append(f"- {item['intent']}: {item['tool']} - {item['why']}")
    lines.extend(
        [
            "First-time agents should call agent_onboarding_guide for setup-specific instruction patches.",
            "Before answering orchestration or planning work, confirm you called the relevant tool and answer the asked task.",
            "When a graph result includes a summary count, cite that summary and do not recount returned lists by hand.",
        ]
    )
    return "\n".join(lines) + "\n"


def guide_payload() -> dict[str, Any]:
    """Return a compact intent-to-tool cheatsheet for connected agents."""
    return {
        "source": "server instructions",
        "routing": list(ROUTING_GUIDE),
        "rules": [
            "First-time agents: call agent_onboarding_guide to patch AGENTS.md/CLAUDE.md/Codex rules for hosted work.",
            "Use session_brief for project cold-start, not get_project + extra searches.",
            "Use search for meaning-first cross-project discovery.",
            "Use graph tools only when you need topology, impact or rationale.",
            "Use check_learnings before risky work or strategic decisions.",
            "Use tasks_summary for counts and list_tasks for rows.",
        ],
        "agent_hint": build_agent_hint(issue="Use when local agent instructions may conflict with hosted MCP."),
    }
