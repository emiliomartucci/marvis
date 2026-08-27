#!/usr/bin/env python3
"""Verify hook source, package, installer, installed bytes and behavior."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from generate_hook_policy import HookGenerationError, generate


class HookPolicyError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HookPolicyError(f"invalid hook manifest: {path}") from exc
    if not isinstance(value, dict):
        raise HookPolicyError("hook manifest root must be an object")
    return value


def _literal_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise HookPolicyError(f"installer assignment missing: {name}")


def _installer_inventory(path: Path) -> set[str]:
    matchers = _literal_assignment(path, "_MATCHER_SCRIPTS")
    support = _literal_assignment(path, "_SUPPORT_FILES")
    if not isinstance(matchers, dict) or not isinstance(support, tuple):
        raise HookPolicyError("installer inventory has an unsupported shape")
    scripts = {str(item) for values in matchers.values() for item in values}
    source = path.read_text(encoding="utf-8")
    if "for name in _COPY_FILES" not in source or "shutil.copy2(src, dst)" not in source:
        raise HookPolicyError("installer no longer copies the declared inventory")
    if "mandatory hook package data missing" not in source:
        raise HookPolicyError("installer does not fail closed on missing package data")
    return scripts | {str(item) for item in support}


def _permission(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        return "process_failure"
    if not result.stdout.strip():
        return "no_output"
    try:
        payload = json.loads(result.stdout)
        return str(payload["hookSpecificOutput"]["permissionDecision"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HookPolicyError(f"hook returned invalid output: {result.stdout[:300]}") from exc


def _run(wrapper: Path, payload: dict[str, Any], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(wrapper)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=20,
        env=env,
    )


def _installed_tree(root: Path, manifest: dict[str, Any], destination: Path) -> Path:
    package_dir = (root / str(manifest["generated_resource"])).parent
    hooks = destination / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for name in manifest["mandatory_installer_files"]:
        source = package_dir / str(name)
        if not source.is_file():
            raise HookPolicyError(f"mandatory packaged hook file missing: {name}")
        shutil.copy2(source, hooks / str(name))
    return hooks


def _assert_dependency_failure_is_closed(
    root: Path,
    manifest: dict[str, Any],
    *,
    mutation: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="marvis-hook-negative-") as raw:
        hooks = _installed_tree(root, manifest, Path(raw))
        wrapper = hooks / "block-dangerous-bash.sh"
        env = os.environ.copy()
        if mutation == "missing_config":
            (hooks / "config.json").unlink()
        elif mutation == "invalid_config":
            (hooks / "config.json").write_text("not-json\n", encoding="utf-8")
        elif mutation == "missing_bridge":
            (hooks / "safety_bridge.py").unlink()
        elif mutation == "missing_helper":
            (hooks / "_config.sh").unlink()
        elif mutation == "missing_python":
            fake = Path(raw) / "bin"
            fake.mkdir()
            python = fake / "python3"
            python.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
            python.chmod(0o755)
            env["PATH"] = f"{fake}:/usr/bin:/bin"
        else:  # pragma: no cover - internal programming error
            raise AssertionError(mutation)
        result = _run(
            wrapper,
            {"cwd": raw, "tool_input": {"command": "rm -rf /"}},
            env=env,
        )
        if _permission(result) != "deny":
            raise HookPolicyError(f"{mutation} did not emit an explicit deny")


def verify(root: Path, *, installed_file: Path | None = None) -> dict[str, Any]:
    try:
        digest = generate(root, write=False)
    except HookGenerationError as exc:
        raise HookPolicyError(str(exc)) from exc
    manifest = _load(root / "contracts/hooks/policy-v1.json")
    mandatory = {str(item) for item in manifest.get("mandatory_installer_files", [])}
    installer = root / str(manifest.get("installer_module", ""))
    if _installer_inventory(installer) != mandatory:
        raise HookPolicyError("manifest and installer file inventories differ")

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    if '"core.scripts.install_hooks"' not in pyproject or '"*.py"' not in pyproject:
        raise HookPolicyError("packaging metadata does not include hook Python resources")

    canonical = root / str(manifest["canonical_source"])
    generated = root / str(manifest["generated_resource"])
    expected_bytes = canonical.read_bytes()
    if generated.read_bytes() != expected_bytes:
        raise HookPolicyError("packaged resource bytes differ from canonical source")
    if installed_file is not None and installed_file.read_bytes() != expected_bytes:
        raise HookPolicyError("wheel-installed hook bytes differ from canonical source")

    behavior_count = 0
    with tempfile.TemporaryDirectory(prefix="marvis-hook-install-") as raw:
        hooks = _installed_tree(root, manifest, Path(raw))
        installed = hooks / str(manifest["installed_name"])
        if hashlib.sha256(installed.read_bytes()).hexdigest() != digest:
            raise HookPolicyError("installer output digest differs from canonical policy")
        for case in manifest.get("behavior_cases", []):
            result = _run(hooks / str(case["wrapper"]), case["payload"])
            observed = _permission(result)
            if observed != case["expected_permission"]:
                raise HookPolicyError(
                    f"behavior case {case['name']} expected {case['expected_permission']}, got {observed}"
                )
            behavior_count += 1

    for mutation in (
        "missing_config",
        "invalid_config",
        "missing_bridge",
        "missing_helper",
        "missing_python",
    ):
        _assert_dependency_failure_is_closed(root, manifest, mutation=mutation)

    return {
        "policy_sha256": digest,
        "mandatory_files": len(mandatory),
        "behavior_cases": behavior_count,
        "dependency_failures": 5,
        "wheel_installed_checked": installed_file is not None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--installed-file", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify(args.root.resolve(), installed_file=args.installed_file)
    except (HookPolicyError, OSError) as exc:
        print(f"hook policy: FAIL: {exc}")
        return 1
    print("hook policy: PASS " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
