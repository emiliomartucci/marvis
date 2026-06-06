#!/usr/bin/env python3
# v1.0.1 - 2026-04-24 - Fix type column to match NODE_PREFIXES (hotfix migration 091)
"""Populate inbox:artifact:<id> graph_nodes from inbox_items with
treatment IN ('save', 'read_save') + refers_to edges to project super-nodes
via static topic → project mapping.

## Scope

Indicizza solo la coda "save" dell'inbox (~429 rows in prod, vs ~8543
read/ignore esclusi). Ogni saved item produce:
  - 1 nodo `inbox:artifact:{sanitized_id}` con metadata (topic, treatment, url, hash)
  - N edge `refers_to` verso project super-nodes mappati dal topic

## Single-writer contract (PAT-2)

sqlite3.connect() sync, foreign_keys=ON, busy_timeout=30000.
UPSERT idempotente (ON CONFLICT node id, DELETE+INSERT edges).

## Hash-gate (incremental)

Salta UPSERT se `metadata.hash == sha256_16(f"{treatment}|{updated_at}")`
del nodo esistente. Alla variazione di treatment o updated_at, l'hash
cambia e il nodo viene riscritto + edges rinfrescati.

## Lifecycle cleanup

Nodi `inbox:artifact:*` gia' in DB il cui item corrispondente NON ha piu'
`treatment IN ('save','read_save')` vengono eliminati (FK cascade sulle
edges). Copre il caso: utente cambia treatment save → ignore → il nodo
sparisce dal KG alla prossima run.

## Topic → project mapping

Default map links only topics that point at this instance's own project.
Deployment-specific links are supplied via the MARVIS_INBOX_TOPIC_MAP_FILE
env var (JSON {topic: [slug, ...]}). See `_load_topic_project_map`.

## Invocation

    python3 scripts/populate_inbox_nodes.py              # dry-run (default)
    python3 scripts/populate_inbox_nodes.py --confirm    # actually write
    python3 scripts/populate_inbox_nodes.py --db ./test.db

## Exit codes

    0 = success
    1 = SQL error (rollback triggered)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger("populate_inbox_nodes")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = "/data/pir/console.db"

# ---------------------------------------------------------------------------
# Sanitizer — mirror of populate_project_nodes._project_node_id helper. MUST
# stay in sync with NODE_ID_PATTERN ([a-zA-Z0-9_\-.]+) in graph_service.py.
# ---------------------------------------------------------------------------
_SLUG_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


def _sanitize_slug(raw: str) -> str:
    """Replace any char outside NODE_ID_PATTERN with `_`."""
    return _SLUG_SAFE_RE.sub("_", raw)


def _project_node_id(slug: str) -> str:
    """Build a NODE_ID_PATTERN-compliant `project:artifact:<safe>` id."""
    return f"project:artifact:{_sanitize_slug(slug)}"


# ---------------------------------------------------------------------------
# Topic → project slug mapping.
#
# Deployment-specific: which inbox topics link to which project super-nodes is
# data, not logic. Default maps only the topics that point at this instance's
# own project (`marvisx`); additional topic→project links are supplied per
# deployment via the MARVIS_INBOX_TOPIC_MAP_FILE env var (JSON object of
# {topic: [slug, ...]}). Edge targets that don't exist in the KG are skipped
# safely, so an empty or partial map never breaks the run.
#
# Slugs may use `&` per filesystem convention; `_project_node_id` sanitizes
# them to match canonical super-node ids.
# ---------------------------------------------------------------------------
_DEFAULT_TOPIC_PROJECT_MAP: dict[str, tuple[str, ...]] = {
    "tooling": ("marvisx",),
    "security-devtools": ("marvisx",),
    "ai-products": ("marvisx",),
    "ai-news": ("marvisx",),
    "general": (),  # no project links by design
}


def _load_topic_project_map() -> dict[str, tuple[str, ...]]:
    """Return the default map merged with an optional deployment override file."""
    result: dict[str, tuple[str, ...]] = dict(_DEFAULT_TOPIC_PROJECT_MAP)
    override_path = os.environ.get("MARVIS_INBOX_TOPIC_MAP_FILE")
    if override_path and Path(override_path).is_file():
        try:
            data = json.loads(Path(override_path).read_text())
            if isinstance(data, dict):
                for topic, slugs in data.items():
                    if isinstance(slugs, (list, tuple)):
                        result[str(topic)] = tuple(str(s) for s in slugs)
        except (ValueError, OSError) as exc:
            logger.warning("inbox topic map override unreadable (%s): %s", override_path, exc)
    return result


TOPIC_PROJECT_MAP: dict[str, tuple[str, ...]] = _load_topic_project_map()


def _resolve_db_path(explicit: str | None = None) -> str:
    """Default to prod DB if present; else repo-local fallback."""
    if explicit:
        return explicit
    prod = Path(DEFAULT_DB)
    if prod.exists():
        return str(prod)
    return str(REPO_ROOT / "console.db")


def _compute_hash(treatment: str, updated_at: str) -> str:
    """16-char hash identifying (treatment, updated_at) pair for idempotency gate."""
    return hashlib.sha256(f"{treatment}|{updated_at}".encode()).hexdigest()[:16]


def _extract_hash(metadata_json: str | None) -> str | None:
    """Read `hash` key from a graph_nodes.metadata JSON blob (None if missing/invalid)."""
    if not metadata_json:
        return None
    try:
        obj = json.loads(metadata_json)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict):
        h = obj.get("hash")
        if isinstance(h, str):
            return h
    return None


def populate_inbox_nodes(db: str, confirm: bool = False) -> int:
    """Upsert inbox nodes + edges. Returns 0 on success, 1 on SQL error."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")

        # 1. Fetch saved inbox items
        saved = conn.execute(
            "SELECT id, COALESCE(title, '[untitled]'), topic, url, treatment, updated_at "
            "FROM inbox_items "
            "WHERE treatment IN ('save', 'read_save')"
        ).fetchall()

        # 2. Fetch existing inbox nodes with their metadata (hash-gate + lifecycle)
        existing_rows = conn.execute(
            "SELECT id, metadata FROM graph_nodes WHERE id LIKE 'inbox:artifact:%'"
        ).fetchall()
        existing_inbox_hashes: dict[str, str | None] = {
            row[0]: _extract_hash(row[1]) for row in existing_rows
        }
        existing_inbox_ids = set(existing_inbox_hashes.keys())

        # 3. Fetch project super-node set (validate edge targets exist)
        project_rows = conn.execute(
            "SELECT id FROM graph_nodes WHERE id LIKE 'project:artifact:%'"
        ).fetchall()
        existing_projects = {r[0] for r in project_rows}

        saved_node_ids: set[str] = set()
        upserted = 0
        skipped_hash = 0
        edges_inserted = 0
        edges_skipped_missing_project = 0
        expected_edges_total = 0  # counter for dry-run reporting

        # 4. Upsert saved items + edges
        for (item_id, title, topic, url, treatment, updated_at) in saved:
            node_id = f"inbox:artifact:{_sanitize_slug(item_id)}"
            saved_node_ids.add(node_id)

            new_hash = _compute_hash(treatment or "", updated_at or "")
            prior_hash = existing_inbox_hashes.get(node_id)

            # Count expected edges regardless of hash-gate (dry-run visibility)
            expected_edges_total += len(TOPIC_PROJECT_MAP.get(topic, ()))

            # Hash-gate: skip entirely if (treatment, updated_at) unchanged
            if prior_hash is not None and prior_hash == new_hash:
                skipped_hash += 1
                continue

            metadata_json = json.dumps(
                {
                    "inbox_item_id": item_id,
                    "topic": topic,
                    "treatment": treatment,
                    "url": url,
                    "hash": new_hash,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            qualified_name = f"inbox.{_sanitize_slug(item_id)}"

            if confirm:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        INSERT INTO graph_nodes
                            (id, type, name, qualified_name, metadata, last_seen_at)
                        VALUES (?, 'inbox', ?, ?, ?, datetime('now'))
                        ON CONFLICT(id) DO UPDATE SET
                            name = excluded.name,
                            qualified_name = excluded.qualified_name,
                            metadata = excluded.metadata,
                            last_seen_at = datetime('now'),
                            updated_at = datetime('now')
                        """,
                        (node_id, (title or "[untitled]")[:200], qualified_name, metadata_json),
                    )
                    # Refresh refers_to edges (idempotent)
                    conn.execute(
                        "DELETE FROM graph_edges "
                        "WHERE source_id = ? AND relation = 'refers_to'",
                        (node_id,),
                    )
                    for target_slug in TOPIC_PROJECT_MAP.get(topic, ()):
                        target_id = _project_node_id(target_slug)
                        if target_id not in existing_projects:
                            edges_skipped_missing_project += 1
                            logger.warning(
                                "Skip edge %s -> %s (project super-node missing)",
                                node_id,
                                target_id,
                            )
                            continue
                        conn.execute(
                            """
                            INSERT INTO graph_edges
                                (source_id, target_id, relation, confidence, source,
                                 metadata, created_at, first_seen_at, last_seen_at)
                            VALUES (?, ?, 'refers_to', 0.7, 'manual', ?,
                                    datetime('now'), datetime('now'), datetime('now'))
                            ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                                last_seen_at = datetime('now'),
                                metadata = excluded.metadata
                            """,
                            (
                                node_id,
                                target_id,
                                json.dumps(
                                    {"via_topic": topic},
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                            ),
                        )
                        edges_inserted += 1
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    logger.error("Failed to upsert inbox node %s: %s", node_id, e)
                    return 1

            upserted += 1

        # 5. Lifecycle cleanup: remove inbox nodes for items no longer saved.
        orphan_inbox = existing_inbox_ids - saved_node_ids
        if orphan_inbox:
            logger.info(
                "Lifecycle: %d inbox nodes to delete (treatment changed away from save*)",
                len(orphan_inbox),
            )
            if confirm:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    placeholders = ",".join("?" * len(orphan_inbox))
                    conn.execute(
                        f"DELETE FROM graph_nodes WHERE id IN ({placeholders})",
                        tuple(orphan_inbox),
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    logger.error("Failed lifecycle delete: %s", e)
                    return 1

        lifecycle_deleted = len(orphan_inbox)

        if confirm:
            msg = (
                f"[populate_inbox_nodes] committed: upserted={upserted}, "
                f"skipped_hash={skipped_hash}, edges_inserted={edges_inserted}, "
                f"edges_skipped_missing_project={edges_skipped_missing_project}, "
                f"lifecycle_deleted={lifecycle_deleted}"
            )
            logger.info(msg)
            print(msg, file=sys.stderr)
        else:
            msg = (
                f"[populate_inbox_nodes] dry-run: would upsert {upserted} nodes "
                f"(skipped_hash={skipped_hash}) + ~{expected_edges_total} edges "
                f"(topic-mapped), lifecycle_delete {lifecycle_deleted}"
            )
            logger.info(msg)
            print(msg, file=sys.stderr)
        return 0

    except sqlite3.Error as e:
        logger.error("SQL error: %s", e)
        return 1
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        description="Populate inbox:artifact super-nodes for saved items + refers_to edges."
    )
    parser.add_argument(
        "--db",
        default=None,
        help="DB path (default: /data/pir/console.db if exists, else repo-local console.db)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually write. Without this flag, dry-run only.",
    )
    args = parser.parse_args()
    db_path = _resolve_db_path(args.db)
    logger.info("DB: %s, confirm: %s", db_path, args.confirm)
    return populate_inbox_nodes(db=db_path, confirm=args.confirm)


if __name__ == "__main__":
    sys.exit(main())
