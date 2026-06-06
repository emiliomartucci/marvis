#!/usr/bin/env python3
# v1.0.0 - 2026-04-14 - KG Fase 1e: touch counter (code churn) populator
"""Populate touch counter columns on graph_nodes from a single git log scan.

## Performance contract

Same batched pattern as `scripts/populate_temporal.py` (validated at ~5-10s on
MarvisX — 234x faster than per-file git log). We run ONE `git log --all
--name-only --format='__COMMIT__ <sha> <ct> <email>'` call, parse the stream
in memory, and aggregate per-file touch data:

- `touch_count_total`: number of commits that touched the file
- `touch_count_7d`: subset with committer timestamp >= now - 7d
- `touch_count_30d`: subset with committer timestamp >= now - 30d
- `touch_authors`: set of distinct author emails (JSON array)
- `touch_last_at`: max committer timestamp (ISO)

## File-level propagation (plan scope)

File touch data is propagated to every code node (`function|file|module`)
sharing the same `file_path`. Function-level precision (`git log -L`) is
deferred — file-level covers 80% of the "which area of the codebase is hot"
use case and keeps the populator bounded.

## Windows

Cut-offs use Python `datetime.now(tz=UTC)` — we do NOT query the DB for
"now" to keep the populator DB-agnostic (it runs before any API connection).
Git committer timestamps (`%ct`) are epoch seconds, timezone-agnostic.

## Single-writer contract

Mirrors `scripts/ast_parser.py` / `scripts/populate_temporal.py`: opens its
own `sqlite3.Connection`, BEGIN IMMEDIATE per write chunk, safe because the
API pool is read-only (`PRAGMA query_only=ON`).

## Invocation

    python -m core.scripts.populate_touch_counter                       # auto-resolve DB
    python -m core.scripts.populate_touch_counter --db /tmp/x.db
    python -m core.scripts.populate_touch_counter --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("populate_touch_counter")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Same scope as populate_temporal: the three areas the AST parser indexes.
SCAN_PATHS: tuple[str, ...] = ("api/", "console/src/", "scripts/")

# Only code nodes get touch data propagated. Artifacts (task/pr/commit/...) are
# immutable records — their "touch count" isn't a useful churn signal.
CODE_NODE_TYPES: tuple[str, ...] = ("function", "file", "module")

_COMMIT_HEADER_RE = re.compile(r"^__COMMIT__ ([0-9a-f]{7,40}) (\d+) (.+)$")


# ---------------------------------------------------------------------------
# Git scanning (single batch call)
# ---------------------------------------------------------------------------


def get_all_file_touches(
    repo_root: Path = REPO_ROOT,
    paths: tuple[str, ...] = SCAN_PATHS,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Single `git log` call aggregated to per-file touch counters.

    Returns `{rel_path: {total, d7, d30, authors, last_at}}` where:
    - total: int — commits touching the file
    - d7 / d30: int — subset within the rolling window
    - authors: set[str] — distinct author emails (becomes a sorted list later)
    - last_at: str — ISO timestamp of the most recent commit

    `now` is injectable for tests; defaults to `datetime.now(UTC)`.

    The order of commits in the output doesn't matter — we aggregate per file
    regardless of oldest/newest first.
    """
    now = now or datetime.now(tz=timezone.utc)
    cutoff_7d = int((now - timedelta(days=7)).timestamp())
    cutoff_30d = int((now - timedelta(days=30)).timestamp())

    t0 = time.perf_counter()
    cmd = [
        "git",
        "-C",
        str(repo_root),
        "log",
        "--all",
        "--name-only",
        "--format=__COMMIT__ %H %ct %aE",
        "--",
        *paths,
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("git log timed out after 120s — returning empty data")
        return {}
    except subprocess.CalledProcessError as e:
        logger.error("git log failed: %s", e.stderr[:500])
        return {}

    result: dict[str, dict[str, Any]] = {}
    cur_sha: str | None = None
    cur_ts: int | None = None
    cur_email: str | None = None

    for line in proc.stdout.splitlines():
        if not line:
            continue
        m = _COMMIT_HEADER_RE.match(line)
        if m:
            cur_sha = m.group(1)
            cur_ts = int(m.group(2))
            cur_email = m.group(3).strip().lower()
            continue
        if cur_sha is None or cur_ts is None or cur_email is None:
            # stray line before first commit header — ignore
            continue
        file_path = line.strip()
        if not file_path:
            continue
        entry = result.get(file_path)
        if entry is None:
            entry = {
                "total": 0,
                "d7": 0,
                "d30": 0,
                "authors": set(),
                "last_ts": 0,
            }
            result[file_path] = entry
        entry["total"] += 1
        if cur_ts >= cutoff_7d:
            entry["d7"] += 1
        if cur_ts >= cutoff_30d:
            entry["d30"] += 1
        entry["authors"].add(cur_email)
        if cur_ts > entry["last_ts"]:
            entry["last_ts"] = cur_ts

    # Normalise: convert authors set → sorted list, last_ts → ISO string.
    for path, entry in result.items():
        entry["authors"] = sorted(entry["authors"])
        if entry["last_ts"]:
            entry["last_at"] = datetime.fromtimestamp(
                entry["last_ts"], tz=timezone.utc
            ).isoformat(sep=" ", timespec="seconds")
        else:
            entry["last_at"] = None
        del entry["last_ts"]

    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "git log aggregated %d files in %.0fms", len(result), elapsed_ms
    )
    return result


# ---------------------------------------------------------------------------
# DB path resolution (mirror of populate_temporal)
# ---------------------------------------------------------------------------


def _resolve_db_path(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    prod = Path("/data/pir/console.db")
    if prod.exists():
        return str(prod)
    return str(REPO_ROOT / "console.db")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def populate_touches(
    db_path: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Backfill touch counters on every code node whose `file_path` is in git.

    Strategy:
    1. `get_all_file_touches()` — single git log scan
    2. SELECT id, file_path FROM graph_nodes WHERE type IN (code types)
       AND file_path IS NOT NULL
    3. Build UPDATE tuples (total, d7, d30, authors_json, last_at, id)
    4. Single batched executemany inside BEGIN IMMEDIATE.

    Nodes whose `file_path` has no git history (new files uncommitted, paths
    outside SCAN_PATHS) are not updated: their defaults (0/0/0/'[]'/NULL from
    migration 068) stand as "no touch data".

    Returns a measurements dict with counts + elapsed_ms.
    """
    db = _resolve_db_path(db_path)
    t0 = time.perf_counter()

    file_data = get_all_file_touches(
        repo_root=repo_root or REPO_ROOT,
        now=now,
    )

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.row_factory = sqlite3.Row

        type_placeholders = ",".join(["?"] * len(CODE_NODE_TYPES))
        cur = conn.execute(
            f"SELECT id, file_path FROM graph_nodes "
            f"WHERE type IN ({type_placeholders}) "
            f"AND file_path IS NOT NULL",
            CODE_NODE_TYPES,
        )
        rows = cur.fetchall()

        updates: list[tuple[int, int, int, str, str | None, str]] = []
        matched = 0
        missing = 0
        for r in rows:
            d = file_data.get(r["file_path"])
            if d is None:
                missing += 1
                continue
            matched += 1
            updates.append(
                (
                    d["total"],
                    d["d7"],
                    d["d30"],
                    json.dumps(d["authors"]),
                    d["last_at"],
                    r["id"],
                )
            )

        nodes_updated = 0
        if updates and not dry_run:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.executemany(
                    """
                    UPDATE graph_nodes
                       SET touch_count_total = ?,
                           touch_count_7d = ?,
                           touch_count_30d = ?,
                           touch_authors = ?,
                           touch_last_at = ?,
                           updated_at = datetime('now')
                     WHERE id = ?
                    """,
                    updates,
                )
                nodes_updated = cur.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "db_path": db,
        "git_files": len(file_data),
        "code_nodes_total": len(rows),
        "code_nodes_matched": matched,
        "code_nodes_missing": missing,
        "nodes_updated": nodes_updated,
        "dry_run": dry_run,
        "elapsed_ms": round(elapsed_ms, 2),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="KG Fase 1e touch counter populator (code churn + bus factor)"
    )
    ap.add_argument("--db", default=None, help="Path to SQLite DB (default: auto-resolve)")
    ap.add_argument(
        "--repo-root",
        default=None,
        help=(
            "Git repo root for `git log` scan (default: scripts/.. — works "
            "in dev. Bug fix Phase 3: in prod il populator gira da /data/pir/ "
            "che NON e' git repo, quindi serve --repo-root ~/workspace)."
        ),
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out = populate_touches(
        db_path=args.db,
        dry_run=args.dry_run,
        repo_root=Path(args.repo_root) if args.repo_root else None,
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
