from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def _load_bridge_module():
    bridge_path = Path(__file__).resolve().parents[2] / "scripts" / "safety_bridge.py"
    spec = importlib.util.spec_from_file_location("safety_bridge", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config() -> dict:
    return {
        "branches": {
            "protected": ["main", "master"],
            "task_pattern": r"^feat/task-[0-9a-f-]+$",
        },
        "worktree": {
            "whitelist_extensions": [".md", ".yaml", ".yml", ".json", ".jsonc"],
            "whitelist_dirs": [
                "docs/",
                "memory/",
                "kb/",
                ".opencode/",
                ".claude/hooks/",
            ],
            "whitelist_files": ["project.yaml", ".task"],
            "whitelist_scripts": ["scripts/*.sh"],
            "blocked_files": [".claude/settings.json"],
        },
        "db_protection": {
            "db_patterns": [r"console\.db", r"/data/pir/.*\.db"],
            "write_keywords": [
                "UPDATE",
                "INSERT",
                "DELETE",
                "DROP",
                "ALTER",
                "CREATE",
                "ATTACH",
                "VACUUM",
                "executescript",
            ],
        },
    }


def test_worktree_blocks_code_edit_on_main(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(bridge, "current_branch", lambda repo_root, cwd=None: "main")

    decision = bridge.check_worktree("/repo/api/main.py", None, _config())

    assert decision.allowed is False
    assert decision.rule == "worktree"


def test_worktree_allows_whitelisted_docs_on_main(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(bridge, "current_branch", lambda repo_root, cwd=None: "main")

    decision = bridge.check_worktree("/repo/docs/plan.md", None, _config())

    assert decision.allowed is True


def test_bash_merge_blocks_multiline_git_merge_on_main(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(bridge, "current_branch", lambda repo_root, cwd=None: "main")

    command = 'printf "git merge in quotes should not count"\n git merge feature-branch'
    decision = bridge.check_bash_merge(command, "/repo", _config())

    assert decision.allowed is False
    assert decision.rule == "bash-merge"


def test_db_direct_write_blocks_sql_against_pir_db(monkeypatch):
    bridge = _load_bridge_module()
    # Default fail-closed: MARVIS_OSS_LOCAL unset → block, byte-for-byte unchanged.
    monkeypatch.delenv("MARVIS_OSS_LOCAL", raising=False)

    decision = bridge.check_db_direct_write(
        "sqlite3 console.db 'UPDATE tasks SET status=\"done\"'", _config()
    )

    assert decision.allowed is False
    assert decision.advisory is False
    assert decision.rule == "db-write"


def test_db_direct_write_still_blocks_when_oss_local(monkeypatch):
    bridge = _load_bridge_module()
    # Governance safety rules never relax under the OSS lite profile.
    monkeypatch.setenv("MARVIS_OSS_LOCAL", "1")

    decision = bridge.check_db_direct_write(
        "sqlite3 console.db 'UPDATE tasks SET status=\"done\"'", _config()
    )

    assert decision.allowed is False
    assert decision.advisory is False
    assert decision.rule == "db-write"


def test_db_direct_write_oss_local_falsey_still_blocks(monkeypatch):
    bridge = _load_bridge_module()
    # A non-truthy value (e.g. "0", "false", empty) must NOT weaken the default.
    for value in ("0", "false", "no", "", "  "):
        monkeypatch.setenv("MARVIS_OSS_LOCAL", value)
        decision = bridge.check_db_direct_write(
            "sqlite3 console.db 'UPDATE tasks SET status=\"done\"'", _config()
        )
        assert decision.allowed is False, value
        assert decision.advisory is False, value
        assert decision.rule == "db-write"


def test_push_without_task_branch_is_blocked(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge, "current_branch", lambda repo_root, cwd=None: "feat/my-branch"
    )

    decision = bridge.check_push_no_task("git push origin HEAD", "/repo", _config())

    assert decision.allowed is False
    assert decision.rule == "push-no-task"


def test_push_with_task_refspec_is_allowed_regardless_of_cwd(monkeypatch):
    """Subagent push: refspec target is feat/task-*, current_branch of main agent is unrelated."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge,
        "current_branch",
        lambda repo_root, cwd=None: "worktree-agent-abc123",
    )

    decision = bridge.check_push_no_task(
        "git push origin feat/task-d3323258-26d8-4800-9c78-ce0c9a7f17da",
        "/repo",
        _config(),
    )

    assert decision.allowed is True


def test_push_refspec_src_colon_dst_main_allowed(monkeypatch):
    """Merge engine push: <sha>:refs/heads/main. Target is protected."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge, "current_branch", lambda repo_root, cwd=None: "main"
    )

    decision = bridge.check_push_no_task(
        "git push origin abc1234:refs/heads/main", "/repo", _config()
    )

    assert decision.allowed is True


def test_push_refspec_unauthorized_branch_denied(monkeypatch):
    """Non-task, non-protected target blocked even if current_branch matches pattern."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge,
        "current_branch",
        lambda repo_root, cwd=None: "feat/task-abc",
    )

    decision = bridge.check_push_no_task(
        "git push origin rogue-branch", "/repo", _config()
    )

    assert decision.allowed is False
    assert decision.rule == "push-no-task"
    assert "rogue-branch" in (decision.reason or "")


def test_push_head_falls_back_to_current_branch(monkeypatch):
    """git push origin HEAD: resolve target as current_branch, not literal 'HEAD'."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge,
        "current_branch",
        lambda repo_root, cwd=None: "feat/task-abc",
    )

    decision = bridge.check_push_no_task(
        "git push origin HEAD", "/repo", _config()
    )

    assert decision.allowed is True


def test_push_all_falls_back_to_current_branch_legacy(monkeypatch):
    """git push --all: bulk flag, no refspec. Fall back to current_branch."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge,
        "current_branch",
        lambda repo_root, cwd=None: "feat/random",
    )

    decision = bridge.check_push_no_task(
        "git push --all origin", "/repo", _config()
    )

    assert decision.allowed is False
    assert decision.rule == "push-no-task"


def test_push_delete_refspec_allowed_for_task_branch(monkeypatch):
    """git push origin :feat/task-xyz (delete): dst still validates."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge, "current_branch", lambda repo_root, cwd=None: "main"
    )

    decision = bridge.check_push_no_task(
        "git push origin :feat/task-abc123-def", "/repo", _config()
    )

    assert decision.allowed is True


def test_push_no_refspec_fallback_current_branch_task(monkeypatch):
    """git push origin (no refspec): fall back to current_branch — legacy path."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge,
        "current_branch",
        lambda repo_root, cwd=None: "feat/task-abc123",
    )

    decision = bridge.check_push_no_task("git push origin", "/repo", _config())

    assert decision.allowed is True


def test_unknown_rule_key_is_blocked_before_config_load(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge,
        "load_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config unavailable")),
    )

    decision = bridge.evaluate_rule("removed-rule-key")

    assert decision.allowed is False
    assert decision.advisory is False
    assert decision.rule == "removed-rule-key"


def test_push_ignores_shell_redirections_and_pipes(monkeypatch):
    """Shell operators (2>&1, |, >) must not be parsed as refspec targets.

    Discovered during session 131 dogfood: `git push origin feat/task-xyz 2>&1 | tail`
    was DENIED because '2>&1' was classified as a rogue branch. shlex.split
    preserves these tokens; the parser must stop at the first shell operator.
    """
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge, "current_branch", lambda repo_root, cwd=None: "main"
    )

    for cmd in [
        "git push origin feat/task-abc123-def 2>&1 | tail -3",
        "git push origin main > /tmp/out.log",
        "git push origin feat/task-abc123-def && echo done",
    ]:
        decision = bridge.check_push_no_task(cmd, "/repo", _config())
        assert decision.allowed is True, f"Failed for: {cmd}"


def test_staging_to_prod_copy_is_blocked():
    bridge = _load_bridge_module()

    decision = bridge.check_staging_to_prod(
        "cp /tmp/staging/api/services/pr_service.py /data/pir/api/services/pr_service.py",
        _config(),
    )

    assert decision.allowed is False
    assert decision.rule == "staging-to-prod"


def test_bash_action_returns_first_matching_rule(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(bridge, "current_branch", lambda repo_root, cwd=None: "main")

    decision = bridge.evaluate_action(
        "bash_command",
        command="git merge feature && git push",
        cwd="/repo",
        config=_config(),
    )

    assert decision.allowed is False
    assert decision.rule == "bash-merge"


def test_secret_scan_runs_outside_git_repo(monkeypatch):
    """S5a: secret scan must NOT fail open when cwd is not a git repo.

    Previously resolve_repo_root() returning None silently allowed, so a
    `git commit` of an API_KEY=sk-live-... outside a repo slipped through.
    """
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: None
    )

    synthetic_secret = "sk-live-" + "abcd1234efgh5678ijkl"
    decision = bridge.check_secret_scan(
        f"git commit -m 'add config' && export API_KEY={synthetic_secret}",
        "/tmp/not-a-repo",
        _config(),
    )

    assert decision.allowed is False
    assert decision.rule == "secret-scan"


def test_secret_scan_outside_repo_allows_clean_command(monkeypatch):
    """Outside a git repo, a clean commit command (no secret) is still allowed."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: None
    )

    decision = bridge.check_secret_scan(
        "git commit -m 'docs: tidy readme'",
        "/tmp/not-a-repo",
        _config(),
    )

    assert decision.allowed is True


def test_secret_scan_blocks_secret_in_staged_diff(monkeypatch):
    """In-repo path (unchanged behaviour): secret in staged diff is blocked."""
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(
        bridge,
        "_git_output",
        lambda args, cwd: "+API_KEY=" + "sk-live-abcd1234efgh5678ijkl",
    )

    decision = bridge.check_secret_scan(
        "git commit -m 'add config'", "/repo", _config()
    )

    assert decision.allowed is False
    assert decision.rule == "secret-scan"


def test_secret_scan_cannot_be_bypassed_with_quoted_git_name(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    staged_secret = "+API_KEY=" + "sk-live-abcd1234efgh5678ijkl"
    monkeypatch.setattr(
        bridge,
        "_git_output",
        lambda args, cwd: staged_secret,
    )

    decision = bridge.check_secret_scan(
        "g''it commit -m 'add config'", "/repo", _config()
    )

    assert decision.allowed is False
    assert decision.rule == "secret-scan"


@pytest.mark.parametrize(
    "added_line",
    [
        "+last_input_tokens,Y=u.last_output_tokens",
        '+children:"Token markup"}),className="font-mono tabular-nums"',
    ],
)
def test_secret_scan_ignores_non_assignment_code(monkeypatch, added_line):
    bridge = _load_bridge_module()
    monkeypatch.setattr(
        bridge, "resolve_repo_root", lambda file_path, cwd=None: "/repo"
    )
    monkeypatch.setattr(bridge, "_git_output", lambda args, cwd: added_line)

    decision = bridge.check_secret_scan(
        "git commit -m 'refresh generated console'", "/repo", _config()
    )

    assert decision.allowed is True
