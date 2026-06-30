from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.api.services import git_ops


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    remote_dir = tmp_path / "remote.git"
    repo_dir = tmp_path / "repo"
    worktrees_dir = tmp_path / "worktrees"

    _git(tmp_path, "init", "--bare", str(remote_dir))
    _git(tmp_path, "clone", str(remote_dir), str(repo_dir))
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test User")

    (repo_dir / "AGENTS.md").write_text("base\n")
    _git(repo_dir, "add", "AGENTS.md")
    _git(repo_dir, "commit", "-m", "init")
    _git(repo_dir, "branch", "-M", "main")
    _git(repo_dir, "push", "-u", "origin", "main")

    monkeypatch.setattr(git_ops, "_GIT_CMD", ["git"])
    monkeypatch.setattr(git_ops, "WORKTREES_BASE", worktrees_dir)
    monkeypatch.setattr(git_ops, "ALLOWED_REPO_PARENTS", [tmp_path.resolve()])

    worktrees_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


def test_merge_branch_sync_succeeds_with_dirty_primary_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = _init_repo(tmp_path, monkeypatch)

    _git(repo_dir, "checkout", "-b", "feat/task-test")
    (repo_dir / "AGENTS.md").write_text("feature\n")
    _git(repo_dir, "commit", "-am", "feature change")
    _git(repo_dir, "checkout", "main")

    # Simulate the exact blocker seen in production: a dirty tracked file in the
    # primary repo should no longer abort the merge.
    (repo_dir / "AGENTS.md").write_text("dirty-local\n")

    result = git_ops._merge_branch_sync(str(repo_dir), "feat/task-test", "main")

    assert result["merged"] is True
    assert result["already_merged"] is False
    assert _git(repo_dir, "rev-parse", "main").stdout.strip() == result["commit_sha"]
    assert _git(repo_dir, "show", "main:AGENTS.md").stdout == "feature\n"
    assert any("AGENTS.md" in line for line in _git(repo_dir, "status", "--short").stdout.splitlines())
    assert list((tmp_path / "worktrees").glob(".merge-*")) == []

    remote_sha = _git(repo_dir, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]
    assert remote_sha == result["commit_sha"]


def test_merge_branch_sync_fast_forwards_stale_local_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates workspace lagging behind origin/main after external push."""
    repo_dir = _init_repo(tmp_path, monkeypatch)

    # External writer clones the remote, advances origin/main, leaving our
    # repo_dir stale — exactly the recurring production scenario.
    external = tmp_path / "external"
    _git(tmp_path, "clone", str(tmp_path / "remote.git"), str(external))
    _git(external, "checkout", "-B", "main", "origin/main")
    _git(external, "config", "user.email", "ext@example.com")
    _git(external, "config", "user.name", "External")
    (external / "EXT.md").write_text("external advance\n")
    _git(external, "add", "EXT.md")
    _git(external, "commit", "-m", "advance origin/main")
    _git(external, "push", "origin", "main")

    # Our feature branch is based on the old main. Merge must still succeed.
    _git(repo_dir, "checkout", "-b", "feat/task-ff")
    (repo_dir / "FEAT.md").write_text("feature\n")
    _git(repo_dir, "add", "FEAT.md")
    _git(repo_dir, "commit", "-m", "feature change")
    _git(repo_dir, "checkout", "main")

    result = git_ops._merge_branch_sync(str(repo_dir), "feat/task-ff", "main")

    assert result["merged"] is True
    remote_sha = _git(repo_dir, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]
    assert remote_sha == result["commit_sha"]
    # Final tree carries both the external advance and our feature change.
    assert (repo_dir / "EXT.md").exists() or _git(repo_dir, "show", "main:EXT.md").returncode == 0
    assert _git(repo_dir, "show", "main:FEAT.md").stdout == "feature\n"


def test_merge_branch_sync_fails_clean_when_local_diverged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local main has unpushed commits AND origin advanced independently."""
    repo_dir = _init_repo(tmp_path, monkeypatch)

    # Local diverges with unpushed commit.
    (repo_dir / "LOCAL.md").write_text("local only\n")
    _git(repo_dir, "add", "LOCAL.md")
    _git(repo_dir, "commit", "-m", "local unpushed")

    # Origin advances independently via external writer.
    external = tmp_path / "external"
    _git(tmp_path, "clone", str(tmp_path / "remote.git"), str(external))
    _git(external, "checkout", "-B", "main", "origin/main")
    _git(external, "config", "user.email", "ext@example.com")
    _git(external, "config", "user.name", "External")
    (external / "EXT.md").write_text("external\n")
    _git(external, "add", "EXT.md")
    _git(external, "commit", "-m", "origin advance")
    _git(external, "push", "origin", "main")

    _git(repo_dir, "checkout", "-b", "feat/task-div")
    (repo_dir / "FEAT.md").write_text("feature\n")
    _git(repo_dir, "add", "FEAT.md")
    _git(repo_dir, "commit", "-m", "feature")
    _git(repo_dir, "checkout", "main")

    with pytest.raises(git_ops.GitOpsError, match="diverged"):
        git_ops._merge_branch_sync(str(repo_dir), "feat/task-div", "main")


def test_merge_branch_sync_conflict_cleans_temp_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = _init_repo(tmp_path, monkeypatch)

    _git(repo_dir, "checkout", "-b", "feat/task-conflict")
    (repo_dir / "AGENTS.md").write_text("feature\n")
    _git(repo_dir, "commit", "-am", "feature change")

    _git(repo_dir, "checkout", "main")
    (repo_dir / "AGENTS.md").write_text("main\n")
    _git(repo_dir, "commit", "-am", "main change")

    with pytest.raises(git_ops.MergeConflictError):
        git_ops._merge_branch_sync(str(repo_dir), "feat/task-conflict", "main")

    assert list((tmp_path / "worktrees").glob(".merge-*")) == []


def test_merge_branch_sync_always_creates_merge_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a clean fast-forwardable branch yields a MERGE commit (--no-ff), so
    release-notes.sh (which walks `git log --merges`) can find + attribute it."""
    repo_dir = _init_repo(tmp_path, monkeypatch)

    _git(repo_dir, "checkout", "-b", "feat/task-ff-mc")
    (repo_dir / "FEAT.md").write_text("feature\n")
    _git(repo_dir, "add", "FEAT.md")
    _git(repo_dir, "commit", "-m", "feat(scope): add feature")
    _git(repo_dir, "checkout", "main")

    result = git_ops._merge_branch_sync(str(repo_dir), "feat/task-ff-mc", "main")
    assert result["merged"] is True
    assert _git(repo_dir, "show", "main:FEAT.md").stdout == "feature\n"

    # A merge commit has two parents — main^2 must resolve (a fast-forward would not).
    second_parent = _git(repo_dir, "rev-parse", "main^2")
    assert second_parent.returncode == 0, "expected a merge commit (2 parents), got a fast-forward"
    # And it shows in the --merges log release-notes.sh walks.
    merges = _git(repo_dir, "log", "main", "--merges", "--oneline")
    assert merges.stdout.strip(), "merge commit must appear in `git log --merges`"
