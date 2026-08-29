# v2.0.0 - 2026-07-03 - Agent-facing MCP routing guidance + de-hijacked onboarding.
"""Shared MCP guidance for server instructions, the ``guide`` tools and the
agent-native onboarding wizard.

P2 de-hijack (2026-07-03): the old self-modification pattern — payloads that
asked the connected agent to edit its own instruction files — is gone. Modern
harnesses flag agent self-instruction edits as a hijack, so canonicality now
travels as FACTS the agent relays to the user, and first-run guidance is a
transparent, per-user wizard (``ONBOARDING_STEPS`` + the ``onboarding_status`` /
``onboarding_answer`` tools). Every machine->agent string here is technical,
terse, English and tenant-agnostic: the user's own LLM does the last-mile
localization into human language, in the style it uses with its user.
"""
from __future__ import annotations

from typing import Any

ROUTING_GUIDE: tuple[dict[str, str], ...] = (
    {
        "intent": "first-time agent setup",
        "tool": "agent_onboarding_guide(client=..., project_slug=...)",
        "why": "Hosted-canonical setup checks plus a pointer to the guided onboarding wizard.",
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
        "intent": "review or deepen an existing plan",
        "tool": (
            "read_file(path) -> write_file(path, content, if_match_sha256) -> "
            "read_file(path)"
        ),
        "why": (
            "Update the same plan with optimistic concurrency and verify readback. "
            "save_plan creates a new plan; save_audit is not part of plan review."
        ),
    },
    {
        "intent": "code path missing / stale code graph",
        "tool": "guide() -> session_brief(slug) -> marvis-graph-export or marvis-index-action",
        "why": (
            "The hosted tenant stores graph nodes, edges and provenance, never source. "
            "Regenerate from the real local repository or CI; reindex_paths refreshes "
            "hosted documents, not the code graph."
        ),
    },
    {
        "intent": "worktree / branch / commit / pull request / review / merge",
        "tool": "Git plus the GitHub connector (or repository-native gh)",
        "why": (
            "Repository lifecycle is never managed through Marvis MCP. Use Marvis "
            "only for the linked task and governed project state after verified "
            "GitHub readback."
        ),
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
        "Call onboarding_status() to start or resume the guided, per-user setup wizard.",
        "Call session_brief(slug) before project work; metadata_path identifies hosted project state, while repo_path is mapping metadata and never a Git workspace authority.",
        "Before code, push, deploy, refactor, reindex or destructive work, call check_learnings(q).",
        "Verify hosted artifacts with hosted read_file/grep/session_brief before claiming they exist.",
    ],
    "non_negotiables": [
        "Hosted work is not proven by local files, local CLI output or stale local instruction files.",
        "Canonicality is stated as facts to relay to the user; the guide never asks the agent to rewrite its own instruction files.",
        "Git and GitHub own worktrees, branches, commits, pull requests, reviews and merges; Marvis MCP never manages repository lifecycle.",
        "List and hot-path tools must stay compact by default.",
    ],
}

# =====================================================================
# Onboarding wizard registry (P2). Six transparent steps. Each string is a
# machine->agent PAYLOAD: technical, terse, English, tenant-agnostic. The
# CONTRACT: every step is a PROPOSAL the agent relays to the user; the MCP
# records only STATE (done/snoozed/skipped) via onboarding_answer, never the
# content of a user's answers unless the welcome_profile step captures a profile
# WITH explicit consent. No payload asks the agent to modify its own config.
# State lives in table user_onboarding (mig 164); logic in
# core.api.use_cases.onboarding_wizard (distinct from tools/onboarding.py, the
# OSS scan_workdir/seed_demo helper).
# =====================================================================
ONBOARDING_STEPS: tuple[dict[str, Any], ...] = (
    {
        "key": "welcome_profile",
        "title": "Who you are (optional profile)",
        "explain": (
            "Capture WHO the user is -> becomes their profile (LLM context + KG "
            "person-entity). Fields: name, role, org unit, response-style pref "
            "(concise|detailed)."
        ),
        "propose": (
            "Ask the user for these; state you'll persist them so Marvis knows "
            "them, deletable anytime."
        ),
        "ask_user": (
            "Explicit consent before saving. onboarding_answer(step_key="
            "'welcome_profile', action='done', consent=true, profile={name, role, "
            "org_unit, response_style}) persists it; done WITHOUT consent records "
            "the step but saves nothing; action='delete_profile' erases it."
        ),
    },
    {
        "key": "marvis_explainer",
        "title": "What Marvis is",
        "explain": (
            "Marvis = shared company brain. Persistent RBAC-scoped store of "
            "projects/tasks/decisions/handoffs/docs/learnings, queried from the "
            "user's OWN LLM over MCP (no separate app). Three capabilities: RECALL "
            "(state survives across sessions, no cold-start), LINK, SYNTHESIZE.\n"
            "ENGINE MODEL: no API key to bring, no compute to provision — the "
            "user's own LLM (this client) IS the engine that drives the brain. "
            "Marvis supplies memory+structure+tools; the LLM supplies reasoning. "
            "Auth = the user's login (OAuth), nothing else to configure.\n"
            "BRAIN: nightly mechanical cycle aggregates tenant events (commits, "
            "tasks, docs, PRs) -> agent synthesis writes journal (what changed, "
            "decisions observed, open loops), drift signals (observed vs expected "
            "knowledge gaps) and findings (approvable conclusions). Consumed via "
            "brain_journal/brain_drift/brain_findings; user-side distributed "
            "synthesis via a scheduled prompt in their client.\n"
            "GRAPH: every artifact (function, file, task, PR, handoff, doc, "
            "learning) is a node; 15 deterministic edge types "
            "(calls/imports/produces/cites/depends_on/mentions/...) link them "
            "cross-project. Answers 'who calls X', 'what breaks if I change X', "
            "'why does X exist' (graph_neighbors/graph_impact/graph_context) — "
            "semantic search (embeddings) + graph beats blind file grep.\n"
            "TOP-5 TOOLS (start here): 1) session_brief(slug) — cold-start bundle "
            "for a project; 2) search(q) — semantic cross-project discovery; 3) "
            "create_task/list_tasks — persistent cross-session work items; 4) "
            "create_handoff — save session state before closing (the RECALL "
            "discipline); 5) check_learnings(q) — past incidents + prevention "
            "before risky actions.\n"
            "Visibility RBAC-scoped: only granted projects; confidential stays "
            "confidential."
        ),
        "propose": (
            "Offer to show the user's projects now OR go deeper on one capability."
        ),
        "ask_user": (
            "What does the user work on most — OR offer auto-discovery (next step)."
        ),
    },
    {
        "key": "propose_projects",
        "title": "Find your projects",
        "explain": (
            "PROJECT (Marvis unit of work) = has ALL of: context (what/why), "
            "handoffs (session-to-session state), tasks, documents/deliverables "
            "(outputs), inputs (source material). A bare repo/folder is NOT a "
            "project until it carries these."
        ),
        "propose": (
            "SCAN the user's available surfaces — connected GitHub repos (tree, "
            "READMEs), local project folders, your own chat memory/history with "
            "this user — and PROPOSE 3-7 candidate projects meeting the "
            "definition. Per candidate: dns-safe slug, one-line context, evidence "
            "(repo/folder/memory path), detected artifacts (docs/tasks/inputs "
            "already present). Do NOT create silently: present -> user "
            "confirms/renames/drops -> create only confirmed."
        ),
        "example": (
            "slug `q3-pricing-review`; context 'Q3 pricing model revision for "
            "product line X'; inputs = market benchmarks, current price list; "
            "deliverables = new pricing sheet, approval memo; tasks = analysis + "
            "stakeholder-review milestones; evidence = folder `~/work/pricing/` + "
            "4 chat sessions on the topic."
        ),
    },
    {
        "key": "brain_intro",
        "title": "The brain and scheduled synthesis",
        "explain": (
            "Nightly brain cycle already aggregates events tenant-wide. User can "
            "run distributed synthesis on THEIR own work via a scheduled prompt "
            "(`/schedule` in their client)."
        ),
        "propose": (
            "Explain the brain, then PROPOSE (don't execute) setting up a "
            "`/schedule`; hand the user the prompt."
        ),
        "ask_user": "Want the /schedule prompt now?",
    },
    {
        "key": "report_bug_howto",
        "title": "Reporting issues",
        "explain": (
            "`report_bug` files an issue routed to the tenant admin; body is "
            "secret-redacted."
        ),
        "propose": "Tell the user how/when to use it.",
    },
    {
        "key": "teams_confidential_basics",
        "title": "Teams and confidential files",
        "explain": (
            "Self-service teams (user creates -> is teamlead); per-file "
            "confidential clearance (owner-controlled)."
        ),
        "propose": "Surface on demand, not pushy.",
    },
)


def ordered_onboarding_steps() -> tuple[dict[str, Any], ...]:
    """The onboarding steps in presentation order."""
    return ONBOARDING_STEPS


def onboarding_step_keys() -> tuple[str, ...]:
    """Step keys in order — the reference set for status/answer validation."""
    return tuple(step["key"] for step in ONBOARDING_STEPS)


def onboarding_step(key: str) -> dict[str, Any] | None:
    """Full payload for a single step, or None when the key is unknown."""
    for step in ONBOARDING_STEPS:
        if step["key"] == key:
            return dict(step)
    return None


# Tier axis = BOTH context-load priority AND the min system_role that may see or
# call the tool (P3 tool profiles RBAC). tier0 -> viewer (safe reads + onboarding
# + bug report), tier1/tier2 -> operator, tier3 -> admin/super_admin. Every
# REGISTERED tool MUST appear in exactly one tier: tests/mcp/test_tool_profiles.py
# fails on any untiered, double-tiered, or stale (unregistered) entry — the rule
# that keeps this map alive as tools are added. Enforcement of the min_role is the
# ToolProfilesMiddleware (HTTP only); the per-tool internal role/scope gates of the
# sensitive tools are independent and stay active (the profile is exposure-only).
TOOL_TIERS: dict[str, dict[str, Any]] = {
    "tier0": {
        "max_description_chars": 900,
        "risk": "core",
        "always_load": True,
        "min_role": "viewer",
        "tools": (
            "guide",
            "agent_onboarding_guide",
            "onboarding_status",
            "onboarding_answer",
            "session_brief",
            "search",
            "check_learnings",
            "read_file",
            "grep",
            "list_projects",
            "get_project",
            "list_tasks",
            "get_task",
            "tasks_summary",
            "list_handoffs",
            "get_handoff",
            "list_learnings",
            "get_learning",
            "report_bug",
            "bug_status",
            # P1 F1: per-user notification inbox — self-scoped, so viewer-safe.
            # ack mutates only the caller's OWN rows (not a workflow write), which
            # is why it sits in the viewer surface alongside its list counterpart:
            # a viewer sees the `notices` counter in session_brief and must be able
            # to open + dismiss its own items.
            "list_notifications",
            "ack_notification",
        ),
    },
    "tier1": {
        "max_description_chars": 600,
        "risk": "frequent_read",
        "always_load": False,
        "min_role": "operator",
        "tools": (
            # project / task / knowledge reads
            "search_handoffs",
            "project_impact",
            "list_todos",
            "list_comments",
            "list_teams",
            "list_access",
            "list_user_requests",
            "list_bug_reports",
            "list_ingest_pending",
            "directory_tree",
            "download_file",
            "check_safety",
            # graph reads
            "graph_capabilities",
            "graph_context",
            "graph_impact",
            "graph_neighbors",
            "graph_hotspots",
            "graph_pattern",
            "graph_resolve",
            "graph_landing",
            "graph_overview",
            "graph_orphans",
            "list_graph_pins",
            # brain reads
            "brain_capabilities",
            "brain_runs",
            "brain_runs_get",
            "brain_events",
            "brain_journal",
            "brain_drift",
            "brain_drift_get",
            "brain_findings",
            "brain_findings_get",
            "brain_memory_operations",
            "brain_memory_operations_get",
            # governed project / Cloud-F control reads
            "get_project_lifecycle",
            "get_cloud_f_control",
            "get_cloud_f_change_operation",
            "get_project_lifecycle_operation",
            "get_governed_decision",
            "list_historical_pointers",
            "get_decision_operation",
        ),
    },
    "tier2": {
        "max_description_chars": 800,
        "risk": "write_workflow",
        "always_load": False,
        "min_role": "operator",
        "tools": (
            # task / knowledge writes
            "create_task",
            "update_task",
            "comment_task",
            "create_handoff",
            "create_learning",
            "update_learning",
            "memory_feedback",
            # todos
            "create_todo",
            "update_todo",
            "delegate_todo",
            # workspace writes
            "write_file",
            "edit",
            "upload_file",
            "upload_attachment",
            "share_file",
            # workflows
            "brainstorm",
            "plan",
            "compound",
            "save_brainstorm",
            "save_plan",
            "save_compound",
            # graph pins
            "pin_graph_node",
            "unpin_graph_node",
            # confidential mark / share
            "mark_confidential",
            "unmark_confidential",
            "share_confidential",
            "unshare_confidential",
            # teams / project / user self-service (F2; internal guards own the gate)
            "create_team",
            "add_team_member",
            "remove_team_member",
            "assign_team_project",
            "unassign_team_project",
            "create_project",
            "add_user",
            # brain write agent-native (distributed synthesis, P4)
            "brain_write_finding",
            "brain_write_journal",
            "brain_staleness",
            # governed project / Cloud-F control writes
            "register_project_lifecycle",
            "acquire_cloud_f_change",
            "complete_cloud_f_change",
            "update_project_selector_watermark",
            "create_governed_decision",
        ),
    },
    "tier3": {
        "max_description_chars": 1000,
        "risk": "dangerous_admin",
        "always_load": False,
        "min_role": "admin",
        "tools": (
            # destructive
            "delete_task",
            "delete_learning",
            "seed_demo",
            "teardown_demo",
            # task triage authority
            "approve_task",
            "reject_task",
            # access / user-role admin
            "grant_access",
            "revoke_access",
            "set_user_role",
            # bug-report triage (operator, cross-tenant read)
            "list_bug_reports_admin",
            # ingest admin
            "approve_ingest_pending",
            "reject_ingest_pending",
            "patch_ingest_pending",
            "classify_ingest",
            "reparse_ingest",
            "upload_contract",
            # index / infra / shell
            "reindex",
            "reindex_paths",
            "kg_reindex_path",
            "run_bash",
            "storage_usage",
            "scan_workdir",
            "write_setup",
            # brain governance (patch / apply / recompute)
            "brain_drift_patch",
            "brain_findings_patch",
            "brain_findings_bulk_patch",
            "brain_findings_apply",
            "brain_memory_operations_patch",
            "brain_memory_operations_apply",
            "brain_cycles_recompute",
            # governed lifecycle authority
            "activate_cloud_f_control",
            "create_project_archive_approval",
            "archive_project",
            "accept_governed_decision",
            "supersede_governed_decision",
            "create_historical_pointer",
        ),
    },
}

# tier -> min system_role, the RBAC axis (P3). Derived from TOOL_TIERS so the two
# never drift. Roles rank via core.api.use_cases._roles.ROLE_HIERARCHY.
TIER_MIN_ROLE: dict[str, str] = {
    name: tier["min_role"] for name, tier in TOOL_TIERS.items()
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
            "Instructions appear local-first; treat hosted MCP as the proof surface."
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
                "marvis/minRole": tier["min_role"],
            }
    return {"marvis/tier": "untiered", "marvis/risk": "unknown"}


def all_tiered_tool_names() -> frozenset[str]:
    """Every tool name assigned to a tier (the completeness-test reference set)."""
    names: set[str] = set()
    for tier in TOOL_TIERS.values():
        names.update(tier["tools"])
    return frozenset(names)


def tier_for_tool(name: str) -> str | None:
    """Return the tier a tool belongs to, or None when untiered."""
    for tier_name, tier in TOOL_TIERS.items():
        if name in tier["tools"]:
            return tier_name
    return None


def min_role_for_tool(name: str) -> str:
    """Min ``system_role`` allowed to see/call ``name``.

    Fail-closed for exposure: an untiered tool requires ``admin`` (invisible to
    viewers/operators until it is explicitly tiered — the completeness test forces
    that decision in CI, this is the runtime backstop).
    """
    for tier in TOOL_TIERS.values():
        if name in tier["tools"]:
            return tier["min_role"]
    return "admin"


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
    """Build a compact canonicality hint (P2 de-hijacked).

    States facts for the agent to relay to the user and points at the onboarding
    wizard. It NEVER emits an instruction-file edit for the agent to self-apply —
    the whole point of the P2 rework. ``_excerpt_warnings`` still surfaces local
    drift as observations (facts), not as a patch.
    """
    warnings = _excerpt_warnings(current_instructions_excerpt)
    severity = "warning" if warnings or issue else "info"
    one_line = (
        "Marvis hosted MCP is canonical for hosted work: relay this to the user, "
        "verify hosted artifacts through hosted tools before claiming completion, "
        "and run onboarding_status to start or resume the guided setup."
    )
    return {
        "kind": "canonicality",
        "severity": severity,
        "one_line": one_line,
        "why": issue
        or "First-time or ambiguous hosted setup; local instructions can silently override hosted context.",
        "warnings": warnings,
        "next_tool": "onboarding_status",
        "dismiss_key": f"hosted-canonical-v2:{_normalize_client(client)}:{project_slug or 'any'}",
    }


def agent_onboarding_payload(
    *,
    client: str | None = None,
    project_slug: str | None = None,
    current_instructions_excerpt: str | None = None,
    issue: str | None = None,
    detail: str = "standard",
) -> dict[str, Any]:
    """Return first-time agent onboarding guidance (P2 de-hijacked).

    Signature is unchanged (client compat). The payload keeps the useful
    canonicality contract and now points at the wizard (``onboarding_status``).
    ``instruction_patches`` stays as an ALWAYS-EMPTY list for back-compat with
    clients that read the key; the old self-modification patches are gone.
    """
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
            "onboarding_status()",
            f"session_brief('{slug}')",
            "check_learnings(q) before risky work",
            "read_file/grep hosted artifacts before claiming completion",
        ],
        # P2 de-hijack: empty for back-compat; canonicality now travels as facts.
        "instruction_patches": [],
        "instruction_patches_note": (
            "deprecated: canonicality is stated as facts for the user to act on; "
            "run onboarding_status for the guided setup wizard."
        ),
        "canonicality": hint,
        "canonicality_checks": [
            f"`session_brief('{slug}')` returns hosted repo_path",
            "reports/plans are readable with hosted `read_file`",
            "code/docs are verified through hosted MCP or hosted server runtime, not local-only files",
        ],
        "common_failures": [
            "Local instruction files claim local Marvis is canonical.",
            "Agent writes a report locally and claims hosted completion without hosted read_file/grep.",
            "Agent uses local CLI or local MCP for a hosted task.",
        ],
        "next_tool": "onboarding_status",
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
    for item in ROUTING_GUIDE[:13]:
        lines.append(f"- {item['intent']}: {item['tool']} - {item['why']}")
    lines.extend(
        [
            "First-time agents should call agent_onboarding_guide, then onboarding_status for the guided setup.",
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
            "First-time agents: call agent_onboarding_guide for hosted-canonical setup, then onboarding_status for the guided wizard.",
            "Use session_brief for project cold-start, not get_project + extra searches.",
            "Use search for meaning-first cross-project discovery.",
            "Use graph tools only when you need topology, impact or rationale.",
            "Use check_learnings before risky work or strategic decisions.",
            "Use tasks_summary for counts and list_tasks for rows.",
            "Reviewing or deepening an existing plan updates that same file with read_file + write_file(if_match_sha256) + readback; never route it to save_audit.",
            "Use Git and GitHub for worktrees, branches, commits, pull requests, reviews, checks, and merges; never call a Marvis branch or PR lifecycle tool.",
        ],
        "agent_hint": build_agent_hint(issue="Use when local agent instructions may conflict with hosted MCP."),
    }
