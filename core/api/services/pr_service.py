# v2.5.0 - 2026-03-13 - CI merge gate: block merge when required checks fail
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import aiosqlite

from core.api.config import settings
from core.api.models import UserInfo
from core.api.services import git_ops
from core.api.services.runas import GIT_CMD
from core.api.services.claude_metrics import find_conversation_for_worktree
from core.api.services.task_transitions import validate_and_transition_task
from core.api.use_cases._errors import ConflictError

logger = logging.getLogger(__name__)

# Per-repo lock to prevent concurrent merge races on the same repository
_repo_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Module-level set to prevent asyncio.Task objects from being GC'd
_background_tasks: set[asyncio.Task] = set()


async def _get_task(db: aiosqlite.Connection, task_id: str) -> aiosqlite.Row:
    """Fetch task or raise ValueError."""
    cursor = await db.execute(
        "SELECT id, status, project FROM tasks WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise ValueError(f"Task not found: {task_id}")
    return row


async def _get_repo_path(db: aiosqlite.Connection, project_slug: str) -> str:
    """Resolve project slug to repo_path. Only code/system projects have repos."""
    # Import here to avoid circular dependency
    from core.api.routers.projects import _find_git_path

    repo = _find_git_path(project_slug)
    if not repo:
        raise ValueError(
            f"Project '{project_slug}' has no git repo (work projects don't support PR workflow)"
        )
    return str(repo)


async def _get_active_pr(
    db: aiosqlite.Connection, task_id: str
) -> aiosqlite.Row | None:
    """Get active PR (draft/open/merging) for a task."""
    cursor = await db.execute(
        "SELECT * FROM pull_requests WHERE task_id = ? AND status IN ('draft', 'open', 'merging')",
        (task_id,),
    )
    return await cursor.fetchone()


async def _get_pr_by_task(
    db: aiosqlite.Connection, task_id: str
) -> aiosqlite.Row | None:
    """Get most recent PR for a task (any status)."""
    cursor = await db.execute(
        "SELECT * FROM pull_requests WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    )
    return await cursor.fetchone()


async def _fetch_github_labels(repo_path: str, branch: str) -> list[str]:
    """Fetch labels for a GitHub PR branch via gh CLI, raising on failure."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "pr",
        "view",
        branch,
        "--json",
        "labels",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("gh pr view labels timed out") from exc
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "gh pr view labels failed")
    payload = json.loads(stdout.decode("utf-8"))
    labels = payload.get("labels") or []
    return sorted(
        str(label.get("name"))
        for label in labels
        if isinstance(label, dict) and label.get("name")
    )


def _pr_row_to_dict(row: aiosqlite.Row) -> dict:
    """Convert PR row to dict."""
    d = {
        "id": row["id"],
        "task_id": row["task_id"],
        "project": row["project"],
        "branch": row["branch"],
        "target": row["target"],
        "status": row["status"],
        "title": row["title"],
        "body": row["body"],
        "worktree_path": row["worktree_path"],
        "closed_reason": row["closed_reason"],
        "merged_at": row["merged_at"],
        "created_at": row["created_at"],
    }
    # Deploy tracking fields (migration 031) — graceful fallback if not yet applied
    for field in ("deploy_status", "deploy_output", "deploy_at"):
        try:
            d[field] = row[field]
        except (IndexError, KeyError):
            d[field] = None
    # Approval fields (migration 037) — graceful fallback if not yet applied
    for field in ("approved_by", "approved_at", "submitted_by"):
        try:
            d[field] = row[field]
        except (IndexError, KeyError):
            d[field] = None
    return d


# Regex to match migration file paths in unified diff output.
# Matches lines like:
#   diff --git a/migrations/024_audit_log.sql b/migrations/024_audit_log.sql
#   +++ b/migrations/024_audit_log.sql
_MIGRATION_RE = re.compile(r"^(?:a/|b/)?migrations/(\d{3})_.*\.sql$", re.MULTILINE)


def extract_migration_numbers(unified_diff: str) -> list[int]:
    """Extract migration version numbers from a PR unified diff."""
    numbers: set[int] = set()
    for match in _MIGRATION_RE.finditer(unified_diff):
        numbers.add(int(match.group(1)))
    return sorted(numbers)


async def get_merge_conflicts(project: str, db: aiosqlite.Connection) -> list[dict]:
    """Detect migration number conflicts across open PRs for a project.

    Returns list of conflict groups, each with migration_number and ordered tasks.
    Only PRs with status='open' are considered.
    """
    cursor = await db.execute(
        "SELECT task_id, branch, target, created_at FROM pull_requests "
        "WHERE project = ? AND status = 'open' ORDER BY created_at ASC",
        (project,),
    )
    rows = await cursor.fetchall()

    if not rows:
        return []

    # Resolve repo path once
    repo_path = await _get_repo_path(db, project)

    # For each open PR, compute diff and extract migration numbers
    # migration_number -> [(task_id, created_at)]
    migration_map: dict[int, list[tuple[str, str]]] = defaultdict(list)

    for row in rows:
        try:
            diff = await git_ops.get_pr_diff_async(
                repo_path, row["branch"], row["target"]
            )
            migration_nums = extract_migration_numbers(diff["unified_diff"])
            for num in migration_nums:
                migration_map[num].append((row["task_id"], row["created_at"]))
        except Exception as exc:
            logger.warning(
                "get_merge_conflicts: cannot get diff for task %s branch %s: %s",
                row["task_id"],
                row["branch"],
                exc,
            )
            continue

    # Build conflict groups (only where 2+ PRs share a migration number)
    conflicts = []
    for mig_num in sorted(migration_map.keys()):
        entries = migration_map[mig_num]
        if len(entries) < 2:
            continue

        # Already sorted by created_at ASC from the SQL query
        first_task_id = entries[0][0]
        tasks = []
        for position, (task_id, created_at) in enumerate(entries, start=1):
            tasks.append(
                {
                    "task_id": task_id,
                    "pr_created_at": created_at,
                    "merge_position": position,
                    "can_merge": position == 1,
                    "blocked_by": first_task_id if position > 1 else None,
                }
            )

        conflicts.append(
            {
                "migration_number": mig_num,
                "tasks": tasks,
            }
        )

    return conflicts


async def _check_main_in_sync(repo_path: str) -> None:
    """Raise ValueError (409) if local main is ahead of origin/main.

    Prevents worktree creation from a stale base, which causes merge conflicts
    when un-pushed local merges exist. Both commands run in a thread pool to
    avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()

    def _get_heads() -> tuple[str, str]:
        git = GIT_CMD
        local = subprocess.run(
            [*git, "-C", repo_path, "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        remote_out = subprocess.run(
            [*git, "-C", repo_path, "ls-remote", "origin", "refs/heads/main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # ls-remote returns "<sha>\trefs/heads/main" or empty string
        remote = remote_out.split()[0] if remote_out else ""
        return local, remote

    local_sha, origin_sha = await loop.run_in_executor(None, _get_heads)

    if origin_sha and local_sha != origin_sha:
        raise ValueError(
            f"Local main ({local_sha[:8]}) differs from origin/main ({origin_sha[:8]}). "
            "Push pending merges first: git push origin main"
        )


async def start_branch(
    task_id: str, db: aiosqlite.Connection, base_branch: str = "main"
) -> dict:
    """Create worktree and PR record. Idempotent."""
    task = await _get_task(db, task_id)

    if task["status"] != "in_progress":
        raise ValueError(
            f"Task must be in_progress to start branch (current: {task['status']})"
        )

    project = task["project"]
    repo_path = await _get_repo_path(db, project)

    # Check for existing active PR
    existing = await _get_active_pr(db, task_id)
    if existing:
        return {
            **_pr_row_to_dict(existing),
            "already_existed": True,
        }

    # Guard: local main must not be ahead of origin/main
    await _check_main_in_sync(repo_path)

    # Create worktree
    wt_info = await git_ops.create_worktree_async(repo_path, task_id, base_branch)

    # Insert PR record
    pr_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO pull_requests (id, task_id, project, branch, target, status, worktree_path)
           VALUES (?, ?, ?, ?, ?, 'draft', ?)""",
        (
            pr_id,
            task_id,
            project,
            wt_info["branch_name"],
            base_branch,
            wt_info["worktree_path"],
        ),
    )
    await db.commit()

    logger.info(
        "Started branch for task %s: PR %s, branch %s",
        task_id,
        pr_id,
        wt_info["branch_name"],
    )

    return {
        "id": pr_id,
        "task_id": task_id,
        "project": project,
        "branch": wt_info["branch_name"],
        "target": base_branch,
        "status": "draft",
        "worktree_path": wt_info["worktree_path"],
        "already_existed": wt_info["already_existed"],
    }


async def start_branch_short_write(
    task_id: str, db: aiosqlite.Connection, base_branch: str = "main"
) -> dict:
    """Create worktree without holding the SQLite writer during git I/O."""
    task = await _get_task(db, task_id)

    if task["status"] != "in_progress":
        raise ValueError(
            f"Task must be in_progress to start branch (current: {task['status']})"
        )

    project = task["project"]
    repo_path = await _get_repo_path(db, project)

    existing = await _get_active_pr(db, task_id)
    if existing:
        return {
            **_pr_row_to_dict(existing),
            "already_existed": True,
        }

    await _check_main_in_sync(repo_path)
    wt_info = await git_ops.create_worktree_async(repo_path, task_id, base_branch)

    from core.api.db import write_db

    pr_id = str(uuid.uuid4())
    async with write_db(label="pr.start_branch.record") as wdb:
        task = await _get_task(wdb, task_id)
        if task["status"] != "in_progress":
            raise ValueError(
                f"Task must be in_progress to start branch (current: {task['status']})"
            )
        existing = await _get_active_pr(wdb, task_id)
        if existing:
            return {
                **_pr_row_to_dict(existing),
                "already_existed": True,
            }
        await wdb.execute(
            """INSERT INTO pull_requests (id, task_id, project, branch, target, status, worktree_path)
               VALUES (?, ?, ?, ?, ?, 'draft', ?)""",
            (
                pr_id,
                task_id,
                project,
                wt_info["branch_name"],
                base_branch,
                wt_info["worktree_path"],
            ),
        )

    logger.info(
        "Started branch for task %s: PR %s, branch %s",
        task_id,
        pr_id,
        wt_info["branch_name"],
    )

    return {
        "id": pr_id,
        "task_id": task_id,
        "project": project,
        "branch": wt_info["branch_name"],
        "target": base_branch,
        "status": "draft",
        "worktree_path": wt_info["worktree_path"],
        "already_existed": wt_info["already_existed"],
    }


async def register_branch(
    task_id: str,
    branch_name: str,
    db: aiosqlite.Connection,
    worktree_path: str | None = None,
) -> dict:
    """Register an externally-created branch/worktree as a draft PR record.

    Used when an agent creates a worktree directly via git (not via /branch endpoint).
    Idempotent: returns existing active PR if one exists for the task.
    """
    task = await _get_task(db, task_id)

    if task["status"] != "in_progress":
        raise ValueError(
            f"Task must be in_progress to register branch (current: {task['status']})"
        )

    project = task["project"]
    # Validate project has a repo (code/system only)
    await _get_repo_path(db, project)

    # Check for existing active PR — idempotent
    existing = await _get_active_pr(db, task_id)
    if existing:
        return {
            **_pr_row_to_dict(existing),
            "already_existed": True,
        }

    # Insert PR record
    pr_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO pull_requests (id, task_id, project, branch, target, status, worktree_path, created_at)
           VALUES (?, ?, ?, ?, 'main', 'draft', ?, ?)""",
        (pr_id, task_id, project, branch_name, worktree_path, now),
    )
    await db.commit()

    logger.info(
        "Registered branch for task %s: PR %s, branch %s", task_id, pr_id, branch_name
    )

    return {
        "id": pr_id,
        "task_id": task_id,
        "project": project,
        "branch": branch_name,
        "target": "main",
        "status": "draft",
        "worktree_path": worktree_path,
        "already_existed": False,
    }


async def submit_pr(
    task_id: str,
    title: str,
    body: str,
    db: aiosqlite.Connection,
    submitted_by: str | None = None,
) -> dict:
    """Validate and move PR from draft to open. Calculate diff."""
    pr = await _get_active_pr(db, task_id)
    if not pr:
        raise ValueError(f"No active PR for task {task_id}")
    if pr["status"] not in ("draft", "open"):
        raise ValueError(
            f"PR must be in draft or open to submit (current: {pr['status']})"
        )

    task = await _get_task(db, task_id)
    repo_path = await _get_repo_path(db, task["project"])

    # Calculate diff before starting transaction (I/O outside lock)
    diff = await git_ops.get_pr_diff_async(repo_path, pr["branch"], pr["target"])

    if diff["is_empty"]:
        raise ValueError(
            "No commits found on branch. Make at least one commit before submitting PR."
        )

    # Scan worktree for conversation_id — do this BEFORE the transaction (I/O outside lock)
    conv_id = None
    if pr["worktree_path"]:
        conv_id = await asyncio.to_thread(
            find_conversation_for_worktree, pr["worktree_path"]
        )

    # Atomic: update PR + transition task in single commit
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "UPDATE pull_requests SET status = 'open', title = ?, body = ? WHERE id = ?",
            (title, body, pr["id"]),
        )
        # submitted_by column added in migration 038 — graceful fallback
        if submitted_by:
            try:
                await db.execute(
                    "UPDATE pull_requests SET submitted_by = ? WHERE id = ?",
                    (submitted_by, pr["id"]),
                )
            except Exception:
                logger.debug(
                    "submitted_by column not yet available (migration 038 pending)"
                )
        if conv_id:
            await db.execute(
                "UPDATE pull_requests SET conversation_id = ? WHERE id = ?",
                (conv_id, pr["id"]),
            )
        await validate_and_transition_task(
            db, task_id, "review", trigger="pr_submit", auto_commit=False
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    if conv_id:
        logger.info(
            "PR %s: linked conversation %.8s from worktree", pr["id"][:8], conv_id
        )
    else:
        logger.warning(
            "PR %s: no JSONL found for worktree %s",
            pr["id"][:8],
            pr["worktree_path"] or "",
        )

    logger.info("Submitted PR %s for task %s: %s", pr["id"], task_id, title)

    # Emit event + generate notification for PR submission
    try:
        from core.api.services.events import emit_event
        from core.api.services.notification_service import generate_from_event

        pr_payload = {"task_id": task_id, "branch": pr["branch"], "title": title}
        event_id = await emit_event(
            db,
            "pr.submitted",
            project=task["project"],
            target_type="pr",
            target_id=pr["id"],
            payload=pr_payload,
        )
        if event_id:
            await generate_from_event(
                db,
                "pr.submitted",
                event_id,
                task["project"],
                None,
                "pr",
                pr["id"],
                pr_payload,
            )
    except Exception as exc:
        logger.warning(
            "PR %s: notification generation failed (non-blocking): %s",
            pr["id"][:8],
            exc,
        )

    # Detect migration number conflicts with other open PRs (non-blocking warning)
    migration_conflicts: list[str] = []
    try:
        conflict_groups = await get_merge_conflicts(task["project"], db)
        for group in conflict_groups:
            task_ids_in_group = [e["task_id"] for e in group["tasks"]]
            if task_id in task_ids_in_group:
                # Format: "025: task-abc123 vs task-def456"
                other_ids = [
                    e["task_id"] for e in group["tasks"] if e["task_id"] != task_id
                ]
                for other_id in other_ids:
                    migration_conflicts.append(
                        f"migration_{group['migration_number']:03d}: {task_id[:8]} vs {other_id[:8]}"
                    )
    except Exception as exc:
        logger.warning(
            "PR %s: migration conflict check failed (non-blocking): %s",
            pr["id"][:8],
            exc,
        )

    if migration_conflicts:
        logger.warning(
            "PR %s submitted with migration conflicts: %s",
            pr["id"][:8],
            migration_conflicts,
        )

    return {
        **_pr_row_to_dict(pr),
        "status": "open",
        "title": title,
        "body": body,
        "diff": diff,
        "migration_conflicts": migration_conflicts,
    }


async def get_pr_status(task_id: str, db: aiosqlite.Connection) -> dict:
    """Return full PR status including diff_summary."""
    pr = await _get_pr_by_task(db, task_id)
    if not pr:
        raise ValueError(f"No PR found for task {task_id}")

    result = _pr_row_to_dict(pr)
    repo_path_for_labels: str | None = None

    # Calculate live diff for active PRs
    if pr["status"] in ("draft", "open", "merging"):
        try:
            task = await _get_task(db, task_id)
            repo_path = await _get_repo_path(db, task["project"])
            repo_path_for_labels = repo_path
            diff = await git_ops.get_pr_diff_async(
                repo_path, pr["branch"], pr["target"]
            )
            result["diff"] = diff
        except Exception as exc:
            logger.warning("Cannot calculate diff for PR %s: %s", pr["id"], exc)
            result["diff"] = None
    else:
        result["diff"] = None

    result["gh_labels"] = []
    if pr["status"] in ("open", "merging", "merged"):
        try:
            if repo_path_for_labels is None:
                task = await _get_task(db, task_id)
                repo_path_for_labels = await _get_repo_path(db, task["project"])
            result["gh_labels"] = await _fetch_github_labels(
                repo_path_for_labels,
                pr["branch"],
            )
        except Exception as exc:
            logger.info("Cannot fetch GitHub labels for PR %s: %s", pr["id"], exc)

    return result


async def _wait_deploy(
    proc: asyncio.subprocess.Process, cmd: str, project: str, pr_id: str | None = None
) -> None:
    """Background: wait for deploy process, record result in PR row."""

    async def _record_deploy(status: str, output: str) -> None:
        """Best-effort write of deploy result to pull_requests."""
        if not pr_id:
            return
        try:
            from core.api.db import write_db

            async with write_db(label="pr.deploy.record") as db:
                await db.execute(
                    "UPDATE pull_requests SET deploy_status=?, deploy_output=?, deploy_at=? "
                    "WHERE id=? AND (deploy_output IS NULL OR deploy_output = '')",
                    (
                        status,
                        output[-2000:],
                        datetime.now(timezone.utc).isoformat(),
                        pr_id,
                    ),
                )
        except Exception as exc:
            logger.warning("Failed to record deploy result for PR %s: %s", pr_id, exc)

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode(errors="replace").strip() if stdout else ""
        rc = proc.returncode
        status = "success" if rc == 0 else "failed"

        if rc == 0:
            logger.info(
                "deploy_command OK for %s (rc=0): %s",
                project,
                output[-500:] if output else "no output",
            )
        else:
            logger.warning(
                "deploy_command FAILED for %s (rc=%d): %s", project, rc, output[-500:]
            )

        await _record_deploy(status, output)

    except asyncio.TimeoutError:
        logger.error("deploy_command timed out after 300s for %s", project)
        proc.kill()
        await _record_deploy("failed", "Deploy timed out after 300s")

    except Exception as exc:
        logger.error("deploy_command exception for %s: %s", project, exc)
        await _record_deploy("failed", f"Exception: {exc}")


# Anti-zombie A (task 115d2d7a): PR body "closes <uuid>" parser.
#
# Matches forms: "closes abc1234", "Closes #abc1234", "fixes abc1234-...",
# "resolved ABC1234". Captures either a full UUID (36 char with dashes)
# or a short hex prefix (7-12 chars). Does NOT match bare hex runs without
# a close/fix/resolve verb, so a 40-char SHA git in the body is safe.
_CLOSE_PATTERN = re.compile(
    r"\b(?:clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed))\s+#?"
    r"([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|[a-f0-9]{7,12})",
    re.IGNORECASE,
)


async def _close_siblings_from_body(
    db: aiosqlite.Connection,
    pr: dict,
    task_project: str,
    primary_task_id: str,
) -> list[str]:
    """Parse PR body for close/fix/resolve patterns and auto-close matching tasks.

    Returns list of task_ids successfully closed (for logging).
    Guards:
    - Skip primary task (already closed by pr_merge trigger).
    - Skip if secondary not in same project.
    - Skip if secondary not in (approved, in_progress).
    - Skip ambiguous short UUID prefixes (multiple matches → unsafe).
    - Best-effort: individual failure does not abort the sweep.
    """
    try:
        body = pr["body"] or ""
    except (IndexError, KeyError):
        body = ""
    if not body:
        return []
    closed: list[str] = []
    # Deduplicate matches (same task mentioned twice in the body).
    mentioned = {m.lower() for m in _CLOSE_PATTERN.findall(body)}
    for ref in mentioned:
        # Expand short UUID prefix (7-12 char) to full via LIKE prefix match.
        # LIMIT 2 → detect ambiguous prefixes cheaply.
        cursor = await db.execute(
            "SELECT id, status, project FROM tasks "
            "WHERE (id = ? OR id LIKE ? || '-%') AND deleted_at IS NULL "
            "LIMIT 2",
            (ref, ref),
        )
        rows = await cursor.fetchall()
        if len(rows) != 1:
            # Ambiguous (multiple tasks with same prefix) or missing — skip.
            if len(rows) > 1:
                logger.warning(
                    "Sibling close skip: ref %r matches %d tasks (ambiguous)",
                    ref,
                    len(rows),
                )
            continue
        row = rows[0]
        secondary_id = row["id"]
        if secondary_id == primary_task_id:
            continue
        if row["project"] != task_project:
            continue
        if row["status"] not in ("approved", "in_progress"):
            continue
        try:
            # approved → completed is not a legal one-step transition
            # (VALID_TRANSITIONS). Bridge via in_progress so the sibling
            # lands in completed cleanly. Use same trigger so the guard
            # allowlist covers both hops.
            if row["status"] == "approved":
                await validate_and_transition_task(
                    db,
                    secondary_id,
                    "in_progress",
                    trigger="closed_by_sibling_pr",
                )
            await validate_and_transition_task(
                db, secondary_id, "completed", trigger="closed_by_sibling_pr"
            )
            closed.append(secondary_id)
        except Exception as exc:
            logger.warning(
                "Sibling close failed for task %s: %s", secondary_id[:8], exc
            )
    return closed


async def merge_pr(
    task_id: str, db: aiosqlite.Connection, merger_id: str | None = None
) -> dict:
    """Merge PR. Called by router after human auth check."""
    pr = await _get_active_pr(db, task_id)
    if not pr:
        raise ValueError(f"No active PR for task {task_id}")
    if pr["status"] != "open":
        raise ValueError(f"PR must be open to merge (current: {pr['status']})")

    # Auto-approve on merge if not already approved (single-user: merge = implicit approval)
    approved_by = None
    try:
        approved_by = pr["approved_by"]
    except (IndexError, KeyError):
        pass
    if approved_by is None and merger_id:
        now_approve = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE pull_requests SET approved_by = ?, approved_at = ? WHERE id = ?",
            (merger_id, now_approve, pr["id"]),
        )
        await db.commit()
        logger.info("PR %s: auto-approved by merger %s", pr["id"][:8], merger_id)

    task = await _get_task(db, task_id)
    repo_path = await _get_repo_path(db, task["project"])

    # Guard: CI required checks must pass before merge
    from core.api.services.ci_service import check_required_ci_passes

    failing_ci = await check_required_ci_passes(task_id, task["project"], db)
    if failing_ci:
        raise ConflictError(
            code="ci_checks_failing",
            message=f"CI checks not passing: {', '.join(failing_ci)}",
        )

    # Guard: check migration merge-order conflicts before allowing merge
    conflicts = await get_merge_conflicts(task["project"], db)
    for group in conflicts:
        for entry in group["tasks"]:
            if entry["task_id"] == task_id and not entry["can_merge"]:
                raise ConflictError(
                    code="migration_merge_order_conflict",
                    message=f"Migration conflict: merge task {entry['blocked_by']} first",
                )

    async with _repo_locks[repo_path]:
        # Lock: set status to merging
        await db.execute(
            "UPDATE pull_requests SET status = 'merging' WHERE id = ?", (pr["id"],)
        )
        await db.commit()

        try:
            merge_result = await git_ops.merge_branch_async(
                repo_path, pr["branch"], pr["target"]
            )
        except git_ops.MergeConflictError as exc:
            # Rollback lock
            await db.execute(
                "UPDATE pull_requests SET status = 'open' WHERE id = ?", (pr["id"],)
            )
            await db.commit()
            raise
        except Exception as exc:
            # Rollback lock on any error
            await db.execute(
                "UPDATE pull_requests SET status = 'open' WHERE id = ?", (pr["id"],)
            )
            await db.commit()
            raise git_ops.GitOpsError(f"Merge failed: {exc}") from exc

        # Sync primary workspace working tree with the merged commit so hooks
        # and scripts running out of repo_path see fresh file content. Fail-soft:
        # never regresses merge success. See api/services/workspace_sync.py.
        pre_merge_sha = merge_result.get("pre_merge_sha") or ""
        if pre_merge_sha and not merge_result.get("already_merged"):
            from core.api.services.workspace_sync import sync_primary_workspace_after_merge

            try:
                await sync_primary_workspace_after_merge(
                    repo_dir=repo_path,
                    merge_commit_sha=merge_result["commit_sha"],
                    pre_merge_sha=pre_merge_sha,
                )
            except Exception as sync_exc:
                logger.warning(
                    "event=workspace_sync_call_failed pr_id=%s exc=%s",
                    pr["id"][:8],
                    sync_exc,
                )

        # Success: update PR
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE pull_requests SET status = 'merged', merged_at = ? WHERE id = ?",
            (now, pr["id"]),
        )
        await db.commit()

        # Store commit SHA for future revert
        await db.execute(
            "UPDATE pull_requests SET commit_sha = ? WHERE id = ?",
            (merge_result["commit_sha"], pr["id"]),
        )
        await db.commit()

        # Auto-transition task to completed
        try:
            await validate_and_transition_task(
                db, task_id, "completed", trigger="pr_merge"
            )
        except ValueError as exc:
            logger.warning(
                "Could not transition task %s to completed: %s", task_id, exc
            )

        # Anti-zombie A (task 115d2d7a): parse PR body for "closes <uuid>"
        # patterns and auto-close sibling tasks that were covered by this PR
        # bundle but lacked a direct task_id binding. Guard: same project +
        # status in (approved|in_progress). Best-effort — failure does not
        # regress the merge success.
        try:
            _closed_siblings = await _close_siblings_from_body(
                db, pr=pr, task_project=task["project"], primary_task_id=task_id
            )
            if _closed_siblings:
                logger.info(
                    "PR %s: auto-closed %d sibling task(s) from body: %s",
                    pr["id"][:8],
                    len(_closed_siblings),
                    ", ".join(t[:8] for t in _closed_siblings),
                )
        except Exception as exc:
            logger.warning(
                "PR %s: sibling closure sweep failed: %s", pr["id"][:8], exc
            )

    # Create agent cost entry linked to this PR (fire-and-forget, non-blocking)
    # pr["conversation_id"] was set at submit time by scanning the worktree JSONL
    try:
        pr_conv_id = pr["conversation_id"]
    except (IndexError, KeyError):
        pr_conv_id = None
    if pr_conv_id:
        from core.api.services import cost_service

        task_obj = asyncio.create_task(
            cost_service.create_agent_entry(
                task_id=task_id,
                project_slug=task["project"],
                source="task_completed",
                created_by="pr_merge",
                db_path=settings.db_path,
                conversation_id=pr_conv_id,
                pr_id=pr["id"],
            ),
            name=f"cost-entry-pr-{pr['id'][:8]}",
        )
        _background_tasks.add(task_obj)
        task_obj.add_done_callback(_background_tasks.discard)
        logger.info(
            "PR %s: scheduled cost entry for conversation %.8s",
            pr["id"][:8],
            pr_conv_id,
        )
    else:
        logger.info(
            "PR %s: no conversation_id, skipping cost entry (session or manual PR)",
            pr["id"][:8],
        )

    # Cleanup worktree (best effort)
    try:
        await git_ops.remove_worktree_async(
            repo_path, pr["worktree_path"], pr["branch"]
        )
    except Exception as exc:
        logger.warning("Worktree cleanup failed for PR %s: %s", pr["id"], exc)

    # Emit pr.merged event for n8n dispatcher
    try:
        from core.api.services.events import emit_event

        # merge_pr already runs under get_write_db() from the router, so re-entering
        # write_db() here would deadlock on the single-writer lock.
        await emit_event(
            db,
            event_type="pr.merged",
            project=task["project"],
            actor_id=None,
            target_type="pr",
            target_id=pr["id"],
            payload={
                "task_id": task_id,
                "branch": pr["branch"],
                "commit_sha": merge_result["commit_sha"],
            },
        )
        await db.commit()
    except Exception:
        logger.warning("Failed to emit pr.merged event for %s (non-critical)", pr["id"])

    logger.info(
        "Merged PR %s for task %s, commit %s",
        pr["id"],
        task_id,
        merge_result["commit_sha"],
    )

    # Execute deploy_command if configured (fire-and-forget, non-blocking)
    # CRITICAL: The deploy script restarts pir-api.service, which kills ALL processes
    # in the service cgroup — including this uvicorn and any child processes.
    # Fix: use sudo systemd-run --scope (system-level) to launch deploy-wrapper.sh in a separate
    # cgroup. The wrapper runs the deploy, captures output, and writes the result
    # directly to SQLite (since the API process will be dead during deploy restart).
    try:
        from core.api.routers.projects import _find_project_entry, _read_project_yaml

        entry = _find_project_entry(task["project"])
        if entry:
            yaml_data = _read_project_yaml(entry.metadata_path)
            deploy_cmd = yaml_data.get("deploy_command") if yaml_data else None
            if deploy_cmd:
                cwd = str(entry.repo_path or entry.metadata_path)
                scope_name = f"deploy-{task['project']}-{pr['id'][:8]}"
                wrapper_path = os.path.join(cwd, "scripts", "deploy-wrapper.sh")

                if not os.path.isfile(wrapper_path):
                    logger.warning(
                        "deploy-wrapper.sh not found at %s, skipping deploy",
                        wrapper_path,
                    )
                else:
                    wrapped_cmd = (
                        f"sudo systemd-run --scope --unit={scope_name} "
                        f"bash {wrapper_path} "
                        f"{deploy_cmd!r} {cwd!r} {str(settings.db_path)!r} {pr['id']!r} {task_id!r}"
                    )
                    logger.info(
                        "Executing deploy_command for %s via scope %s: %r",
                        task["project"],
                        scope_name,
                        deploy_cmd,
                    )
                    proc = await asyncio.create_subprocess_shell(
                        wrapped_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    # Fire-and-forget: the scope runs independently of this process.
                    # _wait_deploy logs the systemd-run exit and serves as a fallback
                    # recorder if the wrapper's own DB write fails.
                    t = asyncio.create_task(
                        _wait_deploy(proc, deploy_cmd, task["project"], pr_id=pr["id"]),
                        name=f"deploy-{pr['id'][:8]}",
                    )
                    _background_tasks.add(t)
                    t.add_done_callback(_background_tasks.discard)
    except Exception as exc:
        logger.warning("deploy_command setup failed for %s: %s", task["project"], exc)

    return {
        "merged": True,
        "already_merged": merge_result["already_merged"],
        "commit_sha": merge_result["commit_sha"],
        "pr_id": pr["id"],
    }


async def _check_reviewer_authorized(
    pr_row: aiosqlite.Row, reviewer: UserInfo, db: aiosqlite.Connection
) -> None:
    """Raise PermissionError if reviewer is not authorized to review this PR.

    Authorization: global admin/super_admin OR team_admin of any team that owns the project.
    """
    from core.api.rbac import check_team_admin

    if reviewer.system_role in ("admin", "super_admin"):
        return

    # Look up all teams that own the project
    cursor = await db.execute(
        "SELECT team_id FROM project_teams WHERE project = ?",
        (pr_row["project"],),
    )
    team_rows = await cursor.fetchall()
    for team_row in team_rows:
        if await check_team_admin(team_row["team_id"], reviewer, db):
            return

    raise PermissionError(
        f"User {reviewer.username!r} is not authorized to review PRs for project {pr_row['project']!r}. "
        "Requires team_admin role on the project or global admin+."
    )


async def approve_pr(
    task_id: str, reviewer: UserInfo, db: aiosqlite.Connection
) -> dict:
    """Approve PR. Four-eyes gate: reviewer cannot be the submitter.

    Authorization: team_admin of the project OR admin/super_admin.
    """
    pr = await _get_active_pr(db, task_id)
    if not pr:
        raise ValueError(f"No active PR for task {task_id}")
    if pr["status"] != "open":
        raise ValueError(f"PR must be open to approve (current: {pr['status']})")

    # Four-eyes gate: reviewer != submitter
    submitted_by = None
    try:
        submitted_by = pr["submitted_by"]
    except (IndexError, KeyError):
        pass
    if submitted_by and submitted_by == reviewer.user_id:
        raise ValueError("Four-eyes violation: you cannot approve your own PR")

    # Authorization check
    await _check_reviewer_authorized(pr, reviewer, db)

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE pull_requests SET approved_by = ?, approved_at = ? WHERE id = ?",
        (reviewer.user_id, now, pr["id"]),
    )
    await db.commit()

    # Re-fetch updated row
    cursor = await db.execute("SELECT * FROM pull_requests WHERE id = ?", (pr["id"],))
    updated = await cursor.fetchone()

    # Emit event (non-blocking)
    try:
        from core.api.services.events import emit_event

        await emit_event(
            db,
            event_type="pr.approved",
            project=pr["project"],
            actor_id=reviewer.user_id,
            target_type="pr",
            target_id=pr["id"],
            payload={
                "task_id": task_id,
                "approved_by": reviewer.username,
                "project": pr["project"],
            },
        )
    except Exception as exc:
        logger.warning(
            "PR %s: emit pr.approved failed (non-blocking): %s", pr["id"][:8], exc
        )

    logger.info("PR %s approved by %s", pr["id"][:8], reviewer.username)
    return _pr_row_to_dict(updated)


async def request_changes_pr(
    task_id: str, reviewer: UserInfo, comment: str, db: aiosqlite.Connection
) -> dict:
    """Request changes on PR. Revokes approval and sends task back to in_progress.

    Authorization: team_admin of the project OR admin/super_admin.
    """
    pr = await _get_active_pr(db, task_id)
    if not pr:
        raise ValueError(f"No active PR for task {task_id}")
    if pr["status"] != "open":
        raise ValueError(
            f"PR must be open to request changes (current: {pr['status']})"
        )

    # Authorization check
    await _check_reviewer_authorized(pr, reviewer, db)

    # Atomic: revoke approval + set review_feedback + transition task
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "UPDATE pull_requests SET approved_by = NULL, approved_at = NULL WHERE id = ?",
            (pr["id"],),
        )
        now_ts = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE tasks SET review_feedback = ?, updated_at = ? WHERE id = ?",
            (comment or None, now_ts, task_id),
        )
        await validate_and_transition_task(
            db,
            task_id,
            "in_progress",
            trigger="pr_changes_requested",
            auto_commit=False,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # Re-fetch updated PR row
    cursor = await db.execute("SELECT * FROM pull_requests WHERE id = ?", (pr["id"],))
    updated = await cursor.fetchone()

    # Emit event (non-blocking)
    try:
        from core.api.services.events import emit_event

        await emit_event(
            db,
            event_type="pr.changes_requested",
            project=pr["project"],
            actor_id=reviewer.user_id,
            target_type="pr",
            target_id=pr["id"],
            payload={
                "task_id": task_id,
                "reviewer": reviewer.username,
                "project": pr["project"],
                "comment": comment,
            },
        )
    except Exception as exc:
        logger.warning(
            "PR %s: emit pr.changes_requested failed (non-blocking): %s",
            pr["id"][:8],
            exc,
        )

    logger.info("PR %s: changes requested by %s", pr["id"][:8], reviewer.username)
    return _pr_row_to_dict(updated)


async def close_pr(task_id: str, reason: str, db: aiosqlite.Connection) -> dict:
    """Close PR without merge. Task returns to in_progress with review_feedback set."""
    pr = await _get_active_pr(db, task_id)
    if not pr:
        raise ValueError(f"No active PR for task {task_id}")
    if pr["status"] not in ("draft", "open"):
        raise ValueError(f"PR must be draft or open to close (current: {pr['status']})")

    # Atomic: close PR + set review_feedback + transition task in single commit
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "UPDATE pull_requests SET status = 'closed', closed_reason = ? WHERE id = ?",
            (reason or None, pr["id"]),
        )
        now_ts = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE tasks SET review_feedback = ?, updated_at = ? WHERE id = ?",
            (reason or None, now_ts, task_id),
        )
        await validate_and_transition_task(
            db, task_id, "in_progress", trigger="pr_close", auto_commit=False
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # Cleanup worktree (best effort)
    if pr["worktree_path"]:
        try:
            task_row = await _get_task(db, task_id)
            repo_path = await _get_repo_path(db, task_row["project"])
            await git_ops.remove_worktree_async(
                repo_path, pr["worktree_path"], pr["branch"]
            )
        except Exception as exc:
            logger.warning("Worktree cleanup failed for PR %s: %s", pr["id"], exc)

    logger.info(
        "Closed PR %s for task %s (reason: %s)", pr["id"], task_id, reason or "none"
    )

    return {"id": pr["id"], "status": "closed", "closed_reason": reason}


async def revert_pr(task_id: str, db: aiosqlite.Connection) -> dict:
    """Create a revert PR for a completed task with a merged PR.

    Creates a new task 'Revert: {title}' + new PR record.
    The revert task starts in_progress with PR open (ready for Triage review).
    """
    # Find merged PR
    cursor = await db.execute(
        "SELECT * FROM pull_requests WHERE task_id = ? AND status = 'merged' ORDER BY merged_at DESC LIMIT 1",
        (task_id,),
    )
    pr = await cursor.fetchone()
    if not pr:
        raise ValueError(f"No merged PR found for task {task_id}")

    commit_sha = pr["commit_sha"]
    if not commit_sha:
        raise ValueError(
            "PR has no commit_sha stored. Only PRs merged after v1.4.0 can be reverted via this endpoint."
        )

    task = await _get_task(db, task_id)
    repo_path = await _get_repo_path(db, task["project"])

    revert_branch = f"revert/task-{task_id[:8]}-{commit_sha[:7]}"

    # Create revert commit on new branch
    revert_info = await git_ops.revert_commit_async(
        repo_path, commit_sha, revert_branch
    )

    now = datetime.now(timezone.utc).isoformat()

    # Create new task for the revert (direct insert — bypass normal pending→approved→in_progress flow)
    new_task_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO tasks
           (id, title, description, project, status, source, priority, delegation, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'in_progress', 'manual', 'high', 'human', ?, ?)""",
        (
            new_task_id,
            f"Revert: {task['title']}",
            f"Revert del commit {commit_sha[:7]} dalla task originale {task_id[:8]}.\n-{os.path.expanduser('~')}/workspace/projects/MarvisX",
            task["project"],
            now,
            now,
        ),
    )

    # Create PR record for the new revert task
    new_pr_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO pull_requests
           (id, task_id, project, branch, target, status, title, body, commit_sha, created_at)
           VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
        (
            new_pr_id,
            new_task_id,
            task["project"],
            revert_branch,
            pr["target"],
            f"Revert: {task['title']}",
            f"Reverts commit {commit_sha[:7]}\n\nOriginal task: {task_id}",
            revert_info["commit_sha"],
            now,
        ),
    )
    await db.commit()

    logger.info(
        "Created revert PR %s for task %s on branch %s (reverts %s)",
        new_pr_id,
        task_id,
        revert_branch,
        commit_sha[:7],
    )

    return {
        "revert_task_id": new_task_id,
        "revert_pr_id": new_pr_id,
        "branch": revert_branch,
        "revert_commit_sha": revert_info["commit_sha"],
        "original_commit_sha": commit_sha,
    }


async def update_pr(
    task_id: str,
    db: aiosqlite.Connection,
    title: str | None = None,
    body: str | None = None,
) -> dict:
    """Update PR title/body for draft or open PR."""
    pr = await _get_active_pr(db, task_id)
    if not pr:
        raise ValueError(f"No active PR for task {task_id}")
    if pr["status"] not in ("draft", "open"):
        raise ValueError(
            f"PR must be draft or open to update (current: {pr['status']})"
        )

    updates = []
    params = []
    if title is not None:
        updates.append("title = ?")
        params.append(title)
    if body is not None:
        updates.append("body = ?")
        params.append(body)

    if not updates:
        return _pr_row_to_dict(pr)

    params.append(pr["id"])
    await db.execute(
        f"UPDATE pull_requests SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    await db.commit()

    # Re-fetch
    cursor = await db.execute("SELECT * FROM pull_requests WHERE id = ?", (pr["id"],))
    updated = await cursor.fetchone()
    return _pr_row_to_dict(updated)
