#!/usr/bin/env python3
# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D2: PR-impact populator CLI
"""Compute PR → function touch attributions and write them to the KG.

Invocation:

    python3 -m core.scripts.populate_pr_impact --pr-id <uuid> [--incremental]
        [--dry-run] [--db /data/pir/console.db] [--job-id <uuid>]
        [--repo ~/workspace]

Pipeline:

1. Pause the kg-watcher (sentinel) so concurrent indexing doesn't fight us
2. Resolve PR metadata (branch, base_sha, head_sha) from `pull_requests` row
3. For every changed file in `base..head`:
   a. Parse NEW (and OLD if needed) with tree-sitter
   b. Walk `git diff --unified=0` hunks and attribute each to a function
   c. `git blame` the function range for `blame_author` PII
4. Open a single-writer sqlite3 transaction and UPSERT:
   - graph_nodes: function + file nodes
   - graph_edges: `defines` (file→function) + `modifies` (pr→function)
   - pr_function_touches: one row per (pr, qualified_name, hunk_range)
5. Resume the kg-watcher
6. Exit 0 on success, 1 on any error

Designed to run as a subprocess from `api.services.pr_impact_pipeline.dispatcher`.
Stays additive in shadow mode: even when fully wired up, the API only
broadcasts `pr_changed` WebSocket events when `PR_IMPACT_ENABLED='on'`.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Allow `python3 scripts/populate_pr_impact.py` standalone.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.api.services.kg_watcher_control import (  # noqa: E402
    pause_watcher,
    resume_watcher,
)
from core.api.services.pr_impact_pipeline.differ import (  # noqa: E402
    attribute_hunks_to_functions,
    blame_email_for_range,
    file_content_at_revision,
    file_hunks,
    list_changed_files,
)
from core.api.services.pr_impact_pipeline.languages import language_for_path  # noqa: E402
from core.api.services.pr_impact_pipeline.writer import (  # noqa: E402
    WriteContext,
    open_writer_connection,
    write_touches,
)


logger = logging.getLogger("populate_pr_impact")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Populate KG PR-Impact edges for a single PR.",
    )
    parser.add_argument("--pr-id", required=True, help="PR task UUID (pull_requests.task_id)")
    parser.add_argument("--db", default="/data/pir/console.db", help="Path to console.db")
    parser.add_argument(
        "--repo",
        default=str(Path.home() / "workspace"),
        help="Path to the source repo (git diff is run here)",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="(reserved for D4) skip files whose hash is unchanged",
    )
    parser.add_argument("--dry-run", action="store_true", help="ROLLBACK after writes")
    parser.add_argument(
        "--job-id",
        default=None,
        help="Optional pr_impact_jobs.job_id for status reporting (dispatcher use)",
    )
    parser.add_argument("--pause-seconds", type=int, default=30, help="Watcher pause window")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    repo = Path(args.repo)
    if not (repo / ".git").exists():
        logger.error("repo path %s is not a git checkout", repo)
        return 1

    try:
        pause_watcher(duration_seconds=args.pause_seconds)
    except Exception as exc:  # noqa: BLE001 — pause failure should not block populator
        logger.warning("pause_watcher failed: %s — proceeding anyway", exc)

    try:
        return _run_populator(args)
    finally:
        try:
            resume_watcher()
        except Exception as exc:  # noqa: BLE001
            logger.warning("resume_watcher failed: %s", exc)


def _run_populator(args: argparse.Namespace) -> int:
    db_path = args.db
    pr_id = args.pr_id
    repo = Path(args.repo)

    pr_meta = _load_pr_metadata(db_path, pr_id)
    if pr_meta is None:
        logger.error("pr_id %s not found in pull_requests", pr_id)
        return 1

    # FK on pr_function_touches.pr_id references pull_requests.id (not task_id);
    # `pr_id` passed on the CLI may be either, so we normalize to the canonical id.
    pr_row_id = pr_meta["id"]
    project_id = pr_meta["project"]
    base_sha = pr_meta["base_sha"]
    head_sha = pr_meta["head_sha"]
    pr_node_id = pr_meta["pr_node_id"]

    if not base_sha or not head_sha:
        logger.warning(
            "pr_id %s missing base_sha (%s) or head_sha (%s) — shadow no-op",
            pr_id,
            base_sha,
            head_sha,
        )
        return 0  # acceptable in shadow mode: nothing to populate yet

    try:
        changes = list_changed_files(repo, base_sha, head_sha)
    except Exception as exc:  # noqa: BLE001 — log + bail
        logger.error("list_changed_files failed: %s", exc)
        return 1

    logger.info("pr_id=%s base=%s head=%s changes=%d", pr_id, base_sha[:8], head_sha[:8], len(changes))

    all_touches = []
    for change in changes:
        if change.status == "D":
            new_content = b""
            old_content = file_content_at_revision(repo, base_sha, change.old_path or change.path)
        else:
            try:
                new_content = file_content_at_revision(repo, head_sha, change.path)
            except Exception:  # noqa: BLE001
                new_content = b""
            old_content = file_content_at_revision(
                repo, base_sha, change.old_path or change.path
            )
        spec = language_for_path(change.path)
        if spec is None:
            continue
        try:
            hunks = file_hunks(
                repo, base_sha, head_sha, change.path, old_path=change.old_path
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hunks failed for %s: %s", change.path, exc)
            continue
        touches = attribute_hunks_to_functions(
            path=change.path,
            status=change.status,
            new_content=new_content,
            old_content=old_content,
            hunks=hunks,
            spec=spec,
        )
        all_touches.extend(touches)

    if not all_touches:
        logger.info("no touches detected for pr_id=%s — exiting clean", pr_id)
        return 0

    blame_author = _pick_blame_author(repo, head_sha, all_touches)

    conn = open_writer_connection(db_path)
    try:
        context = WriteContext(
            pr_id=pr_row_id,  # canonical pull_requests.id (FK target)
            pr_node_id=pr_node_id,
            project_id=project_id,
            commit_sha=head_sha,
            blame_author=blame_author,
        )
        result = write_touches(conn, context=context, touches=all_touches, dry_run=args.dry_run)
    finally:
        conn.close()

    logger.info(
        "pr_id=%s wrote nodes=%d edges=%d touches=%d skipped=%d (dry_run=%s)",
        pr_id,
        result.nodes_written,
        result.edges_written,
        result.touches_written,
        result.skipped,
        args.dry_run,
    )
    return 0


def _load_pr_metadata(db_path: str, pr_id: str) -> dict | None:
    """Read PR row + synthesize pr_node_id.

    We accept either `pr_id` matching `pull_requests.id` (canonical) or
    `pull_requests.task_id` (legacy callers). Returns None if not found.
    """
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, task_id, project, branch, target, commit_sha
              FROM pull_requests
             WHERE id=? OR task_id=?
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (pr_id, pr_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "project": row["project"],
        "head_sha": row["commit_sha"] or "",
        "base_sha": row["target"] or "main",
        "pr_node_id": f"pr:artifact:{row['task_id']}",
    }


def _pick_blame_author(repo: Path, head_sha: str, touches) -> str | None:
    """Find the most-attributed blame author across all touched function ranges."""
    counts: dict[str, int] = {}
    for touch in touches:
        if touch.function is None:
            continue
        email = blame_email_for_range(
            repo,
            head_sha,
            touch.file_path,
            touch.function.line_start,
            touch.function.line_end,
        )
        if email:
            counts[email] = counts.get(email, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


if __name__ == "__main__":
    sys.exit(main())
