#!/usr/bin/env python3
"""Fail-closed integrity gate for security policy, hooks, and exporters.

The gate uses only the Python standard library so its CI job needs no package
installation. GitHub branch protection remains the authority for requiring an
independent CODEOWNER approval; this script verifies the repository half of
that contract and never claims that a review happened.
"""
from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path
from typing import Any


POLICY_PATH = Path("contracts/security/protected_paths.json")
EXPECTED_SCHEMA = "marvis-security-protected-paths/v1"
REQUIRED_GROUPS = {
    "self_protection",
    "hooks",
    "projections",
    "authentication",
    "public_claims",
}
WORKFLOW_PATH = Path(".github/workflows/security-policy-gate.yml")
PUBLIC_CLAIMS_PATH = Path("core/scripts/quality-gates/verify_public_claims.py")


def _load_json(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing required file: {path}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON at {path}: {type(exc).__name__}")
        return None
    if not isinstance(value, dict):
        errors.append(f"policy root must be an object: {path}")
        return None
    return value


def _declared_paths(policy: dict[str, Any], errors: list[str]) -> list[str]:
    groups = policy.get("groups")
    if not isinstance(groups, dict):
        errors.append("policy groups must be an object")
        return []
    missing_groups = REQUIRED_GROUPS - set(groups)
    if missing_groups:
        errors.append(f"policy missing groups: {', '.join(sorted(missing_groups))}")

    paths: list[str] = []
    for group_name, raw_paths in groups.items():
        if not isinstance(raw_paths, list) or not raw_paths:
            errors.append(f"policy group {group_name!r} must be a non-empty list")
            continue
        for raw_path in raw_paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                errors.append(f"policy group {group_name!r} contains an invalid path")
                continue
            path = raw_path.strip()
            parts = Path(path.rstrip("/")).parts
            if path.startswith(("/", "~")) or ".." in parts or "\\" in path:
                errors.append(f"protected path is not repository-relative: {path}")
                continue
            paths.append(path)
    if len(paths) != len(set(paths)):
        errors.append("protected paths must be unique across groups")
    return sorted(set(paths))


def _validate_codeowners(
    root: Path, declared_paths: list[str], owners: list[str], errors: list[str]
) -> None:
    path = root / ".github/CODEOWNERS"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        errors.append(f"cannot read CODEOWNERS: {type(exc).__name__}")
        return

    rules: dict[str, set[str]] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 2:
            rules[fields[0]] = set(fields[1:])
    expected_owners = set(owners)
    for protected_path in declared_paths:
        pattern = "/" + protected_path
        actual = rules.get(pattern)
        if actual is None:
            errors.append(f"CODEOWNERS missing explicit protected rule: {pattern}")
        elif not expected_owners.issubset(actual):
            errors.append(f"CODEOWNERS rule lacks declared owner: {pattern}")


def _validate_workflow(root: Path, declared_paths: list[str], errors: list[str]) -> None:
    path = root / WORKFLOW_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"cannot read security workflow: {type(exc).__name__}")
        return
    def strip_comment(raw: str) -> str:
        quote: str | None = None
        escaped = False
        for index, character in enumerate(raw):
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote == '"':
                escaped = True
                continue
            if character in {"'", '"'}:
                quote = None if quote == character else character if quote is None else quote
                continue
            if character == "#" and quote is None and (
                index == 0 or raw[index - 1].isspace()
            ):
                return raw[:index].rstrip()
        return raw.rstrip()

    active: list[tuple[int, str]] = []
    for raw in text.splitlines():
        content = strip_comment(raw)
        if not content.strip():
            continue
        active.append((len(content) - len(content.lstrip(" ")), content.strip()))

    def top_section(name: str) -> list[tuple[int, str]]:
        marker = f"{name}:"
        for index, (indent, content) in enumerate(active):
            if indent == 0 and content == marker:
                end = len(active)
                for candidate in range(index + 1, len(active)):
                    if active[candidate][0] == 0:
                        end = candidate
                        break
                return active[index + 1 : end]
        errors.append(f"security workflow missing structural section: {name}")
        return []

    def scalar(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    on_section = top_section("on")
    event_names = {
        content[:-1]
        for indent, content in on_section
        if indent == 2 and content.endswith(":")
    }
    if event_names != {"pull_request"}:
        errors.append("security workflow must declare only the pull_request event")

    permissions = top_section("permissions")
    active_permissions = {
        content for indent, content in permissions if indent == 2
    }
    if active_permissions != {"contents: read"}:
        errors.append("security workflow permissions must be exactly active contents: read")

    concurrency = top_section("concurrency")
    if (2, "cancel-in-progress: true") not in concurrency:
        errors.append("security workflow must actively cancel superseded PR runs")

    jobs = top_section("jobs")
    if (4, "timeout-minutes: 3") not in jobs:
        errors.append("security workflow validate job must have timeout-minutes: 3")
    for _indent, content in jobs:
        if content.startswith("continue-on-error:"):
            value = scalar(content.partition(":")[2]).lower()
            if value != "false":
                errors.append(
                    "security workflow must not enable continue-on-error"
                )
        if content.startswith("if:"):
            errors.append(
                "security workflow validate job and steps must not declare an explicit if"
            )
    run_values = [
        scalar(content.partition(":")[2])
        for indent, content in jobs
        if indent >= 6 and content.startswith("run:")
    ]
    if run_values != ["python3 core/scripts/security_policy_gate.py"]:
        errors.append("security workflow must execute the exact active gate command once")

    workflow_paths: set[str] = set()
    in_paths = False
    for indent, content in on_section:
        if indent == 4 and content == "paths:":
            in_paths = True
            continue
        if in_paths and indent <= 4:
            in_paths = False
        if in_paths and indent == 6 and content.startswith("- "):
            workflow_paths.add(scalar(content[2:]))
    for protected_path in declared_paths:
        workflow_pattern = protected_path + "**" if protected_path.endswith("/") else protected_path
        if workflow_pattern not in workflow_paths:
            errors.append(f"security workflow path filter missing: {protected_path}")


def _validate_hook_parity(root: Path, policy: dict[str, Any], errors: list[str]) -> None:
    runtime_files = policy.get("runtime_hook_files")
    if not isinstance(runtime_files, list) or not runtime_files:
        errors.append("runtime_hook_files must be a non-empty list")
        return
    runtime_dir = root / ".claude/hooks"
    package_dir = root / "core/scripts/install_hooks"
    for name in runtime_files:
        if not isinstance(name, str) or Path(name).name != name:
            errors.append(f"invalid runtime hook filename: {name!r}")
            continue
        runtime_path = runtime_dir / name
        package_path = package_dir / name
        try:
            runtime_bytes = runtime_path.read_bytes()
            package_bytes = package_path.read_bytes()
        except OSError as exc:
            errors.append(f"hook parity input missing for {name}: {type(exc).__name__}")
            continue
        if runtime_bytes != package_bytes:
            errors.append(f"installed/source hook drift: {name}")

    try:
        bridge_source = (root / "core/scripts/safety_bridge.py").read_bytes()
        bridge_package = (package_dir / "safety_bridge.py").read_bytes()
    except OSError as exc:
        errors.append(f"safety bridge parity input missing: {type(exc).__name__}")
    else:
        if bridge_source != bridge_package:
            errors.append("installed/source hook drift: safety_bridge.py")

    settings = _load_json(root / ".claude/settings.json", errors)
    if settings is None:
        return
    bindings: dict[str, list[tuple[str, str, str]]] = {}
    for block in settings.get("hooks", {}).get("PreToolUse", []):
        if not isinstance(block, dict):
            continue
        matcher = str(block.get("matcher") or "")
        for hook in block.get("hooks", []):
            if isinstance(hook, dict):
                command = str(hook.get("command") or "")
                if command:
                    filename = command.rsplit("/", 1)[-1]
                    bindings.setdefault(filename, []).append(
                        (matcher, str(hook.get("type") or ""), command)
                    )
    required_bound = {
        name
        for name in runtime_files
        if isinstance(name, str) and name.endswith(".sh") and not name.startswith("_")
    }
    for filename in sorted(required_bound):
        expected_matcher = (
            "Write|Edit|MultiEdit" if filename == "enforce-worktree.sh" else "Bash"
        )
        expected = (
            expected_matcher,
            "command",
            f"$CLAUDE_PROJECT_DIR/.claude/hooks/{filename}",
        )
        if bindings.get(filename) != [expected]:
            errors.append(
                f"runtime hook binding must be exact and active: {filename}"
            )


def _validate_public_claims(root: Path, errors: list[str]) -> None:
    path = root / PUBLIC_CLAIMS_PATH
    spec = importlib.util.spec_from_file_location("_marvis_public_claims_gate", path)
    if spec is None or spec.loader is None:
        errors.append("public claims gate is not importable")
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        module.verify_source(root)
    except Exception as exc:  # fail closed on invalid claims or an unreadable gate
        errors.append(f"public claims gate failed: {exc}")
    finally:
        sys.modules.pop(spec.name, None)


def validate_repository(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    policy = _load_json(root / POLICY_PATH, errors)
    if policy is None:
        return errors
    if policy.get("schema") != EXPECTED_SCHEMA:
        errors.append(f"unexpected policy schema: {policy.get('schema')!r}")

    owners = policy.get("owners")
    if not isinstance(owners, list) or not owners or not all(
        isinstance(owner, str) and owner.startswith("@") for owner in owners
    ):
        errors.append("policy owners must be a non-empty list of GitHub handles")
        owners = []

    review_contract = policy.get("review_contract")
    if not isinstance(review_contract, dict) or review_contract.get("required") != "independent-code-owner-review":
        errors.append("policy must require independent-code-owner-review")

    declared_paths = _declared_paths(policy, errors)
    must_exist = policy.get("must_exist")
    if not isinstance(must_exist, list):
        errors.append("must_exist must be a list")
        must_exist = []
    for item in must_exist:
        if item not in declared_paths:
            errors.append(f"must_exist path is not protected: {item}")
        if not isinstance(item, str) or not (root / item).exists():
            errors.append(f"protected required path is missing: {item}")

    if owners:
        _validate_codeowners(root, declared_paths, owners, errors)
    _validate_workflow(root, declared_paths, errors)
    _validate_hook_parity(root, policy, errors)
    _validate_public_claims(root, errors)
    return errors


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    errors = validate_repository(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("security policy gate: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
