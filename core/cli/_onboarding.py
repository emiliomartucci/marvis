# v1.0.0 - 2026-06-09 - P4: onboarding completion state (single source for doctor + guide).
"""Onboarding completion — the binary "is Marvis fully set up?" state.

`marvis doctor` checks environment HEALTH (OS, Python, data-files, model, …). This
module is the orthogonal layer: a small set of **binary done/not-done states** that
say whether the install is *complete*, split into:

- **required** — without them Marvis does not work (CLI on PATH, config present, MCP
  server registered). "100%" means all required are done.
- **recommended** — "+unlocks X" (hooks, a project, indexed code, the brain, an LLM).
  These never hold someone at 90% by choice (e.g. you may not want the brain).

This is the SINGLE SOURCE OF TRUTH for that list: `marvis doctor` renders it as a
completion section and `marvis guide` documents the same states. A test
(`test_onboarding.py`) asserts the two never diverge.

States already covered by a `marvis doctor` check are mapped to it **by name**
(no re-implementation); the net-new ones (MCP registered, project imported, LLM
configured, hooks installed) carry their own best-effort detector. Detectors are
read-only and never raise — a broken probe degrades to "not done", never a crash.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

Tier = Literal["required", "recommended"]

# A detector returns (done, detail). Best-effort: never raises.
Detector = Callable[[], "tuple[bool, str]"]


@dataclass(frozen=True)
class OnboardingState:
    key: str
    title: str
    tier: Tier
    fix: str  # paste-ready remediation command/hint, "" when none
    # Exactly one mechanism: reuse existing doctor checks by name, OR a detector.
    check_names: tuple[str, ...] = ()
    detector: Detector | None = field(default=None)


# --------------------------------------------------------------------------- #
# Net-new detectors (sync, read-only, never raise)                            #
# --------------------------------------------------------------------------- #


def _detect_mcp_registered() -> tuple[bool, str]:
    """The Claude Code config (~/.claude.json) carries an `mcpServers.marvis` entry.

    Absent file / other MCP client → NOT done with a soft note (never an error):
    we cannot prove registration, so we report it as a gap, not a failure. Only the
    presence of the key is read — never the file's contents.
    """
    cfg = Path.home() / ".claude.json"
    if not cfg.exists():
        return (False, "~/.claude.json not found (or you use a non-Claude-Code MCP client)")
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return (False, "~/.claude.json is not readable/valid JSON")
    servers = (data.get("mcpServers") or {}) if isinstance(data, dict) else {}
    if "marvis" in servers:
        return (True, "mcpServers.marvis registered in Claude Code")
    return (False, "no mcpServers.marvis entry in ~/.claude.json")


def _detect_project_imported() -> tuple[bool, str]:
    """At least one project is registered (a `<dir>/project.yaml` under a projects root)."""
    roots: list[Path] = []
    try:
        from core.api.routers.projects import PROJECT_DIRS  # set by apply_marvis_settings()

        roots = [Path(p) for p in PROJECT_DIRS]
    except Exception:
        roots = []
    if not roots:
        try:
            from core.platform import projects_root_default

            roots = [projects_root_default()]
        except Exception:
            return (False, "could not resolve a projects root")
    count = 0
    for base in roots:
        try:
            count += sum(1 for _ in base.glob("*/project.yaml"))
        except Exception:
            continue
    if count > 0:
        return (True, f"{count} project(s) registered")
    return (False, "no projects imported yet")


def _detect_llm_configured() -> tuple[bool, str]:
    """The brain has an LLM to talk to (so its narrative is not mute).

    Today that means a brain/global LLM gateway key. (P1 adds `claude -p` as a third
    valid option — when present, BRAIN_LLM_PROVIDER=claude_cli + claude on PATH also
    counts.) Embedding/search runs on the local model regardless; this is about the
    brain's generation path.
    """
    try:
        from core.api.config import settings
    except Exception:
        return (False, "settings not loadable")

    # P1: the claude -p path needs no key — the brain runs on the Claude Code
    # subscription. Counts as configured when selected AND `claude` is on PATH.
    if getattr(settings, "brain_llm_provider", "gateway") == "claude_cli":
        import os
        import shutil

        binary = getattr(settings, "marvis_claude_bin", "claude") or "claude"
        if os.path.isabs(binary) or shutil.which(binary) is not None:
            return (True, "brain runs on the Claude Code subscription (claude -p)")
        return (False, f"BRAIN_LLM_PROVIDER=claude_cli but '{binary}' is not on PATH")

    def _secret(value: object) -> str:
        if value is None:
            return ""
        get = getattr(value, "get_secret_value", None)
        return str(get() or "") if callable(get) else str(value or "")

    brain_key = _secret(getattr(settings, "brain_llm_gateway_api_key", None))
    global_key = _secret(getattr(settings, "llm_gateway_api_key", None))
    if brain_key or global_key:
        return (True, "an LLM gateway key is configured")
    return (False, "no brain LLM configured (set BRAIN_LLM_GATEWAY_API_KEY, or use the local-model/claude -p path)")


def _detect_hooks_installed() -> tuple[bool, str]:
    """The governance hooks are installed in the current repo (.claude/settings*.json
    carries a Marvis PreToolUse hook). Best-effort, repo-relative: outside a repo →
    NOT done (recommended only)."""
    for name in (".claude/settings.json", ".claude/settings.local.json"):
        p = Path.cwd() / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pretool = ((data.get("hooks") or {}).get("PreToolUse") or []) if isinstance(data, dict) else []
        blob = json.dumps(pretool)
        if "safety_bridge" in blob or ".claude/hooks" in blob or "marvis" in blob.lower():
            return (True, f"governance hooks present in {name}")
    return (False, "no Marvis governance hooks in this repo's .claude/settings")


# --------------------------------------------------------------------------- #
# The single source of truth                                                  #
# --------------------------------------------------------------------------- #

ONBOARDING_STATES: tuple[OnboardingState, ...] = (
    # required — without these Marvis does not work
    OnboardingState(
        "cli_on_path", "CLI installed and on PATH", "required",
        "reinstall: uv tool install marvisx-cli", check_names=("cli_on_path",),
    ),
    OnboardingState(
        "runtime_files", "Config present and valid", "required",
        "marvis init", check_names=("config_dir", "config_parseable"),
    ),
    OnboardingState(
        "mcp_registered", "MCP server registered in Claude Code", "required",
        "marvis mcp", detector=_detect_mcp_registered,
    ),
    # recommended — "+unlocks X", never blocks the 100%
    OnboardingState(
        "hooks_installed", "Governance hooks installed", "recommended",
        "marvis hooks install", detector=_detect_hooks_installed,
    ),
    OnboardingState(
        "project_imported", "At least one project imported", "recommended",
        "marvis project import <path>", detector=_detect_project_imported,
    ),
    OnboardingState(
        "code_indexed", "Code indexed (projects with code)", "recommended",
        "marvis project index <slug>", check_names=("Knowledge graph freshness",),
    ),
    OnboardingState(
        "brain_enabled", "Brain enabled and scheduled", "recommended",
        "marvis brain enable", check_names=("brain_schedule",),
    ),
    OnboardingState(
        "llm_configured", "LLM configured (brain not mute)", "recommended",
        "set BRAIN_LLM_GATEWAY_API_KEY (or use the local-model / claude -p path)",
        detector=_detect_llm_configured,
    ),
)


def _state_done(state: OnboardingState, by_name: dict) -> tuple[bool, str]:
    """Resolve a single state to (done, detail). Detector states run their probe;
    reuse states are done iff every named doctor check ran and is level 'ok'."""
    if state.detector is not None:
        try:
            return state.detector()
        except Exception as exc:  # detectors should not raise, but never crash doctor
            return (False, f"probe error: {exc}")
    present = [by_name.get(n) for n in state.check_names]
    if any(c is None for c in present):
        missing = [n for n, c in zip(state.check_names, present) if c is None]
        return (False, f"check(s) not run: {', '.join(missing)}")
    done = all(getattr(c, "level", None) == "ok" for c in present)
    detail = "; ".join(f"{getattr(c, 'name', '?')}={getattr(c, 'level', '?')}" for c in present)
    return (done, detail)


def classify(checks: list) -> dict:
    """Aggregate the onboarding states over the already-run `doctor` checks.

    `complete` is True iff ALL **required** states are done — recommended states do
    not gate it (so opting out of the brain never pins you below 100%).
    """
    by_name = {getattr(c, "name", None): c for c in checks}
    rows = [(s, *_state_done(s, by_name)) for s in ONBOARDING_STATES]
    required = [(s, d, dt) for (s, d, dt) in rows if s.tier == "required"]
    recommended = [(s, d, dt) for (s, d, dt) in rows if s.tier == "recommended"]
    req_done = sum(1 for _, d, _ in required if d)
    rec_done = sum(1 for _, d, _ in recommended if d)
    return {
        "rows": rows,  # list[(OnboardingState, done: bool, detail: str)]
        "required": required,
        "recommended": recommended,
        "required_done": req_done,
        "required_total": len(required),
        "recommended_done": rec_done,
        "recommended_total": len(recommended),
        "complete": req_done == len(required),
    }


def completion_json(summary: dict) -> dict:
    """Machine-readable block for `marvis doctor --json` (additive)."""
    return {
        "complete": summary["complete"],
        "required_done": summary["required_done"],
        "required_total": summary["required_total"],
        "recommended_done": summary["recommended_done"],
        "recommended_total": summary["recommended_total"],
        "states": [
            {"key": s.key, "title": s.title, "tier": s.tier, "done": d, "detail": dt, "fix": s.fix}
            for (s, d, dt) in summary["rows"]
        ],
    }
