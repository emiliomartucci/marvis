#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


_SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = (
    _SCRIPT_PATH.parents[2]
    if _SCRIPT_PATH.parents[1].name == "core"
    else _SCRIPT_PATH.parents[1]
)


def _resolve_default_config() -> Path:
    """Locate config.json, supporting BOTH layouts the bridge ships in.

    - Self-contained install: `marvis hooks install` co-locates this file with
      config.json in <project>/.claude/hooks/, so a sibling config.json wins.
    - Source checkout: the bridge lives at core/scripts/safety_bridge.py and the
      config at <repo>/.claude/hooks/config.json (REPO_ROOT-relative).
    The co-located case is checked first so installed hooks are fully portable and
    never look outside their own directory (the doubled-".claude" bug otherwise).
    """
    sibling = _SCRIPT_PATH.parent / "config.json"
    if sibling.is_file():
        return sibling
    return REPO_ROOT / ".claude" / "hooks" / "config.json"


DEFAULT_CONFIG = _resolve_default_config()
GOVERNANCE_CONFIG_FILENAME = "governance.json"
DEFAULT_GOVERNANCE_PROFILE = "strict"
WRITE_KEYWORD_EXTRA_PATTERNS = (r"\.execute\s*\(",)
GIT_SUBCOMMAND_RE = re.compile(r"\bgit\s+([a-z-]+)\b")
QUOTED_STRING_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'')
RM_RF_ROOT_RE = re.compile(
    r"\brm\s+(?:-[A-Za-z]*r[A-Za-z]*f[A-Za-z]*|-[A-Za-z]*f[A-Za-z]*r[A-Za-z]*)\s+"
    r"(?:--\s+)?(?:/|/\*|'/'|\"/\"|'/?\*'|\"/?\*\")(?:\s|$)"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|authorization|bearer)"
    r"[^=\n:]{0,40}[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}"
)

SAFETY_RULES = frozenset(
    {
        "dangerous-bash",
        "db-write",
        "staging-to-prod",
        "secret-scan",
    }
)
PROCESS_RULES = frozenset(
    {
        "worktree",
        "bash-merge",
        "push-no-task",
        "subtree-push",
        "quality-gate",
    }
)
GOVERNANCE_PROFILES = frozenset({"lite", "strict"})
PROFILE_RULE_STATES: dict[str, dict[str, str]] = {
    "lite": {rule: "block" for rule in SAFETY_RULES}
    | {rule: "warn" for rule in PROCESS_RULES},
    "strict": {rule: "block" for rule in SAFETY_RULES | PROCESS_RULES},
}
RULE_ALIASES = {
    "block-dangerous-bash": "dangerous-bash",
    "rm-rf": "dangerous-bash",
    "force-push": "dangerous-bash",
    "block-db-direct-write": "db-write",
    "prod-write": "staging-to-prod",
    "block-staging-to-prod": "staging-to-prod",
    "scan-secrets": "secret-scan",
    "enforce-worktree": "worktree",
    "worktree-per-edit": "worktree",
    "enforce-no-merge-main": "bash-merge",
    "no-merge-triage": "bash-merge",
    "block-push-no-task": "push-no-task",
    "task-first": "push-no-task",
    "block-subtree-push": "subtree-push",
    "pre-commit": "quality-gate",
}


@dataclass
class Decision:
    allowed: bool
    reason: str | None = None
    rule: str | None = None
    advisory: bool = False


def _deny(reason: str, rule: str) -> Decision:
    return Decision(allowed=False, reason=reason, rule=rule)


def _allow() -> Decision:
    return Decision(allowed=True)


def _advise(reason: str, rule: str) -> Decision:
    """Allow the action but carry a non-blocking warning (OSS advisory mode)."""
    return Decision(allowed=True, reason=reason, rule=rule, advisory=True)


def load_config(config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Hook config missing or invalid: {config_path}") from exc


def _resolve_default_governance_config() -> Path:
    if _SCRIPT_PATH.parent.name == "hooks" and _SCRIPT_PATH.parent.parent.name == ".claude":
        return _SCRIPT_PATH.parent.parent / GOVERNANCE_CONFIG_FILENAME
    return REPO_ROOT / ".claude" / GOVERNANCE_CONFIG_FILENAME


def load_governance_profile(
    config_path: Path | None = None,
) -> tuple[str, str]:
    """Return (profile, source) with a strict legacy fallback.

    Existing Marvis checkouts predate governance.json and have strict hook entries
    already wired in settings.json. Missing or malformed profile config therefore
    stays strict for backward compatibility; fresh OSS installs explicitly write
    governance.json with profile=lite.
    """
    path = config_path or _resolve_default_governance_config()
    if not path.is_file():
        return DEFAULT_GOVERNANCE_PROFILE, "legacy-default"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_GOVERNANCE_PROFILE, f"invalid:{path}"
    if not isinstance(data, dict):
        return DEFAULT_GOVERNANCE_PROFILE, f"invalid:{path}"
    profile = str(data.get("profile") or "").strip().lower()
    if profile not in GOVERNANCE_PROFILES:
        return DEFAULT_GOVERNANCE_PROFILE, f"invalid:{path}"
    return profile, str(path)


def _git_output(args: list[str], cwd: str | None) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def resolve_repo_root(file_path: str | None, cwd: str | None = None) -> str | None:
    if not file_path and not cwd:
        return None

    candidate = Path(file_path or cwd or ".")
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd) / candidate

    search_dir = candidate if candidate.is_dir() else candidate.parent
    while str(search_dir) not in ("", "/") and not search_dir.exists():
        search_dir = search_dir.parent

    return _git_output(
        ["rev-parse", "--show-toplevel"], str(search_dir) if search_dir else cwd
    )


def current_branch(repo_root: str | None, cwd: str | None = None) -> str | None:
    target = repo_root or cwd
    if not target:
        return None
    return _git_output(["rev-parse", "--abbrev-ref", "HEAD"], target)


def package_scripts(package_dir: Path) -> set[str]:
    try:
        data = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
    except Exception:
        return set()

    scripts = data.get("scripts") or {}
    if not isinstance(scripts, dict):
        return set()
    return {str(key) for key in scripts.keys()}


def quality_gate_package_dirs(repo_root: Path, staged_files: list[str]) -> list[Path]:
    discovered: list[Path] = []
    seen: set[str] = set()

    for rel_path in staged_files:
        candidate = repo_root / rel_path
        search_dir = candidate if candidate.is_dir() else candidate.parent

        while search_dir != search_dir.parent:
            package_json = search_dir / "package.json"
            if package_json.is_file():
                key = str(search_dir)
                if key not in seen:
                    scripts = package_scripts(search_dir)
                    if "knip:check" in scripts and "lint" in scripts:
                        discovered.append(search_dir)
                        seen.add(key)
                break

            if search_dir == repo_root:
                break
            search_dir = search_dir.parent

    return discovered


def _protected_branches(config: dict[str, Any]) -> set[str]:
    return set(config.get("branches", {}).get("protected", []))


def _is_protected_branch(branch: str | None, config: dict[str, Any]) -> bool:
    return bool(branch and branch in _protected_branches(config))


def is_whitelisted_path(rel_path: str, config: dict[str, Any]) -> bool:
    worktree = config.get("worktree", {})

    if rel_path in set(worktree.get("blocked_files", [])):
        return False

    suffix = Path(rel_path).suffix
    if suffix and suffix in set(worktree.get("whitelist_extensions", [])):
        return True

    for prefix in worktree.get("whitelist_dirs", []):
        if rel_path.startswith(prefix):
            return True

    if rel_path in set(worktree.get("whitelist_files", [])):
        return True

    for pattern in worktree.get("whitelist_scripts", []):
        if fnmatch(rel_path, pattern):
            return True

    return False


def _strip_quoted_strings(command: str) -> str:
    return QUOTED_STRING_RE.sub(" ", command)


def check_worktree(
    file_path: str | None, cwd: str | None, config: dict[str, Any]
) -> Decision:
    if not file_path:
        return _allow()

    repo_root = resolve_repo_root(file_path, cwd)
    if not repo_root:
        return _allow()

    branch = current_branch(repo_root, cwd)
    if not _is_protected_branch(branch, config):
        return _allow()

    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = Path(cwd or repo_root) / candidate

    try:
        rel_path = candidate.relative_to(repo_root).as_posix()
    except Exception:
        rel_path = candidate.as_posix().removeprefix(f"{repo_root.rstrip('/')}/")

    if is_whitelisted_path(rel_path, config):
        return _allow()

    return _deny(
        f"Blocked: editing '{rel_path}' on branch '{branch}'. "
        "Reason: enforce-worktree rule — code edits (non-metadata) on protected branches must flow through PR review. "
        "Fix: create a worktree from a feat/task-{task_id} branch (mcp__pir__register_branch), edit there, then mcp__pir__submit_pr. "
        "If this file should be editable on main (metadata/docs/config), add its extension/prefix to "
        ".claude/hooks/config.json under worktree.whitelist_{extensions,dirs,files}.",
        "worktree",
    )


def check_bash_merge(
    command: str | None, cwd: str | None, config: dict[str, Any]
) -> Decision:
    if not command:
        return _allow()

    stripped = _strip_quoted_strings(command)
    matches = {match.group(1) for match in GIT_SUBCOMMAND_RE.finditer(stripped)}
    if not matches.intersection(
        {"merge", "cherry-pick", "rebase", "am", "apply", "stash"}
    ):
        return _allow()

    branch = current_branch(resolve_repo_root(None, cwd), cwd)
    if not _is_protected_branch(branch, config):
        return _allow()

    return _deny(
        f"Blocked: git merge/cherry-pick/rebase/am/apply/stash on protected branch '{branch}'. "
        "Reason: constitution rule 3 (No Merge on Main) — merges must be human-approved via Triage to prevent bypass of PR review. "
        "Fix: from a feat/task-{task_id} worktree → commit → `mcp__pir__submit_pr(task_id, title, body)` → an admin approves in Triage (your console) → merge happens via console cookie-auth. "
        "Bypass: none available to agents; this is an immutable constitution rule.",
        "bash-merge",
    )


def check_db_direct_write(command: str | None, config: dict[str, Any]) -> Decision:
    if not command:
        return _allow()

    db_cfg = config.get("db_protection", {})
    db_patterns = db_cfg.get("db_patterns", [])
    write_keywords = list(db_cfg.get("write_keywords", [])) + list(
        WRITE_KEYWORD_EXTRA_PATTERNS
    )
    if not db_patterns or not write_keywords:
        return _allow()

    if not any(re.search(pattern, command, re.IGNORECASE) for pattern in db_patterns):
        return _allow()
    if not any(
        re.search(pattern, command, re.IGNORECASE) for pattern in write_keywords
    ):
        return _allow()

    return _deny(
        "Blocked: direct write to a PiR SQLite DB (INSERT/UPDATE/DELETE/DROP/ALTER or .execute()). "
        "Reason: constitution rule 2 (No Hotfix Prod) — DB writes must flow through the PiR API so audit trail, RBAC, and invariants are enforced. "
        "Fix: use the PiR API instead — mcp__pir__create_task / update_task / create_learning / boost_document / register_branch / submit_pr / close_pr etc. "
        "If a write endpoint is missing, create a PiR task first, then add the route to api/routers/ in a feature branch.",
        "db-write",
    )


_PUSH_BULK_FLAGS = frozenset({"--all", "--mirror", "--tags"})


def _extract_push_targets(command: str) -> list[str] | None:
    """Extract target branch names from a git push command's refspec args.

    Returns a list of target branch names (dst side of <src>:<dst>, or the
    refspec itself when no colon) when the push contains at least one explicit
    refspec. Returns None when the command uses no refspec or a bulk flag
    (--all/--mirror/--tags), so the caller can fall back to current_branch.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    try:
        push_idx = tokens.index("push")
    except ValueError:
        return None
    args = tokens[push_idx + 1 :]

    # Stop at the first shell operator so redirections (2>&1, >file) and pipe
    # chains (| tail) don't get interpreted as refspecs. shlex.split preserves
    # these as separate tokens.
    _SHELL_STOP = {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "2>&1", "2>"}
    trimmed: list[str] = []
    for tok in args:
        if tok in _SHELL_STOP or tok.startswith((">", "<", "|", "&", ";")):
            break
        trimmed.append(tok)
    args = trimmed

    if any(tok in _PUSH_BULK_FLAGS for tok in args):
        return None

    positional = [a for a in args if not a.startswith("-")]
    # First positional is the remote (origin/upstream/...). Refspecs follow.
    if len(positional) < 2:
        return None

    targets: list[str] = []
    for rs in positional[1:]:
        dst = rs.split(":", 1)[-1] if ":" in rs else rs
        dst = dst.removeprefix("refs/heads/")
        if dst:
            targets.append(dst)
    return targets or None


def check_push_no_task(
    command: str | None, cwd: str | None, config: dict[str, Any]
) -> Decision:
    if not command or not re.search(r"(^|\s)git\s+push(\s|$)", command):
        return _allow()

    pattern = config.get("branches", {}).get("task_pattern")
    protected = set(config.get("branches", {}).get("protected", []))

    # Validate explicit refspec targets first — this is the correct abstraction
    # level for `git push <remote> [<src>:]<dst>`. Fixes subagent-worktree case
    # where the main agent's CWD branch is unrelated to the push target.
    targets = _extract_push_targets(command)
    if targets is not None:
        ambiguous_targets = [t for t in targets if t == "HEAD"]
        explicit_targets = [t for t in targets if t != "HEAD"]
        for target in explicit_targets:
            if target in protected:
                continue
            if pattern and re.match(pattern, target):
                continue
            return _deny(
                f"Blocked: push target '{target}' is not associated with a PiR task. "
                f"Reason: block-push-no-task rule — every code change needs traceability back to a tracked task (constitution rule 1). "
                f"Expected branch pattern: {pattern!r} (e.g. feat/task-<uuid>). "
                f"Fix: 1) create task via mcp__pir__create_task, 2) mcp__pir__register_branch(task_id, branch_name) to get a canonical name, 3) retry push. "
                f"Or push to a protected branch ({', '.join(sorted(protected)) or '<none configured>'}) if you're the orchestrator merging an approved PR.",
                "push-no-task",
            )
        if not ambiguous_targets:
            return _allow()
        # HEAD target resolves to current branch remotely — fall through to
        # current_branch validation so we don't silently allow unrelated work.

    # Fallback: no explicit refspec (e.g. `git push`, `git push origin`, or
    # bulk flags). Validate the current branch as before.
    branch = current_branch(resolve_repo_root(None, cwd), cwd)
    if not branch:
        return _allow()
    if _is_protected_branch(branch, config):
        return _allow()

    if pattern and re.match(pattern, branch):
        return _allow()

    return _deny(
        f"Blocked: current branch '{branch}' is not associated with a PiR task. "
        f"Reason: block-push-no-task rule — every code change needs traceability back to a tracked task (constitution rule 1). "
        f"Expected branch pattern: {pattern!r} (e.g. feat/task-<uuid>). "
        "Fix: 1) create task via mcp__pir__create_task(title, project, ...), 2) check out a branch matching feat/task-<task_id>, "
        "3) mcp__pir__register_branch(task_id, branch_name) to register the draft PR, 4) retry push.",
        "push-no-task",
    )


def check_subtree_push(
    command: str | None, cwd: str | None, config: dict[str, Any]
) -> Decision:
    if not command or not re.search(
        r"git\s+subtree\s+push|git\s+push\s+console-deploy", command
    ):
        return _allow()

    branch = current_branch(resolve_repo_root(None, cwd), cwd)
    if _is_protected_branch(branch, config):
        return _allow()

    return _deny(
        f"Blocked: git subtree push (or push to console-deploy remote) from branch '{branch or 'unknown'}'. "
        "Reason: constitution rule 5 (No Bypass Orchestrator) — deploys must flow through merge-to-main so the Triage gate stays meaningful. "
        "Fix: submit the PR (`mcp__pir__submit_pr`), let an admin merge it via Triage, THEN run `bash scripts/deploy-from-ref.sh origin/main all` from main. "
        "Never subtree-push from a feature branch — it would publish code that has not been reviewed.",
        "subtree-push",
    )


def check_staging_to_prod(command: str | None, _config: dict[str, Any]) -> Decision:
    if not command:
        return _allow()

    lowered = command.lower()
    is_file_copy = bool(
        re.search(r"\b(cp|rsync|scp|install|mv)\b", lowered)
    )
    if is_file_copy and "/data/pir/api/" in command:
        return _deny(
            "Blocked: a file-copy command (cp/rsync/scp/install/mv) targets the production path '/data/pir/api/'. "
            "Reason: staging-to-prod rule — hot-copying a staging or working tree directly into production bypasses code review. "
            "NOTE: this is a substring match on the FULL command string, so an innocent payload that happens to include '/data/pir/api/' may also trip. "
            "Fix: if you were actually trying to deploy, run the official deploy script AFTER the PR is merged. "
            "If you were only sending an API request (curl/wget), use the MCP tool instead (e.g. `mcp__pir__submit_pr`) — it bypasses shell matching entirely. "
            "If neither applies, rephrase the command so it doesn't copy into '/data/pir/api/' literally.",
            "staging-to-prod",
        )
    return _allow()


def _command_has_git_force_push(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for idx, tok in enumerate(tokens):
        if tok != "git":
            continue
        try:
            push_idx = tokens.index("push", idx + 1)
        except ValueError:
            continue
        push_args = tokens[push_idx + 1 :]
        for arg in push_args:
            if arg in {"|", "||", "&&", ";", "&"}:
                break
            if arg in {"-f", "--force"} or arg.startswith("--force"):
                return True
    return False


def check_dangerous_bash(command: str | None, _config: dict[str, Any]) -> Decision:
    if not command:
        return _allow()

    if RM_RF_ROOT_RE.search(command):
        return _deny(
            "Blocked: destructive root delete command (`rm -rf /`) detected. "
            "Reason: dangerous-bash safety rule — irreversible host damage must be blocked in every governance profile.",
            "dangerous-bash",
        )

    if _command_has_git_force_push(command):
        return _deny(
            "Blocked: force push detected. "
            "Reason: dangerous-bash safety rule — force-push can rewrite shared history and is blocked in every governance profile.",
            "dangerous-bash",
        )

    return _allow()


def check_secret_scan(command: str | None, cwd: str | None, _config: dict[str, Any]) -> Decision:
    if not command or not re.search(r"\bgit\s+commit\b", command):
        return _allow()

    repo_root = resolve_repo_root(None, cwd)
    if not repo_root:
        return _allow()

    diff = _git_output(["diff", "--cached", "--unified=0", "--no-ext-diff"], repo_root)
    if diff is None:
        return _deny(
            "Blocked: secret scan could not inspect the staged diff before commit. "
            "Reason: secret-scan safety rule must not fail open.",
            "secret-scan",
        )

    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if SECRET_ASSIGNMENT_RE.search(line[1:]):
            return _deny(
                "Blocked: possible secret in staged diff. "
                "Reason: secret-scan safety rule protects credentials in every governance profile. "
                "Fix: remove the secret from the commit and use the BYOK vault or environment configuration instead.",
                "secret-scan",
            )

    return _allow()


def check_quality_gate(
    command: str | None, cwd: str | None, config: dict[str, Any]
) -> Decision:
    """Block git commit if staged TS/JS package dirs fail knip or lint checks.

    First rule in safety_bridge that shells out to non-git processes (npm).
    Expect ~2-5s execution time vs <10ms for other rules.
    Only triggers on `git commit` with staged files inside package dirs that
    expose both `knip:check` and `lint` scripts.
    """
    if not command or not re.search(r"\bgit\s+commit\b", command):
        return _allow()

    repo_root = resolve_repo_root(None, cwd)
    if not repo_root:
        return _allow()

    staged = _git_output(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR"], repo_root
    )
    staged_files = (
        [line.strip() for line in staged.splitlines() if line.strip()] if staged else []
    )
    if not staged_files:
        return _allow()

    repo_root_path = Path(repo_root)
    package_dirs = quality_gate_package_dirs(repo_root_path, staged_files)
    if not package_dirs:
        return _allow()

    for package_dir in package_dirs:
        if not (package_dir / "node_modules").is_dir():
            continue

        package_label = (
            str(package_dir.relative_to(repo_root_path))
            if package_dir != repo_root_path
            else "."
        )

        try:
            knip = subprocess.run(
                ["npm", "run", "knip:check"],
                cwd=str(package_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return _deny(
                f"Blocked: quality gate timed out running `npm run knip:check` in {package_label} (>30s). "
                "Reason: knip check is required pre-commit to block dead-export regressions. "
                f"Fix: `cd {package_label} && npm install` to refresh node_modules, then retry commit. "
                "If knip is genuinely slow on this machine, run it manually first to warm the cache.",
                "quality-gate",
            )
        if knip.returncode != 0:
            out = (knip.stderr or knip.stdout or "")[:500]
            return _deny(
                f"Blocked: knip regression in {package_label}. "
                "Reason: quality gate — staged files introduce dead exports / orphan files / unused deps (see .claude/docs/quality-escape-hatch.md). "
                f"Fix: `cd {package_label} && npm run knip:check` locally, remove the dead code OR update the knip baseline if the diff is legitimate, then re-stage and retry commit. "
                f"Knip output (truncated 500 chars):\n{out}",
                "quality-gate",
            )

        try:
            lint = subprocess.run(
                ["npm", "run", "lint"],
                cwd=str(package_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return _deny(
                f"Blocked: quality gate timed out running `npm run lint` in {package_label} (>60s). "
                "Reason: lint is required pre-commit to block SonarJS / react-you-might-not-need-an-effect violations. "
                f"Fix: `cd {package_label} && npm install` to refresh node_modules, then retry commit.",
                "quality-gate",
            )
        if lint.returncode != 0:
            out = (lint.stderr or lint.stdout or "")[:500]
            return _deny(
                f"Blocked: new ESLint violations in {package_label}. "
                "Reason: quality gate — lint runs SonarJS + react-you-might-not-need-an-effect rules to prevent anti-patterns. "
                f"Fix: `cd {package_label} && npm run lint` locally, fix the violations (or narrowly disable with an inline comment + justification per .claude/docs/quality-escape-hatch.md), then re-stage and retry commit. "
                f"Lint output (truncated 500 chars):\n{out}",
                "quality-gate",
            )

    _run_impeccable_warning(staged_files, repo_root_path)

    return _allow()


def _run_impeccable_warning(staged_files: list[str], repo_root: Path) -> None:
    """Print impeccable UI anti-pattern report to stderr. Never blocks the commit."""
    targets = [
        str(repo_root / f)
        for f in staged_files
        if f.startswith("console/src/")
        and f.rsplit(".", 1)[-1] in ("tsx", "jsx", "ts", "js", "css", "html")
    ]
    if not targets:
        return

    try:
        result = subprocess.run(
            ["impeccable", "detect", "--fast", "--json", *targets],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return

    # impeccable writes JSON to stderr and exits non-zero when it finds issues.
    raw = result.stdout.strip() or result.stderr.strip() or "[]"
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(issues, list) or not issues:
        return

    counts: dict[str, int] = {}
    for issue in issues:
        ap = issue.get("antipattern", "unknown")
        counts[ap] = counts.get(ap, 0) + 1
    summary = ", ".join(
        f"{n}x {ap}" for ap, n in sorted(counts.items(), key=lambda x: -x[1])
    )

    print(
        f"\n[impeccable] {len(issues)} UI anti-pattern issue(s) in staged files: {summary}",
        file=sys.stderr,
    )
    repo_prefix = str(repo_root) + "/"
    for issue in issues[:5]:
        rel = issue.get("file", "").replace(repo_prefix, "")
        line = issue.get("line", "?")
        snippet = (issue.get("snippet") or "").strip()[:80]
        print(
            f"  {rel}:{line}  {issue.get('antipattern')}  {snippet}",
            file=sys.stderr,
        )
    if len(issues) > 5:
        print(
            f"  ... and {len(issues) - 5} more. Full scan: impeccable detect console/src/",
            file=sys.stderr,
        )


RULE_CHECKS = {
    "dangerous-bash": lambda file_path, command, cwd, config: check_dangerous_bash(
        command, config
    ),
    "worktree": lambda file_path, command, cwd, config: check_worktree(
        file_path, cwd, config
    ),
    "bash-merge": lambda file_path, command, cwd, config: check_bash_merge(
        command, cwd, config
    ),
    "db-write": lambda file_path, command, cwd, config: check_db_direct_write(
        command, config
    ),
    "push-no-task": lambda file_path, command, cwd, config: check_push_no_task(
        command, cwd, config
    ),
    "subtree-push": lambda file_path, command, cwd, config: check_subtree_push(
        command, cwd, config
    ),
    "staging-to-prod": lambda file_path, command, cwd, config: check_staging_to_prod(
        command, config
    ),
    "secret-scan": lambda file_path, command, cwd, config: check_secret_scan(
        command, cwd, config
    ),
    "quality-gate": lambda file_path, command, cwd, config: check_quality_gate(
        command, cwd, config
    ),
}

ACTION_RULES = {
    "file_write": ("worktree",),
    "bash_command": (
        "dangerous-bash",
        "secret-scan",
        "staging-to-prod",
        "bash-merge",
        "db-write",
        "push-no-task",
        "subtree-push",
        "quality-gate",
    ),
}


def resolve_rule_key(rule: str) -> str | None:
    canonical = RULE_ALIASES.get(rule, rule)
    if canonical in RULE_CHECKS:
        return canonical
    return None


def governance_rule_state(rule: str) -> tuple[str, str, str | None]:
    canonical = resolve_rule_key(rule)
    if canonical is None:
        return "warn", "undefined-rule", None
    profile, source = load_governance_profile()
    state = PROFILE_RULE_STATES.get(profile, PROFILE_RULE_STATES["strict"]).get(
        canonical, "block"
    )
    if canonical in SAFETY_RULES:
        return state, "safety", canonical
    return state, f"profile:{profile}", canonical


def _apply_governance_state(rule: str, decision: Decision) -> Decision:
    if decision.allowed:
        return decision

    state, _source, canonical = governance_rule_state(rule)
    if canonical is None:
        return _advise(
            f"Advisory: safety bridge rule key '{rule}' is not defined. "
            "Allowed instead of hard-blocking because unresolved rule keys must not create a fail-closed outage. "
            "Fix: keep the old key as an alias or update the hook reference in a separate additive change.",
            rule,
        )
    if state == "block":
        return decision
    if state == "warn":
        return _advise(
            f"Advisory ({_source}): {decision.reason or 'rule would block in strict governance'} "
            "This process rule is non-blocking in lite governance and BLOCK in strict.",
            canonical,
        )
    return _allow()


def evaluate_rule(
    rule: str,
    *,
    file_path: str | None = None,
    command: str | None = None,
    cwd: str | None = None,
    config: dict[str, Any] | None = None,
) -> Decision:
    canonical = resolve_rule_key(rule)
    if canonical is None:
        return _advise(
            f"Advisory: safety bridge rule key '{rule}' is not defined. "
            "Allowed instead of hard-blocking because unresolved rule keys must not create a fail-closed outage. "
            "Fix: keep the old key as an alias or update the hook reference in a separate additive change.",
            rule,
        )
    cfg = config or load_config()
    return _apply_governance_state(
        canonical, RULE_CHECKS[canonical](file_path, command, cwd, cfg)
    )


def evaluate_action(
    action_type: str,
    *,
    file_path: str | None = None,
    command: str | None = None,
    cwd: str | None = None,
    config: dict[str, Any] | None = None,
) -> Decision:
    cfg = config or load_config()
    for rule in ACTION_RULES[action_type]:
        decision = evaluate_rule(
            rule, file_path=file_path, command=command, cwd=cwd, config=cfg
        )
        if not decision.allowed:
            return decision
    return _allow()


def _hook_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def _hook_response(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def _hook_advisory_response(reason: str) -> str:
    """Allow the action but attach a non-blocking warning (OSS advisory mode)."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": reason,
            }
        }
    )


def _run_hook(rule: str) -> int:
    try:
        payload = _hook_payload()
        decision = evaluate_rule(
            rule,
            file_path=payload.get("tool_input", {}).get("file_path"),
            command=payload.get("tool_input", {}).get("command"),
            cwd=payload.get("cwd"),
        )
    except Exception as exc:
        print(
            _hook_response(
                f"Blocked (fail-closed): safety bridge rule '{rule}' crashed: {exc}. "
                "Reason: when a hook rule raises, we fail-closed so an unchecked action can't slip through. "
                "Fix: re-read scripts/safety_bridge.py (look at check_" + rule.replace("-", "_") + "), "
                "verify .claude/hooks/config.json is valid JSON, and check the tool_input payload shape. "
                "If the crash is deterministic, file a PiR task before attempting to bypass."
            )
        )
        return 0

    if not decision.allowed and decision.reason:
        print(_hook_response(decision.reason))
    elif decision.advisory and decision.reason:
        print(_hook_advisory_response(decision.reason))
    return 0


def _run_check(
    action_type: str, file_path: str | None, command: str | None, cwd: str | None
) -> int:
    try:
        decision = evaluate_action(
            action_type, file_path=file_path, command=command, cwd=cwd
        )
    except Exception as exc:
        decision = _deny(f"Safety bridge failure: {exc}", "bridge-error")

    print(
        json.dumps(
            {
                "allowed": decision.allowed,
                "reason": decision.reason,
                "rule": decision.rule,
                "advisory": decision.advisory,
            }
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified safety bridge for MarvisX hooks and MCP checks"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    hook = subparsers.add_parser("hook")
    hook.add_argument("--rule", required=True)

    check = subparsers.add_parser("check")
    check.add_argument(
        "--action-type", choices=sorted(ACTION_RULES.keys()), required=True
    )
    check.add_argument("--file-path")
    check.add_argument("--command")
    check.add_argument("--cwd")

    args = parser.parse_args()

    if args.mode == "hook":
        return _run_hook(args.rule)
    return _run_check(args.action_type, args.file_path, args.command, args.cwd)


if __name__ == "__main__":
    raise SystemExit(main())
