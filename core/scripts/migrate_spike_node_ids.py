#!/usr/bin/env python3
# v1.0.0 - 2026-04-14 - KG Fase 1a: migrate spike node_ids (pre-Fase 1a) to py: namespace
"""
One-shot migration: legacy spike node_ids (spike commit 28864df, 270 nodi)
usano il formato `function:api.db.get_db`. Fase 1a introduce il prefix
`py:` / `ts:` per supportare cross-language senza collisioni.

Questo script:
1. UPDATE graph_nodes: id senza prefix → 'py:' + id
2. UPDATE graph_edges: source_id / target_id senza prefix → 'py:' + id
3. Ricrea vincoli FK (ON DELETE CASCADE resta intatto perche' aggiorniamo tutti gli ID in un'unica transazione)

Idempotente: re-run non produce effetti (la WHERE esclude id gia' prefissati).

Single-writer note: vedi docstring di `scripts/ast_parser.py` (stesso contratto).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_db_path(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    prod = Path("/data/pir/console.db")
    if prod.exists():
        return str(prod)
    return str(REPO_ROOT / "console.db")


def migrate(db_path: str) -> dict:
    """Run the legacy-id migration. Returns counts of rows touched."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")  # disable during the batch update
        conn.execute("PRAGMA busy_timeout=15000")

        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM graph_nodes "
                "WHERE id NOT LIKE 'py:%' AND id NOT LIKE 'ts:%'"
            )
            legacy_nodes = cur.fetchone()[0]

            cur = conn.execute(
                "SELECT COUNT(*) FROM graph_edges "
                "WHERE source_id NOT LIKE 'py:%' AND source_id NOT LIKE 'ts:%'"
            )
            legacy_edges_src = cur.fetchone()[0]

            cur = conn.execute(
                "SELECT COUNT(*) FROM graph_edges "
                "WHERE target_id NOT LIKE 'py:%' AND target_id NOT LIKE 'ts:%'"
            )
            legacy_edges_tgt = cur.fetchone()[0]

            if legacy_nodes == 0 and legacy_edges_src == 0 and legacy_edges_tgt == 0:
                conn.commit()
                return {
                    "db_path": db_path,
                    "legacy_nodes_found": 0,
                    "legacy_edges_source_found": 0,
                    "legacy_edges_target_found": 0,
                    "migrated": False,
                    "message": "No legacy ids found (already migrated or empty graph).",
                }

            # Edges first so FK references remain in sync at each step (we
            # keep foreign_keys OFF during the batch, but the order still
            # matters for reasoning about partial failure rollback).
            conn.execute(
                "UPDATE graph_edges SET source_id = 'py:' || source_id "
                "WHERE source_id NOT LIKE 'py:%' AND source_id NOT LIKE 'ts:%'"
            )
            conn.execute(
                "UPDATE graph_edges SET target_id = 'py:' || target_id "
                "WHERE target_id NOT LIKE 'py:%' AND target_id NOT LIKE 'ts:%'"
            )
            conn.execute(
                "UPDATE graph_nodes SET id = 'py:' || id, updated_at = datetime('now') "
                "WHERE id NOT LIKE 'py:%' AND id NOT LIKE 'ts:%'"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

        return {
            "db_path": db_path,
            "legacy_nodes_found": legacy_nodes,
            "legacy_edges_source_found": legacy_edges_src,
            "legacy_edges_target_found": legacy_edges_tgt,
            "migrated": True,
            "message": "Legacy spike ids migrated to py: namespace.",
        }
    finally:
        conn.close()


def _main() -> int:
    ap = argparse.ArgumentParser(description="KG Fase 1a node_id legacy migration")
    ap.add_argument("--db", default=None, help="Path to SQLite DB (default: auto-resolve)")
    args = ap.parse_args()
    db_path = _resolve_db_path(args.db)
    result = migrate(db_path)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
