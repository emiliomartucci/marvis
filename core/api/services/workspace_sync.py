# v1.0.0 - 2026-04-15 - Post-merge working tree sync for primary workspace (session 131)
"""Post-merge primary workspace sync.

After `_merge_branch_sync` updates `refs/heads/<target>` on the primary repo, the
working tree is left at the pre-merge state. Hooks and scripts running out of
`~/workspace/` therefore read stale file content until someone
manually restores. This module replays the merged content into the working
tree for files touched by the merge, while preserving any concurrent user-dirty
work (staged or modified in the tree).

Design constraints (post CE deepen review):
- Use git plumbing, not byte-compare, so CRLF/mode/smudge filters don't confuse us.
- Never touch a file present in `git diff-index --cached` or `git ls-files -m`.
- Serialize with an asyncio.Lock to avoid two concurrent merges interleaving.
- chown .git/index and restored files back to the repo-owning user when the
  API runs as a distinct service account.
- Fail-soft: any subprocess failure logs a structured warning and returns;
  the merge has already succeeded server-side.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from core.api.services.git_ops import _GIT_CMD
from core.api.services.runas import chown_to_runas, runas_user

logger = logging.getLogger(__name__)

_SYNC_LOCK = asyncio.Lock()
_LOCK_TIMEOUT_MS = "5000"  # core.filesLockTimeout, avoid hanging on .git/index.lock


async def sync_primary_workspace_after_merge(
    repo_dir: Path | str,
    merge_commit_sha: str,
    pre_merge_sha: str,
    target_branch: str | None = None,
) -> None:
    """Sync primary workspace working tree with the merged commit.

    No-op when `pre_merge_sha` is empty (already-merged fast path) or equals
    `merge_commit_sha` (nothing changed). Acquires a module-level lock so two
    concurrent merges don't race on the same working tree.
    """
    if not pre_merge_sha or pre_merge_sha == merge_commit_sha:
        return
    async with _SYNC_LOCK:
        await asyncio.to_thread(
            _sync_primary_workspace_sync,
            str(repo_dir),
            merge_commit_sha,
            pre_merge_sha,
            target_branch,
        )


def _run_git(
    args: list[str], cwd: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_GIT_CMD, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _current_branch(repo_dir: str) -> str | None:
    branch = _run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo_dir)
    if branch.returncode != 0:
        return None
    return branch.stdout.strip() or None


def _sync_primary_workspace_sync(
    repo_dir: str, merge_sha: str, pre_merge_sha: str, target_branch: str | None = None
) -> None:
    try:
        diff = _run_git(
            ["diff", "--name-only", f"{pre_merge_sha}..{merge_sha}"], repo_dir
        )
        if diff.returncode != 0:
            logger.warning(
                "event=workspace_sync_failed reason=diff_failed stderr=%s",
                diff.stderr.strip(),
            )
            return
        merged_files = [f for f in diff.stdout.strip().split("\n") if f]
        if not merged_files:
            return

        # Target checkout: update-ref moves HEAD to the merge commit while the
        # index/tree are still at pre_merge_sha. Compare staged changes with
        # pre_merge_sha so stale-but-clean target checkouts do not look dirty.
        # PR checkout: compare staged changes with current HEAD instead; a clean
        # PR branch checkout is not user work just because it differs from main.
        current_branch = _current_branch(repo_dir)
        if target_branch and current_branch != target_branch:
            cached_args = ["diff", "--name-only", "--cached", "--"] + merged_files
        else:
            cached_args = (
                ["diff-index", "--name-only", "--cached", pre_merge_sha, "--"]
                + merged_files
            )

        cached = _run_git(cached_args, repo_dir)
        cached_dirty = {
            f for f in cached.stdout.strip().split("\n") if f
        } if cached.returncode == 0 else set()

        # Files modified in working tree vs index — never touch.
        modified = _run_git(
            ["ls-files", "--modified", "--"] + merged_files, repo_dir
        )
        wt_dirty = {
            f for f in modified.stdout.strip().split("\n") if f
        } if modified.returncode == 0 else set()

        untouchable = cached_dirty | wt_dirty
        restore_targets = [f for f in merged_files if f not in untouchable]
        skipped = [f for f in merged_files if f in untouchable]

        if restore_targets:
            restore = _run_git(
                [
                    "-c",
                    f"core.filesLockTimeout={_LOCK_TIMEOUT_MS}",
                    "restore",
                    "--source",
                    merge_sha,
                    "--staged",
                    "--worktree",
                    "--",
                    *restore_targets,
                ],
                repo_dir,
            )
            if restore.returncode != 0:
                logger.warning(
                    "event=workspace_sync_failed reason=restore_failed "
                    "files=%d stderr=%s",
                    len(restore_targets),
                    restore.stderr.strip(),
                )
                return
            _chown_back_to_runas(Path(repo_dir), restore_targets)
            logger.info(
                "event=workspace_sync_ok files_restored=%d files_skipped=%d",
                len(restore_targets),
                len(skipped),
            )

        if skipped:
            logger.warning(
                "event=workspace_sync_partial files_skipped=%d examples=%s",
                len(skipped),
                ",".join(skipped[:5]),
            )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning(
            "event=workspace_sync_failed reason=exception exc=%s", exc
        )


def _chown_back_to_runas(repo_dir: Path, files: list[str]) -> None:
    """When the API runs as a service account distinct from the repo owner,
    restore tree writes may end up owned by that account. Chown `.git/index`
    and the restored files back so subsequent agents (running as the repo
    owner) can stage freely. No-op in single-user / self-hosted deployments.
    Mirrors `_merge_branch_sync`'s chown block for refs/ and logs/.
    """
    if not runas_user():
        return
    targets: list[Path] = [repo_dir / ".git" / "index"]
    for f in files:
        path = repo_dir / f
        if path.exists():
            targets.append(path)
    chown_to_runas(*targets, recursive=False)
