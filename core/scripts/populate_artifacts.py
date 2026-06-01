#!/usr/bin/env python3
# v1.6.0 - 2026-04-21 - Anti-zombie B: auto-close doc/none tasks referenced in handoff frontmatter task_ids (trigger=handoff_written)
# v1.5.0 - 2026-04-16 - KG Phase 6.5 F: populate_handoffs accepts orphan handoffs (no task_id or task_id_not_in_graph) as nodes without describes edge
# v1.4.0 - 2026-04-16 - KG Phase 6: --all-projects flag + plan/brainstorm doc types (migration 077)
# v1.3.0 - 2026-04-15 - KG Phase 1: --incremental <paths> + --handle-delete + file_state hash gate (migration 074)
# v1.2.0 - 2026-04-14 - KG Fix: populate_handoffs legge valid_learning_ids dalla tabella learnings (era graph_nodes, che non e' ancora popolato quando parte populate_handoffs) + swap ordine main() (knowledge prima di handoffs) per soddisfare FK graph_edges.target_id -> edge cites handoff->learning ora viene effettivamente emesso
# v1.1.0 - 2026-04-14 - KG Fase 1h: extend populate_knowledge_docs to all docs/ subdirs (audit/spike/analysis/research/rubric/guide/mockup)
# v1.0.0 - 2026-04-14 - KG Fase 1c: populate task/PR/commit/handoff/solution/learning artifact nodes
"""Populate work + knowledge artifact nodes into the KG (Fase 1c).

Mirrors the standalone-script pattern of `scripts/ast_parser.py`:
- Opens its own `sqlite3.Connection` (BEGIN IMMEDIATE per chunk)
- Uses chunked UPSERT helpers from `scripts/_graph_writer.py`
- Reads project-yaml-level metadata (handoffs/solutions) from
  `/data/projects/marvisx` directly, learnings/tasks/PRs from the same
  SQLite DB it writes to (cross-project deferred to Fase 1g)

## Single-writer contract

This script is **standalone** — invoked manually or via cron, NEVER from the
API process. It uses BEGIN IMMEDIATE so concurrent reads from `pir-api.service`
(which run on a read-only pool with `PRAGMA query_only=ON`) don't block writes.
Two parallel populators would serialize on the SQLite writer lock — safe but
not parallelized; that's intentional.

## Hard cap

`MAX_TOTAL_NODES_PER_RUN` (default 5000) is a defense-in-depth ceiling. If a
single populate run would emit more than that many nodes the script aborts
**before** writing anything. This catches misconfiguration (e.g. accidentally
pointing at a 100k-handoff archive) before it explodes the graph.

## Functions

1. `populate_commits(conn, since_days=30, max_commits=1000)` — `commit:artifact:{sha7}`
2. `populate_tasks_and_prs(conn, project='marvisx')` — `task:artifact:{uuid}` + `pr:artifact:{pr_id}`,
   produces edge task→pr, contains edge pr→commit (if commit node exists)
3. `populate_handoffs(conn, metadata_path)` — `handoff:artifact:{slug}`,
   describes edge handoff→task (NEGATIVE CONTROL: skip if task_id missing or invalid)
4. `populate_knowledge_docs(conn, metadata_path, project='marvisx')` —
   `solution:artifact:{slug}` + `learning:artifact:{id}`,
   documents edge solution→learning (if frontmatter related_learning),
   cites edge handoff→learning (if frontmatter cites),
   applies_to edge learning→module (if metadata.module → matching module node)

## Invocation

    python -m core.scripts.populate_artifacts                    # auto-resolve DB
    python -m core.scripts.populate_artifacts --db /tmp/x.db
    python -m core.scripts.populate_artifacts --skip-commits     # test escape hatch
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from core.scripts._frontmatter import parse_frontmatter
from core.scripts._graph_writer import (
    chunked_upsert_edges,
    chunked_upsert_nodes,
)
from core.api.services.ingest.parsers.xlsx_parser import parse_xlsx

logger = logging.getLogger("populate_artifacts")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_METADATA_PATH = Path("/data/projects/marvisx")
DEFAULT_PROJECT = "marvisx"
PROJECTS_ROOT = Path("/data/projects")

MAX_TOTAL_NODES_PER_RUN = 5000  # data-integrity hard cap
DEFAULT_SINCE_DAYS = 30
DEFAULT_MAX_COMMITS = 1000

# ---- node id helpers --------------------------------------------------------

# NODE_ID slug allowed chars: matches the Fase 1c regex
# `^(py|ts|task|pr|commit|handoff|solution|learning|xlsx):
#   (function|file|module|artifact|sheet):[a-zA-Z0-9_\-.]+$`
SLUG_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_\-.]+")


def _safe_slug(s: str) -> str:
    """Coerce a free-form string to a NODE_ID-safe slug.

    Replaces forbidden characters with `_`, collapses runs, strips edges.
    Empty result is replaced with `_unknown`.
    """
    s = SLUG_SANITIZE_RE.sub("_", str(s).strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "_unknown"


def _task_id(uuid: str) -> str:
    return f"task:artifact:{_safe_slug(uuid)}"


def _pr_id(pr_id: str) -> str:
    return f"pr:artifact:{_safe_slug(pr_id)}"


def _commit_id(sha7: str) -> str:
    return f"commit:artifact:{_safe_slug(sha7[:7])}"


def _handoff_id(filename: str) -> str:
    # Strip extension + leading "handoff-" prefix for readability
    stem = filename
    if stem.endswith(".md"):
        stem = stem[:-3]
    if stem.startswith("handoff-"):
        stem = stem[len("handoff-") :]
    return f"handoff:artifact:{_safe_slug(stem)}"


def _solution_id(filename: str) -> str:
    return _doc_id("solution", filename)


def _doc_id(doc_type: str, filename: str) -> str:
    """Build a `{doc_type}:artifact:{slug}` node id from a docs/ markdown filename.

    Fase 1h: usato da populate_knowledge_docs per tutti i doc-type
    (solution/audit/spike/analysis/research/rubric/guide/mockup). Il caller
    garantisce che `doc_type` sia uno dei valori del DOC_TYPE_DIR_MAP (i.e.
    accettato dal CHECK constraint della migration 069).
    """
    stem = filename
    if stem.endswith(".md"):
        stem = stem[:-3]
    return f"{doc_type}:artifact:{_safe_slug(stem)}"


def _learning_id(learning_id: str) -> str:
    return f"learning:artifact:{_safe_slug(learning_id)}"


# Fase 1h: mappa doc-type → subdir di docs/. L'ordine delle chiavi NON e'
# significativo, ma le chiavi DEVONO restare sincronizzate con:
#   - migration 069 CHECK(type IN ...) + migration 077 (plan/brainstorm)
#     + migration 098 (policy/contract/transcript/report)
#   - NODE_ID_PATTERN (graph_service.py, ast_parser.py, mcp-pir/index.mjs)
# `learning` resta escluso perche' viene letto dal DB (tabella `learnings`),
# NON da filesystem.
# Phase 6 (2026-04-16): aggiunto plan→plans e brainstorm→brainstorms. Questi
# doc-type esistono da tempo nei /data/projects/<slug>/docs/plans/ e
# /data/projects/<slug>/docs/brainstorms/ ma non erano indicizzati dal KG.
# Phase 1.5 E5-fix9 (2026-05-08): policy/contract/transcript/report erano gia'
# permessi da DB/runtime, ma mancavano nel populator filesystem.
# Migration 125: record copre documenti fattuali/amministrativi generici.
DOC_TYPE_DIR_MAP: dict[str, str] = {
    "solution": "solutions",
    "audit": "audits",
    "spike": "spikes",
    "analysis": "analysis",
    "research": "research",
    "rubric": "rubrics",
    "guide": "guides",
    "mockup": "mockups",
    # Phase 6 additions (2026-04-16, migration 077):
    "plan": "plans",
    "brainstorm": "brainstorms",
    # Business doc taxonomy (2026-05-08, migration 098):
    "policy": "policies",
    "contract": "contracts",
    "transcript": "transcripts",
    "record": "records",
    "report": "reports",
}


def _extract_doc_type(metadata: dict[str, Any], dir_name: str) -> str:
    """Decide il doc-type prioritizzando il frontmatter `type:` sul dir name.

    Regole:
      1. Se `metadata["type"]` e' presente ed e' una chiave di DOC_TYPE_DIR_MAP,
         vince (es. un doc in docs/spikes/ con `type: research` diventa research).
      2. Altrimenti, se `dir_name` matcha un valore di DOC_TYPE_DIR_MAP, usa
         la chiave corrispondente (es. dir `audits` → type `audit`).
      3. Fallback: `solution` (safe default che preserva il comportamento 1c
         per file con frontmatter ambiguo).
    """
    explicit = str(metadata.get("type") or "").strip().lower()
    if explicit in DOC_TYPE_DIR_MAP:
        return explicit
    for type_name, dir_n in DOC_TYPE_DIR_MAP.items():
        if dir_name == dir_n:
            return type_name
    return "solution"


def _module_id(module_qn: str) -> str:
    """Build the module node id used by the AST parser (Fase 1a)."""
    qn = module_qn.strip().lower()
    return f"py:module:{_safe_slug(qn)}"


def _xlsx_workbook_id(sha256: str) -> str:
    return f"xlsx:artifact:{_safe_slug(sha256)}"


def _xlsx_sheet_id(sha256: str, sheet_index: int, sheet_name: str) -> str:
    sheet_slug = _safe_slug(sheet_name)[:80]
    return f"xlsx:sheet:{_safe_slug(sha256)}.{sheet_index:03d}.{sheet_slug}"


# ---- 1. populate_commits ----------------------------------------------------


def populate_commits(
    conn: sqlite3.Connection,
    since_days: int = DEFAULT_SINCE_DAYS,
    max_commits: int = DEFAULT_MAX_COMMITS,
    repo_root: Path | None = None,
) -> dict[str, int]:
    """Read recent commits via `git log` (no gitpython — keep deps minimal).

    Args:
        conn: open sqlite3 connection
        since_days: window for git log --since
        max_commits: hard cap (data-integrity amendment)
        repo_root: defaults to REPO_ROOT

    Returns: `{n_nodes: int, n_edges: int}`
    """
    root = repo_root or REPO_ROOT
    try:
        out = subprocess.check_output(
            [
                "git",
                "log",
                f"--since={since_days}.days",
                f"--max-count={max_commits}",
                "--format=%H%x09%ct%x09%an%x09%s",
            ],
            cwd=root,
            text=True,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("git log failed: %s — skipping commits", e)
        return {"n_nodes": 0, "n_edges": 0}

    nodes: list[dict[str, Any]] = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        full_sha, ts, author, message = parts
        short = full_sha[:7]
        nodes.append({
            "id": _commit_id(short),
            "type": "commit",
            "name": short,
            "qualified_name": full_sha,
            "file_path": None,
            "line_number": None,
            "metadata": {
                "full_sha": full_sha,
                "author": author,
                "timestamp": int(ts),
                "message": message[:500],  # cap to avoid huge json blobs
                "source": "git",
            },
        })

    # NO touches edges — deferred to Fase 1d (need git blame, costly).
    n_nodes = chunked_upsert_nodes(conn, nodes)
    return {"n_nodes": n_nodes, "n_edges": 0}


# ---- 2. populate_tasks_and_prs ---------------------------------------------


def populate_tasks_and_prs(
    conn: sqlite3.Connection,
    project: str = DEFAULT_PROJECT,
) -> dict[str, int]:
    """Read tasks + pull_requests for `project` from the same DB and emit
    nodes + `produces` (task→pr) + `contains` (pr→commit) edges.

    contains edges only emitted when the corresponding `commit:artifact:{sha7}`
    node already exists (i.e. populate_commits ran first and the PR's branch
    name embeds a sha — in practice we use the merged_at + commit metadata
    correlation that's available on `pull_requests.title` containing the SHA
    if the PR was squash-merged. For Fase 1c we skip pr→commit unless an
    explicit `merge_commit_sha` field exists, since it's not currently
    persisted on `pull_requests`. NOOP today, infrastructure ready for 1d.).
    """
    cur = conn.cursor()

    # tasks
    cur.execute(
        """
        SELECT id, title, status, priority, source, created_at, updated_at
          FROM tasks
         WHERE project = ? AND deleted_at IS NULL
        """,
        (project,),
    )
    task_rows = cur.fetchall()

    nodes: list[dict[str, Any]] = []
    for r in task_rows:
        tid, title, status, priority, source, created_at, updated_at = r
        nodes.append({
            "id": _task_id(tid),
            "type": "task",
            "name": tid[:8],
            "qualified_name": f"task.{tid}",
            "file_path": None,
            "line_number": None,
            "metadata": {
                "uuid": tid,
                "title": (title or "")[:300],
                "status": status,
                "priority": priority,
                "source": source,
                "created_at": created_at,
                "updated_at": updated_at,
                "project": project,
            },
        })

    # pull_requests
    cur.execute(
        """
        SELECT id, task_id, branch, status, title, merged_at, created_at
          FROM pull_requests
         WHERE project = ?
        """,
        (project,),
    )
    pr_rows = cur.fetchall()

    edges: list[dict[str, Any]] = []
    existing_task_ids = {t[0] for t in task_rows}

    for r in pr_rows:
        pr_id, task_id, branch, status, title, merged_at, created_at = r
        nodes.append({
            "id": _pr_id(pr_id),
            "type": "pr",
            "name": pr_id[:8],
            "qualified_name": f"pr.{pr_id}",
            "file_path": None,
            "line_number": None,
            "metadata": {
                "pr_id": pr_id,
                "task_id": task_id,
                "branch": branch,
                "status": status,
                "title": (title or "")[:300],
                "merged_at": merged_at,
                "created_at": created_at,
                "project": project,
            },
        })

        # produces edge: task → pr (only if the task was indexed in this run)
        if task_id in existing_task_ids:
            edges.append({
                "source_id": _task_id(task_id),
                "target_id": _pr_id(pr_id),
                "relation": "produces",
                "confidence": 1.0,
                "source": "db",
                "metadata": {"branch": branch},
            })

    n_nodes = chunked_upsert_nodes(conn, nodes)
    n_edges = chunked_upsert_edges(conn, edges)
    return {"n_nodes": n_nodes, "n_edges": n_edges}


# ---- Anti-zombie B (task e103b1ed) -----------------------------------------
#
# When a handoff is indexed, parse its frontmatter `task_ids: [...]` (or
# single `task_id:`) and auto-complete any referenced task whose
# completion_mode is 'doc' or 'none'. Complements anti-zombie A (PR body
# parser, commit ed03cf6) which already closes PR-mode siblings via the
# `handoff_written` trigger (already in validate_and_transition_task
# allowlist after PR #? merged ed03cf6).
#
# Guards:
#   - Skip if task not found (LEFT JOIN null).
#   - Skip if task.project != handoff project.
#   - Skip if completion_mode == 'pr' (handled by A).
#   - Skip if status not in (approved, in_progress).
#   - Idempotent: completed/rejected → no-op silenzioso.
#   - approved → in_progress → completed (bridge: approved → completed is
#     not a legal one-step VALID_TRANSITIONS hop).

# UUIDv4-ish canonical lowercase: 8-4-4-4-12 hex.
_UUID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
)


def _extract_handoff_task_ids(data: dict[str, Any]) -> list[str]:
    """Extract task_ids from handoff frontmatter.

    Tolerates both `task_ids: [uuid, ...]` (list) and `task_id: uuid`
    (single). Returns canonical-lowercased UUIDs only (non-UUID strings
    are silently dropped — no ambiguous prefix expansion here, unlike the
    PR body parser which has a richer regex surface).
    """
    raw_ids: list[Any] = []
    ids_list = data.get("task_ids")
    if isinstance(ids_list, list):
        raw_ids.extend(ids_list)
    single = data.get("task_id")
    if single:
        raw_ids.append(single)

    out: list[str] = []
    seen: set[str] = set()
    for rid in raw_ids:
        if not isinstance(rid, str):
            continue
        norm = rid.strip().lower()
        if not _UUID_RE.match(norm):
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _auto_close_handoff_referenced_tasks(
    conn: sqlite3.Connection,
    task_ids: list[str],
    handoff_project: str,
    handoff_name: str,
) -> list[str]:
    """Auto-complete doc/none tasks referenced by a handoff frontmatter.

    Sync mirror of `api.services.task_transitions.validate_and_transition_task`
    essentials. populate_artifacts runs under `sqlite3.Connection` (not
    aiosqlite) so we inline the guard logic rather than hopping to asyncio.
    Contract stays aligned: same guards, same trigger name (`handoff_written`,
    already in the allowlist after anti-zombie A merged).

    Returns list of task_ids actually transitioned to `completed` (for
    structured logging by the caller).
    """
    if not task_ids:
        return []

    closed: list[str] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    for uuid_norm in task_ids:
        try:
            cursor = conn.execute(
                "SELECT id, status, project, completion_mode "
                "FROM tasks WHERE id = ? AND deleted_at IS NULL",
                (uuid_norm,),
            )
            row = cursor.fetchone()
            if row is None:
                continue
            # Positional (no row_factory on populate_artifacts' conn).
            _tid, status, project_col, completion_mode = row
            completion_mode = completion_mode or "pr"
            if project_col != handoff_project:
                continue
            if completion_mode == "pr":
                # Anti-zombie A territory (PR body parser). Leave alone.
                continue
            if status not in ("approved", "in_progress"):
                # Already completed/rejected/review/failed/pending — no-op.
                continue

            # Guard: refuse close while open PR exists. Research/doc tasks
            # shouldn't carry one, but a stray draft would break the
            # invariant. Mirror the async path.
            pr_cursor = conn.execute(
                "SELECT id FROM pull_requests "
                "WHERE task_id = ? AND status IN ('draft', 'open', 'merging') LIMIT 1",
                (uuid_norm,),
            )
            if pr_cursor.fetchone() is not None:
                logger.info(
                    "handoff closure skip: task %s has open PR",
                    uuid_norm[:8],
                )
                continue

            # approved → in_progress → completed (bridge).
            if status == "approved":
                conn.execute(
                    "UPDATE tasks SET status = 'in_progress', updated_at = ? "
                    "WHERE id = ?",
                    (now, uuid_norm),
                )
            conn.execute(
                "UPDATE tasks SET status = 'completed', updated_at = ? "
                "WHERE id = ?",
                (now, uuid_norm),
            )
            conn.commit()
            closed.append(uuid_norm)
        except Exception as exc:
            # Best-effort: a single failure must not abort the sweep.
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(
                "handoff %s: closure failed for task %s: %s",
                handoff_name, uuid_norm[:8], exc,
            )

    return closed


# ---- 3. populate_handoffs ---------------------------------------------------


def populate_handoffs(
    conn: sqlite3.Connection,
    metadata_path: Path | None = None,
    project: str = DEFAULT_PROJECT,
) -> dict[str, int]:
    """Scan `{metadata_path}/memory/handoff-*.md`, parse frontmatter,
    emit handoff nodes always, + describes edges to task when task_id valid.

    Phase 6.5 F (2026-04-16): orphan handoffs are INDEXED as nodes (no
    describes edge). Previously we skipped them; this starved cross-project
    coverage (344 handoff missed in sessione 141 probe). Orphan-ness is
    NOT persisted on the node (derivable via LEFT JOIN graph_edges WHERE
    relation='describes' AND e.id IS NULL). The stderr log includes an
    ``orphan_reason`` per skipped-edge to aid triage.

    `cites` edges (handoff → learning) are emitted when frontmatter has
    `cites` list of learning ids.
    """
    base = metadata_path or DEFAULT_METADATA_PATH
    memory_dir = Path(base) / "memory"
    if not memory_dir.exists():
        logger.warning("memory dir missing: %s — skipping handoffs", memory_dir)
        return {"n_nodes": 0, "n_edges": 0, "n_skipped": 0}

    # Build set of valid task ids for FK check (from the in-graph task nodes)
    valid_task_ids = {
        row[0]
        for row in conn.execute(
            "SELECT REPLACE(id, 'task:artifact:', '') FROM graph_nodes WHERE type = 'task'"
        ).fetchall()
    }
    # Anti-ordering fix: leggere direttamente dalla tabella learnings invece
    # che da graph_nodes. populate_knowledge_docs crea i learning nodes DOPO
    # populate_handoffs, quindi un check su graph_nodes fallisce sempre e
    # nessun edge `cites` verrebbe mai emesso. La tabella learnings e' la
    # sorgente di verita' e non dipende dall'ordine dei populator.
    # Nota: la tabella `learnings` non ha una colonna `deleted_at`
    # (vedi migration 028), quindi filtriamo solo per project.
    valid_learning_ids = {
        row[0]
        for row in conn.execute(
            "SELECT id FROM learnings WHERE project = ?",
            (project,),
        ).fetchall()
    }

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    orphan_edge_log: list[dict[str, str]] = []
    # Anti-zombie B: collect (handoff_name, [uuid, ...]) for post-upsert sweep.
    closure_candidates: list[tuple[str, list[str]]] = []

    for f in sorted(memory_dir.glob("handoff-*.md")):
        if f.is_symlink():
            continue
        data, _body = parse_frontmatter(f)
        if data is None:
            # Still skipped — no frontmatter means no reliable metadata to index.
            skipped.append({"file": f.name, "reason": "no_frontmatter"})
            continue

        # Anti-zombie B: accumulate ALL referenced task_ids (list form) for
        # closure, independent of the single-task describes edge below.
        referenced_ids = _extract_handoff_task_ids(data)
        if referenced_ids:
            closure_candidates.append((f.name, referenced_ids))

        task_id = data.get("task_id")
        # Fallback: older handoffs may use `task_ids: [list]` — take first.
        if not task_id:
            task_ids_list = data.get("task_ids")
            if isinstance(task_ids_list, list) and task_ids_list:
                task_id = str(task_ids_list[0])

        # Phase 6.5 F: classify orphan cause for triage log (NOT persisted on node).
        orphan_reason: str | None = None
        task_id_resolved: str | None = None
        if not task_id:
            orphan_reason = "missing_task_id"
        else:
            task_id_str = str(task_id)
            if task_id_str not in valid_task_ids:
                orphan_reason = "task_id_not_in_graph"
            else:
                task_id_resolved = task_id_str

        if orphan_reason:
            orphan_edge_log.append({
                "file": f.name,
                "orphan_reason": orphan_reason,
                **({"task_id": str(task_id)} if task_id else {}),
            })

        node_id = _handoff_id(f.name)
        node = {
            "id": node_id,
            "type": "handoff",
            "name": f.stem,
            "qualified_name": f"handoff.{f.stem}",
            "file_path": str(f.relative_to(Path(base).parent.parent))
            if str(f).startswith(str(base.parent.parent))
            else str(f),
            "line_number": None,
            "metadata": {
                "filename": f.name,
                "date": str(data.get("date") or ""),
                "title": str(data.get("title") or "")[:300],
                "session": data.get("session"),
                "tags": data.get("tags") or [],
                "status": data.get("status"),
                # task_id preserved if present in frontmatter, even if not in graph.
                # Orphan-ness itself is NOT persisted here (derive via LEFT JOIN).
                "task_id": task_id_resolved or (str(task_id) if task_id else None),
            },
            # Phase 6: project_id esplicito per supportare --all-projects.
            "project_id": project,
        }
        nodes.append(node)

        # Emit describes edge ONLY if task_id resolves to an in-graph task node.
        if task_id_resolved:
            edges.append({
                "source_id": node_id,
                "target_id": _task_id(task_id_resolved),
                "relation": "describes",
                "confidence": 1.0,
                "source": "frontmatter",
                "source_file": str(f.relative_to(Path(base).parent.parent))
                if str(f).startswith(str(base.parent.parent))
                else str(f),
                "project_id": project,
            })

        # cites edges → learning (if frontmatter has cites list) — always allowed,
        # independent of task_id presence.
        cites = data.get("cites") or []
        if isinstance(cites, list):
            for cited in cites:
                cited_str = str(cited)
                if cited_str in valid_learning_ids:
                    edges.append({
                        "source_id": node_id,
                        "target_id": _learning_id(cited_str),
                        "relation": "cites",
                        "confidence": 1.0,
                        "source": "frontmatter",
                        "project_id": project,
                    })

    if skipped:
        logger.info(
            "handoffs skipped (no frontmatter): %d files — first 5: %s",
            len(skipped),
            skipped[:5],
        )
    if orphan_edge_log:
        logger.info(
            "handoffs indexed as orphan (no describes edge): %d — first 5: %s",
            len(orphan_edge_log),
            orphan_edge_log[:5],
        )

    # Phase 6.5 F: data-integrity — nodes + describes edges share a single
    # transaction boundary. _graph_writer chunked_upsert_* each wrap a
    # BEGIN IMMEDIATE / COMMIT; commit the first only if the second succeeds.
    try:
        n_nodes = chunked_upsert_nodes(conn, nodes)
        n_edges = chunked_upsert_edges(conn, edges)
    except Exception:
        # Best-effort rollback if the edge writer failed after nodes committed.
        # chunked_upsert_* manage their own txn, so we log and re-raise — a
        # full rebuild fixes dangling nodes without edges (additive state).
        logger.exception("populate_handoffs: upsert failed, partial state possible")
        raise

    # Anti-zombie B (task e103b1ed): after handoff nodes landed, sweep
    # referenced doc/none tasks and auto-close them. Best-effort: a failure
    # must never regress the populate run.
    n_closed_tasks = 0
    try:
        for handoff_name, ref_ids in closure_candidates:
            closed = _auto_close_handoff_referenced_tasks(
                conn, ref_ids, handoff_project=project, handoff_name=handoff_name,
            )
            if closed:
                logger.info(
                    "handoff %s: auto-closed %d referenced task(s) "
                    "(trigger=handoff_written): %s",
                    handoff_name, len(closed),
                    ", ".join(t[:8] for t in closed),
                )
                n_closed_tasks += len(closed)
    except Exception:
        logger.exception(
            "populate_handoffs: closure sweep failed (non-fatal)"
        )

    return {
        "n_nodes": n_nodes,
        "n_edges": n_edges,
        "n_skipped": len(skipped),
        "n_orphan": len(orphan_edge_log),
        "n_closed_tasks": n_closed_tasks,
    }


# ---- 4. populate_knowledge_docs --------------------------------------------


def populate_knowledge_docs(
    conn: sqlite3.Connection,
    metadata_path: Path | None = None,
    project: str = DEFAULT_PROJECT,
) -> dict[str, int]:
    """Merge docs/ (filesystem) + learnings (DB) populator.

    - Scan ogni subdir elencata in DOC_TYPE_DIR_MAP sotto `{metadata_path}/docs/`
      (Fase 1h: solutions, audits, spikes, analysis, research, rubrics,
      guides, mockups). Per ciascun *.md con frontmatter, emette
      `{type}:artifact:{slug}` con type dedotto dal frontmatter `type:` o, in
      assenza, dal nome della subdir. Se il frontmatter contiene
      `related_learning` / `learning_id` e la learning esiste, emette
      `documents` edge `{type}:artifact → learning:artifact`.

    - SELECT from `learnings` WHERE project = `project`. Emit
      `learning:artifact:{id}` nodes. If `module` field maps to an
      existing `py:module:*` node (from Fase 1a AST), emit `applies_to`
      edge learning→module.
    """
    base = metadata_path or DEFAULT_METADATA_PATH

    # Existing module nodes (built by AST parser Fase 1a)
    existing_modules = {
        row[0]
        for row in conn.execute(
            "SELECT id FROM graph_nodes WHERE type = 'module'"
        ).fetchall()
    }

    # ---- learnings (DB read) ----
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, category, severity, module, tags, project,
               created_at, updated_at
          FROM learnings
         WHERE project = ?
        """,
        (project,),
    )
    learning_rows = cur.fetchall()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    valid_learning_ids: set[str] = set()

    for r in learning_rows:
        lid, title, category, severity, module, tags_json, project_slug, created_at, updated_at = r
        valid_learning_ids.add(lid)
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (TypeError, ValueError):
            tags = []
        nodes.append({
            "id": _learning_id(lid),
            "type": "learning",
            "name": lid[:8],
            "qualified_name": f"learning.{lid}",
            "file_path": None,
            "line_number": None,
            "metadata": {
                "learning_id": lid,
                "title": (title or "")[:300],
                "category": category,
                "severity": severity,
                "module": module,
                "tags": tags,
                "project": project_slug,
                "created_at": created_at,
                "updated_at": updated_at,
            },
            # Phase 6: project_id esplicito per supportare --all-projects.
            "project_id": project,
        })

        # applies_to edge: learning → module (if module path matches an existing
        # py:module node). The learning.module field is free-text so we
        # normalize and try a direct match.
        if module:
            # learning.module can be like "api/db.py" or "api.db" — normalize both
            mod_dotted = module.replace("/", ".").rstrip(".")
            if mod_dotted.endswith(".py"):
                mod_dotted = mod_dotted[:-3]
            candidate = _module_id(mod_dotted)
            if candidate in existing_modules:
                edges.append({
                    "source_id": _learning_id(lid),
                    "target_id": candidate,
                    "relation": "applies_to",
                    "confidence": 1.0,
                    "source": "db",
                    "metadata": {"raw_module": module},
                    "project_id": project,
                })

    # ---- docs (filesystem read, Fase 1h: tutte le subdir di DOC_TYPE_DIR_MAP) ----
    #
    # Per ogni subdir previsto (solutions/audits/spikes/analysis/research/
    # rubrics/guides/mockups) leggiamo *.md con frontmatter. Il type del nodo
    # e' deciso da `_extract_doc_type`: priorita' al frontmatter `type:`
    # esplicito, fallback al nome della subdir. Questo permette a un doc in
    # docs/spikes/ con `type: research` di essere indicizzato come
    # `research:artifact:...` (caso raro ma supportato).
    docs_root = Path(base) / "docs"
    per_type_counts: dict[str, int] = {t: 0 for t in DOC_TYPE_DIR_MAP}

    if docs_root.exists():
        for doc_type, dir_name in DOC_TYPE_DIR_MAP.items():
            subdir = docs_root / dir_name
            if not subdir.exists():
                continue
            for f in sorted(subdir.rglob("*.md")):
                if f.is_symlink():
                    continue
                data, _body = parse_frontmatter(f)
                if data is None:
                    continue
                resolved_type = _extract_doc_type(data, dir_name)
                sid = _doc_id(resolved_type, f.name)
                per_type_counts[resolved_type] = per_type_counts.get(resolved_type, 0) + 1
                nodes.append({
                    "id": sid,
                    "type": resolved_type,
                    "name": f.stem[:60],
                    "qualified_name": f"{resolved_type}.{f.stem}",
                    "file_path": str(f.relative_to(Path(base).parent.parent))
                    if str(f).startswith(str(base.parent.parent))
                    else str(f),
                    "line_number": None,
                    "metadata": {
                        "filename": f.name,
                        "date": str(data.get("date") or ""),
                        "title": str(data.get("title") or "")[:300],
                        "category": data.get("category"),
                        "severity": data.get("severity"),
                        "tags": data.get("tags") or [],
                        "subdir": dir_name,
                    },
                    # Phase 6: project_id esplicito per --all-projects.
                    "project_id": project,
                })

                # documents edge: doc → learning (quando il frontmatter cita
                # una learning esistente). La relazione resta `documents`
                # anche per audit/spike/... — il CHECK di graph_edges la
                # accetta gia' dal 066 e semanticamente regge ("questo doc
                # documenta quella learning").
                related = data.get("related_learning") or data.get("learning_id")
                if related:
                    related_str = str(related)
                    if related_str in valid_learning_ids:
                        edges.append({
                            "source_id": sid,
                            "target_id": _learning_id(related_str),
                            "relation": "documents",
                            "confidence": 1.0,
                            "source": "frontmatter",
                            "project_id": project,
                        })

    n_nodes = chunked_upsert_nodes(conn, nodes)
    n_edges = chunked_upsert_edges(conn, edges)
    return {
        "n_nodes": n_nodes,
        "n_edges": n_edges,
        "per_type": per_type_counts,
    }


# ---- orchestration ---------------------------------------------------------


def _resolve_db_path(explicit: str | None = None) -> str:
    """Mirror of `core.scripts.ast_parser._resolve_db_path`."""
    if explicit:
        return explicit
    prod = Path("/data/pir/console.db")
    if prod.exists():
        return str(prod)
    return str(REPO_ROOT / "console.db")


def populate_artifacts(
    db_path: str | None = None,
    project: str = DEFAULT_PROJECT,
    metadata_path: Path | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    max_commits: int = DEFAULT_MAX_COMMITS,
    skip_commits: bool = False,
    skip_tasks_prs: bool = False,
    skip_handoffs: bool = False,
    skip_knowledge: bool = False,
    skip_xlsx: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Run all four populators in dependency order. Returns measurements."""
    db = _resolve_db_path(db_path)
    base = metadata_path or DEFAULT_METADATA_PATH

    t0 = time.perf_counter()
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")

        results: dict[str, dict[str, int]] = {}

        # 1. commits (no deps)
        if not skip_commits:
            results["commits"] = populate_commits(
                conn, since_days=since_days, max_commits=max_commits,
                repo_root=repo_root,
            )
        else:
            results["commits"] = {"n_nodes": 0, "n_edges": 0}

        # 2. tasks + prs (depends on commits for contains edges, NOOP for now)
        if not skip_tasks_prs:
            results["tasks_prs"] = populate_tasks_and_prs(conn, project=project)
        else:
            results["tasks_prs"] = {"n_nodes": 0, "n_edges": 0}

        # 3. knowledge docs PRIMA di handoffs: populate_knowledge_docs crea i
        # learning nodes (letti dalla tabella `learnings`), che sono target dei
        # `cites` edges emessi da populate_handoffs. Se invertiamo l'ordine, il
        # FK graph_edges.target_id -> graph_nodes.id fallisce.
        # Difesa in profondita': populate_handoffs carica comunque
        # valid_learning_ids direttamente da `learnings` (non da graph_nodes),
        # quindi rimane robusto anche se ordering cambia in futuro.
        if not skip_knowledge:
            results["knowledge"] = populate_knowledge_docs(
                conn, metadata_path=base, project=project
            )
        else:
            results["knowledge"] = {"n_nodes": 0, "n_edges": 0}

        # 4. XLSX binary artifacts (independent: workbook parent + sheet children).
        if not skip_xlsx:
            results["xlsx"] = populate_xlsx_artifacts(
                conn, metadata_path=base, project=project
            )
        else:
            results["xlsx"] = {"n_nodes": 0, "n_edges": 0, "n_skipped": 0}

        # 5. handoffs (depends on tasks for FK + describes edges, E su
        # learning nodes per i `cites` edges -> deve girare dopo knowledge).
        # `project` e' richiesto per caricare valid_learning_ids direttamente
        # dalla tabella learnings (vedi v1.2.0 anti-ordering fix).
        if not skip_handoffs:
            results["handoffs"] = populate_handoffs(
                conn, metadata_path=base, project=project
            )
        else:
            results["handoffs"] = {"n_nodes": 0, "n_edges": 0, "n_skipped": 0}

        # Hard cap (data-integrity)
        total_nodes = sum(r.get("n_nodes", 0) for r in results.values())
        if total_nodes > MAX_TOTAL_NODES_PER_RUN:
            logger.warning(
                "populated %d artifact nodes (> hard cap %d) — re-evaluate scope",
                total_nodes,
                MAX_TOTAL_NODES_PER_RUN,
            )

    finally:
        conn.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "db_path": db,
        "project": project,
        "metadata_path": str(base),
        "elapsed_ms": round(elapsed_ms, 2),
        "results": results,
    }


# ---- Phase 6 cross-project artifact coverage --------------------------------
#
# Phase 6 (plan docs/solutions/2026-04-16-kg-phase5-verification.md): estendere
# populate_artifacts per indicizzare handoff + knowledge docs di tutti i 68
# progetti sotto /data/projects/. La baseline KG di sessione 140 ha mostrato
# 18% coverage handoff (128/709), con 67 progetti metadata-only (1billion,
# c&i-tool, oltrepocase, propriofacile, ecc.) completamente invisibili al KG.
#
# Scelta di design:
#   - Flag CLI `--all-projects` su questo script (no nuovo script)
#   - Discovery dinamica riusa `core.scripts.populate_cross_project._discover_all_metadata_slugs`
#     (gia' esistente da Phase 3, 68 slug con project.yaml valido).
#   - Per progetti non-marvisx skip populate_commits / populate_tasks_and_prs:
#     quei populator leggono dal repo marvisx (git log) e tabelle DB filtrate
#     per project. Per i metadata-only progetti basta indicizzare handoff +
#     knowledge docs (dove il valore informativo e' concentrato).
#   - Connessione DB unica condivisa: un solo sqlite3.connect + un solo
#     BEGIN IMMEDIATE per chunk (vs 68 aperture separate). Riuso tuning
#     WAL/FK/busy_timeout.
#   - MAX_TOTAL_NODES_PER_RUN (5000) rimane un warning-only check per-project.
#     Applicato globalmente farebbe scattare il warning a ~6-8 progetti tipici.
#     Per --all-projects ha senso applicarlo per-project (che e' quello che
#     gia' fa populate_artifacts chiamato 1 volta). Aggiungiamo un cap globale
#     soft (MAX_TOTAL_NODES_ALL_PROJECTS) che logga warning, non aborta.

# Cap globale per full-rebuild cross-project. A 68 progetti * 200 node medi =
# ~13.6k; scegliamo 50k per assorbire growth + outlier (marvisx da solo ha
# ~3k node). Warning-only, mai abort: il full-rebuild deve completare sempre
# per evitare stati inconsistenti post-crash.
MAX_TOTAL_NODES_ALL_PROJECTS = 50000


def populate_all_projects(
    db_path: str | None = None,
    projects_root: Path | None = None,
    exclude_projects: frozenset[str] | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    max_commits: int = DEFAULT_MAX_COMMITS,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Phase 6: populate handoff + knowledge_docs per tutti i progetti
    metadata-only sotto `projects_root`.

    Discovery: riusa `_discover_all_metadata_slugs` da populate_cross_project
    (68 slug con project.yaml valido). Per marvisx chiama la full
    `populate_artifacts()` (include commits + tasks/PRs), per gli altri solo
    handoff + knowledge_docs (populate_commits userebbe il git log del
    monorepo, irrilevante per i metadata-only; populate_tasks_and_prs
    filtra per project dalla tabella tasks/pull_requests che e' gia' coperta
    dal run marvisx).

    Idempotency: `populate_handoffs` e `populate_knowledge_docs` sono
    UPSERT-based (vedi chunked_upsert_nodes in _graph_writer.py) — run
    ripetuti producono lo stesso stato finale. Non servono gate aggiuntivi.

    Args:
        db_path: override DB path (default: auto-resolve a /data/pir/console.db)
        projects_root: directory /data/projects (default). Test override via
                       injection di `_discover_all_metadata_slugs` fixture.
        exclude_projects: slug da escludere dal loop. Usato per evitare
                          double-run quando il chiamante ha gia' eseguito
                          `populate_artifacts --project=marvisx` prima.
        since_days: passthrough a populate_commits (solo marvisx)
        max_commits: passthrough a populate_commits (solo marvisx)
        repo_root: passthrough a populate_commits (solo marvisx)

    Returns:
        `{db_path, projects_root, per_project: {slug: {handoffs, knowledge, ...}},
          aggregate: {n_projects, n_handoffs, n_docs, n_edges, n_skipped},
          elapsed_ms}`
    """
    from core.scripts.populate_cross_project import _discover_all_metadata_slugs

    db = _resolve_db_path(db_path)
    root = projects_root or PROJECTS_ROOT
    exclude = exclude_projects or frozenset()

    slugs = _discover_all_metadata_slugs(root)
    # Order deterministico per log readability + reproducibility testuale.
    sorted_slugs = sorted(slugs)
    logger.info(
        "[--all-projects] discovered %d project slugs under %s (excluding %d)",
        len(sorted_slugs), root, len(exclude & slugs),
    )

    t0 = time.perf_counter()
    conn = sqlite3.connect(db)
    per_project: dict[str, dict[str, Any]] = {}
    aggregate = {
        "n_projects": 0,
        "n_projects_skipped": 0,
        "n_handoffs": 0,
        "n_docs": 0,
        "n_xlsx": 0,
        "n_edges": 0,
        "n_skipped_handoffs": 0,
    }
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")

        for slug in sorted_slugs:
            if slug in exclude:
                per_project[slug] = {"skipped_reason": "excluded"}
                aggregate["n_projects_skipped"] += 1
                continue

            metadata_path = root / slug
            if not metadata_path.exists():
                per_project[slug] = {"skipped_reason": "metadata_path_missing"}
                aggregate["n_projects_skipped"] += 1
                continue

            # Phase 6 scope: handoff + knowledge_docs per ogni progetto. NON
            # chiamiamo populate_commits (git log del monorepo, irrilevante
            # per metadata-only) ne' populate_tasks_and_prs (gia' coperto
            # dal run marvisx che ha scope DB-wide per project column).
            # Dipendenza ordine (mirror populate_artifacts): knowledge PRIMA
            # di handoffs per evitare FK failure su edge cites.
            try:
                k_result = populate_knowledge_docs(
                    conn, metadata_path=metadata_path, project=slug,
                )
            except Exception as e:
                logger.warning(
                    "populate_knowledge_docs failed for %s: %s — continuing",
                    slug, e,
                )
                k_result = {"n_nodes": 0, "n_edges": 0, "per_type": {}}

            try:
                h_result = populate_handoffs(
                    conn, metadata_path=metadata_path, project=slug,
                )
            except Exception as e:
                logger.warning(
                    "populate_handoffs failed for %s: %s — continuing",
                    slug, e,
                )
                h_result = {"n_nodes": 0, "n_edges": 0, "n_skipped": 0}

            try:
                x_result = populate_xlsx_artifacts(
                    conn, metadata_path=metadata_path, project=slug,
                )
            except Exception as e:
                logger.warning(
                    "populate_xlsx_artifacts failed for %s: %s — continuing",
                    slug, e,
                )
                x_result = {"n_nodes": 0, "n_edges": 0, "n_skipped": 0}

            per_project[slug] = {
                "project": slug,
                "handoffs": h_result.get("n_nodes", 0),
                "docs": k_result.get("n_nodes", 0),
                "xlsx": x_result.get("n_nodes", 0),
                "edges": (
                    h_result.get("n_edges", 0)
                    + k_result.get("n_edges", 0)
                    + x_result.get("n_edges", 0)
                ),
                "skipped": h_result.get("n_skipped", 0),
                "per_type": k_result.get("per_type", {}),
            }

            aggregate["n_projects"] += 1
            aggregate["n_handoffs"] += per_project[slug]["handoffs"]
            aggregate["n_docs"] += per_project[slug]["docs"]
            aggregate["n_xlsx"] += per_project[slug]["xlsx"]
            aggregate["n_edges"] += per_project[slug]["edges"]
            aggregate["n_skipped_handoffs"] += per_project[slug]["skipped"]

            # Structured log per-project summary (session-check friendly).
            logger.info(
                "[all-projects] %s: %s",
                slug, json.dumps(per_project[slug], default=str),
            )

            # Per-project cap warning (defense in depth).
            project_total = (
                per_project[slug]["handoffs"]
                + per_project[slug]["docs"]
                + per_project[slug]["xlsx"]
            )
            if project_total > MAX_TOTAL_NODES_PER_RUN:
                logger.warning(
                    "project %s produced %d nodes (> per-project cap %d) — verify scope",
                    slug, project_total, MAX_TOTAL_NODES_PER_RUN,
                )
    finally:
        conn.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Global cap warning.
    total_new_nodes = aggregate["n_handoffs"] + aggregate["n_docs"] + aggregate["n_xlsx"]
    if total_new_nodes > MAX_TOTAL_NODES_ALL_PROJECTS:
        logger.warning(
            "[--all-projects] populated %d nodes across %d projects "
            "(> global cap %d) — re-evaluate scope or raise cap",
            total_new_nodes, aggregate["n_projects"],
            MAX_TOTAL_NODES_ALL_PROJECTS,
        )

    return {
        "db_path": db,
        "projects_root": str(root),
        "per_project": per_project,
        "aggregate": aggregate,
        "elapsed_ms": round(elapsed_ms, 2),
    }


# ---- Phase 1 incremental support -------------------------------------------
#
# Phase 1 del plan "KG auto-indexing" aggiunge tre capabilities:
#
# 1. `--incremental <paths...>`: processa N file (handoff o doc) in 1 invocazione
#    invece di full scan. Target <500ms per file singolo. Mirror dell'approccio
#    `ast_parser.py --incremental` (batch fork).
# 2. `--handle-delete`: per file spariti dal filesystem, soft-delete il node
#    (graph_nodes.deprecated_at=now) + DELETE edges src_id OR dst_id del node.
#    Mai hard-delete (pattern Fase 1d temporal: preserva storia).
# 3. File-state hash gate: prima di re-indicizzare, confronta sha256 con
#    file_state.sha256 (migration 074). Skip se invariato (idempotency robusta
#    a git checkout / editor touch che rigenerano mtime).
#
# La funzione `populate_artifacts_incremental()` e' l'entry point per il
# kg-watcher daemon (Phase 2). Skip strutturato (file, reason, fix_hint) →
# JSON in stderr invece di log silent.


def _file_sha256(path: Path) -> str:
    """Streaming sha256 (memory-safe per file grandi)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_state_unchanged(
    conn: sqlite3.Connection,
    path: str,
    sha256: str,
    populator: str = "artifacts",
) -> bool:
    """True se il file e' gia' indicizzato con lo stesso hash per questo
    populator → skip. PK composito (path, populator): artifacts e cross_project
    hanno stati indipendenti.
    """
    row = conn.execute(
        "SELECT sha256 FROM file_state WHERE path=? AND populator=?",
        (path, populator),
    ).fetchone()
    return row is not None and row[0] == sha256


def _file_state_record(
    conn: sqlite3.Connection,
    path: str,
    sha256: str,
    populator: str = "artifacts",
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO file_state(path, populator, sha256, indexed_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(path, populator) DO UPDATE SET "
            "sha256=excluded.sha256, indexed_at=excluded.indexed_at",
            (path, populator, sha256),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _file_state_forget(
    conn: sqlite3.Connection,
    path: str,
    populator: str | None = None,
) -> None:
    """Dimentica lo stato del file. populator=None dimentica per entrambi
    (usato da --handle-delete: un file cancellato e' morto per tutti)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if populator is None:
            conn.execute("DELETE FROM file_state WHERE path=?", (path,))
        else:
            conn.execute(
                "DELETE FROM file_state WHERE path=? AND populator=?",
                (path, populator),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _route_metadata_path(path: Path) -> tuple[str, str, Path] | None:
    """Classifica un path come metadata file.

    Returns `(project_slug, kind, metadata_path)` oppure None se il path
    non e' indicizzabile sotto /data/projects/<slug>/.

    kind ∈ {"handoff", "doc", "context", "xlsx"}. Phase 1 indicizza:
      - <slug>/memory/handoff-*.md          → handoff
      - <slug>/docs/<type>/*.md             → doc (type in DOC_TYPE_DIR_MAP values)
      - <slug>/docs/**/*.xlsx               → xlsx (workbook + sheet nodes)
      - <slug>/context.md                   → context (riconosciuto ma non indicizzato
                                               come graph node; presente per log)
    """
    try:
        resolved = path.resolve() if path.exists() else path.absolute()
        rel = resolved.relative_to(PROJECTS_ROOT)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    slug = parts[0]
    metadata_path = PROJECTS_ROOT / slug
    # context.md
    if len(parts) == 2 and parts[1] == "context.md":
        return (slug, "context", metadata_path)
    # memory/handoff-*.md
    if (
        len(parts) >= 3
        and parts[1] == "memory"
        and parts[-1].startswith("handoff-")
        and parts[-1].endswith(".md")
    ):
        return (slug, "handoff", metadata_path)
    # docs/<type>/*.md
    if (
        len(parts) >= 4
        and parts[1] == "docs"
        and parts[2] in DOC_TYPE_DIR_MAP.values()
        and parts[-1].endswith(".md")
    ):
        return (slug, "doc", metadata_path)
    # docs/**/*.xlsx (binary spreadsheet artifacts approved by ingest saga).
    if (
        len(parts) >= 3
        and parts[1] == "docs"
        and parts[-1].lower().endswith(".xlsx")
    ):
        return (slug, "xlsx", metadata_path)
    return None


def _parse_frontmatter_with_reason(
    path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Wrapper che ritorna (data, structured_skip_reason).

    La versione condivisa `parse_frontmatter()` ritorna None su qualsiasi
    errore. Per --incremental serve distinguere "no frontmatter" da
    "YAML parse error" per generare fix_hint utili.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"unreadable: {e.strerror or str(e)}"
    if not text.startswith("---"):
        return None, "no_frontmatter"
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, "unclosed_frontmatter"
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        return None, f"yaml_error: {str(e)[:100]}"
    if not isinstance(data, dict):
        return None, f"frontmatter_not_dict: got {type(data).__name__}"
    return data, None


def _valid_task_ids_for(conn: sqlite3.Connection, project: str) -> set[str]:
    """Task UUID presenti nel graph (stripped del prefix task:artifact:)."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT REPLACE(id, 'task:artifact:', '') FROM graph_nodes "
            "WHERE type='task' AND project_id=?",
            (project,),
        ).fetchall()
    }


def _valid_learning_ids_for(conn: sqlite3.Connection, project: str) -> set[str]:
    """Learning ids che hanno un node in graph_nodes (FK-safe per edge emission).

    In full-run populate_artifacts prima crea i learning nodes via
    populate_knowledge_docs e poi emette edges. In incremental, invece, un
    singolo file (handoff o doc) non ricostruisce i learning nodes da zero:
    assumere che tutti i learning listati in tabella `learnings` abbiano
    gia' un node in graph_nodes causa FK constraint failed. Filtriamo sul
    node set reale: solo le learnings gia' indicizzate contano come target
    validi per `cites` / `documents`.
    """
    return {
        row[0].removeprefix("learning:artifact:")
        for row in conn.execute(
            "SELECT id FROM graph_nodes WHERE type='learning' AND project_id=?",
            (project,),
        ).fetchall()
    }


def _rel_file_path(path: Path, metadata_path: Path) -> str:
    """File path relativo al parent di metadata_path (matches populate_handoffs)."""
    try:
        return str(path.relative_to(metadata_path.parent.parent))
    except ValueError:
        return str(path)


def _xlsx_candidate_paths(path: Path, metadata_path: Path) -> tuple[str, str]:
    return (_rel_file_path(path, metadata_path), str(path))


def _build_xlsx_artifact_nodes(
    path: Path,
    project: str,
    metadata_path: Path,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Build workbook + sheet KG nodes for one `.xlsx` file."""
    sha = _file_sha256(path)
    parsed = parse_xlsx(path)
    structure = parsed.get("structure") or {}
    sheets = structure.get("sheets") or []
    rel_path = _rel_file_path(path, metadata_path)
    parent_id = _xlsx_workbook_id(sha)

    nodes: list[dict[str, Any]] = [{
        "id": parent_id,
        "type": "file",
        "name": path.name[:120],
        "qualified_name": f"xlsx.{path.stem}",
        "file_path": rel_path,
        "line_number": None,
        "metadata": {
            "artifact_kind": "xlsx_workbook",
            "filename": path.name,
            "sha256": sha,
            "bytes": structure.get("bytes"),
            "sheet_count": structure.get("sheet_count", len(sheets)),
            "streaming": structure.get("streaming"),
            "data_only": structure.get("data_only"),
            "keep_links": structure.get("keep_links"),
            "project": project,
        },
        "project_id": project,
    }]
    edges: list[dict[str, Any]] = []

    for sheet in sheets:
        index = int(sheet.get("index") or len(nodes))
        sheet_name = str(sheet.get("name") or f"Sheet {index}")
        sheet_id = _xlsx_sheet_id(sha, index, sheet_name)
        nodes.append({
            "id": sheet_id,
            "type": "file",
            "name": sheet_name[:120],
            "qualified_name": f"xlsx.{path.stem}.{_safe_slug(sheet_name)}",
            "file_path": rel_path,
            "line_number": None,
            "metadata": {
                "artifact_kind": "xlsx_sheet",
                "filename": path.name,
                "sha256": sha,
                "sheet_name": sheet_name,
                "sheet_index": index,
                "row_count": sheet.get("row_count"),
                "column_count": sheet.get("column_count"),
                "truncated": sheet.get("truncated"),
                "project": project,
            },
            "project_id": project,
        })
        edges.append({
            "source_id": parent_id,
            "target_id": sheet_id,
            "relation": "contains",
            "confidence": 1.0,
            "source": "db",
            "source_file": rel_path,
            "metadata": {
                "artifact_kind": "xlsx_sheet",
                "sheet_name": sheet_name,
                "sheet_index": index,
            },
            "project_id": project,
        })

    return sha, nodes, edges


def _retire_stale_xlsx_nodes_for_path(
    conn: sqlite3.Connection,
    path: Path,
    metadata_path: Path,
    active_node_ids: set[str],
) -> dict[str, int]:
    """Deprecate XLSX nodes for the same path that are no longer active."""
    candidate_paths = _xlsx_candidate_paths(path, metadata_path)
    placeholders = ",".join("?" for _ in candidate_paths)
    existing = {
        row[0]
        for row in conn.execute(
            f"""
            SELECT id
              FROM graph_nodes
             WHERE id LIKE 'xlsx:%'
               AND file_path IN ({placeholders})
               AND deprecated_at IS NULL
            """,
            candidate_paths,
        ).fetchall()
    }
    stale = sorted(existing - active_node_ids)
    if not stale:
        return {"nodes_marked": 0, "edges_deleted": 0}

    conn.execute("BEGIN IMMEDIATE")
    try:
        nodes_marked = 0
        edges_deleted = 0
        for node_id in stale:
            cur = conn.execute(
                "UPDATE graph_nodes SET deprecated_at=datetime('now'), "
                "updated_at=datetime('now') WHERE id=? AND deprecated_at IS NULL",
                (node_id,),
            )
            nodes_marked += cur.rowcount
            cur2 = conn.execute(
                "DELETE FROM graph_edges WHERE source_id=? OR target_id=?",
                (node_id, node_id),
            )
            edges_deleted += cur2.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"nodes_marked": nodes_marked, "edges_deleted": edges_deleted}


def _refresh_xlsx_edges(conn: sqlite3.Connection, node_ids: set[str]) -> int:
    """Delete stale `contains` edges from active XLSX nodes before re-upsert."""
    if not node_ids:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        edges_deleted = 0
        for node_id in sorted(node_ids):
            cur = conn.execute(
                "DELETE FROM graph_edges "
                "WHERE source_id=? AND relation='contains' AND source='db'",
                (node_id,),
            )
            edges_deleted += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return edges_deleted


def _index_xlsx(
    conn: sqlite3.Connection,
    path: Path,
    project: str,
    metadata_path: Path,
) -> dict[str, Any]:
    """Index one `.xlsx` workbook as parent + sheet artifact nodes."""
    try:
        _sha, nodes, edges = _build_xlsx_artifact_nodes(path, project, metadata_path)
    except Exception as exc:
        return {"skipped": {
            "file": str(path),
            "kind": "xlsx",
            "reason": f"xlsx_parse_error: {str(exc)[:160]}",
            "fix_hint": "verify the workbook is a non-macro .xlsx file readable by openpyxl",
        }}

    active_ids = {node["id"] for node in nodes}
    retired = _retire_stale_xlsx_nodes_for_path(conn, path, metadata_path, active_ids)
    _refresh_xlsx_edges(conn, active_ids)
    n_nodes = chunked_upsert_nodes(conn, nodes)
    n_edges = chunked_upsert_edges(conn, edges)
    return {
        "nodes_written": n_nodes,
        "edges_written": n_edges,
        "nodes_marked": retired["nodes_marked"],
        "edges_deleted": retired["edges_deleted"],
    }


def populate_xlsx_artifacts(
    conn: sqlite3.Connection,
    metadata_path: Path | None = None,
    project: str = DEFAULT_PROJECT,
) -> dict[str, int]:
    """Scan docs/ for `.xlsx` files and emit workbook + sheet artifact nodes."""
    base = metadata_path or DEFAULT_METADATA_PATH
    docs_root = Path(base) / "docs"
    if not docs_root.exists():
        return {"n_nodes": 0, "n_edges": 0, "n_skipped": 0}

    nodes_written = 0
    edges_written = 0
    skipped = 0
    for path in sorted(docs_root.rglob("*.xlsx")):
        if path.is_symlink():
            continue
        out = _index_xlsx(conn, path, project, Path(base))
        if out.get("skipped"):
            skipped += 1
            logger.info("xlsx artifact skipped: %s", out["skipped"])
            continue
        nodes_written += out.get("nodes_written", 0)
        edges_written += out.get("edges_written", 0)
    return {
        "n_nodes": nodes_written,
        "n_edges": edges_written,
        "n_skipped": skipped,
    }


def _index_handoff(
    conn: sqlite3.Connection,
    path: Path,
    project: str,
    metadata_path: Path,
) -> dict[str, Any]:
    """Index a single handoff file. Mirror `populate_handoffs` per-file body.

    DELETE edges esistenti con source_id=node_id PRIMA di UPSERT → refresh
    pulito (rinominare tag/cites/task_id riflette correttamente sul grafo).
    """
    data, reason = _parse_frontmatter_with_reason(path)
    if data is None:
        return {"skipped": {
            "file": str(path),
            "kind": "handoff",
            "reason": reason or "no_frontmatter",
            "fix_hint": "add valid YAML frontmatter with task_id",
        }}
    task_id = data.get("task_id")
    if not task_id:
        task_ids_list = data.get("task_ids")
        if isinstance(task_ids_list, list) and task_ids_list:
            task_id = str(task_ids_list[0])
    if not task_id:
        return {"skipped": {
            "file": str(path),
            "kind": "handoff",
            "reason": "no_task_id",
            "fix_hint": "add task_id: <uuid> or task_ids: [<uuid>, ...] to frontmatter",
        }}
    task_id = str(task_id)
    valid_task_ids = _valid_task_ids_for(conn, project)
    if task_id not in valid_task_ids:
        return {"skipped": {
            "file": str(path),
            "kind": "handoff",
            "reason": "task_id_not_in_graph",
            "task_id": task_id,
            "fix_hint": "verify task exists in PiR and re-run populate_tasks_and_prs",
        }}

    valid_learning_ids = _valid_learning_ids_for(conn, project)
    node_id = _handoff_id(path.name)

    nodes = [{
        "id": node_id,
        "type": "handoff",
        "name": path.stem,
        "qualified_name": f"handoff.{path.stem}",
        "file_path": _rel_file_path(path, metadata_path),
        "line_number": None,
        "metadata": {
            "filename": path.name,
            "date": str(data.get("date") or ""),
            "title": str(data.get("title") or "")[:300],
            "session": data.get("session"),
            "tags": data.get("tags") or [],
            "status": data.get("status"),
            "task_id": task_id,
        },
        "project_id": project,
    }]
    edges: list[dict[str, Any]] = [{
        "source_id": node_id,
        "target_id": _task_id(task_id),
        "relation": "describes",
        "confidence": 1.0,
        "source": "frontmatter",
        "source_file": _rel_file_path(path, metadata_path),
        "project_id": project,
    }]
    cites = data.get("cites") or []
    if isinstance(cites, list):
        for cited in cites:
            cited_str = str(cited)
            if cited_str in valid_learning_ids:
                edges.append({
                    "source_id": node_id,
                    "target_id": _learning_id(cited_str),
                    "relation": "cites",
                    "confidence": 1.0,
                    "source": "frontmatter",
                    "project_id": project,
                })

    # Refresh pulito: DELETE edges uscenti dal node prima di UPSERT.
    # `chunked_upsert_edges` ha ON CONFLICT DO UPDATE ma non elimina
    # edges "vecchi" che ora sono spariti dal frontmatter (es. tag rimosso).
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM graph_edges WHERE source_id=? AND source='frontmatter'",
            (node_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    n_nodes = chunked_upsert_nodes(conn, nodes)
    n_edges = chunked_upsert_edges(conn, edges)

    # Anti-zombie B (task e103b1ed): sweep task_ids from frontmatter and
    # auto-close doc/none references. Same contract as the bulk flow.
    closed_tasks: list[str] = []
    try:
        referenced_ids = _extract_handoff_task_ids(data)
        if referenced_ids:
            closed_tasks = _auto_close_handoff_referenced_tasks(
                conn, referenced_ids,
                handoff_project=project,
                handoff_name=path.name,
            )
            if closed_tasks:
                logger.info(
                    "handoff %s: auto-closed %d referenced task(s) "
                    "(trigger=handoff_written): %s",
                    path.name, len(closed_tasks),
                    ", ".join(t[:8] for t in closed_tasks),
                )
    except Exception:
        logger.exception(
            "_index_handoff: closure sweep failed for %s (non-fatal)",
            path.name,
        )

    return {
        "nodes_written": n_nodes,
        "edges_written": n_edges,
        "closed_tasks": closed_tasks,
    }


def _index_doc(
    conn: sqlite3.Connection,
    path: Path,
    project: str,
    metadata_path: Path,
) -> dict[str, Any]:
    """Index a single docs/<type>/*.md file (solution/audit/spike/...).

    Mirror del body interno di `populate_knowledge_docs`. Risolve doc_type
    dal frontmatter `type:` o dalla subdir (es. docs/audits/ → audit).
    """
    data, reason = _parse_frontmatter_with_reason(path)
    if data is None:
        return {"skipped": {
            "file": str(path),
            "kind": "doc",
            "reason": reason or "no_frontmatter",
            "fix_hint": "add valid YAML frontmatter",
        }}
    # Subdir e' parts[2] sotto docs/ (es. docs/solutions/foo.md → 'solutions')
    try:
        rel = path.resolve().relative_to(metadata_path)
    except (ValueError, OSError):
        return {"skipped": {
            "file": str(path),
            "kind": "doc",
            "reason": "path_outside_metadata",
            "fix_hint": "path must be under <metadata_path>/docs/<type>/",
        }}
    if len(rel.parts) < 3 or rel.parts[0] != "docs":
        return {"skipped": {
            "file": str(path),
            "kind": "doc",
            "reason": "path_not_in_docs_subdir",
            "fix_hint": "expected <slug>/docs/<type>/<file>.md",
        }}
    dir_name = rel.parts[1]
    resolved_type = _extract_doc_type(data, dir_name)
    sid = _doc_id(resolved_type, path.name)
    nodes = [{
        "id": sid,
        "type": resolved_type,
        "name": path.stem[:60],
        "qualified_name": f"{resolved_type}.{path.stem}",
        "file_path": _rel_file_path(path, metadata_path),
        "line_number": None,
        "metadata": {
            "filename": path.name,
            "date": str(data.get("date") or ""),
            "title": str(data.get("title") or "")[:300],
            "category": data.get("category"),
            "severity": data.get("severity"),
            "tags": data.get("tags") or [],
            "subdir": dir_name,
        },
        "project_id": project,
    }]
    edges: list[dict[str, Any]] = []
    related = data.get("related_learning") or data.get("learning_id")
    if related:
        related_str = str(related)
        valid_learning_ids = _valid_learning_ids_for(conn, project)
        if related_str in valid_learning_ids:
            edges.append({
                "source_id": sid,
                "target_id": _learning_id(related_str),
                "relation": "documents",
                "confidence": 1.0,
                "source": "frontmatter",
                "project_id": project,
            })

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM graph_edges WHERE source_id=? AND source='frontmatter'",
            (sid,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    n_nodes = chunked_upsert_nodes(conn, nodes)
    n_edges = chunked_upsert_edges(conn, edges)
    return {"nodes_written": n_nodes, "edges_written": n_edges}


def _soft_delete_artifact(
    conn: sqlite3.Connection,
    path: Path,
    kind: str,
) -> dict[str, Any]:
    """Soft-delete: deprecated_at=now + DELETE edges con src/dst del node.

    Mai hard-delete: il node resta con `deprecated_at` settato per
    preservare storia temporal (migration 067 aggiunse il campo proprio
    per questo pattern).
    """
    if kind == "handoff":
        node_id = _handoff_id(path.name)
    elif kind == "doc":
        # Per doc non sappiamo il doc_type dal path "morto" (frontmatter
        # perso). Proviamo tutti i doc_type registrati: il match "giusto"
        # ha deprecated_at settato, altri sono NOP (WHERE id=... filtra).
        pass
    elif kind == "xlsx":
        candidate_paths = [str(path)]
        try:
            route = _route_metadata_path(path)
            if route is not None:
                _slug, _kind, metadata_path = route
                candidate_paths.append(_rel_file_path(path, metadata_path))
        except Exception:
            pass
        placeholders = ",".join("?" for _ in candidate_paths)
        candidate_ids = [
            row[0]
            for row in conn.execute(
                f"""
                SELECT id
                  FROM graph_nodes
                 WHERE id LIKE 'xlsx:%'
                   AND file_path IN ({placeholders})
                """,
                tuple(candidate_paths),
            ).fetchall()
        ]
    elif kind == "context":
        return {"nodes_marked": 0, "edges_deleted": 0, "note": "context_md_not_indexed"}
    else:
        return {"nodes_marked": 0, "edges_deleted": 0}

    if kind == "doc":
        candidate_ids = [_doc_id(dt, path.name) for dt in DOC_TYPE_DIR_MAP.keys()]
    elif kind != "xlsx":
        candidate_ids = [node_id]  # type: ignore[name-defined]

    conn.execute("BEGIN IMMEDIATE")
    try:
        nodes_marked = 0
        edges_deleted = 0
        for cid in candidate_ids:
            cur = conn.execute(
                "UPDATE graph_nodes SET deprecated_at=datetime('now'), "
                "updated_at=datetime('now') "
                "WHERE id=? AND deprecated_at IS NULL",
                (cid,),
            )
            nodes_marked += cur.rowcount
            cur2 = conn.execute(
                "DELETE FROM graph_edges WHERE source_id=? OR target_id=?",
                (cid, cid),
            )
            edges_deleted += cur2.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"nodes_marked": nodes_marked, "edges_deleted": edges_deleted}


def populate_artifacts_incremental(
    paths: list[Path],
    db_path: str | None = None,
    handle_delete: bool = False,
    skip_hash_gate: bool = False,
) -> dict[str, Any]:
    """Entry point del Phase 1 incremental path.

    Args:
        paths: lista di file da indicizzare (assoluti, tipicamente
               `/data/projects/<slug>/memory/handoff-*.md` o
               `<slug>/docs/<type>/*.md`).
        db_path: override DB path (default: prod /data/pir/console.db).
        handle_delete: se True, soft-delete tutti i paths (anche se esistono).
        skip_hash_gate: se True, bypassa file_state hash check (forza re-index).

    Returns:
        `{files_processed, files_skipped_hash_unchanged, files_skipped_unroutable,
          files_deleted, nodes_written, edges_written, skipped: [...], elapsed_ms}`

    Il daemon kg-watcher (Phase 2) chiama questa funzione via subprocess
    con argparse.
    """
    db = _resolve_db_path(db_path)
    t0 = time.perf_counter()
    conn = sqlite3.connect(db)
    results: dict[str, Any] = {
        "files_processed": 0,
        "files_skipped_hash_unchanged": 0,
        "files_skipped_unroutable": 0,
        "files_deleted": 0,
        "nodes_written": 0,
        "edges_written": 0,
        "skipped": [],
    }
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")

        for raw in paths:
            p = Path(raw)
            route = _route_metadata_path(p)
            if route is None:
                results["files_skipped_unroutable"] += 1
                results["skipped"].append({
                    "file": str(p),
                    "reason": "unroutable",
                    "fix_hint": (
                        "path must be under /data/projects/<slug>/memory/handoff-*.md "
                        "or <slug>/docs/<type>/*.md or <slug>/docs/**/*.xlsx "
                        "or <slug>/context.md"
                    ),
                })
                continue
            slug, kind, metadata_path = route

            # Delete path (either explicit flag OR file vanished)
            if handle_delete or not p.exists():
                del_out = _soft_delete_artifact(conn, p, kind)
                results["files_deleted"] += 1
                results["edges_written"] += del_out.get("edges_deleted", 0)
                _file_state_forget(conn, str(p))
                continue

            # Hash gate (skip if content unchanged)
            sha: str | None = None
            if not skip_hash_gate:
                sha = _file_sha256(p)
                if _file_state_unchanged(conn, str(p), sha):
                    results["files_skipped_hash_unchanged"] += 1
                    continue

            # Process by kind
            if kind == "handoff":
                out = _index_handoff(conn, p, slug, metadata_path)
            elif kind == "doc":
                out = _index_doc(conn, p, slug, metadata_path)
            elif kind == "xlsx":
                out = _index_xlsx(conn, p, slug, metadata_path)
            elif kind == "context":
                # context.md non è (ancora) un graph node type. Registriamo solo
                # nel file_state per coerenza con watcher events.
                out = {"nodes_written": 0, "edges_written": 0}
            else:
                continue

            if out.get("skipped"):
                results["skipped"].append(out["skipped"])
                continue
            results["files_processed"] += 1
            results["nodes_written"] += out.get("nodes_written", 0)
            results["edges_written"] += out.get("edges_written", 0)

            if sha is not None:
                _file_state_record(conn, str(p), sha)

    finally:
        conn.close()

    results["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return results


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="KG Fase 1c — populate artifact nodes (task/PR/commit/handoff/solution/learning)"
    )
    ap.add_argument("--db", default=None, help="SQLite DB path (default: auto-resolve)")
    ap.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help=f"Project slug to populate (default: {DEFAULT_PROJECT})",
    )
    ap.add_argument(
        "--metadata-path",
        default=str(DEFAULT_METADATA_PATH),
        help="Project metadata path (handoffs/solutions)",
    )
    ap.add_argument(
        "--since-days",
        type=int,
        default=DEFAULT_SINCE_DAYS,
        help="Git log window for commits",
    )
    ap.add_argument(
        "--max-commits",
        type=int,
        default=DEFAULT_MAX_COMMITS,
        help="Hard cap on commit nodes per run",
    )
    ap.add_argument("--skip-commits", action="store_true")
    ap.add_argument("--skip-tasks-prs", action="store_true")
    ap.add_argument("--skip-handoffs", action="store_true")
    ap.add_argument("--skip-knowledge", action="store_true")
    ap.add_argument("--skip-xlsx", action="store_true")
    ap.add_argument(
        "--repo-root",
        default=None,
        help=(
            "Git repo root for `git log` scan in populate_commits "
            "(default: scripts/.. — works in dev). In prod il populator gira "
            "da /data/pir/ che NON e' git repo, quindi serve "
            "--repo-root ~/workspace per popolare commit nodes."
        ),
    )
    # Phase 1 incremental flags
    ap.add_argument(
        "--incremental",
        nargs="+",
        metavar="PATH",
        help="Process N paths in a single invocation instead of a full scan. "
             "Each PATH must be under /data/projects/<slug>/ (memory/, docs/, "
             "or context.md). Mirrors scripts.ast_parser --incremental.",
    )
    ap.add_argument(
        "--handle-delete",
        action="store_true",
        help="Soft-delete nodes (deprecated_at=now) + DELETE edges for --incremental "
             "paths. Use when files have been removed from disk.",
    )
    ap.add_argument(
        "--skip-hash-gate",
        action="store_true",
        help="Bypass file_state content-hash skip (force re-index even if sha256 "
             "unchanged). Used by full-rebuild scripts.",
    )
    # Phase 6 flags (cross-project coverage)
    ap.add_argument(
        "--all-projects",
        action="store_true",
        help="Phase 6: dopo il run --project=marvisx (o al posto), indicizza "
             "handoff + knowledge_docs per ogni slug in /data/projects/<slug>/ "
             "con project.yaml valido (~68 progetti). commits/tasks/PRs NON "
             "sono chiamati per i progetti non-marvisx (scope metadata-only). "
             "Idempotente: UPSERT-based, safe da ri-eseguire. Il cap globale "
             "MAX_TOTAL_NODES_ALL_PROJECTS=50000 e' warning-only.",
    )
    ap.add_argument(
        "--exclude-projects",
        default="",
        help="Phase 6: lista comma-separated di slug da escludere dal loop "
             "--all-projects (es. --exclude-projects=marvisx per evitare "
             "double-run quando il progetto principale e' gia' stato processato).",
    )

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    if args.incremental:
        out = populate_artifacts_incremental(
            paths=[Path(p) for p in args.incremental],
            db_path=args.db,
            handle_delete=args.handle_delete,
            skip_hash_gate=args.skip_hash_gate,
        )
        # Structured output: stdout = summary JSON. Skipped details go to stderr
        # so the daemon parser can route warnings without losing the main JSON.
        if out.get("skipped"):
            print(
                json.dumps({"skipped": out["skipped"]}, indent=2, default=str),
                file=sys.stderr,
            )
        print(json.dumps(out, indent=2, default=str))
        return 0

    if args.all_projects:
        # Phase 6: --all-projects e' uno scope-only flag. Non chiama il
        # populate_artifacts per-project (che include commits/tasks/prs che
        # richiedono il monorepo marvisx). Il chiamante tipico e'
        # kg_full_rebuild.sh che esegue PRIMA `populate_artifacts` (single
        # project default=marvisx) e POI `populate_artifacts --all-projects
        # --exclude-projects=marvisx` per riempire il gap metadata-only.
        exclude = frozenset(
            s.strip() for s in args.exclude_projects.split(",") if s.strip()
        )
        out = populate_all_projects(
            db_path=args.db,
            exclude_projects=exclude,
            since_days=args.since_days,
            max_commits=args.max_commits,
            repo_root=Path(args.repo_root) if args.repo_root else None,
        )
        print(json.dumps(out, indent=2, default=str))
        return 0

    out = populate_artifacts(
        db_path=args.db,
        project=args.project,
        metadata_path=Path(args.metadata_path),
        since_days=args.since_days,
        max_commits=args.max_commits,
        skip_commits=args.skip_commits,
        skip_tasks_prs=args.skip_tasks_prs,
        skip_handoffs=args.skip_handoffs,
        skip_knowledge=args.skip_knowledge,
        skip_xlsx=args.skip_xlsx,
        repo_root=Path(args.repo_root) if args.repo_root else None,
    )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
