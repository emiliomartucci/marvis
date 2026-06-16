from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from core.api.services import git_ops, workspace_sync


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated repo used directly (no remote needed for sync tests)."""
    repo_dir = tmp_path / "repo"
    _git(tmp_path, "init", str(repo_dir))
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test User")
    (repo_dir / "README.md").write_text("base\n")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-m", "init")
    _git(repo_dir, "branch", "-M", "main")
    monkeypatch.setattr(git_ops, "_GIT_CMD", ["git"])
    monkeypatch.setattr(workspace_sync, "_GIT_CMD", ["git"])
    return repo_dir


def _make_commit_on_branch(repo_dir: Path, file_path: str, content: str, msg: str) -> str:
    """Create a commit on a feature branch without changing main."""
    _git(repo_dir, "checkout", "-b", "feat")
    (repo_dir / file_path).parent.mkdir(parents=True, exist_ok=True)
    (repo_dir / file_path).write_text(content)
    _git(repo_dir, "add", file_path)
    _git(repo_dir, "commit", "-m", msg)
    sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    _git(repo_dir, "checkout", "main")
    return sha


def _run_sync(repo_dir: Path, merge_sha: str, pre_merge_sha: str) -> None:
    asyncio.run(
        workspace_sync.sync_primary_workspace_after_merge(
            repo_dir, merge_sha, pre_merge_sha
        )
    )


def test_sync_restores_file_modified_by_merge_when_tree_is_clean(repo: Path) -> None:
    """Primary scenario: hook/script file gets updated, tree was at pre-merge content."""
    pre = _git(repo, "rev-parse", "main").stdout.strip()
    merge_sha = _make_commit_on_branch(repo, "scripts/hook.py", "new content\n", "add hook")

    # Simulate what _merge_branch_sync does: update-ref without touching tree.
    _git(repo, "update-ref", "refs/heads/main", merge_sha)
    # Tree does NOT have scripts/hook.py yet (pre-merge state)
    assert not (repo / "scripts" / "hook.py").exists()

    _run_sync(repo, merge_sha, pre)

    # After sync: file present with merged content
    assert (repo / "scripts" / "hook.py").read_text() == "new content\n"


def test_sync_skips_file_with_staged_user_edits(repo: Path) -> None:
    """User has `git add`'d changes on a file the merge also modifies — never overwrite."""
    pre = _git(repo, "rev-parse", "main").stdout.strip()
    # File exists on main first
    (repo / "config.yml").write_text("v1\n")
    _git(repo, "add", "config.yml")
    _git(repo, "commit", "-m", "add config")
    pre = _git(repo, "rev-parse", "main").stdout.strip()

    # Merge changes config.yml to v2
    merge_sha = _make_commit_on_branch(repo, "config.yml", "v2\n", "bump config")
    _git(repo, "update-ref", "refs/heads/main", merge_sha)

    # User has staged unrelated content in config.yml
    (repo / "config.yml").write_text("user-staged\n")
    _git(repo, "add", "config.yml")

    _run_sync(repo, merge_sha, pre)

    # User's staged content preserved
    assert (repo / "config.yml").read_text() == "user-staged\n"
    staged = _git(repo, "diff", "--cached", "--name-only").stdout
    assert "config.yml" in staged


def test_sync_skips_file_with_unstaged_user_edits(repo: Path) -> None:
    """User has working-tree edits (not staged) on a file the merge touches."""
    (repo / "doc.md").write_text("v1\n")
    _git(repo, "add", "doc.md")
    _git(repo, "commit", "-m", "add doc")
    pre = _git(repo, "rev-parse", "main").stdout.strip()

    merge_sha = _make_commit_on_branch(repo, "doc.md", "v2-merged\n", "bump doc")
    _git(repo, "update-ref", "refs/heads/main", merge_sha)

    # User unstaged edit on doc.md
    (repo / "doc.md").write_text("user-edit\n")

    _run_sync(repo, merge_sha, pre)

    # User's unstaged content preserved
    assert (repo / "doc.md").read_text() == "user-edit\n"


def test_sync_restores_when_user_edits_unrelated_file(repo: Path) -> None:
    """Merge touches A, user dirty on B — A gets synced, B untouched."""
    (repo / "a.py").write_text("a v1\n")
    (repo / "b.py").write_text("b v1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init a b")
    pre = _git(repo, "rev-parse", "main").stdout.strip()

    merge_sha = _make_commit_on_branch(repo, "a.py", "a v2\n", "bump a")
    _git(repo, "update-ref", "refs/heads/main", merge_sha)

    # User edits b.py (unrelated to merge)
    (repo / "b.py").write_text("user-b-edit\n")

    _run_sync(repo, merge_sha, pre)

    assert (repo / "a.py").read_text() == "a v2\n"
    assert (repo / "b.py").read_text() == "user-b-edit\n"


def test_sync_handles_file_deletion_in_merge(repo: Path) -> None:
    """Merge deletes a file — working tree should reflect deletion."""
    (repo / "obsolete.md").write_text("old\n")
    _git(repo, "add", "obsolete.md")
    _git(repo, "commit", "-m", "add obsolete")
    pre = _git(repo, "rev-parse", "main").stdout.strip()

    _git(repo, "checkout", "-b", "feat")
    _git(repo, "rm", "obsolete.md")
    _git(repo, "commit", "-m", "remove obsolete")
    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "main")
    _git(repo, "update-ref", "refs/heads/main", merge_sha)

    # Tree still has obsolete.md at this point
    assert (repo / "obsolete.md").exists()

    _run_sync(repo, merge_sha, pre)

    assert not (repo / "obsolete.md").exists()


def test_sync_noop_when_pre_merge_sha_empty(repo: Path) -> None:
    """already_merged path passes pre_merge_sha="" — should return immediately."""
    _git(repo, "checkout", "-b", "feat")
    (repo / "x.py").write_text("x\n")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-m", "add x")
    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "main")

    # Should not raise, should not create x.py (would require pre_merge_sha)
    _run_sync(repo, merge_sha, "")
    assert not (repo / "x.py").exists()


def test_sync_noop_when_shas_equal(repo: Path) -> None:
    """Fast-path: if pre_merge_sha == merge_sha, nothing to do."""
    pre = _git(repo, "rev-parse", "main").stdout.strip()
    _run_sync(repo, pre, pre)


def test_sync_failsoft_on_invalid_sha(repo: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Non-existent SHA: logs warning, does not raise."""
    pre = _git(repo, "rev-parse", "main").stdout.strip()
    _run_sync(repo, "0000000000000000000000000000000000000000", pre)
    assert any(
        "workspace_sync_failed" in rec.message for rec in caplog.records
    )
