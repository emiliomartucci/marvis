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
import re
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
    guide_detail: str = ""
    # Exactly one mechanism: reuse existing doctor checks by name, OR a detector.
    check_names: tuple[str, ...] = ()
    detector: Detector | None = field(default=None)


# --------------------------------------------------------------------------- #
# Net-new detectors (sync, read-only, never raise)                            #
# --------------------------------------------------------------------------- #


def _detect_mcp_registered() -> tuple[bool, str]:
    """Claude Code can expose Marvis either via plugin or via `mcpServers.marvis`.

    Absent file / other MCP client → NOT done with a soft note (never an error):
    we cannot prove registration, so we report it as a gap, not a failure. Only the
    presence of the key is read — never the file's contents.
    """
    try:
        from core.cli.marvis_mcp import _detect_claude_plugin

        plugin = _detect_claude_plugin()
        if plugin["detected"]:
            return (True, f"{plugin['package']} plugin detected in Claude Code")
    except Exception:
        pass

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


def _detect_console_available() -> tuple[bool, str]:
    """The installed wheel includes the static GUI export."""
    try:
        import importlib.resources as res

        root = res.files("core.api").joinpath("console_dist")
        if root.joinpath("index.html").is_file():
            return (True, "local Console GUI assets are packaged")
        return (False, "core/api/console_dist/index.html not found in this build")
    except Exception as exc:  # noqa: BLE001
        return (False, f"could not inspect packaged Console assets: {exc}")


def _detect_console_autostart() -> tuple[bool, str]:
    """The local API autostart artifact is installed and the loader reports it active."""
    try:
        from core.cli import marvis_console
    except Exception as exc:  # noqa: BLE001
        return (False, f"could not inspect Console autostart: {exc}")

    try:
        backend = marvis_console._backend()
        artifact: Path | None = None
        if backend == "launchd":
            artifact = marvis_console._launchd_plist_path()
        elif backend == "systemd":
            artifact = marvis_console._systemd_service_path()

        if artifact is not None and not artifact.exists():
            return (False, f"{backend} autostart artifact not found")

        status = marvis_console._autostart_status()
    except Exception as exc:  # noqa: BLE001
        return (False, f"could not inspect Console autostart: {exc}")

    backend = str(status.get("backend") or "unknown")
    loader = bool(status.get("ok", status.get("loader")))
    health = status.get("healthz") if isinstance(status.get("healthz"), dict) else {}
    loader_detail = str(status.get("loader_detail") or "loader did not report active")
    health_detail = str(health.get("detail") or "local API health not checked")

    if loader:
        if health.get("ok"):
            return (True, f"{backend} autostart enabled; local API answered {health.get('path')}")
        return (True, f"{backend} autostart enabled; {health_detail}")
    if status.get("file_exists"):
        return (False, f"{backend} autostart artifact exists but is not active ({loader_detail})")
    return (False, f"{backend} autostart not enabled")


def _read_setup_md() -> tuple[Path, str | None]:
    try:
        from core.cli.marvis_init import _default_vault_dir

        path = _default_vault_dir() / "setup.md"
        if not path.exists():
            return (path, None)
        return (path, path.read_text(encoding="utf-8"))
    except Exception:
        return (Path.home() / ".marvis" / "setup.md", None)


def _detect_setup_authored() -> tuple[bool, str]:
    """The GUI/agent authored setup.md contract exists with the four v1 sections."""
    path, content = _read_setup_md()
    if not content:
        return (False, f"{path} not found")
    required = ("Identità", "Sorgenti", "Ritmo", "Fonti del brain")
    missing = [section for section in required if f"## {section}" not in content]
    if missing:
        return (False, f"setup.md missing section(s): {', '.join(missing)}")
    return (True, f"{path} contains the authored setup.md sections")


def _detect_setup_sources() -> tuple[bool, str]:
    """The authored setup.md has explicit source/exclusion lines from the wizard."""
    path, content = _read_setup_md()
    if not content:
        return (False, f"{path} not found")
    marker = "## Sorgenti"
    if marker not in content:
        return (False, "setup.md has no Sorgenti section")
    body = content.split(marker, 1)[1].split("\n## ", 1)[0]
    lower = body.lower()
    has_sources = bool(
        re.search(r"(cartelle|sources?|folders?).*:\s*\S", lower)
        or any(line.strip().startswith("- /") for line in body.splitlines())
    )
    has_exclusions = bool(
        re.search(r"(esclusioni|exclusions?|ignore).*:\s*\S", lower)
    )
    if has_sources and has_exclusions:
        return (True, "setup.md records work sources and exclusions")
    return (False, "setup.md Sorgenti does not yet record explicit sources and exclusions")


def _detect_demo_seeded() -> tuple[bool, str]:
    """Casa Lorenzi demo project marker exists in the configured projects root."""
    try:
        from core.api.routers.projects import PROJECT_DIRS

        roots = [Path(p) for p in PROJECT_DIRS]
    except Exception:
        roots = []
    for root in roots:
        marker = root / "casa-lorenzi" / ".marvis-demo.json"
        if marker.exists():
            return (True, f"Casa Lorenzi demo marker found at {marker}")
    return (False, "Casa Lorenzi demo data not seeded")


# --------------------------------------------------------------------------- #
# The single source of truth                                                  #
# --------------------------------------------------------------------------- #

ONBOARDING_STATES: tuple[OnboardingState, ...] = (
    # required — without these Marvis does not work
    OnboardingState(
        "cli_on_path", "CLI installed and on PATH", "required",
        "reinstall: uv tool install marvisx-cli",
        "the `marvis` command resolves in your shell.",
        check_names=("cli_on_path",),
    ),
    OnboardingState(
        "runtime_files", "Config present and valid", "required",
        "marvis init",
        "`marvis init` created a readable `settings.yaml` in the vault.",
        check_names=("config_dir", "config_parseable"),
    ),
    OnboardingState(
        "mcp_registered", "MCP server registered in Claude Code", "required",
        "marvis mcp",
        "your agent can call the Marvis tools from Claude Code.",
        detector=_detect_mcp_registered,
    ),
    # recommended — "+unlocks X", never blocks the 100%
    OnboardingState(
        "hooks_installed", "Governance hooks installed", "recommended",
        "marvis hooks install",
        "the repo has the Marvis safety and quality hooks installed.",
        detector=_detect_hooks_installed,
    ),
    OnboardingState(
        "project_imported", "At least one project imported", "recommended",
        "marvis project import <path>",
        "Marvis has at least one project folder to work on.",
        detector=_detect_project_imported,
    ),
    OnboardingState(
        "code_indexed", "Code indexed (projects with code)", "recommended",
        "marvis project index <slug>",
        "the Knowledge Graph has indexed code for project search and impact checks.",
        check_names=("Knowledge graph freshness",),
    ),
    OnboardingState(
        "brain_enabled", "Brain enabled and scheduled", "recommended",
        "marvis brain enable",
        "daily reflection can write the brain journal without manual runs.",
        check_names=("brain_schedule",),
    ),
    OnboardingState(
        "llm_configured", "LLM configured (brain not mute)", "recommended",
        "set BRAIN_LLM_GATEWAY_API_KEY (or use the local-model / claude -p path)",
        "the brain has a writing model for narrative summaries and citations.",
        detector=_detect_llm_configured,
    ),
    OnboardingState(
        "console_available", "Local Console GUI packaged", "recommended",
        "pip install -U marvisx-cli, then run: marvis console",
        "the installed wheel includes the browser GUI served by the local API.",
        detector=_detect_console_available,
    ),
    OnboardingState(
        "console_autostart_enabled", "Console autostart enabled", "recommended",
        "marvis autostart enable",
        "the local API starts at login so the Console icon does not open a dead page.",
        detector=_detect_console_autostart,
    ),
    OnboardingState(
        "setup_md_authored", "setup.md authored contract present", "recommended",
        "marvis console, then complete the 5-step onboarding wizard",
        "the vault has `setup.md` with Identità, Sorgenti, Ritmo, and Fonti del brain.",
        detector=_detect_setup_authored,
    ),
    OnboardingState(
        "setup_sources_configured", "Work sources and exclusions configured", "recommended",
        "marvis console, then complete the wizard's Sources step",
        "`setup.md` records the folders to index and the folders to exclude.",
        detector=_detect_setup_sources,
    ),
    OnboardingState(
        "demo_seeded", "Casa Lorenzi demo data seeded", "recommended",
        "marvis console, then seed the Casa Lorenzi demo in the wizard",
        "the optional badged demo data is present and can be removed later.",
        detector=_detect_demo_seeded,
    ),
)

GUIDE_ONBOARDING_STATES_MARKER = "<!-- marvis:onboarding-states -->"
GUIDE_ONBOARDING_STATES_START = "<!-- marvis:onboarding-states:start -->"
GUIDE_ONBOARDING_STATES_END = "<!-- marvis:onboarding-states:end -->"


def guide_completion_markdown() -> str:
    """Render the guide's onboarding-state list from the doctor source of truth."""
    lines: list[str] = []
    labels = {
        "required": "**Required** (without these Marvis does not work):",
        "recommended": "**Recommended** (each unlocks more):",
    }
    for tier in ("required", "recommended"):
        lines.append(labels[tier])
        lines.append("")
        for state in ONBOARDING_STATES:
            if state.tier != tier:
                continue
            detail = state.guide_detail.rstrip(".") or state.title
            fix = state.fix.rstrip(".")
            lines.append(f"- **{state.title}** — {detail}. Fix: `{fix}`.")
        lines.append("")
    return "\n".join(lines).rstrip()


def inject_guide_completion_markdown(text: str) -> str:
    """Replace the guide's generated onboarding block with the current state list."""
    block = guide_completion_markdown()
    pattern = re.compile(
        f"{re.escape(GUIDE_ONBOARDING_STATES_START)}.*?"
        f"{re.escape(GUIDE_ONBOARDING_STATES_END)}",
        flags=re.S,
    )
    if pattern.search(text):
        return pattern.sub(block, text)
    return text.replace(GUIDE_ONBOARDING_STATES_MARKER, block)


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
