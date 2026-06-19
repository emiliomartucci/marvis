# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D2: DB writer for graph_edges + pr_function_touches
"""DB writer for the PR-impact populator.

Encapsulates the single-writer batch under one transaction:

- UPSERT function nodes that the populator just discovered
- UPSERT `defines` edges (file → function) so cold-start callers can
  traverse from a freshly-touched file
- UPSERT `modifies` edges (pr_artifact → function_artifact) with the
  `ModifiesEdgeMetadata` JSON blob
- INSERT `pr_function_touches` rows (idempotent on the natural key)

Designed to run from a subprocess (`scripts/populate_pr_impact.py`) using
the standard library `sqlite3` module — see the plan §8.6 single-writer
subprocess pattern. The pir-api process MUST NOT call this directly; it
dispatches a subprocess via `dispatcher.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass

from core.api.services.pr_impact_pipeline.differ import TouchedFunction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteContext:
    """Inputs the writer needs that aren't already on each TouchedFunction."""

    pr_id: str
    pr_node_id: str  # graph_nodes.id of the PR artifact node (pr:artifact:<uuid>)
    project_id: str | None
    commit_sha: str
    blame_author: str | None  # optional, written per-row downstream
    populator_version: str = "v1"


@dataclass(frozen=True)
class WriteResult:
    nodes_written: int = 0
    edges_written: int = 0
    touches_written: int = 0
    skipped: int = 0


def open_writer_connection(db_path: str) -> sqlite3.Connection:
    """Open the writer-mode sqlite connection used by the populator."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def synthesize_function_node_id(prefix: str, qualified_name: str) -> str:
    """Stable id for the function node — BLAKE2b 16-byte (mirror plan §8.1)."""
    digest = hashlib.blake2b(qualified_name.encode("utf-8"), digest_size=8).hexdigest()
    return f"{prefix}:function:{digest}"


def synthesize_file_node_id(prefix: str, path: str) -> str:
    """Stable id for the file node — same scheme as ast_parser.py file kind."""
    digest = hashlib.blake2b(path.encode("utf-8"), digest_size=6).hexdigest()
    return f"{prefix}:file:{digest}"


def write_touches(
    conn: sqlite3.Connection,
    *,
    context: WriteContext,
    touches: list[TouchedFunction],
    dry_run: bool = False,
) -> WriteResult:
    """UPSERT every TouchedFunction into the DB inside ONE transaction.

    When `dry_run=True` we run all the same statements inside a transaction
    that ROLLBACKs at the end — useful for testing + smoke runs.
    """
    if not touches:
        return WriteResult()

    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _execute_writes(conn, context=context, touches=touches)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if dry_run:
        conn.execute("ROLLBACK")
    else:
        conn.execute("COMMIT")
    return result


def _execute_writes(
    conn: sqlite3.Connection,
    *,
    context: WriteContext,
    touches: list[TouchedFunction],
) -> WriteResult:
    nodes_written = 0
    edges_written = 0
    touches_written = 0
    skipped = 0

    # PR artifact node must exist before we can hang edges off it. The
    # node is normally populated by `scripts/populate_artifacts.py`, but
    # we INSERT OR IGNORE here as well so the populator works against PRs
    # that haven't been swept yet (Brain v1 freshness gap).
    _ensure_pr_node(conn, context=context)
    nodes_written += 1

    for touch in touches:
        if touch.function is None:
            # Top-level changes have no function to attribute. We still
            # record a generic `modifies` edge against the file node so the
            # PR-impact lens shows "PR touched this file".
            skipped += 1
            file_node_id = _ensure_file_node(
                conn, path=touch.file_path, project_id=context.project_id
            )
            nodes_written += 1  # IF NOT EXISTS may noop, accept overcount
            _upsert_modifies_edge(
                conn,
                source_id=context.pr_node_id,
                target_id=file_node_id,
                touch=touch,
                context=context,
            )
            edges_written += 1
            continue

        from core.api.services.pr_impact_pipeline.languages import language_for_path

        spec = language_for_path(touch.file_path)
        if spec is None:
            skipped += 1
            continue

        fn_node_id = synthesize_function_node_id(
            spec.prefix, touch.function.qualified_name
        )
        file_node_id = _ensure_file_node(
            conn, path=touch.file_path, project_id=context.project_id, prefix=spec.prefix
        )
        nodes_written += 1
        _ensure_function_node(
            conn,
            node_id=fn_node_id,
            prefix=spec.prefix,
            function=touch.function,
            file_path=touch.file_path,
            project_id=context.project_id,
        )
        nodes_written += 1

        _upsert_defines_edge(
            conn,
            file_node_id=file_node_id,
            fn_node_id=fn_node_id,
            project_id=context.project_id,
        )
        edges_written += 1

        _upsert_modifies_edge(
            conn,
            source_id=context.pr_node_id,
            target_id=fn_node_id,
            touch=touch,
            context=context,
        )
        edges_written += 1

        _insert_pr_function_touch(
            conn,
            context=context,
            touch=touch,
            function_node_id=fn_node_id,
        )
        touches_written += 1

    return WriteResult(
        nodes_written=nodes_written,
        edges_written=edges_written,
        touches_written=touches_written,
        skipped=skipped,
    )


# --------------------------------------------------------------------------
# Node UPSERTs
# --------------------------------------------------------------------------


def _ensure_pr_node(conn: sqlite3.Connection, *, context: WriteContext) -> None:
    """INSERT OR IGNORE the PR artifact node so `modifies` edges can FK to it.

    The canonical writer for pr artifact nodes lives in
    `scripts/populate_artifacts.py`. We replicate the minimal shape here
    so the populator stays runnable even when artifact sweep is behind.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes (
            id, type, name, qualified_name, project_id
        ) VALUES (?, 'pr', ?, ?, ?)
        """,
        (
            context.pr_node_id,
            f"pr/{context.pr_id[:8]}",
            f"pr:{context.pr_id}",
            context.project_id,
        ),
    )


def _ensure_file_node(
    conn: sqlite3.Connection,
    *,
    path: str,
    project_id: str | None,
    prefix: str = "py",
) -> str:
    file_node_id = synthesize_file_node_id(prefix, path)
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes (id, type, name, qualified_name, file_path, project_id)
        VALUES (?, 'file', ?, ?, ?, ?)
        """,
        (file_node_id, path.rsplit("/", 1)[-1], path, path, project_id),
    )
    return file_node_id


def _ensure_function_node(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    prefix: str,
    function,
    file_path: str,
    project_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes (
            id, type, name, qualified_name, file_path, line_number, project_id
        ) VALUES (?, 'function', ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            function.qualified_name.rsplit(".", 1)[-1],
            function.qualified_name,
            file_path,
            function.line_start,
            project_id,
        ),
    )


# --------------------------------------------------------------------------
# Edge UPSERTs
# --------------------------------------------------------------------------


def _upsert_defines_edge(
    conn: sqlite3.Connection,
    *,
    file_node_id: str,
    fn_node_id: str,
    project_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO graph_edges (source_id, target_id, relation, source, project_id)
        VALUES (?, ?, 'defines', 'ast', ?)
        ON CONFLICT(source_id, target_id, relation) DO UPDATE
        SET last_touched_at = datetime('now')
        """,
        (file_node_id, fn_node_id, project_id),
    )


def _upsert_modifies_edge(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    target_id: str,
    touch: TouchedFunction,
    context: WriteContext,
) -> None:
    metadata = {
        "touch_kind": touch.touch_kind,
        "lines_added": touch.lines_added,
        "lines_removed": touch.lines_removed,
        "commit_sha": context.commit_sha,
        "blame_author": context.blame_author,
        "hunks": len(touch.hunks),
    }
    weight = _weight_for_touch(touch)
    conn.execute(
        """
        INSERT INTO graph_edges (
            source_id, target_id, relation, source, metadata,
            project_id, weight, last_touched_at
        ) VALUES (?, ?, 'modifies', 'git', ?, ?, ?, datetime('now'))
        ON CONFLICT(source_id, target_id, relation) DO UPDATE
        SET metadata = excluded.metadata,
            weight   = excluded.weight,
            last_touched_at = excluded.last_touched_at
        """,
        (
            source_id,
            target_id,
            json.dumps(metadata, sort_keys=True),
            context.project_id,
            weight,
        ),
    )


def _weight_for_touch(touch: TouchedFunction) -> float:
    """Scale weight 0.1-1.0 by how much of the function was touched.

    Pure heuristic for v1 — proper weighting requires diff-coverage ratio
    we don't have in shadow mode. Larger diffs get higher weights to drive
    the cosmo node-size scale in the frontend (sub-03).
    """
    total = touch.lines_added + touch.lines_removed
    if total <= 0:
        return 0.1
    if total >= 50:
        return 1.0
    return max(0.1, min(1.0, total / 50.0))


# --------------------------------------------------------------------------
# pr_function_touches INSERT
# --------------------------------------------------------------------------


def _insert_pr_function_touch(
    conn: sqlite3.Connection,
    *,
    context: WriteContext,
    touch: TouchedFunction,
    function_node_id: str | None,
) -> None:
    assert touch.function is not None  # caller already filtered
    line_start = touch.function.line_start
    line_end = touch.function.line_end
    conn.execute(
        """
        INSERT OR IGNORE INTO pr_function_touches (
            pr_id, function_id, qualified_name_snapshot, source_file,
            source_line_start, source_line_end,
            touched_lines, total_lines, weight,
            blame_author, blame_commit_sha,
            diff_added, diff_removed, project_id, populator_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            context.pr_id,
            function_node_id,
            touch.function.qualified_name,
            touch.file_path,
            line_start,
            line_end,
            touch.lines_added + touch.lines_removed,
            max(line_end - line_start + 1, 1),
            _weight_for_touch(touch),
            context.blame_author,
            context.commit_sha,
            touch.lines_added,
            touch.lines_removed,
            context.project_id,
            context.populator_version,
        ),
    )


__all__ = [
    "WriteContext",
    "WriteResult",
    "open_writer_connection",
    "synthesize_function_node_id",
    "synthesize_file_node_id",
    "write_touches",
]
