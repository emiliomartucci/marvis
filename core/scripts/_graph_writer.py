# v1.2.0 - 2026-04-14 - KG Fase 2: accept project_id in UPSERT helpers (backward-compat default 'marvisx')
# v1.1.0 - 2026-04-14 - KG Fase 1d: last_seen_at / first_seen_at touch on every UPSERT
"""Chunked UPSERT helpers for `graph_nodes` / `graph_edges`.

Refactored out of `scripts/ast_parser.py::_upsert_nodes_chunked` /
`_upsert_edges_chunked` so populator scripts (Fase 1c+) reuse the same
write pattern: BEGIN IMMEDIATE per chunk, ON CONFLICT DO UPDATE (NOT silent
OR IGNORE — we want to refresh metadata/confidence), explicit columns.

## Single-writer note

This module opens nothing — the caller passes a `sqlite3.Connection` already
configured (PRAGMA journal_mode=WAL, foreign_keys=ON, busy_timeout). The
populator scripts run **standalone** (batch/cron, NEVER inside the API
process) so they own their own connection and use BEGIN IMMEDIATE — the
single-writer contract is preserved because the API pool is read-only
(`PRAGMA query_only=ON`), so concurrent reads don't conflict with this
writer.

## Why ON CONFLICT DO UPDATE (not OR IGNORE)

Re-running a populator must refresh metadata (e.g. task status moves from
`pending` → `completed`). OR IGNORE would silently drop the new state and
leave the graph stale. UPSERT preserves the row id and updates `updated_at`.

## Metadata merge contract

For nodes: caller passes the canonical metadata for that run. The chunked
writer overwrites the JSON column entirely with `excluded.metadata`. If the
caller wants to merge with existing metadata (e.g. AST parser preserving
`stub: false` flag from a previous pass), it must do the merge in Python
before calling — see `scripts/ast_parser.py::_upsert_nodes_chunked` for the
canonical merge pattern. This module stays simple: replace, not merge.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

DEFAULT_NODE_BATCH = 500
DEFAULT_EDGE_BATCH = 2000

# Fase 2: default project for writers that don't pass one. Matches migration
# 073 backfill value so pre-Fase-2 writers (ast_parser, populate_touch_counter)
# remain correct without modification.
DEFAULT_PROJECT_ID = "marvisx"


def _chunked(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _normalize_node_metadata(meta: Any) -> str:
    """Coerce a node metadata payload to the canonical JSON string form."""
    if meta is None:
        return "{}"
    if isinstance(meta, str):
        # Already JSON-encoded — trust the caller.
        return meta
    # default=str: frontmatter parsed by pyyaml can carry datetime.date/datetime
    # objects (e.g. handoff `date:`/`session:`); without this json.dumps raises
    # TypeError and the node upsert is skipped, silently starving the KG of every
    # handoff with a date frontmatter (11-day indexing gap, 2026-05-27).
    return json.dumps(meta, sort_keys=True, separators=(",", ":"), default=str)


def chunked_upsert_nodes(
    conn: sqlite3.Connection,
    nodes: list[dict[str, Any]],
    batch_size: int = DEFAULT_NODE_BATCH,
) -> int:
    """Chunked UPSERT into `graph_nodes`.

    Each `nodes` dict requires keys: `id`, `type`, `name`, `qualified_name`.
    Optional: `file_path`, `line_number`, `metadata` (dict or JSON string).

    Returns the number of rows written (sum across chunks).
    """
    if not nodes:
        return 0

    total = 0
    for chunk in _chunked(nodes, batch_size):
        rows = [
            (
                n["id"],
                n["type"],
                n["name"],
                n["qualified_name"],
                n.get("file_path"),
                n.get("line_number"),
                _normalize_node_metadata(n.get("metadata")),
                n.get("project_id", DEFAULT_PROJECT_ID),
            )
            for n in chunk
        ]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                """
                INSERT INTO graph_nodes
                    (id, type, name, qualified_name, file_path, line_number,
                     metadata, last_seen_at, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                ON CONFLICT(id) DO UPDATE SET
                    type = excluded.type,
                    name = excluded.name,
                    qualified_name = excluded.qualified_name,
                    file_path = COALESCE(excluded.file_path, graph_nodes.file_path),
                    line_number = COALESCE(excluded.line_number, graph_nodes.line_number),
                    metadata = excluded.metadata,
                    last_seen_at = datetime('now'),
                    updated_at = datetime('now'),
                    project_id = COALESCE(graph_nodes.project_id, excluded.project_id)
                """,
                rows,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total += len(rows)
    return total


def chunked_upsert_edges(
    conn: sqlite3.Connection,
    edges: list[dict[str, Any]],
    batch_size: int = DEFAULT_EDGE_BATCH,
) -> int:
    """Chunked UPSERT into `graph_edges`.

    Each `edges` dict requires keys: `source_id`, `target_id`, `relation`.
    Optional: `confidence` (default 1.0), `source` (default 'db'),
    `metadata` (dict or JSON string), `source_file`, `source_line`.

    ON CONFLICT(source_id, target_id, relation) refreshes `source_file` /
    `source_line` (when non-null) and bumps `confidence` to MAX(old, new) so
    re-running with stronger evidence improves the score, never weakens it.

    Returns the number of rows written.
    """
    if not edges:
        return 0

    total = 0
    for chunk in _chunked(edges, batch_size):
        rows = [
            (
                e["source_id"],
                e["target_id"],
                e["relation"],
                float(e.get("confidence", 1.0)),
                e.get("source", "db"),
                _normalize_node_metadata(e.get("metadata")),
                e.get("source_file"),
                e.get("source_line"),
                e.get("project_id", DEFAULT_PROJECT_ID),
            )
            for e in chunk
        ]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                """
                INSERT INTO graph_edges
                    (source_id, target_id, relation, confidence, source,
                     metadata, source_file, source_line,
                     first_seen_at, last_seen_at, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?)
                ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                    confidence = MAX(graph_edges.confidence, excluded.confidence),
                    source = excluded.source,
                    metadata = excluded.metadata,
                    source_file = COALESCE(excluded.source_file, graph_edges.source_file),
                    source_line = COALESCE(excluded.source_line, graph_edges.source_line),
                    last_seen_at = datetime('now'),
                    project_id = COALESCE(graph_edges.project_id, excluded.project_id)
                """,
                rows,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total += len(rows)
    return total
