# v1.0.0 - 2026-03-13 - CI/CD feedback loop: check tracking + merge gate + notifications
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlparse

import aiosqlite

logger = logging.getLogger(__name__)

# SSRF protection for details_url: only allow these domains
_ALLOWED_CI_DOMAINS = {"github.com", "api.github.com"}

@dataclass(frozen=True, slots=True)
class RequiredCheck:
    """A check that must pass, and the slice of the tree it actually covers.

    An empty ``paths`` means unconditional: the check is required for every
    change. Anything else is required only when the diff touches one of the
    patterns, mirroring the workflow's own ``paths:`` filter.
    """

    name: str
    paths: tuple[str, ...] = ()


# The hosted marvisx source lineage uses GitHub task-branch pushes for CI while
# keeping GitHub origin/main deliberately separate. This check is therefore a
# code-level floor, not optional tenant UI configuration: without it the hosted
# PR merge gate would silently treat an empty project_ci_config row as green.
#
# The scope below mirrors .github/workflows/openapi-diff.yml. A workflow with a
# `paths:` filter never runs for a diff that misses them, and a check that could
# not run is not the same thing as a check that failed. Without the distinction
# the gate demands a green it can never receive, and every PR outside the API
# surface is blocked forever — which is how this scoping came to exist.
_REQUIRED_CHECK_FLOOR: dict[str, tuple[RequiredCheck, ...]] = {
    "marvisx": (
        RequiredCheck(
            name="OpenAPI Diff",
            # Must stay identical to the `paths:` filter in
            # .github/workflows/openapi-diff.yml (both the pull_request and the
            # push trigger carry the same list). Hand-copying it here drifted
            # once already and silently waived a check GitHub does run, so
            # test_encoded_scope_matches_the_workflow pins the two together.
            paths=(
                "core/api/**",
                "contracts/openapi/**",
                "contracts/actions/**",
                "tests/contracts/**",
                "tests/test_ci_webhook_routing.py",
                "requirements-tenant.txt",
                "requirements-test.txt",
                ".github/workflows/openapi-diff.yml",
            ),
        ),
    ),
}


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile one GitHub-style path filter into a regex.

    ``**`` crosses directory separators; ``*`` and ``?`` do not. Matching the
    filter semantics GitHub itself applies is the whole point: the gate's idea
    of "does this check cover the diff" has to agree with what GitHub decided
    to run, or the two disagree and the gate blocks on a phantom.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _check_covers_diff(check: RequiredCheck, changed_paths: list[str] | None) -> bool:
    """True when this check must pass for this particular diff.

    Fail closed on ignorance. An unknown diff (``None``) or an empty one keeps
    the check required: an empty list is indistinguishable from a diff we failed
    to compute, and reading "I could not look" as "not applicable" would rebuild
    the exact false-green this scoping exists to remove.
    """
    if not check.paths:
        return True
    if not changed_paths:
        return True
    return any(
        _glob_regex(pattern).match(path)
        for pattern in check.paths
        for path in changed_paths
    )


def _parse_required_entry(entry: object) -> RequiredCheck | None:
    """One project_ci_config entry -> RequiredCheck, or None when unusable.

    Accepts both the historical plain-string form and the scoped object form
    ``{"name": ..., "paths": [...]}``. A malformed entry is dropped rather than
    raised: a broken config row must not take the merge gate down with it.
    """
    if isinstance(entry, str):
        name = entry.strip()
        return RequiredCheck(name=name) if name else None
    if isinstance(entry, dict):
        raw_name = entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None
        raw_paths = entry.get("paths")
        paths: tuple[str, ...] = ()
        if isinstance(raw_paths, list):
            paths = tuple(
                p.strip() for p in raw_paths if isinstance(p, str) and p.strip()
            )
        return RequiredCheck(name=raw_name.strip(), paths=paths)
    return None


def _completed_status(conclusion: object) -> str:
    """Map a completed GitHub run onto our ledger status.

    ``skipped`` is recorded as itself instead of being folded into ``failure``:
    a job the workflow deliberately did not run is not a broken build, and
    filing it as one both lies in the ledger and fires a false CI-failure
    notification. It still does not clear the gate — only ``success`` does.
    """
    if conclusion == "success":
        return "success"
    if conclusion == "skipped":
        return "skipped"
    return "failure"
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _validated_head_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _COMMIT_SHA_RE.fullmatch(normalized) else None


def verify_github_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    CRITICAL: Use raw body bytes, NOT re-serialized JSON (would break HMAC).
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _validate_details_url(url: str | None) -> str | None:
    """SSRF protection: only allow HTTPS URLs from known CI domains."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return None
        if parsed.hostname not in _ALLOWED_CI_DOMAINS:
            return None
        return url
    except Exception:
        return None


async def handle_github_ci_event(
    payload: dict,
    delivery_id: str,
    db: aiosqlite.Connection,
) -> dict:
    """Process GitHub check_run or workflow_run webhook event.

    Returns summary of what was processed.
    """
    # Dedup by delivery_id
    if delivery_id:
        async with db.execute(
            "SELECT id FROM ci_checks WHERE delivery_id = ?", (delivery_id,)
        ) as cursor:
            if await cursor.fetchone():
                return {"status": "duplicate", "delivery_id": delivery_id}

    # Handle check_run events (most common for GitHub Actions)
    check_run = payload.get("check_run")
    if check_run:
        return await _process_check_run(check_run, delivery_id, db)

    # Handle workflow_run events
    workflow_run = payload.get("workflow_run")
    if workflow_run:
        return await _process_workflow_run(workflow_run, delivery_id, db)

    return {"status": "ignored", "reason": "no check_run or workflow_run in payload"}


async def _process_check_run(
    check_run: dict,
    delivery_id: str,
    db: aiosqlite.Connection,
) -> dict:
    """Process a GitHub check_run event."""
    check_name = check_run.get("name", "unknown")
    conclusion = check_run.get("conclusion")  # success, failure, neutral, cancelled, etc.
    status_gh = check_run.get("status")  # queued, in_progress, completed

    # Map GitHub status to our status
    if status_gh == "completed":
        status = _completed_status(conclusion)
    elif status_gh == "in_progress":
        status = "running"
    else:
        status = "pending"

    details_url = _validate_details_url(check_run.get("details_url"))
    output_summary = None
    if check_run.get("output", {}).get("summary"):
        output_summary = check_run["output"]["summary"][:2000]  # truncate

    # Find task_id from PR — check_run.pull_requests[].head.ref contains branch name
    task_id = None
    for pr in check_run.get("pull_requests", []):
        branch = pr.get("head", {}).get("ref", "")
        # Extract task ID from branch name feat/task-{uuid}
        if "task-" in branch:
            task_id = branch.split("task-")[-1][:36]  # UUID length
            break

    if not task_id:
        return {"status": "ignored", "reason": "no task_id found in PR branches"}

    check_id = uuid.uuid4().hex[:32]
    now = datetime.now(timezone.utc).isoformat()
    head_sha = _validated_head_sha(check_run.get("head_sha"))

    await db.execute(
        """INSERT INTO ci_checks (id, task_id, check_name, status, details_url, output_summary,
           started_at, completed_at, delivery_id, created_at, head_sha)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id, check_name, attempt)
           DO UPDATE SET status = excluded.status, completed_at = excluded.completed_at,
                         output_summary = excluded.output_summary, details_url = excluded.details_url,
                         head_sha = excluded.head_sha""",
        (
            check_id, task_id, check_name, status, details_url, output_summary,
            check_run.get("started_at"), check_run.get("completed_at") if status_gh == "completed" else None,
            delivery_id, now, head_sha,
        ),
    )
    await db.commit()

    # Notify on failure
    if status == "failure":
        await _notify_ci_failure(task_id, check_name, 1, db)

    return {"status": "processed", "task_id": task_id, "check_name": check_name, "result": status}


async def _process_workflow_run(
    workflow_run: dict,
    delivery_id: str,
    db: aiosqlite.Connection,
) -> dict:
    """Process a GitHub workflow_run event."""
    check_name = workflow_run.get("name", "unknown")
    conclusion = workflow_run.get("conclusion")
    status_gh = workflow_run.get("status")

    if status_gh == "completed":
        status = _completed_status(conclusion)
    elif status_gh == "in_progress":
        status = "running"
    else:
        status = "pending"

    # Extract task_id from head_branch
    head_branch = workflow_run.get("head_branch", "")
    task_id = None
    if "task-" in head_branch:
        task_id = head_branch.split("task-")[-1][:36]

    if not task_id:
        return {"status": "ignored", "reason": "no task_id in head_branch"}

    details_url = _validate_details_url(workflow_run.get("html_url"))
    check_id = uuid.uuid4().hex[:32]
    now = datetime.now(timezone.utc).isoformat()
    head_sha = _validated_head_sha(workflow_run.get("head_sha"))

    await db.execute(
        """INSERT INTO ci_checks (id, task_id, check_name, status, details_url,
           started_at, completed_at, delivery_id, created_at, head_sha)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id, check_name, attempt)
           DO UPDATE SET status = excluded.status, completed_at = excluded.completed_at,
                         details_url = excluded.details_url, head_sha = excluded.head_sha""",
        (
            check_id, task_id, check_name, status, details_url,
            workflow_run.get("run_started_at"), workflow_run.get("updated_at") if status_gh == "completed" else None,
            delivery_id, now, head_sha,
        ),
    )
    await db.commit()

    if status == "failure":
        await _notify_ci_failure(task_id, check_name, 1, db)

    return {"status": "processed", "task_id": task_id, "check_name": check_name, "result": status}


async def _notify_ci_failure(
    task_id: str,
    check_name: str,
    attempt: int,
    db: aiosqlite.Connection,
) -> None:
    """Create notification on CI failure. Auto-retry deferred to v2."""
    try:
        # Find task owner
        async with db.execute(
            "SELECT owner_id, workspace_id FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            task = await cursor.fetchone()

        if not task or not task["owner_id"]:
            return

        from core.api.services.events import emit_event
        await emit_event(
            db,
            event_type="ci.failure",
            project=None,
            actor_id=None,
            target_type="task",
            target_id=task_id,
            payload={"check_name": check_name, "attempt": attempt},
            workspace_id=task["workspace_id"],
        )
    except Exception:
        logger.warning("Failed to notify CI failure for task %s", task_id, exc_info=True)


async def check_required_ci_passes(
    task_id: str,
    project: str | None,
    db: aiosqlite.Connection,
    *,
    head_sha: str | None = None,
    changed_paths: list[str] | None = None,
) -> list[str]:
    """Return list of required checks that haven't passed. Empty = all clear.

    Used by pr_service merge gate.

    ``changed_paths`` lets a check declare itself irrelevant to this diff, so a
    path-filtered workflow that GitHub never ran does not block a merge forever.
    Omitting it (or passing an empty list) keeps every required check in force:
    the gate only ever narrows on positive evidence about what changed.
    """
    if not project:
        return []

    # Get required checks for project
    async with db.execute(
        "SELECT required_checks FROM project_ci_config WHERE project = ?", (project,)
    ) as cursor:
        config_row = await cursor.fetchone()

    required: list[RequiredCheck] = list(_REQUIRED_CHECK_FLOOR.get(project, ()))
    seen = {check.name for check in required}
    if config_row and config_row["required_checks"]:
        try:
            configured = json.loads(config_row["required_checks"])
        except (json.JSONDecodeError, TypeError):
            configured = []
        if isinstance(configured, list):
            for entry in configured:
                parsed = _parse_required_entry(entry)
                if parsed is not None and parsed.name not in seen:
                    required.append(parsed)
                    seen.add(parsed.name)

    if not required:
        return []  # No required checks configured or mandated by source policy

    # Drop only the checks this diff provably cannot trigger. Everything else,
    # including anything we are unsure about, stays required.
    applicable = [
        check.name for check in required if _check_covers_diff(check, changed_paths)
    ]
    if not applicable:
        return []

    # Check which required checks haven't passed
    placeholders = ",".join("?" for _ in applicable)
    sha_clause = " AND head_sha = ?" if head_sha else ""
    params = [task_id, *applicable]
    if head_sha:
        params.append(head_sha)
    async with db.execute(
        f"""SELECT check_name FROM ci_checks
            WHERE task_id = ? AND check_name IN ({placeholders}) AND status = 'success'
            {sha_clause}
            GROUP BY check_name""",
        params,
    ) as cursor:
        passed_rows = await cursor.fetchall()

    passed = {r["check_name"] for r in passed_rows}
    return [name for name in applicable if name not in passed]
