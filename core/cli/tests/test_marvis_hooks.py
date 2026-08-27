from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from core.cli import marvis_hooks


ROOT = Path(__file__).resolve().parents[3]


def test_installer_copies_exact_canonical_policy(tmp_path, monkeypatch) -> None:
    package_dir = ROOT / "core/scripts/install_hooks"
    monkeypatch.setattr(marvis_hooks, "_INSTALL_HOOKS_DIR", package_dir)
    hooks = tmp_path / ".claude/hooks"

    actions = marvis_hooks._copy_scripts(hooks, dry_run=False)

    assert {item["action"] for item in actions} == {"create"}
    canonical = (ROOT / "core/scripts/safety_bridge.py").read_bytes()
    installed = (hooks / "safety_bridge.py").read_bytes()
    assert installed == canonical
    assert hashlib.sha256(installed).hexdigest() == hashlib.sha256(canonical).hexdigest()

    result = subprocess.run(
        ["/bin/bash", str(hooks / "block-dangerous-bash.sh")],
        input=json.dumps(
            {"cwd": str(tmp_path), "tool_input": {"command": "rm -rf /"}}
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_installer_fails_before_copy_when_package_data_is_missing(
    tmp_path, monkeypatch
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    source = ROOT / "core/scripts/install_hooks"
    for name in marvis_hooks._COPY_FILES:
        if name != "safety_bridge.py":
            (package_dir / name).write_bytes((source / name).read_bytes())
    monkeypatch.setattr(marvis_hooks, "_INSTALL_HOOKS_DIR", package_dir)
    hooks = tmp_path / "installed"

    try:
        marvis_hooks._copy_scripts(hooks, dry_run=False)
    except RuntimeError as exc:
        assert "mandatory hook package data missing" in str(exc)
    else:  # pragma: no cover - protects the fail-closed boundary
        raise AssertionError("missing package data was accepted")
    assert not hooks.exists()
