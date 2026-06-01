# v1.4.0 - 2026-04-15 - _merge_branch_sync: fetch + fast-forward local target before merge (fixes recurring non-fast-forward push failures)
# v1.3.0 - 2026-03-12 - Centralize ALLOWED_REPO_PARENTS → api/config.py
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TypedDict

import pygit2

pygit2.option(pygit2.GIT_OPT_SET_OWNER_VALIDATION, 0)

# git/chown operations run as a configurable service-account user (deployments
# where the API process is not the repo owner). Single-user / self-hosted: plain
# `git` with no chown. Same source of truth used by pr_service / routers.projects.
from core.api.services.runas import GIT_CMD as _GIT_CMD, chown_to_runas, runas_user

logger = logging.getLogger(__name__)

# --- Exceptions ---


class GitOpsError(Exception):
    pass


class NotAGitRepoError(GitOpsError):
    pass


class WorktreeConflictError(GitOpsError):
    pass


class MergeConflictError(GitOpsError):
    def __init__(self, conflicting_files: list[str]):
        self.conflicting_files = conflicting_files
        super().__init__(f"Merge conflict in {len(conflicting_files)} files")


# --- Constants ---

TASK_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")

from core.api.config import ALLOWED_REPO_PARENTS  # centralized in config.py

WORKTREES_BASE = Path(os.environ.get("MARVIS_WORKTREES_BASE", str(Path.home() / "dev")))
MERGE_WORKTREE_PREFIX = ".merge-"


# --- Validation ---


def _validate_repo_path(repo_path: str) -> Path:
    """Validate repo_path against allowlist and ensure .git exists."""
    repo = Path(repo_path).resolve()
    if not any(repo.is_relative_to(p) for p in ALLOWED_REPO_PARENTS):
        raise NotAGitRepoError(f"Repo path not in allowlist: {repo}")
    if not (repo / ".git").exists():
        raise NotAGitRepoError(f"No .git directory at {repo}")
    return repo


def _validate_task_id(task_id: str) -> str:
    """Validate task_id format to prevent path traversal."""
    if not TASK_ID_PATTERN.match(task_id):
        raise ValueError(f"Invalid task_id format: {task_id!r}")
    return task_id


# --- TypedDicts ---


class WorktreeInfo(TypedDict):
    worktree_path: str
    branch_name: str
    already_existed: bool


class PrDiffResult(TypedDict):
    stats: dict  # {additions, deletions, files_changed}
    unified_diff: str
    is_empty: bool


class MergeResult(TypedDict):
    merged: bool
    already_merged: bool
    commit_sha: str
    # Empty string when already_merged=True or when primary sha was unreadable.
    # Consumers use this to sync the primary workspace working tree post-merge.
    pre_merge_sha: str


# --- Sync implementations (run via asyncio.to_thread) ---


def _create_worktree_sync(
    repo_path: str, task_id: str, base_branch: str = "main"
) -> WorktreeInfo:
    task_id = _validate_task_id(task_id)
    repo_dir = _validate_repo_path(repo_path)

    branch_name = f"feat/task-{task_id}"
    worktree_path = WORKTREES_BASE / f"task-{task_id}"

    # Idempotent: if worktree already exists, return it
    if worktree_path.exists() and (worktree_path / ".git").exists():
        return WorktreeInfo(
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            already_existed=True,
        )

    repo = pygit2.Repository(str(repo_dir))

    # Resolve base branch to commit
    try:
        base_ref = repo.lookup_branch(base_branch)
        if base_ref is None:
            raise NotAGitRepoError(f"Branch '{base_branch}' not found")
        base_commit = base_ref.peel(pygit2.Commit)
    except Exception as exc:
        raise NotAGitRepoError(
            f"Cannot resolve base branch '{base_branch}': {exc}"
        ) from exc

    # Create or reuse the feature branch
    existing = repo.lookup_branch(branch_name)
    if existing is None:
        repo.branches.local.create(branch_name, base_commit)

    # Ensure worktrees base exists
    WORKTREES_BASE.mkdir(parents=True, exist_ok=True)

    # Create worktree via git CLI (more reliable than pygit2 for worktree management)
    result = subprocess.run(
        [*_GIT_CMD, "worktree", "add", str(worktree_path), branch_name],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Might already be checked out — try to recover
        if "already checked out" in result.stderr:
            raise WorktreeConflictError(
                f"Branch '{branch_name}' already checked out: {result.stderr.strip()}"
            )
        raise GitOpsError(f"git worktree add failed: {result.stderr.strip()}")

    # When the API runs as a service account distinct from the repo owner, the
    # new worktree and .git metadata are owned by that account. Chown them back
    # to the run-as user so agents (running as that user) can commit inside the
    # worktree. No-op in single-user / self-hosted deployments.
    if runas_user():
        # The worktree directory itself
        chown_to_runas(worktree_path)
        # .git/refs and .git/logs in the main repo — git worktree add may create
        # new ref/log entries owned by the service account
        git_dir = repo_dir / ".git"
        for subdir in ("refs", "logs"):
            target = git_dir / subdir
            if target.exists():
                chown_to_runas(target)
        # Also .git/worktrees/ metadata for this worktree
        wt_meta = git_dir / "worktrees" / f"task-{task_id}"
        if wt_meta.exists():
            chown_to_runas(wt_meta)

    logger.info(
        "Created worktree for task %s at %s (branch %s)",
        task_id,
        worktree_path,
        branch_name,
    )

    return WorktreeInfo(
        worktree_path=str(worktree_path),
        branch_name=branch_name,
        already_existed=False,
    )


def _get_pr_diff_sync(
    repo_path: str, branch: str, target: str = "main"
) -> PrDiffResult:
    repo_dir = _validate_repo_path(repo_path)
    repo = pygit2.Repository(str(repo_dir))

    # Resolve branch and target commits
    branch_ref = repo.lookup_branch(branch)
    target_ref = repo.lookup_branch(target)
    if branch_ref is None:
        raise GitOpsError(f"Branch '{branch}' not found")
    if target_ref is None:
        raise GitOpsError(f"Target branch '{target}' not found")

    branch_commit = branch_ref.peel(pygit2.Commit)
    target_commit = target_ref.peel(pygit2.Commit)

    # Merge-base diff: shows only commits on the branch, not target advances
    merge_base_oid = repo.merge_base(branch_commit.id, target_commit.id)
    if merge_base_oid is None:
        raise GitOpsError(f"No common ancestor between '{branch}' and '{target}'")

    merge_base = repo.get(merge_base_oid)
    diff = repo.diff(merge_base.peel(pygit2.Tree), branch_commit.peel(pygit2.Tree))
    diff.find_similar()

    unified = diff.patch or ""
    stats = diff.stats

    return PrDiffResult(
        stats={
            "additions": stats.insertions,
            "deletions": stats.deletions,
            "files_changed": stats.files_changed,
        },
        unified_diff=unified,
        is_empty=stats.files_changed == 0,
    )


def _build_temp_merge_worktree_path(target: str) -> Path:
    safe_target = re.sub(r"[^a-zA-Z0-9._-]+", "-", target).strip("-") or "target"
    return (
        WORKTREES_BASE / f"{MERGE_WORKTREE_PREFIX}{safe_target}-{uuid.uuid4().hex[:8]}"
    )


def _cleanup_temp_merge_worktree(repo_dir: Path, worktree_path: Path) -> None:
    subprocess.run(
        [*_GIT_CMD, "worktree", "remove", "--force", str(worktree_path)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)


def _merge_branch_sync(
    repo_path: str, branch: str, target: str = "main"
) -> MergeResult:
    repo_dir = _validate_repo_path(repo_path)

    # Sync local target with origin before merging. The long-running API workspace
    # can lag behind origin/main after external pushes, causing non-fast-forward
    # push failures. Fail-clean if local has diverged (human rebase required).
    # Use explicit refspec so the remote-tracking ref is updated regardless of
    # remote.origin.fetch config (plain "fetch origin main" only touches FETCH_HEAD).
    fetch = subprocess.run(
        [
            *_GIT_CMD,
            "fetch",
            "origin",
            f"+refs/heads/{target}:refs/remotes/origin/{target}",
        ],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        raise GitOpsError(f"git fetch origin {target} failed: {fetch.stderr.strip()}")

    remote_ref = f"origin/{target}"
    remote_sha_res = subprocess.run(
        [*_GIT_CMD, "rev-parse", remote_ref],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if remote_sha_res.returncode != 0:
        raise GitOpsError(
            f"Cannot resolve '{remote_ref}': {remote_sha_res.stderr.strip()}"
        )
    remote_sha = remote_sha_res.stdout.strip()

    local_sha_res = subprocess.run(
        [*_GIT_CMD, "rev-parse", target],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if local_sha_res.returncode != 0:
        raise GitOpsError(
            f"Cannot resolve local '{target}': {local_sha_res.stderr.strip()}"
        )
    local_sha = local_sha_res.stdout.strip()

    if local_sha != remote_sha:
        local_behind = subprocess.run(
            [*_GIT_CMD, "merge-base", "--is-ancestor", local_sha, remote_sha],
            cwd=str(repo_dir),
            capture_output=True,
        )
        if local_behind.returncode == 0:
            # Local is strict ancestor of origin → fast-forward local to origin.
            ff_res = subprocess.run(
                [*_GIT_CMD, "update-ref", f"refs/heads/{target}", remote_sha, local_sha],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            if ff_res.returncode != 0:
                raise GitOpsError(
                    f"Fast-forward of local '{target}' to '{remote_ref}' failed: "
                    f"{ff_res.stderr.strip()}"
                )
            logger.info(
                "Fast-forwarded local '%s' from %s to %s (origin advanced)",
                target,
                local_sha[:8],
                remote_sha[:8],
            )
        else:
            local_ahead = subprocess.run(
                [*_GIT_CMD, "merge-base", "--is-ancestor", remote_sha, local_sha],
                cwd=str(repo_dir),
                capture_output=True,
            )
            if local_ahead.returncode != 0:
                raise GitOpsError(
                    f"Local '{target}' ({local_sha[:8]}) has diverged from "
                    f"'{remote_ref}' ({remote_sha[:8]}). Human rebase required: "
                    f"cd {repo_dir} && git fetch origin && git rebase origin/{target}"
                )
            # Local is ahead of origin → merge will push forward, no action needed.

    current_target = subprocess.run(
        [*_GIT_CMD, "rev-parse", target],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if current_target.returncode != 0:
        raise GitOpsError(
            f"Cannot resolve current '{target}': {current_target.stderr.strip()}"
        )
    original_target_sha = current_target.stdout.strip()

    # First check if already merged
    result = subprocess.run(
        [*_GIT_CMD, "merge-base", "--is-ancestor", branch, target],
        cwd=str(repo_dir),
        capture_output=True,
    )
    if result.returncode == 0:
        # Branch is already ancestor of target — already merged
        sha = subprocess.run(
            [*_GIT_CMD, "rev-parse", target],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        return MergeResult(
            merged=True,
            already_merged=True,
            commit_sha=sha.stdout.strip(),
            pre_merge_sha="",
        )

    temp_worktree = _build_temp_merge_worktree_path(target)
    add = subprocess.run(
        [*_GIT_CMD, "worktree", "add", "--detach", str(temp_worktree), target],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise GitOpsError(f"git worktree add failed: {add.stderr.strip()}")

    try:
        ff_only = subprocess.run(
            [*_GIT_CMD, "merge-base", "--is-ancestor", target, branch],
            cwd=str(repo_dir),
            capture_output=True,
        )
        merge_args = ["merge", "--no-edit", "--no-verify", branch]
        if ff_only.returncode == 0:
            merge_args = ["merge", "--ff-only", "--no-verify", branch]

        # Merge in an isolated detached worktree so dirty files in the primary repo
        # cannot block the review -> merge gate.
        merge = subprocess.run(
            [*_GIT_CMD, *merge_args],
            cwd=str(temp_worktree),
            capture_output=True,
            text=True,
        )

        if merge.returncode != 0:
            if "CONFLICT" in merge.stdout or "CONFLICT" in merge.stderr:
                status = subprocess.run(
                    [*_GIT_CMD, "diff", "--name-only", "--diff-filter=U"],
                    cwd=str(temp_worktree),
                    capture_output=True,
                    text=True,
                )
                conflicting = [f for f in status.stdout.strip().split("\n") if f]
                subprocess.run(
                    [*_GIT_CMD, "merge", "--abort"],
                    cwd=str(temp_worktree),
                    capture_output=True,
                )
                raise MergeConflictError(conflicting)
            error = merge.stderr.strip() or merge.stdout.strip()
            raise GitOpsError(f"Merge failed: {error}")

        sha = subprocess.run(
            [*_GIT_CMD, "rev-parse", "HEAD"],
            cwd=str(temp_worktree),
            capture_output=True,
            text=True,
        )
        commit_sha = sha.stdout.strip()

        update_ref = subprocess.run(
            [*_GIT_CMD, "update-ref", f"refs/heads/{target}", commit_sha],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if update_ref.returncode != 0:
            raise GitOpsError(
                f"Failed to update '{target}': {update_ref.stderr.strip()}"
            )

        # Chown .git/refs and .git/logs after updating refs — new refs/logs may
        # be created when the API runs as a service account distinct from the
        # repo owner. No-op in single-user / self-hosted deployments.
        if runas_user():
            git_dir = repo_dir / ".git"
            for subdir in ("refs", "logs"):
                target_dir = git_dir / subdir
                if target_dir.exists():
                    chown_to_runas(target_dir)

        push = subprocess.run(
            [*_GIT_CMD, "push", "origin", f"{commit_sha}:refs/heads/{target}"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            rollback = subprocess.run(
                [*_GIT_CMD, "update-ref", f"refs/heads/{target}", original_target_sha],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            if rollback.returncode != 0:
                logger.error(
                    "Failed to roll back local %s after push failure: %s",
                    target,
                    rollback.stderr.strip(),
                )
            logger.warning(
                "Push to origin/%s failed after merge: %s",
                target,
                push.stderr.strip(),
            )
            raise GitOpsError(
                f"Push to origin/{target} failed after merge: {push.stderr.strip() or push.stdout.strip()}"
            )
        else:
            logger.info("Pushed '%s' to origin after merge", target)

        logger.info(
            "Merged branch '%s' into '%s' at %s using isolated worktree %s",
            branch,
            target,
            commit_sha,
            temp_worktree,
        )
        return MergeResult(
            merged=True,
            already_merged=False,
            commit_sha=commit_sha,
            pre_merge_sha=original_target_sha,
        )
    finally:
        _cleanup_temp_merge_worktree(repo_dir, temp_worktree)


def _remove_worktree_sync(repo_path: str, worktree_path: str, branch: str) -> None:
    repo_dir = _validate_repo_path(repo_path)
    wt_path = Path(worktree_path).resolve()

    # Security: verify worktree is inside expected directory
    if not wt_path.is_relative_to(WORKTREES_BASE.resolve()):
        raise GitOpsError(f"Worktree path not in allowed base: {wt_path}")

    # Remove worktree via git CLI
    subprocess.run(
        [*_GIT_CMD, "worktree", "remove", "--force", str(wt_path)],
        cwd=str(repo_dir),
        capture_output=True,
    )

    # Remove directory if it still exists
    if wt_path.exists():
        shutil.rmtree(str(wt_path))

    # Prune stale worktree references
    subprocess.run(
        [*_GIT_CMD, "worktree", "prune"],
        cwd=str(repo_dir),
        capture_output=True,
    )

    # Delete local branch (force, may not be fully merged if PR was closed)
    subprocess.run(
        [*_GIT_CMD, "branch", "-D", branch],
        cwd=str(repo_dir),
        capture_output=True,
    )

    # Delete remote branch (best effort, non-blocking)
    subprocess.run(
        [*_GIT_CMD, "push", "origin", "--delete", branch],
        cwd=str(repo_dir),
        capture_output=True,
    )

    logger.info("Removed worktree %s and branch %s (local + remote)", wt_path, branch)


def _revert_commit_sync(repo_path: str, commit_sha: str, branch_name: str) -> dict:
    """Create a git revert commit on a new branch. Returns new commit SHA."""
    repo_dir = _validate_repo_path(repo_path)

    # Create new branch from main
    branch_result = subprocess.run(
        [*_GIT_CMD, "checkout", "-b", branch_name, "main"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    if branch_result.returncode != 0:
        raise GitOpsError(
            f"Failed to create revert branch: {branch_result.stderr.strip()}"
        )

    try:
        # Revert the commit (no-edit: auto commit message)
        revert_result = subprocess.run(
            [*_GIT_CMD, "revert", "--no-edit", commit_sha],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if revert_result.returncode != 0:
            raise GitOpsError(f"git revert failed: {revert_result.stderr.strip()}")

        # Get new commit SHA
        sha_result = subprocess.run(
            [*_GIT_CMD, "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        new_sha = sha_result.stdout.strip()
    finally:
        # Always return to main
        subprocess.run(
            [*_GIT_CMD, "checkout", "main"], cwd=str(repo_dir), capture_output=True
        )

    logger.info(
        "Created revert commit %s on branch %s (reverts %s)",
        new_sha,
        branch_name,
        commit_sha,
    )
    return {"commit_sha": new_sha, "branch": branch_name}


# --- Async wrappers ---


async def create_worktree_async(
    repo_path: str, task_id: str, base_branch: str = "main"
) -> WorktreeInfo:
    return await asyncio.to_thread(
        _create_worktree_sync, repo_path, task_id, base_branch
    )


async def get_pr_diff_async(
    repo_path: str, branch: str, target: str = "main"
) -> PrDiffResult:
    return await asyncio.to_thread(_get_pr_diff_sync, repo_path, branch, target)


async def merge_branch_async(
    repo_path: str, branch: str, target: str = "main"
) -> MergeResult:
    return await asyncio.to_thread(_merge_branch_sync, repo_path, branch, target)


async def remove_worktree_async(
    repo_path: str, worktree_path: str, branch: str
) -> None:
    return await asyncio.to_thread(
        _remove_worktree_sync, repo_path, worktree_path, branch
    )


async def revert_commit_async(
    repo_path: str, commit_sha: str, branch_name: str
) -> dict:
    return await asyncio.to_thread(
        _revert_commit_sync, repo_path, commit_sha, branch_name
    )
